import secrets
from dataclasses import replace
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .config import ASCII_WHITESPACE, Settings
from .job_service import JobService
from .job_store import JobStore
from .media import MediaProbe, SubprocessMediaProbe
from .runner import RunnerConfigurationError, SubprocessRunner, UnavailableRunner, VideoRunner
from .upload_guard import UploadBodyGuard
from .upload_sessions import UploadSessionError, UploadSessionService
from .system_metrics import SystemMetrics


def create_app(
    *,
    data_root: Path | None = None,
    runner: VideoRunner | None = None,
    media_probe: MediaProbe | None = None,
    max_upload_bytes: int | None = None,
    free_space_bytes: Callable[[Path], int] | None = None,
    frontend_dist: Path | None = None,
    max_pending_jobs: int | None = None,
    metrics_provider: Callable[[], dict[str, float | int | str | None]] | None = None,
) -> FastAPI:
    settings = Settings.from_environment()
    if data_root:
        settings = settings.with_data_root(data_root, max_upload_bytes)
    elif max_upload_bytes:
        settings = settings.with_data_root(settings.data_root, max_upload_bytes)
    if max_pending_jobs is not None:
        settings = replace(settings, max_pending_jobs=max_pending_jobs)
    store = JobStore(settings.data_root)
    store.initialize()
    probe = media_probe or SubprocessMediaProbe(
        settings.ffprobe,
        timeout_seconds=settings.ffprobe_timeout_seconds,
    )
    if runner is None:
        try:
            runner = SubprocessRunner(settings)
        except RunnerConfigurationError as error:
            runner = UnavailableRunner(str(error))
    jobs = JobService(settings, store, probe, runner, free_space_bytes=free_space_bytes)
    uploads = UploadSessionService(settings)
    metrics_provider = metrics_provider or SystemMetrics(settings.device_backend_class).snapshot
    service = FastAPI(title="Video Upscale WebUI API")
    service.state.job_service = jobs
    service.state.upload_session_service = uploads
    if settings.allowed_web_origin:
        service.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.allowed_web_origin],
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Content-Type", "Upload-Offset", "X-Video-Upscale-Request"],
        )
    service.add_middleware(
        UploadBodyGuard,
        max_body_bytes=settings.max_upload_bytes,
        max_chunk_bytes=4 * 1024 * 1024,
        max_metadata_bytes=64 * 1024,
        has_disk_reserve=jobs.has_disk_reserve,
        has_queue_capacity=jobs.has_queue_capacity,
        upload_idle_timeout_seconds=settings.upload_idle_timeout_seconds,
        upload_total_timeout_seconds=settings.upload_total_timeout_seconds,
    )

    @service.middleware("http")
    async def reject_oversized_or_disk_constrained_upload(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/api/jobs":
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    upload_size = int(content_length)
                except ValueError:
                    return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
                if upload_size < 0:
                    return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
                if upload_size > settings.max_upload_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Upload exceeds configured size limit"},
                    )
            if not jobs.has_disk_reserve():
                return JSONResponse(
                    status_code=507,
                    content={"detail": "Insufficient free disk space for upload"},
                )
        return await call_next(request)

    @service.middleware("http")
    async def require_operator_authentication(request: Request, call_next):
        if request.url.path == "/api/health":
            return await call_next(request)
        if request.method == "OPTIONS" and request.headers.get("origin"):
            return await call_next(request)
        actual_login = request.headers.get("tailscale-user-login", "").strip(
            ASCII_WHITESPACE
        )
        if not secrets.compare_digest(
            actual_login.casefold().encode("utf-8"),
            settings.tailscale_user_login.casefold().encode("utf-8"),
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Tailscale identity is not authorized"},
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get(
            "x-video-upscale-request"
        ) != "1":
            return JSONResponse(status_code=403, content={"detail": "Request header required"})
        return await call_next(request)

    @service.middleware("http")
    async def add_browser_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @service.get("/api/health")
    def health():
        capabilities = {
            "backend_id": settings.backend_id,
            "display_name": settings.backend_display_name,
            "platform": settings.platform_name,
            "accelerator": settings.accelerator_name,
            "presets": list(settings.presets),
            "metrics": metrics_provider(),
        }
        if getattr(runner, "health_status", "ready") != "ready":
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "runner": "unavailable",
                    "state": "offline",
                    **capabilities,
                },
            )
        return {"status": "ok", "runner": "ready", "state": "ready", **capabilities}

    @service.get("/api/config")
    def config() -> dict[str, object]:
        return {
            "default_profile": settings.default_profile,
            "presets": list(settings.presets),
            "default_output_scale": settings.default_output_scale,
            "output_scales": [
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
                    "description": (
                        "Quarter width and height; experimental generative restoration."
                    ),
                },
                {
                    "value": 2.0,
                    "label": "2x Upscale",
                    "description": "Double width and height; highest processing cost.",
                },
            ],
        }

    @service.get("/api/backends")
    def backends() -> dict[str, list[dict[str, str | int]]]:
        return {
            "backends": [descriptor.public_dict() for descriptor in settings.backends]
        }

    @service.get("/api/jobs")
    def list_jobs() -> dict[str, list[dict[str, object]]]:
        return {"jobs": [jobs.public_job(job) for job in jobs.list_jobs()]}

    @service.post("/api/jobs", status_code=201)
    async def create_job(
        video: UploadFile = File(...),
        preset: str | None = Form(None),
        color_correction: str = Form("lab"),
        output_scale: float | None = Form(None),
    ) -> dict[str, object]:
        return jobs.public_job(
            await jobs.create_job(video, preset, color_correction, output_scale)
        )

    def upload_error(error: UploadSessionError) -> HTTPException:
        return HTTPException(error.status_code, error.detail)

    @service.post("/api/uploads", status_code=201)
    def create_upload(payload: dict[str, object]) -> dict[str, object]:
        filename = payload.get("filename")
        total_bytes = payload.get("total_bytes")
        options = payload.get("options")
        if type(total_bytes) is int:
            if not jobs.has_queue_capacity():
                raise HTTPException(429, "Processing queue is full")
            jobs.require_disk_reserve("Insufficient free disk space for upload")
            jobs.require_upload_capacity(total_bytes)
        try:
            return uploads.create(
                filename=filename,  # type: ignore[arg-type]
                total_bytes=total_bytes,  # type: ignore[arg-type]
                options=options,  # type: ignore[arg-type]
            )
        except UploadSessionError as error:
            raise upload_error(error) from error

    @service.get("/api/uploads")
    def list_uploads() -> dict[str, list[dict[str, object]]]:
        return {"uploads": uploads.list_pending()}

    @service.get("/api/uploads/{upload_id}")
    def get_upload(upload_id: str) -> dict[str, object]:
        try:
            return uploads.status(upload_id)
        except UploadSessionError as error:
            raise upload_error(error) from error

    @service.put("/api/uploads/{upload_id}")
    async def append_upload(
        upload_id: str,
        request: Request,
        upload_offset: str | None = Header(None, alias="Upload-Offset"),
    ) -> dict[str, object]:
        try:
            offset = int(upload_offset) if upload_offset is not None else -1
        except ValueError:
            offset = -1
        try:
            return uploads.append(upload_id, offset=offset, data=await request.body())
        except UploadSessionError as error:
            raise upload_error(error) from error

    @service.post("/api/uploads/{upload_id}/finalize", status_code=201)
    async def finalize_upload(upload_id: str) -> dict[str, object]:
        existing = jobs.find_job(upload_id)
        if existing is not None:
            try:
                uploads.complete_finalization(upload_id)
            except UploadSessionError:
                pass
            return jobs.public_job(existing)
        try:
            finalized = uploads.claim_finalization(upload_id)
            options = finalized.options
            job = await jobs.create_job_from_staged_file(
                finalized.path,
                job_id=upload_id,
                original_filename=finalized.filename,
                total_bytes=finalized.total_bytes,
                preset=options.get("preset"),  # type: ignore[arg-type]
                color_correction=options.get("color_correction", "lab"),  # type: ignore[arg-type]
                output_scale=options.get("output_scale"),  # type: ignore[arg-type]
            )
        except UploadSessionError as error:
            raise upload_error(error) from error
        except HTTPException as error:
            if error.status_code in {415, 422}:
                uploads.complete_finalization(upload_id)
            else:
                uploads.release_finalization(upload_id)
            raise
        except Exception:
            uploads.release_finalization(upload_id)
            raise HTTPException(503, "Could not validate staged upload") from None
        try:
            uploads.complete_finalization(upload_id)
        except UploadSessionError:
            # Job has already been accepted; session cleanup will retry on restart.
            pass
        return jobs.public_job(job)

    @service.delete("/api/uploads/{upload_id}", status_code=204)
    def discard_upload(upload_id: str) -> Response:
        try:
            uploads.discard(upload_id)
        except UploadSessionError as error:
            raise upload_error(error) from error
        return Response(status_code=204)

    @service.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        return jobs.public_job(jobs.get_job(job_id))

    @service.get("/api/jobs/{job_id}/log")
    def get_job_log(job_id: str, offset: int = Query(0, ge=0)) -> JSONResponse:
        return JSONResponse(
            jobs.read_log_tail(job_id, offset),
            headers={"Cache-Control": "no-store"},
        )

    @service.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, object]:
        return jobs.public_job(jobs.cancel(job_id))

    @service.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str) -> Response:
        jobs.delete(job_id)
        return Response(status_code=204)

    @service.get("/api/jobs/{job_id}/download")
    def download_job(job_id: str) -> FileResponse:
        job = jobs.get_job(job_id)
        if job.status != "completed" or not job.output_path.is_file():
            from fastapi import HTTPException

            raise HTTPException(409, "Completed MP4 is not available")
        return FileResponse(
            job.output_path,
            media_type="video/mp4",
            filename=job.output_filename or f"{job.id}.mp4",
        )

    built_frontend = frontend_dist or settings.project_root / "frontend" / "dist"
    if built_frontend.is_dir():
        service.mount("/", StaticFiles(directory=built_frontend, html=True), name="frontend")

    return service


app = create_app()
