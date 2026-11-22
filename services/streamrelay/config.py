"""Server settings, from environment variables (prefix SR_)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    store_dir: str
    static_token: str | None          # M1: one pre-provisioned device token; pairing arrives in M3
    max_segment_bytes: int = 8 * 1024 * 1024
    playout_delay_target_ms: int = 8000
    resume_threshold_ms: int = 10000
    drain_wait_s: int = 900
    rtmp_url_template: str = "rtmp://127.0.0.1:1935/live/{broadcast_id}"

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ
        return cls(
            database_url=e.get("SR_DATABASE_URL", "postgresql://streamrelay@127.0.0.1:5432/streamrelay"),
            store_dir=e.get("SR_STORE_DIR", "./data/store"),
            static_token=e.get("SR_STATIC_TOKEN") or None,
            max_segment_bytes=int(e.get("SR_MAX_SEGMENT_BYTES", 8 * 1024 * 1024)),
            playout_delay_target_ms=int(e.get("SR_PLAYOUT_DELAY_TARGET_MS", 8000)),
            resume_threshold_ms=int(e.get("SR_RESUME_THRESHOLD_MS", 10000)),
            drain_wait_s=int(e.get("SR_DRAIN_WAIT_S", 900)),
            rtmp_url_template=e.get("SR_RTMP_URL_TEMPLATE", "rtmp://127.0.0.1:1935/live/{broadcast_id}"),
        )
