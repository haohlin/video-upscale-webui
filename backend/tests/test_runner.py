import io
import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import call, patch

import pytest

from app.config import Settings
from app.media import SubprocessMediaProbe
from app.runner import JobCancelled, SubprocessRunner


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
            runner._run_process(["adapter"], None, lambda _percent, _stage: None, lambda: True)

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


def test_ffprobe_returns_bounded_frame_metadata(tmp_path):
    """Admission must receive frame rate and count, not duration alone."""
    probe = SubprocessMediaProbe("ffprobe")
    payload = '{"streams":[{"width":640,"height":360,"avg_frame_rate":"60000/1001","r_frame_rate":"60/1","nb_read_frames":"1800"}],"format":{"duration":"30","format_name":"mov,mp4,m4a,3gp,3g2,mj2"}}'

    with patch("app.media.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = payload
        media = probe.inspect(tmp_path / "video.mp4")

    assert media.frame_rate == pytest.approx(59.94, rel=1e-3)
    assert media.frame_count == 1800
    assert "-count_frames" in run.call_args.args[0]
    assert "-count_packets" not in run.call_args.args[0]
    assert run.call_args.args[0][:2] == ["ffprobe", "-protocol_whitelist"]


def test_ffprobe_rejects_indirect_network_media_format(tmp_path):
    """Allowed filename suffix must not admit a network-fetching manifest."""
    probe = SubprocessMediaProbe("ffprobe")
    payload = '{"streams":[{"width":640,"height":360,"avg_frame_rate":"30/1","r_frame_rate":"30/1","nb_read_frames":"30"}],"format":{"duration":"1","format_name":"hls"}}'

    with patch("app.media.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = payload
        with pytest.raises(ValueError, match="self-contained"):
            probe.inspect(tmp_path / "manifest.mp4")


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
        runner._run_process(["adapter"], None, lambda _percent, _stage: None, lambda: False)


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
        runner._run_process(["adapter"], log_path, lambda _percent, _stage: None, lambda: False)

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
        runner._run_process(["adapter"], None, lambda _percent, _stage: None, lambda: False)

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
            runner._run_process(["adapter"], None, lambda _percent, _stage: None, lambda: True)

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
                lambda _percent, _stage: None,
                lambda: False,
                monitored_paths=[tmp_path / "output.mp4"],
            )

    killpg.assert_called_once_with(9876, signal.SIGTERM)
