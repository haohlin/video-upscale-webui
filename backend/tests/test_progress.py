import json
import math

import pytest

from app.progress import MAX_PROGRESS_JSON_CHARS, aggregate_progress, parse_progress_line


def event_line(**overrides):
    payload = {
        "schema_version": 1,
        "sequence": 7,
        "work_sequence": 5,
        "measured_work": True,
        "event_type": "phase_progress",
        "phase": "decoding",
        "current_unit": 5,
        "total_units": 10,
        "chunk_index": 2,
        "chunk_count": 4,
        "completed_unique_frames": 21,
        "chunk_unique_frames": 21,
        "chunk_context_frames": 4,
        "total_unique_frames": 80,
        "elapsed_seconds": 12.5,
    }
    payload.update(overrides)
    return "EVENT " + json.dumps(payload)


def test_parse_progress_line_accepts_only_bounded_schema_v1_primitives():
    event = parse_progress_line(event_line(extra_ignored="ignored"))
    assert event is not None
    assert event.event_type == "phase_progress"
    assert event.phase == "decoding"
    assert event.current_unit == 5
    assert event.work_sequence == 5
    assert event.measured_work is True


@pytest.mark.parametrize(
    "line",
    [
        "not an event",
        "EVENT []",
        "EVENT {bad json}",
        "EVENT " + "x" * (MAX_PROGRESS_JSON_CHARS + 1),
        event_line(schema_version=2),
        event_line(current_unit=-1),
        event_line(total_units=1_000_000_001),
        event_line(elapsed_seconds=math.inf),
        event_line(phase="x" * 65),
        event_line(phase="/Users/private/input.mp4"),
    ],
)
def test_parse_progress_line_rejects_malformed_sensitive_or_unbounded_events(line):
    assert parse_progress_line(line) is None


@pytest.mark.parametrize(
    ("phase", "current", "expected"),
    [
        ("encoding", 10, 29),
        ("upscaling", 10, 35),
        ("decoding", 10, 47),
        ("postprocessing", 10, 48),
    ],
)
def test_phase_weights_aggregate_inside_chunk_and_generation_cap(phase, current, expected):
    report = aggregate_progress(parse_progress_line(event_line(phase=phase, current_unit=current)))
    assert report.percent == expected
    assert report.percent <= 91


def test_chunk_completion_uses_unique_frames_not_equal_chunk_weight():
    report = aggregate_progress(
        parse_progress_line(
            event_line(
                event_type="chunk_completed",
                phase=None,
                current_unit=None,
                total_units=None,
                completed_unique_frames=21,
                chunk_unique_frames=0,
                total_unique_frames=80,
            )
        )
    )
    assert report.percent == 24


def test_temporal_overlap_never_counts_twice():
    first = aggregate_progress(
        parse_progress_line(
            event_line(
                chunk_index=1,
                completed_unique_frames=0,
                chunk_unique_frames=21,
                total_unique_frames=80,
                phase="postprocessing",
                current_unit=10,
            )
        )
    )
    second = aggregate_progress(
        parse_progress_line(
            event_line(
                event_type="chunk_completed",
                phase=None,
                current_unit=None,
                total_units=None,
                chunk_index=1,
                completed_unique_frames=21,
                chunk_unique_frames=0,
                total_unique_frames=80,
            )
        )
    )
    assert first.percent == second.percent == 24
