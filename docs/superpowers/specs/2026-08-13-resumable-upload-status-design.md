# Resumable Upload and Truthful Status Design

## Goal

Allow one operator on Tailscale to reliably upload a large video over a slow or
intermittent link, see server-confirmed transfer state, and follow exactly one
processing job through validated completion.

## Upload protocol

Browser creates an upload session with video metadata and selected processing
options. Server returns an opaque random session ID and accepted byte offset.
Browser sends sequential 4 MiB chunks with exact offset and total size. Server
writes only at expected offset, fsyncs accepted data, and returns next offset.
Browser retries a failed chunk up to three times with bounded backoff and can
query session state to resume without resending accepted bytes.

Finalization is explicit. Server checks total size, converts staged upload into
the existing validated JobService path, probes media, creates the processing
job, and removes upload-session state. Only one upload session may actively
write at a time. Existing maximum body size, disk reserve, queue capacity,
extension allowlist, Tailscale identity, and same-origin mutation header remain
mandatory. Session IDs never authorize access by themselves.

Incomplete upload sessions expire after 24 hours. Cleanup removes only files
inside the configured staging directory. Completed job inputs/results remain
until manual WebUI deletion.

## Frontend state

UI keeps an explicit submission state: creating, uploading, retrying,
validating, queued, processing, completed, or failed. Progress uses the
server-confirmed offset, not bytes merely handed to the browser network stack.
Speed and ETA use accepted bytes over elapsed transfer time.

Debug console binds to explicit current job ID. Before finalization creates a
job, it shows upload-session events only and never falls back to an old job.
Terminal logs are titled Historical job log and fetched once. Active logs are
titled Live job log and polled every two seconds. Job ID and terminal state are
visible. Completion adds a UI-owned line stating output validation reached
100%; adapter logs remain unmodified evidence.

Job discovery continues while debug console is open, even when the initial job
list contains only terminal jobs.

## Failure behavior

Network errors keep the confirmed offset and show retry count. Exhausted retry
offers Resume; it does not claim a server job exists. Offset mismatch refreshes
session state before retry. Server restart preserves session offset on disk.
Invalid, expired, oversized, conflicting, or out-of-order requests fail closed
without corrupting an existing upload.

## Verification

Use TDD for upload-session invariants and UI context isolation. Keep curated
release gate at 49 or fewer tests by replacing weaker legacy upload/UI cases.
Build frontend, run focused backend tests, run curated release gate, deploy only
while queue is empty, then complete one representative 1x job through HTTPS
Tailscale Serve. Do not run submission stress testing.

## Network boundary

Tailscale-only identity remains mandatory. Proxy exclusion may improve DERP
control/relay traffic but does not replace direct-UDP diagnostics. No direct LAN
unauthenticated listener, Funnel, local token, or alternate login is added.
