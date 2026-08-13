import importlib.util
import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.progress import parse_progress_line


def load_adapter():
    path = Path(__file__).parents[2] / "scripts" / "seedvr2-adapter.py"
    spec = importlib.util.spec_from_file_location("seedvr2_adapter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_fork_progress_reporter():
    for parent in Path(__file__).parents:
        path = parent / "ComfyUI-SeedVR2_VideoUpscaler" / "src" / "cli_progress.py"
        if path.is_file():
            break
    else:
        raise AssertionError("pinned SeedVR2 fork ProgressReporter is unavailable")
    spec = importlib.util.spec_from_file_location("seedvr2_cli_progress", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProgressReporter


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
    assert command.count("--cache_dit") == 1
    assert command.count("--cache_vae") == 1
    assert "--use_cache" not in command
    assert "--cache_model" not in command
    assert "--cache_device" not in command


def test_official_command_opts_into_jsonl_progress(tmp_path):
    adapter = load_adapter()
    command = adapter.build_seedvr2_command(
        input_path=tmp_path / "input.mp4",
        output_path=tmp_path / "out.mp4",
        model_dir=tmp_path / "models",
        model_name="3b.safetensors",
        preset="3b-safe",
        color_correction="lab",
        source_width=1920,
        source_height=1080,
        output_scale=1.0,
        python="python",
        official_cli=tmp_path / "inference_cli.py",
    )

    assert command[command.index("--progress_format") + 1] == "jsonl"


def test_adapter_bridges_only_safe_fork_json_events(capsys):
    adapter = load_adapter()
    safe = json.dumps(
        {
            "schema_version": 1,
            "sequence": 1,
            "work_sequence": 0,
            "measured_work": False,
            "event_type": "heartbeat",
            "elapsed_seconds": 2.0,
        }
    )
    leaked = json.dumps(
        {
            "schema_version": 1,
            "sequence": 2,
            "work_sequence": 0,
            "measured_work": False,
            "event_type": "heartbeat",
            "input_path": "/Users/private/movie.mp4",
        }
    )

    adapter.forward_seedvr2_line(safe)
    adapter.forward_seedvr2_line(leaked)

    output = capsys.readouterr().out.splitlines()
    assert output == [
        'EVENT {"schema_version":1,"sequence":1,"work_sequence":0,'
        '"measured_work":false,"event_type":"heartbeat",'
        '"elapsed_seconds":2.0}'
    ]


def test_adapter_bridges_every_event_shape_emitted_by_actual_fork_reporter(capsys):
    adapter = load_adapter()
    reporter_type = load_fork_progress_reporter()
    stream = io.StringIO()
    reporter = reporter_type(
        progress_format="jsonl",
        stream=stream,
        clock=lambda: 100.0,
        heartbeat_interval=3600,
    )
    reporter.start()
    reporter.emit("model_preparation_started", phase="preparing")
    reporter.emit("model_preparation_completed", phase="preparing")
    reporter.begin_file()
    reporter.emit(
        "chunk_started",
        phase="encoding",
        chunk_index=1,
        chunk_count=1,
        chunk_unique_frames=5,
        chunk_context_frames=0,
        completed_unique_frames=0,
        total_unique_frames=5,
    )
    callback = reporter.phase_callback(
        chunk_index=1,
        chunk_count=1,
        chunk_unique_frames=5,
        chunk_context_frames=0,
        completed_unique_frames=0,
        total_unique_frames=5,
    )
    callback(1, 1, 5, "Phase 1: Encoding")
    callback(1, 1, 5, "Phase 2: Upscaling")
    callback(1, 1, 5, "Phase 3: Decoding")
    callback(1, 1, 5, "Phase 4: Post-processing")
    reporter.emit(
        "heartbeat",
        phase="postprocessing",
        current_unit=1,
        total_units=1,
        current_frames=5,
        chunk_index=1,
        chunk_count=1,
        chunk_unique_frames=5,
        chunk_context_frames=0,
        completed_unique_frames=0,
        total_unique_frames=5,
    )
    reporter.mark_output_started(
        chunk_index=1,
        chunk_count=1,
        chunk_unique_frames=5,
        chunk_context_frames=0,
        completed_unique_frames=0,
        total_unique_frames=5,
    )
    reporter.emit(
        "chunk_completed",
        phase="postprocessing",
        chunk_index=1,
        chunk_count=1,
        chunk_unique_frames=5,
        chunk_context_frames=0,
        completed_unique_frames=5,
        total_unique_frames=5,
    )
    reporter.emit("completed", phase="completed")
    reporter.close()
    actual_events = [json.loads(line) for line in stream.getvalue().splitlines()]

    for event in actual_events:
        assert "current_frames" in event
        adapter.forward_seedvr2_line(json.dumps(event))
    leaked = {**actual_events[0], "input_path": "/Users/private/movie.mp4"}
    unknown = {**actual_events[0], "event_type": "debug_dump"}
    path_phase = {**actual_events[0], "phase": "/Users/private"}
    adapter.forward_seedvr2_line(json.dumps(leaked))
    adapter.forward_seedvr2_line(json.dumps(unknown))
    adapter.forward_seedvr2_line(json.dumps(path_phase))

    bridged = capsys.readouterr().out.splitlines()
    assert len(bridged) == len(actual_events) == 11
    assert all(parse_progress_line(line) is not None for line in bridged)
    payloads = [json.loads(line.removeprefix("EVENT ")) for line in bridged]
    assert [payload["event_type"] for payload in payloads] == [
        "model_preparation_started",
        "model_preparation_completed",
        "chunk_started",
        "phase_progress",
        "phase_progress",
        "phase_progress",
        "phase_progress",
        "heartbeat",
        "output_started",
        "chunk_completed",
        "completed",
    ]
    assert all("current_frames" not in payload for payload in payloads)
    assert payloads[0].get("phase") is None
    assert payloads[8].get("phase") is None
    assert payloads[9]["chunk_unique_frames"] == 0
    assert payloads[9]["completed_unique_frames"] == 5
    assert payloads[10].get("phase") is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "sequence": 1,
            "work_sequence": 0,
            "measured_work": False,
            "event_type": "heartbeat",
            "debug": "safe-looking but untrusted",
        },
        {
            "schema_version": 1,
            "sequence": True,
            "work_sequence": 0,
            "measured_work": False,
            "event_type": "heartbeat",
        },
        {
            "schema_version": 1,
            "sequence": 1,
            "work_sequence": 0,
            "measured_work": True,
            "event_type": "heartbeat",
        },
        {
            "schema_version": 1,
            "sequence": 1,
            "work_sequence": 0,
            "measured_work": True,
            "event_type": "phase_progress",
            "phase": "encoding",
        },
        '{"schema_version":1,"sequence":1,"sequence":2,'
        '"work_sequence":0,"measured_work":false,"event_type":"heartbeat"}',
    ],
)
def test_adapter_drops_untrusted_or_incomplete_json_instead_of_promoting_it(
    capsys, payload
):
    adapter = load_adapter()

    encoded = payload if isinstance(payload, str) else json.dumps(payload)
    adapter.forward_seedvr2_line(encoded)

    assert capsys.readouterr().out == ""


def test_adapter_flushes_harmless_human_output_immediately():
    adapter = load_adapter()

    with patch("builtins.print") as output:
        adapter.forward_seedvr2_line("model-loading\n")

    output.assert_called_once_with("model-loading", flush=True)


def test_adapter_replaces_cuda_install_advice_with_mps_status(capsys):
    adapter = load_adapter()

    with patch.object(adapter.sys, "platform", "darwin"):
        adapter.forward_seedvr2_line(
            "⚠️  SeedVR2 optimizations check: SageAttention ❌ | "
            "Flash Attention ❌ | Triton ❌\n"
        )
        adapter.forward_seedvr2_line(
            "💡 For best performance: pip install sageattention flash-attn triton\n"
        )

    assert capsys.readouterr().out == (
        "Apple MPS uses PyTorch SDPA; CUDA-only SageAttention, Flash Attention, "
        "and Triton are not applicable.\n"
    )


def test_adapter_preserves_harmless_non_event_json_as_human_output(capsys):
    adapter = load_adapter()
    human = '{"level":"info","message":"model loaded"}'

    adapter.forward_seedvr2_line(human)

    assert capsys.readouterr().out == human + "\n"


def test_adapter_drops_non_event_json_containing_a_private_path(capsys):
    adapter = load_adapter()
    leaked = '{"level":"debug","message":"opened /Users/private/movie.mp4"}'

    adapter.forward_seedvr2_line(leaked)

    assert capsys.readouterr().out == ""


def test_adapter_drops_oversized_jsonl_without_leaking_tail(capsys):
    adapter = load_adapter()
    oversized = '{"schema_version":1,"debug":"' + "x" * (
        adapter.MAX_OUTPUT_LINE_CHARS + 1
    ) + '/Users/private/movie.mp4"}'

    adapter.forward_seedvr2_line(oversized)

    assert capsys.readouterr().out == ""


def test_run_command_discards_oversized_jsonl_tail_without_leaking_path(capsys):
    adapter = load_adapter()
    oversized = '{"schema_version":1,"debug":"' + "x" * (
        adapter.MAX_OUTPUT_LINE_CHARS + 1
    ) + '/Users/private/movie.mp4"}\nmodel-ready\n'
    process = SimpleNamespace(
        stdout=io.StringIO(oversized),
        returncode=0,
        poll=lambda: 0,
        wait=lambda: 0,
        terminate=lambda: None,
    )

    with patch.object(adapter.subprocess, "Popen", return_value=process), patch.object(
        adapter.signal, "signal", return_value=adapter.signal.SIG_DFL
    ):
        adapter.run_command(["seedvr2"])

    assert capsys.readouterr().out == "model-ready\n"


def test_run_command_discards_oversized_cr_split_tail_without_leaking_path(capsys):
    adapter = load_adapter()
    prefix = "{" + "x" * (adapter.MAX_OUTPUT_LINE_CHARS - 1) + "\r"
    assert len(prefix) == adapter.MAX_OUTPUT_LINE_CHARS + 1
    process = SimpleNamespace(
        stdout=io.StringIO(prefix + "/Users/private/movie.mp4\nmodel-ready\n"),
        returncode=0,
        poll=lambda: 0,
        wait=lambda: 0,
        terminate=lambda: None,
    )

    with patch.object(adapter.subprocess, "Popen", return_value=process), patch.object(
        adapter.signal, "signal", return_value=adapter.signal.SIG_DFL
    ):
        adapter.run_command(["seedvr2"])

    assert capsys.readouterr().out == "model-ready\n"


def test_run_command_preserves_raw_cr_when_discarding_real_child_output(capsys):
    adapter = load_adapter()
    script = (
        "import os; "
        f"os.write(1, b'{{' + b'x' * {adapter.MAX_OUTPUT_LINE_CHARS - 1} + "
        "b'\\r/Users/private/movie.mp4\\nmodel-ready\\n')"
    )

    adapter.run_command([sys.executable, "-c", script])

    assert capsys.readouterr().out == "model-ready\n"


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
