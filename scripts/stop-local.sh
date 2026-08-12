#!/bin/zsh
# Stop only processes previously recorded by start-local.sh. Default is dry run.

set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/lib.sh"

apply=0
if (( $# > 0 )); then
  case "$1" in
    --dry-run) apply=0 ;;
    --apply) apply=1 ;;
    -h|--help) print -- "usage: $0 [--dry-run|--apply]"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
fi
(( $# == 0 )) || die "usage: $0 [--dry-run|--apply]"

load_runtime_config
assert_safe_data_root

for component in backend comfyui; do
  pid_file="$VIDEO_UPSCALE_DATA_ROOT/run/${component}.pid"
  [[ -f "$pid_file" ]] || continue
  pid="$(<"$pid_file")"
  [[ "$pid" == <-> ]] || die "invalid PID file: $pid_file"
  if ! kill -0 "$pid" 2>/dev/null; then
    if (( apply )); then
      rm -f "$pid_file"
    else
      note "DRY RUN: would remove stale PID file $pid_file"
    fi
    continue
  fi
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command_line" == *"VideoUpscaleWebUI"* || "$command_line" == *"video-upscale-webui"* ]] || die "refusing to stop unexpected PID $pid"
  if (( apply )); then
    kill -TERM "$pid"
    rm -f "$pid_file"
    note "stopped $component pid $pid"
  else
    note "DRY RUN: would stop $component pid $pid"
  fi
done
