#!/usr/bin/env bash
set -euo pipefail

root="${VIDEO_UPSCALE_PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
runtime_env="${VIDEO_UPSCALE_CONFIG_FILE:-/etc/video-upscale-webui/runtime.env}"

command -v nvidia-smi >/dev/null || { echo "nvidia-smi unavailable" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg unavailable" >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe unavailable" >&2; exit 1; }

IFS=, read -r gpu_name gpu_memory < <(
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits | head -n 1
)
gpu_name="${gpu_name# }"; gpu_name="${gpu_name% }"
gpu_memory="${gpu_memory// /}"
[[ "$gpu_name" == "NVIDIA GeForce RTX 4090" ]] || { echo "RTX 4090 unavailable" >&2; exit 1; }
[[ "$gpu_memory" =~ ^[0-9]+$ && "$gpu_memory" -ge 23000 ]] || { echo "24GB-class VRAM unavailable" >&2; exit 1; }
[[ -r "$runtime_env" ]] || { echo "private runtime environment missing" >&2; exit 1; }

set -a
source "$runtime_env"
set +a
"$VIDEO_UPSCALE_PYTHON" - <<'PY'
import torch
assert torch.cuda.is_available(), "CUDA unavailable"
assert torch.cuda.device_count() == 1, "expected one CUDA GPU"
assert "4090" in torch.cuda.get_device_name(0), "RTX 4090 unavailable"
PY

for model in \
  "$VIDEO_UPSCALE_SEEDVR2_3B_MODEL" \
  "$VIDEO_UPSCALE_SEEDVR2_7B_FP8_MODEL" \
  "$VIDEO_UPSCALE_SEEDVR2_VAE_MODEL"; do
  [[ -f "$VIDEO_UPSCALE_SEEDVR2_MODEL_DIR/$model" ]] || {
    echo "required SeedVR2 model missing: $model" >&2
    exit 1
  }
done

[[ "$(git -C "$root" status --porcelain --untracked-files=all)" == "" ]] || {
  echo "repository is modified" >&2; exit 1;
}
printf 'CUDA backend preflight passed\n'
