# 0007: FastAPI + PostgreSQL, no Redis in v1

## Decision
FastAPI (Python 3.12, async) for the control and upload API; PostgreSQL 16 for all state; Alembic migrations. Work coordination (playout supervisor, forwarders, retention jobs) through Postgres rows plus `LISTEN/NOTIFY`. Playout and API share Python, so the protocol model (Pydantic) is one package.

## Consequences
One VPS runs API, Postgres, MediaMTX and playout under Docker Compose. Uploads stream to disk (no buffering whole bodies in memory). Redis is added only if a measured bottleneck needs it.

## Revisit if
Upload throughput per broadcast needs more than one API worker can give, or a shared protocol library in another language becomes worth it.
