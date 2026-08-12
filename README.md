# Video Upscale WebUI

Private, Mac-hosted SeedVR2 video-upscaling WebUI for Tailscale-connected devices. React/Vite provides upload, queue, progress, logs, and MP4 download. FastAPI owns bounded uploads, SQLite job state, and one Apple-Silicon processing worker.

## Security model

- Uvicorn and optional ComfyUI bind only to `127.0.0.1`.
- Tailscale Serve provides private HTTPS; supplied scripts never enable Funnel.
- Every UI and job route requires a generated HTTP Basic credential. Health stays unauthenticated for local monitoring.
- State-changing routes also require a non-simple same-origin header to block browser CSRF.
- Upload concurrency/time/size, queue depth, media duration/resolution/frame count, parser and processing time, output bytes, disk reserve, and per-job log size are bounded and configurable. Job history and finished results persist until the operator deletes them.
- Commands use argument arrays, server-generated media paths, allowlisted profiles, and pinned upstream source revisions.

This is a single-operator service. Do not share its generated access token. Tailnet ACLs should permit only that operator's devices.

## Install

Requirements: Apple Silicon Mac, trusted Python 3.13.11, `uv`, Node/npm, FFmpeg with `libx265`, Tailscale, and enough disk for SeedVR2 models. Automatic Python downloads are disabled.

```zsh
cp deploy/runtime.env.example deploy/runtime.env
$EDITOR deploy/runtime.env
scripts/install-runtime.sh --apply --models
scripts/check-system.sh --require-runtime
scripts/start-local.sh --backend-only
scripts/setup-tailscale-serve.sh --dry-run
scripts/setup-tailscale-serve.sh --apply
```

Installer generates a mode-600 access token at configured `VIDEO_UPSCALE_ACCESS_TOKEN_FILE`. Open that local file to retrieve browser password; username defaults to `video`. Never commit token or `deploy/runtime.env`.

See [runtime guide](docs/runtime.md) and [architecture](docs/architecture.md). Models, inputs, results, logs, credentials, and runtime state remain outside Git.

## Verification

Normal completion and release verification uses one deterministic, cross-repository gate:

```zsh
scripts/test-release.sh
```

The exact manifest is `scripts/release-tests.toml`. It currently runs 47 representative cases across backend, frontend, and the SeedVR2 fork, leaving two slots for Task 8 installer safety tests. It fails before execution if any selected name is missing, duplicated, or outside the enforced 1–49 budget. Set `SEEDVR2_FORK_ROOT` only when the fork is not beside the main WebUI checkout; set `SEEDVR2_TEST_PYTHON` when its test dependencies use a different Python environment.

Exhaustive suites remain opt-in diagnostics:

```zsh
cd backend && uv run --group dev pytest -v
cd frontend && npm run test:exhaustive
cd "$SEEDVR2_FORK_ROOT" && "$SEEDVR2_TEST_PYTHON" -m unittest discover -s tests -p 'test_*.py' -v
```

Exhaustive runs are not normal completion or deployment gates. Release gate intentionally avoids GPU inference, model downloads, network access, live services, and large stress fixtures.
