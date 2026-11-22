"""Operator commands until the dashboard exists (M7).

    streamrelay-admin pair-code EMAIL [--ttl 600]   one-time code for the app's pairing screen
    streamrelay-admin devices [EMAIL]               list devices
    streamrelay-admin revoke DEVICE_ID              revoke a device token at once
"""
from __future__ import annotations

import argparse
import sys
import uuid

import psycopg

from . import db, pairing
from .config import Settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="streamrelay-admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair-code")
    p.add_argument("email")
    p.add_argument("--ttl", type=int, default=600)
    d = sub.add_parser("devices")
    d.add_argument("email", nargs="?")
    r = sub.add_parser("revoke")
    r.add_argument("device_id", type=uuid.UUID)
    a = ap.parse_args(argv)
    s = Settings.from_env()
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        db.migrate(conn)
        if a.cmd == "pair-code":
            with conn.transaction():
                owner = pairing.ensure_user(conn, a.email)
                code = pairing.create_code(conn, owner, a.ttl)
            print(code)
        elif a.cmd == "devices":
            q = ("SELECT d.id, u.email, d.name, d.platform, d.created_at, d.last_seen_at, d.revoked_at FROM devices d"
                 " JOIN users u ON u.id = d.owner_id" + (" WHERE u.email = %s" if a.email else "") + " ORDER BY d.created_at")
            for row in conn.execute(q, (a.email,) if a.email else ()):  # type: ignore[arg-type]
                print("\t".join("" if v is None else str(v) for v in row))
        elif a.cmd == "revoke":
            n = conn.execute("UPDATE devices SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
                             (a.device_id,)).rowcount
            print("revoked" if n else "no such active device")
            return 0 if n else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
