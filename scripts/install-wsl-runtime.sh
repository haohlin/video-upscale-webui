#!/usr/bin/env bash
set -euo pipefail

apply=0
models=0
with_7b=0
while (($#)); do
  case "$1" in
    --dry-run) apply=0 ;;
    --apply) apply=1 ;;
    --models) models=1 ;;
    --with-7b) with_7b=1 ;;
    *) echo "unknown argument" >&2; exit 2 ;;
  esac
  shift
done
(( ! with_7b || models )) || { echo "--with-7b requires --models" >&2; exit 2; }

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VIDEO_UPSCALE_STATE_ROOT="/var/lib/video-upscale-webui"
VIDEO_UPSCALE_SEEDVR2_MODEL_DIR="${VIDEO_UPSCALE_STATE_ROOT}/models/SEEDVR2"
COMFYUI_DIR="${VIDEO_UPSCALE_STATE_ROOT}/runtime/ComfyUI"
SEEDVR2_DIR="${COMFYUI_DIR}/custom_nodes/ComfyUI-SeedVR2_VideoUpscaler"
COMFYUI_REVISION="e651b7bef55a5376343dcb1c0edb79f0142c985e"
SEEDVR2_NODE_REVISION="67a7350959eb077d3184faac7afa5449d8cc30a5"
SEEDVR2_UPSTREAM_REVISION="4490bd1f482e026674543386bb2a4d176da245b9"
SEEDVR2_FORK_REPOSITORY="https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git"
SERVICE_NAME="video-upscale-webui.service"
VIDEO_UPSCALE_PYTHON_VERSION="3.13.12"
UV_VERSION="0.10"

if project_owner="$(stat -c '%U' "$PROJECT_ROOT" 2>/dev/null)"; then :; else
  project_owner="$(stat -f '%Su' "$PROJECT_ROOT")"
fi
project_owner_home="$(python3 -c 'import pwd,sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)' "$project_owner")"
UV_BIN="${VIDEO_UPSCALE_UV:-}"
if [[ -z "$UV_BIN" ]]; then
  for candidate in "$(command -v uv 2>/dev/null || true)" /home/linuxbrew/.linuxbrew/bin/uv \
    "$project_owner_home/.local/bin/uv"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then UV_BIN="$candidate"; break; fi
  done
fi
[[ -x "$UV_BIN" ]] || { echo "uv 0.10 is required; set VIDEO_UPSCALE_UV to an existing executable" >&2; exit 1; }
[[ "$($UV_BIN --version)" == "uv ${UV_VERSION}."* ]] \
  || { echo "uv ${UV_VERSION}.x is required" >&2; exit 1; }

PYTHON_SOURCE="${VIDEO_UPSCALE_PYTHON_SOURCE:-}"
if [[ -z "$PYTHON_SOURCE" ]]; then
  for candidate in "$(command -v python3.13 2>/dev/null || true)" \
    "$project_owner_home/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13" \
    /home/linuxbrew/.linuxbrew/bin/python3.13; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then PYTHON_SOURCE="$candidate"; break; fi
  done
fi
if [[ ! -x "$PYTHON_SOURCE" ]]; then
  run_python_install=1
  PYTHON_SOURCE="${VIDEO_UPSCALE_STATE_ROOT}/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13"
else
  run_python_install=0
  [[ "$($PYTHON_SOURCE -c 'import platform; print(platform.python_version())')" == "$VIDEO_UPSCALE_PYTHON_VERSION" ]] \
    || { echo "Python $VIDEO_UPSCALE_PYTHON_VERSION is required; set VIDEO_UPSCALE_PYTHON_SOURCE" >&2; exit 1; }
fi

if ((apply)); then
  [[ "$(id -u)" == 0 ]] || { echo "--apply must run as root" >&2; exit 1; }
  apt-get update
  apt-get install -y curl ffmpeg git python3 rsync
  id video-upscale >/dev/null 2>&1 || useradd --system --home-dir "$VIDEO_UPSCALE_STATE_ROOT" --shell /usr/sbin/nologin video-upscale
fi

run() { if ((apply)); then "$@"; else printf '+ '; printf '%q ' "$@"; printf '\n'; fi; }
checkout() {
  local repository="$1" destination="$2" revision="$3"
  if [[ -d "$destination/.git" ]]; then
    [[ -z "$(git -C "$destination" status --porcelain --untracked-files=all)" ]] || { echo "runtime checkout modified" >&2; exit 1; }
    run git -C "$destination" fetch --depth=64 origin "$revision"
  else
    run git init "$destination"
    run git -C "$destination" remote add origin "$repository"
    run git -C "$destination" fetch --depth=64 origin "$revision"
  fi
  run git -C "$destination" checkout --detach "$revision"
}

active_job_count() {
  local database="${VIDEO_UPSCALE_STATE_ROOT}/data/jobs.sqlite3"
  [[ -f "$database" ]] || { echo 0; return; }
  python3 - "$database" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1], timeout=30) as connection:
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'preflight', 'running')"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        count = 0
print(count)
PY
}

service_was_active=0
if ((apply)); then
  [[ "$(active_job_count)" == 0 ]] || {
    echo "refusing runtime update while a queued or active job exists" >&2
    exit 1
  }
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    service_was_active=1
    systemctl stop "$SERVICE_NAME"
  fi
  [[ "$(active_job_count)" == 0 ]] || {
    echo "refusing runtime update because a job appeared while stopping the service" >&2
    exit 1
  }
fi

run install -d -m 0750 -o video-upscale -g video-upscale \
  "$VIDEO_UPSCALE_STATE_ROOT" "$VIDEO_UPSCALE_STATE_ROOT/data" \
  "$VIDEO_UPSCALE_STATE_ROOT/runtime" "$VIDEO_UPSCALE_SEEDVR2_MODEL_DIR"
run install -d -m 0755 /opt/video-upscale-webui /etc/video-upscale-webui
run rsync -a --delete --exclude .git --exclude .venv --exclude node_modules \
  --exclude __pycache__ --exclude deploy/runtime.env "$PROJECT_ROOT/" /opt/video-upscale-webui/
if ((run_python_install)); then
  run env UV_PYTHON_INSTALL_DIR="$VIDEO_UPSCALE_STATE_ROOT/python" \
    "$UV_BIN" python install "$VIDEO_UPSCALE_PYTHON_VERSION"
fi
run "$UV_BIN" venv --clear --python "$PYTHON_SOURCE" "$VIDEO_UPSCALE_STATE_ROOT/backend-venv"
run "$UV_BIN" pip install --python "$VIDEO_UPSCALE_STATE_ROOT/backend-venv/bin/python" \
  --require-hashes -r "/opt/video-upscale-webui/backend/requirements.lock"
checkout "https://github.com/Comfy-Org/ComfyUI.git" "$COMFYUI_DIR" "$COMFYUI_REVISION"
checkout "$SEEDVR2_FORK_REPOSITORY" "$SEEDVR2_DIR" "$SEEDVR2_NODE_REVISION"

if ((apply)); then
  git -C "$SEEDVR2_DIR" remote get-url upstream >/dev/null 2>&1 || git -C "$SEEDVR2_DIR" remote add upstream https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git
  git -C "$SEEDVR2_DIR" fetch --depth=1 upstream "$SEEDVR2_UPSTREAM_REVISION"
  git -C "$SEEDVR2_DIR" merge-base --is-ancestor "$SEEDVR2_UPSTREAM_REVISION" "$SEEDVR2_NODE_REVISION"
fi
run "$UV_BIN" venv --clear --python "$PYTHON_SOURCE" "$COMFYUI_DIR/.venv"
run "$UV_BIN" pip install --python "$COMFYUI_DIR/.venv/bin/python" --torch-backend cu128 \
  --require-hashes -r "/opt/video-upscale-webui/deploy/runtime-requirements.cuda.lock"

download_model() {
  local model="$1"
  ((apply)) || { echo "+ download and sha256 validate $model"; return; }
  "$COMFYUI_DIR/.venv/bin/python" - "$SEEDVR2_DIR" "$VIDEO_UPSCALE_SEEDVR2_MODEL_DIR" "$model" <<'PY'
import hashlib, sys
from pathlib import Path
node, destination, filename = sys.argv[1:]
sys.path.insert(0, node)
from huggingface_hub import hf_hub_download
from src.utils.model_registry import DEFAULT_VAE, MODEL_REGISTRY
for item in (filename, DEFAULT_VAE):
    metadata = MODEL_REGISTRY[item]
    path = Path(hf_hub_download(repo_id=metadata.repo, filename=item, local_dir=destination))
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != metadata.sha256:
        path.unlink(missing_ok=True)
        raise SystemExit("model sha256 mismatch")
PY
}
if ((models)); then
  download_model seedvr2_ema_3b_fp8_e4m3fn.safetensors
  ((with_7b)) && download_model seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors
fi

run install -m 0644 "$PROJECT_ROOT/deploy/video-upscale-webui.service" /etc/systemd/system/video-upscale-webui.service
run systemctl daemon-reload
run systemctl enable video-upscale-webui.service
if ((apply)); then
  [[ -r /etc/video-upscale-webui/runtime.env ]] || {
    echo "runtime installed; private /etc/video-upscale-webui/runtime.env is required before start" >&2
    exit 3
  }
  chown root:video-upscale /etc/video-upscale-webui/runtime.env
  chmod 0640 /etc/video-upscale-webui/runtime.env
  if ((service_was_active)); then
    systemctl start "$SERVICE_NAME"
  fi
fi
echo "WSL runtime installation complete; run scripts/check-cuda-system.sh before starting the service"
