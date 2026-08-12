#!/bin/zsh
# Remove expired app-owned media and logs. Default is safe dry run.

set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/lib.sh"

apply=0
retention_hours=24

usage() {
  cat <<'EOF'
usage: scripts/cleanup-data.sh [--dry-run|--apply] [--older-than-hours HOURS]

Deletes media, logs, and database rows only for terminal jobs older than the
retention threshold. Queued, preflight, and running jobs are always preserved.
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run) apply=0 ;;
    --apply) apply=1 ;;
    --older-than-hours)
      shift
      (( $# > 0 )) || die "--older-than-hours needs a value"
      retention_hours="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[[ "$retention_hours" == <-> ]] || die "retention hours must be a non-negative integer"
load_runtime_config
assert_safe_data_root
[[ -x "$VIDEO_UPSCALE_BACKEND_PYTHON" ]] || die "backend Python missing: $VIDEO_UPSCALE_BACKEND_PYTHON"

cleanup_args=(--older-than-hours "$retention_hours")
(( apply )) && cleanup_args+=(--apply)
(
  cd "$VIDEO_UPSCALE_PROJECT_ROOT/backend"
  "$VIDEO_UPSCALE_BACKEND_PYTHON" -m app.cleanup "${cleanup_args[@]}"
)
