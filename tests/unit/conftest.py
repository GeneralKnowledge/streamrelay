"""Fixtures: a fresh database per test on the server named by SR_TEST_DATABASE_URL.

Locally: start any PostgreSQL 16 and point SR_TEST_DATABASE_URL at it, e.g.
postgresql://postgres@127.0.0.1:5499/postgres. CI uses a service container.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from streamrelay.api.main import create_app
from streamrelay.config import Settings

ADMIN_URL = os.environ.get("SR_TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5499/postgres")
ROOT_DIR = Path(__file__).resolve().parents[2]
TOKEN = "test-token-" + "a" * 32


@pytest.fixture
def database_url() -> Iterator[str]:
    name = f"sr_test_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"no PostgreSQL at SR_TEST_DATABASE_URL ({e})")
    admin.execute(f"CREATE DATABASE {name}")
    base, _, _ = ADMIN_URL.rpartition("/")
    yield f"{base}/{name}"
    admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
    admin.close()


@pytest.fixture
def settings(database_url: str, tmp_path) -> Settings:
    return Settings(database_url=database_url, store_dir=str(tmp_path / "store"), static_token=TOKEN,
                    validate_media=False, max_active_broadcasts=100, storage_free_min_bytes=0)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        yield c


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> Path:
    """6 s of the test clip cut into init + three 2 s segments, plus a wrong-codec init."""
    d = tmp_path_factory.mktemp("media")
    subprocess.run(["python3", str(ROOT_DIR / "tools/testclip/make_clip.py"), str(d / "clip.mp4"), "--seconds", "6"], check=True)
    subprocess.run([str(ROOT_DIR / "tools/testclip/segment.sh"), str(d / "clip.mp4"), str(d / "segs")], check=True,
                   capture_output=True)
    (d / "mp4v").mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2",
                    "-c:v", "mpeg4", "-f", "hls", "-hls_time", "2", "-hls_segment_type", "fmp4",
                    "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", str(d / "mp4v/%05d.m4s"),
                    str(d / "mp4v/index.m3u8")], check=True)
    return d / "segs"
