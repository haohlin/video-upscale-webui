import math

from app.eta import ActiveWork, EtaEstimate, PhaseSample, estimate_eta, workload_bucket


PIXEL_FRAMES = 8_294_400
FINGERPRINT = (
    "seedvr2:3b-safe:apple-mps:scale=1:batch=5:chunk=25:overlap=4:"
    "dit_cache=disabled:vae_cache=disabled"
)
PHASE_SECONDS = {
    "encoding": 100,
    "upscaling": 300,
    "decoding": 500,
    "postprocessing": 100,
}


def sample(
    sample_group,
    phase,
    seconds,
    units=10,
    fingerprint=FINGERPRINT,
    pixels=PIXEL_FRAMES,
    valid=True,
):
    return PhaseSample(
        sample_group,
        phase,
        seconds,
        units,
        fingerprint,
        workload_bucket(pixels),
        valid,
    )


def active(
    phase="encoding",
    current=5,
    total=10,
    elapsed=50,
    completed_frames=0,
    chunk_frames=0,
    total_frames=0,
):
    return ActiveWork(
        phase,
        current,
        total,
        FINGERPRINT,
        workload_bucket(PIXEL_FRAMES),
        elapsed,
        completed_frames,
        chunk_frames,
        total_frames,
    )


def test_workload_bucket_is_nonnegative_base_two_order_of_magnitude():
    assert workload_bucket(-10) == 0
    assert workload_bucket(1) == 0
    assert workload_bucket(8) == 3


def test_no_measured_unit_and_no_history_is_calibrating():
    result = estimate_eta(active(current=0, elapsed=0), [], 86_400)

    assert result == EtaEstimate(None, None, "none", "none")


def test_one_comparable_run_returns_wide_low_confidence_range():
    samples = [
        sample("old", phase, seconds) for phase, seconds in PHASE_SECONDS.items()
    ]

    result = estimate_eta(active(current=2, elapsed=20), samples, 86_400)

    assert result.confidence == "low"
    assert result.source == "historical"
    assert result.low_seconds < result.high_seconds
    assert result.low_seconds <= 588
    assert result.high_seconds >= 1_568


def test_remaining_video_chunks_are_included_in_eta():
    samples = [
        sample("current-run", phase, seconds)
        for phase, seconds in PHASE_SECONDS.items()
    ]
    one_chunk = estimate_eta(active(), samples, 86_400)
    four_chunks = estimate_eta(
        active(completed_frames=25, chunk_frames=25, total_frames=100),
        samples,
        86_400,
    )

    assert one_chunk.low_seconds is not None
    assert one_chunk.high_seconds is not None
    assert four_chunks.low_seconds is not None
    assert four_chunks.high_seconds is not None
    assert four_chunks.low_seconds > one_chunk.low_seconds * 3
    assert four_chunks.high_seconds > one_chunk.high_seconds * 3


def test_three_matching_runs_plus_stable_current_rate_is_medium_confidence():
    samples = [
        sample(f"job-{index}", phase, seconds + index)
        for index in range(3)
        for phase, seconds in PHASE_SECONDS.items()
    ]

    result = estimate_eta(active(), samples, 86_400)

    assert result.confidence == "medium"
    assert result.source == "historical"


def test_high_confidence_requires_total_width_at_most_twenty_percent_of_midpoint():
    def estimate_with_later_phase_quartiles(
        lower_quartile: float, upper_quartile: float
    ) -> EtaEstimate:
        factors = [0.7, lower_quartile, 1.0, upper_quartile, 1.3]
        samples = [
            sample(
                f"job-{index}",
                phase,
                seconds if phase == "encoding" else seconds * factor,
            )
            for index, factor in enumerate(factors)
            for phase, seconds in PHASE_SECONDS.items()
        ]
        return estimate_eta(active(), samples, 86_400)

    boundary = estimate_with_later_phase_quartiles(0.9, 1.1)
    above_boundary = estimate_with_later_phase_quartiles(0.89, 1.11)

    assert boundary.confidence == "high"
    boundary_midpoint = (boundary.low_seconds + boundary.high_seconds) / 2
    assert boundary.high_seconds - boundary.low_seconds <= boundary_midpoint * 0.20
    assert above_boundary.confidence == "medium"
    above_midpoint = (
        above_boundary.low_seconds + above_boundary.high_seconds
    ) / 2
    assert (
        above_boundary.high_seconds - above_boundary.low_seconds
        > above_midpoint * 0.20
    )


def test_deadline_clamp_never_promotes_wide_evidence_to_high_confidence():
    factors = [0.7, 0.89, 1.0, 1.11, 1.3]
    samples = [
        sample(
            f"job-{index}",
            phase,
            seconds if phase == "encoding" else seconds * factor,
        )
        for index, factor in enumerate(factors)
        for phase, seconds in PHASE_SECONDS.items()
    ]

    unclamped = estimate_eta(active(), samples, 86_400)
    clamped = estimate_eta(active(), samples, 500)

    assert unclamped.confidence == "medium"
    assert 500 < unclamped.low_seconds < unclamped.high_seconds
    assert clamped == EtaEstimate(500, 500, "medium", "historical")


def test_deadline_clamp_preserves_legitimate_high_confidence():
    samples = [
        sample(f"job-{index}", phase, seconds)
        for index in range(5)
        for phase, seconds in PHASE_SECONDS.items()
    ]

    unclamped = estimate_eta(active(), samples, 86_400)
    clamped = estimate_eta(active(), samples, 500)

    assert unclamped.confidence == "high"
    assert clamped == EtaEstimate(500, 500, "high", "historical")


def test_missing_rate_for_any_remaining_phase_is_calibrating():
    samples = [sample(f"job-{index}", "encoding", 100) for index in range(3)]

    assert estimate_eta(active(), samples, 86_400) == EtaEstimate(
        None, None, "none", "none"
    )


def test_mismatched_failed_nonfinite_and_extreme_samples_do_not_change_range():
    baseline = [
        sample(f"ok-{index}", phase, seconds + index)
        for index in range(3)
        for phase, seconds in {
            "decoding": 100,
            "postprocessing": 20,
        }.items()
    ]
    noisy = baseline + [
        sample("wrong", "decoding", 1, fingerprint=FINGERPRINT + ":other"),
        sample("failed", "decoding", 1, valid=False),
        sample("far-bucket", "decoding", 1, pixels=PIXEL_FRAMES * 8),
        sample("outlier-one", "decoding", 100_000),
        sample("outlier-two", "decoding", 200_000),
        sample("unknown", "other-phase", 1),
        sample("nan", "decoding", math.nan),
        sample("zero-seconds", "decoding", 0),
        sample("zero-units", "decoding", 1, units=0),
    ]
    current = active(phase="decoding", current=2, elapsed=20)

    baseline_result = estimate_eta(current, baseline, 86_400)
    assert baseline_result.low_seconds is not None
    assert estimate_eta(current, noisy, 86_400) == baseline_result


def test_eta_range_is_finite_ordered_and_clamped_to_deadline():
    huge_samples = [
        sample(f"job-{index}", phase, 1_000_000 + index)
        for index in range(3)
        for phase in PHASE_SECONDS
    ]

    result = estimate_eta(active(), huge_samples, 300)

    assert result.low_seconds is not None
    assert result.high_seconds is not None
    assert 0 <= result.low_seconds <= result.high_seconds <= 300


def test_completed_current_run_samples_can_calibrate_all_remaining_phases():
    samples = [
        sample("current-run", phase, seconds)
        for phase, seconds in PHASE_SECONDS.items()
    ]

    result = estimate_eta(active(), samples, 86_400)

    assert result.low_seconds is not None
    assert result.high_seconds is not None
    assert result.confidence == "low"
    assert result.source == "measured"
