# Pending Upload Recovery Design

## Goal

Make unfinished resumable uploads visible after a page refresh and let the single operator explicitly resume, end, or ignore them while starting a new upload.

## Scope

Add one authenticated read endpoint and one small frontend section. Keep the existing upload session format, chunk protocol, job lifecycle, authentication, and processing pipeline unchanged.

## Backend

`GET /api/uploads` returns every unexpired, non-finalizing upload session as the existing public session shape:

- `id`
- `filename`
- `total_bytes`
- `accepted_bytes`
- `expires_at`

The upload session service lists only metadata files with valid session IDs. It loads each record through the existing validation and expiry path. Invalid, unsafe, expired, or finalizing records are omitted. Results sort newest-expiring first for stable presentation.

Existing `GET /api/uploads/{id}`, `PUT`, `POST finalize`, and `DELETE` behavior remains unchanged. Tailscale identity authentication still protects reads; the existing mutation header still protects deletes and writes.

## Frontend

Initial refresh and normal polling fetch jobs and pending uploads independently. A failed pending-upload fetch reports the normal connection error and does not invent local state.

The upload panel shows a compact **Pending uploads** list. Each card displays filename, confirmed bytes versus total bytes, percentage, and expiry time.

- **Resume** opens a dedicated file picker for that session. Browser security requires the operator to reselect the original file. Resume proceeds only when filename and exact byte size match the server record; otherwise the UI shows an error and leaves the session untouched.
- **End upload** asks for confirmation, calls the existing delete endpoint, then removes the card after server success.
- The normal picker is labeled **Upload new**. It always creates a separate session and never silently selects or replaces a pending upload.

When resumed upload finalizes, its pending card disappears and the returned job appears in the normal queue. Refreshing the page reconstructs pending cards from server state, not browser storage.

## Error Handling

- Missing or expired session during resume: refresh pending uploads and show that session is no longer available.
- Wrong file: reject before sending bytes.
- Upload retry exhaustion: keep session visible and show its server-confirmed offset.
- Delete failure: retain card and show server error.
- Concurrent refresh: server response is authoritative; cards are keyed by session ID.

## Testing

Focused tests cover:

1. Listing returns valid pending sessions and omits expired, unsafe, and finalizing records.
2. Route requires Tailscale identity.
3. Refresh renders pending cards.
4. Resume requires exact filename and size, then reuses session ID and confirmed offset.
5. End upload calls delete and removes card only after success.
6. Upload new creates a new session even when a matching pending upload exists.

Keep the curated release gate at 49 or fewer tests by replacing weaker cases rather than expanding the total. Run one short browser/API smoke; do not run stress testing.

## Non-Goals

- No automatic matching by filename or size.
- No browser-local persistence.
- No multi-user ownership model.
- No parallel upload support.
- No changes to SeedVR2 processing, ETA, output retention, Tailscale routing, or proxy configuration.
