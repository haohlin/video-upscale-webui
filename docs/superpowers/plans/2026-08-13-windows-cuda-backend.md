# Windows CUDA Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a complete Video Upscale backend in WSL2 on RTX 4090 and let the existing Mac-hosted WebUI select, monitor, and control either independent backend.

**Architecture:** Mac remains default WebUI origin and one complete MPS backend. Windows WSL2 runs the same FastAPI application with CUDA-specific settings and persistent storage/models. Browser talks directly to each Tailscale Serve endpoint; frontend attaches a stable backend ID to every upload and job operation.

**Tech Stack:** FastAPI, SQLite, React/Vite/TypeScript, WSL2 systemd, PyTorch CUDA, FFmpeg, SeedVR2 standalone CLI, Tailscale Serve.

## Global Constraints

- Keep current frontend layout; add only compact host selector, health state, and host badges.
- Each backend owns its uploads, jobs, logs, results, SQLite database, and model cache.
- Auto prefers healthy Windows and falls back to Mac only for new uploads.
- Never silently migrate a running or resumable job between backends.
- Keep model files persistent; never download models per job.
- Keep both APIs tailnet-only with Funnel disabled and exact Tailscale identity checks.
- Never commit private Windows hostnames, paths, credentials, or operator identity.
- Keep curated release gate between 1 and 49 tests; no submission stress test.
- Work directly on `feature/windows-cuda-backend`; do not create a worktree.

---

### Task 1: Backend identity, capabilities, and exact-origin CORS

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/domain.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/job_service.py`
- Test: `backend/tests/test_health.py`
- Test: `backend/tests/test_jobs.py`

**Interfaces:**
- Produces `Settings.backend_id`, `backend_display_name`, `platform_name`, `accelerator_name`, `allowed_web_origin`, and platform-selected preset IDs.
- Produces additive `/api/health` fields `backend_id`, `display_name`, `platform`, `accelerator`, `state`, and `presets`.
- Preserves existing health and `/api/config` fields for one-backend clients.

- [ ] Write failing tests proving Mac and CUDA capability descriptors, CUDA presets, exact allowed-origin preflight, rejection of other origins, and unchanged Tailscale identity enforcement.
- [ ] Run only those test nodes and confirm expected failures.
- [ ] Add validated environment settings. Accept backend IDs matching `^[a-z0-9][a-z0-9-]{0,31}$`; require HTTPS origin without path/query when configured.
- [ ] Build preset allowlist from platform: Mac exposes `3b-safe` plus experimental 7B; CUDA exposes `7b-fp8-quality` and `3b-fp8-fast`.
- [ ] Add FastAPI `CORSMiddleware` only when one exact origin is configured; allow `GET,POST,PUT,DELETE`, `Content-Type`, `Upload-Offset`, and `X-Video-Upscale-Request`; never wildcard origins.
- [ ] Return additive capability metadata and use selected preset allowlist during job creation.
- [ ] Run focused tests and commit `feat: describe independent processing backends`.

### Task 2: CUDA runner profile and persistent SeedVR2 models

**Files:**
- Modify: `scripts/seedvr2-adapter.py`
- Modify: `backend/app/job_service.py`
- Modify: `backend/app/runner.py`
- Test: `backend/tests/test_seedvr2_adapter.py`
- Test: `backend/tests/test_runner.py`
- Test: `backend/tests/test_jobs.py`

**Interfaces:**
- Maps `7b-fp8-quality` to configured 7B FP8 model and `3b-fp8-fast` to configured 3B FP8 model.
- Consumes `VIDEO_UPSCALE_DEVICE_BACKEND_CLASS=nvidia-cuda` and fixed environment-derived CUDA profile values.
- Keeps existing structured progress and cancellation contract unchanged.

- [ ] Write failing tests for CUDA preset/model mapping, CUDA device selection, memory-preserving 7B arguments, model-cache arguments, stable runtime fingerprint, and unchanged MPS commands.
- [ ] Run selected tests and confirm failures.
- [ ] Make adapter choose platform profile without shell interpolation. CUDA baseline uses device 0, SDPA, DiT/VAE caches, temporal overlap 4, bounded tiling, and memory preservation for 7B.
- [ ] Keep model directory external and persistent. Missing models produce preparation state and one controlled download through reviewed SeedVR2 logic; completed models are reused.
- [ ] Keep process group cancellation, output validation, deadlines, and progress bridge unchanged.
- [ ] Run focused tests and commit `feat: add RTX 4090 SeedVR2 profiles`.

### Task 3: Multi-backend API client and resource affinity

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `frontend/src/__tests__/api.test.ts`
- Test: `frontend/src/__tests__/App.test.tsx`

**Interfaces:**
- Defines `BackendDescriptor { id, display_name, api_base_url, preference }` and `Owned<T> = T & { backend_id, backend_display_name }`.
- Defines `createApiClient(descriptor)` whose job/upload/log/download methods always use descriptor URL.
- Reads initial backend descriptors from Mac `/api/backends`; current-origin Mac descriptor remains fallback.

- [ ] Write failing API tests proving encoded routing to two API roots and no cross-backend cancel/delete/log/download.
- [ ] Write failing UI tests proving Auto preference, explicit Mac selection, Windows-offline fallback, host badges, merged lists, and persisted upload affinity after refresh.
- [ ] Run selected frontend tests and confirm failures.
- [ ] Refactor API functions behind `createApiClient`; preserve request headers and resumable retry behavior.
- [ ] Aggregate health/jobs/uploads with `Promise.allSettled`, keeping one offline backend from hiding the other.
- [ ] Store `{backend_id, upload_id}` for pending local resume state. Route every resource action by owning backend.
- [ ] Add compact selector and badges using existing visual language. Do not reorganize upload, queue, progress, or debug sections.
- [ ] Run selected tests/build and commit `feat: select Mac or Windows processing backend`.

### Task 4: Mac backend registry endpoint

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/main.py`
- Modify: `deploy/runtime.env.example`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Consumes `VIDEO_UPSCALE_BACKENDS_JSON`, a bounded JSON array supplied only by private runtime configuration.
- Produces authenticated `GET /api/backends` containing current-origin Mac plus configured Windows descriptor.

- [ ] Write failing tests for empty fallback, valid two-backend registry, duplicate ID rejection, non-HTTPS remote URL rejection, and bounded descriptor count/string lengths.
- [ ] Run selected tests and confirm failures.
- [ ] Parse at startup; cap at four descriptors, validate stable IDs and HTTPS URLs, reject userinfo/query/fragment, and never log raw private URLs.
- [ ] Return descriptors without credentials or operator identity.
- [ ] Run focused tests and commit `feat: publish private backend registry`.

### Task 5: WSL2 installation and private remote workflow

**Files:**
- Create: `deploy/video-upscale-webui.service`
- Create: `scripts/install-wsl-runtime.sh`
- Create: `scripts/start-wsl.sh`
- Create: `scripts/check-cuda-system.sh`
- Modify: `README.md`
- Test: `backend/tests/test_install_wsl.py`
- Machine-local only: `~/.local/bin/video-upscale-remote`
- Machine-local only: `~/.config/video-upscale-webui/remote.env`

**Interfaces:**
- `video-upscale-remote status|clone <branch>|sync <branch>|run <AllowlistedAction>` loads private host and WSL settings without printing them.
- Allowlisted actions: `Preflight`, `Install`, `Test`, `Start`, `Stop`, `Health`, `Smoke3B`, `Smoke7B`, `Logs`.
- Installer creates persistent data/runtime/model roots and a systemd service bound to WSL loopback.

- [ ] Write shell/static tests proving branch validation, exact-SHA sync, clean-tree checks, no embedded host/path, loopback binding, persistent model directory, active-job update refusal, and action allowlist.
- [ ] Run selected tests and confirm failures.
- [ ] Add idempotent WSL installer using Ubuntu packages, pinned Python environment, pinned SeedVR2 fork revision, CUDA PyTorch, FFmpeg, and systemd. Never clear model/data directories during updates.
- [ ] Add CUDA preflight checking WSL GPU visibility, 24GB-class VRAM, PyTorch CUDA, FFmpeg, disk, branch, and exact commit.
- [ ] Add machine-local wrapper following `h3vr-remote` quoting, clean sync, private config, and bounded-output patterns. Reuse existing configured Windows host; do not rediscover or expose it.
- [ ] Run focused tests and commit `feat: install Windows CUDA backend`.

### Task 6: Remote clone, tune, deploy, and real proof

**Files:**
- Modify only if measurements require bounded profile changes from Task 2.
- Private runtime configuration only for endpoint, origin, host, WSL root, and operator login.

**Interfaces:**
- Uses Task 5 wrapper only; no ad-hoc remote shell commands.
- Produces deployed Windows health/capability response and persistent models.

- [ ] Push `feature/windows-cuda-backend`, clone it into configured WSL2 development root, and verify local HEAD equals `origin` and Windows HEAD.
- [ ] Run remote `Preflight`; verify RTX 4090, CUDA, WSL loopback reachability from Windows, Tailscale connected, and Funnel absent.
- [ ] Run remote `Install`; prepare 3B FP8, 7B FP8, and VAE models once in persistent model storage.
- [ ] Run capped Windows unit/release selection, then a generated short low-resolution 3B job.
- [ ] Run same short input with 7B FP8. Record peak VRAM, wall time, chosen batch/chunk/tiling, result validity, and model reuse on second invocation.
- [ ] Enable only benchmark-proven CUDA optimizations; rerun one short comparison after each candidate and retain stable winners.
- [ ] Configure Windows Tailscale Serve to the loopback service, exact Mac origin CORS, Funnel disabled, and operator identity.
- [ ] Configure Mac private backend registry, rebuild/restart Mac WebUI, and verify one browser upload routes directly to Windows.
- [ ] Stop Windows service temporarily and prove Auto selects Mac for a new test job; restore Windows and verify both histories remain visible.
- [ ] Run `scripts/test-release.sh`, capped at 49, frontend build, diff check, exact deployed SHA checks, then commit any measured profile adjustment.

