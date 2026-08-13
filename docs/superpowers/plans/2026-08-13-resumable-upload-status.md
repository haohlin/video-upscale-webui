# Resumable Upload and Truthful Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver resumable server-confirmed uploads and a job-bound status console, then deploy and finish one real 1x job.

**Architecture:** Add a small persisted upload-session service and authenticated REST endpoints, then have frontend upload fixed sequential chunks and explicitly bind monitoring to the resulting job. Preserve existing JobService validation/queue path by finalizing a staged upload through the same media checks.

**Tech Stack:** FastAPI, Python stdlib filesystem/JSON primitives, SQLite job store, React/TypeScript, XMLHttpRequest/fetch, Vitest, pytest, Tailscale Serve.

## Global Constraints

- Tailscale identity plus `X-Video-Upscale-Request: 1` remains mandatory.
- One active writer; 4 MiB sequential chunks; three retries; 24-hour session expiry.
- Existing upload, media, dimension, pixel-frame, disk, and queue limits remain authoritative.
- Results persist until manual deletion.
- Curated release gate remains at 49 or fewer tests; no stress test.
- Do not restart or deploy while any job is queued, preflight, or running.

---

### Task 1: Persisted resumable upload sessions

**Files:**
- Create: `backend/app/upload_sessions.py`
- Create: `backend/tests/test_upload_sessions.py`
- Modify: `backend/app/config.py`

**Interfaces:**
- Produces: `UploadSessionService.create`, `status`, `append`, `finalize`, and `discard`.
- Session response fields: `id`, `filename`, `total_bytes`, `accepted_bytes`, `expires_at`.

- [ ] Write failing tests for opaque IDs, exact offsets, restart recovery, size bounds, expiry, and no-follow staging writes.
- [ ] Run only `backend/tests/test_upload_sessions.py`; confirm intended failures.
- [ ] Implement metadata stored beside a random staging file using atomic replace; open data file no-follow and append only at accepted offset.
- [ ] Run focused tests; confirm pass.
- [ ] Commit backend session primitive.

### Task 2: Authenticated upload API and existing job pipeline finalization

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/job_service.py`
- Modify: `backend/app/upload_guard.py`
- Create: `backend/tests/test_resumable_upload_api.py`

**Interfaces:**
- `POST /api/uploads` creates session from JSON metadata/options.
- `GET /api/uploads/{id}` returns server-confirmed offset.
- `PUT /api/uploads/{id}` consumes one chunk with `Upload-Offset`.
- `POST /api/uploads/{id}/finalize` returns normal public `Job`.
- `DELETE /api/uploads/{id}` discards incomplete session.

- [ ] Write failing API tests for authorization, CSRF header, exact offsets, retries, restart resume, finalization, and rejection cleanup.
- [ ] Run focused API tests; confirm intended failures.
- [ ] Extract JobService staged-file validation so legacy multipart and resumable finalization share one code path.
- [ ] Add endpoints and update raw body guard to bound each chunk while preserving legacy endpoint compatibility.
- [ ] Run focused backend tests; confirm pass.
- [ ] Commit API/finalization change.

### Task 3: Resumable browser client and truthful console

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/__tests__/api.test.ts`
- Modify: `frontend/src/__tests__/App.test.tsx`

**Interfaces:**
- `createJob` creates session, uploads sequential 4 MiB slices, resumes from server offset, finalizes, and reports confirmed progress plus retry state.
- Debug console receives explicit upload/job context and never selects unrelated terminal fallback during submission.

- [ ] Write failing tests for confirmed offset progress, retry/resume, no old-job mixing, historical labels, active polling, and validated completion copy.
- [ ] Run focused frontend tests; confirm intended failures.
- [ ] Implement minimal client protocol and explicit monitored job state.
- [ ] Run focused tests and frontend build; confirm pass.
- [ ] Commit frontend change.

### Task 4: Bounded release gate and deployment

**Files:**
- Modify: `scripts/release-tests.toml`
- Modify: `README.md`

**Interfaces:**
- Release gate includes representative session security, API resume, and console-isolation cases without exceeding 49.

- [ ] Replace weaker legacy cases; run `scripts/test-release.sh --check` and confirm exact count at most 49.
- [ ] Run focused backend tests, focused frontend tests/build, then curated release gate.
- [ ] Run security diff scan for WebUI changes and fix only P0/high findings.
- [ ] Commit docs/gate, sanitize author email, push feature branch, fast-forward clean main.
- [ ] Confirm database has no queued/preflight/running job; deploy through existing installer/update gate.
- [ ] Verify loopback backend, Tailscale identity denial/allow behavior, and Funnel absence.
- [ ] Upload one representative video through HTTPS, observe confirmed transfer, finish one 1x job, validate/download MP4.
- [ ] Recheck Tailscale path as direct versus DERP and report remaining network boundary honestly.
