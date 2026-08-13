# SeedVR2 MPS Streaming Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse SeedVR2 DiT and VAE models across streaming chunks on Apple MPS and remove misleading CUDA optimization advice.

**Architecture:** Keep current CLI-per-job architecture. Enable official per-process cache flags at the adapter boundary and record this exact runtime configuration in ETA fingerprints. Normalize only the known generic optimization banner on Darwin.

**Tech Stack:** Python 3.13, FastAPI, SeedVR2 CLI, PyTorch MPS, pytest, zsh LaunchAgent.

## Global Constraints

- Work directly on `main`; no worktree.
- Do not install SageAttention, Flash Attention, or Triton on macOS.
- Keep `chunk_size=25`, VAE tiling, and SDPA.
- Keep model files under existing application-support model directory.
- Run no more than 49 tests in the release gate.
- Never restart while a job is active.

---

### Task 1: Enable official streaming model cache

**Files:**
- Modify: `backend/tests/test_seedvr2_adapter.py`
- Modify: `scripts/seedvr2-adapter.py`
- Modify: `backend/tests/test_jobs.py`
- Modify: `backend/app/job_service.py`

**Interfaces:**
- Consumes: `build_seedvr2_command(...) -> list[str]`
- Produces: command containing `--cache_dit` and `--cache_vae`; fingerprint ending in `dit_cache=enabled:vae_cache=enabled`

- [ ] Add assertions for both official CLI flags and enabled fingerprint.
- [ ] Run exact tests and verify RED because flags/fingerprint are disabled.
- [ ] Add both flags after streaming options and change fingerprint literals.
- [ ] Run exact tests and focused adapter/job selections; verify GREEN.
- [ ] Commit production and tests.

### Task 2: Make MPS optimization status truthful

**Files:**
- Modify: `backend/tests/test_seedvr2_adapter.py`
- Modify: `scripts/seedvr2-adapter.py`

**Interfaces:**
- Consumes: human SeedVR2 stdout lines in `forward_seedvr2_line(line: str)`
- Produces: CUDA package suggestion suppressed on Darwin; one accurate `Apple MPS uses PyTorch SDPA` line retained in visible log

- [ ] Add a Darwin regression test for exact generic warning lines.
- [ ] Run it and verify RED because CUDA installation advice is printed.
- [ ] Add narrow exact-line suppression/replacement without affecting JSON events or other human logs.
- [ ] Run focused adapter tests; verify GREEN.
- [ ] Commit production and test.

### Task 3: Verify, deploy, and requeue preserved media

**Files:**
- Modify only if exact release-test names changed: `scripts/release-tests.toml`

**Interfaces:**
- Consumes: preserved cancelled input and deployed loopback API
- Produces: completed five-frame MPS smoke and a new queued/running job for the preserved source

- [ ] Run `scripts/test-release.sh`; require exactly 49 passing tests.
- [ ] Build frontend and verify clean Git state.
- [ ] Confirm SQLite has no queued, preflight, or running job.
- [ ] Restart exact LaunchAgent and verify loopback plus Tailscale health.
- [ ] Submit a five-frame 320×256 1x smoke; require completed valid MP4.
- [ ] Requeue preserved 4K source through loopback resumable API.
- [ ] Verify one preparation lifecycle and measurable progress beyond initial preparation.
- [ ] Push `main` only after verification.

