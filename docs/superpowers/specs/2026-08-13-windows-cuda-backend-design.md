# Windows CUDA Backend and Unified Mac WebUI Design

## Goal

Keep two complete Video Upscale backends: the existing Mac MPS service and a new WSL2 CUDA service using the Windows RTX 4090. Keep the current React layout and present both machines through one WebUI. Each backend owns its uploads, jobs, logs, results, SQLite database, and model cache.

## Architecture

The browser loads the WebUI from the Mac Tailscale Serve endpoint. Frontend runtime configuration names the Mac and Windows API endpoints without committing private hostnames. The browser uploads directly to the selected backend; the Mac never proxies video bytes to Windows.

Both backends run the same FastAPI application and API contract. Platform profiles choose the local runner:

- Mac: existing Apple MPS runner and SeedVR2 3B FP8 Safe preset.
- Windows WSL2: NVIDIA CUDA runner with SeedVR2 7B FP8 Quality and 3B FP8 Fast presets.

Both services remain tailnet-only. Funnel stays disabled. Each backend independently validates the same Tailscale operator identity and mutation header. Windows CORS accepts only the configured Mac WebUI origin.

## Backend Selection and Job Ownership

The frontend adds a compact processing-host selector without reorganizing the existing layout:

- `Auto — RTX 4090 preferred`
- `Windows RTX 4090`
- `Mac M4 Pro`

Backend health is `Ready`, `Busy`, or `Offline`. `Auto` sends new uploads to Windows while it is healthy and otherwise uses Mac. The selected backend ID is stored with each local resumable-upload reference and every fetched job view.

Upload resume, finalization, job polling, logs, cancellation, result download, and deletion always use the owning backend. Jobs never migrate silently. If Windows becomes unavailable during processing, the UI reports interruption and lets the operator retry as a new job on Mac. Existing Windows jobs remain visible when the selector changes.

## Windows Runtime

The repository is cloned into the private configured WSL2 development root on branch `feature/windows-cuda-backend`. Private hostnames, WSL paths, identities, and credentials stay in a mode-600 machine-local configuration consumed by a short scoped remote wrapper. The workflow verifies clean Git state, branch, and exact commit before remote actions.

WSL2 owns persistent directories for uploads, job state, results, logs, runtime checkout, and models. SeedVR2 model files download during explicit runtime installation or preset preparation, never for each job. A systemd service runs the FastAPI backend, while Windows startup ensures the WSL distribution starts after reboot.

Runtime prerequisites are pinned PyTorch CUDA, FFmpeg/ffprobe, the reviewed SeedVR2 fork revision, and required Python packages. CUDA availability and the RTX 4090 identity are checked before the runner reports ready.

## CUDA Presets and Tuning

`SeedVR2 7B FP8 Quality` is the default Windows quality preset. It uses memory preservation/offload and bounded VAE tiling appropriate for 24 GB VRAM. `SeedVR2 3B FP8 Fast` is the lower-latency option. No job silently downgrades models or resolution after submission.

CUDA SDPA is the compatibility baseline. Flash Attention, Triton, SageAttention, and `torch.compile` are enabled only after a short representative benchmark proves faster execution and stable output on this exact machine. Batch size, chunk size, temporal overlap, tiling, and offload settings become an explicit runtime profile included in ETA calibration fingerprints.

## API and Frontend Contract

Frontend runtime configuration contains public-in-tailnet backend descriptors: stable ID, display name, API base URL, and preference order. `/api/health` adds backend identity, platform, accelerator, state, and supported preset descriptors while retaining existing health fields.

API clients are keyed by backend ID. Aggregated job and upload lists attach backend metadata client-side; backend database schemas do not need cross-machine foreign keys. IDs are displayed and routed as `(backend_id, resource_id)` pairs to avoid accidental cross-backend operations.

Windows permits the exact configured Mac WebUI origin, required HTTP methods, and required headers. Wildcard origins and credential-bearing public access are prohibited.

## Failure Handling

- Windows offline before upload: `Auto` selects Mac; explicit Windows selection blocks with a clear error.
- Windows drops during upload: resumable session remains on Windows and resumes when Windows returns; operator may start a separate Mac upload.
- Windows drops during processing: job remains Windows-owned and visibly unavailable; no duplicate Mac processing starts automatically.
- Preset exceeds VRAM: bounded preflight rejects it with a useful message; no silent preset change.
- Backend identity or API contract mismatch: frontend marks that backend incompatible and does not send mutations.

## Verification

Keep the curated release gate at 49 or fewer cases. Add representative coverage by replacing overlapping cases rather than growing an exhaustive default suite:

- backend health descriptors and platform preset contract;
- exact-origin CORS and Tailscale identity enforcement;
- Auto selection, explicit selection, and offline fallback;
- upload and job backend affinity across refresh;
- cross-backend job aggregation and operation routing;
- CUDA runner command, cancellation, and persistent model paths;
- Windows installer/config validation without exposing private values.

Run one real short SeedVR2 7B FP8 RTX 4090 job, one 3B FP8 comparison, and one browser upload from the Mac WebUI to the Windows backend. Verify logs, progress, result download, restart persistence, model reuse, exact deployed commit, and Mac fallback. Submission stress testing is excluded.

## Delivery Order

1. Add platform-neutral backend descriptors and CUDA runner profile.
2. Add Windows WSL2 installation, service, and private remote workflow.
3. Install persistent runtime/models and benchmark 7B/3B profiles.
4. Add multi-backend frontend client, selector, aggregation, and affinity.
5. Run capped focused tests and real Windows smoke jobs.
6. Deploy Windows backend, update Mac frontend runtime configuration, and prove Mac fallback.

