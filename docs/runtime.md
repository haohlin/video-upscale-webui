# Mac Runtime and Tailscale Operation

## Design boundary

Runtime assets live under `~/Library/Application Support/VideoUpscaleWebUI`, outside this Git repository. `start-local.sh` builds Vite frontend before FastAPI starts; FastAPI serves that built WebUI at `/` and keeps its API under `/api`. Every route except `/api/health` requires Tailscale Serve's verified `Tailscale-User-Login` header to match the configured single operator. The browser app is only service published through Tailscale. FastAPI listens on `127.0.0.1:8000`; optional ComfyUI listens on `127.0.0.1:8188`; neither binds to LAN or public interfaces.

Normal pre-deployment verification is `scripts/test-release.sh`, followed by `cd frontend && npm run build`. The release manifest contains 49 deterministic cases: 29 backend, 10 frontend, and 10 fork. Full pytest, Vitest, and unittest discovery are retained only for opt-in diagnosis. Future release-critical tests must fit within the 49-test ceiling.

SeedVR2 runs through its official standalone CLI located under the isolated ComfyUI custom node. Backend invokes tracked `scripts/seedvr2-adapter.py`; adapter maps stable profile arguments to upstream CLI arguments, keeps every process invocation as an argument array, then remuxes source audio into final MP4. `deploy/runtime.env` provides absolute adapter, official CLI, Python, model-directory, FFmpeg, and FFprobe paths. Never interpolate filenames into a shell command.

Default profile is `3b-safe`: 2x, batch size 5, chunk size 25, temporal overlap 4, tiled VAE, LAB color correction, no BlockSwap and no overflow/swap mode. `7b-fp8-experimental` remains opt-in and must pass the 10-second, maximum-480p probe first. No automatic downgrade or second 2x pass is allowed.

Real-ESRGAN is deferred: this Apple-Silicon runtime has no supported video executor, so it is not exposed by API or WebUI. Do not add it back until a concrete runtime and test coverage exist.

## First activation

Run from repository root after backend and frontend dependencies are installed:

```zsh
cp deploy/runtime.env.example deploy/runtime.env
$EDITOR deploy/runtime.env
scripts/check-system.sh
scripts/install-runtime.sh --apply --models
scripts/check-system.sh --require-runtime
scripts/start-local.sh --backend-only
scripts/setup-tailscale-serve.sh --dry-run
scripts/setup-tailscale-serve.sh --apply
```

`install-runtime.sh --apply --models` downloads only 3B FP8 plus shared VAE through Hugging Face's resumable client and validates each SHA-256 against the pinned SeedVR2 registry. It creates ComfyUI, reviewed `ComfyUI-SeedVR2_VideoUpscaler`, models, logs, `inputs`, `staging`, and `results` under application support; no model/runtime asset enters Git. ComfyUI and SeedVR2-node commits are pinned, and their executable Python dependency union installs only from repository-owned `deploy/runtime-requirements.lock` with hash enforcement. Update source pins, lock input, and lock output together. Applied updates refuse queued, preflight, or running jobs, quiesce the exact LaunchAgent before changing runtime files, and restart it only after successful validation. Migration removes only the old default `${VIDEO_UPSCALE_DATA_ROOT}/access-token`; if an older installation used a custom token path, remove that file manually after confirming the old service is stopped.

Regenerate the runtime lock only after reviewing the pinned upstream requirement files:

```zsh
uv pip compile deploy/runtime-requirements.in --generate-hashes \
  --python-version 3.13 --python-platform aarch64-apple-darwin --no-header \
  --output-file deploy/runtime-requirements.lock
```

Open the HTTPS URL printed by `tailscale serve status` from a browser signed into Tailscale as `VIDEO_UPSCALE_TAILSCALE_USER_LOGIN`. Serve injects the verified login header; no second WebUI password or token is used. Tailscale Serve is private and uses HTTPS port 8444 by default so it does not replace existing services on ports 443 or 8443. The script never invokes Funnel or enables public exposure.

Before entering `--apply`, dry-run every state-changing command. Large install or runtime failure logs appear under configured `VIDEO_UPSCALE_DATA_ROOT/logs`.

## Persistent service

Install per-user LaunchAgents only after interactive smoke test passes:

```zsh
scripts/install-launchagents.sh --dry-run
scripts/install-launchagents.sh --apply
launchctl print gui/$(id -u)/com.haohanl.video-upscale-webui
tailscale serve status
```

Main agent starts the backend in foreground so `launchd` restarts it. The backend invokes SeedVR2's official CLI from the isolated ComfyUI node; it does not keep a separate ComfyUI web server resident. Jobs and finished MP4 files persist until the operator deletes them in WebUI; per-job log size remains capped. Run `scripts/start-local.sh --with-comfy` only for local ComfyUI maintenance. Stop an interactive instance with `scripts/stop-local.sh --dry-run`, then `--apply`. Stop a persistent instance with:

```zsh
launchctl bootout gui/$(id -u)/com.haohanl.video-upscale-webui
```

## Operational checks

```zsh
scripts/check-system.sh --require-runtime
curl --fail --silent http://127.0.0.1:8000/api/health
tailscale serve status
```

`check-system.sh` confirms arm64, exact trusted Python, `uv`, Node dependency integrity, `ffmpeg`, `ffprobe`, `libx265`, Tailscale connection, configured operator login, free-disk reserve, backend Python, SeedVR2 CLI, and PyTorch MPS. Automatic Python downloads are disabled. Upload time/size, decoded frame workload, processing output, and free-disk reserve are continuously bounded. `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.75` and `PYTORCH_MPS_LOW_WATERMARK_RATIO=0.60` are exported to every adapter/probe process; unlike upstream `0.0` defaults, they retain allocator limits.

Run experimental 7B only with explicit operator intent:

```zsh
scripts/install-runtime.sh --apply --models --with-7b
scripts/probe-7b.sh --dry-run --input /absolute/path/short-video.mp4
scripts/probe-7b.sh --apply --input /absolute/path/short-video.mp4
```

Probe creates a 10-second, 480p-bounded working segment then requests 2x SeedVR2 7B FP8. It records normal SeedVR2 output/logs and fails on MPS OOM. It never enables BlockSwap or falls back to 3B.

## Required user touchpoints

- Confirm generated `deploy/runtime.env` paths if repository moves. Keep file local.
- Set `VIDEO_UPSCALE_TAILSCALE_USER_LOGIN` to the one allowed operator; no arbitrary-email development login or fallback credential is supported.
- Be signed into Tailscale on Mac host and every client. Restrict tailnet ACLs to this single operator's devices and keep Funnel disabled.
- Allow model download network traffic and reserve enough disk for runtime, model weights, and temporary video work.
- On iPhone Safari, select upload field then choose Photo Library. This uses browser's native Photos picker; no iOS app or Share extension is required.
