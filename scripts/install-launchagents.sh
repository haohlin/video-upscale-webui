#!/bin/zsh
# Install per-user LaunchAgents from tracked templates. Default is dry run.

set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/lib.sh"

apply=0
usage() {
  cat <<'EOF'
usage: scripts/install-launchagents.sh [--dry-run|--apply]

Installs per-user service LaunchAgent. Results persist until deleted from WebUI.
No administrator password is needed. Existing legacy cleanup agent is removed.
EOF
}

if (( $# > 0 )); then
  case "$1" in
    --dry-run) apply=0 ;;
    --apply) apply=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
fi
(( $# == 0 )) || die "usage: $0 [--dry-run|--apply]"

load_runtime_config
assert_safe_data_root
require_command launchctl
require_command plutil
user_id="$(id -u)"
launch_dir="$HOME/Library/LaunchAgents"
legacy_cleanup_label="com.haohanl.video-upscale-webui.cleanup"
legacy_cleanup_destination="$launch_dir/${legacy_cleanup_label}.plist"

escape_sed_replacement() {
  print -r -- "$1" | sed 's/[&|]/\\&/g'
}

install_one() {
  local label="$1"
  local template="$2"
  local destination="$launch_dir/${label}.plist"
  local safe_project safe_data
  safe_project="$(escape_sed_replacement "$VIDEO_UPSCALE_PROJECT_ROOT")"
  safe_data="$(escape_sed_replacement "$VIDEO_UPSCALE_DATA_ROOT")"

  if (( ! apply )); then
    note "DRY RUN: render and load $label into gui/${user_id}"
    return
  fi
  mkdir -p "$launch_dir" "$VIDEO_UPSCALE_DATA_ROOT/logs"
  sed -e "s|__PROJECT_ROOT__|${safe_project}|g" -e "s|__DATA_ROOT__|${safe_data}|g" "$template" > "$destination"
  plutil -lint "$destination" >/dev/null
  if launchctl print "gui/${user_id}/${label}" >/dev/null 2>&1; then
    launchctl bootout "gui/${user_id}/${label}"
  fi
  launchctl bootstrap "gui/${user_id}" "$destination"
  launchctl kickstart -k "gui/${user_id}/${label}"
  note "installed $label"
}

if launchctl print "gui/${user_id}/${legacy_cleanup_label}" >/dev/null 2>&1 || [[ -f "$legacy_cleanup_destination" ]]; then
  if (( apply )); then
    if launchctl print "gui/${user_id}/${legacy_cleanup_label}" >/dev/null 2>&1; then
      launchctl bootout "gui/${user_id}/${legacy_cleanup_label}"
    fi
    rm -f "$legacy_cleanup_destination"
    note "removed legacy automatic cleanup agent"
  else
    note "DRY RUN: remove legacy automatic cleanup agent"
  fi
fi

install_one "com.haohanl.video-upscale-webui" "$VIDEO_UPSCALE_PROJECT_ROOT/deploy/com.haohanl.video-upscale-webui.plist.template"
