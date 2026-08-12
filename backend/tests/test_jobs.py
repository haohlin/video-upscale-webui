import base64
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import MediaInfo, target_dimensions
from app.eta import EtaEstimate, workload_bucket
from app.job_service import JobService
from app.job_store import JobStore
from app.main import create_app
from app.progress import ProgressEvent, ProgressReport


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
        report_progress(
            ProgressReport(percent=20, stage="preflight", invocation="preflight")
        )

    def run(self, job, report_progress, is_cancelled):
        Path(job.output_path).write_bytes(b"mp4-result")
        report_progress(ProgressReport(percent=100, stage="encoding", invocation="full"))


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
        report_progress(
            ProgressReport(
                percent=100,
                stage="preflight-complete",
                invocation="preflight",
            )
        )

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


def submit_video(
    client,
    *,
    preset="3b-safe",
    color_correction="lab",
    output_scale=None,
    name="clip.mp4",
):
    data = {"preset": preset, "color_correction": color_correction}
    if output_scale is not None:
        data["output_scale"] = output_scale
    return client.post(
        "/api/jobs",
        files={"video": (name, b"not a real video because probe is injected", "video/mp4")},
        data=data,
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


@pytest.mark.parametrize(
    ("width", "height", "scale", "expected"),
    [
        (1920, 1080, 1.0, (1920, 1080)),
        (1920, 1080, 0.5, (960, 540)),
        (2160, 3840, 0.25, (540, 960)),
        (1281, 719, 0.5, (640, 360)),
        (1920, 1080, 2.0, (3840, 2160)),
    ],
)
def test_target_dimensions_preserve_aspect_and_even_codec_dimensions(
    width, height, scale, expected
):
    assert target_dimensions(width, height, scale) == expected


def test_upload_defaults_to_original_resolution_and_exposes_target_dimensions(tmp_path):
    client = make_client(tmp_path)
    response = submit_video(client)
    assert response.status_code == 201
    assert response.json()["output_scale"] == 1.0
    assert response.json()["target_width"] == 640
    assert response.json()["target_height"] == 360
    assert response.json()["frame_count"] == 105
    assert response.json()["runtime_profile_fingerprint"] == (
        "seedvr2:3b-safe:apple-mps:scale=1:batch=5:chunk=25:overlap=4:"
        "dit_cache=disabled:vae_cache=disabled"
    )


@pytest.mark.parametrize("scale", ["0.25", "0.5", "1", "2"])
def test_upload_accepts_fixed_output_scale_allowlist(tmp_path, scale):
    class ScaleProbe:
        def inspect(self, path: Path) -> dict[str, float | int]:
            if path.parent.name == "results":
                width, height = target_dimensions(1920, 1080, float(scale))
                return {"duration_seconds": 3.5, "width": width, "height": height}
            return {"duration_seconds": 3.5, "width": 1920, "height": 1080}

    client = make_client(tmp_path, probe=ScaleProbe())
    response = submit_video(client, output_scale=scale)
    assert response.status_code == 201
    assert response.json()["output_scale"] == float(scale)


@pytest.mark.parametrize("scale", ["0", "0.3", "4", "nan", "inf", "1;touch /tmp/pwned"])
def test_upload_rejects_non_allowlisted_output_scale(tmp_path, scale):
    client = make_client(tmp_path)
    response = submit_video(client, output_scale=scale)
    assert response.status_code == 422
    assert client.get("/api/jobs").json() == {"jobs": []}


def test_quarter_scale_rejects_unsafe_short_edge(tmp_path):
    client = make_client(tmp_path)
    response = submit_video(client, output_scale="0.25")
    assert response.status_code == 422
    assert response.json()["detail"] == "Target shortest edge must be at least 256 pixels"


@pytest.mark.parametrize(
    ("width", "height", "scale", "detail"),
    [
        (4096, 2160, 2.0, "Target longest edge must not exceed 7680 pixels"),
        (3840, 2162, 2.0, "Target pixel count must not exceed 33177600 pixels"),
    ],
)
def test_target_dimensions_enforce_edge_and_pixel_ceiling(width, height, scale, detail):
    with pytest.raises(ValueError, match=detail):
        target_dimensions(width, height, scale)


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


def test_output_mismatch_fails_job_before_completion(tmp_path):
    """Completing a job with wrong final dimensions must make this test fail."""

    class OutputMismatchProbe:
        def inspect(self, path: Path) -> dict[str, float | int]:
            if path.parent.name == "results":
                return {"duration_seconds": 3.5, "width": 320, "height": 180}
            return {"duration_seconds": 3.5, "width": 640, "height": 360}

    client = make_client(tmp_path, probe=OutputMismatchProbe())
    job_id = submit_video(client).json()["id"]

    failed = wait_for_status(client, job_id, "failed")

    assert failed["error"] == "Final MP4 dimensions do not match validated target"


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


def test_config_exposes_output_scale_options_and_default(tmp_path):
    client = make_client(tmp_path)

    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["default_output_scale"] == 1.0
    assert response.json()["output_scales"] == [
        {
            "value": 1.0,
            "label": "1x Original",
            "description": "Original dimensions; full generative restoration.",
        },
        {
            "value": 0.5,
            "label": "0.5x Balanced",
            "description": (
                "Half width and height; generative restoration with fewer output pixels."
            ),
        },
        {
            "value": 0.25,
            "label": "0.25x Fast",
            "description": "Quarter width and height; experimental generative restoration.",
        },
        {
            "value": 2.0,
            "label": "2x Upscale",
            "description": "Double width and height; highest processing cost.",
        },
    ]


def test_timing_migration_upgrades_legacy_database_and_is_idempotent(tmp_path):
    database_path = tmp_path / "jobs.sqlite3"
    tmp_path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                input_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                log_path TEXT NOT NULL,
                preset TEXT NOT NULL,
                color_correction TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                stage TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                output_filename TEXT,
                error TEXT,
                requires_preflight INTEGER NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL
            )
            """
        )
        rows = [
            ("legacy", "completed", "completed"),
            ("active-legacy", "running", "upscaling"),
        ]
        for job_id, status, stage in rows:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, original_filename, input_path, output_path, log_path, preset,
                    color_correction, status, progress, stage, created_at, updated_at,
                    output_filename, error, requires_preflight, cancel_requested,
                    duration_seconds, width, height
                ) VALUES (?, ?, ?, ?, ?, '3b-safe', 'lab', ?, 50, ?, ?, ?, NULL, NULL, 0, 0, 1, 640, 360)
                """,
                (
                    job_id,
                    f"{job_id}.mp4",
                    str(tmp_path / "inputs" / f"{job_id}.mp4"),
                    str(tmp_path / "results" / f"{job_id}.mp4"),
                    str(tmp_path / "logs" / f"{job_id}.log"),
                    status,
                    stage,
                    "2026-08-12T00:00:00+00:00",
                    "2026-08-12T00:00:00+00:00",
                ),
            )

    store = JobStore(tmp_path)
    store.initialize()
    with sqlite3.connect(database_path) as connection:
        schema_after_first = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
        data_after_first = connection.execute(
            "SELECT id, output_scale, target_width, target_height, frame_count, runtime_profile_fingerprint FROM jobs ORDER BY id"
        ).fetchall()
    store.initialize()

    job = store.get("legacy")
    assert job is not None
    assert job.output_scale == 2.0
    assert (job.target_width, job.target_height) == (1280, 720)
    assert job.frame_count == 0
    assert job.runtime_profile_fingerprint == "legacy:unknown"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0] == schema_after_first
        assert connection.execute(
            "SELECT id, output_scale, target_width, target_height, frame_count, runtime_profile_fingerprint FROM jobs ORDER BY id"
        ).fetchall() == data_after_first

    interrupted = store.recover_interrupted()
    assert [job.id for job in interrupted] == ["active-legacy"]
    recovered = store.get("active-legacy")
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.stage == "interrupted"


def test_job_progress_never_decreases_during_ffmpeg_fallback(tmp_path):
    """A fallback progress restart must not move stored percent backward."""
    store = JobStore(tmp_path)
    store.initialize()
    input_path = store.inputs / "fallback.mp4"
    input_path.write_bytes(b"input")
    job = store.create(
        job_id="fallback",
        original_filename="fallback.mp4",
        input_path=input_path,
        output_path=store.results / "fallback.mp4",
        log_path=store.logs / "fallback.log",
        preset="3b-safe",
        color_correction="lab",
        media=MediaInfo(duration_seconds=1, width=320, height=180),
    )
    assert store.claim_next_queued() is not None
    store.update_progress(job.id, 95, "audio-remux")

    store.update_progress(job.id, 92, "audio-remux-retry")

    updated = store.get(job.id)
    assert updated is not None
    assert updated.progress == 95
    assert updated.stage == "audio-remux-retry"


def test_active_job_progress_is_capped_below_validated_completion(tmp_path):
    """Runner progress must not expose 100 before final validation completes."""
    store = JobStore(tmp_path)
    store.initialize()
    input_path = store.inputs / "active.mp4"
    input_path.write_bytes(b"input")
    job = store.create(
        job_id="active-progress",
        original_filename="active.mp4",
        input_path=input_path,
        output_path=store.results / "active-progress.mp4",
        log_path=store.logs / "active-progress.log",
        preset="3b-safe",
        color_correction="lab",
        media=MediaInfo(duration_seconds=1, width=320, height=180),
    )
    assert store.claim_next_queued() is not None

    store.update_progress(job.id, 100, "adapter-complete")

    active = store.get(job.id)
    assert active is not None
    assert active.status == "running"
    assert active.progress == 99
    store.complete(job.id, publish_performance=False)
    completed = store.get(job.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.progress == 100


@pytest.mark.parametrize("active_status", ["running", "preflight"])
def test_progress_update_normalizes_stale_active_100_to_99(tmp_path, active_status):
    """A persisted active 100 from older code must be repaired on next update."""
    store = JobStore(tmp_path)
    store.initialize()
    input_path = store.inputs / f"{active_status}.mp4"
    input_path.write_bytes(b"input")
    job = store.create(
        job_id=f"stale-{active_status}",
        original_filename=input_path.name,
        input_path=input_path,
        output_path=store.results / f"stale-{active_status}.mp4",
        log_path=store.logs / f"stale-{active_status}.log",
        preset="3b-safe",
        color_correction="lab",
        media=MediaInfo(duration_seconds=1, width=320, height=180),
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = ?, progress = 100 WHERE id = ?",
            (active_status, job.id),
        )

    store.update_progress(job.id, 98, "post-upgrade-progress")

    normalized = store.get(job.id)
    assert normalized is not None
    assert normalized.status == active_status
    assert normalized.progress == 99
    assert normalized.stage == "post-upgrade-progress"


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


def _create_timing_test_job(
    store: JobStore,
    job_id: str,
    *,
    fingerprint: str = "seedvr2:3b-safe:apple-mps:scale=1",
):
    input_path = store.inputs / f"{job_id}.mp4"
    input_path.write_bytes(b"input")
    return store.create(
        job_id=job_id,
        original_filename=f"private-{job_id}-1920x1080.mp4",
        input_path=input_path,
        output_path=store.results / f"{job_id}.mp4",
        log_path=store.logs / f"{job_id}.log",
        preset="3b-safe",
        color_correction="lab",
        media=MediaInfo(
            duration_seconds=4,
            width=640,
            height=360,
            frame_count=100,
        ),
        output_scale=1.0,
        target_width=640,
        target_height=360,
        runtime_profile_fingerprint=fingerprint,
    )


def _phase_report(
    *,
    sequence: int,
    work_sequence: int,
    current_unit: int,
    total_units: int = 10,
    invocation: str = "full",
    elapsed_seconds: float = 5.0,
    phase: str = "encoding",
    chunk_index: int = 1,
    chunk_count: int = 4,
) -> ProgressReport:
    event = ProgressEvent(
        sequence=sequence,
        work_sequence=work_sequence,
        measured_work=True,
        event_type="phase_progress",
        phase=phase,
        current_unit=current_unit,
        total_units=total_units,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        completed_unique_frames=0,
        chunk_unique_frames=25,
        chunk_context_frames=4,
        total_unique_frames=100,
        elapsed_seconds=elapsed_seconds,
    )
    return ProgressReport(
        percent=20 + current_unit,
        stage=phase,
        invocation=invocation,
        work_sequence=work_sequence,
        measured_work=True,
        event=event,
    )


def _heartbeat_report(*, sequence: int, work_sequence: int, invocation: str = "full"):
    event = ProgressEvent(
        sequence=sequence,
        work_sequence=work_sequence,
        measured_work=False,
        event_type="heartbeat",
    )
    return ProgressReport(
        percent=0,
        stage="heartbeat",
        invocation=invocation,
        work_sequence=work_sequence,
        measured_work=False,
        event=event,
    )


def _chunk_completed_report(
    *, sequence: int, work_sequence: int, invocation: str = "full"
):
    event = ProgressEvent(
        sequence=sequence,
        work_sequence=work_sequence,
        measured_work=True,
        event_type="chunk_completed",
        chunk_index=1,
        chunk_count=4,
        completed_unique_frames=25,
        chunk_unique_frames=0,
        total_unique_frames=100,
    )
    return ProgressReport(
        percent=43,
        stage="chunk-complete",
        invocation=invocation,
        work_sequence=work_sequence,
        measured_work=True,
        event=event,
    )


def test_timing_and_monotonic_report_freshness_are_persisted(tmp_path, monkeypatch):
    """Regressed event order or heartbeat-only traffic must not fake work freshness."""
    store = JobStore(tmp_path)
    store.initialize()
    job = _create_timing_test_job(store, "timing-monotonic")
    claimed_at = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    t1 = claimed_at + timedelta(seconds=10)
    t2 = claimed_at + timedelta(seconds=20)
    monkeypatch.setattr(store, "_now", lambda: claimed_at.isoformat())

    running = store.claim_next_queued()

    assert running is not None
    assert running.started_at == claimed_at.isoformat()
    assert running.finished_at is None
    sequence_7 = _phase_report(sequence=7, work_sequence=5, current_unit=5)
    assert store.record_report(job.id, sequence_7, now=t1) is True
    assert store.record_report(
        job.id,
        _phase_report(sequence=6, work_sequence=6, current_unit=6),
        now=t2,
    ) is False
    updated = store.get(job.id)
    assert updated is not None
    assert updated.phase_name == "encoding"
    assert updated.phase_current == 5
    assert updated.phase_total == 10
    assert updated.chunk_current == 1
    assert updated.chunk_total == 4
    assert updated.last_heartbeat_at == t1.isoformat()
    assert updated.last_progress_at == t1.isoformat()
    assert updated.last_event_invocation == "full"
    assert updated.last_event_sequence == 7
    assert updated.last_work_sequence == 5

    assert store.record_report(job.id, _heartbeat_report(sequence=8, work_sequence=5), now=t2)
    heartbeat = store.get(job.id)
    assert heartbeat is not None
    assert heartbeat.last_heartbeat_at == t2.isoformat()
    assert heartbeat.last_progress_at == t1.isoformat()
    assert heartbeat.progress == updated.progress

    duplicate_time = t2 + timedelta(seconds=1)
    assert store.record_report(
        job.id,
        _phase_report(sequence=9, work_sequence=5, current_unit=5),
        now=duplicate_time,
    )
    duplicate = store.get(job.id)
    assert duplicate is not None
    assert duplicate.last_heartbeat_at == duplicate_time.isoformat()
    assert duplicate.last_progress_at == t1.isoformat()

    store.complete(job.id, publish_performance=False, now=t2)
    completed = store.get(job.id)
    assert completed is not None
    assert completed.finished_at == t2.isoformat()


def test_invocation_sequence_resets_between_preflight_and_full(tmp_path):
    """Preflight sequence numbers must not reject fresh full-run reports."""
    store = JobStore(tmp_path)
    store.initialize()
    job = _create_timing_test_job(store, "invocation-reset")
    assert store.claim_next_queued() is not None
    store.mark_preflight(job.id)
    now = datetime(2026, 8, 12, 2, 0, tzinfo=UTC)

    assert store.record_report(
        job.id,
        _phase_report(
            sequence=7,
            work_sequence=5,
            current_unit=5,
            invocation="preflight",
        ),
        now=now,
    )
    store.mark_running(job.id)
    assert store.record_report(
        job.id,
        _phase_report(sequence=1, work_sequence=1, current_unit=1),
        now=now + timedelta(seconds=1),
    )
    updated = store.get(job.id)
    assert updated is not None
    assert updated.last_event_invocation == "full"
    assert updated.last_event_sequence == 1
    assert updated.last_work_sequence == 1
    assert updated.phase_current == 1


def test_phase_metrics_publish_anonymized_performance_samples_and_cascade(tmp_path):
    """Deleting job history must remove owned metrics without erasing anonymous rates."""
    store = JobStore(tmp_path)
    store.initialize()
    job = _create_timing_test_job(store, "secret-job-name")
    assert store.claim_next_queued() is not None
    started = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
    finished = started + timedelta(seconds=10)

    assert store.record_report(
        job.id,
        _phase_report(sequence=7, work_sequence=5, current_unit=1, elapsed_seconds=1),
        now=started,
    )
    assert store.record_report(
        job.id,
        _phase_report(
            sequence=8,
            work_sequence=6,
            current_unit=10,
            elapsed_seconds=10,
        ),
        now=finished,
    )
    samples = store.phase_samples(job.id)
    assert len(samples) == 1
    assert samples[0].phase == "encoding"
    assert samples[0].elapsed_seconds == 10
    assert samples[0].completed_units == 10
    assert samples[0].valid is True

    store.complete(
        job.id,
        publish_performance=True,
        now=finished + timedelta(seconds=1),
    )
    with sqlite3.connect(store.database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(performance_samples)")
        }
        rows = connection.execute(
            "SELECT sample_group, phase, seconds_per_unit, workload_bucket, "
            "runtime_profile_fingerprint, sample_date FROM performance_samples"
        ).fetchall()
        metric_count = connection.execute(
            "SELECT COUNT(*) FROM job_phase_metrics WHERE job_id = ?", (job.id,)
        ).fetchone()[0]
    assert columns == {
        "id",
        "sample_group",
        "phase",
        "seconds_per_unit",
        "workload_bucket",
        "runtime_profile_fingerprint",
        "sample_date",
    }
    assert metric_count == 1
    assert len(rows) == 1
    sample_group, phase, rate, bucket, fingerprint, sample_date = rows[0]
    assert len(sample_group) == 32
    assert phase == "encoding"
    assert rate == 1.0
    assert bucket == 22
    assert fingerprint == job.runtime_profile_fingerprint
    assert sample_date == "2026-08-12"
    assert job.id not in repr(rows)
    assert job.original_filename not in repr(rows)
    assert str(job.input_path) not in repr(rows)

    assert store.delete(job.id)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_phase_metrics").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM performance_samples").fetchone()[0] == 1


def test_performance_samples_exclude_cancelled_failed_legacy_and_stale_jobs(tmp_path):
    """Invalid, private, or stale jobs must never train historical ETA."""
    store = JobStore(tmp_path)
    store.initialize()
    now = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)

    cancelled = _create_timing_test_job(store, "cancelled-sample")
    assert store.claim_next_queued() is not None
    assert store.record_report(
        cancelled.id,
        _phase_report(sequence=1, work_sequence=1, current_unit=10, elapsed_seconds=10),
        now=now,
    )
    store.mark_cancelled(cancelled.id)

    failed = _create_timing_test_job(store, "failed-sample")
    assert store.claim_next_queued() is not None
    assert store.record_report(
        failed.id,
        _phase_report(sequence=1, work_sequence=1, current_unit=10, elapsed_seconds=10),
        now=now,
    )
    store.fail(failed.id, "failed")

    legacy = _create_timing_test_job(
        store,
        "legacy-sample",
        fingerprint="legacy:unknown",
    )
    assert store.claim_next_queued() is not None
    assert store.record_report(
        legacy.id,
        _phase_report(sequence=1, work_sequence=1, current_unit=10, elapsed_seconds=10),
        now=now,
    )
    store.complete(legacy.id, publish_performance=True, now=now + timedelta(seconds=1))

    stale = _create_timing_test_job(store, "stale-sample")
    assert store.claim_next_queued() is not None
    assert store.record_report(
        stale.id,
        _phase_report(sequence=1, work_sequence=1, current_unit=10, elapsed_seconds=10),
        now=now,
    )
    store.complete(stale.id, publish_performance=True, now=now + timedelta(seconds=121))

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM performance_samples").fetchone()[0] == 0


def test_phase_metrics_reject_completion_after_stale_reporting_gap(
    tmp_path, monkeypatch
):
    """A late completion event must not rehabilitate stale phase timing."""
    store = JobStore(tmp_path)
    store.initialize()
    job = _create_timing_test_job(store, "stale-phase-gap")
    claimed_at = datetime(2026, 8, 12, 4, 30, tzinfo=UTC)
    monkeypatch.setattr(store, "_now", lambda: claimed_at.isoformat())
    assert store.claim_next_queued() is not None

    stale_report_at = claimed_at + timedelta(seconds=400)
    assert store.record_report(
        job.id,
        _phase_report(
            sequence=1,
            work_sequence=1,
            current_unit=10,
            elapsed_seconds=10,
        ),
        now=stale_report_at,
    )
    samples = store.phase_samples(job.id)
    assert len(samples) == 1
    assert samples[0].valid is False
    assert store.record_report(
        job.id,
        _phase_report(
            sequence=2,
            work_sequence=2,
            current_unit=10,
            elapsed_seconds=10,
        ),
        now=stale_report_at + timedelta(seconds=1),
    )
    assert store.phase_samples(job.id)[0].valid is False

    store.complete(
        job.id,
        publish_performance=True,
        now=stale_report_at + timedelta(seconds=2),
    )
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM performance_samples").fetchone()[0] == 0


def test_phase_metrics_reject_completion_when_heartbeat_is_fresh_but_work_is_stale(
    tmp_path, monkeypatch
):
    """Heartbeat liveness must not make an old measured-work rate publishable."""
    store = JobStore(tmp_path)
    store.initialize()
    job = _create_timing_test_job(store, "stale-work-gap")
    claimed_at = datetime(2026, 8, 12, 4, 35, tzinfo=UTC)
    monkeypatch.setattr(store, "_now", lambda: claimed_at.isoformat())
    assert store.claim_next_queued() is not None
    assert store.record_report(
        job.id,
        _phase_report(sequence=1, work_sequence=1, current_unit=1, elapsed_seconds=1),
        now=claimed_at,
    )
    assert store.record_report(
        job.id,
        _heartbeat_report(sequence=2, work_sequence=1),
        now=claimed_at + timedelta(seconds=250),
    )

    completed_at = claimed_at + timedelta(seconds=301)
    assert store.record_report(
        job.id,
        _phase_report(
            sequence=3,
            work_sequence=2,
            current_unit=10,
            elapsed_seconds=10,
        ),
        now=completed_at,
    )
    assert store.phase_samples(job.id)[0].valid is False
    store.complete(
        job.id,
        publish_performance=True,
        now=completed_at + timedelta(seconds=1),
    )
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM performance_samples").fetchone()[0] == 0


@pytest.mark.parametrize(
    "report",
    [
        ProgressReport(
            percent=30,
            stage="encoding",
            invocation="full",
            work_sequence=1,
            measured_work=True,
            event=ProgressEvent(
                sequence=1,
                work_sequence=1,
                measured_work=False,
                event_type="phase_progress",
                phase="encoding",
                current_unit=10,
                total_units=10,
                chunk_index=1,
                chunk_count=1,
                completed_unique_frames=0,
                chunk_unique_frames=25,
                chunk_context_frames=4,
                total_unique_frames=25,
                elapsed_seconds=10,
            ),
        ),
        ProgressReport(
            percent=30,
            stage="encoding",
            invocation="full",
            work_sequence=1,
            measured_work=False,
            event=ProgressEvent(
                sequence=1,
                work_sequence=1,
                measured_work=True,
                event_type="phase_progress",
                phase="encoding",
                current_unit=10,
                total_units=10,
                chunk_index=1,
                chunk_count=1,
                completed_unique_frames=0,
                chunk_unique_frames=25,
                chunk_context_frames=4,
                total_unique_frames=25,
                elapsed_seconds=10,
            ),
        ),
        ProgressReport(
            percent=30,
            stage="encoding",
            invocation="full",
            work_sequence=2,
            measured_work=True,
            event=ProgressEvent(
                sequence=1,
                work_sequence=1,
                measured_work=True,
                event_type="phase_progress",
                phase="encoding",
                current_unit=10,
                total_units=10,
                chunk_index=1,
                chunk_count=1,
                completed_unique_frames=0,
                chunk_unique_frames=25,
                chunk_context_frames=4,
                total_unique_frames=25,
                elapsed_seconds=10,
            ),
        ),
    ],
    ids=["event-unmeasured", "report-unmeasured", "work-sequence"],
)
def test_phase_metrics_reject_contradictory_report_event_metadata(tmp_path, report):
    """Wrapper metadata disagreement must not mutate job or metric history."""
    store = JobStore(tmp_path)
    store.initialize()
    job = _create_timing_test_job(store, "contradictory-report")
    assert store.claim_next_queued() is not None

    assert store.record_report(
        job.id,
        report,
        now=datetime(2026, 8, 12, 4, 45, tzinfo=UTC),
    ) is False
    updated = store.get(job.id)
    assert updated is not None
    assert updated.last_heartbeat_at is None
    assert updated.last_progress_at is None
    assert store.phase_samples(job.id) == []


def test_timing_migration_upgrades_version_one_and_fresh_version_two_is_stable(tmp_path):
    database_path = tmp_path / "jobs.sqlite3"
    tmp_path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                input_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                log_path TEXT NOT NULL,
                preset TEXT NOT NULL,
                color_correction TEXT NOT NULL,
                output_scale REAL NOT NULL,
                target_width INTEGER NOT NULL,
                target_height INTEGER NOT NULL,
                frame_count INTEGER NOT NULL,
                runtime_profile_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                stage TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                output_filename TEXT,
                error TEXT,
                requires_preflight INTEGER NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
        connection.execute(
            """
            INSERT INTO jobs VALUES (
                'version-one', 'private.mp4', '/private/input.mp4', '/private/output.mp4',
                '/private/log', '3b-safe', 'lab', 1.0, 640, 360, 100,
                'seedvr2:3b-safe', 'queued', 0, 'queued',
                '2026-08-12T00:00:00+00:00', '2026-08-12T00:00:00+00:00',
                NULL, NULL, 0, 0, 4.0, 640, 360
            )
            """
        )

    store = JobStore(tmp_path)
    store.initialize()
    migrated = store.get("version-one")
    assert migrated is not None
    assert migrated.started_at is None
    assert migrated.finished_at is None
    assert migrated.last_heartbeat_at is None
    assert migrated.last_progress_at is None
    assert migrated.progress_source == "none"
    assert migrated.eta_confidence == "none"
    assert migrated.last_event_invocation is None
    assert migrated.last_event_sequence == -1
    assert migrated.last_work_sequence == -1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        schema_before = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    store.initialize()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == schema_before


def test_timing_public_job_separates_heartbeat_and_work_staleness(tmp_path):
    """Fresh heartbeats must show alive-but-not-advancing instead of healthy work."""
    settings = Settings.from_environment().with_data_root(tmp_path, None)
    store = JobStore(tmp_path)
    store.initialize()
    service = JobService(settings, store, ValidProbe(), CompletedRunner())
    job = _create_timing_test_job(store, "public-timing")
    queued_at = datetime(2026, 8, 12, 5, 0, tzinfo=UTC)
    assert service.public_job(job, now=queued_at)["elapsed_seconds"] is None
    store._now = lambda: queued_at.isoformat()
    assert store.claim_next_queued() is not None
    assert store.record_report(
        job.id,
        _phase_report(sequence=1, work_sequence=1, current_unit=1, elapsed_seconds=1),
        now=queued_at,
    )
    assert store.record_report(
        job.id,
        _heartbeat_report(sequence=2, work_sequence=1),
        now=queued_at + timedelta(seconds=250),
    )

    payload = service.public_job(
        store.get(job.id),
        now=queued_at + timedelta(seconds=301),
    )
    assert payload["elapsed_seconds"] == 301
    assert payload["heartbeat_stale"] is False
    assert payload["progress_stale"] is True
    assert payload["eta_low_seconds"] is None
    assert payload["eta_high_seconds"] is None
    assert payload["eta_confidence"] == "none"


def test_eta_samples_include_only_valid_comparable_history_and_earlier_chunks(
    tmp_path,
):
    """Wrong-profile, wrong-workload, invalid, and active metrics must not calibrate ETA."""
    fingerprint = (
        "seedvr2:3b-safe:apple-mps:scale=1:batch=5:chunk=25:overlap=4:"
        "dit_cache=disabled:vae_cache=disabled"
    )
    store = JobStore(tmp_path)
    store.initialize()
    job = _create_timing_test_job(store, "eta-samples", fingerprint=fingerprint)
    assert store.claim_next_queued() is not None
    now = datetime.now(UTC)
    assert store.record_report(
        job.id,
        _phase_report(
            sequence=1,
            work_sequence=1,
            current_unit=10,
            elapsed_seconds=100,
            chunk_index=1,
        ),
        now=now,
    )
    assert store.record_report(
        job.id,
        _phase_report(
            sequence=2,
            work_sequence=2,
            current_unit=5,
            elapsed_seconds=50,
            chunk_index=2,
        ),
        now=now + timedelta(seconds=1),
    )
    active_bucket = workload_bucket(640 * 360 * 25)
    with sqlite3.connect(store.database_path) as connection:
        connection.executemany(
            """
            INSERT INTO performance_samples (
                sample_group, phase, seconds_per_unit, workload_bucket,
                runtime_profile_fingerprint, sample_date
            ) VALUES (?, ?, ?, ?, ?, '2026-08-12')
            """,
            [
                ("matching", "encoding", 10.0, active_bucket, fingerprint),
                ("near-bucket", "encoding", 11.0, active_bucket + 1, fingerprint),
                ("wrong-profile", "encoding", 1.0, active_bucket, fingerprint + ":other"),
                ("far-bucket", "encoding", 1.0, active_bucket + 2, fingerprint),
                ("nonfinite", "encoding", float("inf"), active_bucket, fingerprint),
                ("nonnumeric", "encoding", "not-a-rate", active_bucket, fingerprint),
            ],
        )

    samples = store.eta_samples(job.id)

    assert {sample.sample_group for sample in samples} == {
        "current-run",
        "matching",
        "near-bucket",
    }
    current = [sample for sample in samples if sample.sample_group == "current-run"]
    assert len(current) == 1
    assert current[0].elapsed_seconds == 100
    assert current[0].completed_units == 10
    assert all(sample.valid for sample in samples)


def test_update_eta_clears_calibrating_bounds_without_losing_measured_progress(
    tmp_path,
):
    """A calibrating estimate must clear stale bounds but preserve measured counters."""
    store = JobStore(tmp_path)
    store.initialize()
    job = _create_timing_test_job(store, "eta-update")
    assert store.claim_next_queued() is not None
    assert store.record_report(
        job.id,
        _phase_report(sequence=1, work_sequence=1, current_unit=2, elapsed_seconds=20),
    )
    store.update_eta(job.id, EtaEstimate(100, 200, "low", "historical"))
    historical = store.get(job.id)
    assert historical is not None
    assert historical.progress_source == "historical"
    store.update_eta(job.id, EtaEstimate(90, 180, "low", "measured"))
    measured = store.get(job.id)
    assert measured is not None
    assert measured.progress_source == "measured"
    store.update_eta(job.id, EtaEstimate(None, None, "none", "none"))

    updated = store.get(job.id)
    assert updated is not None
    assert updated.eta_low_seconds is None
    assert updated.eta_high_seconds is None
    assert updated.eta_confidence == "none"
    assert updated.progress_source == "measured"


def test_service_clamps_eta_to_remaining_process_deadline(tmp_path, monkeypatch):
    """Elapsed process time must reduce ETA ceiling instead of restarting deadline."""
    monkeypatch.setenv("VIDEO_UPSCALE_MAX_PROCESS_SECONDS", "300")
    settings = Settings.from_environment().with_data_root(tmp_path, None)
    store = JobStore(tmp_path)
    store.initialize()
    service = JobService(settings, store, ValidProbe(), CompletedRunner())
    job = _create_timing_test_job(store, "eta-deadline")
    started_at = datetime.now(UTC) - timedelta(seconds=200)
    monkeypatch.setattr(store, "_now", lambda: started_at.isoformat())
    assert store.claim_next_queued() is not None
    _seed_eta_history(
        store,
        fingerprint=job.runtime_profile_fingerprint,
        sample_group_prefix="deadline",
        seconds_multiplier=100,
    )

    assert service._record_progress(
        job,
        _phase_report(sequence=1, work_sequence=1, current_unit=5, elapsed_seconds=50),
    )

    updated = store.get(job.id)
    assert updated is not None
    assert updated.eta_low_seconds is not None
    assert updated.eta_high_seconds is not None
    assert 0 <= updated.eta_low_seconds <= updated.eta_high_seconds <= 100


def test_accepted_chunk_completion_clears_eta_after_skipped_final_phase_report(
    tmp_path,
):
    """Immediate chunk completion must not retain ETA from older phase counters."""
    settings = Settings.from_environment().with_data_root(tmp_path, None)
    store = JobStore(tmp_path)
    store.initialize()
    service = JobService(settings, store, ValidProbe(), CompletedRunner())
    job = _create_timing_test_job(store, "eta-chunk-refresh")
    assert store.claim_next_queued() is not None
    _seed_eta_history(
        store,
        fingerprint=job.runtime_profile_fingerprint,
        sample_group_prefix="chunk-refresh",
    )
    assert service._record_progress(
        job,
        _phase_report(sequence=1, work_sequence=1, current_unit=5, elapsed_seconds=50),
    )
    before = store.get(job.id)
    assert before is not None
    assert before.eta_low_seconds is not None

    assert service._record_progress(
        job,
        _chunk_completed_report(sequence=2, work_sequence=2),
    )

    refreshed = store.get(job.id)
    assert refreshed is not None
    assert refreshed.phase_name is None
    assert refreshed.eta_low_seconds is None
    assert refreshed.eta_high_seconds is None
    assert refreshed.eta_confidence == "none"
    assert refreshed.progress_source == "measured"


class EtaReportingRunner:
    def __init__(self, *, refresh: bool = False):
        self.reported = threading.Event()
        self.refresh_requested = threading.Event()
        self.refreshed = threading.Event()
        self.release = threading.Event()
        self.refresh = refresh

    def preflight(self, job, limits, report_progress, is_cancelled):
        raise AssertionError("unexpected preflight")

    def run(self, job, report_progress, is_cancelled):
        report_progress(
            _phase_report(
                sequence=1,
                work_sequence=1,
                current_unit=5,
                elapsed_seconds=50,
            )
        )
        self.reported.set()
        if self.refresh:
            self.refresh_requested.wait(1)
            report_progress(
                _phase_report(
                    sequence=2,
                    work_sequence=1,
                    current_unit=5,
                    elapsed_seconds=50,
                )
            )
            self.refreshed.set()
        self.release.wait(1)
        Path(job.output_path).write_bytes(b"mp4-result")


class CurrentRunEtaReportingRunner:
    def __init__(self):
        self.reported = threading.Event()
        self.release = threading.Event()

    def preflight(self, job, limits, report_progress, is_cancelled):
        raise AssertionError("unexpected preflight")

    def run(self, job, report_progress, is_cancelled):
        for sequence, (phase, elapsed_seconds) in enumerate(
            {
                "encoding": 100,
                "upscaling": 300,
                "decoding": 500,
                "postprocessing": 100,
            }.items(),
            start=1,
        ):
            report_progress(
                _phase_report(
                    sequence=sequence,
                    work_sequence=sequence,
                    current_unit=10,
                    elapsed_seconds=elapsed_seconds,
                    phase=phase,
                    chunk_index=1,
                )
            )
        report_progress(
            _phase_report(
                sequence=5,
                work_sequence=5,
                current_unit=5,
                elapsed_seconds=50,
                chunk_index=2,
            )
        )
        self.reported.set()
        self.release.wait(1)
        Path(job.output_path).write_bytes(b"mp4-result")


def _seed_eta_history(
    store: JobStore,
    *,
    fingerprint: str,
    sample_group_prefix: str,
    seconds_multiplier: float = 1.0,
):
    bucket = workload_bucket(640 * 360 * 25)
    phase_seconds = {
        "encoding": 100,
        "upscaling": 300,
        "decoding": 500,
        "postprocessing": 100,
    }
    with sqlite3.connect(store.database_path) as connection:
        connection.executemany(
            """
            INSERT INTO performance_samples (
                sample_group, phase, seconds_per_unit, workload_bucket,
                runtime_profile_fingerprint, sample_date
            ) VALUES (?, ?, ?, ?, ?, '2026-08-12')
            """,
            [
                (
                    f"{sample_group_prefix}-{index}",
                    phase,
                    seconds * seconds_multiplier / 10,
                    bucket,
                    fingerprint,
                )
                for index in range(3)
                for phase, seconds in phase_seconds.items()
            ],
        )


def test_first_active_job_exposes_measured_progress_while_eta_calibrates(tmp_path):
    """No history must never fabricate ETA bounds or hide real phase counters."""
    runner = EtaReportingRunner()
    client = make_client(tmp_path, runner=runner)
    job_id = submit_video(client).json()["id"]
    assert runner.reported.wait(1)

    payload = client.get(f"/api/jobs/{job_id}").json()
    runner.release.set()

    assert payload["eta_low_seconds"] is None
    assert payload["eta_high_seconds"] is None
    assert payload["eta_confidence"] == "none"
    assert payload["progress_source"] == "measured"


def test_api_eta_exposes_measured_source_from_completed_current_run_chunks(tmp_path):
    """Current-run calibration must serialize measured ETA provenance."""
    runner = CurrentRunEtaReportingRunner()
    client = make_client(tmp_path, runner=runner)
    job_id = submit_video(client).json()["id"]
    assert runner.reported.wait(1)

    payload = client.get(f"/api/jobs/{job_id}").json()
    runner.release.set()

    assert payload["eta_low_seconds"] is not None
    assert payload["eta_high_seconds"] is not None
    assert payload["eta_confidence"] == "low"
    assert payload["progress_source"] == "measured"


def test_api_eta_learns_anonymous_history_without_job_rows_or_profile_noise(tmp_path):
    """ETA refresh must use durable matching history, not deleted jobs or noisy profiles."""
    fingerprint = (
        "seedvr2:3b-safe:apple-mps:scale=1:batch=5:chunk=25:overlap=4:"
        "dit_cache=disabled:vae_cache=disabled"
    )
    runner = EtaReportingRunner(refresh=True)
    app = create_app(
        data_root=tmp_path,
        runner=runner,
        media_probe=ValidProbe(),
    )
    client = make_client_headers_client(app)
    store = app.state.job_service.store
    source_job_ids = []
    for index in range(3):
        source = _create_timing_test_job(store, f"eta-source-{index}")
        store.fail(source.id, "test source is already anonymized")
        source_job_ids.append(source.id)
    _seed_eta_history(
        store,
        fingerprint=fingerprint,
        sample_group_prefix="anonymous-match",
    )

    job_id = submit_video(client).json()["id"]
    assert runner.reported.wait(1)
    before = client.get(f"/api/jobs/{job_id}").json()
    assert before["eta_confidence"] == "medium"
    assert before["progress_source"] == "historical"
    assert isinstance(before["eta_low_seconds"], int)
    assert isinstance(before["eta_high_seconds"], int)
    assert before["eta_low_seconds"] <= before["eta_high_seconds"]

    for source_job_id in source_job_ids:
        assert store.delete(source_job_id)
    _seed_eta_history(
        store,
        fingerprint=fingerprint.replace("3b-safe", "7b-fp8-experimental"),
        sample_group_prefix="wrong-profile",
        seconds_multiplier=100,
    )
    runner.refresh_requested.set()
    assert runner.refreshed.wait(1)
    after = client.get(f"/api/jobs/{job_id}").json()
    runner.release.set()

    assert after["eta_low_seconds"] == before["eta_low_seconds"]
    assert after["eta_high_seconds"] == before["eta_high_seconds"]
    assert after["eta_confidence"] == before["eta_confidence"] == "medium"
