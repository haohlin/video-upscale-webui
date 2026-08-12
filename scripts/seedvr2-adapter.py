#!/usr/bin/env python3
"""Stable app-to-official SeedVR2 CLI adapter. No shell execution."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


PRESETS = {
    "3b-safe": "VIDEO_UPSCALE_SEEDVR2_3B_MODEL",
    "7b-fp8-experimental": "VIDEO_UPSCALE_SEEDVR2_7B_FP8_MODEL",
}

PROFILE_PARAMETERS = {
    "3b-safe": {
        "batch_size": "5",
        "chunk_size": "25",
        "temporal_overlap": "4",
    },
    "7b-fp8-experimental": {
        "batch_size": "5",
        "chunk_size": "25",
        "temporal_overlap": "4",
    },
}
MAX_PROBE_OUTPUT_BYTES = 64 * 1024
MAX_OUTPUT_LINE_CHARS = 64 * 1024


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing runtime environment: {name}")
    return value


def emit_progress(percent: int, stage: str) -> None:
    print(f"PROGRESS {percent} {stage}", flush=True)


def positive_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def source_dimensions(path: Path, ffprobe: str) -> tuple[int, int]:
    command = [
        ffprobe,
        "-protocol_whitelist",
        "file",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            command,
            check=False,
            stdout=output,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError("Could not read source dimensions")
        if output.tell() > MAX_PROBE_OUTPUT_BYTES:
            raise RuntimeError("Source metadata exceeds safety limit")
        output.seek(0)
        streams = json.loads(output.read(MAX_PROBE_OUTPUT_BYTES + 1)).get("streams", [])
    if not streams:
        raise RuntimeError("No video stream found")
    width, height = int(streams[0]["width"]), int(streams[0]["height"])
    if width <= 0 or height <= 0:
        raise RuntimeError("Invalid source dimensions")
    return width, height


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Video Upscale WebUI SeedVR2 runtime adapter",
        allow_abbrev=False,
    )
    argument_parser.add_argument("--input", required=True, type=Path)
    argument_parser.add_argument("--output", required=True, type=Path)
    argument_parser.add_argument("--preset", required=True, choices=tuple(PRESETS))
    argument_parser.add_argument("--color-correction", required=True, choices=("lab", "none"))
    argument_parser.add_argument("--mode", required=True, choices=("preflight", "full"))
    argument_parser.add_argument("--model-dir", required=True, type=Path)
    argument_parser.add_argument(
        "--output-scale", required=True, type=float, choices=(0.25, 0.5, 1.0, 2.0)
    )
    argument_parser.add_argument(
        "--duration-seconds", required=True, type=positive_finite_float
    )
    return argument_parser


def run_command(command: list[str]) -> None:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None

    def terminate_child(signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.terminate()
        raise SystemExit(128 + signum)

    previous_term = signal.signal(signal.SIGTERM, terminate_child)
    previous_int = signal.signal(signal.SIGINT, terminate_child)
    try:
        while line := process.stdout.readline(MAX_OUTPUT_LINE_CHARS + 1):
            print(line, end="", flush=True)
        if process.wait() != 0:
            raise RuntimeError(f"SeedVR2 CLI exited with {process.returncode}")
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        if process.poll() is None:
            process.terminate()


def final_mp4_command(
    *,
    video: Path,
    source: Path,
    output: Path,
    ffmpeg: str,
    audio_codec: str = "aac",
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file",
        "-i",
        str(video),
        "-protocol_whitelist",
        "file",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-map_metadata",
        "1",
        "-c:v",
        "libx265",
        "-profile:v",
        "main10",
        "-pix_fmt",
        "yuv420p10le",
        "-tag:v",
        "hvc1",
        "-crf",
        "16",
        "-preset",
        "medium",
        "-c:a",
        audio_codec,
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        str(output),
    ]


def run_ffmpeg_with_progress(command: list[str], duration_seconds: float) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    last_percent = 92
    emit_progress(last_percent, "audio-remux")
    while line := process.stdout.readline(MAX_OUTPUT_LINE_CHARS + 1):
        bounded = line[:MAX_OUTPUT_LINE_CHARS].strip()
        if bounded.startswith("out_time_us="):
            try:
                seconds = max(0.0, int(bounded.split("=", 1)[1]) / 1_000_000)
            except ValueError:
                continue
            percent = min(
                99,
                92 + int(7 * min(1.0, seconds / duration_seconds)),
            )
            if percent > last_percent:
                last_percent = percent
                emit_progress(percent, "audio-remux")
        elif bounded == "progress=end" and last_percent < 99:
            last_percent = 99
            emit_progress(99, "audio-remux")
    if process.wait() != 0:
        raise subprocess.CalledProcessError(process.returncode, command)


def remux_audio(
    video: Path,
    source: Path,
    output: Path,
    ffmpeg: str,
    duration_seconds: float,
) -> None:
    """Keep MP4-compatible source audio; use AAC only when copy cannot mux."""
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_ffmpeg_with_progress(
            final_mp4_command(
                video=video,
                source=source,
                output=output,
                ffmpeg=ffmpeg,
                audio_codec="copy",
            ),
            duration_seconds,
        )
    except subprocess.CalledProcessError:
        output.unlink(missing_ok=True)
        run_ffmpeg_with_progress(
            final_mp4_command(
                video=video,
                source=source,
                output=output,
                ffmpeg=ffmpeg,
                audio_codec="aac",
            ),
            duration_seconds,
        )


def target_short_side(
    source_width: int, source_height: int, output_scale: float
) -> int:
    target_width = max(2, int(source_width * output_scale / 2 + 0.5) * 2)
    target_height = max(2, int(source_height * output_scale / 2 + 0.5) * 2)
    shortest = min(target_width, target_height)
    if shortest < 256:
        raise ValueError("Target shortest edge must be at least 256 pixels")
    if max(target_width, target_height) > 7680:
        raise ValueError("Target longest edge must not exceed 7680 pixels")
    if target_width * target_height > 33_177_600:
        raise ValueError("Target pixel count must not exceed 33177600 pixels")
    return shortest


def build_seedvr2_command(
    *,
    input_path: Path,
    output_path: Path,
    model_dir: Path,
    model_name: str,
    preset: str,
    color_correction: str,
    source_width: int,
    source_height: int,
    output_scale: float,
    python: str,
    official_cli: Path,
) -> list[str]:
    parameters = PROFILE_PARAMETERS[preset]
    resolution = target_short_side(source_width, source_height, output_scale)
    return [
        python,
        str(official_cli),
        str(input_path),
        "--output",
        str(output_path),
        "--output_format",
        "mp4",
        "--video_backend",
        "ffmpeg",
        "--10bit",
        "--model_dir",
        str(model_dir),
        "--dit_model",
        model_name,
        "--resolution",
        str(resolution),
        "--batch_size",
        parameters["batch_size"],
        "--uniform_batch_size",
        "--chunk_size",
        parameters["chunk_size"],
        "--temporal_overlap",
        parameters["temporal_overlap"],
        "--color_correction",
        color_correction,
        "--vae_encode_tiled",
        "--vae_decode_tiled",
    ]


def main() -> int:
    args = parser().parse_args()
    if not args.input.is_file():
        raise RuntimeError(f"Input does not exist: {args.input}")
    official_cli = Path(required_environment("VIDEO_UPSCALE_SEEDVR2_OFFICIAL_CLI"))
    if not official_cli.is_file():
        raise RuntimeError(f"Official SeedVR2 CLI does not exist: {official_cli}")
    ffmpeg = required_environment("VIDEO_UPSCALE_FFMPEG")
    ffprobe = required_environment("VIDEO_UPSCALE_FFPROBE")
    model_name = required_environment(PRESETS[args.preset])
    width, height = source_dimensions(args.input, ffprobe)
    temporary_output = args.output.with_name(f"{args.output.stem}.video-only.mp4")
    temporary_output.unlink(missing_ok=True)
    args.output.unlink(missing_ok=True)

    command = build_seedvr2_command(
        input_path=args.input,
        output_path=temporary_output,
        model_dir=args.model_dir,
        model_name=model_name,
        preset=args.preset,
        color_correction=args.color_correction,
        source_width=width,
        source_height=height,
        output_scale=args.output_scale,
        python=sys.executable,
        official_cli=official_cli,
    )
    emit_progress(5, "seedvr2-start")
    try:
        run_command(command)
        if not temporary_output.is_file():
            raise RuntimeError("SeedVR2 did not create a video output")
        remux_audio(
            temporary_output,
            args.input,
            args.output,
            ffmpeg,
            args.duration_seconds,
        )
        if not args.output.is_file():
            raise RuntimeError("FFmpeg did not create final MP4")
    finally:
        temporary_output.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SeedVR2 adapter error: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
