# Upload protocol v1

The contract between a capture client (iPhone first) and the server. Versioned independently of both apps: the path prefix is `/v1`, and any breaking change is `/v2`. The machine-readable form (OpenAPI + JSON Schema) lives in `packages/protocol/` and is generated from the server's Pydantic models; this page is the human description and wins if they disagree until M3 makes the schema authoritative.

## Principles

- Every 2 s segment is an independent, idempotent, checksummed upload.
- The server acknowledges (`201`/`200`) only after the bytes are durably stored. The client deletes nothing until acknowledged, and then only per the retention setting.
- Order of arrival does not matter. Sequence numbers and timestamps decide order.
- The client can always ask "what do you have?" and reconcile its queue from the answer, after a crash, a reinstall of state, or a long outage.

## Identifiers

| Name | Form | Notes |
|---|---|---|
| `broadcast_id` | UUIDv7 | Created by the server. Never trusted alone: every request is also checked against the authenticated device's owner |
| `epoch` | integer ≥ 0 | One unbroken capture run within a broadcast (ADR 0003) |
| `sequence` | integer ≥ 0 | Per epoch, starts at 0, no gaps in what the client produces |
| `init_id` | integer ≥ 0 | Per broadcast; which init segment a media segment needs |
| `start_us`, `duration_us` | integer microseconds | On the broadcast timeline (0 = broadcast start), from the capture clock |

A segment's key is `(broadcast_id, epoch, sequence)`.

## Authentication

- **Device pairing** (once): the dashboard shows a one-time pairing code; the app calls `POST /v1/devices/pair {code, name, platform}` and receives a device token (random 256-bit, shown once, stored in the iPhone Keychain, stored hashed on the server).
- **Requests:** `Authorization: Bearer <device token>` over TLS. Tokens can be revoked from the dashboard.
- Every broadcast-scoped request checks `broadcast.owner_id == device.owner_id` and that the broadcast is not finished/deleted. A guessed `broadcast_id` from another user returns `404`, the same as a missing one.

## Endpoints

### Create a broadcast
```
POST /v1/broadcasts
{ "title": "Pentland Hills walk", "destination_ids": ["..."], "catch_up": {"mode": "catch_up", "max_speed": 1.5},
  "client_broadcast_ref": "6f1c…" }
→ 201 { "broadcast_id": "0193…", "playout_delay_target_ms": 8000, "max_segment_bytes": 8388608 }
```
`client_broadcast_ref` is a client-generated UUID that makes creation idempotent: retrying with the same ref returns the same broadcast. The app may start recording **before** this succeeds (no signal at the start); segments wait in the queue until it has a `broadcast_id`.

`catch_up.mode`: `catch_up` (play the backlog faster until live) or `skip_to_live` (after an outage, jump to the newest footage; the backlog is still uploaded and archived). Broadcaster's choice, changeable during the broadcast.

### Upload an init segment
```
PUT /v1/broadcasts/{id}/inits/{init_id}
Content-Type: video/mp4
X-SR-SHA256: <hex>
<ftyp+moov bytes>
→ 201 Created | 200 OK (identical already stored) | 409 Conflict (different bytes for this init_id)
```

### Upload a media segment
```
PUT /v1/broadcasts/{id}/segments/{epoch}/{sequence}
Content-Type: video/mp4
Content-Length: 1048576
X-SR-SHA256: <hex of body>
X-SR-Init: 0
X-SR-Start-Us: 304000000
X-SR-Duration-Us: 2000000
X-SR-Flags: final            (optional: last segment of this epoch)
<moof+mdat bytes>
```
Responses:

| Status | Meaning | Client action |
|---|---|---|
| `201 Created` | Stored durably, checksum matched | Mark acknowledged |
| `200 OK` | Already stored with the same checksum (duplicate) | Mark acknowledged |
| `409 Conflict` | Already stored with a **different** checksum | Stop and report; a client bug, never overwrite |
| `412 Precondition Failed` | Init segment `X-SR-Init` not uploaded yet | Upload the init, retry |
| `422 Unprocessable` | Checksum mismatch, size over limit, timestamps invalid | Retry once from disk (corruption in transit); if it repeats, mark the segment damaged and report |
| `404` | Broadcast unknown or not yours | Stop uploading this broadcast; surface to the user |
| `410 Gone` | Broadcast deleted by the user | Stop; keep local files per retention |
| `429` / `503` | Server busy | Back off per `Retry-After` |
| network error / timeout | Unknown whether stored | Retry; idempotency makes it safe |

Why one PUT instead of the spec's POST-then-PUT: on a bad connection every round trip is a chance to fail; the metadata fits in headers, and the PUT is idempotent by key + checksum, which gives the same guarantees.

**Resume:** v1 retries the whole segment (≈1 MB). Reserved for later: `Content-Range` partial PUTs with `HEAD …/segments/{epoch}/{sequence}` returning `X-SR-Received-Bytes`.

### End an epoch or the broadcast
```
POST /v1/broadcasts/{id}/epochs/{epoch}/end   { "last_sequence": 899, "reason": "interrupted|app_restart|camera_lost|stopped" }
POST /v1/broadcasts/{id}/finish               { "last_epoch": 2 }
```
`finish` means "no more capture". Playout keeps going until everything received has been played (or skipped, in skip-to-live), then ends the outputs. Late uploads after `finish` are still accepted for the archive.

### Status (reconciliation)
```
GET /v1/broadcasts/{id}/status
→ 200 {
  "state": "catching_up",
  "epochs": [
    {"epoch": 0, "received_through": 899, "missing": [], "ended": true},
    {"epoch": 1, "received_through": 151, "missing": [152, 160], "ended": false}
  ],
  "inits": [0],
  "playout": { "position_us": 1804000000, "behind_live_ms": 182000, "speed": 1.25 },
  "playout_delay_ms": 182000
}
```
`received_through` is the highest sequence with no gap below it; `missing` lists holes above it (capped at 500 entries, with `"missing_truncated": true`). The client uses this after any restart to re-mark acknowledged segments and re-queue missing ones.

### Heartbeat
```
POST /v1/broadcasts/{id}/heartbeat
{ "epoch": 1, "captured_through": 210, "queue_depth": 59, "queue_bytes": 61865984,
  "battery": 0.62, "thermal": "fair", "free_bytes": 23500000000, "network": "cellular" }
```
Every 5 s while connected. Lets the server tell "phone offline" from "phone online but behind", and feeds the dashboard. Optional for correctness: the server never relies on heartbeats to order media.

## Client upload rules

- **Order:** in `catch_up` mode, oldest missing first. In `skip_to_live` mode, newest first, then backfill oldest-first for the archive.
- **Concurrency:** up to 3 segment PUTs in flight over one HTTP/2 connection; drop to 1 when error rate rises.
- **Backoff:** exponential from 1 s to 30 s with full jitter; reset on success. Network-change notifications (`NWPathMonitor`) retry at once.
- **Background:** when the app leaves the foreground, hand the remaining queue to a background `URLSession`; on relaunch, reconcile with `GET status` before resuming.
- **Local deletion:** only after acknowledgement **and** the retention setting allows it (default: keep local copies until the user deletes them or space runs low, with a warning first).

## Server rules

- Validate: size ≤ `max_segment_bytes`; checksum; `start_us` monotonic within the epoch (±50 ms jitter allowed); duration 0.5–4 s.
- Write to a temp file, fsync, rename, then insert the row, then respond. A crash between rename and insert is repaired at startup by scanning for files with no row.
- Never play a segment twice: the playout records the last played `(epoch, sequence)` in Postgres in the same transaction that advances its position.
- Rate limit per device (default 20 segment PUTs/s, far above the 0.5/s a live client needs plus catch-up).
