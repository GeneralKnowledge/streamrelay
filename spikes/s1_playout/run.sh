#!/usr/bin/env bash
# One S1 run: MediaMTX, feeder, playout and an independent recorder.
# Usage: run.sh <segments dir> <work dir> <speed> <duration s> [gap start] [gap len]
set -euo pipefail
segs=$1; work=$2; speed=$3; dur=$4; gs=${5:-120}; gl=${6:-180}
here=$(cd "$(dirname "$0")" && pwd)
mtx=${MEDIAMTX:-mediamtx}
rm -rf "$work"; mkdir -p "$work/in"
"$mtx" "$here/mediamtx.yml" > "$work/mediamtx.log" 2>&1 & mtx_pid=$!
trap 'kill $mtx_pid 2>/dev/null || true' EXIT
sleep 1
t0=$(python3 -c 'import time; print(time.time() + 2)')
python3 "$here/feeder.py" "$segs" "$work/in" --log "$work/feeder.jsonl" --t0 "$t0" \
  --gap-start "$gs" --gap-len "$gl" --duration "$dur" & feed_pid=$!
python3 "$here/playout.py" "$work/in" --speed "$speed" --duration "$((dur + 10))" \
  --log "$work/playout.jsonl" --t0 "$t0" 2> "$work/playout.err" & play_pid=$!
# Independent viewer: record what MediaMTX serves, warnings to reader.log.
until grep -q "is publishing to path 's1'" "$work/mediamtx.log"; do sleep 0.1; done
ffmpeg -nostdin -v warning -i rtmp://127.0.0.1:1935/s1 -c copy -f flv "$work/rec.flv" 2> "$work/reader.log" & rec_pid=$!
wait $play_pid; wait $feed_pid || true
sleep 2; kill -INT $rec_pid 2>/dev/null || true; wait $rec_pid 2>/dev/null || true
echo "run done: $work"
