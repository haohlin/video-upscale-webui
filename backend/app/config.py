from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024
DEFAULT_3B_MODEL = "seedvr2_ema_3b_fp8_e4m3fn.safetensors"
DEFAULT_7B_FP8_MODEL = "seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors"
DEFAULT_VAE_MODEL = "ema_vae_fp16.safetensors"


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
    access_username: str = "video"
    access_token: str = ""
    ffprobe_timeout_seconds: int = 30
    upload_idle_timeout_seconds: int = 30
    upload_total_timeout_seconds: int = 6 * 60 * 60
    max_duration_seconds: int = 3600
    max_source_dimension: int = 3840
    max_source_pixels: int = 3840 * 2160
    max_source_frame_rate: int = 120
    max_source_frames: int = 216_000
    max_pending_jobs: int = 3
    max_process_seconds: int = 24 * 60 * 60
    max_job_log_bytes: int = 10 * 1024 * 1024
    max_job_artifact_bytes: int = 64 * 1024 * 1024 * 1024

    @property
    def disk_reserve_bytes(self) -> int:
        return self.disk_reserve_gb * 1024 * 1024 * 1024

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
        access_token = os.environ.get("VIDEO_UPSCALE_ACCESS_TOKEN", "")
        token_file = Path(
            os.environ.get("VIDEO_UPSCALE_ACCESS_TOKEN_FILE", data_root / "access-token")
        ).expanduser()
        if not access_token and token_file.is_file():
            if token_file.stat().st_mode & 0o077:
                raise RuntimeError("Video Upscale access token file must use mode 600")
            access_token = token_file.read_text(encoding="utf-8").strip()
        if not access_token:
            raise RuntimeError("Video Upscale access token is not configured")
        model_dir_value = os.environ.get("VIDEO_UPSCALE_SEEDVR2_MODEL_DIR")
        return cls(
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
            device_backend_class=os.environ.get(
                "VIDEO_UPSCALE_DEVICE_BACKEND_CLASS", "apple-mps"
            ),
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
            access_username=os.environ.get("VIDEO_UPSCALE_ACCESS_USERNAME", "video"),
            access_token=access_token,
            ffprobe_timeout_seconds=int(
                os.environ.get("VIDEO_UPSCALE_FFPROBE_TIMEOUT_SECONDS", "30")
            ),
            upload_idle_timeout_seconds=int(
                os.environ.get("VIDEO_UPSCALE_UPLOAD_IDLE_TIMEOUT_SECONDS", "30")
            ),
            upload_total_timeout_seconds=int(
                os.environ.get("VIDEO_UPSCALE_UPLOAD_TOTAL_TIMEOUT_SECONDS", str(6 * 60 * 60))
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
        )

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
            access_username=self.access_username,
            access_token=self.access_token,
            ffprobe_timeout_seconds=self.ffprobe_timeout_seconds,
            upload_idle_timeout_seconds=self.upload_idle_timeout_seconds,
            upload_total_timeout_seconds=self.upload_total_timeout_seconds,
            max_duration_seconds=self.max_duration_seconds,
            max_source_dimension=self.max_source_dimension,
            max_source_pixels=self.max_source_pixels,
            max_source_frame_rate=self.max_source_frame_rate,
            max_source_frames=self.max_source_frames,
            max_pending_jobs=self.max_pending_jobs,
            max_process_seconds=self.max_process_seconds,
            max_job_log_bytes=self.max_job_log_bytes,
            max_job_artifact_bytes=self.max_job_artifact_bytes,
        )
