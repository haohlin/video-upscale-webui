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
    "3b-fp8-fast": "VIDEO_UPSCALE_SEEDVR2_3B_MODEL",
    "7b-fp8-quality": "VIDEO_UPSCALE_SEEDVR2_7B_FP8_MODEL",
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
    "3b-fp8-fast": {
        "batch_size": "5",
        "chunk_size": "25",
        "temporal_overlap": "4",
    },
    "7b-fp8-quality": {
        "batch_size": "5",
        "chunk_size": "25",
        "temporal_overlap": "4",
    },
}
MAX_PROBE_OUTPUT_BYTES = 64 * 1024
MAX_OUTPUT_LINE_CHARS = 64 * 1024
MAX_COUNTER = 1_000_000_000
FORK_EVENT_KEYS = (
    "schema_version",
    "sequence",
    "work_sequence",
    "measured_work",
    "event_type",
    "phase",
    "current_unit",
    "total_units",
    "current_frames",
    "chunk_index",
    "chunk_count",
    "completed_unique_frames",
    "chunk_unique_frames",
    "chunk_context_frames",
    "total_unique_frames",
    "elapsed_seconds",
)
CANONICAL_EVENT_KEYS = tuple(
    key for key in FORK_EVENT_KEYS if key != "current_frames"
)
FORK_EVENT_TYPES = frozenset(
    {
        "model_preparation_started",
        "model_preparation_completed",
        "chunk_started",
        "phase_progress",
        "chunk_completed",
        "heartbeat",
        "output_started",
        "completed",
    }
)
FORK_PHASES = frozenset(
    {
        "preparing",
        "encoding",
        "upscaling",
        "decoding",
        "postprocessing",
        "output",
        "completed",
    }
)
CANONICAL_PHASES = frozenset(
    {"encoding", "upscaling", "decoding", "postprocessing"}
)
FORK_COUNTER_KEYS = (
    "sequence",
    "work_sequence",
    "current_unit",
    "total_units",
    "current_frames",
    "chunk_index",
    "chunk_count",
    "completed_unique_frames",
    "chunk_unique_frames",
    "chunk_context_frames",
    "total_unique_frames",
)
HUMAN_JSON_KEYS = frozenset({"level", "message"})
HUMAN_JSON_LEVELS = frozenset({"debug", "info", "warning", "error"})
SENSITIVE_HUMAN_MARKERS = ("/", "\\", "..")
CUDA_OPTIMIZATION_STATUS = (
    "⚠️  SeedVR2 optimizations check: SageAttention ❌ | "
    "Flash Attention ❌ | Triton ❌"
)
CUDA_OPTIMIZATION_ADVICE = (
    "💡 For best performance: pip install sageattention flash-attn triton"
)
MPS_OPTIMIZATION_STATUS = (
    "Apple MPS uses PyTorch SDPA; CUDA-only SageAttention, Flash Attention, "
    "and Triton are not applicable."
)


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing runtime environment: {name}")
    return value


def emit_progress(percent: int, stage: str) -> None:
    print(f"PROGRESS {percent} {stage}", flush=True)


def _valid_fork_event(payload: dict[str, object]) -> bool:
    if set(payload) - set(FORK_EVENT_KEYS):
        return False
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        return False
    if type(payload.get("measured_work")) is not bool:
        return False
    event_type = payload.get("event_type")
    if type(event_type) is not str or event_type not in FORK_EVENT_TYPES:
        return False
    phase = payload.get("phase")
    if phase is not None and (type(phase) is not str or phase not in FORK_PHASES):
        return False
    for key in FORK_COUNTER_KEYS:
        value = payload.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_COUNTER):
            return False
    sequence = payload.get("sequence")
    work_sequence = payload.get("work_sequence")
    if type(sequence) is not int or type(work_sequence) is not int:
        return False
    if work_sequence > sequence:
        return False
    current_unit = payload.get("current_unit")
    total_units = payload.get("total_units")
    if current_unit is not None and total_units is not None and current_unit > total_units:
        return False
    chunk_index = payload.get("chunk_index")
    chunk_count = payload.get("chunk_count")
    if chunk_count and (
        type(chunk_index) is not int
        or chunk_index < 1
        or chunk_index > chunk_count
    ):
        return False
    elapsed_seconds = payload.get("elapsed_seconds")
    if elapsed_seconds is not None and (
        type(elapsed_seconds) not in {int, float}
        or not math.isfinite(elapsed_seconds)
        or not 0 <= elapsed_seconds <= MAX_COUNTER
    ):
        return False

    measured_work = payload["measured_work"]
    completed_unique_frames = payload.get("completed_unique_frames")
    chunk_unique_frames = payload.get("chunk_unique_frames")
    chunk_context_frames = payload.get("chunk_context_frames")
    total_unique_frames = payload.get("total_unique_frames")
    if event_type == "phase_progress":
        required = (
            phase,
            current_unit,
            total_units,
            chunk_index,
            chunk_count,
            completed_unique_frames,
            chunk_unique_frames,
            chunk_context_frames,
            total_unique_frames,
        )
        if any(value is None for value in required):
            return False
        assert type(total_units) is int
        assert type(chunk_unique_frames) is int
        assert type(total_unique_frames) is int
        assert type(completed_unique_frames) is int
        if (
            not measured_work
            or total_units <= 0
            or chunk_index <= 0
            or chunk_count <= 0
            or chunk_unique_frames <= 0
            or total_unique_frames <= 0
            or completed_unique_frames + chunk_unique_frames > total_unique_frames
        ):
            return False
    elif event_type == "chunk_completed":
        required = (
            chunk_index,
            chunk_count,
            completed_unique_frames,
            chunk_unique_frames,
            total_unique_frames,
        )
        if any(value is None for value in required):
            return False
        assert type(chunk_unique_frames) is int
        assert type(completed_unique_frames) is int
        assert type(total_unique_frames) is int
        if (
            not measured_work
            or chunk_index <= 0
            or chunk_count <= 0
            or chunk_unique_frames <= 0
            or total_unique_frames <= 0
            or completed_unique_frames < chunk_unique_frames
            or completed_unique_frames > total_unique_frames
        ):
            return False
    elif event_type == "heartbeat" and measured_work:
        return False
    return True


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _safe_human_json(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != HUMAN_JSON_KEYS:
        return False
    level = payload.get("level")
    message = payload.get("message")
    return bool(
        type(level) is str
        and level in HUMAN_JSON_LEVELS
        and type(message) is str
        and 0 < len(message) <= MAX_OUTPUT_LINE_CHARS
        and not any(marker in message for marker in SENSITIVE_HUMAN_MARKERS)
    )


def forward_seedvr2_line(line: str) -> None:
    stripped = line.rstrip("\r\n")
    if os.environ.get("VIDEO_UPSCALE_DEVICE_BACKEND_CLASS") == "apple-mps":
        if stripped == CUDA_OPTIMIZATION_STATUS:
            print(MPS_OPTIMIZATION_STATUS, flush=True)
            return
        if stripped == CUDA_OPTIMIZATION_ADVICE:
            return
    if len(stripped) > MAX_OUTPUT_LINE_CHARS:
        if stripped.lstrip().startswith(("{", "[")):
            return
        print(stripped[:MAX_OUTPUT_LINE_CHARS], flush=True)
        return
    try:
        payload = json.loads(
            stripped,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, RecursionError, ValueError):
        if stripped.lstrip().startswith(("{", "[")):
            return
        print(stripped, flush=True)
        return
    if _safe_human_json(payload):
        print(stripped, flush=True)
        return
    if not isinstance(payload, dict) or not _valid_fork_event(payload):
        return
    canonical = {
        key: payload[key] for key in CANONICAL_EVENT_KEYS if key in payload
    }
    if canonical.get("phase") not in CANONICAL_PHASES:
        canonical.pop("phase", None)
    if canonical.get("chunk_index") == 0 and canonical.get("chunk_count") == 0:
        canonical.pop("chunk_index", None)
        canonical.pop("chunk_count", None)
    if canonical.get("event_type") == "chunk_completed":
        canonical["chunk_unique_frames"] = 0
    print(
        "EVENT " + json.dumps(canonical, separators=(",", ":"), allow_nan=False),
        flush=True,
    )


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
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert process.stdout is not None

    def terminate_child(signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.terminate()
        raise SystemExit(128 + signum)

    previous_term = signal.signal(signal.SIGTERM, terminate_child)
    previous_int = signal.signal(signal.SIGINT, terminate_child)
    try:
        while line := process.stdout.readline(MAX_OUTPUT_LINE_CHARS + 1):
            text_line = (
                line.decode("utf-8", errors="replace")
                if isinstance(line, bytes)
                else line
            )
            forward_seedvr2_line(text_line)
            newline = b"\n" if isinstance(line, bytes) else "\n"
            if len(line) > MAX_OUTPUT_LINE_CHARS and not line.endswith(newline):
                while remainder := process.stdout.readline(MAX_OUTPUT_LINE_CHARS + 1):
                    newline = b"\n" if isinstance(remainder, bytes) else "\n"
                    if remainder.endswith(newline):
                        break
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
    source_width: int,
    source_height: int,
    output_scale: float,
    *,
    minimum: int = 256,
) -> int:
    target_width = max(2, int(source_width * output_scale / 2 + 0.5) * 2)
    target_height = max(2, int(source_height * output_scale / 2 + 0.5) * 2)
    shortest = min(target_width, target_height)
    if shortest < minimum:
        raise ValueError(f"Target shortest edge must be at least {minimum} pixels")
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
    mode: str = "full",
    python: str,
    official_cli: Path,
) -> list[str]:
    parameters = PROFILE_PARAMETERS[preset]
    resolution = target_short_side(
        source_width,
        source_height,
        output_scale,
        minimum=16 if mode == "preflight" else 256,
    )
    command = [
        python,
        str(official_cli),
        str(input_path),
        "--output",
        str(output_path),
        "--output_format",
        "mp4",
        "--progress_format",
        "jsonl",
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
        "--cache_dit",
        "--cache_vae",
        "--temporal_overlap",
        parameters["temporal_overlap"],
        "--color_correction",
        color_correction,
        "--vae_encode_tiled",
        "--vae_decode_tiled",
    ]
    if preset in {"3b-fp8-fast", "7b-fp8-quality"}:
        command.extend(
            [
                "--cuda_device",
                "0",
                "--attention_mode",
                "sdpa",
                "--dit_offload_device",
                "cpu",
                "--vae_offload_device",
                "cpu",
            ]
        )
    if preset == "7b-fp8-quality":
        command.extend(["--blocks_to_swap", "32", "--swap_io_components"])
    return command


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
        mode=args.mode,
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
