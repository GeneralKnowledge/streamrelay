#!/usr/bin/env bash
# M1 acceptance test (docs/plan/milestones.md, M1): a 20-minute test clip
# uploaded in real time with three outages (10 s, 60 s, 5 min), skip-to-live,
# checked by tools/verify on an independent recording of the output.
#
#   tests/media-continuity/m1_gaps.sh [work dir]
#
# Takes about 22 minutes. Makes the clip on first use (cached in $M1_CACHE).
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=${1:-$root/data/m1-run}
cache=${M1_CACHE:-$root/data/m1-clip}
if [[ ! -f "$cache/segs/index.m3u8" ]]; then
  mkdir -p "$cache"
  python3 "$root/tools/testclip/make_clip.py" "$cache/clip.mp4" --seconds 1200
  "$root/tools/testclip/segment.sh" "$cache/clip.mp4" "$cache/segs"
fi
# outages start at 120 s, 300 s and 600 s of the broadcast; light protocol faults throughout
"$root/tests/media-continuity/run_upload.sh" "$cache/segs" "$work" --mode skip_to_live \
  --offline 120:10,300:60,600:300 --duplicate 0.05 --corrupt 0.02 --reorder 0.05 --seed 7
python3 "$root/tools/verify/verify.py" "$work" --buffer-target 8 --resume-threshold 10
