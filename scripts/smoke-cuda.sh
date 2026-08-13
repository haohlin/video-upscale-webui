#!/usr/bin/env bash
set -euo pipefail

[[ $# == 1 ]] || { echo "usage: scripts/smoke-cuda.sh 3b-fp8-fast|7b-fp8-quality" >&2; exit 2; }
preset="$1"
case "$preset" in 3b-fp8-fast|7b-fp8-quality) ;; *) echo "unsupported CUDA preset" >&2; exit 2 ;; esac

runtime_env="${VIDEO_UPSCALE_CONFIG_FILE:-/etc/video-upscale-webui/runtime.env}"
[[ -r "$runtime_env" ]] || { echo "private runtime environment missing" >&2; exit 1; }
set -a
source "$runtime_env"
set +a

smoke_root="$(mktemp -d)"
trap 'rm -r "$smoke_root"' EXIT
input="$smoke_root/cuda-smoke.mp4"
result="$smoke_root/result.mp4"
base_url="http://127.0.0.1:${VIDEO_UPSCALE_APP_PORT:-8000}"
auth_header="Tailscale-User-Login: ${VIDEO_UPSCALE_TAILSCALE_USER_LOGIN}"

ffmpeg -hide_banner -loglevel error -f lavfi \
  -i 'testsrc2=size=256x256:rate=5:duration=1' -c:v libx264 -pix_fmt yuv420p "$input"

response="$(curl --fail --silent --show-error -X POST \
  -H "$auth_header" -H 'X-Video-Upscale-Request: 1' \
  -F "video=@${input};type=video/mp4" -F "preset=${preset}" \
  -F 'color_correction=lab' -F 'output_scale=1' "${base_url}/api/jobs")"
job_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$response")"

for _ in {1..360}; do
  response="$(curl --fail --silent --show-error -H "$auth_header" "${base_url}/api/jobs/${job_id}")"
  status="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$response")"
  case "$status" in
    completed) break ;;
    failed|cancelled)
      python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get("error") or data["status"], file=sys.stderr)' <<<"$response"
      exit 1
      ;;
  esac
  sleep 5
done
[[ "$status" == completed ]] || { echo "CUDA smoke job timed out" >&2; exit 1; }

curl --fail --silent --show-error -H "$auth_header" \
  "${base_url}/api/jobs/${job_id}/download" -o "$result"
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height \
  -of default=noprint_wrappers=1 "$result"
printf 'CUDA smoke completed: %s\n' "$preset"
