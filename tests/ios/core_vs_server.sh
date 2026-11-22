#!/usr/bin/env bash
# The iPhone app's upload core (ios/, built as `sr-upload`) against the real
# server through the bad-network proxy, with the API killed -9 once a run.
# Segments join the manifest at recording pace; a new epoch every EPOCH_EVERY
# segments stands in for interruptions. Then every segment in the database is
# checked against the source file and the stored file.
#
#   tests/ios/core_vs_server.sh [work dir] [minutes]
# Needs: Postgres 16, streamrelay-api on PATH, and either `swift` or Docker
# (image with Swift 6 + libsqlite3-dev: SWIFT_IMAGE, default swift-sqlite).
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=${1:-$root/data/ios-core-run}; minutes=${2:-10}
epoch_every=${EPOCH_EVERY:-60}
base=${M1_CACHE:-$root/data/m1-clip}/segs
rm -rf "$work"; mkdir -p "$work"; work=$(realpath "$work")
n=$((minutes * 30))

pids=()
cleanup() { local rc=$?; set +e     # keep the run's own exit status; tidying up can't change it
            [[ -n "${phone:-}" ]] && kill "$phone" 2>/dev/null; [[ -n "${container:-}" ]] && docker kill "$container" >/dev/null 2>&1
            for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done; [[ -n "${api:-}" ]] && kill "$api" 2>/dev/null
            wait 2>/dev/null
            if [[ -n "${pgdir:-}" ]]; then $as_pg "$PG_BIN/pg_ctl -D $pgdir/data -m fast stop" >/dev/null 2>&1; fi
            exit "$rc"; }
trap cleanup EXIT
trap 'exit 1' INT TERM
PG_BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}; pgdir=$(mktemp -d); port=${PGPORT_IOS:-5496}
as_pg="sh -c"; if [[ $(id -u) == 0 ]]; then chown postgres "$pgdir"; as_pg="su postgres -c"; fi
$as_pg "$PG_BIN/initdb -D $pgdir/data -A trust -U postgres" > "$work/postgres.log" 2>&1
$as_pg "$PG_BIN/pg_ctl -D $pgdir/data -o '-p $port -k /tmp' -l $pgdir/log -w start" >> "$work/postgres.log" 2>&1
export SR_DATABASE_URL="postgresql://postgres@127.0.0.1:$port/postgres"
export SR_STORE_DIR="$work/store" SR_PORT=${SR_PORT:-8082} SR_STORAGE_FREE_MIN_BYTES=${SR_STORAGE_FREE_MIN_BYTES:-500000000}
proxy_port=${PROXY_PORT:-8092}
start_api() { local deadline=$(( $(date +%s) + 10 ))
              # a server left over from another run would answer the health check in this one's place
              while curl -s -o /dev/null "http://127.0.0.1:$SR_PORT/v1/health"; do
                (( $(date +%s) > deadline )) && { echo "port $SR_PORT is already in use"; exit 1; }
                sleep 0.2
              done
              streamrelay-api >> "$work/api.log" 2>&1 & api=$!
              deadline=$(( $(date +%s) + 60 ))
              until curl -sf "http://127.0.0.1:$SR_PORT/v1/health" > /dev/null; do
                if ! kill -0 "$api" 2>/dev/null || (( $(date +%s) > deadline )); then
                  echo "API did not start:"; tail -20 "$work/api.log"; exit 1
                fi
                sleep 0.2
              done; }
start_api
code=$(streamrelay-admin pair-code ios-core@localhost)
python3 "$root/tools/chaos/proxy.py" --listen "$proxy_port" --upstream "127.0.0.1:$SR_PORT" --log "$work/proxy.jsonl" \
  --online-min 40 --online-max 120 --reset-every 30 --seed 23 & pids+=($!)

args=(--api "http://127.0.0.1:$proxy_port" --pair "$code" --segments "$base" --limit "$n" --realtime
      --epoch-every "$epoch_every" --mode catch_up --work "$work/phone")
mkdir -p "$work/phone"
if command -v swift > /dev/null; then
  (cd "$root/ios" && swift build --product sr-upload > "$work/build.log" 2>&1)
  "$root/ios/.build/debug/sr-upload" "${args[@]}" > "$work/broadcast_id" 2> "$work/phone.log" & phone=$!
else
  container=sr-upload-$$
  docker run --rm --name "$container" --network host -v "$root/ios:/src" -v "$base:$base:ro" -v "$work:$work" -w /src "${SWIFT_IMAGE:-swift-sqlite}" \
    bash -c "swift build --product sr-upload > $work/build.log 2>&1 && .build/debug/sr-upload ${args[*]}" \
    > "$work/broadcast_id" 2> "$work/phone.log" & phone=$!
fi
echo "ios core run: $minutes min of segments, new epoch every $epoch_every"
killed=0; started=$(date +%s)
while kill -0 $phone 2>/dev/null; do
  sleep 2
  if (( !killed && $(date +%s) - started >= minutes * 30 )); then      # halfway: kill -9 the API
    kill -9 "$api"; wait "$api" 2>/dev/null || true
    echo "{\"wall\": $(date +%s.%N), \"event\": \"api_killed\"}" >> "$work/kills.jsonl"
    start_api; killed=1
  fi
done
uploader=0; wait $phone || uploader=$?
echo "uploader: exit $uploader"; phone=
verdict=0
python3 - "$work" "$base" "$n" "$epoch_every" <<'PY' || verdict=$?
import hashlib, json, os, sys
import psycopg
work, base, n, every = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
bid = open(os.path.join(work, "broadcast_id")).read().strip()
problems = []
with psycopg.connect(os.environ["SR_DATABASE_URL"]) as c:
    rows = {(e, s): (bytes(h).hex(), k, st, du, f) for e, s, h, k, st, du, f in c.execute(
        "SELECT epoch, sequence, sha256, storage_key, start_us, duration_us, size_bytes FROM segments WHERE broadcast_id = %s", (bid,))}
    ends = dict(c.execute("SELECT epoch, last_sequence FROM epochs WHERE broadcast_id = %s", (bid,)).fetchall())
    state, fin = c.execute("SELECT state, finish_requested_at IS NOT NULL FROM broadcasts WHERE id = %s", (bid,)).fetchone()
start = 0
for i in range(n):
    key = (i // every, i % every)
    data = open(os.path.join(base, f"{i:05d}.m4s"), "rb").read()
    want = hashlib.sha256(data).hexdigest()
    row = rows.pop(key, None)
    if row is None:
        problems.append(f"{key}: not stored"); continue
    if row[0] != want:
        problems.append(f"{key}: stored checksum differs from the source")
    disk = open(os.path.join(os.environ["SR_STORE_DIR"], row[1]), "rb").read()
    if hashlib.sha256(disk).hexdigest() != want:
        problems.append(f"{key}: file on disk differs")
    if row[2] != start:
        problems.append(f"{key}: start_us {row[2]} != {start}")
    start += row[3]
problems += [f"{k}: stored but never recorded" for k in rows]
epochs = (n - 1) // every + 1
for e in range(epochs):
    last = min(every, n - e * every) - 1
    if ends.get(e) != last:
        problems.append(f"epoch {e}: end {ends.get(e)} != {last}")
if not fin:
    problems.append("finish never arrived")
summary = {"broadcast_id": bid, "segments": n, "epochs": epochs, "state": state, "finish_requested": fin,
           "problems": problems}
json.dump(summary, open(os.path.join(work, "check.json"), "w"), indent=1)
print(json.dumps({k: v for k, v in summary.items() if k != "problems"}), f"problems: {len(problems)}")
for p in problems[:20]:
    print("  ", p)
sys.exit(1 if problems else 0)
PY
(( uploader == 0 && verdict == 0 )) || { echo "FAIL (uploader $uploader, server check $verdict)"; exit 1; }
echo PASS
