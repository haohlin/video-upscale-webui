from __future__ import annotations

import math
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .domain import Job, MediaInfo, PhaseSample
from .progress import MAX_COUNTER, ProgressEvent, ProgressReport


HEARTBEAT_STALE_SECONDS = 120
PROGRESS_STALE_SECONDS = 300


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
                version = 1
            elif version == 0:
                self._migrate_scale_schema(connection)
                connection.execute("PRAGMA user_version = 1")
                version = 1
            if version == 1:
                self._migrate_timing_schema(connection)
                connection.execute("PRAGMA user_version = 2")

    @staticmethod
    def _migrate_scale_schema(connection: sqlite3.Connection) -> None:
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

    @staticmethod
    def _migrate_timing_schema(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
        }
        migrations = {
            "started_at": "ALTER TABLE jobs ADD COLUMN started_at TEXT",
            "finished_at": "ALTER TABLE jobs ADD COLUMN finished_at TEXT",
            "last_heartbeat_at": "ALTER TABLE jobs ADD COLUMN last_heartbeat_at TEXT",
            "last_progress_at": "ALTER TABLE jobs ADD COLUMN last_progress_at TEXT",
            "progress_source": (
                "ALTER TABLE jobs ADD COLUMN progress_source TEXT NOT NULL DEFAULT 'none'"
            ),
            "phase_name": "ALTER TABLE jobs ADD COLUMN phase_name TEXT",
            "phase_current": "ALTER TABLE jobs ADD COLUMN phase_current INTEGER",
            "phase_total": "ALTER TABLE jobs ADD COLUMN phase_total INTEGER",
            "chunk_current": "ALTER TABLE jobs ADD COLUMN chunk_current INTEGER",
            "chunk_total": "ALTER TABLE jobs ADD COLUMN chunk_total INTEGER",
            "eta_low_seconds": "ALTER TABLE jobs ADD COLUMN eta_low_seconds INTEGER",
            "eta_high_seconds": "ALTER TABLE jobs ADD COLUMN eta_high_seconds INTEGER",
            "eta_confidence": (
                "ALTER TABLE jobs ADD COLUMN eta_confidence TEXT NOT NULL DEFAULT 'none'"
            ),
            "last_event_invocation": (
                "ALTER TABLE jobs ADD COLUMN last_event_invocation TEXT"
            ),
            "last_event_sequence": (
                "ALTER TABLE jobs ADD COLUMN last_event_sequence INTEGER NOT NULL DEFAULT -1"
            ),
            "last_work_sequence": (
                "ALTER TABLE jobs ADD COLUMN last_work_sequence INTEGER NOT NULL DEFAULT -1"
            ),
        }
        for name, statement in migrations.items():
            if name not in columns:
                connection.execute(statement)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_phase_metrics (
                job_id TEXT NOT NULL,
                invocation TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                phase TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                completed_units INTEGER NOT NULL,
                total_units INTEGER NOT NULL,
                completed_unique_frames INTEGER NOT NULL,
                chunk_unique_frames INTEGER NOT NULL,
                chunk_context_frames INTEGER NOT NULL,
                total_unique_frames INTEGER NOT NULL,
                output_pixel_frames INTEGER NOT NULL,
                elapsed_seconds REAL NOT NULL,
                runtime_profile_fingerprint TEXT NOT NULL,
                valid_sample INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (job_id, invocation, chunk_index, phase),
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS performance_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_group TEXT NOT NULL,
                phase TEXT NOT NULL,
                seconds_per_unit REAL NOT NULL,
                workload_bucket INTEGER NOT NULL,
                runtime_profile_fingerprint TEXT NOT NULL,
                sample_date TEXT NOT NULL,
                CHECK (seconds_per_unit > 0)
            )
            """
        )

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
                "UPDATE jobs SET status = 'running', stage = 'starting', "
                "started_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
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
                "UPDATE jobs SET progress = MIN(99, MAX(progress, ?)), stage = ?, updated_at = ? WHERE id = ? AND status IN ('running', 'preflight')",
                (max(0, min(99, int(progress))), stage[:64], self._now(), job_id),
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
                """
                UPDATE jobs
                SET status = 'running', progress = 0, stage = 'upscaling',
                    phase_name = NULL, phase_current = NULL, phase_total = NULL,
                    chunk_current = NULL, chunk_total = NULL,
                    eta_low_seconds = NULL, eta_high_seconds = NULL,
                    eta_confidence = 'none', last_event_invocation = 'full',
                    last_event_sequence = -1, last_work_sequence = -1,
                    updated_at = ?
                WHERE id = ?
                """,
                (self._now(), job_id),
            )

    def record_report(
        self,
        job_id: str,
        report: ProgressReport,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(UTC)
        timestamp = current.isoformat()
        invocation = report.invocation
        event = report.event
        if invocation not in {"preflight", "full"} or len(invocation) > 64:
            return False
        if not self._valid_work_sequence(report.work_sequence):
            return False
        if event is not None and not self._valid_event(event):
            return False
        if event is not None and (
            report.measured_work != event.measured_work
            or report.work_sequence != event.work_sequence
        ):
            return False

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ? AND status IN ('running', 'preflight')",
                (job_id,),
            ).fetchone()
            if row is None:
                return False

            stored_invocation = row["last_event_invocation"]
            event_baseline = int(row["last_event_sequence"])
            work_baseline = int(row["last_work_sequence"])
            if stored_invocation is None:
                event_baseline = -1
                work_baseline = -1
            elif invocation != stored_invocation:
                if not (stored_invocation == "preflight" and invocation == "full"):
                    return False
                event_baseline = -1
                work_baseline = -1

            if event is not None and event.sequence <= event_baseline:
                return False

            if event is not None and event.event_type == "phase_progress":
                assert event.chunk_index is not None
                assert event.phase is not None
                prior_metric = connection.execute(
                    """
                    SELECT * FROM job_phase_metrics
                    WHERE job_id = ? AND invocation = ? AND chunk_index = ? AND phase = ?
                    """,
                    (job_id, invocation, event.chunk_index, event.phase),
                ).fetchone()
                if prior_metric is not None and not self._metric_is_monotonic(
                    prior_metric, event
                ):
                    return False

            work_sequence = event.work_sequence if event is not None else report.work_sequence
            fresh_work = bool(
                report.measured_work
                and work_sequence >= 0
                and work_sequence > work_baseline
            )
            prior_heartbeat_fresh = self._fresh_at(
                row["last_heartbeat_at"] or row["started_at"],
                current,
                HEARTBEAT_STALE_SECONDS,
            )
            prior_progress_fresh = self._fresh_at(
                row["last_progress_at"] or row["started_at"],
                current,
                PROGRESS_STALE_SECONDS,
            )
            heartbeat = event is not None and event.event_type == "heartbeat"
            progress = int(row["progress"])
            if not heartbeat:
                progress = min(99, max(progress, max(0, min(99, int(report.percent)))))
            phase_name = row["phase_name"]
            phase_current = row["phase_current"]
            phase_total = row["phase_total"]
            chunk_current = row["chunk_current"]
            chunk_total = row["chunk_total"]
            if event is not None and event.event_type == "phase_progress":
                phase_name = event.phase
                phase_current = event.current_unit
                phase_total = event.total_units
                chunk_current = event.chunk_index
                chunk_total = event.chunk_count
            elif event is not None and event.event_type == "chunk_completed":
                phase_name = None
                phase_current = None
                phase_total = None
                chunk_current = event.chunk_index
                chunk_total = event.chunk_count

            connection.execute(
                """
                UPDATE jobs
                SET progress = ?, stage = ?, updated_at = ?, last_heartbeat_at = ?,
                    last_progress_at = ?, progress_source = ?, phase_name = ?,
                    phase_current = ?, phase_total = ?, chunk_current = ?, chunk_total = ?,
                    last_event_invocation = ?, last_event_sequence = ?,
                    last_work_sequence = ?
                WHERE id = ?
                """,
                (
                    progress,
                    report.stage[:64],
                    timestamp,
                    timestamp,
                    timestamp if fresh_work else row["last_progress_at"],
                    "measured" if fresh_work else row["progress_source"],
                    phase_name,
                    phase_current,
                    phase_total,
                    chunk_current,
                    chunk_total,
                    invocation,
                    event.sequence if event is not None else event_baseline,
                    work_sequence if fresh_work else work_baseline,
                    job_id,
                ),
            )

            if event is not None and event.event_type == "phase_progress":
                self._upsert_phase_metric(
                    connection,
                    row=row,
                    invocation=invocation,
                    event=event,
                    timestamp=timestamp,
                    valid_sample=bool(
                        invocation == "full"
                        and fresh_work
                        and event.current_unit == event.total_units
                        and event.elapsed_seconds is not None
                        and event.elapsed_seconds > 0
                        and prior_heartbeat_fresh
                        and prior_progress_fresh
                    ),
                )
            elif event is not None and event.event_type == "chunk_completed":
                connection.execute(
                    """
                    UPDATE job_phase_metrics
                    SET finished_at = COALESCE(finished_at, ?)
                    WHERE job_id = ? AND invocation = ? AND chunk_index = ?
                    """,
                    (timestamp, job_id, invocation, event.chunk_index),
                )
        return True

    def complete(
        self,
        job_id: str,
        *,
        publish_performance: bool,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(UTC)
        timestamp = current.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ? AND status IN ('running', 'preflight')",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            if (
                publish_performance
                and row["runtime_profile_fingerprint"] != "legacy:unknown"
                and row["progress_source"] == "measured"
                and self._fresh_at(
                    row["last_heartbeat_at"], current, HEARTBEAT_STALE_SECONDS
                )
                and self._fresh_at(
                    row["last_progress_at"], current, PROGRESS_STALE_SECONDS
                )
            ):
                metrics = connection.execute(
                    """
                    SELECT * FROM job_phase_metrics
                    WHERE job_id = ? AND invocation = 'full' AND valid_sample = 1
                      AND finished_at IS NOT NULL AND completed_units > 0
                      AND elapsed_seconds > 0
                    ORDER BY chunk_index, phase
                    """,
                    (job_id,),
                ).fetchall()
                if metrics:
                    sample_group = secrets.token_hex(16)
                    sample_date = current.astimezone(UTC).date().isoformat()
                    connection.executemany(
                        """
                        INSERT INTO performance_samples (
                            sample_group, phase, seconds_per_unit, workload_bucket,
                            runtime_profile_fingerprint, sample_date
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                sample_group,
                                metric["phase"],
                                metric["elapsed_seconds"] / metric["completed_units"],
                                self._workload_bucket(metric["output_pixel_frames"]),
                                metric["runtime_profile_fingerprint"],
                                sample_date,
                            )
                            for metric in metrics
                        ],
                    )
            connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', progress = 100, stage = 'completed',
                    output_filename = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (f"{job_id}.mp4", timestamp, timestamp, job_id),
            )

    def phase_samples(self, job_id: str) -> list[PhaseSample]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_phase_metrics
                WHERE job_id = ? ORDER BY chunk_index, phase
                """,
                (job_id,),
            ).fetchall()
        return [
            PhaseSample(
                sample_group="current-run",
                phase=row["phase"],
                elapsed_seconds=float(row["elapsed_seconds"]),
                completed_units=int(row["completed_units"]),
                runtime_profile_fingerprint=row["runtime_profile_fingerprint"],
                workload_bucket=self._workload_bucket(row["output_pixel_frames"]),
                valid=bool(row["valid_sample"]),
            )
            for row in rows
        ]

    def recover_interrupted(self) -> list[Job]:
        """Make stale active work terminal so a restart cannot block queue progress."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('running', 'preflight')"
            ).fetchall()
            now = self._now()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', stage = 'interrupted',
                    error = 'Interrupted by application restart', finished_at = ?, updated_at = ?
                WHERE status IN ('running', 'preflight')
                """,
                (now, now),
            )
        return [self._to_job(row) for row in rows]

    def fail(self, job_id: str, error: str) -> None:
        with self._connect() as connection:
            now = self._now()
            connection.execute(
                """
                UPDATE jobs SET status = 'failed', stage = 'failed', error = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:1000], now, now, job_id),
            )

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if not job:
            return None
        with self._connect() as connection:
            if job.status == "queued":
                now = self._now()
                connection.execute(
                    """
                    UPDATE jobs SET status = 'cancelled', stage = 'cancelled',
                        cancel_requested = 1, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, job_id),
                )
            elif job.status in ("running", "preflight"):
                connection.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (self._now(), job_id),
                )
        return self.get(job_id)

    def mark_cancelled(self, job_id: str) -> None:
        with self._connect() as connection:
            now = self._now()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', stage = 'cancelled', cancel_requested = 1,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, job_id),
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

    @staticmethod
    def _valid_work_sequence(value: object) -> bool:
        return type(value) is int and -1 <= value <= MAX_COUNTER

    @classmethod
    def _valid_event(cls, event: ProgressEvent) -> bool:
        counter_names = (
            "sequence",
            "work_sequence",
            "current_unit",
            "total_units",
            "chunk_index",
            "chunk_count",
            "completed_unique_frames",
            "chunk_unique_frames",
            "chunk_context_frames",
            "total_unique_frames",
        )
        for name in counter_names:
            value = getattr(event, name)
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_COUNTER):
                return False
        if (
            len(event.event_type) > 64
            or event.phase is not None
            and len(event.phase) > 64
        ):
            return False
        if event.elapsed_seconds is not None and (
            type(event.elapsed_seconds) not in {int, float}
            or not math.isfinite(event.elapsed_seconds)
            or not 0 <= event.elapsed_seconds <= MAX_COUNTER
        ):
            return False
        if event.event_type == "phase_progress":
            required = (
                event.phase,
                event.current_unit,
                event.total_units,
                event.chunk_index,
                event.chunk_count,
                event.completed_unique_frames,
                event.chunk_unique_frames,
                event.chunk_context_frames,
                event.total_unique_frames,
            )
            if any(value is None for value in required) or not event.measured_work:
                return False
        return True

    @staticmethod
    def _metric_is_monotonic(row: sqlite3.Row, event: ProgressEvent) -> bool:
        assert event.current_unit is not None
        assert event.total_units is not None
        assert event.completed_unique_frames is not None
        assert event.chunk_unique_frames is not None
        assert event.chunk_context_frames is not None
        assert event.total_unique_frames is not None
        if event.current_unit < row["completed_units"]:
            return False
        fixed_values = {
            "total_units": event.total_units,
            "completed_unique_frames": event.completed_unique_frames,
            "chunk_unique_frames": event.chunk_unique_frames,
            "chunk_context_frames": event.chunk_context_frames,
            "total_unique_frames": event.total_unique_frames,
        }
        if any(row[name] != value for name, value in fixed_values.items()):
            return False
        return not (
            event.elapsed_seconds is not None
            and event.elapsed_seconds < row["elapsed_seconds"]
        )

    @staticmethod
    def _upsert_phase_metric(
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        invocation: str,
        event: ProgressEvent,
        timestamp: str,
        valid_sample: bool,
    ) -> None:
        assert event.phase is not None
        assert event.current_unit is not None
        assert event.total_units is not None
        assert event.chunk_index is not None
        assert event.completed_unique_frames is not None
        assert event.chunk_unique_frames is not None
        assert event.chunk_context_frames is not None
        assert event.total_unique_frames is not None
        completed = event.current_unit == event.total_units
        elapsed_seconds = float(event.elapsed_seconds or 0)
        connection.execute(
            """
            INSERT INTO job_phase_metrics (
                job_id, invocation, chunk_index, phase, started_at, finished_at,
                completed_units, total_units, completed_unique_frames,
                chunk_unique_frames, chunk_context_frames, total_unique_frames,
                output_pixel_frames, elapsed_seconds, runtime_profile_fingerprint,
                valid_sample
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, invocation, chunk_index, phase) DO UPDATE SET
                finished_at = CASE
                    WHEN excluded.completed_units = excluded.total_units
                    THEN excluded.finished_at ELSE job_phase_metrics.finished_at END,
                completed_units = excluded.completed_units,
                total_units = excluded.total_units,
                completed_unique_frames = excluded.completed_unique_frames,
                chunk_unique_frames = excluded.chunk_unique_frames,
                chunk_context_frames = excluded.chunk_context_frames,
                total_unique_frames = excluded.total_unique_frames,
                output_pixel_frames = excluded.output_pixel_frames,
                elapsed_seconds = excluded.elapsed_seconds,
                runtime_profile_fingerprint = excluded.runtime_profile_fingerprint,
                valid_sample = CASE
                    WHEN job_phase_metrics.finished_at IS NOT NULL
                    THEN job_phase_metrics.valid_sample
                    ELSE MAX(job_phase_metrics.valid_sample, excluded.valid_sample)
                END
            """,
            (
                row["id"],
                invocation,
                event.chunk_index,
                event.phase,
                timestamp,
                timestamp if completed else None,
                event.current_unit,
                event.total_units,
                event.completed_unique_frames,
                event.chunk_unique_frames,
                event.chunk_context_frames,
                event.total_unique_frames,
                row["target_width"] * row["target_height"] * event.chunk_unique_frames,
                elapsed_seconds,
                row["runtime_profile_fingerprint"],
                int(valid_sample),
            ),
        )

    @staticmethod
    def _fresh_at(value: str | None, current: datetime, limit_seconds: int) -> bool:
        if value is None:
            return False
        try:
            recorded = datetime.fromisoformat(value)
        except ValueError:
            return False
        return 0 <= (current - recorded).total_seconds() <= limit_seconds

    @staticmethod
    def _workload_bucket(pixel_frames: int) -> int:
        return max(0, int(math.log2(max(1, pixel_frames))))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            last_progress_at=row["last_progress_at"],
            progress_source=row["progress_source"],
            phase_name=row["phase_name"],
            phase_current=row["phase_current"],
            phase_total=row["phase_total"],
            chunk_current=row["chunk_current"],
            chunk_total=row["chunk_total"],
            eta_low_seconds=row["eta_low_seconds"],
            eta_high_seconds=row["eta_high_seconds"],
            eta_confidence=row["eta_confidence"],
            last_event_invocation=row["last_event_invocation"],
            last_event_sequence=row["last_event_sequence"],
            last_work_sequence=row["last_work_sequence"],
            output_filename=row["output_filename"],
            error=row["error"],
            requires_preflight=bool(row["requires_preflight"]),
            cancel_requested=bool(row["cancel_requested"]),
            duration_seconds=row["duration_seconds"],
            width=row["width"],
            height=row["height"],
        )
