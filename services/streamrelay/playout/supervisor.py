"""Playout supervisor: one playout process per active broadcast.

Polls Postgres for broadcasts that are not finished, failed or deleted and
starts `python -m streamrelay.playout.engine --broadcast <id>` for each. A
process that exits with an error is restarted (it resumes from its checkpoint)
with a growing delay; one that exits cleanly has finished its broadcast.
LISTEN/NOTIFY can replace the polling later; one second is plenty for M1.

    streamrelay-playout [--log-dir DIR]
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
import uuid

import psycopg

from ..config import Settings

log = logging.getLogger("streamrelay.playout.supervisor")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default=os.environ.get("SR_PLAYOUT_LOG_DIR"))
    ap.add_argument("--once", type=uuid.UUID, help="run only this broadcast, then exit when it finishes")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = Settings.from_env()
    procs: dict[uuid.UUID, subprocess.Popen[bytes]] = {}
    restarts: dict[uuid.UUID, tuple[int, float]] = {}     # id -> (count, not before)
    done: set[uuid.UUID] = set()
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            with psycopg.connect(s.database_url, autocommit=True) as conn:
                rows = conn.execute("SELECT id FROM broadcasts WHERE state NOT IN ('finished', 'failed')"
                                    " AND deleted_at IS NULL ORDER BY created_at").fetchall()
        except psycopg.Error as e:
            log.warning("database: %s", e)
            time.sleep(2)
            continue
        active = {r[0] for r in rows}
        if a.once:
            active &= {a.once}
        for bid, p in list(procs.items()):
            rc = p.poll()
            if rc is None:
                continue
            del procs[bid]
            if rc == 0:
                log.info("%s: playout finished", bid)
                done.add(bid)
            else:
                n, _ = restarts.get(bid, (0, 0.0))
                delay = min(30.0, 2.0 ** n)
                restarts[bid] = (n + 1, time.time() + delay)
                log.warning("%s: playout exited with %s; restarting in %.0f s", bid, rc, delay)
        if a.once and a.once in done:
            return 0
        for bid in active - procs.keys() - done:
            if restarts.get(bid, (0, 0.0))[1] > time.time():
                continue
            cmd = [sys.executable, "-m", "streamrelay.playout.engine", "--broadcast", str(bid)]
            if a.log_dir:
                os.makedirs(a.log_dir, exist_ok=True)
                cmd += ["--log", os.path.join(a.log_dir, f"{bid}.jsonl")]
            log.info("%s: starting playout", bid)
            procs[bid] = subprocess.Popen(cmd)
        time.sleep(1)
    for p in procs.values():
        p.terminate()
    for p in procs.values():
        p.wait(10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
