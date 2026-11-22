"""Server limits: per-device rate limit and storage caps (storage-and-security.md)."""
from __future__ import annotations

import os
import time
import uuid

import psycopg

from .store import Rejected


class RateLimiter:
    """Token bucket per device, in memory (one API process in v1)."""

    def __init__(self, per_second: float, burst: float | None = None):
        self.rate = per_second
        self.burst = burst if burst is not None else per_second * 2
        self.buckets: dict[uuid.UUID, tuple[float, float]] = {}

    def take(self, device: uuid.UUID) -> None:
        now = time.monotonic()
        tokens, last = self.buckets.get(device, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[device] = (tokens, now)
            raise Rejected(429, "rate limit", retry_after=max(1, int((1 - tokens) / self.rate) + 1))
        self.buckets[device] = (tokens - 1, now)


class StorageGuard:
    """507 when stored media would pass SR_STORAGE_MAX_BYTES or free space drops below SR_STORAGE_FREE_MIN_BYTES.

    The stored total is read from the database at most once a second and then
    kept up to date in memory as writes land.
    """

    def __init__(self, root: str, max_bytes: int | None, free_min_bytes: int):
        self.root, self.max_bytes, self.free_min = root, max_bytes, free_min_bytes
        self.used: int | None = None
        self.read_at = 0.0

    async def refresh(self, conn: psycopg.AsyncConnection) -> None:
        if self.used is None or time.monotonic() - self.read_at > 1.0:
            cur = await conn.execute(
                "SELECT coalesce((SELECT sum(size_bytes) FROM segments), 0) + coalesce((SELECT sum(size_bytes) FROM inits), 0)")
            row = await cur.fetchone()
            self.used = int(row[0]) if row else 0
            self.read_at = time.monotonic()

    def free_bytes(self) -> int:
        st = os.statvfs(self.root)
        return st.f_bavail * st.f_frsize

    async def check(self, conn: psycopg.AsyncConnection, incoming: int) -> None:
        await self.refresh(conn)
        if self.max_bytes is not None and (self.used or 0) + incoming > self.max_bytes:
            raise Rejected(507, "server storage cap reached", retry_after=30)
        if self.free_bytes() - incoming < self.free_min:
            raise Rejected(507, "server disk nearly full", retry_after=30)

    def stored(self, n: int) -> None:
        if self.used is not None:
            self.used += n
