# SeedVR2 MPS Streaming Cache Design

## Problem

The WebUI launches one SeedVR2 CLI process per job and already keeps model files on disk. Streaming jobs split long videos into chunks, but the adapter omits `--cache_dit` and `--cache_vae`. The CLI therefore calls `prepare_runner` without reusable model IDs for every chunk. A 2160×3840, 923-frame job has 37 chunks and spent more than six minutes preparing its first chunk.

The CLI also prints generic SageAttention, Flash Attention, and Triton installation advice on Apple MPS. Those packages provide CUDA kernels and are not useful for this Mac runtime.

## Approved Scope

- Add `--cache_dit` and `--cache_vae` to every WebUI streaming SeedVR2 command.
- Keep existing 25-frame chunk size, VAE tiling, 3B model, SDPA attention, and MPS backend.
- Cache only inside one CLI process. Do not keep tens of gigabytes resident after a job ends.
- Update runtime fingerprint from disabled to enabled so old ETA samples cannot contaminate new estimates.
- Replace generic CUDA optimization advice in visible Mac logs with one accurate MPS message.
- Preserve the cancelled source video, deploy only while no job is active, and requeue it through loopback without another Tailscale upload.

## Data Flow

`JobService` records an enabled-cache fingerprint. `seedvr2-adapter.py` passes both official CLI cache flags. SeedVR2 creates one `runner_cache` for the streaming file, caches DiT and VAE after their first use, and reuses them for later chunks. Models remain in the existing disk model directory and are never downloaded during a job.

## Safety

SeedVR2 defaults cached-model offload to CPU. On Apple Silicon this remains unified memory. Existing 25-frame chunks limit peak working tensors. The first real smoke uses a five-frame 320×256 clip; the preserved 4K job starts only after that succeeds. Current process cancellation uses the API and preserves the input file.

## Verification

- RED/GREEN adapter command test for both cache flags.
- RED/GREEN job API test for enabled-cache runtime fingerprint.
- Focused adapter and job tests only.
- Existing capped 49-test release gate.
- Real MPS smoke proves completed MP4.
- Requeued 4K job must report model preparation once, then advance beyond chunk 1 without another cold load.

