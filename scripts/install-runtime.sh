#!/bin/zsh
# Install isolated ComfyUI + reviewed SeedVR2 fork outside this Git checkout.

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
SEEDVR2_NODE_REVISION="67a7350959eb077d3184faac7afa5449d8cc30a5"
SEEDVR2_UPSTREAM_REVISION="4490bd1f482e026674543386bb2a4d176da245b9"
SEEDVR2_FORK_REPOSITORY="https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git"
SEEDVR2_UPSTREAM_REPOSITORY="https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git"

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

for tool in uv git ffmpeg ffprobe; do
  require_command "$tool"
done
[[ "$(uname -m)" == "arm64" ]] || die "Apple Silicon arm64 required"
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libx265' || die "ffmpeg lacks libx265 encoder"
require_disk_reserve
python_candidate="$(uv python find --no-python-downloads --system "$VIDEO_UPSCALE_PYTHON_VERSION")" \
  || die "trusted Python $VIDEO_UPSCALE_PYTHON_VERSION is not installed; automatic downloads are disabled"
[[ "$("$python_candidate" -c 'import platform; print(platform.python_version())')" == "$VIDEO_UPSCALE_PYTHON_VERSION" ]] \
  || die "resolved Python does not match VIDEO_UPSCALE_PYTHON_VERSION"

service_was_loaded=0
service_quiesced=0
launchctl_path="$(command -v launchctl)" || die "missing required command: launchctl"
launchagent_domain="gui/$(id -u)/com.haohanl.video-upscale-webui"
launchagent_plist="$HOME/Library/LaunchAgents/com.haohanl.video-upscale-webui.plist"
jobs_database="$VIDEO_UPSCALE_DATA_ROOT/jobs.sqlite3"
legacy_access_token_file="${VIDEO_UPSCALE_DATA_ROOT}/access-token"

report_stopped_service_on_failure() {
  local exit_status=$?
  if (( exit_status != 0 && service_quiesced )); then
    print -u2 -- "runtime update failed; service remains stopped. After inspection, recover with: $launchctl_path bootstrap gui/$(id -u) $launchagent_plist"
  fi
}

if (( apply )); then
  if "$launchctl_path" print "$launchagent_domain" >/dev/null 2>&1; then
    service_was_loaded=1
  fi
  if "$SCRIPT_DIR/runtime-update-gate.py" \
    --database "$jobs_database" \
    --launchctl "$launchctl_path" \
    --domain "$launchagent_domain"; then
    :
  else
    gate_status=$?
    if (( gate_status == 75 )); then
      die "refusing runtime update while a queued or active job exists"
    elif (( gate_status == 76 )); then
      service_quiesced=$service_was_loaded
      trap report_stopped_service_on_failure EXIT
      die "job appeared while stopping service; runtime unchanged"
    fi
    service_quiesced=$service_was_loaded
    trap report_stopped_service_on_failure EXIT
    die "could not quiesce runtime service"
  fi
  service_quiesced=$service_was_loaded
  trap report_stopped_service_on_failure EXIT
fi

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
  local expected_upstream="${4:-}"
  local upstream_revision="${5:-}"

  [[ "$revision" =~ '^[0-9a-f]{40}$' ]] || die "runtime revision must be a literal 40-character SHA"
  if [[ -n "$expected_upstream" ]]; then
    [[ "$upstream_revision" =~ '^[0-9a-f]{40}$' ]] \
      || die "runtime upstream revision must be a literal 40-character SHA"
  fi

  verify_checkout() {
    local actual_origin
    local actual_upstream
    local ignored_python
    local worktree_status
    actual_origin="$(git -C "$destination" remote get-url origin)"
    [[ "$actual_origin" == "$repository" ]] || die "runtime origin mismatch at $destination"
    if [[ -n "$expected_upstream" ]]; then
      actual_upstream="$(git -C "$destination" remote get-url upstream)"
      [[ "$actual_upstream" == "$expected_upstream" ]] \
        || die "runtime upstream mismatch at $destination"
      git -C "$destination" merge-base --is-ancestor "$upstream_revision" "$revision" \
        || die "runtime revision lacks expected upstream ancestry at $destination"
    fi
    [[ "$(git -C "$destination" rev-parse HEAD)" == "$revision" ]] \
      || die "runtime revision mismatch at $destination"
    worktree_status="$(git -C "$destination" status --porcelain --untracked-files=all)"
    [[ -z "$worktree_status" ]] || die "runtime checkout is modified at $destination"
    [[ ! -f "$destination/.gitmodules" ]] || die "runtime checkout has unexpected submodules at $destination"
    if [[ "$repository" == "$SEEDVR2_FORK_REPOSITORY" ]]; then
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
    local actual_origin
    local current_revision
    local ignored_python
    local worktree_status
    worktree_status="$(git -C "$destination" status --porcelain --untracked-files=all)"
    [[ -z "$worktree_status" ]] || die "runtime checkout is modified at $destination"
    [[ ! -f "$destination/.gitmodules" ]] \
      || die "runtime checkout has unexpected submodules at $destination"
    if [[ -n "$expected_upstream" ]]; then
      ignored_python="$(git -C "$destination" ls-files --others --ignored --exclude-standard -- '*.py')"
      [[ -z "$ignored_python" ]] \
        || die "runtime checkout contains ignored Python source at $destination"
    fi
    actual_origin="$(git -C "$destination" remote get-url origin)"
    if [[ "$actual_origin" != "$repository" ]]; then
      if (( update )) && [[ -n "$expected_upstream" && "$actual_origin" == "$expected_upstream" ]]; then
        run git -C "$destination" remote set-url origin "$repository"
      else
        die "runtime origin mismatch at $destination"
      fi
    fi
    if [[ -n "$expected_upstream" ]]; then
      if git -C "$destination" remote get-url upstream >/dev/null 2>&1; then
        local actual_upstream
        actual_upstream="$(git -C "$destination" remote get-url upstream)"
        [[ "$actual_upstream" == "$expected_upstream" ]] \
          || die "runtime upstream mismatch at $destination"
      else
        run git -C "$destination" remote add upstream "$expected_upstream"
      fi
      run git -C "$destination" fetch --depth=1 upstream "$upstream_revision"
      if (( update )); then
        run git -C "$destination" fetch --depth=64 origin "$revision"
      fi
    fi
    current_revision="$(git -C "$destination" rev-parse HEAD)"
    if [[ "$current_revision" == "$revision" ]]; then
      note "pinned Git checkout present: $destination"
    elif (( update )); then
      if [[ -z "$expected_upstream" ]]; then
        run git -C "$destination" fetch --depth=1 origin "$revision"
      fi
      run git -C "$destination" checkout --detach "$revision"
    else
      die "runtime revision mismatch at $destination; re-run with --update to use pinned $revision"
    fi
  elif [[ -e "$destination" ]]; then
    die "refusing to overwrite non-Git path: $destination"
  else
    run git init "$destination"
    run git -C "$destination" remote add origin "$repository"
    if [[ -n "$expected_upstream" ]]; then
      run git -C "$destination" remote add upstream "$expected_upstream"
      run git -C "$destination" fetch --depth=1 upstream "$upstream_revision"
    fi
    if [[ -n "$expected_upstream" ]]; then
      run git -C "$destination" fetch --depth=64 origin "$revision"
    else
      run git -C "$destination" fetch --depth=1 origin "$revision"
    fi
    run git -C "$destination" checkout --detach FETCH_HEAD
  fi
  if (( apply )) || (( ! update )) && [[ -d "$destination/.git" ]]; then
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

# Old releases created this one credential file. Applied updates reach here only
# after runtime-update-gate.py has quiesced the service. Never broaden this path.
run rm -f -- "$legacy_access_token_file"

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
checkout_pinned_revision "$SEEDVR2_FORK_REPOSITORY" "$VIDEO_UPSCALE_SEEDVR2_DIR" \
  "$SEEDVR2_NODE_REVISION" "$SEEDVR2_UPSTREAM_REPOSITORY" "$SEEDVR2_UPSTREAM_REVISION"

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

if (( apply )); then
  if "$SCRIPT_DIR/runtime-update-gate.py" \
    --database "$jobs_database" \
    --launchctl "$launchctl_path" \
    --domain "$launchagent_domain" \
    --check-only; then
    :
  else
    gate_status=$?
    if (( gate_status == 75 )); then
      die "refusing to restart runtime while a queued or active job exists"
    fi
    die "could not verify runtime queue before restart"
  fi
  if (( service_was_loaded )); then
    launchctl bootstrap "gui/$(id -u)" "$launchagent_plist"
    service_was_loaded=0
    service_quiesced=0
    note "restarted Video Upscale WebUI after runtime install"
  fi
  trap - EXIT
fi

note "runtime installation complete"
