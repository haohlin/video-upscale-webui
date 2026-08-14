# Live Backend Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show only usable processing hosts, expose two-second machine telemetry, route Windows through WSL Tailscale Serve, and make Windows 7B Quality direct-run by default.

**Architecture:** Extend the existing health contract with dependency-free host metrics. Keep readiness aggregation in the React client and keep deployment topology private in runtime configuration.

**Tech Stack:** FastAPI, Python standard library, React, TypeScript, Vitest, pytest, Tailscale Serve, WSL2, CUDA.

## Global Constraints

- Do not add a monitoring dependency or WebSocket service.
- Poll at 2 seconds and tolerate individual metric failures.
- Keep exhaustive suites optional; run the capped release gate.
- Do not redownload persistent SeedVR2 models.

---

### Task 1: Metrics health contract

**Files:**
- Create: `backend/app/system_metrics.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Produces: `SystemMetrics.snapshot() -> dict[str, float | int | str | None]`
- Produces: `/api/health.metrics`

- [ ] Add a failing health test with an injected metrics provider.
- [ ] Implement bounded Linux, NVIDIA, and macOS collectors.
- [ ] Run the focused health tests.

### Task 2: Ready-only selector and status cards

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `frontend/src/__tests__/App.test.tsx`

**Interfaces:**
- Consumes: `Health.metrics`.
- Produces: ready-only host options and all-host status cards refreshed every 2 seconds.

- [ ] Add failing UI tests for ready-only options, resource cards, and reconnect.
- [ ] Store the latest health snapshot per backend and poll every 2 seconds.
- [ ] Render compact responsive machine cards.
- [ ] Run focused Vitest tests and build.

### Task 3: CUDA preflight and WSL route

**Files:**
- Modify: `backend/app/job_service.py`
- Modify: `backend/app/job_store.py`
- Modify: `scripts/setup-windows-tailscale-serve.ps1`
- Modify: `deploy/runtime.wsl.env.example`
- Test: `backend/tests/test_jobs.py`
- Test: `backend/tests/test_install_wsl.py`

**Interfaces:**
- Produces: `requires_preflight=false` for CUDA 7B jobs.
- Produces: private WSL Tailscale HTTPS endpoint on port 8443.

- [ ] Preserve the failing CUDA preflight test and add a failing WSL Serve topology test.
- [ ] Limit preflight to Apple MPS and configure Serve inside WSL.
- [ ] Run focused tests.

### Task 4: Deploy and real job

**Files:**
- Runtime only: Mac LaunchAgent, WSL systemd service, private runtime configuration.

- [ ] Run `scripts/test-release.sh`, frontend build, syntax checks, and `git diff --check`.
- [ ] Commit and push the feature branch.
- [ ] Sync and install on Windows without model download.
- [ ] Restart the idle Mac service and verify both live health endpoints.
- [ ] Upload `大堡礁3-clip1.mp4` via the resumable API with `7b-fp8-quality`, scale `1`, and wait for terminal state.
- [ ] Verify `requires_preflight=false`, output dimensions, streams, and duration with ffprobe.
