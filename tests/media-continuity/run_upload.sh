#!/usr/bin/env bash
# One media-continuity run on this machine: Postgres, MediaMTX, the API, the
# playout supervisor, the fake phone and an independent viewer (ffmpeg -c copy
# reading what MediaMTX serves). Then tools/verify checks the recording.
#
#   run_upload.sh <segments dir> <work dir> [fake-phone options...]
#
# Needs: python package installed (pip install -e .), ffmpeg, mediamtx on PATH
# (or MEDIAMTX=...), and either SR_DATABASE_URL pointing at an empty database
# or PostgreSQL binaries (PG_BIN, default /usr/lib/postgresql/16/bin) to start
# a throwaway cluster.
set -euo pipefail
segs=$(realpath "$1"); work=$2; shift 2
root=$(cd "$(dirname "$0")/../.." && pwd)
rm -rf "$work"; mkdir -p "$work"; work=$(realpath "$work")
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true
            if [[ -n "${pgdir:-}" ]]; then $as_pg "$PG_BIN/pg_ctl -D $pgdir/data -m fast stop" >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT

# Postgres
if [[ -z "${SR_DATABASE_URL:-}" ]]; then
  PG_BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}
  pgdir=$(mktemp -d); port=${PGPORT_M1:-5498}
  as_pg="sh -c"; if [[ $(id -u) == 0 ]]; then chown postgres "$pgdir"; as_pg="su postgres -c"; fi
  $as_pg "$PG_BIN/initdb -D $pgdir/data -A trust -U postgres" > "$work/postgres.log" 2>&1
  $as_pg "$PG_BIN/pg_ctl -D $pgdir/data -o '-p $port -k /tmp' -l $pgdir/log -w start" >> "$work/postgres.log" 2>&1
  export SR_DATABASE_URL="postgresql://postgres@127.0.0.1:$port/postgres"
fi
export SR_STORE_DIR="$work/store" SR_STATIC_TOKEN=${SR_STATIC_TOKEN:-m1-$(od -An -tx1 -N16 /dev/urandom | tr -d " \n")}
export SR_RTMP_URL_TEMPLATE="rtmp://127.0.0.1:1935/live/{broadcast_id}" SR_PORT=${SR_PORT:-8080}

# MediaMTX (the receiver; its log counts publish sessions)
${MEDIAMTX:-mediamtx} "$root/deploy/mediamtx.yml" > "$work/mediamtx.log" 2>&1 & pids+=($!)
# API, then the playout supervisor
streamrelay-api > "$work/api.log" 2>&1 & pids+=($!)
until curl -sf "http://127.0.0.1:$SR_PORT/v1/health" > /dev/null; do sleep 0.2; done
streamrelay-playout --log-dir "$work/playout" > "$work/playout.log" 2>&1 & pids+=($!)

# The phone
python3 "$root/tools/fake_phone/fake_phone.py" --api "http://127.0.0.1:$SR_PORT" --token "$SR_STATIC_TOKEN" \
  --segments "$segs" --log "$work/phone.jsonl" "$@" > "$work/phone.out" 2>&1 & phone=$!
until [[ -s "$work/phone.out" ]]; do sleep 0.1; kill -0 $phone 2>/dev/null || { cat "$work/phone.out"; exit 1; }; done
bid=$(head -1 "$work/phone.out")
echo "broadcast $bid"

# Independent viewer: starts as soon as the playout publishes
until grep -q "is publishing to path 'live/$bid'" "$work/mediamtx.log"; do sleep 0.1; done
ffmpeg -nostdin -v warning -i "rtmp://127.0.0.1:1935/live/$bid" -c copy -f flv "$work/rec.flv" 2> "$work/reader.log" & rec=$!
echo "viewer started; uploading in real time"

wait $phone || { echo "fake phone failed"; tail "$work/phone.out"; exit 1; }
echo "phone done; waiting for the playout to finish"
until grep -q "$bid: playout finished" "$work/playout.log"; do sleep 1; done
# the viewer ends by itself when the publisher closes; give it 15 s
for _ in $(seq 150); do kill -0 $rec 2>/dev/null || break; sleep 0.1; done
kill -INT $rec 2>/dev/null || true; wait $rec 2>/dev/null || true
cp "$work/playout/$bid.jsonl" "$work/playout.jsonl"
psql "$SR_DATABASE_URL" -Atc "SELECT play_result, count(*) FROM segments GROUP BY 1 ORDER BY 1" > "$work/segments_played.txt"
psql "$SR_DATABASE_URL" -Atc "SELECT at, detail FROM events WHERE kind = 'state_change' ORDER BY id" > "$work/state_events.txt"
echo "run done: $work"
