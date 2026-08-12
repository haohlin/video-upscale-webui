from datetime import UTC, datetime, timedelta

from app.cleanup import cleanup_expired_jobs
from app.domain import MediaInfo
from app.job_store import JobStore


def test_cleanup_removes_only_expired_terminal_job_files(tmp_path):
    """Age cleanup must preserve every queued or running job-owned file."""
    store = JobStore(tmp_path)
    store.initialize()
    for job_id in ("active", "terminal"):
        input_path = store.inputs / f"{job_id}.mp4"
        input_path.write_bytes(b"input")
        store.create(
            job_id=job_id,
            original_filename=f"{job_id}.mp4",
            input_path=input_path,
            output_path=store.results / f"{job_id}.mp4",
            log_path=store.logs / f"{job_id}.log",
            preset="3b-safe",
            color_correction="lab",
            media=MediaInfo(duration_seconds=1, width=320, height=180),
        )
    store.fail("terminal", "done")
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    with store._connect() as connection:
        connection.execute("UPDATE jobs SET updated_at = ?", (old,))

    removed = cleanup_expired_jobs(store, older_than_hours=24, apply=True)

    assert removed == ["terminal"]
    assert (store.inputs / "active.mp4").exists()
    assert not (store.inputs / "terminal.mp4").exists()
    assert store.get("active") is not None
    assert store.get("terminal") is None
