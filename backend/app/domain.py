from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PRESETS = frozenset({"3b-safe", "7b-fp8-experimental"})
COLOR_CORRECTIONS = frozenset({"lab", "none"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float
    width: int
    height: int
    frame_rate: float = 30.0
    frame_count: int = 0
    format_name: str = "mov,mp4,m4a,3gp,3g2,mj2"


@dataclass(frozen=True)
class PreflightLimits:
    max_duration_seconds: int = 10
    max_height: int = 480
    max_frames: int = 1_200


@dataclass(frozen=True)
class Job:
    id: str
    original_filename: str
    input_path: Path
    output_path: Path
    log_path: Path
    preset: str
    color_correction: str
    status: str
    progress: int
    stage: str
    created_at: str
    updated_at: str
    output_filename: str | None
    error: str | None
    requires_preflight: bool
    cancel_requested: bool
    duration_seconds: float
    width: int
    height: int

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "original_filename": self.original_filename,
            "preset": self.preset,
            "color_correction": self.color_correction,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "output_filename": self.output_filename,
            "error": self.error,
            "requires_preflight": self.requires_preflight,
        }
