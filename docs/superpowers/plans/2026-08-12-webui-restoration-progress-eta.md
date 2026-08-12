# WebUI SeedVR2 Restoration Progress and ETA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add selectable generative SeedVR2 output scales, measured phase/chunk progress, elapsed time, honest ETA ranges, and a safely pinned SeedVR2 fork runtime to the existing private WebUI.

**Architecture:** Keep SeedVR2 as the only processing engine. Backend owns scale validation, target dimensions, structured-event validation, persistence, progress aggregation, and local-history ETA estimation; tracked adapter only converts the fork's opt-in JSONL stream into a bounded `EVENT <json>` line protocol. React consumes additive API fields, renders measured progress or `Calibrating…`, and updates elapsed time locally between two-second polls.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, pytest, React 19, TypeScript 7, Vite 8, Vitest, Testing Library, zsh, pinned SeedVR2 fork CLI

## Global Constraints

- Execute Tasks 1-9 in an isolated worktree created with `superpowers:using-git-worktrees`; do not edit or switch the live service checkout in place.
- Keep FastAPI bound to `127.0.0.1`; Tailscale Serve remains only remote entry point.
- Never enable Tailscale Funnel.
- Preserve HTTP Basic authentication and same-origin mutation header.
- Keep one private, Tailscale-served WebUI for all supported video restoration work.
- Focus first release on generative SeedVR2 restoration; do not add DPIR or conventional filters.
- Supported output scales are exactly `0.25`, `0.5`, `1`, and `2`; missing scale defaults to `1`.
- `1x Original` is default; `0.5x Balanced` and `0.25x Fast` resize before SeedVR2; `2x Upscale` preserves existing behavior.
- Target dimensions preserve aspect ratio, round to even codec dimensions, and reject a target whose shortest edge is below `256` pixels.
- Reject targets whose longest edge exceeds `7680` pixels or whose area exceeds `33177600` pixels.
- Keep SeedVR2 3B Safe recommended and SeedVR2 7B FP8 experimental with mandatory preflight.
- No engine chaining and no claim that downscaled output preserves original-resolution fine detail.
- No fake linear percentage before measured counters exist; measured generation progress is capped at `91`, final remux at `99`, and only validated MP4 completion reaches `100`.
- SeedVR2 phase display weights remain exactly encoding `20%`, DiT upscaling `25%`, decoding `50%`, and post-processing `5%`; these are not ETA-time weights.
- ETA is always a range, never a single finish timestamp; show `Calibrating…` when evidence is insufficient.
- ETA history remains local SQLite data. Failed, cancelled, interrupted, stale-heartbeat, and mismatched runtime-profile jobs never train estimator.
- Deleting a job removes job-owned phase metrics and artifacts but preserves anonymized `performance_samples` rows that contain no job ID, filename, path, exact dimensions, or timestamps finer than UTC date.
- Progress stream silence never kills a job by itself; existing `86400`-second processing deadline remains authoritative.
- Track `last_heartbeat_at` and `last_progress_at` separately: heartbeats prove process liveness; only changed measured work updates progress time.
- Fork event sequence monotonicity is scoped to one adapter invocation (`preflight` or `full`), never across both invocations.
- Chunk aggregation uses overlap-excluding unique-frame counts, not equal chunk weights; `chunk_completed` closes each chunk.
- Final FFmpeg work reports measured `-progress pipe:1` timestamps from display progress `92` through `99`.
- Progress JSON excludes paths, filenames, credentials, environment data, and model-directory values.
- Bound progress JSON lines to `16384` characters, stage/phase strings to `64` characters, counters to `1000000000`, and accepted event frequency to at most `10` nonterminal events per second.
- Keep subprocess execution shell-free.
- Runtime checkout stays outside WebUI Git repository and pins exact reviewed fork commit from `feature/cli-progress-events`.
- Refuse runtime mutation while any job is `queued`, `preflight`, or `running`; stop exact LaunchAgent and recheck database before install, then recheck again before restart.
- Runtime fingerprints include explicit cache settings; shipped profiles keep every unbenchmarked SeedVR2 cache disabled and add no cache-enabling CLI flag.
- Preserve persistent jobs/results and transactional manual deletion.
- Run `codex-security:security-scan` against fork diff and WebUI before either public push.

---

## File Structure

- Create `backend/app/progress.py`: strict adapter-event parser and phase/chunk-to-display-progress conversion.
- Create `backend/app/eta.py`: pure ETA sample filtering, quantile bounds, confidence, and deadline clamping.
- Create `backend/tests/test_progress.py`: malformed, bounded, monotonic, weighted, and rate-limit event tests.
- Create `backend/tests/test_eta.py`: calibrating, confidence, matching, outlier, and clamp tests.
- Create `scripts/runtime-update-gate.py`: atomically quiesce exact LaunchAgent while holding SQLite admission lock.
- Modify `backend/app/domain.py`: scale constants, target-dimension helper, expanded immutable `Job`, progress/ETA value types.
- Modify `backend/app/config.py`: default scale, stale threshold, device class, and runtime fingerprint inputs.
- Modify `backend/app/job_store.py`: additive jobs migration, phase metrics table, atomic event/timing writes, historical sample queries, cascade deletion.
- Modify `backend/app/job_service.py`: scale admission, target/output validation, event persistence, ETA refresh, additive public response.
- Modify `backend/app/runner.py`: pass selected scale, parse structured adapter events, rate-limit reports, preserve legacy progress handling.
- Modify `backend/app/main.py`: config metadata, `output_scale` form field, and service-owned public serialization.
- Modify `backend/tests/test_jobs.py`: scale/API/migration/lifecycle/output/history/deletion coverage.
- Modify `backend/tests/test_runner.py`: structured event forwarding, line bounds, rate limiting, and adapter argv coverage.
- Modify `backend/tests/test_seedvr2_adapter.py`: scale calculations, fork JSONL option, sanitized event bridge, and default output behavior.
- Modify `scripts/seedvr2-adapter.py`: scale argv and fork-event bridge.
- Modify `scripts/install-runtime.sh`: fork origin/upstream verification, exact revision pin, and active-job update gate.
- Modify `backend/tests/test_install_security.py`: fork provenance and active-job update tests.
- Modify `backend/tests/test_runner.py`: bounded final-output ffprobe validation and per-invocation sequence scope.
- Modify `frontend/src/types.ts`: scale/config/progress/ETA API types.
- Modify `frontend/src/api.ts`: submit `output_scale`.
- Modify `frontend/src/App.tsx`: scale selector, target preview, truthful progress, elapsed, ETA, confidence, stale warning.
- Modify `frontend/src/styles.css`: responsive scale and timing presentation.
- Modify `frontend/src/__tests__/api.test.ts`: multipart scale assertion.
- Modify `frontend/src/__tests__/App.test.tsx`: scale and progress/ETA UI behavior.
- Modify `frontend/src/__tests__/styles.test.ts`: responsive/accessibility style contract.
- Modify `deploy/runtime.env.example`: scale/stale/device-class settings and fork documentation.
- Modify `README.md`, `docs/architecture.md`, and `docs/runtime.md`: new workflow, API states, fork update/deployment procedure.

## External Fork Dependency

Do not begin Tasks 4, 8, or 9 until fork plan completes and supplies both interfaces below:

```text
fork_repository = https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git
fork_branch = feature/cli-progress-events
fork_revision = reviewed 40-character lowercase commit SHA from that branch
```

Fork CLI contract consumed by this plan:

```json
{"schema_version":1,"sequence":1,"work_sequence":1,"measured_work":true,"event_type":"phase_progress","phase":"encoding","current_unit":1,"total_units":10,"chunk_index":1,"chunk_count":4,"completed_unique_frames":0,"chunk_unique_frames":21,"chunk_context_frames":4,"total_unique_frames":80,"elapsed_seconds":2.5}
```

`--progress_format jsonl` is opt-in. Supported `event_type` values are `model_preparation_started`, `model_preparation_completed`, `chunk_started`, `phase_progress`, `chunk_completed`, `heartbeat`, `output_started`, and `completed`. `sequence` increments for every event within one invocation; `work_sequence` increments only when `measured_work=true` and measured counters advance. For an active chunk, `completed_unique_frames` excludes that chunk and all temporal-overlap frames; `chunk_unique_frames` counts only new frames contributed by that chunk; `chunk_context_frames` reports overlap supplied for temporal context but never enters progress; `total_unique_frames` equals source frame count. Default fork output remains human-readable and compatible with upstream.

### Task 1: Output Scale Domain, Migration, and API Contract

**Files:**
- Modify: `backend/app/domain.py:7-65`
- Modify: `backend/app/config.py:8-118`
- Modify: `backend/app/job_store.py:19-286`
- Modify: `backend/app/job_service.py:33-82,239-276`
- Modify: `backend/app/main.py:99-126`
- Test: `backend/tests/test_jobs.py`

**Interfaces:**
- Consumes: existing `MediaInfo`, upload admission, `JobStore.create`, and form-based `/api/jobs`.
- Produces: `OUTPUT_SCALES: frozenset[float]`, `DEFAULT_OUTPUT_SCALE = 1.0`, `MIN_TARGET_SHORT_SIDE = 256`, `MAX_TARGET_EDGE = 7680`, `MAX_TARGET_PIXELS = 33_177_600`, `target_dimensions(width: int, height: int, scale: float) -> tuple[int, int]`, `Job.output_scale: float`, `Job.target_width: int`, `Job.target_height: int`, `Job.frame_count: int`, and `Job.runtime_profile_fingerprint: str`.

- [ ] **Step 1: Write failing target-dimension and scale-admission tests**

Add to `backend/tests/test_jobs.py`:

```python
import sqlite3

import pytest

from app.domain import target_dimensions


@pytest.mark.parametrize(
    ("width", "height", "scale", "expected"),
    [
        (1920, 1080, 1.0, (1920, 1080)),
        (1920, 1080, 0.5, (960, 540)),
        (2160, 3840, 0.25, (540, 960)),
        (1281, 719, 0.5, (640, 360)),
        (1920, 1080, 2.0, (3840, 2160)),
    ],
)
def test_target_dimensions_preserve_aspect_and_even_codec_dimensions(width, height, scale, expected):
    assert target_dimensions(width, height, scale) == expected


def test_upload_defaults_to_original_resolution_and_exposes_target_dimensions(tmp_path):
    client = make_client(tmp_path)
    response = submit_video(client)
    assert response.status_code == 201
    assert response.json()["output_scale"] == 1.0
    assert response.json()["target_width"] == 640
    assert response.json()["target_height"] == 360


@pytest.mark.parametrize("scale", ["0.25", "0.5", "1", "2"])
def test_upload_accepts_fixed_output_scale_allowlist(tmp_path, scale):
    class ScaleProbe:
        def inspect(self, path: Path) -> dict[str, float | int]:
            return {"duration_seconds": 3.5, "width": 1920, "height": 1080}

    client = make_client(tmp_path, probe=ScaleProbe())
    response = submit_video(client, output_scale=scale)
    assert response.status_code == 201
    assert response.json()["output_scale"] == float(scale)


@pytest.mark.parametrize("scale", ["0", "0.3", "4", "nan", "inf", "1;touch /tmp/pwned"])
def test_upload_rejects_non_allowlisted_output_scale(tmp_path, scale):
    client = make_client(tmp_path)
    response = submit_video(client, output_scale=scale)
    assert response.status_code == 422
    assert client.get("/api/jobs").json() == {"jobs": []}


def test_quarter_scale_rejects_unsafe_short_edge(tmp_path):
    client = make_client(tmp_path)
    response = submit_video(client, output_scale="0.25")
    assert response.status_code == 422
    assert response.json()["detail"] == "Target shortest edge must be at least 256 pixels"


@pytest.mark.parametrize(
    ("width", "height", "scale", "detail"),
    [
        (4096, 2160, 2.0, "Target longest edge must not exceed 7680 pixels"),
        (3840, 2162, 2.0, "Target pixel count must not exceed 33177600 pixels"),
    ],
)
def test_target_dimensions_enforce_edge_and_pixel_ceiling(width, height, scale, detail):
    with pytest.raises(ValueError, match=detail):
        target_dimensions(width, height, scale)
```

Extend helper exactly:

```python
def submit_video(client, *, preset="3b-safe", color_correction="lab", output_scale=None, name="clip.mp4"):
    data = {"preset": preset, "color_correction": color_correction}
    if output_scale is not None:
        data["output_scale"] = output_scale
    return client.post(
        "/api/jobs",
        files={"video": (name, b"not a real video because probe is injected", "video/mp4")},
        data=data,
    )
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `cd backend && uv run --group dev pytest tests/test_jobs.py -k 'target_dimensions or output_scale or quarter_scale' -v`

Expected: collection/import failure for `target_dimensions`, then missing `output_scale` contract failures after import exists.

- [ ] **Step 3: Add scale constants and deterministic dimension calculation**

Add to `backend/app/domain.py`:

```python
import math

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
```

Add immutable `Job` fields immediately after `color_correction`:

```python
output_scale: float
target_width: int
target_height: int
frame_count: int
runtime_profile_fingerprint: str
```

Expose these five fields from `Job.public_dict()`.

- [ ] **Step 4: Add ordered, idempotent legacy migrations and profile fingerprint**

Use `PRAGMA user_version` with scale-schema version `1`. Fresh databases create Task 1 schema then set version `1`. Version `0` databases inspect `PRAGMA table_info(jobs)` because released databases predate versioning; run all missing-column statements and backfills inside one `BEGIN IMMEDIATE` transaction, then set version `1`. Re-running `initialize()` at version `1` performs no `ALTER TABLE` or backfill. Task 5 advances this ordered schema to version `2`. Never infer a historical cache/device/model configuration that was not stored.

After existing `CREATE TABLE`, inspect columns and add missing columns one at a time:

```python
columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
migrations = {
    "output_scale": "ALTER TABLE jobs ADD COLUMN output_scale REAL NOT NULL DEFAULT 2.0",
    "target_width": "ALTER TABLE jobs ADD COLUMN target_width INTEGER NOT NULL DEFAULT 0",
    "target_height": "ALTER TABLE jobs ADD COLUMN target_height INTEGER NOT NULL DEFAULT 0",
    "frame_count": "ALTER TABLE jobs ADD COLUMN frame_count INTEGER NOT NULL DEFAULT 0",
    "runtime_profile_fingerprint": "ALTER TABLE jobs ADD COLUMN runtime_profile_fingerprint TEXT NOT NULL DEFAULT 'legacy:unknown'",
}
for name, statement in migrations.items():
    if name not in columns:
        connection.execute(statement)
connection.execute(
    "UPDATE jobs SET target_width = width * 2, target_height = height * 2 "
    "WHERE target_width = 0 OR target_height = 0"
)
```

Task 1 legacy backfill is exact: `output_scale=2.0`, `target_width=width*2`, `target_height=height*2`, `frame_count=0`, and `runtime_profile_fingerprint='legacy:unknown'`. Task 5 gives these rows null timing/progress fields and excludes them from training. Legacy dimensions exceeding new ceilings remain readable/downloadable but cannot be cloned into a new job.

Extend `JobStore.create` with keyword-only `output_scale`, `target_width`, `target_height`, and `runtime_profile_fingerprint`; persist `media.frame_count`. Extend `_to_job` with all five fields.

Add to `Settings`:

```python
default_output_scale: float = 1.0
device_backend_class: str = "apple-mps"
heartbeat_stale_seconds: int = 120
progress_stale_seconds: int = 300
```

Read `VIDEO_UPSCALE_DEFAULT_OUTPUT_SCALE`, `VIDEO_UPSCALE_DEVICE_BACKEND_CLASS`, `VIDEO_UPSCALE_HEARTBEAT_STALE_SECONDS`, and `VIDEO_UPSCALE_PROGRESS_STALE_SECONDS` in `from_environment`; copy them in `with_data_root`.

In `JobService.create_job`, parse form value as `float`, require membership in `OUTPUT_SCALES`, call `target_dimensions`, and construct fingerprint exactly:

```python
runtime_profile_fingerprint = (
    f"seedvr2:{preset}:{self.settings.device_backend_class}:"
    f"scale={output_scale:g}:batch=5:chunk=25:overlap=4:"
    "dit_cache=disabled:vae_cache=disabled"
)
```

Translate `ValueError` from target sizing to `HTTPException(422, str(error))` before `JobStore.create`.

- [ ] **Step 5: Wire form and config API**

Change signatures:

```python
async def JobService.create_job(
    self,
    upload: UploadFile,
    preset: str | None,
    color_correction: str,
    output_scale: float | None,
) -> Job: ...
```

```python
output_scale: float | None = Form(None)
```

Return config payload:

```python
return {
    "default_profile": settings.default_profile,
    "presets": ["3b-safe", "7b-fp8-experimental"],
    "default_output_scale": settings.default_output_scale,
    "output_scales": [
        {"value": 1.0, "label": "1x Original", "description": "Original dimensions; full generative restoration."},
        {"value": 0.5, "label": "0.5x Balanced", "description": "Half width and height; generative restoration with fewer output pixels."},
        {"value": 0.25, "label": "0.25x Fast", "description": "Quarter width and height; experimental generative restoration."},
        {"value": 2.0, "label": "2x Upscale", "description": "Double width and height; highest processing cost."},
    ],
}
```

- [ ] **Step 6: Add legacy migration test**

Create a pre-feature SQLite `jobs` table in `test_legacy_database_migrates_existing_jobs_as_2x`, insert both terminal and active legacy rows, run `JobStore.initialize()` twice, then assert:

```python
job = store.get("legacy")
assert job is not None
assert job.output_scale == 2.0
assert (job.target_width, job.target_height) == (1280, 720)
assert job.frame_count == 0
assert job.runtime_profile_fingerprint == "legacy:unknown"
assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
```

Assert second initialization leaves identical schema/data and active legacy rows are recovered through existing interrupted-job behavior after migration.

- [ ] **Step 7: Run backend tests and commit**

Run: `cd backend && uv run --group dev pytest tests/test_jobs.py -v`

Expected: PASS.

```bash
git add backend/app/domain.py backend/app/config.py backend/app/job_store.py backend/app/job_service.py backend/app/main.py backend/tests/test_jobs.py
git commit -m "feat: add SeedVR2 restoration scales"
```

### Task 2: Adapter Scale Selection and Output Validation

**Files:**
- Modify: `scripts/seedvr2-adapter.py:21-285`
- Modify: `backend/app/runner.py:107-214`
- Modify: `backend/app/job_service.py:205-237`
- Modify: `backend/app/media.py:11-95`
- Test: `backend/tests/test_seedvr2_adapter.py`
- Test: `backend/tests/test_runner.py`
- Test: `backend/tests/test_jobs.py`

**Interfaces:**
- Consumes: Task 1 `Job.output_scale`, `target_width`, `target_height`, and `target_dimensions`.
- Produces: adapter `--output-scale {0.25,0.5,1,2}`, `target_short_side(source_width: int, source_height: int, output_scale: float) -> int`, final output dimension validation within two pixels per axis through bounded `SubprocessMediaProbe`, and FFmpeg `92`-through-`99` remux progress from `-progress pipe:1`.

- [ ] **Step 1: Write failing adapter and runner scale tests**

Replace fixed 2x test with:

```python
@pytest.mark.parametrize(
    ("scale", "expected_short_side"),
    [(0.25, 540), (0.5, 1080), (1.0, 2160), (2.0, 4320)],
)
def test_adapter_uses_selected_seedvr2_target_short_side(tmp_path, scale, expected_short_side):
    adapter = load_adapter()
    command = adapter.build_seedvr2_command(
        input_path=tmp_path / "input.mp4",
        output_path=tmp_path / "video-only.mp4",
        model_dir=tmp_path / "models",
        model_name="three-b.safetensors",
        preset="3b-safe",
        color_correction="lab",
        source_width=2160,
        source_height=3840,
        output_scale=scale,
        python="seed-python",
        official_cli=tmp_path / "inference_cli.py",
    )
    assert command[command.index("--resolution") + 1] == str(expected_short_side)
    assert "--use_cache" not in command
    assert "--cache_model" not in command
    assert "--cache_device" not in command
```

Add runner assertion to `backend/tests/test_runner.py` by patching `_run_process`, invoking `_execute` with a `Job(output_scale=0.5, ...)`, and asserting adjacent argv:

```python
command = run_process.call_args.args[0]
assert command[command.index("--output-scale") + 1] == "0.5"
```

Add output mismatch test to `backend/tests/test_jobs.py` with a runner that writes output and a probe returning `320x180` for output while job expects `640x360`:

```python
failed = wait_for_status(client, job_id, "failed")
assert failed["error"] == "Final MP4 dimensions do not match validated target"
```

Add bounded final-output probe test to `backend/tests/test_runner.py`:

```python
def test_final_output_ffprobe_metadata_is_bounded(tmp_path):
    probe = SubprocessMediaProbe("ffprobe")

    def oversized_probe(_command, *, stdout, **_kwargs):
        stdout.write(b"x" * (MAX_PROBE_OUTPUT_BYTES + 1))
        return SimpleNamespace(returncode=0)

    with patch("app.media.subprocess.run", side_effect=oversized_probe):
        with pytest.raises(ValueError, match="ffprobe metadata exceeds safety limit"):
            probe.inspect(tmp_path / "result.mp4")
```

Add FFmpeg progress test to `backend/tests/test_seedvr2_adapter.py` with fake stdout lines `out_time_us=0`, `out_time_us=50000000`, `out_time_us=100000000`, and `progress=end` for a `100`-second source. Assert reports are monotonic, begin at `92`, include `95`, end at `99`, and command contains adjacent `-progress pipe:1`.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `cd backend && uv run --group dev pytest tests/test_seedvr2_adapter.py tests/test_runner.py tests/test_jobs.py -k 'scale or output_mismatch or final_output_ffprobe or ffmpeg_progress' -v`

Expected: FAIL because adapter and runner lack output-scale argv and service does not inspect final dimensions.

- [ ] **Step 3: Add scale to adapter without shell interpolation**

Add parser option:

```python
argument_parser.add_argument("--output-scale", required=True, type=float, choices=(0.25, 0.5, 1.0, 2.0))
```

Add `output_scale: float` to `build_seedvr2_command`; calculate:

```python
target_width = max(2, int(source_width * output_scale / 2 + 0.5) * 2)
target_height = max(2, int(source_height * output_scale / 2 + 0.5) * 2)
target_short_side = min(target_width, target_height)
if target_short_side < 256:
    raise ValueError("Target shortest edge must be at least 256 pixels")
if max(target_width, target_height) > 7680:
    raise ValueError("Target longest edge must not exceed 7680 pixels")
if target_width * target_height > 33_177_600:
    raise ValueError("Target pixel count must not exceed 33177600 pixels")
```

Pass `args.output_scale` from `main`. In `SubprocessRunner._execute`, add fixed argv elements:

```python
"--output-scale",
format(job.output_scale, "g"),
"--duration-seconds",
format(job.duration_seconds, ".6f"),
```

Do not add any SeedVR2 cache flag. `dit_cache=disabled:vae_cache=disabled` in runtime fingerprint records measured configuration; enabling cache requires separate benchmarks, design approval, fingerprint value, and tests.

- [ ] **Step 4: Validate final MP4 dimensions before completion**

After runner output existence check and before `store.complete`, inspect output through existing `MediaProbe`; reject if either axis differs by more than two pixels:

```python
output_media = normalize_media_info(self.media_probe.inspect(job.output_path))
if (
    abs(output_media.width - job.target_width) > 2
    or abs(output_media.height - job.target_height) > 2
):
    raise RuntimeError("Final MP4 dimensions do not match validated target")
```

Update injected test probes so `inspect(path)` returns target dimensions for result paths, not only input dimensions.

- [ ] **Step 5: Emit measured FFmpeg finalization progress from 92 through 99**

Add required finite positive adapter `--duration-seconds`. Add `"-progress", "pipe:1"` to `final_mp4_command`. Replace `subprocess.run` remux calls with:

```python
def run_ffmpeg_with_progress(command: list[str], duration_seconds: float) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    last_percent = 92
    emit_progress(last_percent, "audio-remux")
    while line := process.stdout.readline(MAX_OUTPUT_LINE_CHARS + 1):
        bounded = line[:MAX_OUTPUT_LINE_CHARS].strip()
        if bounded.startswith("out_time_us="):
            try:
                seconds = max(0.0, int(bounded.split("=", 1)[1]) / 1_000_000)
            except ValueError:
                continue
            percent = min(99, 92 + int(7 * min(1.0, seconds / duration_seconds)))
            if percent > last_percent:
                last_percent = percent
                emit_progress(percent, "audio-remux")
        elif bounded == "progress=end" and last_percent < 99:
            last_percent = 99
            emit_progress(99, "audio-remux")
    if process.wait() != 0:
        raise subprocess.CalledProcessError(process.returncode, command)
```

Use same function for audio-copy attempt and AAC fallback. Progress may restart at `92` during fallback; backend monotonic merge must retain prior value. `completed` remains `99` until JobService validates output and calls `complete` for `100`.

- [ ] **Step 6: Prove final validation uses bounded shared ffprobe path**

Do not create a separate result-only probe. Keep `SubprocessMediaProbe.inspect` temporary-file capture capped by `MAX_PROBE_OUTPUT_BYTES = 64 * 1024`; remove its test-only `result.stdout` bypass so production and mocked calls always read only bounded temporary-file bytes. Run upload and output metadata through this same method.

- [ ] **Step 7: Run tests and commit**

Run: `cd backend && uv run --group dev pytest tests/test_seedvr2_adapter.py tests/test_runner.py tests/test_jobs.py -v`

Expected: PASS.

```bash
git add scripts/seedvr2-adapter.py backend/app/runner.py backend/app/job_service.py backend/app/media.py backend/tests/test_seedvr2_adapter.py backend/tests/test_runner.py backend/tests/test_jobs.py
git commit -m "feat: run SeedVR2 at selected output scale"
```

### Task 3: Strict Structured Progress Event Contract

**Files:**
- Create: `backend/app/progress.py`
- Create: `backend/tests/test_progress.py`
- Modify: `backend/app/domain.py`
- Modify: `backend/app/runner.py:13-321`
- Test: `backend/tests/test_runner.py`

**Interfaces:**
- Consumes: fork event JSON contract documented under External Fork Dependency.
- Produces: `ProgressEvent`, `ProgressReport`, `parse_progress_line(line: str) -> ProgressEvent | None`, `aggregate_progress(event: ProgressEvent) -> ProgressReport`, and runner callback `ProgressReporter = Callable[[ProgressReport], None]`; each report carries invocation scope `preflight` or `full`.

- [ ] **Step 1: Write failing parser boundary and weighting tests**

Create `backend/tests/test_progress.py`:

```python
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
    [("encoding", 10, 29), ("upscaling", 10, 35), ("decoding", 10, 47), ("postprocessing", 10, 48)],
)
def test_phase_weights_aggregate_inside_chunk_and_generation_cap(phase, current, expected):
    report = aggregate_progress(parse_progress_line(event_line(phase=phase, current_unit=current)))
    assert report.percent == expected
    assert report.percent <= 91


def test_chunk_completion_uses_unique_frames_not_equal_chunk_weight():
    report = aggregate_progress(parse_progress_line(event_line(
        event_type="chunk_completed",
        phase=None,
        current_unit=None,
        total_units=None,
        completed_unique_frames=21,
        chunk_unique_frames=0,
        total_unique_frames=80,
    )))
    assert report.percent == 24


def test_temporal_overlap_never_counts_twice():
    first = aggregate_progress(parse_progress_line(event_line(
        chunk_index=1, completed_unique_frames=0, chunk_unique_frames=21,
        total_unique_frames=80, phase="postprocessing", current_unit=10,
    )))
    second = aggregate_progress(parse_progress_line(event_line(
        event_type="chunk_completed", phase=None, current_unit=None, total_units=None,
        chunk_index=1, completed_unique_frames=21, chunk_unique_frames=0,
        total_unique_frames=80,
    )))
    assert first.percent == second.percent == 24
```

Expected active-chunk percentage uses `round(91 * (completed_unique_frames + chunk_unique_frames * (phase_offset + phase_weight * current_unit/total_units)) / total_unique_frames)`. `completed_unique_frames` excludes current chunk and temporal-overlap frames. `chunk_completed` uses `round(91 * completed_unique_frames / total_unique_frames)` after fork includes newly closed chunk. Equal chunk count never enters percentage math.

- [ ] **Step 2: Run parser tests and verify failure**

Run: `cd backend && uv run --group dev pytest tests/test_progress.py -v`

Expected: import failure because `app.progress` does not exist.

- [ ] **Step 3: Implement bounded parser and weighted aggregation**

Create `backend/app/progress.py` with these exact public types and constants:

```python
from __future__ import annotations

import json
import math
from dataclasses import dataclass

MAX_PROGRESS_JSON_CHARS = 16_384
MAX_EVENT_STRING_CHARS = 64
MAX_COUNTER = 1_000_000_000
EVENT_TYPES = frozenset({
    "model_preparation_started", "model_preparation_completed", "chunk_started",
    "phase_progress", "chunk_completed", "heartbeat", "output_started", "completed",
})
PHASES = ("encoding", "upscaling", "decoding", "postprocessing")
PHASE_WEIGHTS = {"encoding": 0.20, "upscaling": 0.25, "decoding": 0.50, "postprocessing": 0.05}
PHASE_OFFSETS = {"encoding": 0.0, "upscaling": 0.20, "decoding": 0.45, "postprocessing": 0.95}


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


def parse_progress_line(line: str) -> ProgressEvent | None: ...
def aggregate_progress(event: ProgressEvent) -> ProgressReport: ...
```

Parser requirements:

```python
if not line.startswith("EVENT "):
    return None
encoded = line[6:]
if len(encoded) > MAX_PROGRESS_JSON_CHARS:
    return None
try:
    payload = json.loads(encoded)
except (json.JSONDecodeError, RecursionError):
    return None
if not isinstance(payload, dict) or payload.get("schema_version") != 1:
    return None
```

Read only named fields. Require `work_sequence` as bounded integer and `measured_work` as a real JSON boolean; reject booleans where integer counters are required. Reject nonfinite elapsed time, negative counters, counters above `MAX_COUNTER`, `work_sequence > sequence`, `current_unit > total_units`, `chunk_index < 1`, `chunk_index > chunk_count`, unknown event/phase, strings over 64 characters, and any retained string containing `/`, `\\`, `..`, or `:`. For phase events require `measured_work=true`, positive `chunk_unique_frames` and `total_unique_frames`, nonnegative `chunk_context_frames`, and `completed_unique_frames + chunk_unique_frames <= total_unique_frames`. For `chunk_completed`, require `measured_work=true`, `chunk_unique_frames == 0`, and `completed_unique_frames <= total_unique_frames`. Heartbeat requires `measured_work=false`; model/output lifecycle events may use false. Ignore unknown keys.

Stage map is exact: `model-preparation`, `chunk-start`, `encoding`, `ai-upscaling`, `decoding`, `post-processing`, `chunk-complete`, `heartbeat`, `seedvr2-output`, `seedvr2-complete`. Fork `output_started` and `completed` remain at generation cap `91`; only Task 2 FFmpeg `PROGRESS` timestamps advance `92` through `99`. Heartbeat retains `0` until persistence merges it with prior progress. `aggregate_progress` copies event `work_sequence` and `measured_work` into report.

- [ ] **Step 4: Convert runner callback while retaining legacy `PROGRESS` compatibility**

Change `ProgressReporter` to `Callable[[ProgressReport], None]`. For legacy lines, report:

```python
percent = int(match.group(1))
last_seen_work_sequence += 1
report_progress(ProgressReport(
    percent=percent,
    stage=match.group(2) or "processing",
    invocation=invocation,
    work_sequence=last_seen_work_sequence,
    measured_work=percent > last_legacy_percent,
))
last_legacy_percent = max(last_legacy_percent, percent)
```

For structured lines:

```python
event = parse_progress_line(line.strip())
if event is not None:
    report_progress(dataclasses.replace(aggregate_progress(event), invocation=invocation))
```

Add required `_run_process(..., invocation: Literal["preflight", "full"])`; `_execute` passes its `mode`, and preview passes `preflight`. Update `UnavailableRunner`, preview no-op, and all fake runners/tests to call `report_progress(ProgressReport(..., invocation="full"))`. Do not remove legacy regex because Task 2 FFmpeg finalization deliberately emits bounded legacy `PROGRESS` lines.

- [ ] **Step 5: Add per-invocation monotonic sequence and 10 Hz rate-limit test**

In `backend/tests/test_runner.py`, one `_run_process(invocation="preflight")` feeds sequence `2`, regressing sequence `1`, then 20 heartbeats at one timestamp; assert sequence `1` is rejected and only one rate-limited heartbeat passes. A second `_run_process(invocation="full")` feeds sequence `1`; assert it is accepted because sequence scope restarted. Within each `_run_process`, initialize `last_sequence = -1`, `last_seen_work_sequence = -1`, `last_legacy_percent = -1`, and `last_nonterminal_report_at`; reject fork `event.sequence <= last_sequence`; accept terminal fork lifecycle events immediately; otherwise accept at `>= 0.1` second intervals. Each accepted fork event updates `last_seen_work_sequence=max(last_seen_work_sequence,event.work_sequence)`; later FFmpeg reports increment from that value, so one invocation has monotonic work sequence while fork event sequence remains untouched. Never reject ordinary log persistence.

- [ ] **Step 6: Run tests and commit**

Run: `cd backend && uv run --group dev pytest tests/test_progress.py tests/test_runner.py -v`

Expected: PASS.

```bash
git add backend/app/progress.py backend/app/domain.py backend/app/runner.py backend/tests/test_progress.py backend/tests/test_runner.py backend/tests/test_jobs.py
git commit -m "feat: validate measured SeedVR2 progress events"
```

### Task 4: Fork JSONL Adapter Bridge

**Files:**
- Modify: `scripts/seedvr2-adapter.py:38-119,199-285`
- Modify: `backend/app/runner.py:130-157`
- Test: `backend/tests/test_seedvr2_adapter.py`

**Interfaces:**
- Consumes: completed fork's `--progress_format jsonl` and schema-v1 JSON objects.
- Produces: sanitized, canonical `EVENT <compact-json>` stdout lines; all human-readable fork output remains visible and immediately flushed.

- [ ] **Step 1: Write failing bridge tests**

Add to `backend/tests/test_seedvr2_adapter.py`:

```python
def test_official_command_opts_into_jsonl_progress(tmp_path):
    adapter = load_adapter()
    command = adapter.build_seedvr2_command(
        input_path=tmp_path / "input.mp4", output_path=tmp_path / "out.mp4",
        model_dir=tmp_path / "models", model_name="3b.safetensors", preset="3b-safe",
        color_correction="lab", source_width=1920, source_height=1080, output_scale=1.0,
        python="python", official_cli=tmp_path / "inference_cli.py",
    )
    assert command[command.index("--progress_format") + 1] == "jsonl"


def test_adapter_bridges_only_safe_fork_json_events(capsys):
    adapter = load_adapter()
    safe = '{"schema_version":1,"sequence":1,"work_sequence":0,"measured_work":false,"event_type":"heartbeat","elapsed_seconds":2.0}'
    leaked = '{"schema_version":1,"sequence":2,"work_sequence":0,"measured_work":false,"event_type":"heartbeat","input_path":"/Users/private/movie.mp4"}'
    adapter.forward_seedvr2_line(safe)
    adapter.forward_seedvr2_line(leaked)
    output = capsys.readouterr().out.splitlines()
    assert output[0].startswith("EVENT ")
    assert "/Users/private" not in "\n".join(output)
    assert "input_path" not in "\n".join(output)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd backend && uv run --group dev pytest tests/test_seedvr2_adapter.py -k 'jsonl or bridges' -v`

Expected: FAIL because command lacks fork flag and `forward_seedvr2_line` is undefined.

- [ ] **Step 3: Add canonical safe bridge**

Add adapter constants matching backend allowed keys/types. Implement:

```python
FORK_EVENT_KEYS = (
    "schema_version", "sequence", "work_sequence", "measured_work", "event_type",
    "phase", "current_unit", "total_units", "chunk_index", "chunk_count",
    "completed_unique_frames", "chunk_unique_frames", "chunk_context_frames",
    "total_unique_frames", "elapsed_seconds",
)


def forward_seedvr2_line(line: str) -> None:
    stripped = line.rstrip("\r\n")[:MAX_OUTPUT_LINE_CHARS]
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        print(stripped, flush=True)
        return
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        print(stripped, flush=True)
        return
    sanitized = {key: payload[key] for key in FORK_EVENT_KEYS if key in payload}
    print("EVENT " + json.dumps(sanitized, separators=(",", ":"), allow_nan=False), flush=True)
```

Change `run_command` loop to call `forward_seedvr2_line(line)`. Add `--progress_format jsonl` to fork argv. Backend runner continues persisting human output, but `consume_output_line` must omit `EVENT ` lines from user-visible log to avoid duplicating machine data.

- [ ] **Step 4: Preserve cancellation/nonzero behavior tests**

Keep existing early-stream test. Add a fake child returning code `7`; assert `run_command` raises `RuntimeError("SeedVR2 CLI exited with 7")`. Add signal test confirming the direct fork process still receives termination.

- [ ] **Step 5: Run tests and commit**

Run: `cd backend && uv run --group dev pytest tests/test_seedvr2_adapter.py tests/test_runner.py -v`

Expected: PASS.

```bash
git add scripts/seedvr2-adapter.py backend/app/runner.py backend/tests/test_seedvr2_adapter.py backend/tests/test_runner.py
git commit -m "feat: bridge fork progress into WebUI"
```

### Task 5: Timing Persistence and Phase Metrics

**Files:**
- Modify: `backend/app/domain.py`
- Modify: `backend/app/job_store.py:19-286`
- Modify: `backend/app/job_service.py:92-237`
- Test: `backend/tests/test_jobs.py`

**Interfaces:**
- Consumes: Task 3 `ProgressReport` and `ProgressEvent`.
- Produces: job timing/progress columns, job-owned `job_phase_metrics`, durable anonymized `performance_samples`, `JobStore.record_report(job_id: str, report: ProgressReport, now: datetime | None = None) -> bool`, `JobStore.complete(job_id: str, *, publish_performance: bool, now: datetime | None = None) -> None`, `JobStore.phase_samples(job_id: str) -> list[PhaseSample]`, and service public serialization with separate heartbeat/progress freshness.

- [ ] **Step 1: Write failing lifecycle, monotonic, and cascade tests**

Add tests that claim a queued job at fixed UTC time, record two valid phase reports, then complete it. Assert:

```python
assert running.started_at is not None
assert running.finished_at is None
assert store.record_report(job.id, sequence_7_report, now=t1) is True
assert store.record_report(job.id, sequence_6_report, now=t2) is False
updated = store.get(job.id)
assert updated.phase_current == 5
assert updated.phase_total == 10
assert updated.chunk_current == 1
assert updated.chunk_total == 4
assert updated.last_heartbeat_at == t1.isoformat()
assert updated.last_progress_at == t1.isoformat()
assert updated.last_event_invocation == "full"
assert updated.last_event_sequence == 7
assert updated.last_work_sequence == 5
store.complete(job.id, publish_performance=True, now=t2)
assert store.get(job.id).finished_at is not None
```

Record a heartbeat with higher `sequence`, unchanged `work_sequence`, and `measured_work=false` at `t2`; assert `last_heartbeat_at == t2` while `last_progress_at == t1`. Record duplicate measured counters with unchanged `work_sequence`; assert progress time stays `t1`. Record `preflight` sequence `7`, reset through `mark_running`, then record `full` sequence `1`; assert both invocation-local sequences are accepted.

After full phase completion, assert one row exists for `(job_id, invocation, chunk_index, phase)`. Complete job and assert anonymized `performance_samples` rows contain phase/rate/fingerprint/bucket but no job ID or exact filename/path/dimensions. After `store.delete(job.id)`, assert job-owned metrics are zero while performance samples remain unchanged. Cancelled/failed/legacy/stale-heartbeat jobs must create zero performance samples.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `cd backend && uv run --group dev pytest tests/test_jobs.py -k 'timing or monotonic or phase_metrics or performance_samples or invocation' -v`

Expected: FAIL because timing columns/table and `record_report` do not exist.

- [ ] **Step 3: Add additive timing schema and typed fields**

Migrate jobs with nullable/default columns:

```sql
started_at TEXT,
finished_at TEXT,
last_heartbeat_at TEXT,
last_progress_at TEXT,
progress_source TEXT NOT NULL DEFAULT 'none',
phase_name TEXT,
phase_current INTEGER,
phase_total INTEGER,
chunk_current INTEGER,
chunk_total INTEGER,
eta_low_seconds INTEGER,
eta_high_seconds INTEGER,
eta_confidence TEXT NOT NULL DEFAULT 'none',
last_event_invocation TEXT,
last_event_sequence INTEGER NOT NULL DEFAULT -1,
last_work_sequence INTEGER NOT NULL DEFAULT -1
```

Advance `PRAGMA user_version` from Task 1 version `1` to timing/history version `2` in one `BEGIN IMMEDIATE` transaction. Version `0` runs version `0→1` scale migration first, then `1→2`; version `1` runs only timing/history migration; version `2` is a no-op. Legacy rows receive null `started_at`, `finished_at`, `last_heartbeat_at`, `last_progress_at`, phase/chunk/ETA fields; `progress_source='none'`, `eta_confidence='none'`, sequences `-1`, invocation null, and fingerprint remains `legacy:unknown`. Test all three entry versions and two consecutive `initialize()` calls.

Create:

```sql
CREATE TABLE IF NOT EXISTS job_phase_metrics (
    job_id TEXT NOT NULL,
    invocation TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    phase TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    completed_units INTEGER NOT NULL,
    total_units INTEGER NOT NULL,
    completed_unique_frames INTEGER NOT NULL,
    chunk_unique_frames INTEGER NOT NULL,
    chunk_context_frames INTEGER NOT NULL,
    total_unique_frames INTEGER NOT NULL,
    output_pixel_frames INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL,
    runtime_profile_fingerprint TEXT NOT NULL,
    valid_sample INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, invocation, chunk_index, phase),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
)
```

Create independent history with no foreign key and no job identifier:

```sql
CREATE TABLE IF NOT EXISTS performance_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_group TEXT NOT NULL,
    phase TEXT NOT NULL,
    seconds_per_unit REAL NOT NULL,
    workload_bucket INTEGER NOT NULL,
    runtime_profile_fingerprint TEXT NOT NULL,
    sample_date TEXT NOT NULL,
    CHECK (seconds_per_unit > 0)
)
```

`sample_group` is a fresh `secrets.token_hex(16)` generated only at successful completion so phase rows from one anonymous run can be grouped; it is not derived from or stored back on job. `sample_date` stores only `YYYY-MM-DD` UTC. Table contains no job ID, filename, path, source/target dimensions, original creation timestamp, or foreign key. `JobStore.complete(..., publish_performance=True)` inserts eligible full-invocation phase metrics into `performance_samples` and marks job completed in same transaction. JobService passes true only after final output validation when fingerprint is non-legacy and neither heartbeat nor measured progress is stale at completion; store additionally requires every inserted metric valid. All other terminal paths pass false or never call complete. Deleting job cascades only `job_phase_metrics`; never delete `performance_samples` during manual job deletion.

Enable `PRAGMA foreign_keys = ON` in every `_connect`. Add matching nullable fields to `Job` and public payload.

- [ ] **Step 4: Persist lifecycle and reports atomically**

`claim_next_queued` sets `started_at = now`. Terminal methods set `finished_at = now`. `mark_running` after preflight resets phase/chunk/event/ETA fields, sets invocation scope ready for `full`, and resets both event/work sequences to `-1` without resetting original `started_at`.

`record_report` transaction rules:

1. Load active job under `BEGIN IMMEDIATE`.
2. If `report.invocation` differs from stored invocation, accept it only for valid `preflight→full` transition and reset event/work sequence baselines. Within same invocation, reject event sequence not greater than `last_event_sequence`.
3. Preserve existing percent for heartbeat; otherwise store `min(99, max(existing, report.percent))`.
4. Set `last_heartbeat_at` for every accepted report. Set `last_progress_at=now`, `last_work_sequence`, and `progress_source='measured'` only when `report.measured_work is True` and `report.work_sequence > stored last_work_sequence`; fork reports copy these fields from event, while FFmpeg reports derive them from increasing `out_time_us`. Duplicate counters and heartbeats never refresh progress time.
5. Upsert `(job, invocation, chunk, phase)` using server timestamps and overlap-excluding unique-frame fields. `output_pixel_frames = target_width * target_height * chunk_unique_frames`; context frames never enter work normalization. Mark `valid_sample=1` only when phase completes, elapsed duration is positive, heartbeat/progress was not stale, and full job remains active.
6. `chunk_completed` closes current chunk metrics and advances frame-weighted progress; it does not create a synthetic phase sample.
7. Bound every stage to 64 characters and every counter to Task 3 limits.

Change JobService reporter to:

```python
report_progress = lambda report: self.store.record_report(job.id, report)
```

- [ ] **Step 5: Add dynamic elapsed and stale serialization**

Add:

```python
def public_job(self, job: Job, *, now: datetime | None = None) -> dict[str, object]:
    current = now or datetime.now(UTC)
    payload = job.public_dict()
    started = datetime.fromisoformat(job.started_at) if job.started_at else None
    finished = datetime.fromisoformat(job.finished_at) if job.finished_at else current
    payload["elapsed_seconds"] = max(0, int((finished - started).total_seconds())) if started else None
    heartbeat = datetime.fromisoformat(job.last_heartbeat_at) if job.last_heartbeat_at else None
    progress = datetime.fromisoformat(job.last_progress_at) if job.last_progress_at else None
    active = job.status in {"running", "preflight"}
    heartbeat_basis = heartbeat or started
    progress_basis = progress or started
    payload["heartbeat_stale"] = bool(
        active and heartbeat_basis
        and (current - heartbeat_basis).total_seconds() > self.settings.heartbeat_stale_seconds
    )
    payload["progress_stale"] = bool(
        active and progress_basis
        and (current - progress_basis).total_seconds() > self.settings.progress_stale_seconds
    )
    return payload
```

Use `jobs.public_job(...)` for create/get/list/cancel endpoints. Queued jobs have `elapsed_seconds=null`; active timer begins at claim. Configure `heartbeat_stale_seconds=120` and `progress_stale_seconds=300`. A current heartbeat plus stale progress means alive-but-not-advancing; stale heartbeat means reporting channel itself is stale. Hide ETA for either stale state, but never cancel solely for staleness.

- [ ] **Step 6: Run tests and commit**

Run: `cd backend && uv run --group dev pytest tests/test_jobs.py tests/test_progress.py -v`

Expected: PASS.

```bash
git add backend/app/domain.py backend/app/job_store.py backend/app/job_service.py backend/app/main.py backend/tests/test_jobs.py
git commit -m "feat: persist SeedVR2 phase timing"
```

### Task 6: Honest ETA Estimator and Historical Calibration

**Files:**
- Create: `backend/app/eta.py`
- Create: `backend/tests/test_eta.py`
- Modify: `backend/app/domain.py`
- Modify: `backend/app/job_store.py`
- Modify: `backend/app/job_service.py`
- Test: `backend/tests/test_jobs.py`

**Interfaces:**
- Consumes: current job's valid full-invocation `job_phase_metrics`, durable anonymized `performance_samples`, active phase counters, runtime fingerprint including cache state, output pixel-frame workload, and configured deadline.
- Produces: `PhaseSample`, `EtaEstimate`, `workload_bucket(pixel_frames: int) -> int`, `estimate_eta(active: ActiveWork, samples: list[PhaseSample], deadline_seconds: int) -> EtaEstimate`, and persisted `eta_low_seconds`, `eta_high_seconds`, `eta_confidence`, `progress_source`.

- [ ] **Step 1: Write failing pure estimator tests**

Create `backend/tests/test_eta.py` covering exact policies:

```python
from app.eta import ActiveWork, PhaseSample, estimate_eta, workload_bucket


def sample(sample_group, phase, seconds, units=10, fingerprint="same", pixels=8_294_400):
    return PhaseSample(sample_group, phase, seconds, units, fingerprint, workload_bucket(pixels), True)


def test_no_measured_unit_and_no_history_is_calibrating():
    result = estimate_eta(ActiveWork("encoding", 0, 10, "same", workload_bucket(8_294_400), 0), [], 86_400)
    assert result == EtaEstimate(None, None, "none", "none")


def test_one_comparable_run_returns_wide_low_confidence_range():
    samples = [sample("old", phase, seconds) for phase, seconds in {
        "encoding": 100, "upscaling": 300, "decoding": 500, "postprocessing": 100,
    }.items()]
    result = estimate_eta(ActiveWork("encoding", 2, 10, "same", workload_bucket(8_294_400), 20), samples, 86_400)
    assert result.confidence == "low"
    assert result.source == "historical"
    assert result.low_seconds < result.high_seconds


def test_three_matching_runs_plus_stable_current_rate_is_medium_confidence():
    samples = [sample(f"job-{index}", phase, seconds + index) for index in range(3) for phase, seconds in {
        "encoding": 100, "upscaling": 300, "decoding": 500, "postprocessing": 100,
    }.items()]
    result = estimate_eta(ActiveWork("encoding", 5, 10, "same", workload_bucket(8_294_400), 50), samples, 86_400)
    assert result.confidence == "medium"


def test_mismatched_failed_and_extreme_samples_do_not_change_range():
    baseline = [sample(f"ok-{index}", "decoding", 100 + index) for index in range(3)]
    noisy = baseline + [
        PhaseSample("wrong", "decoding", 1, 10, "other", workload_bucket(8_294_400), True),
        PhaseSample("failed", "decoding", 1, 10, "same", workload_bucket(8_294_400), False),
        sample("outlier", "decoding", 100_000),
    ]
    active = ActiveWork("decoding", 2, 10, "same", workload_bucket(8_294_400), 20)
    assert estimate_eta(active, noisy, 86_400) == estimate_eta(active, baseline, 86_400)


def test_eta_range_is_finite_ordered_and_clamped_to_deadline():
    result = estimate_eta(active, huge_samples, 300)
    assert 0 <= result.low_seconds <= result.high_seconds <= 300
```

- [ ] **Step 2: Run estimator tests and verify failure**

Run: `cd backend && uv run --group dev pytest tests/test_eta.py -v`

Expected: import failure because `app.eta` does not exist.

- [ ] **Step 3: Implement deterministic estimator**

Create exact value types:

```python
@dataclass(frozen=True)
class PhaseSample:
    sample_group: str
    phase: str
    elapsed_seconds: float
    completed_units: int
    runtime_profile_fingerprint: str
    workload_bucket: int
    valid: bool

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
```

Implement workload bucket as `max(0, int(math.log2(max(1, pixel_frames))))`. Filter samples to `valid`, exact same runtime fingerprint including `dit_cache=disabled:vae_cache=disabled`, same bucket within `±1`, positive finite seconds/units, and known phase. Reject a rate outside `[median/5, median*5]` when at least three rates exist. Use median seconds/unit for central rate and nearest-rank 25th/75th percentiles for bounds. Count distinct anonymized `sample_group` values for confidence; never require a deleted job row.

Remaining work includes current phase remainder plus every later phase in `("encoding", "upscaling", "decoding", "postprocessing")`. A completed phase sample from an earlier chunk of current job counts before historical jobs. If any remaining phase lacks a rate, return calibrating. Widen one/two-job history to `0.6x–1.6x`; widen three-plus to at least `0.8x–1.2x`; high confidence requires five comparable jobs, current-unit coefficient of variation at most `0.10`, and final range width at most `20%` of midpoint. Clamp both bounds to remaining process deadline and force `high >= low`.

- [ ] **Step 4: Query only comparable valid history and refresh ETA after reports**

Add `JobStore.eta_samples(job_id)` that reads completed earlier chunks for current job from `job_phase_metrics` and reads historical rows directly from `performance_samples`. It must not join historical samples back to jobs. Exclude current invalid/preflight metrics, mismatched fingerprints/buckets, and invalid numeric values. Use synthetic in-memory sample group `current-run` only for current job metrics.

After every accepted phase/chunk report, JobService builds `ActiveWork`, calls `estimate_eta`, and persists estimate through:

```python
def update_eta(self, job_id: str, estimate: EtaEstimate) -> None:
    ...
```

When estimate is `none`, store null bounds, confidence `none`, and keep `progress_source='measured'` if counters exist. On stale heartbeat, public response hides stored ETA bounds and returns confidence `none`; stored history remains but is not marked valid.

- [ ] **Step 5: Add API integration tests for calibrating and learned history**

First active job with no samples must expose:

```python
assert payload["eta_low_seconds"] is None
assert payload["eta_high_seconds"] is None
assert payload["eta_confidence"] == "none"
assert payload["progress_source"] == "measured"
```

Seed three anonymized matching `performance_samples` groups, report current phase units, then assert integer ordered bounds and `eta_confidence == "medium"`. Delete every source job before query and assert estimate stays identical. Seed same workload under another preset/scale/cache fingerprint and assert bounds do not change.

- [ ] **Step 6: Run tests and commit**

Run: `cd backend && uv run --group dev pytest tests/test_eta.py tests/test_jobs.py -v`

Expected: PASS.

```bash
git add backend/app/eta.py backend/app/domain.py backend/app/job_store.py backend/app/job_service.py backend/tests/test_eta.py backend/tests/test_jobs.py
git commit -m "feat: estimate honest SeedVR2 ETA ranges"
```

### Task 7: Frontend Scale, Measured Progress, Elapsed Time, and ETA UI

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/__tests__/api.test.ts`
- Modify: `frontend/src/__tests__/App.test.tsx`
- Modify: `frontend/src/__tests__/styles.test.ts`

**Interfaces:**
- Consumes: additive config/job API from Tasks 1, 5, and 6.
- Produces: `OutputScale`, scale selector, optional browser metadata preview, measured/indeterminate progress rendering, elapsed clock, ETA range/confidence, and stale warning.

- [ ] **Step 1: Extend TypeScript API types and failing multipart test**

Add:

```typescript
export const outputScales = [0.25, 0.5, 1, 2] as const;
export type OutputScale = (typeof outputScales)[number];
export type EtaConfidence = "none" | "low" | "medium" | "high";
export type ProgressSource = "none" | "measured" | "historical";
```

Extend `Job` with scale/target/timing/progress fields from backend, all timing/counters nullable except `output_scale`, dimensions, confidence, source, and stale boolean. Extend `RuntimeConfig` with:

```typescript
default_output_scale: OutputScale;
output_scales: Array<{ value: OutputScale; label: string; description: string }>;
```

Change API signature:

```typescript
createJob(video, preset, colorCorrection, outputScale, callbacks)
```

In API test, inspect sent `FormData` and assert `body.get("output_scale") === "0.5"`.

- [ ] **Step 2: Run API test and verify failure**

Run: `cd frontend && npm run test:exhaustive -- src/__tests__/api.test.ts`

Expected: FAIL because scale argument/form field does not exist.

- [ ] **Step 3: Submit selected scale and use server config**

Add `const [outputScale, setOutputScale] = useState<OutputScale>(1);`. On config load, validate against `outputScales` and set default. Render config-provided labels/descriptions in an accessible radio fieldset named `Output resolution`. Pass scale as fourth `createJob` argument and callbacks fifth.

Use exact UI copy:

```text
1x Original — Original dimensions; full generative restoration.
0.5x Balanced — Half width and height; generative restoration with fewer output pixels.
0.25x Fast — Quarter width and height; experimental generative restoration.
2x Upscale — Double width and height; highest processing cost.
```

Replace fixed `2×` labels in job/result cards and footnote with `formatScale(job.output_scale)`.

- [ ] **Step 4: Add optional local dimension preview test and implementation**

Test with mocked `HTMLVideoElement.videoWidth=1920`, `videoHeight=1080`, and `loadedmetadata`; select `0.5x` and assert `Expected output: 960 × 540`. If metadata cannot load, omit preview; backend validation remains authoritative. Revoke every created object URL on file replacement/unmount.

- [ ] **Step 5: Write failing measured progress and timer tests**

Use fake timers and a running job:

```typescript
{
  progress: 38, progress_source: "measured", stage: "decoding",
  phase_name: "decoding", phase_current: 5, phase_total: 10,
  chunk_current: 2, chunk_total: 4, started_at: "2026-08-12T06:00:00Z",
  finished_at: null, elapsed_seconds: 120, eta_low_seconds: 2700,
  eta_high_seconds: 3600, eta_confidence: "medium", last_heartbeat_at: "2026-08-12T06:02:00Z",
  last_progress_at: "2026-08-12T06:01:55Z", heartbeat_stale: false, progress_stale: false,
}
```

Assert visible `38%`, `Decoding · 5/10 · chunk 2/4`, `Elapsed 2m 00s`, `ETA 45–60 min`, and `Medium confidence`. Advance local clock five seconds without API response; assert `Elapsed 2m 05s`.

Add cases:

- no bounds: `Calibrating…` and no invented percentage when source is `none`;
- heartbeat stale: `Progress signal stale — processing may still be active` and no ETA;
- heartbeat current but progress stale: `Process is alive, but measured work has not advanced` and no ETA;
- legacy missing optional timing fields: renders active card without throwing;
- completed: exact `100%` only from backend completed state.

- [ ] **Step 6: Implement display formatters and truthful states**

Add pure helpers in `App.tsx`:

```typescript
function formatDuration(seconds: number): string
function formatEtaRange(low: number, high: number): string
function formatScale(scale: number): string
```

Use a one-second interval only while active jobs have `started_at`; derive displayed elapsed from server `elapsed_seconds + (Date.now() - lastPollReceivedAt)/1000`, preventing browser/client clock-zone drift. Reset baseline each successful job poll.

Measured progressbar requires `progress_source === "measured"`, finite percent, and neither `heartbeat_stale` nor `progress_stale`. Otherwise render indeterminate bar with `aria-valuetext="Calibrating progress"` or correct stale warning. `last_heartbeat_at` labels reporting liveness; `last_progress_at` labels last measured-work advance and must not be replaced by heartbeat time. Render phase/chunk counters only when totals are positive. Capitalize confidence label. Never compute ETA from percentage in browser.

- [ ] **Step 7: Add responsive and accessible styles**

Add `.scale-fieldset`, `.scale-option`, `.job-timing`, `.eta-confidence`, and `.progress-warning` styles. In `styles.test.ts`, assert the mobile media query keeps scale cards one column and `.progress-warning` does not use color alone (`border` plus text). Ensure fieldset legend, progressbar accessible value, and warning `role="status"` are present.

- [ ] **Step 8: Run frontend tests/build and commit**

Run: `cd frontend && npm run test:exhaustive`

Expected: PASS.

Run: `cd frontend && npm run build`

Expected: TypeScript build and Vite production build PASS.

```bash
git add frontend/src/types.ts frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/src/__tests__/api.test.ts frontend/src/__tests__/App.test.tsx frontend/src/__tests__/styles.test.ts
git commit -m "feat: show restoration scale and ETA"
```

### Task 8: Fork Pin, Provenance, and Active-Job-Safe Runtime Update

**Files:**
- Create: `scripts/runtime-update-gate.py`
- Modify: `scripts/install-runtime.sh:1-210,272-281`
- Modify: `backend/tests/test_install_security.py`
- Modify: `deploy/runtime.env.example`

**Interfaces:**
- Consumes: completed fork dependency's exact `fork_revision` and fork repository/branch.
- Produces: runtime origin `https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git`, remote `upstream` pointing to `https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git`, literal exact revision pin, SQLite-locked queue gate, exact LaunchAgent quiescence, post-stop recheck, and pre-restart recheck.

- [ ] **Step 1: Write failing static installer security tests**

Add:

```python
def test_runtime_pins_reviewed_seedvr2_fork_and_upstream_provenance():
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()
    assert "https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git" in script
    assert "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git" in script
    assert "remote get-url upstream" in script
    assert '[[ "$actual_upstream" == "$expected_upstream" ]]' in script


def test_runtime_update_quiesces_before_any_apply_mutation():
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()
    gate_script = (PROJECT_ROOT / "scripts/runtime-update-gate.py").read_text()
    gate = script.index("runtime-update-gate.py")
    first_mutation = min(
        script.index('run mkdir -p'),
        script.index("uv venv --clear"),
        script.index("fetch --depth=1"),
    )
    assert gate < first_mutation
    assert 'BLOCKING_STATUSES = ("queued", "preflight", "running")' in gate_script
    assert script.index("--check-only") < script.index("launchctl bootstrap")
```

Create `backend/tests/test_runtime_update_gate.py`. Import helper by file path. Parameterize `queued`, `preflight`, and `running`; create one row, call `quiesce`, assert return `75`, exact LaunchAgent was not stopped, and no DB/runtime file changed. Empty DB test asserts argument arrays call `launchctl print gui/<uid>/com.haohanl.video-upscale-webui`, then `launchctl bootout` for same exact domain, then performs second blocking-state query while SQLite `BEGIN IMMEDIATE` remains open. Mock second query to return a queued row and assert helper returns `76` before installer can mutate.

- [ ] **Step 2: Run tests and verify failure**

Run: `cd backend && uv run --group dev pytest tests/test_install_security.py -v`

Expected: FAIL because installer still pins upstream origin and lacks upstream remote/active-job gate.

- [ ] **Step 3: Insert exact reviewed fork revision from dependency**

Before editing, verify dependency output without printing credentials:

```zsh
fork_revision="$(gh api repos/haohlin/ComfyUI-SeedVR2_VideoUpscaler/git/ref/heads/feature/cli-progress-events --jq '.object.sha')"
[[ "$fork_revision" == [0-9a-f]## ]] && (( ${#fork_revision} == 40 )) || exit 1
git ls-remote https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git "$fork_revision" | grep -q "$fork_revision" || exit 1
```

Replace `SEEDVR2_NODE_REVISION` value with command's literal 40-character result during execution. Do not resolve branch head inside installer. Replace checkout repository with fork URL. Extend `checkout_pinned_revision(repository, destination, revision, expected_upstream="")`; for SeedVR2 add/verify remote `upstream` exactly. Keep clean-worktree, no-submodule, and ignored-Python checks.

- [ ] **Step 4: Atomically quiesce exact LaunchAgent and recheck queue before any apply mutation**

Create `scripts/runtime-update-gate.py` with CLI:

```text
runtime-update-gate.py --database PATH --launchctl PATH --domain DOMAIN [--check-only]
```

Core implementation uses no shell:

```python
BLOCKING_STATUSES = ("queued", "preflight", "running")


def blocking_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?, ?)",
        BLOCKING_STATUSES,
    ).fetchone()[0])


def quiesce(database: Path, launchctl: str, domain: str, check_only: bool) -> int:
    if not database.exists():
        return 0
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if blocking_count(connection):
            return 75
        if not check_only:
            loaded = subprocess.run(
                [launchctl, "print", domain],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if loaded:
                subprocess.run([launchctl, "bootout", domain], check=True)
            if blocking_count(connection):
                return 76
        connection.commit()
    return 0
```

Installer records whether exact domain was loaded, then invokes helper for every `--apply` before `mkdir`, token generation, venv recreation, Git fetch/checkout, dependency reinstall, config write, or model download. Exit `75` means `die "refusing runtime update while a queued or active job exists"`; exit `76` means `die "job appeared while stopping service; runtime unchanged"`. Holding `BEGIN IMMEDIATE` blocks new queue insertion during `bootout`; second query proves no queued/preflight/running state after stop. Missing database means zero jobs.

On successful install, invoke helper again with `--check-only`; only then, if service was previously loaded, run argument-array-equivalent command:

```zsh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.haohanl.video-upscale-webui.plist"
```

Do not use `kickstart` on a stopped/unloaded agent. Remove old model-install automatic restart block. If install fails after quiescence, leave service stopped and report recovery command; never restart a partially updated runtime automatically.

- [ ] **Step 5: Document new settings in runtime example**

Add exact values:

```zsh
VIDEO_UPSCALE_DEFAULT_OUTPUT_SCALE="1"
VIDEO_UPSCALE_DEVICE_BACKEND_CLASS="apple-mps"
VIDEO_UPSCALE_HEARTBEAT_STALE_SECONDS="120"
VIDEO_UPSCALE_PROGRESS_STALE_SECONDS="300"
```

State fork runtime is pinned, external, and updated only through reviewed `--update` after queue becomes terminal.

- [ ] **Step 6: Run dry-run/security tests and commit**

Run: `cd backend && uv run --group dev pytest tests/test_install_security.py tests/test_runtime_update_gate.py -v`

Expected: PASS.

Run: `scripts/install-runtime.sh --dry-run --update`

Expected: prints fork fetch/verification operations; changes no runtime files and restarts no service.

```bash
git add scripts/runtime-update-gate.py scripts/install-runtime.sh backend/tests/test_install_security.py backend/tests/test_runtime_update_gate.py deploy/runtime.env.example
git commit -m "chore: pin reviewed SeedVR2 progress fork"
```

### Task 9: Documentation, Curated Release Verification, Security Scan, and Deferred Deployment

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/runtime.md`
- Test: count-bounded cross-repository release manifest

**Interfaces:**
- Consumes: Tasks 1-8 and exact fork commit.
- Produces: operator docs, verified release candidate, security-scan evidence, and a deployment checkpoint that cannot interrupt active processing.

- [ ] **Step 1: Update operator and architecture documentation**

Document all four scale labels and pixel-cost meaning; explain downscaled modes remain generative but produce smaller frames. Replace old fixed `2x` and automatic work-file cleanup claims. Document additive job fields, `Calibrating…`, ETA confidence, stale heartbeat semantics, local-only history, and why ETA is unavailable before evidence exists.

In `docs/runtime.md`, record exact fork origin, upstream remote, feature branch, literal pinned revision, fork update review procedure, `--dry-run --update`, and terminal-job gate. Keep loopback/Tailscale/Funnel/auth requirements unchanged.

- [ ] **Step 2: Run curated cross-repository release verification**

Run: `cd backend && uv sync --locked && cd .. && scripts/test-release.sh`

Expected before Task 8: exactly 47 tests PASS: 27 backend, 10 frontend, and 10 SeedVR2 fork, leaving two reserved slots for exact active-job installer safety nodes. Gate fails before execution for zero tests, more than 49 tests, duplicate names, or missing exact test names. It covers scale admission and target sizing, progress schema and real fork bridge, output validation, nonzero exit handling and cancellation, ETA/calibration/stale/timing persistence, frontend scale/progress/ETA/API behavior, fork lifecycle, and all seven security regression boundaries. Task 8 may fill the two reserved slots without raising the ceiling.

- [ ] **Step 3: Build frontend**

Run: `cd frontend && npm ci --ignore-scripts && npm run build`

Expected: TypeScript and Vite production build PASS. Full pytest, Vitest, and unittest discovery remain opt-in diagnostics, not normal completion or deployment gates.

- [ ] **Step 4: Run repository system and dry-run checks**

Run: `scripts/check-system.sh`

Expected: host prerequisites PASS; runtime-required checks may remain skipped until deployment.

Run: `scripts/install-runtime.sh --dry-run --update`

Expected: no writes, no restart, exact fork revision displayed in planned checkout operation.

- [ ] **Step 5: Run required security scans before public push**

Invoke `codex-security:security-scan` first against fork feature-branch diff and then against WebUI diff. Required result for each repository: zero unresolved critical/high findings; validate any lower finding before disposition. Fix validated findings with failing regression tests, rerun affected suite, and rerun scan. Never include credentials, local absolute runtime paths, tokens, input filenames, or model paths in scan artifacts or public Git history.

- [ ] **Step 6: Commit documentation and verified code state**

```bash
git add README.md docs/architecture.md docs/runtime.md
git commit -m "docs: explain restoration progress and ETA"
```

Run: `git status --short`

Expected: clean worktree.

- [ ] **Step 7: Push only after both scans pass**

Run: `git push origin main`

Expected: WebUI commits published; fork branch was already published by fork dependency plan. Do not deploy in this step.

- [ ] **Step 8: Wait for current processing to become terminal without signaling it**

Read job API/SQLite and process status only. Do not run `launchctl bootout`, `launchctl kickstart`, `kill`, `pkill`, `stop-local.sh`, runtime checkout, or venv update while any job is `queued`, `running`, or `preflight`.

Expected terminal states: `completed`, `failed`, or `cancelled` through existing job lifecycle.

- [ ] **Step 9: Deploy pinned runtime and run private smoke jobs**

After zero active jobs:

```zsh
scripts/install-runtime.sh --dry-run --update
scripts/install-runtime.sh --apply --update
scripts/check-system.sh --require-runtime
curl --fail --silent http://127.0.0.1:8000/api/health
tailscale serve status
```

Expected: installer atomically verifies empty queued/preflight/running set, stops exact LaunchAgent, rechecks DB, installs fork, rechecks DB, and bootstraps previously loaded LaunchAgent. Fork origin/upstream/revision verified; health `{"status":"ok","runner":"ready"}`; FastAPI remains loopback-only, Tailscale Serve remains private, Funnel absent.

Submit short low-resolution `1x` smoke job. Verify model-preparation stage, phase/chunk counters, elapsed timer, initial `Calibrating…`, eventual ETA range when evidence becomes sufficient, validated MP4 dimensions, source audio, persistent download, and manual deletion. Repeat `0.5x`; optionally run `0.25x` only with source shortest edge at least `1024`; run short `2x` regression last.

Expected: no percentage reaches `100` before output validation; stale warning does not appear while heartbeat remains current; results persist until operator deletes them.

## Self-Review

- Spec coverage: bounded output scales, fork dependency, invocation-scoped structured progress, overlap-excluding frame aggregation, separate heartbeat/progress freshness, FFmpeg finalization progress, elapsed timing, deletion-independent anonymized history, ordered legacy migrations, cache-aware fingerprints, API, frontend, atomic runtime quiescence, privacy, testing, security scan, and deferred deployment each map to Tasks 1-9.
- Placeholder scan: no `TBD`, `TODO`, generic error-handling instruction, or undefined follow-on task remains. Fork revision is a typed dependency output and Task 8 gives exact retrieval/verification commands; installer still receives a literal immutable SHA.
- Type consistency: `output_scale` uses backend `float` and frontend `OutputScale`; fork key remains `event_type`; `ProgressReport` carries invocation/work advancement from runner to store; unique/context frame counters remain bounded integers; ETA fields remain nullable integers with four confidence values; heartbeat/progress stale states are separate server-derived booleans; durable samples use anonymous `sample_group`, never job ID.
