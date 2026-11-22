#!/usr/bin/env bash
# M3 chaos test (docs/plan/milestones.md, M3): the fake phone uploads MINUTES of
# real segments (default 120) in real time through a bad-network proxy (random
# 5-120 s outages, 0-10% loss as mid-request resets, 300 ms latency, uplink
# capped at 3 then 6 Mbps, random resets) while the API is killed with -9 every
# KILL_EVERY seconds (default 1200) and restarted. Duplicates, reordering and
# one corrupted upload in 100 on top. tools/chaos/ledger.py then checks every
# segment against the database and the disk.
#
#   tests/chaos/m3_chaos.sh [work dir] [minutes]
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=${1:-$root/data/chaos-run}; minutes=${2:-120}
kill_every=${KILL_EVERY:-1200}
base=${M1_CACHE:-$root/data/m1-clip}/segs
if [[ ! -f "$base/index.m3u8" ]]; then
  mkdir -p "$(dirname "$base")"
  python3 "$root/tools/testclip/make_clip.py" "$(dirname "$base")/clip.mp4" --seconds 1200
  "$root/tools/testclip/segment.sh" "$(dirname "$base")/clip.mp4" "$base"
fi
rm -rf "$work"; mkdir -p "$work/segs"; work=$(realpath "$work")
# MINUTES of segments: the 20-minute clip's segments repeated
n=$((minutes * 30)); have=$(ls "$base"/*.m4s | wc -l)
cp "$base/init.mp4" "$work/segs/"
{ echo "#EXTM3U"; for ((i = 0; i < n; i++)); do
    ln -s "$base/$(printf %05d $((i % have))).m4s" "$work/segs/$(printf %05d $i).m4s"; echo "#EXTINF:2.000000,"; echo "x"
  done; } > "$work/segs/index.m3u8"

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; [[ -n "${api:-}" ]] && kill "$api" 2>/dev/null
            wait 2>/dev/null || true
            if [[ -n "${pgdir:-}" ]]; then $as_pg "$PG_BIN/pg_ctl -D $pgdir/data -m fast stop" >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT
if [[ -z "${SR_DATABASE_URL:-}" ]]; then
  PG_BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}; pgdir=$(mktemp -d); port=${PGPORT_CHAOS:-5497}
  as_pg="sh -c"; if [[ $(id -u) == 0 ]]; then chown postgres "$pgdir"; as_pg="su postgres -c"; fi
  $as_pg "$PG_BIN/initdb -D $pgdir/data -A trust -U postgres" > "$work/postgres.log" 2>&1
  $as_pg "$PG_BIN/pg_ctl -D $pgdir/data -o '-p $port -k /tmp' -l $pgdir/log -w start" >> "$work/postgres.log" 2>&1
  export SR_DATABASE_URL="postgresql://postgres@127.0.0.1:$port/postgres"
fi
export SR_STORE_DIR="$work/store" SR_PORT=${SR_PORT:-8081} SR_STORAGE_FREE_MIN_BYTES=${SR_STORAGE_FREE_MIN_BYTES:-500000000}
start_api() { streamrelay-api >> "$work/api.log" 2>&1 & api=$!
              until curl -sf "http://127.0.0.1:$SR_PORT/v1/health" > /dev/null; do sleep 0.2; done; }
start_api
code=$(streamrelay-admin pair-code chaos@localhost)
python3 "$root/tools/chaos/proxy.py" --listen 8091 --upstream "127.0.0.1:$SR_PORT" --log "$work/proxy.jsonl" & pids+=($!)
python3 "$root/tools/fake_phone/fake_phone.py" --api http://127.0.0.1:8091 --pair "$code" --segments "$work/segs" \
  --mode catch_up --duplicate 0.05 --reorder 0.05 --corrupt 0.01 --seed 5 --log "$work/phone.jsonl" > "$work/phone.out" 2>&1 & phone=$!
echo "chaos run: $minutes min of segments, API killed every $kill_every s"
last_kill=$(date +%s)
while kill -0 $phone 2>/dev/null; do
  sleep 5
  if (( $(date +%s) - last_kill >= kill_every )); then
    kill -9 "$api"; wait "$api" 2>/dev/null || true
    echo "{\"wall\": $(date +%s.%N), \"event\": \"api_killed\"}" >> "$work/kills.jsonl"
    start_api; last_kill=$(date +%s)
  fi
done
wait $phone || { echo "fake phone failed:"; tail -5 "$work/phone.out"; }
python3 "$root/tools/chaos/ledger.py" "$work" "$work/segs" --db "$SR_DATABASE_URL" --store "$SR_STORE_DIR"
