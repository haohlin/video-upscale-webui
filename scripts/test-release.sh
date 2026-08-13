#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -x "$project_root/backend/.venv/bin/python" ]]; then
  exec "$project_root/backend/.venv/bin/python" "$project_root/scripts/run-release-tests.py" "$@"
fi

command -v uv >/dev/null 2>&1 || {
  printf '%s\n' "release gate failed: backend environment missing and uv unavailable" >&2
  exit 2
}
exec uv run --project "$project_root/backend" --group dev \
  python "$project_root/scripts/run-release-tests.py" "$@"
