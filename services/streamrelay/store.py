"""Segment store: the durable write sequence and the startup scan.

Protocol v1, server rules: a write is acknowledged only after the bytes are
fsynced, renamed into place, the directory fsynced and the row committed. Writes
to one key are serialised by a transaction-scoped advisory lock, and a stored
file is never replaced.

The body is received into a private temp file *before* the lock is taken, so a
slow upload never holds a database transaction open; everything that decides
the outcome (row check, rename, insert, commit) happens under the lock.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import psycopg

TMP = ".tmp"


class Rejected(Exception):
    """A request the protocol answers with a 4xx status."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass
class Received:
    path: str
    size: int
    sha256: bytes


@dataclass
class SegmentMeta:
    broadcast_id: uuid.UUID
    epoch: int
    sequence: int
    init_id: int
    start_us: int
    duration_us: int
    final: bool


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(os.path.join(self.root, TMP), exist_ok=True)
        # Test hook: runs after the rename and before the commit.
        self.after_rename: Callable[[], Awaitable[None]] | None = None

    # ---- paths -------------------------------------------------------------
    def path(self, key: str) -> str:
        return os.path.join(self.root, key)

    @staticmethod
    def init_key(bid: uuid.UUID, init_id: int) -> str:
        return f"{bid}/inits/{init_id}.mp4"

    @staticmethod
    def segment_key(bid: uuid.UUID, epoch: int, seq: int) -> str:
        return f"{bid}/{epoch}/{seq}.m4s"

    # ---- receiving ---------------------------------------------------------
    async def receive(self, body: AsyncIterator[bytes], limit: int, expected_len: int | None) -> Received:
        """Stream a request body to a temp file, hashing as it goes, then fsync it."""
        path = os.path.join(self.root, TMP, f"{uuid.uuid4().hex}.part")
        h, size = hashlib.sha256(), 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            async for chunk in body:
                size += len(chunk)
                if size > limit:
                    raise Rejected(422, f"body over max_segment_bytes ({limit})")
                h.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
            if expected_len is not None and size != expected_len:
                raise Rejected(422, f"body is {size} bytes, Content-Length said {expected_len}")
            await asyncio.to_thread(os.fsync, fd)
        except BaseException:
            os.close(fd)
            os.unlink(path)
            raise
        os.close(fd)
        return Received(path, size, h.digest())

    def discard(self, rec: Received) -> None:
        try:
            os.unlink(rec.path)
        except FileNotFoundError:
            pass

    async def _place(self, rec: Received, key: str) -> str:
        final = self.path(key)
        d = os.path.dirname(final)
        os.makedirs(d, exist_ok=True)
        os.rename(rec.path, final)
        await asyncio.to_thread(_fsync_dir, d)
        return final

    # ---- durable writes ----------------------------------------------------
    async def put_init(self, conn: psycopg.AsyncConnection, bid: uuid.UUID, init_id: int, rec: Received) -> int:
        key = self.init_key(bid, init_id)
        final = None
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"init:{bid}:{init_id}",))
                cur = await conn.execute("SELECT sha256 FROM inits WHERE broadcast_id = %s AND init_id = %s",
                                         (bid, init_id))
                row = await cur.fetchone()
                if row is not None:
                    self.discard(rec)
                    if bytes(row[0]) == rec.sha256:
                        return 200
                    raise Rejected(409, "a different init segment is already stored for this init_id")
                final = await self._place(rec, key)
                if self.after_rename:
                    await self.after_rename()
                await conn.execute(
                    "INSERT INTO inits (broadcast_id, init_id, sha256, size_bytes, storage_key, codec)"
                    " VALUES (%s, %s, %s, %s, %s, 'unprobed')", (bid, init_id, rec.sha256, rec.size, key))
            return 201
        except Exception:
            # An ordinary failure after the rename: the row was not committed, so the
            # file must not stay. (A process crash here is handled by the startup scan.)
            if final:
                _unlink_quiet(final)
            self.discard(rec)
            raise

    async def put_segment(self, conn: psycopg.AsyncConnection, m: SegmentMeta, rec: Received) -> int:
        key = self.segment_key(m.broadcast_id, m.epoch, m.sequence)
        final = None
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                                   (f"seg:{m.broadcast_id}:{m.epoch}:{m.sequence}",))
                cur = await conn.execute(
                    "SELECT sha256 FROM segments WHERE broadcast_id = %s AND epoch = %s AND sequence = %s",
                    (m.broadcast_id, m.epoch, m.sequence))
                row = await cur.fetchone()
                if row is not None:
                    self.discard(rec)
                    if bytes(row[0]) == rec.sha256:
                        return 200
                    raise Rejected(409, "a different segment is already stored under this key")
                await self._check_segment(conn, m)
                final = await self._place(rec, key)
                if self.after_rename:
                    await self.after_rename()
                await conn.execute(
                    "INSERT INTO segments (broadcast_id, epoch, sequence, init_id, start_us, duration_us,"
                    " size_bytes, sha256, storage_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (m.broadcast_id, m.epoch, m.sequence, m.init_id, m.start_us, m.duration_us,
                     rec.size, rec.sha256, key))
            return 201
        except Exception:
            if final:
                _unlink_quiet(final)
            self.discard(rec)
            raise

    async def _check_segment(self, conn: psycopg.AsyncConnection, m: SegmentMeta) -> None:
        cur = await conn.execute("SELECT 1 FROM inits WHERE broadcast_id = %s AND init_id = %s",
                                 (m.broadcast_id, m.init_id))
        if await cur.fetchone() is None:
            raise Rejected(412, f"init segment {m.init_id} not uploaded yet")
        await conn.execute("INSERT INTO epochs (broadcast_id, epoch) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                           (m.broadcast_id, m.epoch))
        cur = await conn.execute("SELECT last_sequence FROM epochs WHERE broadcast_id = %s AND epoch = %s",
                                 (m.broadcast_id, m.epoch))
        row = await cur.fetchone()
        if row and row[0] is not None and m.sequence > row[0]:
            raise Rejected(422, f"sequence {m.sequence} is after this epoch's end ({row[0]})")
        # start_us monotonic within the epoch, against the nearest stored neighbours (±50 ms jitter)
        cur = await conn.execute(
            "(SELECT sequence, start_us, duration_us FROM segments WHERE broadcast_id = %s AND epoch = %s"
            "  AND sequence < %s ORDER BY sequence DESC LIMIT 1)"
            " UNION ALL "
            "(SELECT sequence, start_us, duration_us FROM segments WHERE broadcast_id = %s AND epoch = %s"
            "  AND sequence > %s ORDER BY sequence LIMIT 1)",
            (m.broadcast_id, m.epoch, m.sequence) * 2)
        for seq, start, dur in await cur.fetchall():
            if seq < m.sequence and m.start_us < start + dur - 50_000:
                raise Rejected(422, f"start_us goes backwards against sequence {seq}")
            if seq > m.sequence and start < m.start_us + m.duration_us - 50_000:
                raise Rejected(422, f"start_us goes backwards against sequence {seq}")
        await conn.execute(
            "UPDATE epochs SET first_start_us = %s WHERE broadcast_id = %s AND epoch = %s"
            " AND (first_start_us IS NULL OR first_start_us > %s)",
            (m.start_us, m.broadcast_id, m.epoch, m.start_us))

    # ---- startup scan ------------------------------------------------------
    def scan(self, conn: psycopg.Connection) -> dict[str, int]:
        """Delete temp files and stored files with no committed row.

        Runs to completion before the API accepts any PUT, so it never races a
        live write. A file with a committed row is never deleted.
        """
        removed_tmp = removed_orphans = kept = 0
        tmp = os.path.join(self.root, TMP)
        for name in os.listdir(tmp):
            os.unlink(os.path.join(tmp, name))
            removed_tmp += 1
        known = {r[0] for r in conn.execute("SELECT storage_key FROM segments UNION ALL SELECT storage_key FROM inits")}
        for dirpath, dirnames, filenames in os.walk(self.root):
            if os.path.relpath(dirpath, self.root) == TMP:
                dirnames.clear()
                continue
            for name in filenames:
                full = os.path.join(dirpath, name)
                key = os.path.relpath(full, self.root)
                if key in known:
                    kept += 1
                else:
                    os.unlink(full)
                    _fsync_dir(dirpath)
                    removed_orphans += 1
        return {"removed_tmp": removed_tmp, "removed_orphans": removed_orphans, "kept": kept}


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
