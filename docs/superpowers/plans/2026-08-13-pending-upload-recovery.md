# Pending Upload Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show durable pending uploads after refresh and let the operator explicitly resume, end, or start a separate new upload.

**Architecture:** Extend the existing disk-backed upload service with one validated listing method and expose it through one authenticated endpoint. Fetch that server state beside jobs, then render small pending-upload cards whose resume action asks the browser for the original file and reuses the selected session ID.

**Tech Stack:** Python 3.13, FastAPI, React 19, TypeScript, Vitest, pytest.

## Global Constraints

- Keep existing session files, chunk protocol, job lifecycle, Tailscale identity gate, and mutation header unchanged.
- Resume requires exact filename and byte-size match; never auto-match.
- Upload new always creates a separate session.
- No parallel uploads, browser-local persistence, or multi-user ownership.
- Curated release gate stays at 49 tests or fewer; no stress test.

---

### Task 1: Validated Pending-Upload Listing

**Files:**
- Modify: `backend/app/upload_sessions.py:97-210`
- Modify: `backend/app/main.py:180-220`
- Test: `backend/tests/test_upload_sessions.py`
- Test: `backend/tests/test_resumable_upload_api.py`

**Interfaces:**
- Produces: `UploadSessionService.list_pending() -> list[dict[str, object]]`
- Produces: `GET /api/uploads -> {"uploads": list[public session]}`

- [ ] **Step 1: Write failing service tests**

Add tests that create two sessions, advance one, expire a third through the injected clock, and mark a fourth finalizing. Assert `list_pending()` returns only non-expired, non-finalizing sessions, including server-confirmed offsets, sorted by `expires_at` descending.

```python
pending = service.list_pending()
assert [item["id"] for item in pending] == [newer["id"], older["id"]]
assert pending[1]["accepted_bytes"] == 3
assert expired["id"] not in {item["id"] for item in pending}
assert claimed["id"] not in {item["id"] for item in pending}
```

- [ ] **Step 2: Write failing route tests**

```python
listed = api.get("/api/uploads")
assert listed.status_code == 200
assert listed.json() == {"uploads": [expect_session]}
assert TestClient(api.app).get("/api/uploads").status_code == 403
```

- [ ] **Step 3: Run RED tests**

Run:

```bash
cd backend
uv run --locked pytest -q \
  tests/test_upload_sessions.py -k list_pending \
  tests/test_resumable_upload_api.py -k list_pending
```

Expected: failures because `list_pending` and exact collection route do not exist.

- [ ] **Step 4: Implement minimal listing method**

Under existing service-wide `RLock`, run `_cleanup_expired()`, inspect only `*.json` names matching `SESSION_ID_PATTERN`, load through `_load()`, omit invalid/unsafe/expired/finalizing records, convert with `_public()`, and sort by `(expires_at, id)` descending.

```python
def list_pending(self) -> list[dict[str, object]]:
    with self._session_lock:
        self._cleanup_expired()
        pending: list[dict[str, object]] = []
        for metadata_path in self.staging.glob("*.json"):
            session_id = metadata_path.stem
            if not SESSION_ID_PATTERN.fullmatch(session_id):
                continue
            try:
                record = self._load(session_id)
                if not self._finalizing(record):
                    pending.append(self._public(record))
            except (OSError, ValueError, json.JSONDecodeError, UploadSessionError):
                continue
        return sorted(
            pending,
            key=lambda item: (str(item["expires_at"]), str(item["id"])),
            reverse=True,
        )
```

- [ ] **Step 5: Add exact collection route before dynamic route**

```python
@service.get("/api/uploads")
def list_uploads() -> dict[str, list[dict[str, object]]]:
    return {"uploads": uploads.list_pending()}
```

- [ ] **Step 6: Run GREEN tests and commit**

```bash
cd backend
uv run --locked pytest -q \
  tests/test_upload_sessions.py -k list_pending \
  tests/test_resumable_upload_api.py -k 'list_pending or upload_routes_require_operator'
cd ..
git add backend/app/upload_sessions.py backend/app/main.py \
  backend/tests/test_upload_sessions.py backend/tests/test_resumable_upload_api.py
git commit -m "feat: list pending uploads"
```

Expected: selected tests pass.

---

### Task 2: Pending Upload API and UI Controls

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `frontend/src/__tests__/api.test.ts`
- Test: `frontend/src/__tests__/App.test.tsx`

**Interfaces:**
- Consumes: `GET /api/uploads` collection from Task 1
- Produces: `getUploads() -> Promise<UploadSession[]>`
- Produces: `discardUpload(id: string) -> Promise<void>`
- Produces: pending-upload cards with `Resume` and `End upload`

- [ ] **Step 1: Define test fixture and mocked API shape**

Export this type from `types.ts`, import it in tests, and add `getUploads` plus `discardUpload` to the App API mock.

```ts
export interface UploadSession {
  id: string;
  filename: string;
  total_bytes: number;
  accepted_bytes: number;
  expires_at: string;
}
```

- [ ] **Step 2: Write failing API tests**

```ts
it("lists and discards pending uploads", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(response(200, { uploads: [session] }))
    .mockResolvedValueOnce({ ok: true, status: 204, json: async () => undefined } as Response);
  vi.stubGlobal("fetch", fetchMock);
  await expect(getUploads()).resolves.toEqual([session]);
  await expect(discardUpload(session.id)).resolves.toBeUndefined();
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/uploads/upload-1",
    expect.objectContaining({ method: "DELETE" }),
  );
});
```

- [ ] **Step 3: Write failing App refresh/resume/discard tests**

Tests must assert real visible behavior:

```ts
expect(await screen.findByText("Pending uploads")).toBeVisible();
expect(screen.getByText("12 MB / 60 MB confirmed")).toBeVisible();
await user.click(screen.getByRole("button", { name: "Resume lake.mov" }));
await user.upload(screen.getByLabelText("Choose file to resume lake.mov"), matchingFile);
expect(screen.getByRole("button", { name: "Resume upload" })).toBeVisible();
```

Add separate assertions that a wrong filename or size shows an error without calling `createJob`, `End upload` calls `window.confirm` and `discardUpload`, and choosing the normal new-file input leaves `resumeSessionId` undefined.

- [ ] **Step 4: Run RED frontend tests**

```bash
cd frontend
npm test -- --run src/__tests__/api.test.ts src/__tests__/App.test.tsx
```

Expected: failures because collection API and pending cards do not exist.

- [ ] **Step 5: Implement API functions**

`request` must support `204 No Content` without parsing JSON.

```ts
if (response.status === 204) return undefined as T;

export async function getUploads(): Promise<UploadSession[]> {
  return (await request<{ uploads: UploadSession[] }>("/uploads")).uploads;
}

export async function discardUpload(id: string): Promise<void> {
  await request<void>(`/uploads/${encodeURIComponent(id)}`, { method: "DELETE" });
}
```

- [ ] **Step 6: Implement minimal App state and refresh**

Add `pendingUploads`, `resumeTarget`, and a dedicated hidden resume file input. Fetch health, jobs, and uploads together. Keep server state authoritative.

```ts
const [pendingUploads, setPendingUploads] = useState<UploadSession[]>([]);
const [resumeTarget, setResumeTarget] = useState<UploadSession | null>(null);

const [health, nextJobs, nextUploads] = await Promise.all([
  getHealth(), getJobs(), getUploads(),
]);
setJobs(sortJobs(nextJobs));
setPendingUploads(nextUploads);
```

Resume selection verifies exact `file.name === target.filename` and `file.size === target.total_bytes`, then sets `selectedFile` and `resumeSessionId`. Normal `chooseFile` continues clearing resume state. On upload failure, refresh so current confirmed offset appears in pending cards. On successful finalize, refresh removes session and adds job.

- [ ] **Step 7: Render compact pending cards and controls**

Each keyed card displays filename, confirmed fraction, percent, and local expiry. `Resume` triggers the dedicated file picker. `End upload` requires `window.confirm`, awaits delete, and only then removes card. Change drop-zone copy to “Upload a new video” so new-session behavior is explicit.

- [ ] **Step 8: Run GREEN frontend tests and build**

```bash
cd frontend
npm test -- --run src/__tests__/api.test.ts src/__tests__/App.test.tsx
npm run build
cd ..
git add frontend/src/types.ts frontend/src/api.ts frontend/src/App.tsx \
  frontend/src/styles.css frontend/src/__tests__/api.test.ts frontend/src/__tests__/App.test.tsx
git commit -m "feat: recover pending uploads after refresh"
```

Expected: focused tests and TypeScript build pass.

---

### Task 3: Bounded Release Gate and Live Recovery Smoke

**Files:**
- Modify: `scripts/release-tests.toml`

**Interfaces:**
- Consumes: backend listing and frontend controls from Tasks 1-2
- Produces: release coverage capped at 49 tests

- [ ] **Step 1: Replace weaker release cases without increasing count**

Add one backend listing/auth test and two frontend pending-upload tests. Remove three older overlapping UI/auth cases. Confirm manifest total remains at most 49.

- [ ] **Step 2: Run manifest validation and release gate**

```bash
scripts/test-release.sh --check
scripts/test-release.sh
```

Expected: manifest valid and no more than 49 selected tests pass.

- [ ] **Step 3: Run one local API/UI smoke**

Restart only after confirming no queued, preflight, or running jobs. Verify:

1. `GET /api/uploads` lists existing unfinished sessions.
2. Refresh preserves pending cards and existing jobs.
3. Resume rejects a synthetic wrong file locally.
4. End upload deletes one new synthetic session, not existing user sessions.
5. Upload new creates a distinct synthetic session, then discard it.

Do not delete or finalize the operator's existing three `大堡礁3-clip1.mov` sessions.

- [ ] **Step 4: Commit release manifest**

```bash
git add scripts/release-tests.toml
git commit -m "test: cover pending upload recovery"
```

- [ ] **Step 5: Final verification**

```bash
git diff --check
git status --short
```

Expected: clean tracked worktree; only intentional external runtime data remains.
