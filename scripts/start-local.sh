#!/bin/zsh
# Start only loopback-bound app and optional ComfyUI process.

set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/lib.sh"

dry_run=0
with_comfy=0
backend_only=0
foreground=0

usage() {
  cat <<'EOF'
usage: scripts/start-local.sh [--dry-run] [--with-comfy|--backend-only] [--foreground]

Default starts backend only. --with-comfy additionally starts local ComfyUI on
127.0.0.1. Tailscale Serve is configured separately and proxies backend only.
--foreground is for LaunchAgent: ComfyUI backgrounds, backend replaces script.
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --with-comfy) with_comfy=1 ;;
    --backend-only) backend_only=1 ;;
    --foreground) foreground=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

(( ! (with_comfy && backend_only) )) || die "--with-comfy and --backend-only conflict"
load_runtime_config
require_disk_reserve

[[ -x "$VIDEO_UPSCALE_BACKEND_PYTHON" ]] || die "backend Python missing: $VIDEO_UPSCALE_BACKEND_PYTHON"
[[ -d "$VIDEO_UPSCALE_PROJECT_ROOT/backend" ]] || die "backend directory missing: $VIDEO_UPSCALE_PROJECT_ROOT/backend"
[[ -f "$VIDEO_UPSCALE_PROJECT_ROOT/frontend/package.json" ]] || die "frontend package missing"
require_command npm
require_command node
node "$VIDEO_UPSCALE_PROJECT_ROOT/scripts/verify-frontend-lock.mjs"

if (( with_comfy )); then
  [[ -x "$VIDEO_UPSCALE_PYTHON" ]] || die "SeedVR2 Python missing: $VIDEO_UPSCALE_PYTHON"
  [[ -f "$VIDEO_UPSCALE_COMFY_DIR/main.py" ]] || die "ComfyUI missing: $VIDEO_UPSCALE_COMFY_DIR"
fi

if (( dry_run )); then
  note "DRY RUN: backend binds 127.0.0.1:${VIDEO_UPSCALE_APP_PORT}"
  note "DRY RUN: npm build frontend before backend starts"
  if (( with_comfy )); then
    note "DRY RUN: ComfyUI binds 127.0.0.1:${VIDEO_UPSCALE_COMFY_PORT}"
  fi
  exit 0
fi

assert_safe_data_root
mkdir -p "$VIDEO_UPSCALE_DATA_ROOT/logs" "$VIDEO_UPSCALE_DATA_ROOT/run"

backend_pid_file="$VIDEO_UPSCALE_DATA_ROOT/run/backend.pid"
comfy_pid_file="$VIDEO_UPSCALE_DATA_ROOT/run/comfyui.pid"

build_frontend() {
  (
    cd "$VIDEO_UPSCALE_PROJECT_ROOT/frontend"
    npm ci --ignore-scripts
    npm run build
    [[ -f dist/index.html ]] || die "frontend build did not create dist/index.html"
  )
}

assert_backend_port_is_owned() {
  local existing_pids
  existing_pids="$(listener_pids "$VIDEO_UPSCALE_APP_PORT" || true)"
  [[ -z "$existing_pids" ]] && return
  require_loopback_listener_or_unused "$VIDEO_UPSCALE_APP_PORT"
  [[ -f "$backend_pid_file" ]] || die "port ${VIDEO_UPSCALE_APP_PORT} is occupied by an untracked process"
  local expected_pid
  expected_pid="$(<"$backend_pid_file")"
  [[ "$existing_pids" == "$expected_pid" ]] || die "port ${VIDEO_UPSCALE_APP_PORT} is occupied by a non-app process"
  ps -p "$expected_pid" -o command= | grep -Fq "$VIDEO_UPSCALE_BACKEND_MODULE" || die "port ${VIDEO_UPSCALE_APP_PORT} process is not Video Upscale backend"
}

start_backend() {
  if is_port_listening "$VIDEO_UPSCALE_APP_PORT"; then
    assert_backend_port_is_owned
    note "backend already listening on 127.0.0.1:${VIDEO_UPSCALE_APP_PORT}"
    return
  fi
  (
    cd "$VIDEO_UPSCALE_PROJECT_ROOT/backend"
    exec nohup "$VIDEO_UPSCALE_BACKEND_PYTHON" -m uvicorn "$VIDEO_UPSCALE_BACKEND_MODULE" \
      --host 127.0.0.1 --port "$VIDEO_UPSCALE_APP_PORT" --no-access-log
  ) >> "$VIDEO_UPSCALE_DATA_ROOT/logs/backend.log" 2>&1 &
  print -r -- "$!" > "$backend_pid_file"
  note "started backend pid $(cat "$backend_pid_file") on 127.0.0.1:${VIDEO_UPSCALE_APP_PORT}"
}

start_comfy() {
  if is_port_listening "$VIDEO_UPSCALE_COMFY_PORT"; then
    note "ComfyUI already listening on 127.0.0.1:${VIDEO_UPSCALE_COMFY_PORT}"
    return
  fi
  (
    cd "$VIDEO_UPSCALE_COMFY_DIR"
    exec nohup "$VIDEO_UPSCALE_PYTHON" main.py --listen 127.0.0.1 --port "$VIDEO_UPSCALE_COMFY_PORT" --disable-auto-launch
  ) >> "$VIDEO_UPSCALE_DATA_ROOT/logs/comfyui.log" 2>&1 &
  print -r -- "$!" > "$comfy_pid_file"
  note "started ComfyUI pid $(cat "$comfy_pid_file") on 127.0.0.1:${VIDEO_UPSCALE_COMFY_PORT}"
}

if (( foreground )); then
  build_frontend
  (( with_comfy )) && start_comfy
  if is_port_listening "$VIDEO_UPSCALE_APP_PORT"; then
    require_loopback_listener_or_unused "$VIDEO_UPSCALE_APP_PORT"
    die "backend port ${VIDEO_UPSCALE_APP_PORT} already in use; refusing LaunchAgent foreground start"
  fi
  cd "$VIDEO_UPSCALE_PROJECT_ROOT/backend"
  exec "$VIDEO_UPSCALE_BACKEND_PYTHON" -m uvicorn "$VIDEO_UPSCALE_BACKEND_MODULE" \
    --host 127.0.0.1 --port "$VIDEO_UPSCALE_APP_PORT" --no-access-log
fi

build_frontend
start_backend
(( with_comfy )) && start_comfy
