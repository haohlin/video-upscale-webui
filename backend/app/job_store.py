from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .domain import Job, MediaInfo


class JobStore:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.inputs = data_root / "inputs"
        self.results = data_root / "results"
        self.logs = data_root / "logs"
        self.staging = data_root / "staging"
        self.database_path = data_root / "jobs.sqlite3"

    def initialize(self) -> None:
        for directory in (self.inputs, self.results, self.logs, self.staging):
            directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
            ).fetchone()
            if not table_exists:
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
                    )
                    """
                )
                connection.execute("PRAGMA user_version = 1")
            elif version == 0:
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
                }
                migrations = {
                    "output_scale": (
                        "ALTER TABLE jobs ADD COLUMN output_scale REAL NOT NULL DEFAULT 2.0"
                    ),
                    "target_width": (
                        "ALTER TABLE jobs ADD COLUMN target_width INTEGER NOT NULL DEFAULT 0"
                    ),
                    "target_height": (
                        "ALTER TABLE jobs ADD COLUMN target_height INTEGER NOT NULL DEFAULT 0"
                    ),
                    "frame_count": (
                        "ALTER TABLE jobs ADD COLUMN frame_count INTEGER NOT NULL DEFAULT 0"
                    ),
                    "runtime_profile_fingerprint": (
                        "ALTER TABLE jobs ADD COLUMN runtime_profile_fingerprint "
                        "TEXT NOT NULL DEFAULT 'legacy:unknown'"
                    ),
                }
                for name, statement in migrations.items():
                    if name not in columns:
                        connection.execute(statement)
                connection.execute(
                    "UPDATE jobs SET target_width = width * 2, target_height = height * 2 "
                    "WHERE target_width = 0 OR target_height = 0"
                )
                connection.execute("PRAGMA user_version = 1")

    def create(
        self,
        *,
        job_id: str,
        original_filename: str,
        input_path: Path,
        output_path: Path,
        log_path: Path,
        preset: str,
        color_correction: str,
        media: MediaInfo,
        output_scale: float = 2.0,
        target_width: int | None = None,
        target_height: int | None = None,
        runtime_profile_fingerprint: str = "legacy:unknown",
    ) -> Job:
        now = self._now()
        requires_preflight = preset == "7b-fp8-experimental"
        target_width = target_width if target_width is not None else media.width * 2
        target_height = target_height if target_height is not None else media.height * 2
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, original_filename, input_path, output_path, log_path, preset,
                    color_correction, output_scale, target_width, target_height,
                    frame_count, runtime_profile_fingerprint, status, progress, stage,
                    created_at, updated_at, output_filename, error, requires_preflight,
                    cancel_requested, duration_seconds, width, height
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 'queued', ?, ?, NULL, NULL, ?, 0, ?, ?, ?)
                """,
                (
                    job_id,
                    original_filename,
                    str(input_path),
                    str(output_path),
                    str(log_path),
                    preset,
                    color_correction,
                    output_scale,
                    target_width,
                    target_height,
                    media.frame_count,
                    runtime_profile_fingerprint,
                    now,
                    now,
                    int(requires_preflight),
                    media.duration_seconds,
                    media.width,
                    media.height,
                ),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._to_job(row) if row else None

    def list(self) -> list[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._to_job(row) for row in rows]

    def pending_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running', 'preflight')"
            ).fetchone()
        return int(row[0])

    def expired_terminal(self, cutoff: str) -> list[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('completed', 'failed', 'cancelled')
                  AND updated_at < ?
                ORDER BY updated_at
                """,
                (cutoff,),
            ).fetchall()
        return [self._to_job(row) for row in rows]

    def claim_next_queued(self) -> Job | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # SQLite also protects queue exclusivity if deployment accidentally starts two servers.
            active = connection.execute(
                "SELECT 1 FROM jobs WHERE status IN ('running', 'preflight') LIMIT 1"
            ).fetchone()
            if active:
                return None
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = self._now()
            connection.execute(
                "UPDATE jobs SET status = 'running', stage = 'starting', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return self._to_job(row)

    def has_queued(self) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM jobs WHERE status = 'queued' LIMIT 1"
            ).fetchone() is not None

    def update_progress(self, job_id: str, progress: int, stage: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET progress = ?, stage = ?, updated_at = ? WHERE id = ? AND status IN ('running', 'preflight')",
                (max(0, min(100, int(progress))), stage[:120], self._now(), job_id),
            )

    def mark_preflight(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'preflight', stage = '7b-safety-probe', updated_at = ? WHERE id = ?",
                (self._now(), job_id),
            )

    def mark_running(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'running', progress = 0, stage = 'upscaling', updated_at = ? WHERE id = ?",
                (self._now(), job_id),
            )

    def complete(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', progress = 100, stage = 'completed',
                    output_filename = ?, updated_at = ?
                WHERE id = ?
                """,
                (f"{job_id}.mp4", self._now(), job_id),
            )

    def recover_interrupted(self) -> list[Job]:
        """Make stale active work terminal so a restart cannot block queue progress."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('running', 'preflight')"
            ).fetchall()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', stage = 'interrupted',
                    error = 'Interrupted by application restart', updated_at = ?
                WHERE status IN ('running', 'preflight')
                """,
                (self._now(),),
            )
        return [self._to_job(row) for row in rows]

    def fail(self, job_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'failed', stage = 'failed', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:1000], self._now(), job_id),
            )

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if not job:
            return None
        with self._connect() as connection:
            if job.status == "queued":
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled', stage = 'cancelled', cancel_requested = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (self._now(), job_id),
                )
            elif job.status in ("running", "preflight"):
                connection.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (self._now(), job_id),
                )
        return self.get(job_id)

    def mark_cancelled(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', stage = 'cancelled', cancel_requested = 1, updated_at = ?
                WHERE id = ?
                """,
                (self._now(), job_id),
            )

    def cancellation_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row[0])

    def delete(self, job_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cursor.rowcount == 1

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            original_filename=row["original_filename"],
            input_path=Path(row["input_path"]),
            output_path=Path(row["output_path"]),
            log_path=Path(row["log_path"]),
            preset=row["preset"],
            color_correction=row["color_correction"],
            output_scale=row["output_scale"],
            target_width=row["target_width"],
            target_height=row["target_height"],
            frame_count=row["frame_count"],
            runtime_profile_fingerprint=row["runtime_profile_fingerprint"],
            status=row["status"],
            progress=row["progress"],
            stage=row["stage"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            output_filename=row["output_filename"],
            error=row["error"],
            requires_preflight=bool(row["requires_preflight"]),
            cancel_requested=bool(row["cancel_requested"]),
            duration_seconds=row["duration_seconds"],
            width=row["width"],
            height=row["height"],
        )
