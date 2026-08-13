from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


COLOR_CORRECTIONS = frozenset({"lab", "none"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
OUTPUT_SCALES = frozenset({0.25, 0.5, 1.0, 2.0})
DEFAULT_OUTPUT_SCALE = 1.0
MIN_TARGET_SHORT_SIDE = 256
MAX_TARGET_EDGE = 7680
MAX_TARGET_PIXELS = 33_177_600


def _even_dimension(value: float) -> int:
    return max(2, int(math.floor(value / 2.0 + 0.5)) * 2)


def target_dimensions(width: int, height: int, scale: float) -> tuple[int, int]:
    if scale not in OUTPUT_SCALES or width <= 0 or height <= 0:
        raise ValueError("Unsupported output scale")
    target = (_even_dimension(width * scale), _even_dimension(height * scale))
    if min(target) < MIN_TARGET_SHORT_SIDE:
        raise ValueError(f"Target shortest edge must be at least {MIN_TARGET_SHORT_SIDE} pixels")
    if max(target) > MAX_TARGET_EDGE:
        raise ValueError(f"Target longest edge must not exceed {MAX_TARGET_EDGE} pixels")
    if target[0] * target[1] > MAX_TARGET_PIXELS:
        raise ValueError(f"Target pixel count must not exceed {MAX_TARGET_PIXELS} pixels")
    return target


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
    output_scale: float
    target_width: int
    target_height: int
    frame_count: int
    runtime_profile_fingerprint: str
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
    started_at: str | None = None
    finished_at: str | None = None
    last_heartbeat_at: str | None = None
    last_progress_at: str | None = None
    progress_source: str = "none"
    phase_name: str | None = None
    phase_current: int | None = None
    phase_total: int | None = None
    chunk_current: int | None = None
    chunk_total: int | None = None
    eta_low_seconds: int | None = None
    eta_high_seconds: int | None = None
    eta_confidence: str = "none"
    last_event_invocation: str | None = None
    last_event_sequence: int = -1
    last_work_sequence: int = -1

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "original_filename": self.original_filename,
            "preset": self.preset,
            "color_correction": self.color_correction,
            "output_scale": self.output_scale,
            "target_width": self.target_width,
            "target_height": self.target_height,
            "frame_count": self.frame_count,
            "runtime_profile_fingerprint": self.runtime_profile_fingerprint,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_progress_at": self.last_progress_at,
            "progress_source": self.progress_source,
            "phase_name": self.phase_name,
            "phase_current": self.phase_current,
            "phase_total": self.phase_total,
            "chunk_current": self.chunk_current,
            "chunk_total": self.chunk_total,
            "eta_low_seconds": self.eta_low_seconds,
            "eta_high_seconds": self.eta_high_seconds,
            "eta_confidence": self.eta_confidence,
            "output_filename": self.output_filename,
            "error": self.error,
            "requires_preflight": self.requires_preflight,
        }


@dataclass(frozen=True)
class PhaseSample:
    sample_group: str
    phase: str
    elapsed_seconds: float
    completed_units: int
    runtime_profile_fingerprint: str
    workload_bucket: int
    valid: bool
