# Database schema

Reference SQL: [schema.sql](schema.sql). PostgreSQL 16, Alembic migrations.

## Tables and why

| Table | Holds | Notes |
|---|---|---|
| `users` | Accounts | Single-user self-hosting still has one row |
| `devices` | Paired phones | Token stored hashed; revocation stops all uploads at once |
| `pairing_codes` | One-time codes shown in the dashboard | Short expiry, hashed |
| `destinations` | Twitch/YouTube/custom RTMP | Stream keys encrypted (AES-256-GCM, key from server config/secret file); the API never returns them |
| `broadcasts` | One live session | `UNIQUE(device_id, client_ref)` makes creation idempotent; `owner_id` is checked on every request |
| `inits` | fMP4 init segments | Per encoder configuration |
| `epochs` | Unbroken capture runs | Distinguishes "late" from "lost" footage |
| `segments` | One row per 2 s segment | Primary key = the protocol's idempotency key; `played_at` set exactly once |
| `playout_checkpoints` | Where playout is | Updated every second and in the same transaction as `played_at` |
| `broadcast_destinations` | Per-broadcast destination health | Each destination independent |
| `events` | Append-only timeline | Dashboard history, debugging, acceptance tests |
| `device_heartbeats` | Last phone report | Battery, thermal, queue depth |

## Sizes

A 3-hour broadcast is 5,400 segment rows: trivial for Postgres. Storage, not the database, is the cost (≈1.8 GB/hour at 4 Mbps).

## Invariants the tests check

1. A `(broadcast, epoch, sequence)` exists at most once, and its checksum never changes.
2. `played_at` goes from NULL to a value once and never back.
3. Every segment's `init_id` exists before the segment row.
4. A device can read or write only broadcasts whose `owner_id` matches its own.
