"""Device pairing: one-time codes, device tokens, revocation (protocol v1, Authentication).

A pairing code is 8 characters from an alphabet without look-alikes, valid
for 10 minutes and once. The device token is 256 random bits, returned once
and stored only as its SHA-256.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import psycopg

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8


def normalise(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


def code_hash(code: str) -> bytes:
    return hashlib.sha256(("pair:" + normalise(code)).encode()).digest()


def new_code() -> str:
    c = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    return f"{c[:4]}-{c[4:]}"


def new_token() -> str:
    return secrets.token_urlsafe(32)          # 256 bits


def ensure_user(conn: psycopg.Connection, email: str) -> uuid.UUID:
    row = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
    if row:
        return row[0]
    uid = uuid.uuid4()
    conn.execute("INSERT INTO users (id, email, password_hash) VALUES (%s, %s, '!')", (uid, email))
    return uid


def create_code(conn: psycopg.Connection, owner: uuid.UUID, ttl_s: int = 600) -> str:
    code = new_code()
    conn.execute("INSERT INTO pairing_codes (code_hash, owner_id, expires_at) VALUES (%s, %s, %s)",
                 (code_hash(code), owner, datetime.now(UTC) + timedelta(seconds=ttl_s)))
    return code
