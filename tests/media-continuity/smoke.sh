#!/usr/bin/env bash
# Short continuity check for CI (about 2 minutes): a 60 s clip with one 12 s
# outage, the same harness and verify gates as m1_gaps.sh.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=${1:-$root/data/smoke-run}
cache=$root/data/smoke-clip
if [[ ! -f "$cache/segs/index.m3u8" ]]; then
  mkdir -p "$cache"
  python3 "$root/tools/testclip/make_clip.py" "$cache/clip.mp4" --seconds 60
  "$root/tools/testclip/segment.sh" "$cache/clip.mp4" "$cache/segs"
fi
"$root/tests/media-continuity/run_upload.sh" "$cache/segs" "$work" --offline 20:12 --duplicate 0.1 --corrupt 0.05 --seed 3
python3 "$root/tools/verify/verify.py" "$work"
