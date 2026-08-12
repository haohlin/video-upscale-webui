# Tailscale Identity Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Follow TDD.

**Goal:** Replace HTTP Basic credentials with single-operator Tailscale Serve identity and deploy a job-accepting private WebUI.

**Architecture:** A loopback-only FastAPI middleware trusts Tailscale Serve's stripped-and-injected `Tailscale-User-Login` header and matches it to one configured operator. Runtime scripts remove the obsolete token path and verify Serve remains private.

**Tech Stack:** FastAPI, Tailscale Serve, zsh, pytest, existing 49-case release gate.

## Global Constraints

- Only P0/high security blockers remain required.
- No test command may execute more than 49 cases.
- Funnel must remain disabled; backend remains on `127.0.0.1`.
- Stop only after a real short 1x job is accepted.

---

### Task 1: Tailscale identity middleware and configuration

**Files:** `backend/app/main.py`, `backend/app/config.py`, `backend/tests/test_health.py`, `backend/tests/conftest.py`

- [ ] Add failing tests for correct, missing, and wrong `Tailscale-User-Login`, absence of `WWW-Authenticate`, health exemption, and mutation header.
- [ ] Run only those tests and observe the expected failures.
- [ ] Replace Basic verification with exact configured login matching and remove token fields/loading.
- [ ] Run the focused tests and keep the selected count below 49.

### Task 2: Runtime migration and documentation

**Files:** `scripts/lib.sh`, `scripts/install-runtime.sh`, `scripts/check-system.sh`, `deploy/runtime.env.example`, `README.md`, `docs/runtime.md`, `docs/architecture.md`, relevant installer tests and release manifest.

- [ ] Add failing static/runtime tests proving no token generation/check remains and the operator login is required.
- [ ] Remove token settings and add `VIDEO_UPSCALE_TAILSCALE_USER_LOGIN`.
- [ ] Remove only the exact legacy token file after the applied updater has safely quiesced the service.
- [ ] Update operator documentation and keep the release manifest at or below 49.
- [ ] Run `scripts/test-release.sh`, installer dry-run, and syntax checks.

### Task 3: Pin, scan, publish, and deploy

**Files:** Task 8 installer files plus final documentation.

- [ ] Independently review and security-scan the final fork P0 commit; pin its literal SHA only after approval.
- [ ] Review and scan the final WebUI revision; fix only validated P0/high blockers.
- [ ] Push both branches only after scans.
- [ ] With no queued/preflight/running job, apply the updater and restart the exact LaunchAgent.
- [ ] Verify loopback binding, private Serve/Funnel absence, tailnet identity access, and health.
- [ ] Submit one short low-resolution 1x job and prove it reaches `queued`, `preflight`, `running`, or `completed`.

