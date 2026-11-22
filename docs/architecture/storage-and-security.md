# Storage and security

## Storage

**Rates.** Size per hour ≈ (video + audio bitrate) × 3600 / 8, plus about 2% fMP4 overhead.

| Video + audio | Per hour | 3 hours |
|---|---|---|
| 2.5 Mbps + 128 kbps | ≈ 1.2 GB | ≈ 3.6 GB |
| 4 Mbps + 128 kbps | ≈ 1.9 GB | ≈ 5.6 GB |
| 6 Mbps + 128 kbps | ≈ 2.8 GB | ≈ 8.3 GB |

**Phone.**
- Before Start: estimate hours remaining from free space at the chosen preset and show it; warn under 1 hour.
- Reserve: stop recording cleanly (final segment flagged, epoch ended) when free space falls below 500 MB, so the OS and the queue database keep working.
- Never delete an unacknowledged segment. Acknowledged segments are deleted only by the retention setting (default: keep until the user deletes, or the "free space after upload" setting is on).
- Show backlog: count, bytes, and minutes of footage waiting.

**Server.** Configured in the server's environment:

| Setting | Default | On breach |
|---|---|---|
| `SR_STORAGE_MAX_BYTES` (all segments) | 80% of the volume | New uploads get `507 Insufficient Storage` with `Retry-After`; client keeps segments and backs off; dashboard alert |
| `SR_STORAGE_FREE_MIN_BYTES` | 5 GB | Same as above |
| `SR_BROADCAST_MAX_HOURS` | 12 | Broadcast moves to `finishing`; later uploads are archive-only |
| `SR_MAX_ACTIVE_BROADCASTS` | 2 | `POST /v1/broadcasts` returns `429` |
| `SR_ARCHIVE_RETENTION_DAYS` | 30 | Archived segments of finished broadcasts deleted by a daily job; never while the broadcast is active |

Cleanup never touches a segment of an active broadcast, and never deletes before the retention period even if `played_at` is set.

## Security

| Area | Rule | Test |
|---|---|---|
| Ownership | Every broadcast-scoped query filters by the caller's `owner_id`; database composite keys stop cross-account device/destination links | `tests/sql/cross_account.sql`; API test: device of account B gets `404` on every endpoint of A's broadcast |
| Device auth | 256-bit random token, stored as SHA-256, revocable; sent only over TLS | Revoked token gets `401` within one request |
| Stream keys | AES-256-GCM in the database, key from a server secret file; never returned by the API, never logged, never on the phone; `rtmps://` for Twitch/YouTube enforced in schema | Grep of logs from a test run finds no key; API responses checked |
| Replay / duplicates | Uploads are idempotent by `(broadcast, epoch, sequence, sha256)`; a replayed PUT is a no-op `200`; per-device rate limit | Duplicate-upload test in M3 |
| Untrusted media | Before acceptance: size cap, checksum, `ffprobe` with a timeout (5 s) and memory limit in a separate process to check container, codecs (H.264/AAC only), resolution ≤ 1920×1080, duration. Playout decodes only validated segments, in a process with CPU/memory limits | Fuzzed and truncated segments rejected with `422`, server keeps running |
| Paths | Storage keys are built only from server-generated UUIDs and integers: `{broadcast_id}/{epoch}/{sequence}.m4s`; no client string reaches a path; files opened with `O_NOFOLLOW` under one root | Unit test with `../` and symlink attempts |
| Resource exhaustion | Request body limit (`max_segment_bytes`), streaming to disk, connection and rate limits, storage caps above, max active broadcasts | Load test in M7 |
| Admin surface | No admin endpoints on the public port; MediaMTX API/ports bound to the Compose network only | Port scan of a deployed instance in M7 |
