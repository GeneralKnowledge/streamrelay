"""Protocol v1 subset: idempotency, checksums, ordering rules, epoch end, finish, status."""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from streamrelay.api.main import create_app
from streamrelay.config import Settings
from streamrelay.store import SegmentMeta, Store

INIT = b"\x00\x00\x00\x18ftypiso6" + b"init" * 100
S = 2_000_000


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def create(c: TestClient, ref: uuid.UUID | None = None, mode: str = "skip_to_live") -> str:
    r = c.post("/v1/broadcasts", json={"title": "t", "client_broadcast_ref": str(ref or uuid.uuid4()),
                                       "catch_up": {"mode": mode}})
    assert r.status_code in (200, 201), r.text
    return r.json()["broadcast_id"]


def put_init(c: TestClient, bid: str, body: bytes = INIT, init_id: int = 0) -> int:
    return c.put(f"/v1/broadcasts/{bid}/inits/{init_id}", content=body,
                 headers={"X-SR-SHA256": sha(body), "Content-Type": "video/mp4"}).status_code


def put_seg(c: TestClient, bid: str, seq: int, body: bytes | None = None, epoch: int = 0, *, digest: str | None = None,
            start: int | None = None, dur: int = S, init: int = 0, final: bool = False):
    body = body if body is not None else f"segment {epoch}/{seq}".encode() * 1000
    h = {"X-SR-SHA256": digest or sha(body), "X-SR-Init": str(init),
         "X-SR-Start-Us": str(start if start is not None else seq * S), "X-SR-Duration-Us": str(dur)}
    if final:
        h["X-SR-Flags"] = "final"
    return c.put(f"/v1/broadcasts/{bid}/segments/{epoch}/{seq}", content=body, headers=h)


def test_create_is_idempotent(client: TestClient):
    ref = uuid.uuid4()
    a = client.post("/v1/broadcasts", json={"title": "t", "client_broadcast_ref": str(ref)})
    b = client.post("/v1/broadcasts", json={"title": "t", "client_broadcast_ref": str(ref)})
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json()["broadcast_id"] == b.json()["broadcast_id"]
    assert a.json()["max_segment_bytes"] == 8 * 1024 * 1024
    assert uuid.UUID(a.json()["broadcast_id"]).version == 7


def test_auth_required(client: TestClient):
    r = client.post("/v1/broadcasts", json={"title": "t", "client_broadcast_ref": str(uuid.uuid4())},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_unknown_broadcast_is_404(client: TestClient):
    assert put_seg(client, str(uuid.uuid4()), 0).status_code == 404
    assert client.get(f"/v1/broadcasts/{uuid.uuid4()}/status").status_code == 404


def test_init_idempotency(client: TestClient):
    bid = create(client)
    assert put_init(client, bid) == 201
    assert put_init(client, bid) == 200
    assert put_init(client, bid, INIT + b"x") == 409


def test_segment_201_200_409(client: TestClient, settings: Settings):
    bid = create(client)
    put_init(client, bid)
    body = b"A" * 100_000
    assert put_seg(client, bid, 0, body).status_code == 201
    assert put_seg(client, bid, 0, body).status_code == 200
    other = b"B" * 100_000
    assert put_seg(client, bid, 0, other).status_code == 409
    # the stored file was not touched by the 409
    with open(os.path.join(settings.store_dir, Store.segment_key(uuid.UUID(bid), 0, 0)), "rb") as f:
        assert f.read() == body


def test_checksum_mismatch_is_422_and_stores_nothing(client: TestClient, settings: Settings):
    bid = create(client)
    put_init(client, bid)
    r = put_seg(client, bid, 0, b"real body", digest=sha(b"other body"))
    assert r.status_code == 422 and "checksum" in r.json()["error"]
    assert not os.path.exists(os.path.join(settings.store_dir, bid, "0"))
    assert os.listdir(os.path.join(settings.store_dir, ".tmp")) == []
    assert put_seg(client, bid, 0, b"real body").status_code == 201


def test_bad_metadata_is_422(client: TestClient):
    bid = create(client)
    put_init(client, bid)
    assert put_seg(client, bid, 0, dur=100_000).status_code == 422             # too short
    assert put_seg(client, bid, 0, dur=5_000_000).status_code == 422           # too long
    assert put_seg(client, bid, 0, dur=100_000, final=True).status_code == 201  # final may be short
    assert put_seg(client, bid, 1, digest="zz").status_code == 422
    assert put_seg(client, bid, 2, b"x" * (8 * 1024 * 1024 + 1)).status_code == 422


def test_start_us_must_be_monotonic(client: TestClient):
    bid = create(client)
    put_init(client, bid)
    assert put_seg(client, bid, 5, start=5 * S).status_code == 201
    assert put_seg(client, bid, 6, start=5 * S).status_code == 422             # overlaps 5 by 2 s
    assert put_seg(client, bid, 6, start=6 * S - 40_000).status_code == 201    # within 50 ms jitter
    assert put_seg(client, bid, 4, start=5 * S).status_code == 422             # ends after 5 starts


def test_missing_init_is_412(client: TestClient):
    bid = create(client)
    assert put_seg(client, bid, 0).status_code == 412
    put_init(client, bid)
    assert put_seg(client, bid, 0).status_code == 201


def test_out_of_order_arrival_and_status(client: TestClient):
    bid = create(client)
    put_init(client, bid)
    for seq in (3, 0, 1, 7, 5):
        assert put_seg(client, bid, seq).status_code == 201
    st = client.get(f"/v1/broadcasts/{bid}/status").json()
    assert st["inits"] == [0]
    assert st["epochs"] == [{"epoch": 0, "received_through": 1, "missing": [2, 4, 6], "ended": False}]
    assert client.post(f"/v1/broadcasts/{bid}/epochs/0/end", json={"last_sequence": 9, "reason": "stopped"}).status_code == 200
    st = client.get(f"/v1/broadcasts/{bid}/status").json()
    assert st["epochs"][0] == {"epoch": 0, "received_through": 1, "missing": [2, 4, 6, 8, 9], "ended": True}


def test_epoch_end_rules(client: TestClient):
    bid = create(client)
    put_init(client, bid)
    put_seg(client, bid, 4)
    r = client.post(f"/v1/broadcasts/{bid}/epochs/0/end", json={"last_sequence": 3, "reason": "stopped"})
    assert r.status_code == 409                                                # 4 is already stored
    assert client.post(f"/v1/broadcasts/{bid}/epochs/0/end", json={"last_sequence": 4, "reason": "stopped"}).status_code == 200
    assert client.post(f"/v1/broadcasts/{bid}/epochs/0/end", json={"last_sequence": 4, "reason": "stopped"}).status_code == 200
    assert client.post(f"/v1/broadcasts/{bid}/epochs/0/end", json={"last_sequence": 5, "reason": "stopped"}).status_code == 409
    assert put_seg(client, bid, 5).status_code == 422                          # after the epoch's end


def test_finish_needs_every_epoch_ended(client: TestClient):
    bid = create(client)
    put_init(client, bid)
    put_seg(client, bid, 0)
    put_seg(client, bid, 0, epoch=2)
    assert client.post(f"/v1/broadcasts/{bid}/finish", json={"last_epoch": 2}).status_code == 409
    client.post(f"/v1/broadcasts/{bid}/epochs/0/end", json={"last_sequence": 0, "reason": "interrupted"})
    client.post(f"/v1/broadcasts/{bid}/epochs/1/end", json={"last_sequence": -1, "reason": "app_restart"})
    assert client.post(f"/v1/broadcasts/{bid}/finish", json={"last_epoch": 2}).status_code == 409
    client.post(f"/v1/broadcasts/{bid}/epochs/2/end", json={"last_sequence": 0, "reason": "stopped"})
    assert client.post(f"/v1/broadcasts/{bid}/finish", json={"last_epoch": 1}).status_code == 409   # epoch 2 exists
    assert client.post(f"/v1/broadcasts/{bid}/finish", json={"last_epoch": 2}).status_code == 200
    assert client.get(f"/v1/broadcasts/{bid}/status").json()["state"] == "finishing"
    # a finished broadcast still accepts its owner's uploads (archive)
    assert put_seg(client, bid, 0, b"late", epoch=2).status_code == 409        # different bytes, same key
    assert put_seg(client, bid, 0, epoch=2).status_code == 200


def test_deleted_broadcast_is_410(client: TestClient, database_url: str):
    bid = create(client)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("UPDATE broadcasts SET deleted_at = now() WHERE id = %s", (bid,))
    assert put_init(client, bid) == 410


def test_other_account_gets_404(client: TestClient, database_url: str):
    bid = create(client)
    with psycopg.connect(database_url, autocommit=True) as conn:
        u, d = uuid.uuid4(), uuid.uuid4()
        conn.execute("INSERT INTO users (id, email, password_hash) VALUES (%s, 'b@x', '!')", (u,))
        conn.execute("INSERT INTO devices (id, owner_id, name, platform, token_hash) VALUES (%s, %s, 'b', 'test', %s)",
                     (d, u, hashlib.sha256(b"other-token").digest()))
    h = {"Authorization": "Bearer other-token"}
    assert client.get(f"/v1/broadcasts/{bid}/status", headers=h).status_code == 404
    assert client.put(f"/v1/broadcasts/{bid}/inits/0", content=INIT,
                      headers={**h, "X-SR-SHA256": sha(INIT)}).status_code == 404


class SimulatedCrash(BaseException):
    """Stands in for the process dying: ordinary `except Exception` cleanup does not run."""


def test_crash_between_rename_and_commit(settings: Settings, client: TestClient):
    """A crash after the rename and before the commit: no acknowledgement was sent, the
    row is not committed, and the next startup deletes the file, so nothing is ever
    acknowledged-but-missing. The client's retry then stores it normally."""
    bid = create(client)
    put_init(client, bid)
    body = b"C" * 50_000
    store = Store(settings.store_dir)

    async def crash() -> None:
        raise SimulatedCrash()

    async def write() -> None:
        store.after_rename = crash
        async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:

            async def chunks():
                yield body

            rec = await store.receive(chunks(), settings.max_segment_bytes, len(body))
            await store.put_segment(conn, SegmentMeta(uuid.UUID(bid), 0, 0, 0, 0, S, False), rec)

    with pytest.raises(SimulatedCrash):
        asyncio.run(write())
    path = os.path.join(settings.store_dir, Store.segment_key(uuid.UUID(bid), 0, 0))
    assert os.path.exists(path)                       # renamed into place...
    with psycopg.connect(settings.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM segments").fetchone() == (0,)   # ...but never committed

    # restart: the startup scan runs before any PUT is accepted
    with TestClient(create_app(settings), headers=client.headers) as c2:
        assert not os.path.exists(path)
        assert c2.get(f"/v1/broadcasts/{bid}/status").json()["epochs"] == []
        assert put_seg(c2, bid, 0, body).status_code == 201
        assert put_seg(c2, bid, 0, body).status_code == 200


def test_scan_keeps_committed_files(settings: Settings, client: TestClient):
    bid = create(client)
    put_init(client, bid)
    assert put_seg(client, bid, 0).status_code == 201
    with open(os.path.join(settings.store_dir, ".tmp", "leftover.part"), "wb") as f:
        f.write(b"partial")
    with psycopg.connect(settings.database_url) as conn:
        result = Store(settings.store_dir).scan(conn)
    assert result == {"removed_tmp": 1, "removed_orphans": 0, "kept": 2}


def test_concurrent_identical_puts_store_once(client: TestClient, settings: Settings):
    """Two PUTs of one key at once: the advisory lock serialises them; one 201, one 200."""
    bid = create(client)
    put_init(client, bid)
    body = b"D" * 200_000
    store = Store(settings.store_dir)

    async def one() -> int:
        async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
            async def chunks():
                yield body
            rec = await store.receive(chunks(), settings.max_segment_bytes, None)
            return await store.put_segment(conn, SegmentMeta(uuid.UUID(bid), 0, 0, 0, 0, S, False), rec)

    async def both() -> list[int]:
        return sorted(await asyncio.gather(one(), one(), one()))

    assert asyncio.run(both()) == [200, 200, 201]


def test_schema_doc_matches_migration():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(root, "docs/architecture/schema.sql")) as a, \
         open(os.path.join(root, "services/streamrelay/migrations/0001_initial.sql")) as b:
        assert a.read() == b.read()
