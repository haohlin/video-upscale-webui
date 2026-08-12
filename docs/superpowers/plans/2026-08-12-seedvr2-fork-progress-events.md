# SeedVR2 Fork CLI Progress Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a maintainable SeedVR2 fork branch that adds opt-in, immediately flushed, privacy-bounded JSONL progress and heartbeat events to standalone single-GPU CLI processing without changing default CLI behavior.

**Architecture:** Keep fork `main` identical to upstream and implement changes only on `feature/cli-progress-events`. Add a dependency-free `src/cli_progress.py` reporter responsible for schema, sequencing, bounded serialization, heartbeat threading, and phase-name normalization; thread one reporter through existing standalone CLI functions so current generation callbacks emit measured phase and chunk counters. Human output remains unchanged unless `--progress_format jsonl` is selected, and WebUI runtime remains untouched until its separate integration plan is complete.

**Tech Stack:** Python 3.10+, standard-library `argparse`, `json`, `threading`, `time`, `unittest`; existing PyTorch/OpenCV SeedVR2 CLI

## Global Constraints

- Work in `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler`, not the live runtime checkout under `~/Library/Application Support/VideoUpscaleWebUI/runtime`.
- Create public fork `haohlin/ComfyUI-SeedVR2_VideoUpscaler`; configure `origin` to that fork and `upstream` to `https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git`.
- Keep fork `main` identical to upstream `main`; all code commits belong on `feature/cli-progress-events`.
- Base branch on reviewed upstream commit `4490bd1f482e026674543386bb2a4d176da245b9`.
- Add only opt-in `--progress_format jsonl`; invocation without this option must produce no machine events and retain existing human-readable behavior.
- Support measured structured progress only for standalone single-GPU processing in this release; multi-GPU execution behavior and exit semantics remain unchanged.
- Emit schema version `1`; each machine event is one UTF-8 JSON object no larger than `4096` encoded bytes, followed by newline and immediate flush.
- Event names are exactly `model_preparation_started`, `model_preparation_completed`, `chunk_started`, `phase_progress`, `chunk_completed`, `heartbeat`, `output_started`, and `completed`.
- Machine events contain no paths, filenames, credentials, environment data, model-directory values, exception messages, or arbitrary user strings.
- Canonical phases are exactly `preparing`, `encoding`, `upscaling`, `decoding`, `postprocessing`, `output`, and `completed`.
- Every event carries `sequence`, scoped to one `ProgressReporter` and therefore one CLI invocation; each new CLI invocation starts at sequence `1`.
- Every event carries `measured_work` and `work_sequence`; only a new `phase_progress` counter or `chunk_completed` advances `work_sequence`, while heartbeat and lifecycle events set `measured_work=false` and repeat the last work sequence.
- Every chunk-scoped event carries bounded `chunk_unique_frames`, `chunk_context_frames`, `completed_unique_frames`, and `total_unique_frames`; overlap/context frames never contribute to completed or total unique-frame work.
- Overall frame-weighted progress is `(completed_unique_frames + chunk_unique_frames * phase_fraction) / total_unique_frames`; `phase_fraction` uses existing four-phase offsets and weights and heartbeat never changes either numerator or work sequence.
- Preserve existing generation phase callback meanings: encoding `20%`, DiT upscaling `25%`, decoding `50%`, and post-processing `5%`; fork reports counters while WebUI computes weighted display progress.
- Emit invocation-wide model preparation around model-weight availability with chunk fields zero, then emit a separate preparation start/completion pair for every chunk around `prepare_runner` and text-embedding loading; never claim model preparation happened only once when uncached chunks repeat it.
- For preparation events, `chunk_index=0`, `chunk_count=0` means invocation-wide weight availability; positive chunk fields mean per-chunk runner/embedding preparation.
- Heartbeat interval defaults to `10.0` seconds; silence never cancels or kills processing.
- Keep subprocess behavior shell-free; this fork feature creates no subprocess commands.
- Do not restart service, modify live runtime, or disturb any active video job while implementing this plan.
- Run `codex-security:security-scan` on fork diff before first public branch push.
- Do not open upstream pull request in this release.

---

## File Structure

- Create `src/cli_progress.py`: owns event schema, validation, bounded JSONL serialization, monotonic sequence numbers, heartbeat lifecycle, state snapshots, and upstream phase-name mapping.
- Create `tests/test_cli_progress.py`: fast standard-library unit tests for reporter output, bounds, privacy, phase mapping, monotonic counters, flush, and heartbeat behavior.
- Create `tests/test_inference_cli_progress.py`: focused CLI wiring tests using mocks so no model download, GPU inference, or media processing occurs.
- Modify `inference_cli.py`: adds CLI option, creates and closes reporter, marks model/output/completion stages, and passes reporter/chunk context through existing single-GPU call chain.
- Modify `README.md`: documents option, schema, single-GPU scope, privacy boundary, and one example invocation.

---

### Task 1: Create Fork Checkout and Core JSONL Reporter

**Files:**
- Create: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/src/cli_progress.py`
- Create: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/tests/test_cli_progress.py`

**Interfaces:**
- Consumes: Python standard library only.
- Produces: `ProgressReporter(progress_format: str = "none", stream: TextIO | None = None, clock: Callable[[], float] = time.monotonic, heartbeat_interval: float = 10.0)`; methods `start() -> None`, `close() -> None`, `emit(event_type: str, *, phase: str, current_unit: int = 0, total_units: int = 0, current_frames: int = 0, chunk_index: int = 0, chunk_count: int = 0, chunk_unique_frames: int = 0, chunk_context_frames: int = 0, completed_unique_frames: int = 0, total_unique_frames: int = 0) -> None`, `phase_callback(*, chunk_index: int, chunk_count: int, chunk_unique_frames: int, chunk_context_frames: int, completed_unique_frames: int, total_unique_frames: int) -> Callable[[int, int, int, str], None]`, and property `enabled: bool`.
- Produces: constants `SCHEMA_VERSION = 1`, `MAX_EVENT_BYTES = 4096`, `MAX_COUNTER = 2**63 - 1`, `DEFAULT_HEARTBEAT_INTERVAL = 10.0`.

- [ ] **Step 1: Create fork and isolated feature branch**

Run from `/Users/haohanl/dev`:

```bash
gh repo fork numz/ComfyUI-SeedVR2_VideoUpscaler --clone=false
git clone https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git /Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler
git -C /Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler remote add upstream https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git
git -C /Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler fetch upstream main
git -C /Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler switch main
git -C /Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler merge --ff-only 4490bd1f482e026674543386bb2a4d176da245b9
test "$(git -C /Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler rev-parse HEAD)" = "4490bd1f482e026674543386bb2a4d176da245b9"
git -C /Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler switch -c feature/cli-progress-events
```

Expected: final command creates `feature/cli-progress-events`; `origin` names fork and `upstream` names source repository.

- [ ] **Step 2: Write failing reporter tests**

Create `tests/test_cli_progress.py`:

```python
import io
import json
import time
import unittest

from src.cli_progress import MAX_COUNTER, MAX_EVENT_BYTES, ProgressReporter


class FlushCountingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def parsed_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class ProgressReporterTests(unittest.TestCase):
    def test_disabled_reporter_writes_nothing(self) -> None:
        stream = FlushCountingStream()
        reporter = ProgressReporter(progress_format="none", stream=stream)

        reporter.start()
        reporter.emit("model_preparation_started", phase="preparing")
        reporter.close()

        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(stream.flush_count, 0)

    def test_jsonl_event_has_stable_schema_sequence_and_flush(self) -> None:
        stream = FlushCountingStream()
        ticks = iter((100.0, 101.25))
        reporter = ProgressReporter(
            progress_format="jsonl",
            stream=stream,
            clock=lambda: next(ticks),
        )

        reporter.start()
        reporter.emit(
            "phase_progress",
            phase="encoding",
            current_unit=2,
            total_units=5,
            current_frames=9,
            chunk_index=1,
            chunk_count=3,
            chunk_unique_frames=40,
            chunk_context_frames=0,
            completed_unique_frames=0,
            total_unique_frames=100,
        )
        reporter.close()

        self.assertEqual(parsed_lines(stream), [{
            "schema_version": 1,
            "sequence": 1,
            "work_sequence": 1,
            "measured_work": True,
            "event_type": "phase_progress",
            "elapsed_seconds": 1.25,
            "phase": "encoding",
            "current_unit": 2,
            "total_units": 5,
            "current_frames": 9,
            "chunk_index": 1,
            "chunk_count": 3,
            "chunk_unique_frames": 40,
            "chunk_context_frames": 0,
            "completed_unique_frames": 0,
            "total_unique_frames": 100,
        }])
        self.assertEqual(stream.flush_count, 1)

    def test_phase_callback_normalizes_all_four_upstream_names(self) -> None:
        stream = FlushCountingStream()
        reporter = ProgressReporter(progress_format="jsonl", stream=stream)
        reporter.start()
        callback = reporter.phase_callback(
            chunk_index=1,
            chunk_count=4,
            chunk_unique_frames=25,
            chunk_context_frames=3,
            completed_unique_frames=0,
            total_unique_frames=100,
        )

        callback(1, 8, 5, "Phase 1: Encoding (batch 1)")
        callback(2, 8, 1, "Phase 2: Upscaling")
        callback(3, 8, 1, "Phase 3: Decoding")
        callback(4, 8, 1, "Phase 4: Post-processing")
        reporter.close()

        events = parsed_lines(stream)
        self.assertEqual(
            [event["phase"] for event in events],
            ["encoding", "upscaling", "decoding", "postprocessing"],
        )
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3, 4])
        self.assertEqual([event["work_sequence"] for event in events], [1, 2, 3, 4])
        self.assertTrue(all(event["chunk_index"] == 1 for event in events))
        self.assertTrue(all(event["chunk_count"] == 4 for event in events))

    def test_invalid_or_decreasing_phase_counters_are_rejected(self) -> None:
        stream = FlushCountingStream()
        reporter = ProgressReporter(progress_format="jsonl", stream=stream)
        reporter.start()
        callback = reporter.phase_callback(
            chunk_index=1,
            chunk_count=1,
            chunk_unique_frames=5,
            chunk_context_frames=0,
            completed_unique_frames=0,
            total_unique_frames=5,
        )

        callback(2, 4, 5, "Phase 1: Encoding")
        callback(2, 4, 5, "Phase 1: Encoding")

        with self.assertRaises(ValueError):
            callback(2, 5, 5, "Phase 1: Encoding")
        with self.assertRaises(ValueError):
            callback(1, 4, 5, "Phase 1: Encoding")
        with self.assertRaises(ValueError):
            callback(5, 4, 5, "Phase 1: Encoding")
        with self.assertRaises(ValueError):
            callback(1, 1, 1, "untrusted/path/to/input.mp4")

        reporter.close()
        self.assertEqual(len(parsed_lines(stream)), 1)

    def test_event_is_bounded_and_has_no_user_text_fields(self) -> None:
        stream = FlushCountingStream()
        reporter = ProgressReporter(progress_format="jsonl", stream=stream)
        reporter.start()
        reporter.emit(
            "chunk_started",
            phase="encoding",
            chunk_index=1,
            chunk_count=2,
            chunk_unique_frames=3,
            chunk_context_frames=0,
            completed_unique_frames=0,
            total_unique_frames=5,
        )
        reporter.emit(
            "chunk_completed",
            phase="postprocessing",
            chunk_index=1,
            chunk_count=2,
            chunk_unique_frames=3,
            chunk_context_frames=0,
            completed_unique_frames=3,
            total_unique_frames=5,
        )
        reporter.emit(
            "chunk_started",
            phase="encoding",
            chunk_index=1,
            chunk_count=2,
            chunk_unique_frames=3,
            chunk_context_frames=0,
            completed_unique_frames=0,
            total_unique_frames=5,
        )
        reporter.close()

        line = stream.getvalue().encode("utf-8")
        event = parsed_lines(stream)[0]
        self.assertLessEqual(len(line.rstrip(b"\n")), MAX_EVENT_BYTES)
        self.assertEqual(set(event), {
            "schema_version", "sequence", "work_sequence", "measured_work", "event_type",
            "elapsed_seconds", "phase", "current_unit", "total_units", "current_frames",
            "chunk_index", "chunk_count", "chunk_unique_frames", "chunk_context_frames",
            "completed_unique_frames", "total_unique_frames",
        })
        self.assertNotIn("path", json.dumps(event).lower())
        self.assertNotIn("filename", json.dumps(event).lower())

        with self.assertRaises(ValueError):
            reporter.emit(
                "phase_progress",
                phase="encoding",
                current_unit=MAX_COUNTER + 1,
                total_units=MAX_COUNTER + 1,
            )

    def test_heartbeat_repeats_last_bounded_state(self) -> None:
        stream = FlushCountingStream()
        reporter = ProgressReporter(
            progress_format="jsonl",
            stream=stream,
            heartbeat_interval=0.01,
        )
        reporter.start()
        reporter.emit(
            "phase_progress",
            phase="upscaling",
            current_unit=3,
            total_units=9,
            current_frames=1,
            chunk_index=1,
            chunk_count=2,
            chunk_unique_frames=5,
            chunk_context_frames=0,
            completed_unique_frames=0,
            total_unique_frames=10,
        )
        time.sleep(0.035)
        reporter.close()

        heartbeats = [event for event in parsed_lines(stream) if event["event_type"] == "heartbeat"]
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertTrue(all(event["phase"] == "upscaling" for event in heartbeats))
        self.assertTrue(all(event["current_unit"] == 3 for event in heartbeats))
        self.assertTrue(all(event["measured_work"] is False for event in heartbeats))
        self.assertTrue(all(event["work_sequence"] == 1 for event in heartbeats))
        sequences = [int(event["sequence"]) for event in parsed_lines(stream)]
        self.assertEqual(sequences, sorted(set(sequences)))

    def test_chunk_completion_excludes_overlap_and_advances_work_once(self) -> None:
        stream = FlushCountingStream()
        reporter = ProgressReporter(progress_format="jsonl", stream=stream)
        reporter.start()
        reporter.emit(
            "chunk_started",
            phase="encoding",
            chunk_index=2,
            chunk_count=2,
            chunk_unique_frames=2,
            chunk_context_frames=2,
            completed_unique_frames=3,
            total_unique_frames=5,
        )
        reporter.emit(
            "chunk_completed",
            phase="postprocessing",
            chunk_index=2,
            chunk_count=2,
            chunk_unique_frames=2,
            chunk_context_frames=2,
            completed_unique_frames=5,
            total_unique_frames=5,
        )
        reporter.close()

        _, _, started, completed = parsed_lines(stream)
        self.assertFalse(started["measured_work"])
        self.assertEqual(started["work_sequence"], 1)
        self.assertTrue(completed["measured_work"])
        self.assertEqual(completed["work_sequence"], 2)
        self.assertEqual(completed["completed_unique_frames"], 5)
        self.assertEqual(completed["chunk_context_frames"], 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests and verify missing module failure**

Run:

```bash
cd /Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler
python -m unittest tests.test_cli_progress -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'src.cli_progress'`.

- [ ] **Step 4: Implement minimal reporter**

Create `src/cli_progress.py`:

```python
from __future__ import annotations

import json
import sys
import threading
import time
from typing import Callable, TextIO


SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 4096
MAX_COUNTER = 2**63 - 1
DEFAULT_HEARTBEAT_INTERVAL = 10.0

EVENTS = frozenset({
    "model_preparation_started",
    "model_preparation_completed",
    "chunk_started",
    "phase_progress",
    "chunk_completed",
    "heartbeat",
    "output_started",
    "completed",
})
PHASES = frozenset({
    "preparing",
    "encoding",
    "upscaling",
    "decoding",
    "postprocessing",
    "output",
    "completed",
})
UPSTREAM_PHASES = {
    "Phase 1: Encoding": "encoding",
    "Phase 2: Upscaling": "upscaling",
    "Phase 3: Decoding": "decoding",
    "Phase 4: Post-processing": "postprocessing",
}


class ProgressReporter:
    def __init__(
        self,
        progress_format: str = "none",
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        if progress_format not in {"none", "jsonl"}:
            raise ValueError("progress_format must be 'none' or 'jsonl'")
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        self.enabled = progress_format == "jsonl"
        self._stream = stream if stream is not None else sys.stdout
        self._clock = clock
        self._heartbeat_interval = heartbeat_interval
        self._started_at = 0.0
        self._sequence = 0
        self._work_sequence = 0
        self._completed_unique_frames = 0
        self._total_unique_frames: int | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = {
            "phase": "preparing",
            "current_unit": 0,
            "total_units": 0,
            "current_frames": 0,
            "chunk_index": 0,
            "chunk_count": 0,
            "chunk_unique_frames": 0,
            "chunk_context_frames": 0,
            "completed_unique_frames": 0,
            "total_unique_frames": 0,
        }
        self._phase_counters: dict[tuple[int, str], tuple[int, int]] = {}

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._started_at = self._clock()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="seedvr2-progress-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._heartbeat_interval * 2))
            self._thread = None

    def emit(
        self,
        event_type: str,
        *,
        phase: str,
        current_unit: int = 0,
        total_units: int = 0,
        current_frames: int = 0,
        chunk_index: int = 0,
        chunk_count: int = 0,
        chunk_unique_frames: int = 0,
        chunk_context_frames: int = 0,
        completed_unique_frames: int = 0,
        total_unique_frames: int = 0,
    ) -> None:
        if not self.enabled:
            return
        if event_type not in EVENTS:
            raise ValueError("unknown progress event")
        if phase not in PHASES:
            raise ValueError("unknown progress phase")
        values = (
            current_unit,
            total_units,
            current_frames,
            chunk_index,
            chunk_count,
            chunk_unique_frames,
            chunk_context_frames,
            completed_unique_frames,
            total_unique_frames,
        )
        if any(
            not isinstance(value, int) or value < 0 or value > MAX_COUNTER
            for value in values
        ):
            raise ValueError("progress counters must be bounded non-negative integers")
        if total_units and current_unit > total_units:
            raise ValueError("current_unit cannot exceed total_units")
        if chunk_count and (chunk_index < 1 or chunk_index > chunk_count):
            raise ValueError("chunk_index must be within chunk_count")
        if completed_unique_frames > total_unique_frames:
            raise ValueError("completed unique frames cannot exceed total")
        if event_type in {"chunk_started", "phase_progress"} and (
            completed_unique_frames + chunk_unique_frames > total_unique_frames
        ):
            raise ValueError("chunk unique frames exceed remaining unique work")
        if event_type == "chunk_completed" and completed_unique_frames < chunk_unique_frames:
            raise ValueError("completed unique frames cannot omit current chunk")
        if total_unique_frames:
            if self._total_unique_frames is None:
                self._total_unique_frames = total_unique_frames
            elif total_unique_frames != self._total_unique_frames:
                raise ValueError("total unique frames cannot change")
        if event_type in {"chunk_started", "phase_progress"} and (
            completed_unique_frames != self._completed_unique_frames
        ):
            raise ValueError("completed unique frame counter mismatch")
        if event_type == "chunk_completed":
            expected_completed = self._completed_unique_frames + chunk_unique_frames
            if completed_unique_frames != expected_completed:
                raise ValueError("chunk completion must add unique frames exactly once")
            self._completed_unique_frames = completed_unique_frames

        state = {
            "phase": phase,
            "current_unit": current_unit,
            "total_units": total_units,
            "current_frames": current_frames,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "chunk_unique_frames": chunk_unique_frames,
            "chunk_context_frames": chunk_context_frames,
            "completed_unique_frames": completed_unique_frames,
            "total_unique_frames": total_unique_frames,
        }
        with self._lock:
            self._state = state
            measured_work = event_type in {"phase_progress", "chunk_completed"}
            if measured_work:
                self._work_sequence += 1
            self._write_locked(event_type, state, measured_work=measured_work)

    def phase_callback(
        self,
        *,
        chunk_index: int,
        chunk_count: int,
        chunk_unique_frames: int,
        chunk_context_frames: int,
        completed_unique_frames: int,
        total_unique_frames: int,
    ) -> Callable[[int, int, int, str], None]:
        def callback(
            current_step: int,
            total_steps: int,
            current_frames: int,
            phase_name: str,
        ) -> None:
            phase_key = phase_name.split(" (")[0]
            phase = UPSTREAM_PHASES.get(phase_key)
            if phase is None:
                raise ValueError("unknown upstream progress phase")
            key = (chunk_index, phase)
            previous = self._phase_counters.get(key)
            if current_step < 0 or total_steps <= 0 or current_step > total_steps:
                raise ValueError("invalid phase progress counters")
            if previous is not None:
                if total_steps != previous[1] or current_step < previous[0]:
                    raise ValueError("phase progress counter regression")
                if current_step == previous[0]:
                    return
            self._phase_counters[key] = (current_step, total_steps)
            self.emit(
                "phase_progress",
                phase=phase,
                current_unit=current_step,
                total_units=total_steps,
                current_frames=current_frames,
                chunk_index=chunk_index,
                chunk_count=chunk_count,
                chunk_unique_frames=chunk_unique_frames,
                chunk_context_frames=chunk_context_frames,
                completed_unique_frames=completed_unique_frames,
                total_unique_frames=total_unique_frames,
            )

        return callback

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_interval):
            with self._lock:
                self._write_locked("heartbeat", self._state, measured_work=False)

    def _write_locked(
        self,
        event_type: str,
        state: dict[str, int | str],
        *,
        measured_work: bool,
    ) -> None:
        self._sequence += 1
        payload = {
            "schema_version": SCHEMA_VERSION,
            "sequence": self._sequence,
            "work_sequence": self._work_sequence,
            "measured_work": measured_work,
            "event_type": event_type,
            "elapsed_seconds": round(max(0.0, self._clock() - self._started_at), 3),
            **state,
        }
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("progress event exceeds maximum size")
        self._stream.write(line + "\n")
        self._stream.flush()
```

- [ ] **Step 5: Run reporter tests**

Run:

```bash
python -m unittest tests.test_cli_progress -v
```

Expected: seven tests PASS.

- [ ] **Step 6: Commit core reporter**

```bash
git add src/cli_progress.py tests/test_cli_progress.py
git commit -m "feat: add bounded CLI progress reporter"
```

---

### Task 2: Add Opt-in CLI Lifecycle Events

**Files:**
- Modify: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/inference_cli.py:45-49,1339-1487,1494-1712`
- Create: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/tests/test_inference_cli_progress.py`

**Interfaces:**
- Consumes: `ProgressReporter` from Task 1.
- Produces: CLI option `--progress_format {none,jsonl}` with default `none`.
- Produces: `main(argv: list[str] | None = None) -> int`; module entry point raises `SystemExit(main())` and successful direct calls return `0`.
- Produces: invocation-wide `model_preparation_started`/`model_preparation_completed` around `download_weight`, `completed` only after successful media processing, and a heartbeat thread closed in `finally` on success or error.
- Produces: fresh event and work sequences for every call to `main`; reporter state is never module-global or reused between CLI invocations.

- [ ] **Step 1: Write failing CLI option and lifecycle tests**

Create `tests/test_inference_cli_progress.py`:

```python
import contextlib
import io
import json
import unittest
from unittest import mock

import inference_cli


def json_events(output: str) -> list[dict[str, object]]:
    events = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("schema_version") == 1:
            events.append(value)
    return events


class CliProgressLifecycleTests(unittest.TestCase):
    def base_args(self) -> list[str]:
        return ["input.mp4", "--output", "output.mp4"]

    def test_default_cli_emits_no_machine_events(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(inference_cli, "download_weight", return_value=True), \
             mock.patch.object(inference_cli, "get_input_type", return_value="video"), \
             mock.patch.object(inference_cli, "process_single_file", return_value=3), \
             contextlib.redirect_stdout(stdout):
            result = inference_cli.main(self.base_args())

        self.assertEqual(result, 0)
        self.assertEqual(json_events(stdout.getvalue()), [])

    def test_jsonl_cli_emits_preparation_then_completed(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(inference_cli, "download_weight", return_value=True), \
             mock.patch.object(inference_cli, "get_input_type", return_value="video"), \
             mock.patch.object(inference_cli, "process_single_file", return_value=3), \
             contextlib.redirect_stdout(stdout):
            result = inference_cli.main(self.base_args() + ["--progress_format", "jsonl"])

        self.assertEqual(result, 0)
        events = json_events(stdout.getvalue())
        self.assertEqual(events[0]["event_type"], "model_preparation_started")
        self.assertEqual(events[0]["phase"], "preparing")
        self.assertEqual(events[1]["event_type"], "model_preparation_completed")
        self.assertEqual(events[-1]["event_type"], "completed")
        self.assertEqual(events[-1]["phase"], "completed")
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))

    def test_event_sequence_restarts_for_each_cli_invocation(self) -> None:
        first_stdout = io.StringIO()
        second_stdout = io.StringIO()
        patches = (
            mock.patch.object(inference_cli, "download_weight", return_value=True),
            mock.patch.object(inference_cli, "get_input_type", return_value="video"),
            mock.patch.object(inference_cli, "process_single_file", return_value=1),
        )
        with patches[0], patches[1], patches[2]:
            with contextlib.redirect_stdout(first_stdout):
                self.assertEqual(inference_cli.main(self.base_args() + ["--progress_format", "jsonl"]), 0)
            with contextlib.redirect_stdout(second_stdout):
                self.assertEqual(inference_cli.main(self.base_args() + ["--progress_format", "jsonl"]), 0)

        self.assertEqual(json_events(first_stdout.getvalue())[0]["sequence"], 1)
        self.assertEqual(json_events(second_stdout.getvalue())[0]["sequence"], 1)
        self.assertEqual(json_events(first_stdout.getvalue())[0]["work_sequence"], 0)
        self.assertEqual(json_events(second_stdout.getvalue())[0]["work_sequence"], 0)

    def test_failed_download_keeps_nonzero_exit_and_no_completed_event(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(inference_cli, "download_weight", return_value=False), \
             contextlib.redirect_stdout(stdout):
            result = inference_cli.main(self.base_args() + ["--progress_format", "jsonl"])

        self.assertEqual(result, 1)
        self.assertNotIn("completed", [event["event_type"] for event in json_events(stdout.getvalue())])

    def test_invalid_progress_format_is_rejected_by_argparse(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                inference_cli.main(self.base_args() + ["--progress_format", "xml"])

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run focused tests and verify signature failure**

Run:

```bash
python -m unittest tests.test_inference_cli_progress.CliProgressLifecycleTests -v
```

Expected: FAIL because `main()` accepts no argument and parser lacks `--progress_format`.

- [ ] **Step 3: Add argument parsing interface**

In `inference_cli.py`, import reporter beside project imports:

```python
from src.cli_progress import ProgressReporter
```

Change parser function signature from its current no-argument form to:

```python
def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
```

Add under `Debugging` options, after `--debug`:

```python
    debug_group.add_argument(
        "--progress_format",
        type=str,
        default="none",
        choices=["none", "jsonl"],
        help="Emit opt-in machine-readable progress events (default: none)",
    )
```

Replace auto-help and parse return with:

```python
    effective_argv = sys.argv[1:] if argv is None else argv
    if not effective_argv:
        effective_argv = ["--help"]
    return parser.parse_args(effective_argv)
```

- [ ] **Step 4: Make main return exit codes and own reporter lifecycle**

Change main signature and initial setup:

```python
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_arguments(argv)
    debug.enabled = args.debug
    progress = ProgressReporter(progress_format=args.progress_format)
    progress.start()
```

Move existing `try:` so it begins immediately after `progress.start()`. Indent header printing, tile checks, ffmpeg checks, cache notices, device selection, model download, media processing, and success return inside this one `try`. This ensures every return after reporter creation reaches the existing `finally`; argument parsing remains before reporter creation, so argparse exits need no cleanup.

Immediately before `download_weight(...)`, emit:

```python
        progress.emit("model_preparation_started", phase="preparing")
```

Immediately after a successful `download_weight(...)` return and before input inspection, close invocation-wide preparation with zero chunk fields:

```python
        progress.emit("model_preparation_completed", phase="preparing")
```

Pass `progress=progress` to every `process_single_file(...)` call. Task 3 adds that parameter; until then, update mocked lifecycle tests to assert `progress` keyword is accepted by mocks.

Replace each validation-path `sys.exit(1)` inside `main` with `return 1`. Leave argparse's own `SystemExit(2)` unchanged. Remove old nested `try:` that previously began immediately before `start_time = time.time()` so main has only one reporter-owning `try`/`except`/`finally`. In exception handler, keep existing bounded human error logging and traceback, then `return 1` instead of `sys.exit(1)`.

After FPS logging on successful processing, emit and return:

```python
        progress.emit("completed", phase="completed")
        return 0
```

In `finally`, close reporter before existing footer:

```python
        progress.close()
        debug.log(f"Process {os.getpid()} terminating - VRAM will be automatically freed", category="cleanup", force=True)
        debug.print_footer()
```

Replace module entry point with:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run lifecycle tests**

Run:

```bash
python -m unittest tests.test_inference_cli_progress.CliProgressLifecycleTests -v
```

Expected: five tests PASS.

- [ ] **Step 6: Verify default help compatibility**

Run:

```bash
python inference_cli.py --help > /tmp/seedvr2-help.txt
test "$(grep -c -- '--progress_format {none,jsonl}' /tmp/seedvr2-help.txt)" = "1"
test "$(grep -c '^{"schema_version"' /tmp/seedvr2-help.txt)" = "0"
```

Expected: both assertions succeed; no machine event appears without opt-in.

- [ ] **Step 7: Commit CLI lifecycle**

```bash
git add inference_cli.py tests/test_inference_cli_progress.py
git commit -m "feat: add opt-in CLI progress lifecycle"
```

---

### Task 3: Wire Chunk and Four-Phase Measured Progress

**Files:**
- Modify: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/inference_cli.py:424-718,831-1000,1107-1125`
- Modify: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/tests/test_inference_cli_progress.py`

**Interfaces:**
- Consumes: keyword-only `ProgressReporter.phase_callback(...)` with complete unique/context frame accounting from Task 1.
- Produces: `process_single_file(..., runner_cache: Optional[Dict[str, Any]] = None, progress: Optional[ProgressReporter] = None) -> int`.
- Produces: `_stream_video_chunks(..., progress: Optional[ProgressReporter] = None) -> Generator[torch.Tensor, None, None]`.
- Produces: `_process_frames_core(..., runner_cache: Optional[Dict[str, Any]] = None, progress: Optional[ProgressReporter] = None, chunk_index: int = 1, chunk_count: int = 1, chunk_unique_frames: int = 0, chunk_context_frames: int = 0, completed_unique_frames: int = 0, total_unique_frames: int = 0) -> torch.Tensor`.
- Produces: `_single_gpu_direct_processing(..., runner_cache: Optional[Dict[str, Any]], progress: Optional[ProgressReporter] = None, chunk_index: int = 1, chunk_count: int = 1, chunk_unique_frames: int = 0, chunk_context_frames: int = 0, completed_unique_frames: int = 0, total_unique_frames: int = 0) -> torch.Tensor`.
- Produces: `chunk_completed` after successful consumption/output of each yielded chunk; cumulative completion counts only new source frames, never temporal-overlap context.

- [ ] **Step 1: Write failing single-GPU phase wiring test**

Append to `CliProgressLifecycleTests` in `tests/test_inference_cli_progress.py`:

```python
    def test_single_gpu_core_passes_same_phase_callback_to_four_phases(self) -> None:
        reporter = mock.Mock()
        phase_callback = mock.Mock()
        reporter.enabled = True
        reporter.phase_callback.return_value = phase_callback
        runner = mock.Mock()
        context = {
            "dit_device": "mps",
            "vae_device": "mps",
            "compute_dtype": mock.Mock(),
        }
        args = mock.Mock(
            cache_dit=False,
            cache_vae=False,
            dit_offload_device="none",
            vae_offload_device="none",
            tensor_offload_device="cpu",
            compile_dit=False,
            compile_vae=False,
            model_dir=None,
            dit_model="seedvr2_ema_3b_fp8_e4m3fn.safetensors",
            blocks_to_swap=0,
            swap_io_components=False,
            vae_encode_tiled=False,
            vae_encode_tile_size=1024,
            vae_encode_tile_overlap=128,
            vae_decode_tiled=False,
            vae_decode_tile_size=1024,
            vae_decode_tile_overlap=128,
            tile_debug="false",
            attention_mode="sdpa",
            resolution=1080,
            max_resolution=0,
            batch_size=5,
            uniform_batch_size=False,
            seed=42,
            prepend_frames=0,
            temporal_overlap=0,
            input_noise_scale=0.0,
            color_correction="lab",
            latent_noise_scale=0.0,
        )
        frames = mock.Mock()
        frames.is_cuda = False
        frames.is_mps = False
        frames.dtype = None
        phase_result = {"final_video": frames}

        with mock.patch.object(inference_cli, "get_gpu_backend", return_value="mps"), \
             mock.patch.object(inference_cli, "_device_id_to_name", return_value="mps"), \
             mock.patch.object(inference_cli, "_parse_offload_device", return_value=None), \
             mock.patch.object(inference_cli, "setup_generation_context", return_value=context), \
             mock.patch.object(inference_cli, "prepare_runner", return_value=(runner, {})), \
             mock.patch.object(inference_cli, "load_text_embeddings", return_value=mock.Mock()), \
             mock.patch.object(inference_cli, "compute_generation_info", return_value=(frames, {})), \
             mock.patch.object(inference_cli, "log_generation_start"), \
             mock.patch.object(inference_cli, "encode_all_batches", return_value=context) as encode, \
             mock.patch.object(inference_cli, "upscale_all_batches", return_value=context) as upscale, \
             mock.patch.object(inference_cli, "decode_all_batches", return_value=context) as decode, \
             mock.patch.object(inference_cli, "postprocess_all_batches", return_value=phase_result) as postprocess:
            inference_cli._process_frames_core(
                frames,
                args,
                "0",
                inference_cli.debug,
                progress=reporter,
                chunk_index=2,
                chunk_count=5,
                chunk_unique_frames=20,
                chunk_context_frames=3,
                completed_unique_frames=20,
                total_unique_frames=100,
            )
            inference_cli._process_frames_core(
                frames,
                args,
                "0",
                inference_cli.debug,
                progress=reporter,
                chunk_index=3,
                chunk_count=5,
                chunk_unique_frames=20,
                chunk_context_frames=3,
                completed_unique_frames=40,
                total_unique_frames=100,
            )

        reporter.phase_callback.assert_has_calls([
            mock.call(
                chunk_index=2,
                chunk_count=5,
                chunk_unique_frames=20,
                chunk_context_frames=3,
                completed_unique_frames=20,
                total_unique_frames=100,
            ),
            mock.call(
                chunk_index=3,
                chunk_count=5,
                chunk_unique_frames=20,
                chunk_context_frames=3,
                completed_unique_frames=40,
                total_unique_frames=100,
            ),
        ])
        preparation_calls = [
            call for call in reporter.emit.call_args_list
            if call.args and call.args[0] in {
                "model_preparation_started",
                "model_preparation_completed",
            }
        ]
        self.assertEqual(
            [call.args[0] for call in preparation_calls],
            [
                "model_preparation_started",
                "model_preparation_completed",
                "model_preparation_started",
                "model_preparation_completed",
            ],
        )
        self.assertEqual([call.kwargs["chunk_index"] for call in preparation_calls], [2, 2, 3, 3])
        self.assertTrue(all(call.kwargs["chunk_context_frames"] == 3 for call in preparation_calls))
        self.assertIs(encode.call_args.kwargs["progress_callback"], phase_callback)
        self.assertIs(upscale.call_args.kwargs["progress_callback"], phase_callback)
        self.assertIs(decode.call_args.kwargs["progress_callback"], phase_callback)
        self.assertIs(postprocess.call_args.kwargs["progress_callback"], phase_callback)
```

- [ ] **Step 2: Write failing chunk event test**

Append to same class:

```python
    def test_streaming_chunks_exclude_overlap_from_completed_work(self) -> None:
        reporter = mock.Mock()
        reporter.enabled = True
        cap = mock.Mock()
        first = inference_cli.torch.zeros((3, 2, 2, 3))
        second = inference_cli.torch.zeros((2, 2, 2, 3))
        results = [
            inference_cli.torch.zeros((3, 2, 2, 3)),
            inference_cli.torch.zeros((4, 2, 2, 3)),
        ]

        with mock.patch.object(inference_cli, "_read_frames_from_cap", side_effect=[first, second]), \
             mock.patch.object(inference_cli, "_process_frames_core", side_effect=results), \
             mock.patch.object(inference_cli, "clear_memory"):
            produced = list(inference_cli._stream_video_chunks(
                cap=cap,
                frames_to_process=5,
                chunk_size=3,
                overlap=2,
                args=mock.Mock(prepend_frames=0),
                device_id="0",
                debug=inference_cli.debug,
                runner_cache=None,
                total_chunks=2,
                progress=reporter,
            ))

        self.assertEqual([result.shape[0] for result in produced], [3, 2])
        started = [
            call for call in reporter.emit.call_args_list
            if call.args == ("chunk_started",)
        ]
        completed = [
            call for call in reporter.emit.call_args_list
            if call.args == ("chunk_completed",)
        ]
        self.assertEqual([call.kwargs["chunk_unique_frames"] for call in started], [3, 2])
        self.assertEqual([call.kwargs["chunk_context_frames"] for call in started], [0, 2])
        self.assertEqual([call.kwargs["completed_unique_frames"] for call in started], [0, 3])
        self.assertEqual([call.kwargs["completed_unique_frames"] for call in completed], [3, 5])
        self.assertTrue(all(call.kwargs["total_unique_frames"] == 5 for call in started + completed))
```

- [ ] **Step 3: Run new tests and verify missing keyword failures**

Run:

```bash
python -m unittest \
  tests.test_inference_cli_progress.CliProgressLifecycleTests.test_single_gpu_core_passes_same_phase_callback_to_four_phases \
  tests.test_inference_cli_progress.CliProgressLifecycleTests.test_streaming_chunks_exclude_overlap_from_completed_work -v
```

Expected: FAIL with unexpected progress/frame-accounting keyword arguments or missing `chunk_completed` events.

- [ ] **Step 4: Thread reporter through single-file and streaming functions**

Add `progress: Optional[ProgressReporter] = None` to `process_single_file` after `runner_cache`. For single-GPU video call `_stream_video_chunks` with:

```python
                progress=progress,
```

For single-GPU image call `_single_gpu_direct_processing` with:

```python
        result = _single_gpu_direct_processing(
            frames_tensor,
            args,
            device_list[0],
            runner_cache,
            progress=progress,
            chunk_index=1,
            chunk_count=1,
            chunk_unique_frames=1,
            chunk_context_frames=0,
            completed_unique_frames=0,
            total_unique_frames=1,
        )
```

Add `progress: Optional[ProgressReporter] = None` as final `_stream_video_chunks` parameter. Existing variables already expose `new_frames.shape[0]` and `context_count`; initialize `completed_unique_frames = 0` before loop. Immediately before processing each chunk, add:

```python
        if progress is not None and progress.enabled:
            progress.emit(
                "chunk_started",
                phase="encoding",
                chunk_index=chunk_idx,
                chunk_count=max(1, total_chunks),
                chunk_unique_frames=int(new_frames.shape[0]),
                chunk_context_frames=context_count,
                completed_unique_frames=completed_unique_frames,
                total_unique_frames=frames_to_process,
            )
```

Pass these arguments into `_process_frames_core`:

```python
            progress=progress,
            chunk_index=chunk_idx,
            chunk_count=max(1, total_chunks),
            chunk_unique_frames=int(new_frames.shape[0]),
            chunk_context_frames=context_count,
            completed_unique_frames=completed_unique_frames,
            total_unique_frames=frames_to_process,
        )
```

Immediately after `yield result`, which runs only when caller successfully finishes its output-write loop body and requests next generator item, add:

```python
        completed_unique_frames += int(new_frames.shape[0])
        if progress is not None and progress.enabled:
            progress.emit(
                "chunk_completed",
                phase="postprocessing",
                chunk_index=chunk_idx,
                chunk_count=max(1, total_chunks),
                chunk_unique_frames=int(new_frames.shape[0]),
                chunk_context_frames=context_count,
                completed_unique_frames=completed_unique_frames,
                total_unique_frames=frames_to_process,
            )
```

Keep context removal before yield. Thus second chunk may process `chunk_unique_frames + chunk_context_frames` tensors, but completion advances only `chunk_unique_frames`. For non-streaming video, `total_chunks=1`, `chunk_context_frames=0`, and same code emits one truthful start/completion pair.

- [ ] **Step 5: Activate existing four-phase callbacks in core**

Add final parameters to `_process_frames_core`:

```python
    progress: Optional[ProgressReporter] = None,
    chunk_index: int = 1,
    chunk_count: int = 1,
    chunk_unique_frames: int = 0,
    chunk_context_frames: int = 0,
    completed_unique_frames: int = 0,
    total_unique_frames: int = 0,
```

Do not add one-time `mark_model_prepared` state. `_process_frames_core` may call `prepare_runner` and `load_text_embeddings` on every uncached streaming chunk. Immediately before `prepare_runner(...)`, emit chunk-scoped preparation start:

```python
    if progress is not None and progress.enabled:
        progress.emit(
            "model_preparation_started",
            phase="preparing",
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            chunk_unique_frames=chunk_unique_frames,
            chunk_context_frames=chunk_context_frames,
            completed_unique_frames=completed_unique_frames,
            total_unique_frames=total_unique_frames,
        )
```

Immediately after `load_text_embeddings(...)`, emit chunk-scoped completion and construct callback:

```python
    if progress is not None and progress.enabled:
        progress.emit(
            "model_preparation_completed",
            phase="preparing",
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            chunk_unique_frames=chunk_unique_frames,
            chunk_context_frames=chunk_context_frames,
            completed_unique_frames=completed_unique_frames,
            total_unique_frames=total_unique_frames,
        )
        phase_callback = progress.phase_callback(
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            chunk_unique_frames=chunk_unique_frames,
            chunk_context_frames=chunk_context_frames,
            completed_unique_frames=completed_unique_frames,
            total_unique_frames=total_unique_frames,
        )
    else:
        phase_callback = None
```

Replace all four existing `progress_callback=None` arguments in `_process_frames_core` with `progress_callback=phase_callback`.

Change `_single_gpu_direct_processing` signature to accept reporter and complete chunk/frame context, then forward every field:

```python
def _single_gpu_direct_processing(
    frames_tensor: torch.Tensor,
    args: argparse.Namespace,
    device_id: str,
    runner_cache: Optional[Dict[str, Any]],
    progress: Optional[ProgressReporter] = None,
    chunk_index: int = 1,
    chunk_count: int = 1,
    chunk_unique_frames: int = 0,
    chunk_context_frames: int = 0,
    completed_unique_frames: int = 0,
    total_unique_frames: int = 0,
) -> torch.Tensor:
    return _process_frames_core(
        frames_tensor=frames_tensor,
        args=args,
        device_id=device_id,
        debug=debug,
        runner_cache=runner_cache,
        progress=progress,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        chunk_unique_frames=chunk_unique_frames,
        chunk_context_frames=chunk_context_frames,
        completed_unique_frames=completed_unique_frames,
        total_unique_frames=total_unique_frames,
    )
```

Calls from `_gpu_processing` remain unchanged and therefore use disabled default reporter behavior.

- [ ] **Step 6: Run reporter and wiring tests**

Run:

```bash
python -m unittest tests.test_cli_progress tests.test_inference_cli_progress -v
```

Expected: all tests PASS; no model download or GPU inference occurs.

- [ ] **Step 7: Commit measured phase wiring**

```bash
git add src/cli_progress.py inference_cli.py tests/test_cli_progress.py tests/test_inference_cli_progress.py
git commit -m "feat: emit measured SeedVR2 phase progress"
```

---

### Task 4: Emit Output Boundary and Verify Privacy and Failure Semantics

**Files:**
- Modify: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/inference_cli.py:424-594`
- Modify: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/src/cli_progress.py`
- Modify: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/tests/test_inference_cli_progress.py`

**Interfaces:**
- Consumes: reporter lifecycle and counters from Tasks 1-3.
- Produces: `ProgressReporter.mark_output_started(*, chunk_index: int, chunk_count: int, chunk_unique_frames: int, chunk_context_frames: int, completed_unique_frames: int, total_unique_frames: int) -> None`, idempotent across streaming chunks and preserving current frame-accounting state.
- Produces: `output_started` before first output frame/write and no `completed` event on media exceptions or nonzero exits.

- [ ] **Step 1: Write failing output and privacy tests**

Append to `CliProgressLifecycleTests`:

```python
    def test_output_event_is_once_and_contains_no_paths(self) -> None:
        stream = io.StringIO()
        reporter = inference_cli.ProgressReporter(progress_format="jsonl", stream=stream)
        reporter.start()

        reporter.mark_output_started(
            chunk_index=1,
            chunk_count=3,
            chunk_unique_frames=3,
            chunk_context_frames=0,
            completed_unique_frames=0,
            total_unique_frames=8,
        )
        reporter.mark_output_started(
            chunk_index=2,
            chunk_count=3,
            chunk_unique_frames=3,
            chunk_context_frames=2,
            completed_unique_frames=3,
            total_unique_frames=8,
        )
        reporter.close()

        events = json_events(stream.getvalue())
        output_events = [event for event in events if event["event_type"] == "output_started"]
        self.assertEqual(len(output_events), 1)
        serialized = json.dumps(output_events[0]).lower()
        self.assertNotIn("input.mp4", serialized)
        self.assertNotIn("output.mp4", serialized)
        self.assertNotIn("model_dir", serialized)

    def test_processing_exception_returns_nonzero_without_completed(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(inference_cli, "download_weight", return_value=True), \
             mock.patch.object(inference_cli, "get_input_type", return_value="video"), \
             mock.patch.object(inference_cli, "process_single_file", side_effect=RuntimeError("failed")), \
             contextlib.redirect_stdout(stdout):
            result = inference_cli.main(self.base_args() + ["--progress_format", "jsonl"])

        self.assertEqual(result, 1)
        events = json_events(stdout.getvalue())
        self.assertNotIn("completed", [event["event_type"] for event in events])

    def test_keyboard_interrupt_propagates_without_completed_event(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(inference_cli, "download_weight", return_value=True), \
             mock.patch.object(inference_cli, "get_input_type", return_value="video"), \
             mock.patch.object(inference_cli, "process_single_file", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(stdout):
            with self.assertRaises(KeyboardInterrupt):
                inference_cli.main(self.base_args() + ["--progress_format", "jsonl"])

        events = json_events(stdout.getvalue())
        self.assertNotIn("completed", [event["event_type"] for event in events])
```

- [ ] **Step 2: Run new tests and verify missing method failure**

Run:

```bash
python -m unittest \
  tests.test_inference_cli_progress.CliProgressLifecycleTests.test_output_event_is_once_and_contains_no_paths \
  tests.test_inference_cli_progress.CliProgressLifecycleTests.test_processing_exception_returns_nonzero_without_completed \
  tests.test_inference_cli_progress.CliProgressLifecycleTests.test_keyboard_interrupt_propagates_without_completed_event -v
```

Expected: first test FAILS with missing `mark_output_started`; exception and interrupt tests PASS after Task 2 lifecycle work.

- [ ] **Step 3: Implement idempotent output event**

Initialize `self._output_started = False` in `ProgressReporter.__init__`, then add:

```python
    def mark_output_started(
        self,
        *,
        chunk_index: int,
        chunk_count: int,
        chunk_unique_frames: int,
        chunk_context_frames: int,
        completed_unique_frames: int,
        total_unique_frames: int,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._output_started:
                return
            self._output_started = True
        self.emit(
            "output_started",
            phase="output",
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            chunk_unique_frames=chunk_unique_frames,
            chunk_context_frames=chunk_context_frames,
            completed_unique_frames=completed_unique_frames,
            total_unique_frames=total_unique_frames,
        )
```

- [ ] **Step 4: Mark first output write without exposing path**

In single-GPU video loop, immediately before `save_frames_to_image` / `save_frames_to_video`, add:

```python
                if progress is not None and progress.enabled:
                    progress.mark_output_started(
                        chunk_index=chunk_count,
                        chunk_count=total_chunks,
                        chunk_unique_frames=int(result.shape[0]),
                        chunk_context_frames=0,
                        completed_unique_frames=frames_written,
                        total_unique_frames=frames_to_process,
                    )
```

Use `chunk_count` as current chunk index because loop increments it before output. For multi-GPU video, before saving returned result, call:

```python
            if progress is not None and progress.enabled:
                progress.mark_output_started(
                    chunk_index=1,
                    chunk_count=1,
                    chunk_unique_frames=frames_to_process,
                    chunk_context_frames=0,
                    completed_unique_frames=0,
                    total_unique_frames=frames_to_process,
                )
```

For image, immediately before `_save_image_bgr`, call:

```python
    if progress is not None and progress.enabled:
        progress.mark_output_started(
            chunk_index=1,
            chunk_count=1,
            chunk_unique_frames=1,
            chunk_context_frames=0,
            completed_unique_frames=0,
            total_unique_frames=1,
        )
```

Do not pass input or output paths into reporter methods.

- [ ] **Step 5: Run full fork unit suite**

Run:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all reporter and CLI tests PASS.

- [ ] **Step 6: Commit output and failure boundary**

```bash
git add src/cli_progress.py inference_cli.py tests/test_inference_cli_progress.py
git commit -m "feat: report bounded CLI output boundary"
```

---

### Task 5: Document Contract and Run Fork Release Gates

**Files:**
- Modify: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/README.md`
- Test: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/tests/test_cli_progress.py`
- Test: `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler/tests/test_inference_cli_progress.py`

**Interfaces:**
- Consumes: final schema and option from Tasks 1-4.
- Produces: public documentation matching exact CLI contract.
- Produces: security-reviewed public branch `origin/feature/cli-progress-events`; fork `main` remains unchanged from upstream.

- [ ] **Step 1: Add exact README section**

Append under CLI usage documentation in `README.md`:

````markdown
### Machine-readable CLI progress

Standalone single-GPU processing can emit opt-in JSON Lines progress:

```bash
python inference_cli.py input.mp4 --output output.mp4 --progress_format jsonl
```

Default `--progress_format none` preserves existing human-readable output. JSONL events use schema version `1`; `event_type` is one of `model_preparation_started`, `model_preparation_completed`, `chunk_started`, `phase_progress`, `chunk_completed`, `heartbeat`, `output_started`, and `completed`. `sequence` restarts at 1 per CLI invocation. `work_sequence` advances only for `measured_work=true` phase or chunk-completion events, never for heartbeat. Phase counters come from existing encode, upscale, decode, and post-process callbacks. Chunk fields separate unique source frames from temporal-overlap context so overlap is never double-counted. Each event is flushed immediately, is limited to 4096 UTF-8 bytes, and excludes input/output paths, filenames, credentials, environment values, and model-directory values.

Multi-GPU phase progress is not included in this first version. Absence of heartbeat does not cancel inference; callers retain their own deadline and cancellation policy.
````

- [ ] **Step 2: Run deterministic curated release gate twice to catch heartbeat leaks**

From the WebUI checkout, run:

```bash
SEEDVR2_FORK_ROOT=/path/to/ComfyUI-SeedVR2_VideoUpscaler scripts/test-release.sh
SEEDVR2_FORK_ROOT=/path/to/ComfyUI-SeedVR2_VideoUpscaler scripts/test-release.sh
```

Expected: both count-bounded release runs PASS and process exits without lingering `seedvr2-progress-heartbeat` thread. Full unittest discovery remains an opt-in diagnostic.

- [ ] **Step 3: Inspect exact fork diff and ancestry**

Run:

```bash
git status --short
git diff --check upstream/main...HEAD
git log --oneline --decorate upstream/main..HEAD
test "$(git merge-base upstream/main HEAD)" = "4490bd1f482e026674543386bb2a4d176da245b9"
test "$(git rev-list --count upstream/main..main)" = "0"
```

Expected: only README remains uncommitted; diff check and both ancestry assertions succeed; feature commits appear only on feature branch.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md
git commit -m "docs: describe CLI progress event contract"
```

- [ ] **Step 5: Run required security scan before public push**

Invoke `codex-security:security-scan` from `/Users/haohanl/dev/ComfyUI-SeedVR2_VideoUpscaler` against `upstream/main...HEAD` with focus on:

```text
JSONL injection or unbounded output; path, filename, environment, credential, or model-directory disclosure; thread lifecycle or deadlock; counter overflow/regression; default CLI compatibility; exception and exit-code changes.
```

Expected: no unresolved high- or critical-severity findings. Fix validated findings through `codex-security:fix-finding`, rerun unit tests, and rerun `codex-security:security-scan` before proceeding.

- [ ] **Step 6: Run final verification after security fixes**

Run:

```bash
SEEDVR2_FORK_ROOT=/path/to/ComfyUI-SeedVR2_VideoUpscaler scripts/test-release.sh
git diff --check upstream/main...HEAD
git status --short
```

Expected: all tests PASS, diff check produces no output, worktree is clean.

- [ ] **Step 7: Push feature branch only**

```bash
git push --set-upstream origin feature/cli-progress-events
```

Expected: public fork branch exists; no force push; `origin/main` remains at upstream baseline.

- [ ] **Step 8: Record immutable revision for WebUI integration**

Run:

```bash
git rev-parse HEAD
git ls-remote --exit-code origin refs/heads/feature/cli-progress-events
```

Expected: local `HEAD` equals remote branch hash. Supply this full 40-character revision to WebUI installer-pin task; do not update live runtime in this plan.

---

## Completion Criteria

- Fork exists with correct `origin` and `upstream` remotes.
- Fork `main` contains no WebUI-specific commits.
- `feature/cli-progress-events` exposes opt-in schema-version-1 JSONL.
- Default invocation emits no machine events.
- Single-GPU encode, upscale, decode, and post-process callbacks emit measured counters.
- Streaming chunk indexes, invocation-scoped event sequences, work sequences, and overlap-excluding completed unique-frame counters are monotonic.
- Heartbeats carry `measured_work=false` and never advance work counters.
- Invocation-wide and repeated per-chunk model-preparation events are distinguishable by zero versus positive chunk indexes.
- Heartbeats flush during long operations and close cleanly on every exit path.
- Machine events remain within 4096 bytes and contain no sensitive or user-controlled text.
- `completed` appears only on successful processing.
- Unit tests pass twice, security scan is clean, and only feature branch is pushed.
- Live WebUI runtime and active job remain untouched.
