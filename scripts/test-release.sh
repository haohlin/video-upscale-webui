#!/bin/zsh
set -euo pipefail

project_root="${0:A:h:h}"
if [[ -x "$project_root/backend/.venv/bin/python" ]]; then
  exec "$project_root/backend/.venv/bin/python" "$project_root/scripts/run-release-tests.py" "$@"
fi

command -v uv >/dev/null 2>&1 || {
  print -u2 "release gate failed: backend environment missing and uv unavailable"
  exit 2
}
exec uv run --project "$project_root/backend" --group dev \
  python "$project_root/scripts/run-release-tests.py" "$@"
