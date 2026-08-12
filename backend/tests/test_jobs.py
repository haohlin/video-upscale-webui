import base64
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import MediaInfo
from app.job_service import JobService
from app.job_store import JobStore
from app.main import create_app


class ValidProbe:
    def inspect(self, path: Path) -> dict[str, float | int]:
        return {"duration_seconds": 3.5, "width": 640, "height": 360}


class RejectedProbe:
    def inspect(self, path: Path) -> dict[str, float | int]:
        raise ValueError("ffprobe did not find a video stream")


class CompletedRunner:
    def preflight(self, job, limits, report_progress, is_cancelled):
        assert limits.max_duration_seconds == 10
        assert limits.max_height == 480
        report_progress(20, "preflight")

    def run(self, job, report_progress, is_cancelled):
        Path(job.output_path).write_bytes(b"mp4-result")
        report_progress(100, "encoding")


class BlockingRunner:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def preflight(self, job, limits, report_progress, is_cancelled):
        raise AssertionError("unexpected preflight")

    def run(self, job, report_progress, is_cancelled):
        self.started.set()
        while not self.release.wait(0.01):
            if is_cancelled():
                raise RuntimeError("cancelled")
        Path(job.output_path).write_bytes(b"mp4-result")


class RecordingRunner(CompletedRunner):
    def __init__(self):
        self.calls: list[str] = []

    def preflight(self, job, limits, report_progress, is_cancelled):
        self.calls.append("preflight")
        super().preflight(job, limits, report_progress, is_cancelled)

    def run(self, job, report_progress, is_cancelled):
        self.calls.append("run")
        super().run(job, report_progress, is_cancelled)


class BlockingPreflightRunner(CompletedRunner):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def preflight(self, job, limits, report_progress, is_cancelled):
        self.started.set()
        self.release.wait(1)
        super().preflight(job, limits, report_progress, is_cancelled)


class FullRunAfterCompletedPreflightRunner(CompletedRunner):
    def __init__(self):
        self.full_run_started = threading.Event()
        self.release = threading.Event()

    def preflight(self, job, limits, report_progress, is_cancelled):
        report_progress(100, "preflight-complete")

    def run(self, job, report_progress, is_cancelled):
        self.full_run_started.set()
        self.release.wait(1)
        Path(job.output_path).write_bytes(b"mp4-result")


def make_client(tmp_path, *, runner=None, probe=None, max_upload_bytes=None, **kwargs):
    return make_client_headers_client(
        create_app(
            data_root=tmp_path,
            runner=runner or CompletedRunner(),
            media_probe=probe or ValidProbe(),
            max_upload_bytes=max_upload_bytes,
            **kwargs,
        )
    )


def make_client_headers_client(app):
    credentials = base64.b64encode(b"video:test-access-token").decode("ascii")
    return TestClient(
        app,
        headers={
            "Authorization": f"Basic {credentials}",
            "X-Video-Upscale-Request": "1",
        },
    )


def submit_video(client, *, preset="3b-safe", color_correction="lab", name="clip.mp4"):
    return client.post(
        "/api/jobs",
        files={"video": (name, b"not a real video because probe is injected", "video/mp4")},
        data={"preset": preset, "color_correction": color_correction},
    )


def submit_video_without_preset(client, *, name="clip.mp4"):
    return client.post(
        "/api/jobs",
        files={"video": (name, b"not a real video because probe is injected", "video/mp4")},
    )


def wait_for_status(client, job_id, wanted_status):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["status"] == wanted_status:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {wanted_status}")


def test_upload_creates_a_persisted_job_with_public_contract_fields(tmp_path):
    """Dropping validated upload persistence must make this test fail."""
    client = make_client(tmp_path)

    response = submit_video(client, color_correction="none", name="My Clip.MP4")

    assert response.status_code == 201
    job = response.json()
    assert job["original_filename"] == "My Clip.MP4"
    assert job["preset"] == "3b-safe"
    assert job["color_correction"] == "none"
    assert job["requires_preflight"] is False
    assert job["output_filename"] is None
    assert client.get(f"/api/jobs/{job['id']}").json()["id"] == job["id"]
    assert client.get("/api/jobs").json()["jobs"][0]["id"] == job["id"]


def test_upload_rejects_unsupported_filename_before_persisting(tmp_path):
    """Accepting non-video extensions must make this test fail."""
    client = make_client(tmp_path)

    response = submit_video(client, name="clip.exe")

    assert response.status_code == 415
    assert client.get("/api/jobs").json() == {"jobs": []}


def test_upload_rejects_video_that_ffprobe_cannot_validate(tmp_path):
    """Skipping ffprobe video validation must make this test fail."""
    client = make_client(tmp_path, probe=RejectedProbe())

    response = submit_video(client)

    assert response.status_code == 422
    assert client.get("/api/jobs").json() == {"jobs": []}


def test_upload_stops_stream_when_configured_size_limit_is_exceeded(tmp_path):
    """Removing streaming size enforcement must make this test fail."""
    client = make_client(tmp_path, max_upload_bytes=8)

    response = submit_video(client)

    assert response.status_code == 413
    assert list((tmp_path / "inputs").glob("*")) == []


def test_upload_rejects_oversized_content_length_before_streaming_multipart(tmp_path):
    """Parsing an oversized multipart body before rejecting it must make this test fail."""
    app = create_app(
        data_root=tmp_path,
        runner=CompletedRunner(),
        media_probe=ValidProbe(),
        max_upload_bytes=1,
    )

    async def must_not_stream(*_args, **_kwargs):
        raise AssertionError("multipart stream was parsed")

    app.state.job_service._stream_upload = must_not_stream
    response = make_client_headers_client(app).post(
        "/api/jobs",
        files={"video": ("clip.mp4", b"video", "video/mp4")},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Upload exceeds configured size limit"


def test_upload_rejects_when_free_space_would_cross_configured_reserve(tmp_path):
    """Accepting an upload after free disk falls below reserve must make this test fail."""
    reserve = 20 * 1024 * 1024 * 1024
    client = make_client(
        tmp_path,
        free_space_bytes=lambda _path: reserve - 1,
    )

    response = submit_video(client)

    assert response.status_code == 507
    assert response.json()["detail"] == "Insufficient free disk space for upload"
    assert list((tmp_path / "inputs").glob("*")) == []


def test_upload_copy_reserves_space_for_spooled_file(tmp_path):
    """Duplicating a spooled upload below disk reserve must make this test fail."""
    reserve = 20 * 1024 * 1024 * 1024
    upload_bytes = len(b"not a real video because probe is injected")
    client = make_client(
        tmp_path,
        free_space_bytes=lambda _path: reserve + upload_bytes - 1,
    )

    response = submit_video(client)

    assert response.status_code == 507
    assert response.json()["detail"] == "Insufficient free disk space to store upload"
    assert list((tmp_path / "inputs").glob("*")) == []


def test_upload_rejects_media_beyond_configured_workload_limit(tmp_path):
    """Accepting an extreme-duration video must make this test fail."""

    class LongVideoProbe:
        def inspect(self, path: Path) -> dict[str, float | int]:
            return {"duration_seconds": 3601, "width": 640, "height": 360}

    client = make_client(tmp_path, probe=LongVideoProbe())

    response = submit_video(client)

    assert response.status_code == 422
    assert response.json()["detail"] == "Video exceeds maximum duration of 3600 seconds"


def test_upload_rejects_media_beyond_frame_budget(tmp_path):
    """A compact high-frame-count video must not bypass workload admission."""

    class HighFrameCountProbe:
        def inspect(self, path: Path) -> dict[str, float | int]:
            return {
                "duration_seconds": 60,
                "width": 640,
                "height": 360,
                "frame_rate": 121,
                "frame_count": 217_800,
            }

    client = make_client(tmp_path, probe=HighFrameCountProbe())

    response = submit_video(client)

    assert response.status_code == 422
    assert response.json()["detail"] == "Video frame rate exceeds configured safety limit"


def test_upload_rejects_when_bounded_queue_is_full(tmp_path):
    """Allowing unlimited queued GPU work must make this test fail."""
    runner = BlockingRunner()
    client = make_client(tmp_path, runner=runner, max_pending_jobs=2)
    assert submit_video(client).status_code == 201
    assert runner.started.wait(1)
    assert submit_video(client).status_code == 201

    rejected = submit_video(client)
    runner.release.set()

    assert rejected.status_code == 429
    assert rejected.json()["detail"] == "Processing queue is full"


def test_completed_job_downloads_mp4_and_delete_removes_media_and_record(tmp_path):
    """Not removing completed media and record must make this test fail."""
    client = make_client(tmp_path)
    job_id = submit_video(client).json()["id"]
    completed = wait_for_status(client, job_id, "completed")

    download = client.get(f"/api/jobs/{job_id}/download")
    delete = client.delete(f"/api/jobs/{job_id}")

    assert completed["output_filename"].endswith(".mp4")
    assert download.status_code == 200
    assert download.content == b"mp4-result"
    assert delete.status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert list((tmp_path / "inputs").glob("*")) == []
    assert list((tmp_path / "results").glob("*")) == []


def test_queued_job_can_be_cancelled_without_running(tmp_path):
    """Running a cancelled queued job must make this test fail."""
    runner = BlockingRunner()
    client = make_client(tmp_path, runner=runner)
    first = submit_video(client).json()["id"]
    assert runner.started.wait(1)
    second = submit_video(client).json()["id"]

    cancelled = client.post(f"/api/jobs/{second}/cancel")
    runner.release.set()
    wait_for_status(client, first, "completed")

    assert cancelled.status_code == 200
    assert client.get(f"/api/jobs/{second}").json()["status"] == "cancelled"


def test_7b_runs_mandatory_limited_preflight_before_full_upscale(tmp_path):
    """Skipping 7B preflight or running it after full processing must fail."""
    runner = RecordingRunner()
    client = make_client(tmp_path, runner=runner)
    job_id = submit_video(client, preset="7b-fp8-experimental").json()["id"]

    job = wait_for_status(client, job_id, "completed")

    assert job["requires_preflight"] is True
    assert runner.calls == ["preflight", "run"]


def test_7b_job_exposes_preflight_state_and_can_be_cancelled_during_probe(tmp_path):
    """The UI must show the required safety probe, not generic processing."""
    runner = BlockingPreflightRunner()
    client = make_client(tmp_path, runner=runner)
    job_id = submit_video(client, preset="7b-fp8-experimental").json()["id"]

    assert runner.started.wait(1)
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "preflight"
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    runner.release.set()

    assert wait_for_status(client, job_id, "cancelled")["stage"] == "cancelled"


def test_7b_full_run_resets_completed_probe_progress_before_rendering(tmp_path):
    """Leaving 100% from preflight during full rendering must make this test fail."""
    runner = FullRunAfterCompletedPreflightRunner()
    client = make_client(tmp_path, runner=runner)
    job_id = submit_video(client, preset="7b-fp8-experimental").json()["id"]

    assert runner.full_run_started.wait(1)
    active = client.get(f"/api/jobs/{job_id}").json()
    runner.release.set()

    assert active["status"] == "running"
    assert active["stage"] == "upscaling"
    assert active["progress"] == 0


def test_job_log_endpoint_returns_incremental_safe_tail(tmp_path):
    """Removing incremental job-owned logs would leave the debug console blind."""
    runner = BlockingRunner()
    client = make_client(tmp_path, runner=runner)
    job_id = submit_video(client).json()["id"]
    assert runner.started.wait(1)
    (tmp_path / "logs" / f"{job_id}.log").write_text("stage one\nstage two\n")

    response = client.get(f"/api/jobs/{job_id}/log?offset=10")
    runner.release.set()

    assert response.status_code == 200
    assert response.json() == {
        "text": "stage two\n",
        "next_offset": 20,
        "size": 20,
        "truncated": False,
    }


def test_configured_default_profile_is_used_when_upload_omits_preset(tmp_path, monkeypatch):
    """Hard-coding 3B instead of honoring runtime default must make this test fail."""
    monkeypatch.setenv("VIDEO_UPSCALE_DEFAULT_PROFILE", "7b-fp8-experimental")
    client = make_client(tmp_path, runner=CompletedRunner())

    response = submit_video_without_preset(client)

    assert response.status_code == 201
    assert response.json()["preset"] == "7b-fp8-experimental"
    assert response.json()["requires_preflight"] is True


def test_restart_marks_interrupted_job_failed_and_resumes_queued_work(tmp_path):
    """Leaving a stale running job blocking queued work after restart must make this test fail."""
    store = JobStore(tmp_path)
    store.initialize()
    media = MediaInfo(duration_seconds=1, width=320, height=180)
    first_input = store.inputs / "first.mp4"
    second_input = store.inputs / "second.mp4"
    first_input.write_bytes(b"input")
    second_input.write_bytes(b"input")
    first = store.create(
        job_id="first",
        original_filename="first.mp4",
        input_path=first_input,
        output_path=store.results / "first.mp4",
        log_path=store.logs / "first.log",
        preset="3b-safe",
        color_correction="lab",
        media=media,
    )
    store.claim_next_queued()
    second = store.create(
        job_id="second",
        original_filename="second.mp4",
        input_path=second_input,
        output_path=store.results / "second.mp4",
        log_path=store.logs / "second.log",
        preset="3b-safe",
        color_correction="lab",
        media=media,
    )

    client = make_client(tmp_path, runner=CompletedRunner())

    assert wait_for_status(client, second.id, "completed")["status"] == "completed"
    recovered = client.get(f"/api/jobs/{first.id}").json()
    assert recovered["status"] == "failed"
    assert recovered["stage"] == "interrupted"
    assert recovered["error"] == "Interrupted by application restart"


def test_job_is_rejected_before_runner_when_disk_reserve_is_no_longer_available(tmp_path):
    """Calling SeedVR2 after free disk crosses reserve must make this test fail."""
    settings = Settings.from_environment().with_data_root(tmp_path, None)
    store = JobStore(tmp_path)
    store.initialize()
    input_path = store.inputs / "queued.mp4"
    input_path.write_bytes(b"input")
    job = store.create(
        job_id="queued",
        original_filename="queued.mp4",
        input_path=input_path,
        output_path=store.results / "queued.mp4",
        log_path=store.logs / "queued.log",
        preset="3b-safe",
        color_correction="lab",
        media=MediaInfo(duration_seconds=1, width=320, height=180),
    )
    class RunnerThatMustNotRun:
        def preflight(self, job, limits, report_progress, is_cancelled):
            raise AssertionError("runner was called")

        def run(self, job, report_progress, is_cancelled):
            raise AssertionError("runner was called")

    service = JobService(
        settings,
        store,
        ValidProbe(),
        RunnerThatMustNotRun(),
        free_space_bytes=lambda _path: settings.disk_reserve_bytes - 1,
    )
    service._run_job(job)

    result = store.get(job.id)
    assert result is not None
    assert result.status == "failed"
    assert result.error == "Insufficient free disk space before processing"


def test_processing_failure_removes_partial_result_and_staging_files(tmp_path):
    """Failed processing must not retain disk-consuming partial artifacts."""
    settings = Settings.from_environment().with_data_root(tmp_path, None)
    store = JobStore(tmp_path)
    store.initialize()
    job = store.create(
        job_id="partial-job",
        original_filename="clip.mp4",
        input_path=store.inputs / "partial-job.mp4",
        output_path=store.results / "partial-job.mp4",
        log_path=store.logs / "partial-job.log",
        preset="3b-safe",
        color_correction="lab",
        media=MediaInfo(duration_seconds=1, width=320, height=180),
    )
    store.fail(job.id, "test setup")
    job = store.get(job.id)
    assert job is not None

    class PartialRunner:
        def preflight(self, job, limits, report_progress, is_cancelled):
            raise AssertionError("not used")

        def run(self, job, report_progress, is_cancelled):
            job.output_path.write_bytes(b"partial")
            (store.staging / f"{job.id}.video-only.mp4").write_bytes(b"partial")
            raise RuntimeError("processing failed")

    service = JobService(settings, store, ValidProbe(), PartialRunner())
    service._run_job(job)

    assert not job.output_path.exists()
    assert list(store.staging.glob(f"{job.id}*")) == []


def test_restart_recovery_removes_interrupted_partial_artifacts(tmp_path):
    """Restart must clean partial output and staging before new work begins."""
    settings = Settings.from_environment().with_data_root(tmp_path, None)
    store = JobStore(tmp_path)
    store.initialize()
    job = store.create(
        job_id="interrupted-job",
        original_filename="clip.mp4",
        input_path=store.inputs / "interrupted-job.mp4",
        output_path=store.results / "interrupted-job.mp4",
        log_path=store.logs / "interrupted-job.log",
        preset="3b-safe",
        color_correction="lab",
        media=MediaInfo(duration_seconds=1, width=320, height=180),
    )
    claimed = store.claim_next_queued()
    assert claimed is not None
    job.output_path.write_bytes(b"partial")
    (store.staging / f"{job.id}.video-only.mp4").write_bytes(b"partial")

    JobService(settings, store, ValidProbe(), CompletedRunner())

    assert not job.output_path.exists()
    assert list(store.staging.glob(f"{job.id}*")) == []


def test_runtime_media_layout_uses_inputs_staging_and_results(tmp_path):
    """Returning results to obsolete outputs directory must make this test fail."""
    store = JobStore(tmp_path)
    store.initialize()

    assert store.inputs == tmp_path / "inputs"
    assert store.staging == tmp_path / "staging"
    assert store.results == tmp_path / "results"


def test_completed_results_remain_until_manual_delete(tmp_path):
    """Automatic history limits must never delete completed outputs."""
    client = make_client(tmp_path)
    first = submit_video(client).json()["id"]
    wait_for_status(client, first, "completed")
    second = submit_video(client).json()["id"]
    wait_for_status(client, second, "completed")

    jobs = client.get("/api/jobs").json()["jobs"]

    assert [job["id"] for job in jobs] == [second, first]
    assert (tmp_path / "inputs" / f"{first}.mp4").exists()
    assert (tmp_path / "results" / f"{first}.mp4").exists()
