from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from .config import Settings
from .job_store import JobStore


def cleanup_expired_jobs(
    store: JobStore,
    *,
    older_than_hours: int,
    apply: bool,
) -> list[str]:
    cutoff = (datetime.now(UTC) - timedelta(hours=older_than_hours)).isoformat()
    jobs = store.expired_terminal(cutoff)
    if not apply:
        return [job.id for job in jobs]
    for job in jobs:
        for path in (job.input_path, job.output_path, job.log_path):
            path.unlink(missing_ok=True)
        for path in store.staging.glob(f"{job.id}*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        store.delete(job.id)
    return [job.id for job in jobs]


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean terminal Video Upscale jobs")
    parser.add_argument("--older-than-hours", type=int, default=24)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.older_than_hours < 0:
        parser.error("--older-than-hours must be non-negative")
    settings = Settings.from_environment()
    store = JobStore(settings.data_root)
    store.initialize()
    for job_id in cleanup_expired_jobs(
        store,
        older_than_hours=args.older_than_hours,
        apply=args.apply,
    ):
        print(f"{'removed' if args.apply else 'would remove'} terminal job {job_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
