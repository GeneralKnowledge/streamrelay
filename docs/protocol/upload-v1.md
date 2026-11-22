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

- **Device pairing** (once): the dashboard shows a one-time pairing code; the app calls `POST /v1/devices/pair {code, name, platform}` and receives a device token (random 256-bit, shown once, stored in the iPhone Keychain, stored hashed on the server). Answers: `201 {device_id, token}`; `403` for a code that is unknown, used or expired (codes are 8 characters, single use, valid 10 minutes, made with `streamrelay-admin pair-code EMAIL` until the dashboard exists); `429` when pairing attempts pass 10 a minute. `streamrelay-admin revoke DEVICE_ID` revokes a token; every later request answers `401`.
- **Requests:** `Authorization: Bearer <device token>` over TLS. Tokens can be revoked from the dashboard.
- Every broadcast-scoped request checks `broadcast.owner_id == device.owner_id`. A guessed `broadcast_id` from another user returns `404`, the same as a missing one.
- A **finished** broadcast still accepts its owner's init, segment, epoch-end and status requests, so a queue that drains after Stop reaches the archive. A **deleted** broadcast returns `410` to every request.

## Endpoints

### Create a broadcast
```
POST /v1/broadcasts
{ "title": "Pentland Hills walk", "destination_ids": ["..."], "catch_up": {"mode": "catch_up", "max_speed": 1.5, "max_delay_s": null},
  "client_broadcast_ref": "6f1c…" }
→ 201 { "broadcast_id": "0193…", "playout_delay_target_ms": 8000, "max_segment_bytes": 8388608 }
```
`client_broadcast_ref` is a client-generated UUID that makes creation idempotent: retrying with the same ref returns the same broadcast. The app may start recording **before** this succeeds (no signal at the start); segments wait in the queue until it has a `broadcast_id`.

`catch_up.mode`, the broadcaster's choice, changeable during the broadcast:
- `catch_up`: play the backlog faster (`max_speed`) until live.
- `hold`: freeze on the last frame during an outage, then carry on at 1× from exactly where it stopped. Nothing is skipped or sped up; every outage adds its length to the delay. `max_delay_s` sets a limit: `null` (the default) means never speed up and accept any delay (for example a body-cam stream where all footage matters); a number means speed up at `max_speed` only while the delay is above that many seconds, then return to 1×.
- `skip_to_live`: after an outage, jump to the newest footage; the backlog is still uploaded and archived.

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
| `507 Insufficient Storage` | Server storage cap reached ([limits](../architecture/storage-and-security.md#storage)) | Keep the segment, back off per `Retry-After`, show "Server full" |
| network error / timeout | Unknown whether stored | Retry; idempotency makes it safe |

Why one PUT instead of the spec's POST-then-PUT: on a bad connection every round trip is a chance to fail; the metadata fits in headers, and the PUT is idempotent by key + checksum, which gives the same guarantees.

**Resume:** v1 retries the whole segment (≈1 MB). Reserved for later: `Content-Range` partial PUTs with `HEAD …/segments/{epoch}/{sequence}` returning `X-SR-Received-Bytes`.

### End an epoch or the broadcast
```
POST /v1/broadcasts/{id}/epochs/{epoch}/end   { "last_sequence": 899, "reason": "interrupted|app_restart|camera_lost|stopped" }
POST /v1/broadcasts/{id}/finish               { "last_epoch": 2 }
```
**Every epoch gets an end call.** The client sends `epochs/{n}/end` when it closes an epoch (Stop, interruption, restart). After a crash, the relaunched app sends it for the crashed epoch from its manifest, using the last complete segment, before opening the next epoch. An epoch that closed before its first segment completed is ended with `"last_sequence": -1` (empty); it counts as ended and complete with no segments. End calls go through the same queue as segments and are retried the same way.

`finish` means "no more capture" and is sent after the last epoch's end call. The server rejects `finish` with `409` if any epoch up to `last_epoch` has no end call.

**When a backlog counts as complete:** every epoch `0..last_epoch` has an end call, and every sequence `0..last_sequence` of each epoch is stored (nothing, for an empty epoch with `last_sequence: -1`). An empty epoch (`last_sequence: -1`) is complete as soon as its end call arrives; a broadcast whose epochs are all empty finishes as soon as `finish` is accepted.

In `finishing`, the playout plays what its mode plays (`catch_up` and `hold`: everything in order; `skip_to_live`: from its current position onwards) until the backlog is complete and that footage has played. Both modes have the same bound: if the backlog is still incomplete when the **drain wait** (default 15 minutes, per broadcast) expires, the missing segments are marked `skipped`, the playout keeps draining the footage it already has, then shows the end card and closes the outputs. Uploads after that are accepted for the archive only. A stored segment that fails to decode, including the final one, is marked `damaged` and skipped at once; it counts as played, so it never holds up the finish.

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
Answers `204`. Every 5 s while connected. Lets the server tell "phone offline" from "phone online but behind", and feeds the dashboard. Optional for correctness: the server never relies on heartbeats to order media.

## Client upload rules

- **Order:** in `catch_up` and `hold` modes, oldest missing first. In `skip_to_live` mode, newest first, then backfill oldest-first for the archive.
- **Concurrency:** up to 3 segment PUTs in flight over one HTTP/2 connection; drop to 1 when error rate rises.
- **Backoff:** exponential from 1 s to 30 s with full jitter; reset on success. Network-change notifications (`NWPathMonitor`) retry at once.
- **Background:** when the app leaves the foreground, hand the remaining queue to a background `URLSession`; on relaunch, reconcile with `GET status` before resuming.
- **Local deletion:** only after acknowledgement **and** the retention setting allows it (default: keep local copies until the user deletes them; low free space never deletes footage on its own, it stops recording at the reserve and warns, and the "free space after upload" setting is an explicit opt-in) ([phone storage](../architecture/storage-and-security.md#storage)).

## Live and archive guarantees

Two different promises, tested separately:

- **Live completeness.** In `catch_up` and `hold` modes, every segment acknowledged before the playout reaches it is broadcast once, in order, except one that fails to decode, which is marked `damaged` and skipped (≤ about 1 s repeated after a playout crash). A segment that arrives after the playout has moved past it (skip-to-live, drain wait expired, or broadcast already finished) is marked `skipped` for live.
- **Archive completeness.** Every acknowledged segment is kept for the archive, whenever it arrives, including after `finish` and after live playout has ended, until the retention period ([limits](../architecture/storage-and-security.md#storage)) or the user deletes the broadcast. The archive download reassembles all epochs in order.

## Server rules

- Validate: size ≤ `max_segment_bytes`; checksum; `ffprobe` check of container and codecs in a time- and memory-limited process ([untrusted media](../architecture/storage-and-security.md#security)); `start_us` monotonic within the epoch (±50 ms jitter allowed); duration 0.5–4 s, except a segment flagged `final`, which may be any length above 0.
- Writes are **serialised per segment key**: the handler takes a Postgres transaction-scoped advisory lock on `(broadcast_id, epoch, sequence)` before touching storage. The body is first streamed to a private temp file, hashed and fsynced, so a slow upload never holds a transaction open. Then, under the lock: if a row exists with the same checksum, respond `200` and delete the temp file; if a row exists with a different checksum, respond `409` without touching the stored file; otherwise rename the temp file into place, fsync the directory, insert the row and **commit** (Postgres `synchronous_commit` on, the default), which releases the lock. Only then respond. No acknowledgement is sent before the commit succeeds, and a stored file is never replaced.
- A crash after the rename but before the commit leaves a file with no row (the lock dies with the transaction). The startup scan runs to completion **before the server accepts any segment `PUT`**, so it can never race a live write, and it **deletes** such files: the client never got an acknowledgement, so it still holds the segment and its metadata and will upload it again. A file with a committed row is never deleted by the scan.
- The playout never plays a segment twice in normal running. After a playout crash it resumes from its last checkpoint, so up to about one second can repeat ([playout crash recovery](../architecture/playout.md#crash-recovery)).
- Rate limit per device (default 20 segment PUTs/s, far above the 0.5/s a live client needs plus catch-up).
