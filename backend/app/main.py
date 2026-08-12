import secrets
from dataclasses import replace
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ASCII_WHITESPACE, Settings
from .job_service import JobService
from .job_store import JobStore
from .media import MediaProbe, SubprocessMediaProbe
from .runner import RunnerConfigurationError, SubprocessRunner, UnavailableRunner, VideoRunner
from .upload_guard import UploadBodyGuard


def create_app(
    *,
    data_root: Path | None = None,
    runner: VideoRunner | None = None,
    media_probe: MediaProbe | None = None,
    max_upload_bytes: int | None = None,
    free_space_bytes: Callable[[Path], int] | None = None,
    frontend_dist: Path | None = None,
    max_pending_jobs: int | None = None,
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
    service = FastAPI(title="Video Upscale WebUI API")
    service.state.job_service = jobs
    service.add_middleware(
        UploadBodyGuard,
        max_body_bytes=settings.max_upload_bytes,
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
        if getattr(runner, "health_status", "ready") != "ready":
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "runner": "unavailable",
                },
            )
        return {"status": "ok", "runner": "ready"}

    @service.get("/api/config")
    def config() -> dict[str, object]:
        return {
            "default_profile": settings.default_profile,
            "presets": ["3b-safe", "7b-fp8-experimental"],
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
