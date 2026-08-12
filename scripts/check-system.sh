#!/bin/zsh
# Read-only runtime preflight. Exit nonzero when requested requirements fail.

set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/lib.sh"

require_runtime=0
if (( $# > 0 )); then
  [[ "$1" == "--require-runtime" && $# == 1 ]] || die "usage: $0 [--require-runtime]"
  require_runtime=1
fi

load_runtime_config

exit_status=0
check() {
  local label="$1"
  shift
  if "$@"; then
    note "ok: $label"
  else
    note "missing: $label"
    exit_status=1
  fi
}

check "Apple Silicon arm64" test "$(uname -m)" = "arm64"
check "uv" command -v uv
check "ffmpeg" command -v ffmpeg
check "ffprobe" command -v ffprobe
check "Node.js" command -v node
check "frontend dependency integrity" node "$VIDEO_UPSCALE_PROJECT_ROOT/scripts/verify-frontend-lock.mjs"
check "ffmpeg libx265 encoder" sh -c 'ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "libx265"'
check "Tailscale connected" sh -c 'tailscale status --json 2>/dev/null | grep -q "\"BackendState\"[[:space:]]*:[[:space:]]*\"Running\""'

if [[ -f "$VIDEO_UPSCALE_ACCESS_TOKEN_FILE" ]] \
  && [[ "$(stat -f '%Lp' "$VIDEO_UPSCALE_ACCESS_TOKEN_FILE")" == "600" ]] \
  && (( $(wc -c < "$VIDEO_UPSCALE_ACCESS_TOKEN_FILE") >= 64 )); then
  note "ok: private browser access token"
else
  note "missing: mode-600 browser access token (run scripts/install-runtime.sh --apply)"
  (( require_runtime )) && exit_status=1
fi

if [[ -x "$VIDEO_UPSCALE_PYTHON" ]]; then
  check "SeedVR2 exact Python" "$VIDEO_UPSCALE_PYTHON" -c \
    'import os, platform; assert platform.python_version() == os.environ["VIDEO_UPSCALE_PYTHON_VERSION"]'
  check "SeedVR2 Python MPS" "$VIDEO_UPSCALE_PYTHON" -c 'import torch; assert torch.backends.mps.is_built() and torch.backends.mps.is_available()'
else
  note "pending: SeedVR2 Python/MPS (run scripts/install-runtime.sh --apply)"
  (( require_runtime )) && exit_status=1
fi

if [[ -x "$VIDEO_UPSCALE_BACKEND_PYTHON" ]]; then
  check "backend exact Python" "$VIDEO_UPSCALE_BACKEND_PYTHON" -c \
    'import os, platform; assert platform.python_version() == os.environ["VIDEO_UPSCALE_PYTHON_VERSION"]'
  check "backend Python" "$VIDEO_UPSCALE_BACKEND_PYTHON" -c 'import fastapi, uvicorn'
else
  note "pending: backend Python (run uv sync in backend)"
  (( require_runtime )) && exit_status=1
fi

if [[ -f "$VIDEO_UPSCALE_SEEDVR2_CLI" && -f "$VIDEO_UPSCALE_SEEDVR2_OFFICIAL_CLI" ]]; then
  note "ok: SeedVR2 adapter and official CLI"
else
  note "pending: SeedVR2 adapter or official CLI (run scripts/install-runtime.sh --apply)"
  (( require_runtime )) && exit_status=1
fi

available="$(available_gib "$VIDEO_UPSCALE_PROJECT_ROOT")"
if (( available >= VIDEO_UPSCALE_DISK_RESERVE_GB )); then
  note "ok: disk ${available} GiB free; reserve ${VIDEO_UPSCALE_DISK_RESERVE_GB} GiB"
else
  note "missing: disk ${available} GiB free; reserve ${VIDEO_UPSCALE_DISK_RESERVE_GB} GiB"
  exit_status=1
fi

exit "$exit_status"
