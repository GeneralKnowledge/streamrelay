"""Upload API, protocol v1 subset for M1.

Endpoints: create broadcast, PUT init, PUT segment, epoch end, finish, status.
Auth: a bearer device token looked up by hash in `devices` (M1 provisions one
from SR_STATIC_TOKEN; pairing arrives in M3). Every broadcast-scoped request
checks ownership; another account's broadcast is a 404, a deleted one a 410.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from .. import db
from ..config import Settings
from ..ids import uuid7
from ..store import Rejected, SegmentMeta, Store

log = logging.getLogger("streamrelay.api")
MISSING_CAP = 500


class CatchUp(BaseModel):
    mode: Literal["catch_up", "hold", "skip_to_live"] = "catch_up"
    max_speed: float = Field(1.25, ge=1.0, le=1.5)
    max_delay_s: int | None = Field(None, gt=0)


class CreateBroadcast(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    destination_ids: list[uuid.UUID] = []
    catch_up: CatchUp = CatchUp()
    client_broadcast_ref: uuid.UUID


class EpochEnd(BaseModel):
    last_sequence: int = Field(ge=-1)
    reason: Literal["interrupted", "app_restart", "camera_lost", "stopped"]


class Finish(BaseModel):
    last_epoch: int = Field(ge=0)


def create_app(settings: Settings) -> FastAPI:
    store = Store(settings.store_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Migrations and the startup scan finish before the server accepts any request.
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            applied = db.migrate(conn)
            if applied:
                log.info("migrations applied: %s", applied)
            if settings.static_token:
                db.ensure_static_device(conn, settings.static_token)
            log.info("startup scan: %s", store.scan(conn))
        pool = AsyncConnectionPool(settings.database_url, min_size=2, max_size=10, open=False)
        await pool.open()
        app.state.pool = pool
        yield
        await pool.close()

    app = FastAPI(title="StreamRelay upload API", version="1", lifespan=lifespan)
    app.state.store = store

    @app.exception_handler(Rejected)
    async def rejected(_: Request, exc: Rejected) -> JSONResponse:
        return JSONResponse({"error": exc.reason}, status_code=exc.status)

    async def conn_dep(request: Request):
        async with request.app.state.pool.connection() as conn:
            yield conn

    Conn = Annotated[psycopg.AsyncConnection, Depends(conn_dep)]

    async def device(conn: Conn, authorization: Annotated[str | None, Header()] = None) -> tuple[uuid.UUID, uuid.UUID]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        cur = await conn.execute(
            "SELECT id, owner_id FROM devices WHERE token_hash = %s AND revoked_at IS NULL",
            (db.token_hash(authorization[7:]),))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(401, "unknown or revoked token")
        return row[0], row[1]

    Device = Annotated[tuple[uuid.UUID, uuid.UUID], Depends(device)]

    async def owned(conn: psycopg.AsyncConnection, dev: tuple[uuid.UUID, uuid.UUID], bid: uuid.UUID) -> None:
        cur = await conn.execute("SELECT deleted_at FROM broadcasts WHERE id = %s AND owner_id = %s", (bid, dev[1]))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "broadcast not found")
        if row[0] is not None:
            raise HTTPException(410, "broadcast deleted")

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/broadcasts", status_code=201)
    async def create(body: CreateBroadcast, conn: Conn, dev: Device, response: Response) -> dict[str, Any]:
        if body.destination_ids:
            raise Rejected(422, "destinations arrive in M5; send an empty destination_ids list")
        bid = uuid7()
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO broadcasts (id, owner_id, device_id, client_ref, title, catch_up_mode,"
                " hold_max_delay_s, catch_up_max_speed, buffer_target_ms)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (device_id, client_ref) DO NOTHING RETURNING id",
                (bid, dev[1], dev[0], body.client_broadcast_ref, body.title, body.catch_up.mode,
                 body.catch_up.max_delay_s if body.catch_up.mode == "hold" else None,
                 body.catch_up.max_speed, settings.playout_delay_target_ms))
            if await cur.fetchone() is None:
                cur = await conn.execute("SELECT id FROM broadcasts WHERE device_id = %s AND client_ref = %s",
                                         (dev[0], body.client_broadcast_ref))
                row = await cur.fetchone()
                assert row is not None
                bid = row[0]
                response.status_code = 200
            else:
                await conn.execute("INSERT INTO events (broadcast_id, kind, detail) VALUES (%s, 'created', '{}')",
                                   (bid,))
        return {"broadcast_id": str(bid), "playout_delay_target_ms": settings.playout_delay_target_ms,
                "max_segment_bytes": settings.max_segment_bytes}

    def content_length(request: Request) -> int | None:
        v = request.headers.get("content-length")
        if v is None:
            return None
        n = int(v)
        if n > settings.max_segment_bytes:
            raise Rejected(422, f"body over max_segment_bytes ({settings.max_segment_bytes})")
        return n

    def sha_header(value: str | None) -> bytes:
        try:
            if value is None or len(value) != 64:
                raise ValueError
            return bytes.fromhex(value)
        except ValueError:
            raise Rejected(422, "X-SR-SHA256 must be 64 hex characters") from None

    @app.put("/v1/broadcasts/{bid}/inits/{init_id}")
    async def put_init(bid: uuid.UUID, init_id: int, request: Request, conn: Conn, dev: Device,
                       x_sr_sha256: Annotated[str | None, Header()] = None) -> Response:
        await owned(conn, dev, bid)
        if init_id < 0:
            raise Rejected(422, "init_id must be >= 0")
        want = sha_header(x_sr_sha256)
        rec = await store.receive(request.stream(), settings.max_segment_bytes, content_length(request))
        if rec.sha256 != want:
            store.discard(rec)
            raise Rejected(422, "checksum mismatch")
        status = await store.put_init(conn, bid, init_id, rec)
        return Response(status_code=status)

    @app.put("/v1/broadcasts/{bid}/segments/{epoch}/{sequence}")
    async def put_segment(
        bid: uuid.UUID, epoch: int, sequence: int, request: Request, conn: Conn, dev: Device,
        x_sr_sha256: Annotated[str | None, Header()] = None,
        x_sr_init: Annotated[int | None, Header()] = None,
        x_sr_start_us: Annotated[int | None, Header()] = None,
        x_sr_duration_us: Annotated[int | None, Header()] = None,
        x_sr_flags: Annotated[str | None, Header()] = None,
    ) -> Response:
        await owned(conn, dev, bid)
        if epoch < 0 or sequence < 0:
            raise Rejected(422, "epoch and sequence must be >= 0")
        if x_sr_init is None or x_sr_start_us is None or x_sr_duration_us is None:
            raise Rejected(422, "X-SR-Init, X-SR-Start-Us and X-SR-Duration-Us are required")
        final = "final" in (x_sr_flags or "").split(",")
        if x_sr_start_us < 0:
            raise Rejected(422, "start_us must be >= 0")
        if final and x_sr_duration_us <= 0 or not final and not 500_000 <= x_sr_duration_us <= 4_000_000:
            raise Rejected(422, "duration_us out of range (0.5-4 s, or > 0 for the final segment)")
        want = sha_header(x_sr_sha256)
        rec = await store.receive(request.stream(), settings.max_segment_bytes, content_length(request))
        if rec.sha256 != want:
            store.discard(rec)
            raise Rejected(422, "checksum mismatch")
        meta = SegmentMeta(bid, epoch, sequence, x_sr_init, x_sr_start_us, x_sr_duration_us, final)
        status = await store.put_segment(conn, meta, rec)
        return Response(status_code=status)

    @app.post("/v1/broadcasts/{bid}/epochs/{epoch}/end")
    async def end_epoch(bid: uuid.UUID, epoch: int, body: EpochEnd, conn: Conn, dev: Device) -> dict[str, Any]:
        await owned(conn, dev, bid)
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"epoch:{bid}:{epoch}",))
            await conn.execute("INSERT INTO epochs (broadcast_id, epoch) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                               (bid, epoch))
            cur = await conn.execute("SELECT last_sequence FROM epochs WHERE broadcast_id = %s AND epoch = %s",
                                     (bid, epoch))
            row = await cur.fetchone()
            assert row is not None
            if row[0] is not None:
                if row[0] != body.last_sequence:
                    raise Rejected(409, f"epoch already ended at {row[0]}")
                return {"epoch": epoch, "last_sequence": row[0]}
            cur = await conn.execute("SELECT max(sequence) FROM segments WHERE broadcast_id = %s AND epoch = %s",
                                     (bid, epoch))
            top = (await cur.fetchone() or [None])[0]
            if top is not None and top > body.last_sequence:
                raise Rejected(409, f"sequence {top} is already stored, after last_sequence {body.last_sequence}")
            await conn.execute("UPDATE epochs SET last_sequence = %s, end_reason = %s"
                               " WHERE broadcast_id = %s AND epoch = %s", (body.last_sequence, body.reason, bid, epoch))
            await conn.execute("INSERT INTO events (broadcast_id, kind, detail) VALUES (%s, 'epoch_end', %s)",
                               (bid, Jsonb({"epoch": epoch, **body.model_dump()})))
        return {"epoch": epoch, "last_sequence": body.last_sequence}

    @app.post("/v1/broadcasts/{bid}/finish")
    async def finish(bid: uuid.UUID, body: Finish, conn: Conn, dev: Device) -> dict[str, Any]:
        await owned(conn, dev, bid)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT g FROM generate_series(0, %s) g LEFT JOIN epochs e ON e.broadcast_id = %s AND e.epoch = g"
                " WHERE e.last_sequence IS NULL ORDER BY g", (body.last_epoch, bid))
            open_ = [r[0] for r in await cur.fetchall()]
            if open_:
                raise Rejected(409, f"epochs without an end call: {open_}")
            cur = await conn.execute("SELECT max(epoch) FROM epochs WHERE broadcast_id = %s", (bid,))
            top = (await cur.fetchone() or [None])[0]
            if top is not None and top > body.last_epoch:
                raise Rejected(409, f"epoch {top} exists, after last_epoch {body.last_epoch}")
            cur = await conn.execute(
                "UPDATE broadcasts SET finish_requested_at = now(),"
                " state = CASE WHEN state IN ('finished', 'failed') THEN state ELSE 'finishing' END"
                " WHERE id = %s AND finish_requested_at IS NULL RETURNING id", (bid,))
            if await cur.fetchone() is not None:
                await conn.execute("INSERT INTO events (broadcast_id, kind, detail) VALUES (%s, 'finish', %s)",
                                   (bid, Jsonb(body.model_dump())))
        return {"state": "finishing"}

    @app.get("/v1/broadcasts/{bid}/status")
    async def status(bid: uuid.UUID, conn: Conn, dev: Device) -> dict[str, Any]:
        await owned(conn, dev, bid)
        cur = await conn.execute("SELECT state FROM broadcasts WHERE id = %s", (bid,))
        state = (await cur.fetchone() or ["starting"])[0]
        cur = await conn.execute("SELECT epoch, last_sequence FROM epochs WHERE broadcast_id = %s ORDER BY epoch",
                                 (bid,))
        epochs_ = await cur.fetchall()
        out = []
        for ep, last in epochs_:
            cur = await conn.execute("SELECT sequence FROM segments WHERE broadcast_id = %s AND epoch = %s"
                                     " ORDER BY sequence", (bid, ep))
            seqs = [r[0] for r in await cur.fetchall()]
            through = -1
            for s in seqs:
                if s != through + 1:
                    break
                through = s
            top = last if last is not None else (seqs[-1] if seqs else -1)
            have = set(seqs)
            missing = [s for s in range(through + 1, top + 1) if s not in have]
            item: dict[str, Any] = {"epoch": ep, "received_through": through, "missing": missing[:MISSING_CAP],
                                    "ended": last is not None}
            if len(missing) > MISSING_CAP:
                item["missing_truncated"] = True
            out.append(item)
        cur = await conn.execute("SELECT init_id FROM inits WHERE broadcast_id = %s ORDER BY init_id", (bid,))
        inits = [r[0] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT position_us, speed, behind_live_ms FROM playout_checkpoints WHERE broadcast_id = %s",
                                 (bid,))
        p = await cur.fetchone()
        playout = None
        if p:
            playout = {"position_us": p[0], "behind_live_ms": p[2], "speed": float(p[1])}
        return {"state": state, "epochs": out, "inits": inits, "playout": playout,
                "playout_delay_ms": p[2] if p else None}

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import os

    uvicorn.run(create_app(Settings.from_env()), host=os.environ.get("SR_HOST", "0.0.0.0"),
                port=int(os.environ.get("SR_PORT", "8080")), log_level="info")
