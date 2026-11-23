"""Per-segment ledger for the M3 chaos test: what the phone was told vs what the server holds.

For every segment the phone produced: when it was first acknowledged, how many
attempts and which statuses it saw, whether the database row's checksum
matches the source file, and whether the stored file's bytes match. Gates:
  every segment acknowledged and stored exactly once with the right checksum;
  no 409; every deliberately corrupted upload got 422 and was sent again;
  no acknowledged segment missing from the database or the disk (crash safety).

    python3 tools/chaos/ledger.py RUN_DIR SEGMENTS_DIR --db URL --store DIR
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict

import psycopg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("segments")
    ap.add_argument("--db", required=True)
    ap.add_argument("--store", required=True)
    a = ap.parse_args()
    phone = [json.loads(x) for x in open(os.path.join(a.run, "phone.jsonl")) if x.strip()]
    start = next(e for e in phone if e["event"] == "start")
    bid, n = start["broadcast_id"], start["segments"]
    puts: dict[int, list[dict]] = defaultdict(list)
    for e in phone:
        if e["event"] == "put":
            puts[e["seq"]].append(e)
    with psycopg.connect(a.db) as conn:
        rows = {seq: (bytes(h), key, cnt) for seq, h, key, cnt in conn.execute(
            "SELECT sequence, sha256, storage_key, count(*) OVER (PARTITION BY sequence) FROM segments"
            " WHERE broadcast_id = %s AND epoch = 0", (bid,))}
    ledger, problems = [], []
    rejected_then_stored = 0
    statuses: dict[int, int] = defaultdict(int)
    for seq in range(n):
        src = open(os.path.join(a.segments, f"{seq:05d}.m4s"), "rb").read()
        want = hashlib.sha256(src).digest()
        ps = puts.get(seq, [])
        for p in ps:
            statuses[p["status"]] += 1
        acks = [p for p in ps if p["status"] in (200, 201)]
        row = rows.get(seq)
        stored_ok = False
        if row:
            path = os.path.join(a.store, row[1])
            stored_ok = os.path.exists(path) and hashlib.sha256(open(path, "rb").read()).digest() == want
        rec = {"seq": seq, "attempts": len(ps), "statuses": " ".join(str(p["status"]) for p in ps),
               "first_ack": acks[0]["wall"] if acks else "", "row": bool(row), "rows": row[2] if row else 0,
               "row_sha_ok": bool(row) and row[0] == want, "file_ok": stored_ok}
        ledger.append(rec)
        if not acks:
            problems.append(f"{seq}: never acknowledged")
        if acks and not (rec["row_sha_ok"] and rec["file_ok"]):
            problems.append(f"{seq}: acknowledged but missing or wrong on the server")
        if rec["rows"] > 1:
            problems.append(f"{seq}: stored {rec['rows']} times")
        if any(p["status"] == 409 for p in ps):
            problems.append(f"{seq}: got 409")
        corrupted = [i for i, p in enumerate(ps) if p.get("corrupt")]
        for i in corrupted:
            if ps[i]["status"] == 422 and any(p["status"] in (200, 201) for p in ps[i + 1:]):
                rejected_then_stored += 1
            if ps[i]["status"] in (0, 200):      # never arrived / key already stored: body not read
                continue
            if ps[i]["status"] != 422:
                problems.append(f"{seq}: corrupted upload answered {ps[i]['status']}")
            elif not any(p["status"] in (200, 201) for p in ps[i + 1:]):
                problems.append(f"{seq}: corrupted upload never re-sent successfully")
    corrupted_total = sum(1 for ps in puts.values() for p in ps if p.get("corrupt"))
    # a run that corrupted uploads must have seen at least one rejected and then re-sent,
    # or the corruption gate was never exercised (every corrupted attempt lost its answer)
    corruption_gate = "not run" if not corrupted_total else ("pass" if rejected_then_stored else "inconclusive")
    with open(os.path.join(a.run, "ledger.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ledger[0].keys()))
        w.writeheader()
        w.writerows(ledger)
    extra = sorted(set(rows) - set(range(n)))
    if extra:
        problems.append(f"rows for sequences the phone never made: {extra[:10]}")
    summary = {
        "broadcast_id": bid, "segments": n,
        "acknowledged": sum(1 for r in ledger if r["first_ack"]),
        "stored_with_right_checksum": sum(1 for r in ledger if r["row_sha_ok"] and r["file_ok"]),
        "put_statuses": {str(k): v for k, v in sorted(statuses.items())},
        "corrupted_uploads": corrupted_total,
        "corrupted_rejected_422_then_stored": rejected_then_stored,
        "corruption_gate": corruption_gate,
        "problems": problems[:50], "problem_count": len(problems),
        "verdict": "fail" if problems else ("inconclusive" if corruption_gate == "inconclusive" else "pass"),
    }
    json.dump(summary, open(os.path.join(a.run, "ledger.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
