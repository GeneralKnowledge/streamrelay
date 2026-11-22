# Repository status

## What exists

Updated 2026-11-22 (evening), from the repository.

| State | Items |
|---|---|
| **Done, with evidence** | **M1 (media pipeline proof)**: upload API, durable segment store, playout service (skip-to-live, checkpoints), supervisor, testclip, fake phone, verify, Compose and CI; `m1_gaps.sh` passed every gate ([results](../../tests/results/m1/2026-11-22/RESULTS.md)). Spike S1 passed every measured gate ([results](../../spikes/s1_playout/RESULTS.md)); the listening check is still open. `tests/sql/cross_account.sql` passes. |
| **Built, Linux part tested, phone part waiting** | **M3 server**: pairing and device tokens, revocation, ownership checks, media validation (ffprobe), storage caps (`507`), rate limits, path safety, heartbeat, late uploads archived as `skipped`; unit tests in `tests/unit/test_m3.py`. **M3 chaos harness** and its 2-hour run: 3600/3600 segments stored once with the right checksum through 28 outages and 6 API kills ([results](../../tests/results/m3/2026-11-22/RESULTS.md)). **iPhone app** (`ios/`): the upload core (manifest, protocol client, queue) is tested on Linux, in CI, and against the real server through the bad-network proxy; the recorder, the condition handling, the background hand-off and the screens are written but **not yet compiled** (they need the iOS SDK: laptop xtool build). **Recording checker** for pulled phone folders. |
| **Waiting on the phone** | M2 segment validity, reconstruction and condition matrix; the M3 iPhone run. Steps in [phone-sitting-m2-m3.md](phone-sitting-m2-m3.md). |
| **Not started** | M4 catch-up speed stage and end-to-end proof, M5 destinations, M6, M7. |
| **Technically uncertain** | 1. AVAssetWriter's fMP4 output gives 2 s IDR-aligned segments with continuous audio that join cleanly (M2). 2. `atempo` speech at 1.25–1.5× is acceptable. 3. How the iPhone behaves under lock, background, calls, Low Power Mode, thermal pressure and low storage (M2). 4. Twitch and YouTube behaviour over long holding periods. 5. Whether the phone's uplink leaves headroom to clear a backlog. |
| **Needs a decision (Reuben)** | None open. |

## Consistency check of the current docs

Found and fixed in this review:

- **Cross-account links were possible.** `broadcasts.device_id` and `broadcast_destinations.destination_id` were plain foreign keys, so a broadcast could reference another account's device or destination. Now composite keys `(id, owner_id)` tie them to one owner, and `tests/sql/cross_account.sql` checks it.
- **Encrypted ingest was only prose.** ADR 0006 said Twitch/YouTube must use `rtmps://`; the schema now enforces `rtmps://` for every destination with a check (encrypted only, Reuben 2026-11-22).
- **Live completeness and archive completeness were blurred.** The protocol now states both guarantees separately ([upload-v1](../protocol/upload-v1.md#live-and-archive-guarantees)).
- **Storage limits were missing.** No server storage cap, broadcast duration cap or concurrency cap existed; now specified in [storage-and-security](../architecture/storage-and-security.md), with a `507` response.
- **Untrusted media and path handling were unspecified.** Now specified in the same doc.
- **Milestone order** has been changed to the order in Reuben's follow-up review (media pipeline proof first, OBS before Twitch). The earlier "M1 = phone to Twitch" plan is folded into M1–M4, and the end-to-end phone demonstration is the M4 exit test.

Checked and consistent: protocol endpoints ↔ schema tables (`inits`, `epochs`, `segments`, `playout_checkpoints`); playout `finishing` rules ↔ protocol `finish` rules; crash guarantee (≤ about 1 s repeated, nothing skipped) stated the same in playout and protocol.

## Reuse

| Need | Reuse | Not building |
|---|---|---|
| fMP4 segmenting on iPhone | `AVAssetWriter` with `.mpeg4AppleHLS` profile and `AVAssetWriterDelegate` | A custom muxer (unless the M2 spike disproves it) |
| fMP4 segments for the bench | FFmpeg's HLS muxer (`-hls_segment_type fmp4`) | A Python muxer |
| Decode/encode/filters | PyAV (FFmpeg libraries), `atempo` | Our own codec glue |
| RTMP server, preview, fan-out hub | MediaMTX | An RTMP server |
| Per-destination push | `ffmpeg -c copy` processes | An RTMP client |
| Network faults | A small user-space TCP proxy (`tools/chaos/proxy.py`): runs on any CI runner without root; loss is modelled as mid-request resets, its visible effect over TCP | `tc netem` (needs root and namespaces) |
| Media checks | `ffprobe` JSON output | A parser |
| iOS build and install from Linux | The existing ixOS/xtool workflow and build queue on the laptop | A Mac or CI-only build |
| Background uploads on iPhone | Background `URLSession` | Our own scheduler |

## Decisions made (Reuben, 2026-11-22)

1. **Language of the iPhone queue: Swift.** The app, its queue and its uploader stay native Swift for now. No Rust core is planned; revisit only if an Android client or Linux tests of the queue make the case.
2. **iPhone slot: its own app for the test.** Reuben deletes an installed app for a phone sitting and StreamRelay installs as its own app. It always reuses one bundle id, so it never spends more than one of the free team's 10 new App IDs a week.
3. **Relay server for phone tests (M4): the Hetzner box.** It is reachable on mobile data; the laptop sleeps.
4. **Stream keys: encrypted only.** Every destination uses `rtmps://`. No plain-rtmp opt-in in v1 ([ADR 0006](../decisions/0006-mediamtx-fanout.md)).
