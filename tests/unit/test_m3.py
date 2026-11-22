"""M3 server: pairing, revocation, cross-account isolation, media validation, limits, path safety, late uploads."""
from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from streamrelay import pairing
from streamrelay.api.main import create_app
from streamrelay.config import Settings

ROOT = Path(__file__).resolve().parents[2]
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg/ffprobe")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def pair_device(client: TestClient, database_url: str, email: str, platform: str = "test") -> dict:
    with psycopg.connect(database_url, autocommit=True) as conn:
        code = pairing.create_code(conn, pairing.ensure_user(conn, email))
    r = client.post("/v1/devices/pair", json={"code": code, "name": "phone", "platform": platform})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", **r.json()}


def create(client: TestClient, h: dict, mode: str = "skip_to_live") -> str:
    r = client.post("/v1/broadcasts", headers={"Authorization": h["Authorization"]},
                    json={"title": "t", "client_broadcast_ref": str(uuid.uuid4()), "catch_up": {"mode": mode}})
    assert r.status_code == 201, r.text
    return r.json()["broadcast_id"]


def auth(h: dict) -> dict:
    return {"Authorization": h["Authorization"]}


def seg_headers(body: bytes, seq: int, dur: int = 2_000_000, **extra: str) -> dict:
    return {"X-SR-SHA256": sha(body), "X-SR-Init": "0", "X-SR-Start-Us": str(seq * 2_000_000),
            "X-SR-Duration-Us": str(dur), **extra}


# ---- pairing and revocation ---------------------------------------------------------------------

def test_pairing_code_works_once(client: TestClient, database_url: str):
    with psycopg.connect(database_url, autocommit=True) as conn:
        code = pairing.create_code(conn, pairing.ensure_user(conn, "a@x"))
    body = {"code": code.lower().replace("-", ""), "name": "iPhone", "platform": "ios"}   # typed loosely: still accepted
    r = client.post("/v1/devices/pair", json=body)
    assert r.status_code == 201
    token = r.json()["token"]
    assert len(token) >= 43                                                   # 256 bits, url-safe base64
    assert client.post("/v1/devices/pair", json=body).status_code == 403        # used
    with psycopg.connect(database_url) as conn:                               # stored hashed, never in clear
        assert conn.execute("SELECT count(*) FROM devices WHERE token_hash = %s",
                            (hashlib.sha256(token.encode()).digest(),)).fetchone() == (1,)


def test_expired_and_unknown_codes(client: TestClient, database_url: str):
    with psycopg.connect(database_url, autocommit=True) as conn:
        code = pairing.create_code(conn, pairing.ensure_user(conn, "a@x"), ttl_s=-1)
    assert client.post("/v1/devices/pair", json={"code": code, "name": "p", "platform": "ios"}).status_code == 403
    assert client.post("/v1/devices/pair", json={"code": "ABCD-EFGH", "name": "p", "platform": "ios"}).status_code == 403


def test_pairing_attempts_are_rate_limited(settings: Settings):
    s = dataclasses.replace(settings, pairing_attempts_per_minute=3)
    with TestClient(create_app(s)) as c:
        codes = [c.post("/v1/devices/pair", json={"code": "ABCD-EFGH", "name": "p", "platform": "ios"}).status_code
                 for _ in range(5)]
    assert codes[:3] == [403, 403, 403] and codes[3:] == [429, 429]


def test_revoked_token_gets_401_on_the_next_request(client: TestClient, database_url: str):
    a = pair_device(client, database_url, "a@x")
    bid = create(client, a)
    assert client.get(f"/v1/broadcasts/{bid}/status", headers=auth(a)).status_code == 200
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (a["device_id"],))
    assert client.get(f"/v1/broadcasts/{bid}/status", headers=auth(a)).status_code == 401


def test_every_endpoint_is_404_for_another_account(client: TestClient, database_url: str):
    a = pair_device(client, database_url, "a@x")
    b = pair_device(client, database_url, "b@x")
    bid = create(client, a)
    body = b"x" * 100
    calls = [
        ("put", f"/v1/broadcasts/{bid}/inits/0", {"content": body, "headers": {"X-SR-SHA256": sha(body)}}),
        ("put", f"/v1/broadcasts/{bid}/segments/0/0", {"content": body, "headers": seg_headers(body, 0)}),
        ("post", f"/v1/broadcasts/{bid}/epochs/0/end", {"json": {"last_sequence": 0, "reason": "stopped"}}),
        ("post", f"/v1/broadcasts/{bid}/finish", {"json": {"last_epoch": 0}}),
        ("get", f"/v1/broadcasts/{bid}/status", {}),
        ("post", f"/v1/broadcasts/{bid}/heartbeat", {"json": {"epoch": 0}}),
    ]
    for method, url, kw in calls:
        h = {**kw.pop("headers", {}), **auth(b)}
        r = getattr(client, method)(url, headers=h, **kw)
        assert r.status_code == 404, (url, r.status_code, r.text)
    # and A still sees an untouched broadcast
    st = client.get(f"/v1/broadcasts/{bid}/status", headers=auth(a)).json()
    assert st["epochs"] == [] and st["inits"] == []


def test_heartbeat(client: TestClient, database_url: str):
    a = pair_device(client, database_url, "a@x")
    bid = create(client, a)
    r = client.post(f"/v1/broadcasts/{bid}/heartbeat", headers=auth(a),
                    json={"epoch": 0, "captured_through": 10, "queue_depth": 3, "battery": 0.5, "network": "wifi"})
    assert r.status_code == 204
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT body->>'queue_depth' FROM device_heartbeats").fetchone() == ("3",)
        assert conn.execute("SELECT last_seen_at IS NOT NULL FROM devices WHERE id = %s", (a["device_id"],)).fetchone() == (True,)


def test_max_active_broadcasts(settings: Settings):
    s = dataclasses.replace(settings, max_active_broadcasts=2)
    with TestClient(create_app(s), headers={"Authorization": f"Bearer {s.static_token}"}) as c:
        for _ in range(2):
            assert c.post("/v1/broadcasts", json={"title": "t", "client_broadcast_ref": str(uuid.uuid4())}).status_code == 201
        r = c.post("/v1/broadcasts", json={"title": "t", "client_broadcast_ref": str(uuid.uuid4())})
        assert r.status_code == 429 and r.headers["retry-after"]


# ---- media validation ---------------------------------------------------------------------------

@pytest.fixture
def vclient(settings: Settings):
    s = dataclasses.replace(settings, validate_media=True)
    with TestClient(create_app(s), headers={"Authorization": f"Bearer {s.static_token}"}) as c:
        yield c


@needs_ffmpeg
def test_real_segments_accepted_and_codec_recorded(vclient: TestClient, media: Path, database_url: str):
    bid = create(vclient, {"Authorization": vclient.headers["Authorization"]})
    init = (media / "init.mp4").read_bytes()
    assert vclient.put(f"/v1/broadcasts/{bid}/inits/0", content=init, headers={"X-SR-SHA256": sha(init)}).status_code == 201
    for i in range(3):
        body = (media / f"{i:05d}.m4s").read_bytes()
        r = vclient.put(f"/v1/broadcasts/{bid}/segments/0/{i}", content=body, headers=seg_headers(body, i))
        assert r.status_code == 201, r.text
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT codec, width, height FROM inits").fetchone() == ("h264,aac", 1280, 720)


@needs_ffmpeg
def test_bad_media_rejected_and_server_keeps_running(vclient: TestClient, media: Path):
    bid = create(vclient, {"Authorization": vclient.headers["Authorization"]})
    init = (media / "init.mp4").read_bytes()
    vclient.put(f"/v1/broadcasts/{bid}/inits/0", content=init, headers={"X-SR-SHA256": sha(init)})
    good = (media / "00000.m4s").read_bytes()
    cases = {
        "truncated": good[: len(good) // 2],
        "random": os.urandom(len(good)),
        "header fuzz": good[:8] + os.urandom(200) + good[208:],
        "empty": b"",
    }
    for name, body in cases.items():
        r = vclient.put(f"/v1/broadcasts/{bid}/segments/0/0", content=body, headers=seg_headers(body, 0))
        assert r.status_code == 422, (name, r.status_code, r.text)
    r = vclient.put(f"/v1/broadcasts/{bid}/segments/0/0", content=good, headers=seg_headers(good, 0, dur=3_000_000))
    assert r.status_code == 422 and "X-SR-Duration-Us" in r.json()["error"]       # declared length is wrong
    assert vclient.put(f"/v1/broadcasts/{bid}/segments/0/0", content=good, headers=seg_headers(good, 0)).status_code == 201


@needs_ffmpeg
def test_wrong_codec_init_rejected(vclient: TestClient, media: Path):
    bid = create(vclient, {"Authorization": vclient.headers["Authorization"]})
    init = (media.parent / "mp4v" / "init.mp4").read_bytes()
    r = vclient.put(f"/v1/broadcasts/{bid}/inits/0", content=init, headers={"X-SR-SHA256": sha(init)})
    assert r.status_code == 422 and "H.264" in r.json()["error"]
    junk = os.urandom(5000)
    assert vclient.put(f"/v1/broadcasts/{bid}/inits/0", content=junk, headers={"X-SR-SHA256": sha(junk)}).status_code == 422
    noaudio = (media.parent / "noaudio" / "init.mp4").read_bytes()     # the playout decoder needs the audio track
    r = vclient.put(f"/v1/broadcasts/{bid}/inits/0", content=noaudio, headers={"X-SR-SHA256": sha(noaudio)})
    assert r.status_code == 422 and "AAC" in r.json()["error"]


@needs_ffmpeg
def test_wrong_container_rejected(vclient: TestClient, media: Path):
    """Clean H.264/AAC in MPEG-TS probes fine with format detection, but the playout reads MP4 only."""
    bid = create(vclient, {"Authorization": vclient.headers["Authorization"]})
    ts = (media.parent / "clip.ts").read_bytes()
    head, tail = ts[:1880], ts[1880:]
    r = vclient.put(f"/v1/broadcasts/{bid}/inits/0", content=head, headers={"X-SR-SHA256": sha(head)})
    assert r.status_code == 422, r.text
    init = (media / "init.mp4").read_bytes()
    assert vclient.put(f"/v1/broadcasts/{bid}/inits/0", content=init, headers={"X-SR-SHA256": sha(init)}).status_code == 201
    r = vclient.put(f"/v1/broadcasts/{bid}/segments/0/0", content=tail, headers=seg_headers(tail, 0))
    assert r.status_code == 422, r.text


@needs_ffmpeg
def test_final_segment_duration_is_checked_too(vclient: TestClient, media: Path):
    bid = create(vclient, {"Authorization": vclient.headers["Authorization"]})
    init = (media / "init.mp4").read_bytes()
    vclient.put(f"/v1/broadcasts/{bid}/inits/0", content=init, headers={"X-SR-SHA256": sha(init)})
    good = (media / "00000.m4s").read_bytes()
    h = seg_headers(good, 0, dur=3_000_000) | {"X-SR-Flags": "final"}
    assert vclient.put(f"/v1/broadcasts/{bid}/segments/0/0", content=good, headers=h).status_code == 422
    h = seg_headers(good, 0) | {"X-SR-Flags": "final"}
    assert vclient.put(f"/v1/broadcasts/{bid}/segments/0/0", content=good, headers=h).status_code == 201


# ---- limits -------------------------------------------------------------------------------------

def test_storage_cap_507_then_resumes(settings: Settings):
    s = dataclasses.replace(settings, storage_max_bytes=250_000)
    with TestClient(create_app(s), headers={"Authorization": f"Bearer {s.static_token}"}) as c:
        bid = create(c, {"Authorization": c.headers["Authorization"]})
        init = b"i" * 1000
        assert c.put(f"/v1/broadcasts/{bid}/inits/0", content=init, headers={"X-SR-SHA256": sha(init)}).status_code == 201
        body = b"s" * 100_000
        codes = [c.put(f"/v1/broadcasts/{bid}/segments/0/{i}", content=body, headers=seg_headers(body, i)).status_code
                 for i in range(3)]
        assert codes == [201, 201, 507]
        r = c.put(f"/v1/broadcasts/{bid}/segments/0/2", content=body, headers=seg_headers(body, 2))
        assert r.status_code == 507 and int(r.headers["retry-after"]) > 0
    # space freed (cap raised): the held segment goes in
    s2 = dataclasses.replace(settings, storage_max_bytes=1_000_000)
    with TestClient(create_app(s2), headers={"Authorization": f"Bearer {s.static_token}"}) as c:
        assert c.put(f"/v1/broadcasts/{bid}/segments/0/2", content=body, headers=seg_headers(body, 2)).status_code == 201


def test_identical_init_replay_is_200_even_when_storage_is_full(settings: Settings):
    s = dataclasses.replace(settings, storage_max_bytes=1500)
    with TestClient(create_app(s), headers={"Authorization": f"Bearer {s.static_token}"}) as c:
        bid = create(c, {"Authorization": c.headers["Authorization"]})
        init = b"i" * 1000
        assert c.put(f"/v1/broadcasts/{bid}/inits/0", content=init, headers={"X-SR-SHA256": sha(init)}).status_code == 201
        assert c.put(f"/v1/broadcasts/{bid}/inits/1", content=init, headers={"X-SR-SHA256": sha(init)}).status_code == 507
        assert c.put(f"/v1/broadcasts/{bid}/inits/0", content=init, headers={"X-SR-SHA256": sha(init)}).status_code == 200


def test_concurrent_uploads_cannot_both_pass_the_cap(tmp_path: Path):
    """Admission reserves the bytes, so the second of two simultaneous uploads is refused."""
    import asyncio

    from streamrelay.limits import StorageGuard
    from streamrelay.store import Rejected

    g = StorageGuard(str(tmp_path), max_bytes=100, free_min_bytes=0)
    g.used, g.read_at = 90, 10**12          # fresh: no database read

    async def run() -> None:
        held = await g.admit(None, 6)  # type: ignore[arg-type]
        with pytest.raises(Rejected) as e:
            await g.admit(None, 6)  # type: ignore[arg-type]
        assert e.value.status == 507
        g.release(held)
        g.release(await g.admit(None, 6))  # type: ignore[arg-type]
    asyncio.run(run())


def test_concurrent_first_refreshes_share_one_read(tmp_path: Path):
    """Two uploads at start-up both need the first total: the second waits for it instead of failing."""
    import asyncio

    from streamrelay.limits import StorageGuard

    g = StorageGuard(str(tmp_path), max_bytes=1000, free_min_bytes=0)
    reads = 0

    class Conn:
        async def execute(self, _sql: str) -> Conn:
            nonlocal reads
            reads += 1
            await asyncio.sleep(0.05)          # the other request arrives while this read is out
            return self

        async def fetchone(self) -> tuple[int]:
            return (40,)

    async def run() -> None:
        a, b = await asyncio.gather(g.admit(Conn(), 5), g.admit(Conn(), 5))  # type: ignore[arg-type]
        assert g.used == 40 and reads == 1
        g.release(a)
        g.release(b)
    asyncio.run(run())


def test_control_bodies_are_capped_before_parsing(client: TestClient):
    big = b'{"code": "' + b"A" * 100_000 + b'", "name": "x", "platform": "test"}'
    r = client.post("/v1/devices/pair", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413

    def chunks():                                   # no Content-Length: counted as it arrives
        for _ in range(20):
            yield b" " * 10_000
    r = client.post("/v1/devices/pair", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_free_space_floor_507(settings: Settings):
    s = dataclasses.replace(settings, storage_free_min_bytes=10**18)
    with TestClient(create_app(s), headers={"Authorization": f"Bearer {s.static_token}"}) as c:
        bid = create(c, {"Authorization": c.headers["Authorization"]})
        assert c.put(f"/v1/broadcasts/{bid}/inits/0", content=b"i", headers={"X-SR-SHA256": sha(b"i")}).status_code == 507


def test_segment_rate_limit(settings: Settings):
    s = dataclasses.replace(settings, segment_puts_per_second=2)
    with TestClient(create_app(s), headers={"Authorization": f"Bearer {s.static_token}"}) as c:
        bid = create(c, {"Authorization": c.headers["Authorization"]})
        c.put(f"/v1/broadcasts/{bid}/inits/0", content=b"i", headers={"X-SR-SHA256": sha(b"i")})
        codes = []
        for i in range(8):
            body = f"seg{i}".encode()
            codes.append(c.put(f"/v1/broadcasts/{bid}/segments/0/{i}", content=body, headers=seg_headers(body, i)).status_code)
    assert codes.count(201) >= 4 and 429 in codes


# ---- paths --------------------------------------------------------------------------------------

def test_path_traversal_never_reaches_the_store(client: TestClient, settings: Settings):
    bid = create(client, {"Authorization": client.headers["Authorization"]})
    for url in (f"/v1/broadcasts/{bid}/segments/0/..%2F..%2Fx", "/v1/broadcasts/..%2F..%2Fetc/segments/0/0",
                f"/v1/broadcasts/{bid}/segments/-1/0", f"/v1/broadcasts/{bid}/inits/..%2Fx"):
        r = client.put(url, content=b"x", headers=seg_headers(b"x", 0))
        assert r.status_code in (404, 422), (url, r.status_code)
    assert sorted(os.listdir(settings.store_dir)) == [".tmp"]


def test_symlinked_store_directory_rejected(client: TestClient, settings: Settings, tmp_path: Path):
    bid = create(client, {"Authorization": client.headers["Authorization"]})
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, os.path.join(settings.store_dir, bid))
    r = client.put(f"/v1/broadcasts/{bid}/inits/0", content=b"i", headers={"X-SR-SHA256": sha(b"i")})
    assert r.status_code == 422
    assert list(outside.iterdir()) == []


# ---- late uploads -------------------------------------------------------------------------------

def test_upload_after_finished_is_archived_and_skipped_for_live(client: TestClient, database_url: str):
    bid = create(client, {"Authorization": client.headers["Authorization"]})
    client.put(f"/v1/broadcasts/{bid}/inits/0", content=b"i", headers={"X-SR-SHA256": sha(b"i")})
    client.post(f"/v1/broadcasts/{bid}/epochs/0/end", json={"last_sequence": 1, "reason": "stopped"})
    client.post(f"/v1/broadcasts/{bid}/finish", json={"last_epoch": 0})
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("UPDATE broadcasts SET state = 'finished', finished_at = now() WHERE id = %s", (bid,))
    assert client.put(f"/v1/broadcasts/{bid}/segments/0/1", content=b"late", headers=seg_headers(b"late", 1)).status_code == 201
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT play_result FROM segments WHERE sequence = 1").fetchone() == ("skipped",)
    st = client.get(f"/v1/broadcasts/{bid}/status").json()
    assert st["epochs"][0]["missing"] == [0]
