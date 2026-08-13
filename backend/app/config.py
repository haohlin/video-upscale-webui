from __future__ import annotations

import os
import re
import sys
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024
DEFAULT_3B_MODEL = "seedvr2_ema_3b_fp8_e4m3fn.safetensors"
DEFAULT_7B_FP8_MODEL = "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors"
DEFAULT_VAE_MODEL = "ema_vae_fp16.safetensors"
ASCII_WHITESPACE = " \t\r\n\v\f"
BACKEND_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PLATFORM_PRESETS = {
    "apple-mps": ("3b-safe", "7b-fp8-experimental"),
    "nvidia-cuda": ("7b-fp8-quality", "3b-fp8-fast"),
}
MAX_BACKENDS = 4
MAX_BACKEND_LABEL_CHARS = 64


@dataclass(frozen=True)
class BackendDescriptor:
    id: str
    display_name: str
    api_base_url: str
    preference: int

    def public_dict(self) -> dict[str, str | int]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "api_base_url": self.api_base_url,
            "preference": self.preference,
        }


def _parse_backend_registry(
    raw: str | None, current: BackendDescriptor
) -> tuple[BackendDescriptor, ...]:
    try:
        payload = json.loads(raw) if raw else []
        if type(payload) is not list or len(payload) > MAX_BACKENDS - 1:
            raise ValueError
        descriptors = [current]
        ids = {current.id}
        for item in payload:
            if type(item) is not dict or set(item) != {
                "id", "display_name", "api_base_url", "preference"
            }:
                raise ValueError
            backend_id = item["id"]
            display_name = item["display_name"]
            api_base_url = item["api_base_url"]
            preference = item["preference"]
            if (
                type(backend_id) is not str
                or not BACKEND_ID_PATTERN.fullmatch(backend_id)
                or backend_id in ids
                or type(display_name) is not str
                or not 1 <= len(display_name) <= MAX_BACKEND_LABEL_CHARS
                or type(api_base_url) is not str
                or type(preference) is not int
                or not 0 <= preference <= 1000
            ):
                raise ValueError
            parsed = urlsplit(api_base_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError
            ids.add(backend_id)
            descriptors.append(
                BackendDescriptor(
                    id=backend_id,
                    display_name=display_name,
                    api_base_url=api_base_url.rstrip("/"),
                    preference=preference,
                )
            )
        return tuple(sorted(descriptors, key=lambda descriptor: descriptor.preference))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Video Upscale backend registry is invalid") from error


@dataclass(frozen=True)
class Settings:
    project_root: Path
    runtime_root: Path
    data_root: Path
    seedvr2_cli: str | None
    seedvr2_model_dir: Path | None
    python: str
    app_port: int
    disk_reserve_gb: int
    default_profile: str
    ffmpeg: str
    ffprobe: str
    default_output_scale: float = 1.0
    device_backend_class: str = "apple-mps"
    heartbeat_stale_seconds: int = 120
    progress_stale_seconds: int = 300
    max_upload_bytes: int = DEFAULT_UPLOAD_LIMIT
    seedvr2_3b_model: str = DEFAULT_3B_MODEL
    seedvr2_7b_fp8_model: str = DEFAULT_7B_FP8_MODEL
    seedvr2_vae_model: str = DEFAULT_VAE_MODEL
    tailscale_user_login: str = ""
    ffprobe_timeout_seconds: int = 30
    upload_idle_timeout_seconds: int = 30
    upload_total_timeout_seconds: int = 6 * 60 * 60
    upload_session_ttl_seconds: int = 24 * 60 * 60
    max_duration_seconds: int = 3600
    max_source_dimension: int = 3840
    max_source_pixels: int = 3840 * 2160
    max_source_frame_rate: int = 120
    max_source_frames: int = 216_000
    max_pending_jobs: int = 3
    max_process_seconds: int = 24 * 60 * 60
    max_job_log_bytes: int = 10 * 1024 * 1024
    max_job_artifact_bytes: int = 64 * 1024 * 1024 * 1024
    backend_id: str = "mac"
    backend_display_name: str = "Mac M4 Pro"
    platform_name: str = "macos"
    accelerator_name: str = "Apple MPS"
    allowed_web_origin: str | None = None
    backends: tuple[BackendDescriptor, ...] = ()

    @property
    def disk_reserve_bytes(self) -> int:
        return self.disk_reserve_gb * 1024 * 1024 * 1024

    @property
    def presets(self) -> tuple[str, ...]:
        try:
            return PLATFORM_PRESETS[self.device_backend_class]
        except KeyError as error:
            raise RuntimeError("Unsupported Video Upscale device backend class") from error

    @classmethod
    def from_environment(cls) -> "Settings":
        project_root = Path(
            os.environ.get(
                "VIDEO_UPSCALE_PROJECT_ROOT",
                Path(__file__).resolve().parents[2],
            )
        ).expanduser().resolve()
        runtime_root = Path(
            os.environ.get("VIDEO_UPSCALE_RUNTIME_ROOT", project_root / ".runtime")
        ).expanduser()
        data_root = Path(
            os.environ.get("VIDEO_UPSCALE_DATA_ROOT", runtime_root / "data")
        ).expanduser()
        tailscale_user_login = os.environ.get(
            "VIDEO_UPSCALE_TAILSCALE_USER_LOGIN", ""
        ).strip(ASCII_WHITESPACE)
        if not tailscale_user_login:
            raise RuntimeError("Video Upscale Tailscale user login is not configured")
        device_backend_class = os.environ.get(
            "VIDEO_UPSCALE_DEVICE_BACKEND_CLASS", "apple-mps"
        )
        backend_id = os.environ.get("VIDEO_UPSCALE_BACKEND_ID", "mac")
        if not BACKEND_ID_PATTERN.fullmatch(backend_id):
            raise RuntimeError("Video Upscale backend ID is invalid")
        allowed_web_origin = os.environ.get("VIDEO_UPSCALE_ALLOWED_WEB_ORIGIN") or None
        if allowed_web_origin:
            parsed_origin = urlsplit(allowed_web_origin)
            if (
                parsed_origin.scheme != "https"
                or not parsed_origin.hostname
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise RuntimeError("Video Upscale allowed Web origin is invalid")
            allowed_web_origin = allowed_web_origin.rstrip("/")
        model_dir_value = os.environ.get("VIDEO_UPSCALE_SEEDVR2_MODEL_DIR")
        backend_display_name = os.environ.get(
            "VIDEO_UPSCALE_BACKEND_DISPLAY_NAME", "Mac M4 Pro"
        )
        settings = cls(
            project_root=project_root,
            runtime_root=runtime_root,
            data_root=data_root,
            seedvr2_cli=os.environ.get("VIDEO_UPSCALE_SEEDVR2_CLI"),
            seedvr2_model_dir=Path(model_dir_value).expanduser()
            if model_dir_value
            else None,
            python=os.environ.get("VIDEO_UPSCALE_PYTHON", sys.executable),
            app_port=int(os.environ.get("VIDEO_UPSCALE_APP_PORT", "8765")),
            disk_reserve_gb=int(os.environ.get("VIDEO_UPSCALE_DISK_RESERVE_GB", "20")),
            default_profile=os.environ.get("VIDEO_UPSCALE_DEFAULT_PROFILE", "3b-safe"),
            ffmpeg=os.environ.get("VIDEO_UPSCALE_FFMPEG", "ffmpeg"),
            ffprobe=os.environ.get("VIDEO_UPSCALE_FFPROBE", "ffprobe"),
            default_output_scale=float(
                os.environ.get("VIDEO_UPSCALE_DEFAULT_OUTPUT_SCALE", "1.0")
            ),
            device_backend_class=device_backend_class,
            heartbeat_stale_seconds=int(
                os.environ.get("VIDEO_UPSCALE_HEARTBEAT_STALE_SECONDS", "120")
            ),
            progress_stale_seconds=int(
                os.environ.get("VIDEO_UPSCALE_PROGRESS_STALE_SECONDS", "300")
            ),
            seedvr2_3b_model=os.environ.get("VIDEO_UPSCALE_SEEDVR2_3B_MODEL", DEFAULT_3B_MODEL),
            seedvr2_7b_fp8_model=os.environ.get(
                "VIDEO_UPSCALE_SEEDVR2_7B_FP8_MODEL", DEFAULT_7B_FP8_MODEL
            ),
            tailscale_user_login=tailscale_user_login,
            ffprobe_timeout_seconds=int(
                os.environ.get("VIDEO_UPSCALE_FFPROBE_TIMEOUT_SECONDS", "30")
            ),
            upload_idle_timeout_seconds=int(
                os.environ.get("VIDEO_UPSCALE_UPLOAD_IDLE_TIMEOUT_SECONDS", "30")
            ),
            upload_total_timeout_seconds=int(
                os.environ.get("VIDEO_UPSCALE_UPLOAD_TOTAL_TIMEOUT_SECONDS", str(6 * 60 * 60))
            ),
            upload_session_ttl_seconds=int(
                os.environ.get("VIDEO_UPSCALE_UPLOAD_SESSION_TTL_SECONDS", str(24 * 60 * 60))
            ),
            max_duration_seconds=int(
                os.environ.get("VIDEO_UPSCALE_MAX_DURATION_SECONDS", "3600")
            ),
            max_source_dimension=int(
                os.environ.get("VIDEO_UPSCALE_MAX_SOURCE_DIMENSION", "3840")
            ),
            max_source_pixels=int(
                os.environ.get("VIDEO_UPSCALE_MAX_SOURCE_PIXELS", str(3840 * 2160))
            ),
            max_source_frame_rate=int(
                os.environ.get("VIDEO_UPSCALE_MAX_SOURCE_FRAME_RATE", "120")
            ),
            max_source_frames=int(
                os.environ.get("VIDEO_UPSCALE_MAX_SOURCE_FRAMES", "216000")
            ),
            max_pending_jobs=int(os.environ.get("VIDEO_UPSCALE_MAX_PENDING_JOBS", "3")),
            max_process_seconds=int(
                os.environ.get("VIDEO_UPSCALE_MAX_PROCESS_SECONDS", str(24 * 60 * 60))
            ),
            max_job_log_bytes=int(
                os.environ.get("VIDEO_UPSCALE_MAX_JOB_LOG_BYTES", str(10 * 1024 * 1024))
            ),
            max_job_artifact_bytes=int(
                os.environ.get("VIDEO_UPSCALE_MAX_JOB_ARTIFACT_BYTES", str(64 * 1024 * 1024 * 1024))
            ),
            backend_id=backend_id,
            backend_display_name=backend_display_name,
            platform_name=os.environ.get("VIDEO_UPSCALE_PLATFORM_NAME", "macos"),
            accelerator_name=os.environ.get(
                "VIDEO_UPSCALE_ACCELERATOR_NAME", "Apple MPS"
            ),
            allowed_web_origin=allowed_web_origin,
            backends=_parse_backend_registry(
                os.environ.get("VIDEO_UPSCALE_BACKENDS_JSON"),
                BackendDescriptor(
                    id=backend_id,
                    display_name=backend_display_name,
                    api_base_url="",
                    preference=100,
                ),
            ),
        )
        if settings.default_profile not in settings.presets:
            raise RuntimeError("Default processing profile is unavailable on this backend")
        return settings

    def with_data_root(self, data_root: Path, max_upload_bytes: int | None) -> "Settings":
        return Settings(
            project_root=self.project_root,
            runtime_root=self.runtime_root,
            data_root=data_root,
            seedvr2_cli=self.seedvr2_cli,
            seedvr2_model_dir=self.seedvr2_model_dir,
            python=self.python,
            app_port=self.app_port,
            disk_reserve_gb=self.disk_reserve_gb,
            default_profile=self.default_profile,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            default_output_scale=self.default_output_scale,
            device_backend_class=self.device_backend_class,
            heartbeat_stale_seconds=self.heartbeat_stale_seconds,
            progress_stale_seconds=self.progress_stale_seconds,
            max_upload_bytes=max_upload_bytes or self.max_upload_bytes,
            seedvr2_3b_model=self.seedvr2_3b_model,
            seedvr2_7b_fp8_model=self.seedvr2_7b_fp8_model,
            seedvr2_vae_model=self.seedvr2_vae_model,
            tailscale_user_login=self.tailscale_user_login,
            ffprobe_timeout_seconds=self.ffprobe_timeout_seconds,
            upload_idle_timeout_seconds=self.upload_idle_timeout_seconds,
            upload_total_timeout_seconds=self.upload_total_timeout_seconds,
            upload_session_ttl_seconds=self.upload_session_ttl_seconds,
            max_duration_seconds=self.max_duration_seconds,
            max_source_dimension=self.max_source_dimension,
            max_source_pixels=self.max_source_pixels,
            max_source_frame_rate=self.max_source_frame_rate,
            max_source_frames=self.max_source_frames,
            max_pending_jobs=self.max_pending_jobs,
            max_process_seconds=self.max_process_seconds,
            max_job_log_bytes=self.max_job_log_bytes,
            max_job_artifact_bytes=self.max_job_artifact_bytes,
            backend_id=self.backend_id,
            backend_display_name=self.backend_display_name,
            platform_name=self.platform_name,
            accelerator_name=self.accelerator_name,
            allowed_web_origin=self.allowed_web_origin,
            backends=self.backends,
        )
