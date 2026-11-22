#!/usr/bin/env bash
# Cut the clip into fMP4 init + 2 s segments, the shape AVAssetWriter's
# .mpeg4AppleHLS profile produces (init.mp4, then 00000.m4s, 00001.m4s, ...).
set -euo pipefail
clip=$1; out=$2
mkdir -p "$out"
ffmpeg -v error -y -i "$clip" -c copy -f hls -hls_time 2 -hls_segment_type fmp4 \
  -hls_list_size 0 -hls_fmp4_init_filename init.mp4 \
  -hls_segment_filename "$out/%05d.m4s" "$out/index.m3u8"
ls "$out"/*.m4s | wc -l
