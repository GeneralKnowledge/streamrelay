"""Migrations and connection helpers.

Migrations are the numbered SQL files in `migrations/`, applied in order and
recorded in `schema_migrations`. (ADR 0007 names Alembic; plain SQL files are
enough until the schema first changes after release.)
"""
from __future__ import annotations

import hashlib
import uuid
from importlib import resources

import psycopg

MIGRATION_LOCK = 0x5352_0001        # advisory lock id so two processes never migrate at once


def migrations() -> list[tuple[str, str]]:
    files = resources.files("streamrelay") / "migrations"
    out = [(f.name, f.read_text()) for f in files.iterdir() if f.name.endswith(".sql")]
    return sorted(out)


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations. Returns the names applied."""
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations"
                     " (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for name, sql in migrations():
            if name in done:
                continue
            conn.execute(sql)  # type: ignore[arg-type]
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
            applied.append(name)
    return applied


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


STATIC_USER = uuid.UUID("00000000-0000-7000-8000-000000000001")
STATIC_DEVICE = uuid.UUID("00000000-0000-7000-8000-000000000002")


def ensure_static_device(conn: psycopg.Connection, token: str) -> None:
    """M1 only: one user and one device whose token comes from SR_STATIC_TOKEN.

    Requests still authenticate by looking the token's hash up in `devices`, the
    same path real paired devices use from M3.
    """
    with conn.transaction():
        conn.execute("INSERT INTO users (id, email, password_hash) VALUES (%s, 'm1@localhost', '!')"
                     " ON CONFLICT (id) DO NOTHING", (STATIC_USER,))
        conn.execute("INSERT INTO devices (id, owner_id, name, platform, token_hash) VALUES (%s, %s, 'M1 static', 'test', %s)"
                     " ON CONFLICT (id) DO UPDATE SET token_hash = EXCLUDED.token_hash, revoked_at = NULL",
                     (STATIC_DEVICE, STATIC_USER, token_hash(token)))
