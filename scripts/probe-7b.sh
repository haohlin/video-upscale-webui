#!/bin/zsh
# Explicit experimental SeedVR2 7B FP8 probe; never called by normal jobs.

set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/lib.sh"

apply=0
input=""

usage() {
  cat <<'EOF'
usage: scripts/probe-7b.sh [--dry-run|--apply] --input /absolute/path/video.mp4

Creates a first-10-second, maximum-480p source probe and runs SeedVR2 7B FP8
at 2x. It does not use BlockSwap, memory overflow, or automatic 3B fallback.
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run) apply=0 ;;
    --apply) apply=1 ;;
    --input)
      shift
      (( $# > 0 )) || die "--input needs a path"
      input="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[[ -n "$input" ]] || die "--input is required"
require_absolute_path input "$input"
[[ -f "$input" ]] || die "input file not found: $input"

load_runtime_config
require_disk_reserve

probe_root="$VIDEO_UPSCALE_DATA_ROOT/staging/7b-probe"
probe_input="$probe_root/input-10s-480p.mp4"
probe_output="$VIDEO_UPSCALE_DATA_ROOT/results/seedvr2-7b-fp8-probe.mp4"

if (( ! apply )); then
  note "DRY RUN. 7B probe is experimental; it may OOM on 48 GB unified memory."
  note "+ ffmpeg -t 10 -vf scale='if(gt(iw,ih),-2,min(480,iw))':'if(gt(iw,ih),min(480,ih),-2)' ..."
  note "+ SeedVR2 7B FP8: batch 5, chunk 25, overlap 4, tiled VAE, 2x target"
  exit 0
fi

[[ -x "$VIDEO_UPSCALE_PYTHON" ]] || die "SeedVR2 Python missing: $VIDEO_UPSCALE_PYTHON"
[[ -f "$VIDEO_UPSCALE_SEEDVR2_OFFICIAL_CLI" ]] || die "official SeedVR2 CLI missing: $VIDEO_UPSCALE_SEEDVR2_OFFICIAL_CLI"
require_command ffmpeg
require_command ffprobe
assert_safe_data_root
mkdir -p "$probe_root" "$VIDEO_UPSCALE_DATA_ROOT/results"

metadata="$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "$input")"
width="${metadata%,*}"
height="${metadata#*,}"
[[ "$width" == <-> && "$height" == <-> ]] || die "could not read source dimensions"
short_side=$(( width < height ? width : height ))
target_short_side=$(( short_side * 2 ))
(( target_short_side <= 960 )) || target_short_side=960

# Create only a bounded test segment. Source file remains untouched.
ffmpeg -y -hide_banner -loglevel error -i "$input" -t 10 \
  -vf "scale='if(gt(iw,ih),-2,min(480,iw))':'if(gt(iw,ih),min(480,ih),-2)'" \
  -map 0:v:0 -an -c:v libx264 -pix_fmt yuv420p "$probe_input"

"$VIDEO_UPSCALE_PYTHON" "$VIDEO_UPSCALE_SEEDVR2_OFFICIAL_CLI" "$probe_input" \
  --output "$probe_output" --output_format mp4 --video_backend ffmpeg --10bit \
  --model_dir "$VIDEO_UPSCALE_SEEDVR2_MODEL_DIR" \
  --dit_model "$VIDEO_UPSCALE_SEEDVR2_7B_FP8_MODEL" \
  --resolution "$target_short_side" --batch_size 5 --uniform_batch_size \
  --chunk_size 25 --temporal_overlap 4 --color_correction lab \
  --vae_encode_tiled --vae_decode_tiled

note "7B probe completed: $probe_output"
