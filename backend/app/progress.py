from __future__ import annotations

import json
import math
from dataclasses import dataclass


MAX_PROGRESS_JSON_CHARS = 16_384
MAX_EVENT_STRING_CHARS = 64
MAX_COUNTER = 1_000_000_000
EVENT_TYPES = frozenset(
    {
        "model_preparation_started",
        "model_preparation_completed",
        "chunk_started",
        "phase_progress",
        "chunk_completed",
        "heartbeat",
        "output_started",
        "completed",
    }
)
PHASES = ("encoding", "upscaling", "decoding", "postprocessing")
PHASE_WEIGHTS = {
    "encoding": 0.20,
    "upscaling": 0.25,
    "decoding": 0.50,
    "postprocessing": 0.05,
}
PHASE_OFFSETS = {
    "encoding": 0.0,
    "upscaling": 0.20,
    "decoding": 0.45,
    "postprocessing": 0.95,
}
GENERATION_CAP = 91


@dataclass(frozen=True)
class ProgressEvent:
    sequence: int
    work_sequence: int
    measured_work: bool
    event_type: str
    phase: str | None = None
    current_unit: int | None = None
    total_units: int | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
    completed_unique_frames: int | None = None
    chunk_unique_frames: int | None = None
    chunk_context_frames: int | None = None
    total_unique_frames: int | None = None
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class ProgressReport:
    percent: int
    stage: str
    invocation: str = ""
    work_sequence: int = -1
    measured_work: bool = False
    event: ProgressEvent | None = None


_COUNTER_FIELDS = (
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
_SENSITIVE_STRING_MARKERS = ("/", "\\", "..", ":")
_EVENT_STAGES = {
    "model_preparation_started": "model-preparation",
    "model_preparation_completed": "model-preparation",
    "chunk_started": "chunk-start",
    "chunk_completed": "chunk-complete",
    "heartbeat": "heartbeat",
    "output_started": "seedvr2-output",
    "completed": "seedvr2-complete",
}
_PHASE_STAGES = {
    "encoding": "encoding",
    "upscaling": "ai-upscaling",
    "decoding": "decoding",
    "postprocessing": "post-processing",
}


def _bounded_counter(value: object, *, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ValueError
    return value


def _safe_string(value: object, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_EVENT_STRING_CHARS
        or any(marker in value for marker in _SENSITIVE_STRING_MARKERS)
    ):
        raise ValueError
    return value


def parse_progress_line(line: str) -> ProgressEvent | None:
    if not line.startswith("EVENT "):
        return None
    encoded = line[6:]
    if len(encoded) > MAX_PROGRESS_JSON_CHARS:
        return None
    try:
        payload = json.loads(encoded)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None

    try:
        if type(payload.get("schema_version")) is not int:
            raise ValueError
        counters = {
            name: _bounded_counter(
                payload.get(name), required=name in {"sequence", "work_sequence"}
            )
            for name in _COUNTER_FIELDS
        }
        measured_work = payload.get("measured_work")
        if type(measured_work) is not bool:
            raise ValueError
        event_type = _safe_string(payload.get("event_type"), required=True)
        phase = _safe_string(payload.get("phase"))
        if event_type not in EVENT_TYPES or phase is not None and phase not in PHASES:
            raise ValueError

        elapsed_value = payload.get("elapsed_seconds")
        elapsed_seconds: float | None = None
        if elapsed_value is not None:
            if type(elapsed_value) not in {int, float}:
                raise ValueError
            if type(elapsed_value) is int and not 0 <= elapsed_value <= MAX_COUNTER:
                raise ValueError
            elapsed_seconds = float(elapsed_value)
            if not math.isfinite(elapsed_seconds) or not 0 <= elapsed_seconds <= MAX_COUNTER:
                raise ValueError

        sequence = counters["sequence"]
        work_sequence = counters["work_sequence"]
        assert sequence is not None and work_sequence is not None
        if work_sequence > sequence:
            raise ValueError
        current_unit = counters["current_unit"]
        total_units = counters["total_units"]
        if current_unit is not None and total_units is not None and current_unit > total_units:
            raise ValueError
        chunk_index = counters["chunk_index"]
        chunk_count = counters["chunk_count"]
        if chunk_index is not None and chunk_index < 1:
            raise ValueError
        if chunk_index is not None and chunk_count is not None and chunk_index > chunk_count:
            raise ValueError

        completed_unique_frames = counters["completed_unique_frames"]
        chunk_unique_frames = counters["chunk_unique_frames"]
        chunk_context_frames = counters["chunk_context_frames"]
        total_unique_frames = counters["total_unique_frames"]
        if event_type == "phase_progress":
            required_values = (
                phase,
                current_unit,
                total_units,
                chunk_index,
                chunk_count,
                completed_unique_frames,
                chunk_unique_frames,
                chunk_context_frames,
                total_unique_frames,
            )
            if any(value is None for value in required_values):
                raise ValueError
            assert total_units is not None
            assert chunk_unique_frames is not None
            assert total_unique_frames is not None
            assert completed_unique_frames is not None
            if (
                not measured_work
                or total_units <= 0
                or chunk_unique_frames <= 0
                or total_unique_frames <= 0
                or completed_unique_frames + chunk_unique_frames > total_unique_frames
            ):
                raise ValueError
        elif event_type == "chunk_completed":
            if any(
                value is None
                for value in (
                    chunk_index,
                    chunk_count,
                    completed_unique_frames,
                    chunk_unique_frames,
                    total_unique_frames,
                )
            ):
                raise ValueError
            assert chunk_unique_frames is not None
            assert completed_unique_frames is not None
            assert total_unique_frames is not None
            if (
                not measured_work
                or chunk_unique_frames != 0
                or total_unique_frames <= 0
                or completed_unique_frames > total_unique_frames
            ):
                raise ValueError
        elif event_type == "heartbeat" and measured_work:
            raise ValueError

        return ProgressEvent(
            sequence=sequence,
            work_sequence=work_sequence,
            measured_work=measured_work,
            event_type=event_type,
            phase=phase,
            current_unit=current_unit,
            total_units=total_units,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            completed_unique_frames=completed_unique_frames,
            chunk_unique_frames=chunk_unique_frames,
            chunk_context_frames=chunk_context_frames,
            total_unique_frames=total_unique_frames,
            elapsed_seconds=elapsed_seconds,
        )
    except (AssertionError, OverflowError, ValueError):
        return None


def aggregate_progress(event: ProgressEvent) -> ProgressReport:
    if event.event_type == "phase_progress":
        assert event.phase is not None
        assert event.current_unit is not None
        assert event.total_units is not None
        assert event.completed_unique_frames is not None
        assert event.chunk_unique_frames is not None
        assert event.total_unique_frames is not None
        phase_fraction = PHASE_OFFSETS[event.phase] + (
            PHASE_WEIGHTS[event.phase] * event.current_unit / event.total_units
        )
        completed_frames = event.completed_unique_frames + event.chunk_unique_frames * phase_fraction
        percent = round(GENERATION_CAP * completed_frames / event.total_unique_frames)
        stage = _PHASE_STAGES[event.phase]
    elif event.event_type == "chunk_completed":
        assert event.completed_unique_frames is not None
        assert event.total_unique_frames is not None
        percent = round(
            GENERATION_CAP * event.completed_unique_frames / event.total_unique_frames
        )
        stage = _EVENT_STAGES[event.event_type]
    elif event.event_type == "output_started":
        if (
            event.completed_unique_frames is not None
            and event.chunk_unique_frames is not None
            and event.total_unique_frames is not None
            and event.total_unique_frames > 0
        ):
            ready_frames = (
                event.completed_unique_frames + event.chunk_unique_frames
            )
            percent = round(
                GENERATION_CAP * ready_frames / event.total_unique_frames
            )
        else:
            percent = 0
        stage = _EVENT_STAGES[event.event_type]
    elif event.event_type == "completed":
        percent = GENERATION_CAP
        stage = _EVENT_STAGES[event.event_type]
    else:
        percent = 0
        stage = _EVENT_STAGES[event.event_type]

    return ProgressReport(
        percent=min(GENERATION_CAP, percent),
        stage=stage,
        work_sequence=event.work_sequence,
        measured_work=event.measured_work,
        event=event,
    )
