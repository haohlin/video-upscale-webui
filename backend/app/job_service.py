from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from shutil import disk_usage
from typing import Callable

from fastapi import HTTPException, UploadFile

from .config import Settings
from .domain import (
    COLOR_CORRECTIONS,
    OUTPUT_SCALES,
    PRESETS,
    TERMINAL_STATUSES,
    Job,
    PreflightLimits,
    target_dimensions,
)
from .eta import ActiveWork, EtaEstimate, estimate_eta, workload_bucket
from .job_store import JobStore
from .media import MediaProbe, normalize_media_info
from .progress import ProgressReport
from .runner import JobCancelled, VideoRunner

ALLOWED_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm"})
CHUNK_SIZE = 1024 * 1024
MAX_LOG_READ_BYTES = 64 * 1024


class JobService:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        media_probe: MediaProbe,
        runner: VideoRunner,
        *,
        free_space_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.media_probe = media_probe
        self.runner = runner
        self._free_space_bytes = free_space_bytes or (lambda path: disk_usage(path).free)
        self._worker_lock = threading.Lock()
        self._worker_active = False
        for interrupted_job in self.store.recover_interrupted():
            self._remove_partial_artifacts(interrupted_job)
        self._ensure_worker()

    async def create_job(
        self,
        upload: UploadFile,
        preset: str | None,
        color_correction: str,
        output_scale: float | None,
    ) -> Job:
        preset = preset or self.settings.default_profile
        if preset not in PRESETS:
            raise HTTPException(422, "Unsupported processing preset")
        if color_correction not in COLOR_CORRECTIONS:
            raise HTTPException(422, "Unsupported color correction")
        output_scale = (
            self.settings.default_output_scale if output_scale is None else output_scale
        )
        if output_scale not in OUTPUT_SCALES:
            raise HTTPException(422, "Unsupported output scale")
        original_filename = upload.filename or "upload"
        suffix = Path(original_filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(415, "Supported video formats: MP4, MOV, MKV, AVI, WebM")
        if not self.has_queue_capacity():
            raise HTTPException(429, "Processing queue is full")
        self.require_disk_reserve("Insufficient free disk space for upload")
        upload_size = upload.size if isinstance(upload.size, int) and upload.size >= 0 else self.settings.max_upload_bytes
        self.require_upload_capacity(upload_size)

        job_id = str(uuid.uuid4())
        input_path = self.store.inputs / f"{job_id}{suffix}"
        try:
            await self._stream_upload(upload, input_path)
            media = normalize_media_info(await asyncio.to_thread(self.media_probe.inspect, input_path))
            self._validate_media(
                media.duration_seconds,
                media.width,
                media.height,
                media.frame_rate,
                media.frame_count,
                media.format_name,
            )
            target_width, target_height = target_dimensions(
                media.width, media.height, output_scale
            )
        except HTTPException:
            input_path.unlink(missing_ok=True)
            raise
        except ValueError as error:
            input_path.unlink(missing_ok=True)
            raise HTTPException(422, str(error)) from error
        except Exception as error:
            input_path.unlink(missing_ok=True)
            raise HTTPException(422, "Could not validate uploaded video") from error
        finally:
            await upload.close()

        runtime_profile_fingerprint = (
            f"seedvr2:{preset}:{self.settings.device_backend_class}:"
            f"scale={output_scale:g}:batch=5:chunk=25:overlap=4:"
            "dit_cache=disabled:vae_cache=disabled"
        )
        job = self.store.create(
            job_id=job_id,
            original_filename=Path(original_filename).name,
            input_path=input_path,
            output_path=self.store.results / f"{job_id}.mp4",
            log_path=self.store.logs / f"{job_id}.log",
            preset=preset,
            color_correction=color_correction,
            media=media,
            output_scale=output_scale,
            target_width=target_width,
            target_height=target_height,
            runtime_profile_fingerprint=runtime_profile_fingerprint,
        )
        self._ensure_worker()
        return job

    def has_disk_reserve(self) -> bool:
        try:
            return self._free_space_bytes(self.settings.data_root) >= self.settings.disk_reserve_bytes
        except OSError:
            return False

    def require_disk_reserve(self, detail: str) -> None:
        if not self.has_disk_reserve():
            raise HTTPException(507, detail)

    def require_upload_capacity(self, upload_size: int) -> None:
        try:
            free = self._free_space_bytes(self.settings.data_root)
        except OSError:
            free = 0
        if free - upload_size < self.settings.disk_reserve_bytes:
            raise HTTPException(507, "Insufficient free disk space to store upload")

    def get_job(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    def list_jobs(self) -> list[Job]:
        return self.store.list()

    def public_job(self, job: Job, *, now: datetime | None = None) -> dict[str, object]:
        current = now or datetime.now(UTC)
        payload = job.public_dict()
        started = datetime.fromisoformat(job.started_at) if job.started_at else None
        finished = datetime.fromisoformat(job.finished_at) if job.finished_at else current
        payload["elapsed_seconds"] = (
            max(0, int((finished - started).total_seconds())) if started else None
        )
        heartbeat = (
            datetime.fromisoformat(job.last_heartbeat_at)
            if job.last_heartbeat_at
            else None
        )
        progress = (
            datetime.fromisoformat(job.last_progress_at)
            if job.last_progress_at
            else None
        )
        active = job.status in {"running", "preflight"}
        heartbeat_basis = heartbeat or started
        progress_basis = progress or started
        payload["heartbeat_stale"] = bool(
            active
            and heartbeat_basis
            and (current - heartbeat_basis).total_seconds()
            > self.settings.heartbeat_stale_seconds
        )
        payload["progress_stale"] = bool(
            active
            and progress_basis
            and (current - progress_basis).total_seconds()
            > self.settings.progress_stale_seconds
        )
        if payload["heartbeat_stale"] or payload["progress_stale"]:
            payload["eta_low_seconds"] = None
            payload["eta_high_seconds"] = None
            payload["eta_confidence"] = "none"
        return payload

    def has_queue_capacity(self) -> bool:
        return self.store.pending_count() < self.settings.max_pending_jobs

    def read_log_tail(self, job_id: str, offset: int) -> dict[str, object]:
        """Return a bounded, job-owned log slice without exposing paths."""
        job = self.get_job(job_id)
        if not job.log_path.is_file():
            return {"text": "", "next_offset": 0, "size": 0, "truncated": False}

        size = job.log_path.stat().st_size
        start = min(max(offset, 0), size)
        truncated = False
        if size - start > MAX_LOG_READ_BYTES:
            start = size - MAX_LOG_READ_BYTES
            truncated = True
        with job.log_path.open("rb") as stream:
            stream.seek(start)
            text = stream.read(MAX_LOG_READ_BYTES).decode("utf-8", errors="replace")
        return {"text": text, "next_offset": size, "size": size, "truncated": truncated}

    def cancel(self, job_id: str) -> Job:
        job = self.store.cancel(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    def delete(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job.status not in TERMINAL_STATUSES:
            raise HTTPException(409, "Cancel or finish this job before deleting it")
        job.input_path.unlink(missing_ok=True)
        job.output_path.unlink(missing_ok=True)
        job.log_path.unlink(missing_ok=True)
        self._remove_partial_artifacts(job)
        self.store.delete(job_id)

    async def _stream_upload(self, upload: UploadFile, destination: Path) -> None:
        total = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as stream:
            while chunk := await upload.read(CHUNK_SIZE):
                total += len(chunk)
                if total > self.settings.max_upload_bytes:
                    raise HTTPException(413, "Upload exceeds configured size limit")
                stream.write(chunk)

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_active:
                return
            self._worker_active = True
            threading.Thread(target=self._run_queued_jobs, daemon=True, name="video-upscale-worker").start()

    def _run_queued_jobs(self) -> None:
        while True:
            job = self.store.claim_next_queued()
            if job:
                self._run_job(job)
                continue
            with self._worker_lock:
                if self.store.has_queued():
                    continue
                self._worker_active = False
                return

    def _run_job(self, job: Job) -> None:
        report_progress = lambda report: self._record_progress(job, report)
        is_cancelled = lambda: self.store.cancellation_requested(job.id)
        try:
            if not self.has_disk_reserve():
                raise RuntimeError("Insufficient free disk space before processing")
            if job.requires_preflight:
                self.store.mark_preflight(job.id)
                self.runner.preflight(job, PreflightLimits(), report_progress, is_cancelled)
            if is_cancelled():
                raise JobCancelled()
            if job.requires_preflight:
                self.store.mark_running(job.id)
            self.runner.run(job, report_progress, is_cancelled)
            if is_cancelled():
                raise JobCancelled()
            if not job.output_path.is_file():
                raise RuntimeError("Runner did not produce an MP4 output")
            output_media = normalize_media_info(self.media_probe.inspect(job.output_path))
            if (
                abs(output_media.width - job.target_width) > 2
                or abs(output_media.height - job.target_height) > 2
            ):
                raise RuntimeError("Final MP4 dimensions do not match validated target")
            current = self.store.get(job.id)
            publish_performance = False
            if (
                current is not None
                and current.runtime_profile_fingerprint != "legacy:unknown"
                and current.progress_source in {"measured", "historical"}
            ):
                freshness = self.public_job(current)
                publish_performance = not bool(
                    freshness["heartbeat_stale"] or freshness["progress_stale"]
                )
            self.store.complete(job.id, publish_performance=publish_performance)
        except JobCancelled:
            self._remove_partial_artifacts(job)
            self.store.mark_cancelled(job.id)
        except Exception as error:
            if is_cancelled():
                self._remove_partial_artifacts(job)
                self.store.mark_cancelled(job.id)
            else:
                self._remove_partial_artifacts(job)
                self.store.fail(job.id, str(error))

    def _record_progress(self, job: Job, report: ProgressReport) -> bool:
        accepted = self.store.record_report(job.id, report)
        if not accepted:
            return False
        event = report.event
        if (
            report.invocation == "full"
            and event is not None
            and event.event_type == "chunk_completed"
        ):
            self.store.update_eta(
                job.id,
                EtaEstimate(None, None, "none", "none"),
            )
            return True
        if (
            report.invocation != "full"
            or event is None
            or event.event_type != "phase_progress"
            or event.phase is None
            or event.current_unit is None
            or event.total_units is None
            or event.chunk_unique_frames is None
            or event.elapsed_seconds is None
        ):
            return True
        active = ActiveWork(
            phase=event.phase,
            current_unit=event.current_unit,
            total_units=event.total_units,
            runtime_profile_fingerprint=job.runtime_profile_fingerprint,
            workload_bucket=workload_bucket(
                job.target_width * job.target_height * event.chunk_unique_frames
            ),
            phase_elapsed_seconds=event.elapsed_seconds,
        )
        current_job = self.store.get(job.id)
        remaining_deadline = self.settings.max_process_seconds
        if current_job is not None and current_job.started_at is not None:
            started_at = datetime.fromisoformat(current_job.started_at)
            elapsed = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
            remaining_deadline = max(0, int(remaining_deadline - elapsed))
        estimate = estimate_eta(
            active,
            self.store.eta_samples(job.id),
            remaining_deadline,
        )
        self.store.update_eta(job.id, estimate)
        return True

    def _validate_media(
        self,
        duration_seconds: float,
        width: int,
        height: int,
        frame_rate: float,
        frame_count: int,
        format_name: str,
    ) -> None:
        if duration_seconds > self.settings.max_duration_seconds:
            raise HTTPException(
                422,
                f"Video exceeds maximum duration of {self.settings.max_duration_seconds} seconds",
            )
        if max(width, height) > self.settings.max_source_dimension:
            raise HTTPException(422, "Video dimensions exceed configured safety limit")
        if width * height > self.settings.max_source_pixels:
            raise HTTPException(422, "Video pixel count exceeds configured safety limit")
        if frame_rate > self.settings.max_source_frame_rate:
            raise HTTPException(422, "Video frame rate exceeds configured safety limit")
        if frame_count > self.settings.max_source_frames:
            raise HTTPException(422, "Video frame count exceeds configured safety limit")
        if format_name not in {"avi", "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2"}:
            raise HTTPException(422, "Uploaded media is not a supported self-contained format")

    def _remove_partial_artifacts(self, job: Job) -> None:
        for directory in (self.store.staging, self.store.results):
            for path in directory.glob(f"{job.id}*"):
                if path.is_file():
                    path.unlink(missing_ok=True)
