"""Fixtures: a fresh database per test on the server named by SR_TEST_DATABASE_URL.

Locally: start any PostgreSQL 16 and point SR_TEST_DATABASE_URL at it, e.g.
postgresql://postgres@127.0.0.1:5499/postgres. CI uses a service container.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from streamrelay.api.main import create_app
from streamrelay.config import Settings

ADMIN_URL = os.environ.get("SR_TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5499/postgres")
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
    return Settings(database_url=database_url, store_dir=str(tmp_path / "store"), static_token=TOKEN)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        yield c
