import importlib.util
import io
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def load_adapter():
    path = Path(__file__).parents[2] / "scripts" / "seedvr2-adapter.py"
    spec = importlib.util.spec_from_file_location("seedvr2_adapter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_adapter_streams_short_output_before_child_exit():
    """Waiting for an 8 KiB buffer must not hide SeedVR2's live output."""
    adapter = load_adapter()
    output_seen = threading.Event()
    output_seen_early: list[bool] = []

    def capture_print(*args, **_kwargs):
        if args and "model-loading" in str(args[0]):
            output_seen.set()

    def observe() -> None:
        output_seen_early.append(output_seen.wait(0.75))

    with patch("builtins.print", side_effect=capture_print):
        observer = threading.Thread(target=observe)
        observer.start()
        adapter.run_command(
            [
                sys.executable,
                "-c",
                "import time; print('model-loading', flush=True); time.sleep(1.5)",
            ]
        )
        observer.join(timeout=2)

    assert not observer.is_alive()
    assert output_seen_early == [True]


@pytest.mark.parametrize(
    ("scale", "expected_short_side"),
    [(0.25, 540), (0.5, 1080), (1.0, 2160), (2.0, 4320)],
)
def test_adapter_uses_selected_seedvr2_target_short_side(
    tmp_path, scale, expected_short_side
):
    """Ignoring selected restoration scale must make this test fail."""
    adapter = load_adapter()
    command = adapter.build_seedvr2_command(
        input_path=tmp_path / "input.mp4",
        output_path=tmp_path / "video-only.mp4",
        model_dir=tmp_path / "models",
        model_name="three-b.safetensors",
        preset="3b-safe",
        color_correction="lab",
        source_width=2160,
        source_height=3840,
        output_scale=scale,
        python="seed-python",
        official_cli=tmp_path / "inference_cli.py",
    )

    assert command[command.index("--resolution") + 1] == str(expected_short_side)
    assert "--use_cache" not in command
    assert "--cache_model" not in command
    assert "--cache_device" not in command


def test_final_mp4_contract_is_hevc_main10_and_transcodes_audio(tmp_path):
    """Dropping HEVC Main10 or AAC audio output guarantees must make this test fail."""
    adapter = load_adapter()
    command = adapter.final_mp4_command(
        video=tmp_path / "video-only.mp4",
        source=tmp_path / "source.mov",
        output=tmp_path / "output.mp4",
        ffmpeg="ffmpeg",
    )

    assert command[command.index("-c:v") + 1] == "libx265"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p10le"
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert command[command.index("-c:a") + 1] == "aac"
    input_indexes = [index for index, item in enumerate(command) if item == "-i"]
    assert len(input_indexes) == 2
    for input_index in input_indexes:
        assert command[input_index - 2 : input_index] == ["-protocol_whitelist", "file"]


def test_ffmpeg_progress_reports_measured_finalization_from_92_through_99(tmp_path):
    """Opaque remux execution must make this test fail."""
    adapter = load_adapter()
    command = adapter.final_mp4_command(
        video=tmp_path / "video-only.mp4",
        source=tmp_path / "source.mov",
        output=tmp_path / "output.mp4",
        ffmpeg="ffmpeg",
    )
    process = SimpleNamespace(
        stdout=io.StringIO(
            "out_time_us=0\nout_time_us=50000000\nout_time_us=100000000\nprogress=end\n"
        ),
        returncode=0,
        wait=lambda: 0,
    )
    reports: list[tuple[int, str]] = []

    with patch.object(adapter.subprocess, "Popen", return_value=process), patch.object(
        adapter, "emit_progress", side_effect=lambda percent, stage: reports.append((percent, stage))
    ):
        adapter.run_ffmpeg_with_progress(command, duration_seconds=100)

    percentages = [percent for percent, _stage in reports]
    assert percentages == sorted(percentages)
    assert percentages[0] == 92
    assert 95 in percentages
    assert percentages[-1] == 99
    assert command[command.index("-progress") + 1] == "pipe:1"


def test_adapter_public_parser_rejects_unimplemented_realesrgan_profile():
    """Advertising unavailable Real-ESRGAN from shipped adapter must make this test fail."""
    adapter = load_adapter()

    try:
        adapter.parser().parse_args(
            [
                "--input", "input.mp4", "--output", "output.mp4", "--preset", "realesrgan-conservative",
                "--color-correction", "lab", "--mode", "full", "--model-dir", "models",
                "--output-scale", "1", "--duration-seconds", "1",
            ]
        )
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("unimplemented Real-ESRGAN profile was accepted")


def test_adapter_public_parser_accepts_scale_and_positive_finite_duration():
    """Rejecting runner-provided scale or duration must make this test fail."""
    adapter = load_adapter()

    args = adapter.parser().parse_args(
        [
            "--input", "input.mp4", "--output", "output.mp4", "--preset", "3b-safe",
            "--color-correction", "lab", "--mode", "full", "--model-dir", "models",
            "--output-scale", "0.5", "--duration-seconds", "3.5",
        ]
    )

    assert args.output_scale == 0.5
    assert args.duration_seconds == 3.5


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--output-scale", "0.3"),
        ("--duration-seconds", "0"),
        ("--duration-seconds", "-1"),
        ("--duration-seconds", "nan"),
        ("--duration-seconds", "inf"),
    ],
)
def test_adapter_public_parser_rejects_unsafe_scale_or_duration(option, value):
    """Unsafe scale or duration reaching processing must make this test fail."""
    adapter = load_adapter()
    arguments = [
        "--input", "input.mp4", "--output", "output.mp4", "--preset", "3b-safe",
        "--color-correction", "lab", "--mode", "full", "--model-dir", "models",
        "--output-scale", "0.5", "--duration-seconds", "3.5",
    ]
    arguments[arguments.index(option) + 1] = value

    with pytest.raises(SystemExit) as error:
        adapter.parser().parse_args(arguments)

    assert error.value.code == 2


def test_adapter_rejects_target_resolution_above_safety_limit(tmp_path):
    """Forwarding an extreme source resolution into SeedVR2 must make this test fail."""
    adapter = load_adapter()

    try:
        adapter.build_seedvr2_command(
            input_path=tmp_path / "input.mp4",
            output_path=tmp_path / "output.mp4",
            model_dir=tmp_path / "models",
            model_name="three-b.safetensors",
            preset="3b-safe",
            color_correction="lab",
            source_width=8192,
            source_height=4320,
            output_scale=2.0,
            python="python",
            official_cli=tmp_path / "cli.py",
        )
    except ValueError as error:
        assert str(error) == "Target longest edge must not exceed 7680 pixels"
    else:
        raise AssertionError("extreme target resolution was accepted")
