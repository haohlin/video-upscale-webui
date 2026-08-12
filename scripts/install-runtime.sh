#!/bin/zsh
# Install isolated ComfyUI + official SeedVR2 node outside this Git checkout.

set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/lib.sh"

apply=0
download_models=0
download_7b=0
update=0

# Vetted upstream snapshots. Change intentionally and validate this Mac before
# updating either value; runtime installation must never silently follow main.
COMFYUI_REVISION="e651b7bef55a5376343dcb1c0edb79f0142c985e"
SEEDVR2_NODE_REVISION="4490bd1f482e026674543386bb2a4d176da245b9"

usage() {
  cat <<'EOF'
usage: scripts/install-runtime.sh [--dry-run|--apply] [--models] [--with-7b] [--update]

--apply     Perform installation. Default and --dry-run only print commands.
--models    Download and SHA-validate default SeedVR2 3B FP8 + VAE weights.
--with-7b   Also download 7B FP8 experimental weights. Requires --models.
--update    Reconcile existing runtime checkouts to the pinned revisions in this script.
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run) apply=0 ;;
    --apply) apply=1 ;;
    --models) download_models=1 ;;
    --with-7b) download_7b=1 ;;
    --update) update=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

(( ! download_7b || download_models )) || die "--with-7b requires --models"
load_runtime_config

for tool in uv git ffmpeg ffprobe openssl; do
  require_command "$tool"
done
[[ "$(uname -m)" == "arm64" ]] || die "Apple Silicon arm64 required"
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libx265' || die "ffmpeg lacks libx265 encoder"
require_disk_reserve
python_candidate="$(uv python find --no-python-downloads --system "$VIDEO_UPSCALE_PYTHON_VERSION")" \
  || die "trusted Python $VIDEO_UPSCALE_PYTHON_VERSION is not installed; automatic downloads are disabled"
[[ "$("$python_candidate" -c 'import platform; print(platform.python_version())')" == "$VIDEO_UPSCALE_PYTHON_VERSION" ]] \
  || die "resolved Python does not match VIDEO_UPSCALE_PYTHON_VERSION"

run() {
  if (( apply )); then
    command "$@"
  else
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  fi
}

checkout_pinned_revision() {
  local repository="$1"
  local destination="$2"
  local revision="$3"

  verify_checkout() {
    local actual_origin
    local ignored_python
    local worktree_status
    actual_origin="$(git -C "$destination" remote get-url origin)"
    [[ "$actual_origin" == "$repository" ]] || die "runtime origin mismatch at $destination"
    [[ "$(git -C "$destination" rev-parse HEAD)" == "$revision" ]] \
      || die "runtime revision mismatch at $destination"
    worktree_status="$(git -C "$destination" status --porcelain --untracked-files=all)"
    [[ -z "$worktree_status" ]] || die "runtime checkout is modified at $destination"
    [[ ! -f "$destination/.gitmodules" ]] || die "runtime checkout has unexpected submodules at $destination"
    if [[ "$repository" == "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git" ]]; then
      ignored_python="$(git -C "$destination" ls-files --others --ignored --exclude-standard -- '*.py')"
      [[ -z "$ignored_python" ]] || die "runtime checkout contains ignored Python source at $destination"
    fi
    if [[ "$repository" == "https://github.com/Comfy-Org/ComfyUI.git" ]]; then
      local unexpected_custom_node
      unexpected_custom_node="$(find "$destination/custom_nodes" -mindepth 1 -maxdepth 1 \
        ! -name '__pycache__' \
        ! -name 'ComfyUI-SeedVR2_VideoUpscaler' \
        ! -name 'example_node.py.example' \
        ! -name 'websocket_image_save.py' -print -quit)"
      [[ -z "$unexpected_custom_node" ]] || die "runtime checkout contains unexpected custom nodes at $destination"
    fi
  }

  if [[ -d "$destination/.git" ]]; then
    local current_revision
    current_revision="$(git -C "$destination" rev-parse HEAD)"
    if [[ "$current_revision" == "$revision" ]]; then
      note "pinned Git checkout present: $destination"
    elif (( update )); then
      run git -C "$destination" fetch --depth=1 origin "$revision"
      run git -C "$destination" checkout --detach "$revision"
    else
      die "runtime revision mismatch at $destination; re-run with --update to use pinned $revision"
    fi
  elif [[ -e "$destination" ]]; then
    die "refusing to overwrite non-Git path: $destination"
  else
    run git init "$destination"
    run git -C "$destination" remote add origin "$repository"
    run git -C "$destination" fetch --depth=1 origin "$revision"
    run git -C "$destination" checkout --detach FETCH_HEAD
  fi
  if (( apply )) || [[ -d "$destination/.git" ]]; then
    verify_checkout
  fi
}

if (( ! apply )); then
  note "DRY RUN. Re-run with --apply to change this Mac."
fi

run mkdir -p "$VIDEO_UPSCALE_RUNTIME_ROOT" "$VIDEO_UPSCALE_DATA_ROOT" \
  "$VIDEO_UPSCALE_DATA_ROOT/inputs" "$VIDEO_UPSCALE_DATA_ROOT/results" \
  "$VIDEO_UPSCALE_DATA_ROOT/staging" "$VIDEO_UPSCALE_DATA_ROOT/logs" \
  "$VIDEO_UPSCALE_SEEDVR2_MODEL_DIR"

if [[ ! -f "$VIDEO_UPSCALE_ACCESS_TOKEN_FILE" ]]; then
  if (( apply )); then
    umask 077
    openssl rand -hex 32 > "$VIDEO_UPSCALE_ACCESS_TOKEN_FILE"
    chmod 600 "$VIDEO_UPSCALE_ACCESS_TOKEN_FILE"
    note "generated private browser access token at configured token file"
  else
    note "+ generate mode-600 browser access token at $VIDEO_UPSCALE_ACCESS_TOKEN_FILE"
  fi
fi

# This is separate from SeedVR2/ComfyUI Python. It provides FastAPI/Uvicorn
# expected by start-local.sh at the exact interpreter path in runtime.env.
# Runtime config points at <venv>/bin/python; create the venv root, not its
# bin directory, so the configured interpreter exists after `uv venv`.
backend_venv_dir="${VIDEO_UPSCALE_BACKEND_PYTHON:h:h}"
run uv venv --clear --no-python-downloads --python "$python_candidate" "$backend_venv_dir"
run uv sync --locked --reinstall --project "$VIDEO_UPSCALE_PROJECT_ROOT/backend" \
  --python "$VIDEO_UPSCALE_BACKEND_PYTHON" --no-dev
if (( apply )); then
  [[ -x "$VIDEO_UPSCALE_BACKEND_PYTHON" ]] || die "backend Python missing after sync: $VIDEO_UPSCALE_BACKEND_PYTHON"
fi

checkout_pinned_revision "https://github.com/Comfy-Org/ComfyUI.git" "$VIDEO_UPSCALE_COMFY_DIR" "$COMFYUI_REVISION"
checkout_pinned_revision "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git" "$VIDEO_UPSCALE_SEEDVR2_DIR" "$SEEDVR2_NODE_REVISION"

if (( apply )); then
  [[ -f "$VIDEO_UPSCALE_SEEDVR2_OFFICIAL_CLI" ]] || die "official SeedVR2 CLI missing after clone: $VIDEO_UPSCALE_SEEDVR2_OFFICIAL_CLI"
  [[ -f "$VIDEO_UPSCALE_SEEDVR2_CLI" ]] || die "backend adapter missing: $VIDEO_UPSCALE_SEEDVR2_CLI"
fi

run uv venv --clear --no-python-downloads --python "$python_candidate" "$VIDEO_UPSCALE_COMFY_DIR/.venv"
runtime_lock="$VIDEO_UPSCALE_PROJECT_ROOT/deploy/runtime-requirements.lock"
[[ -f "$runtime_lock" ]] || die "repository-owned runtime dependency lock missing: $runtime_lock"
run uv pip install --reinstall --python "$VIDEO_UPSCALE_PYTHON" --require-hashes -r "$runtime_lock"

extra_paths="$VIDEO_UPSCALE_COMFY_DIR/extra_model_paths.yaml"
if (( apply )); then
  cat > "$extra_paths" <<EOF
# Generated by video-upscale-webui. Runtime-only file, outside Git checkout.
seedvr2:
  base_path: "${VIDEO_UPSCALE_RUNTIME_ROOT}/models"
  seedvr2: "SEEDVR2"
EOF
else
  note "+ write $extra_paths with external SeedVR2 model path"
fi

if (( apply )); then
  "$VIDEO_UPSCALE_PYTHON" -c 'import torch; assert torch.backends.mps.is_built() and torch.backends.mps.is_available(), "MPS unavailable"'
  note "ok: Apple MPS available in isolated SeedVR2 runtime"
fi

download_one_model() {
  local dit_model="$1"
  if (( ! apply )); then
    note "+ Hugging Face resumable download and SHA-validate ${dit_model} plus shared VAE into ${VIDEO_UPSCALE_SEEDVR2_MODEL_DIR}"
    return
  fi
  # The Mac's authenticated local proxy rejects Hugging Face Xet reconstruction
  # requests and truncates long CDN streams. This process-scoped direct route
  # leaves system proxy and Tailscale settings unchanged; fall back is handled
  # by Hugging Face resume plus the checksum below.
  HF_HUB_DISABLE_XET=1 HF_HUB_DOWNLOAD_TIMEOUT=120 \
    HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= NO_PROXY='*' \
    http_proxy= https_proxy= all_proxy= no_proxy='*' \
    "$VIDEO_UPSCALE_PYTHON" - "$VIDEO_UPSCALE_SEEDVR2_DIR" "$VIDEO_UPSCALE_SEEDVR2_MODEL_DIR" "$dit_model" <<'PY'
import hashlib
import sys
import time
from pathlib import Path

node_dir, model_dir, dit_model = sys.argv[1:]
sys.path.insert(0, node_dir)
from huggingface_hub import hf_hub_download
from src.utils.model_registry import DEFAULT_VAE, MODEL_REGISTRY

destination = Path(model_dir)
destination.mkdir(parents=True, exist_ok=True)

for filename in (dit_model, DEFAULT_VAE):
    model_info = MODEL_REGISTRY[filename]
    for attempt in range(1, 6):
        try:
            downloaded = Path(
                hf_hub_download(
                    repo_id=model_info.repo,
                    filename=filename,
                    local_dir=destination,
                )
            )
            break
        except Exception as error:
            if attempt == 5:
                raise
            wait_seconds = attempt * 15
            print(
                f"download attempt {attempt}/5 failed for {filename}: {error}; "
                f"retrying in {wait_seconds}s",
                flush=True,
            )
            time.sleep(wait_seconds)
    with downloaded.open("rb") as file_handle:
        digest = hashlib.file_digest(file_handle, "sha256").hexdigest()
    if digest != model_info.sha256:
        downloaded.unlink(missing_ok=True)
        raise SystemExit(f"SHA-256 validation failed for {filename}")
    print(f"validated: {filename}")

PY
}

if (( download_models )); then
  download_one_model "$VIDEO_UPSCALE_SEEDVR2_3B_MODEL"
  if (( download_7b )); then
    download_one_model "$VIDEO_UPSCALE_SEEDVR2_7B_FP8_MODEL"
  fi
else
  note "models skipped. Install default production model with: scripts/install-runtime.sh --apply --models"
fi

if (( apply && download_models )) && launchctl print "gui/$(id -u)/com.haohanl.video-upscale-webui" >/dev/null 2>&1; then
  # A backend started before models existed uses an unavailable runner by design.
  # Restart the exact app label only after successful model validation.
  launchctl kickstart -k "gui/$(id -u)/com.haohanl.video-upscale-webui"
  note "restarted Video Upscale WebUI after runtime install"
fi

note "runtime installation complete"
