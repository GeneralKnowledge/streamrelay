# Repository status

Audit of `GeneralKnowledge/streamrelay` on 2026-11-22, from the repository itself, not from PR descriptions.

## What exists

| Where | Contents |
|---|---|
| `main` (772d8e5) | A one-paragraph `README.md`. Nothing else. |
| PR #1 (open, docs only) | ADRs 0001–0009, upload protocol v1, playout design, Postgres schema, milestones, risks, issue list, and (from this review) a cross-account SQL test |
| Issues | None |
| Code, CI, Compose files, iOS project | None |

## Assessment

| State | Items |
|---|---|
| **Implemented** | Nothing that runs, except: `schema.sql` loads into PostgreSQL 16, and `tests/sql/cross_account.sql` proves the database rejects cross-account links (device, destination) and unencrypted platform ingest URLs. |
| **Partially implemented** | None. Everything below is a written design. |
| **Specified, not started** | Upload protocol v1; playout state machine; schema; destination forwarders; segment store; iPhone capture and queue; all tests and tools named in the milestones. |
| **Technically uncertain** (proved or disproved only by running code) | 1. One PyAV process can switch between decoded segments, generated holding frames and 1.25–1.5× catch-up while publishing one unbroken RTMP stream in real time on a small VPS. 2. AVAssetWriter's fMP4 segment output gives 2 s IDR-aligned segments with continuous audio across boundaries that join cleanly. 3. `atempo` speech at 1.25–1.5× is acceptable. 4. How the iPhone behaves for capture under lock, background, calls, Low Power Mode, thermal pressure and low storage (we know the documented rules, not the measured behaviour). 5. Twitch and YouTube behaviour over long holding periods and after a forwarder reconnect. 6. Whether the phone's uplink leaves enough headroom to clear a backlog in practice. |
| **Needs a decision (Reuben)** | See [Decisions needed](#decisions-needed). |

## Consistency check of the current docs

Found and fixed in this review:

- **Cross-account links were possible.** `broadcasts.device_id` and `broadcast_destinations.destination_id` were plain foreign keys, so a broadcast could reference another account's device or destination. Now composite keys `(id, owner_id)` tie them to one owner, and `tests/sql/cross_account.sql` checks it.
- **Encrypted ingest was only prose.** ADR 0006 said Twitch/YouTube must use `rtmps://`; the schema now enforces it with a check, and plain `rtmp://` needs an explicit opt-in on a custom destination.
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
| Network faults | `tc netem` in a network namespace | A proxy-based simulator |
| Media checks | `ffprobe` JSON output | A parser |
| iOS build and install from Linux | The existing ixOS/xtool workflow and build queue on the laptop | A Mac or CI-only build |
| Background uploads on iPhone | Background `URLSession` | Our own scheduler |

## Decisions needed

1. **Language of the iPhone queue.** The follow-up asks for native Swift + AVFoundation. The ixOS convention puts state in a Rust core. Recommendation: Swift-only for the M2 capture spike; decide before M3 whether the queue and uploader move into a Rust core (gains: shared protocol code with a future Android client and Linux tests; cost: a UniFFI boundary).
2. **iPhone slot for the M2 spike.** All three free-team slots are used. Options: a StreamRelay screen inside Device Lab (no new slot, shares its bundle id), or temporarily replace the WebDriverAgent slot.
3. **Where the relay server runs for phone tests (M4).** Hetzner box (reachable on mobile data) recommended over the laptop (sleeps).
4. **Stream-key transport rule** (open review thread on ADR 0006): rtmps-only for Twitch/YouTube, plain rtmp only with explicit opt-in for custom destinations.
