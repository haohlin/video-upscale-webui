from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .domain import PhaseSample
from .progress import PHASES


@dataclass(frozen=True)
class ActiveWork:
    phase: str
    current_unit: int
    total_units: int
    runtime_profile_fingerprint: str
    workload_bucket: int
    phase_elapsed_seconds: float


@dataclass(frozen=True)
class EtaEstimate:
    low_seconds: int | None
    high_seconds: int | None
    confidence: str
    source: str


def workload_bucket(pixel_frames: int) -> int:
    return max(0, int(math.log2(max(1, pixel_frames))))


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _without_extreme_rates(rates: list[float]) -> list[float]:
    if len(rates) < 3:
        return rates
    median_rate = statistics.median(rates)
    return [rate for rate in rates if median_rate / 5 <= rate <= median_rate * 5]


def _without_extreme_grouped_rates(
    rates: list[tuple[float, str | None]],
) -> list[tuple[float, str | None]]:
    if len(rates) < 3:
        return rates
    median_rate = statistics.median(rate for rate, _group in rates)
    return [
        (rate, group)
        for rate, group in rates
        if median_rate / 5 <= rate <= median_rate * 5
    ]


def _valid_active_rate(active: ActiveWork) -> float | None:
    if (
        active.current_unit <= 0
        or active.current_unit > active.total_units
        or not math.isfinite(active.phase_elapsed_seconds)
        or active.phase_elapsed_seconds <= 0
    ):
        return None
    return active.phase_elapsed_seconds / active.current_unit


def _stable_current_rate(current_rate: float | None, phase_rates: list[float]) -> bool:
    if current_rate is None or not phase_rates:
        return False
    comparable = _without_extreme_rates([*phase_rates, current_rate])
    if current_rate not in comparable:
        return False
    mean_rate = statistics.fmean(comparable)
    if mean_rate <= 0:
        return False
    return statistics.pstdev(comparable) / mean_rate <= 0.10


def estimate_eta(
    active: ActiveWork,
    samples: list[PhaseSample],
    deadline_seconds: int,
) -> EtaEstimate:
    if (
        active.phase not in PHASES
        or active.current_unit < 0
        or active.total_units <= 0
        or active.current_unit > active.total_units
    ):
        return EtaEstimate(None, None, "none", "none")

    rates_by_phase: dict[str, list[tuple[float, str | None]]] = {
        phase: [] for phase in PHASES
    }
    for item in samples:
        if (
            not item.valid
            or item.phase not in rates_by_phase
            or item.runtime_profile_fingerprint
            != active.runtime_profile_fingerprint
            or abs(item.workload_bucket - active.workload_bucket) > 1
            or item.completed_units <= 0
            or not math.isfinite(item.elapsed_seconds)
            or item.elapsed_seconds <= 0
        ):
            continue
        rates_by_phase[item.phase].append(
            (item.elapsed_seconds / item.completed_units, item.sample_group)
        )

    current_rate = _valid_active_rate(active)
    historical_active_rates = [rate for rate, _group in rates_by_phase[active.phase]]
    current_rate_stable = _stable_current_rate(
        current_rate, historical_active_rates
    )
    if current_rate is not None:
        rates_by_phase[active.phase].append((current_rate, None))

    for phase in PHASES:
        rates_by_phase[phase] = _without_extreme_grouped_rates(
            rates_by_phase[phase]
        )

    active_index = PHASES.index(active.phase)
    remaining_units = {
        phase: (
            active.total_units - active.current_unit
            if phase == active.phase
            else active.total_units
        )
        for phase in PHASES[active_index:]
    }
    if any(not rates_by_phase[phase] for phase in remaining_units):
        return EtaEstimate(None, None, "none", "none")

    retained_groups = {
        group
        for phase in remaining_units
        for _rate, group in rates_by_phase[phase]
        if group is not None
    }
    historical_groups = retained_groups - {"current-run"}
    has_current_run_sample = "current-run" in retained_groups

    central_seconds = 0.0
    low_seconds = 0.0
    high_seconds = 0.0
    for phase, units in remaining_units.items():
        rates = [rate for rate, _group in rates_by_phase[phase]]
        central_seconds += statistics.median(rates) * units
        low_seconds += _nearest_rank(rates, 0.25) * units
        high_seconds += _nearest_rank(rates, 0.75) * units

    comparable_jobs = len(historical_groups)
    if comparable_jobs <= 2:
        low_seconds = min(low_seconds, central_seconds * 0.6)
        high_seconds = max(high_seconds, central_seconds * 1.6)
    elif comparable_jobs >= 5 and current_rate_stable:
        # Five stable runs may earn high confidence, so use a 20%-total-width
        # calibration floor. Wider observed quartiles remain wider and medium.
        low_seconds = min(low_seconds, central_seconds * 0.9)
        high_seconds = max(high_seconds, central_seconds * 1.1)
    else:
        low_seconds = min(low_seconds, central_seconds * 0.8)
        high_seconds = max(high_seconds, central_seconds * 1.2)

    evidence_low = max(0, math.floor(low_seconds))
    evidence_high = max(evidence_low, max(0, math.ceil(high_seconds)))
    midpoint = (evidence_low + evidence_high) / 2
    range_width = evidence_high - evidence_low
    if (
        comparable_jobs >= 5
        and current_rate_stable
        and midpoint > 0
        and range_width <= midpoint * 0.20
    ):
        confidence = "high"
    elif comparable_jobs >= 3 and current_rate_stable:
        confidence = "medium"
    else:
        confidence = "low"
    deadline = max(0, int(deadline_seconds))
    low = min(deadline, evidence_low)
    high = max(low, min(deadline, evidence_high))
    source = "historical" if historical_groups else "measured"
    if not historical_groups and not has_current_run_sample and current_rate is None:
        return EtaEstimate(None, None, "none", "none")
    return EtaEstimate(low, high, confidence, source)
