#!/bin/zsh
# Configure private Tailscale Serve. Never enables Funnel or public internet.

set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/lib.sh"

apply=0
replace=0

usage() {
  cat <<'EOF'
usage: scripts/setup-tailscale-serve.sh [--dry-run|--apply] [--replace]

Configures configured Tailscale HTTPS port to proxy only local backend 127.0.0.1:8000.
Refuses to replace an existing Serve configuration unless --replace is explicit.
Never invokes Funnel or exposes public internet.
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run) apply=0 ;;
    --apply) apply=1 ;;
    --replace) replace=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

load_runtime_config
require_command tailscale

status_json="$(tailscale status --json 2>/dev/null)" || die "Tailscale is not running; open Tailscale and sign in first"
print -r -- "$status_json" | grep -q '"BackendState"[[:space:]]*:[[:space:]]*"Running"' || die "Tailscale backend is not running"

serve_json="$(tailscale serve status --json 2>/dev/null || true)"
has_existing_https=0
if print -r -- "$serve_json" | grep -Eq "\"${VIDEO_UPSCALE_TAILSCALE_HTTPS_PORT}\"[[:space:]]*:"; then
  has_existing_https=1
fi

if (( has_existing_https && ! replace )); then
  die "Tailscale Serve already uses HTTPS/${VIDEO_UPSCALE_TAILSCALE_HTTPS_PORT}; inspect with 'tailscale serve status' or re-run with --replace"
fi

if (( ! apply )); then
  note "DRY RUN. This will keep service private to tailnet devices."
  note "+ tailscale serve --https=${VIDEO_UPSCALE_TAILSCALE_HTTPS_PORT} --bg http://127.0.0.1:${VIDEO_UPSCALE_APP_PORT}"
  exit 0
fi

# Scope is exact HTTPS port used by this app. Serve is tailnet-only; do not use
# Funnel and do not reset other hosted services.
tailscale serve --https="$VIDEO_UPSCALE_TAILSCALE_HTTPS_PORT" --bg "http://127.0.0.1:${VIDEO_UPSCALE_APP_PORT}"

note "private Tailscale Serve enabled"
tailscale serve status
