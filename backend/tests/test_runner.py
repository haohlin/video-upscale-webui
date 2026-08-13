import io
import json
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from app.config import Settings
from app.domain import Job
from app.media import MAX_PROBE_OUTPUT_BYTES, SubprocessMediaProbe
from app.progress import ProgressReport
from app.runner import JobCancelled, SubprocessRunner


def test_runner_maps_cuda_presets_to_existing_persistent_model_names(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(tmp_path / "adapter.py"),
        seedvr2_model_dir=tmp_path / "models",
        python=sys.executable,
        app_port=8765,
        disk_reserve_gb=0,
        default_profile="7b-fp8-quality",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        device_backend_class="nvidia-cuda",
    )

    assert SubprocessRunner._model_for_preset(settings, "7b-fp8-quality") == settings.seedvr2_7b_fp8_model
    assert SubprocessRunner._model_for_preset(settings, "3b-fp8-fast") == settings.seedvr2_3b_model


def test_runner_streams_short_progress_before_child_exit(tmp_path):
    """Waiting for an 8 KiB buffer must not hide live progress from the UI."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python=sys.executable,
        app_port=8765,
        disk_reserve_gb=0,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)
    progress_seen = threading.Event()
    cancel = threading.Event()
    worker_error: list[BaseException] = []

    def report_progress(report: ProgressReport) -> None:
        if (report.percent, report.stage) == (5, "loading"):
            progress_seen.set()

    def run() -> None:
        try:
            runner._run_process(
                [
                    sys.executable,
                    "-c",
                    "import time; print('PROGRESS 5 loading', flush=True); time.sleep(2)",
                ],
                None,
                report_progress,
                cancel.is_set,
                invocation="full",
            )
        except JobCancelled:
            pass
        except BaseException as error:
            worker_error.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert progress_seen.wait(0.75)
    finally:
        cancel.set()
        thread.join(timeout=4)

    assert not thread.is_alive()
    assert worker_error == []


def test_structured_progress_sequence_and_rate_limit_are_scoped_per_invocation(tmp_path):
    """Sequence regression or event floods must not cross invocation boundaries."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python=sys.executable,
        app_port=8765,
        disk_reserve_gb=0,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)

    def event(sequence, event_type="heartbeat"):
        return "EVENT " + json.dumps(
            {
                "schema_version": 1,
                "sequence": sequence,
                "work_sequence": 0,
                "measured_work": False,
                "event_type": event_type,
                "elapsed_seconds": 1.0,
            }
        )

    preflight_lines = [event(2, "output_started"), event(1)]
    preflight_lines.extend(event(sequence) for sequence in range(3, 23))

    class Process:
        pid = 1234
        returncode = 0

        def __init__(self, lines):
            self.stdout = io.StringIO("\n".join(lines) + "\n")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    reports: list[ProgressReport] = []
    with patch(
        "app.runner.subprocess.Popen",
        side_effect=[Process(preflight_lines), Process([event(1)])],
    ), patch("app.runner.time.monotonic", return_value=100.0):
        runner._run_process(
            ["adapter"],
            None,
            reports.append,
            lambda: False,
            invocation="preflight",
        )
        runner._run_process(
            ["adapter"],
            None,
            reports.append,
            lambda: False,
            invocation="full",
        )

    assert [(report.invocation, report.event.sequence) for report in reports] == [
        ("preflight", 2),
        ("preflight", 3),
        ("full", 1),
    ]


def test_legacy_progress_continues_work_sequence_after_structured_progress(tmp_path):
    """Legacy finalization reports must remain ordered after fork measured work."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python=sys.executable,
        app_port=8765,
        disk_reserve_gb=0,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)
    structured = "EVENT " + json.dumps(
        {
            "schema_version": 1,
            "sequence": 7,
            "work_sequence": 5,
            "measured_work": False,
            "event_type": "output_started",
            "elapsed_seconds": 12.5,
        }
    )

    class Process:
        pid = 1234
        returncode = 0
        stdout = io.StringIO(
            structured
            + "\nPROGRESS 92 finalizing\nPROGRESS 92 finalizing\nPROGRESS 93 finalizing\n"
        )

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    reports: list[ProgressReport] = []
    with patch("app.runner.subprocess.Popen", return_value=Process()):
        runner._run_process(
            ["adapter"],
            None,
            reports.append,
            lambda: False,
            invocation="full",
        )

    assert [report.work_sequence for report in reports] == [5, 6, 7, 8]
    assert [report.measured_work for report in reports] == [False, True, False, True]
    assert [report.percent for report in reports] == [91, 92, 92, 93]


def test_runner_omits_machine_event_lines_from_visible_job_log(tmp_path):
    """Persisting canonical EVENT payloads duplicates machine data in operator logs."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python=sys.executable,
        app_port=8765,
        disk_reserve_gb=0,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)
    event = "EVENT " + json.dumps(
        {
            "schema_version": 1,
            "sequence": 1,
            "work_sequence": 0,
            "measured_work": False,
            "event_type": "heartbeat",
        }
    )

    class Process:
        pid = 1234
        returncode = 0
        stdout = io.StringIO(f"human model-loading\n{event}\nhuman ready\n")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    log_path = tmp_path / "job.log"
    with patch("app.runner.subprocess.Popen", return_value=Process()):
        runner._run_process(
            ["adapter"],
            log_path,
            lambda _report: None,
            lambda: False,
            invocation="full",
        )

    assert log_path.read_text() == "human model-loading\nhuman ready\n"


def test_runner_refuses_to_advertise_ready_before_default_model_is_present(tmp_path):
    """A partial model download must keep WebUI health degraded."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python="python3",
        app_port=8765,
        disk_reserve_gb=20,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )

    with pytest.raises(Exception, match="model is not ready"):
        SubprocessRunner(settings)


def test_cancellation_terminates_and_kills_adapter_process_group_before_returning(tmp_path):
    """Leaving SeedVR2 child processes alive after cancellation must make this test fail."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python="python3",
        app_port=8765,
        disk_reserve_gb=20,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)

    class Process:
        pid = 4321
        returncode = 0
        stdout = io.StringIO("")

        def __init__(self):
            self.wait_calls = []

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise subprocess.TimeoutExpired(["adapter"], timeout)
            return 0

    process = Process()
    with patch("app.runner.subprocess.Popen", return_value=process) as popen, patch(
        "app.runner.os.killpg"
    ) as killpg:
        with pytest.raises(JobCancelled):
            runner._run_process(
                ["adapter"],
                None,
                lambda _report: None,
                lambda: True,
                invocation="full",
            )

    assert popen.call_args.kwargs["start_new_session"] is True
    assert killpg.call_args_list == [
        call(process.pid, signal.SIGTERM),
        call(process.pid, signal.SIGKILL),
    ]
    assert process.wait_calls == [5, 5]


def test_ffprobe_timeout_rejects_pathological_media(tmp_path):
    """Letting ffprobe run forever on attacker media must make this test fail."""
    probe = SubprocessMediaProbe("ffprobe", timeout_seconds=7)

    with patch(
        "app.media.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["ffprobe"], 7),
    ) as run:
        with pytest.raises(ValueError, match="timed out"):
            probe.inspect(tmp_path / "video.mp4")

    assert run.call_args.kwargs["timeout"] == 7


def test_final_output_ffprobe_metadata_is_bounded(tmp_path):
    """Final validation must not allow unbounded ffprobe output."""
    probe = SubprocessMediaProbe("ffprobe")

    def oversized_probe(_command, *, stdout, **_kwargs):
        stdout.write(b"x" * (MAX_PROBE_OUTPUT_BYTES + 1))
        return SimpleNamespace(returncode=0)

    with patch("app.media.subprocess.run", side_effect=oversized_probe):
        with pytest.raises(ValueError, match="ffprobe metadata exceeds safety limit"):
            probe.inspect(tmp_path / "result.mp4")


def test_ffprobe_uses_container_frame_metadata_without_full_decode(tmp_path):
    """Admission must not decode every frame before accepting a normal upload."""
    probe = SubprocessMediaProbe("ffprobe")
    payload = '{"streams":[{"width":640,"height":360,"avg_frame_rate":"60000/1001","r_frame_rate":"60/1","nb_frames":"1800"}],"format":{"duration":"30","format_name":"mov,mp4,m4a,3gp,3g2,mj2"}}'

    def write_payload(_command, *, stdout, **_kwargs):
        stdout.write(payload.encode())
        return SimpleNamespace(returncode=0)

    with patch("app.media.subprocess.run", side_effect=write_payload) as run:
        media = probe.inspect(tmp_path / "video.mp4")

    assert media.frame_rate == pytest.approx(59.94, rel=1e-3)
    assert media.frame_count == 1800
    assert "-count_frames" not in run.call_args.args[0]
    assert "-count_packets" not in run.call_args.args[0]
    assert run.call_args.args[0][:2] == ["ffprobe", "-protocol_whitelist"]


def test_ffprobe_rejects_indirect_network_media_format(tmp_path):
    """Allowed filename suffix must not admit a network-fetching manifest."""
    probe = SubprocessMediaProbe("ffprobe")
    payload = '{"streams":[{"width":640,"height":360,"avg_frame_rate":"30/1","r_frame_rate":"30/1","nb_read_frames":"30"}],"format":{"duration":"1","format_name":"hls"}}'

    def write_payload(_command, *, stdout, **_kwargs):
        stdout.write(payload.encode())
        return SimpleNamespace(returncode=0)

    with patch("app.media.subprocess.run", side_effect=write_payload):
        with pytest.raises(ValueError, match="self-contained"):
            probe.inspect(tmp_path / "manifest.mp4")


def test_runner_passes_selected_output_scale_to_adapter(tmp_path):
    """Dropping selected scale from adapter argv must make this test fail."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python="python3",
        app_port=8765,
        disk_reserve_gb=0,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)
    job = Job(
        id="scale-job",
        original_filename="input.mp4",
        input_path=tmp_path / "input.mp4",
        output_path=tmp_path / "output.mp4",
        log_path=tmp_path / "job.log",
        preset="3b-safe",
        color_correction="lab",
        output_scale=0.5,
        target_width=640,
        target_height=360,
        frame_count=105,
        runtime_profile_fingerprint="test",
        status="running",
        progress=0,
        stage="upscaling",
        created_at="2026-08-12T00:00:00+00:00",
        updated_at="2026-08-12T00:00:00+00:00",
        output_filename=None,
        error=None,
        requires_preflight=False,
        cancel_requested=False,
        duration_seconds=3.5,
        width=1280,
        height=720,
    )

    with patch.object(runner, "_run_process") as run_process:
        runner._execute(
            job,
            input_path=job.input_path,
            output_path=job.output_path,
            mode="full",
            report_progress=lambda _report: None,
            is_cancelled=lambda: False,
        )

    command = run_process.call_args.args[0]
    assert command[command.index("--output-scale") + 1] == "0.5"
    assert command[command.index("--duration-seconds") + 1] == "3.500000"


def test_runner_wait_uses_configured_processing_deadline(tmp_path):
    """Waiting forever after adapter output closes must make this test fail."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python="python3",
        app_port=8765,
        disk_reserve_gb=20,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        max_process_seconds=17,
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)

    class Process:
        pid = 1234
        returncode = 0
        stdout = io.StringIO("")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            assert timeout is not None and timeout <= 17
            return 0

    with patch("app.runner.subprocess.Popen", return_value=Process()):
        runner._run_process(
            ["adapter"],
            None,
            lambda _report: None,
            lambda: False,
            invocation="full",
        )


def test_runner_caps_persisted_adapter_log(tmp_path):
    """Writing unlimited adapter output to a retained log must make this test fail."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python="python3",
        app_port=8765,
        disk_reserve_gb=20,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        max_job_log_bytes=32,
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)
    log_path = tmp_path / "job.log"

    class Process:
        pid = 1234
        returncode = 0
        stdout = io.StringIO("0123456789abcdef\n" * 10)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    with patch("app.runner.subprocess.Popen", return_value=Process()):
        runner._run_process(
            ["adapter"],
            log_path,
            lambda _report: None,
            lambda: False,
            invocation="full",
        )

    assert log_path.stat().st_size <= 32


def test_runner_uses_bounded_output_queue(tmp_path):
    """Child output must apply producer backpressure before memory can grow."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python="python3",
        app_port=8765,
        disk_reserve_gb=20,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)

    class Process:
        pid = 1234
        returncode = 0
        stdout = io.StringIO("PROGRESS 5 start\n")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    with patch("app.runner.subprocess.Popen", return_value=Process()), patch(
        "app.runner.queue.Queue", wraps=__import__("queue").Queue
    ) as queue_type:
        runner._run_process(
            ["adapter"],
            None,
            lambda _report: None,
            lambda: False,
            invocation="full",
        )

    assert queue_type.call_args.kwargs["maxsize"] > 0


def test_runner_joins_output_collector_after_cancellation(tmp_path):
    """Cancellation must not strand a producer blocked behind a full queue."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python="python3",
        app_port=8765,
        disk_reserve_gb=20,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)

    class Process:
        pid = 1234
        stdout = io.StringIO("output\n" * 200_000)

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    collectors = []
    real_thread = threading.Thread

    def make_thread(*args, **kwargs):
        collector = real_thread(*args, **kwargs)
        collectors.append(collector)
        return collector

    with patch("app.runner.subprocess.Popen", return_value=Process()), patch.object(
        runner, "_stop_process_group"
    ), patch("app.runner.threading.Thread", side_effect=make_thread):
        with pytest.raises(JobCancelled):
            runner._run_process(
                ["adapter"],
                None,
                lambda _report: None,
                lambda: True,
                invocation="full",
            )

    assert len(collectors) == 1
    assert not collectors[0].is_alive()


def test_runner_stops_process_when_artifact_budget_is_exhausted(tmp_path):
    """Processing must stop before output growth consumes disk reserve."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text("# adapter\n")
    settings = Settings(
        project_root=tmp_path,
        runtime_root=tmp_path,
        data_root=tmp_path,
        seedvr2_cli=str(adapter),
        seedvr2_model_dir=tmp_path,
        python="python3",
        app_port=8765,
        disk_reserve_gb=20,
        default_profile="3b-safe",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    (tmp_path / settings.seedvr2_3b_model).write_bytes(b"model")
    (tmp_path / settings.seedvr2_vae_model).write_bytes(b"vae")
    runner = SubprocessRunner(settings)

    class Process:
        pid = 9876
        stdout = io.StringIO("working\n")

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    with patch("app.runner.subprocess.Popen", return_value=Process()), patch.object(
        runner, "_artifacts_within_limits", return_value=False
    ), patch("app.runner.os.killpg") as killpg:
        with pytest.raises(RuntimeError, match="artifact safety limit"):
            runner._run_process(
                ["adapter"],
                None,
                lambda _report: None,
                lambda: False,
                invocation="full",
                monitored_paths=[tmp_path / "output.mp4"],
            )

    killpg.assert_called_once_with(9876, signal.SIGTERM)
