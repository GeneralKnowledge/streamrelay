# Milestones

Each milestone ends with a demonstration and an acceptance test that a script or a person can run. The order proves the hard media problem before any polish. **Device** marks the milestones that need the iPhone; M1 needs none.

| | Milestone | Needs | Proves |
|---|---|---|---|
| M1 | Server media pipeline prototype | Linux only | One unbroken output through upload, outage, holding screen and catch-up |
| M2 | iPhone recording engine | **iPhone** | Phone produces correct segments and never loses them |
| M3 | Reliable upload | Linux, then **iPhone** | No segment lost or played twice through repeated disconnects |
| M4 | Hybrid recovery, end to end | **iPhone** | M1's result with a real phone and real outages |
| M5 | Multi-destination | Linux + Twitch/YouTube test accounts | Destinations fail independently; platforms tolerate our stream |
| M6 | Product-quality iPhone app | **iPhone** | An ordinary person can broadcast without understanding it |
| M7 | Web dashboard | Browser | A whole broadcast managed from the dashboard |
| M8 | Self-hostable release | Fresh Linux VPS | A third party deploys it unaided |

Later (not scheduled): Android client, adaptive bitrate, separate archive/broadcast quality, audio-only capture through interruptions, own viewer page with per-viewer catch-up choice, OAuth destination connections, several broadcasters per server.

---

## M1: Server media pipeline prototype

**Goal.** Answer the question the whole product depends on: can independently uploaded segments, with a 5-minute hole, become one continuous stream that never drops, with a holding screen and a catch-up that sounds right?

**Built.**
- `tools/fake-phone`: turns a test clip into fMP4 init + 2 s segments exactly as AVAssetWriter will (FFmpeg's HLS muxer in fMP4 mode), then uploads them in real time with protocol v1. Can pause, reorder, duplicate and corrupt uploads on command.
- `tools/testclip`: generates the source: a frame counter and broadcast time burned into each frame as a machine-readable block, plus a 1 kHz tone with a beep every second.
- Minimal API: create broadcast, PUT init, PUT segment, status (protocol v1, no auth beyond a static token yet), filesystem store, Postgres.
- Playout (ADR 0005) with states `starting`, `live`, `holding`, `catching_up`, `finishing`.
- MediaMTX in Docker; one forwarder to a **second** MediaMTX standing in for Twitch.
- `tools/verify`: records the "platform" side and checks it automatically.
- Network faults with `tc netem` in a network namespace (outage, 300 ms latency, 5% loss, uplink capped to 3 and 6 Mbps).

**Acceptance test** (`tests/media-continuity/m1_outage.sh`, one command, ≈40 min):
1. 30-minute test clip at 720p30, 4 Mbps. Fake phone uploads in real time.
2. At minute 5 the network namespace drops all traffic for 5 minutes, then restores it (uplink 6 Mbps).
3. Pass when all of these hold on the recording taken from the stand-in platform:
   - Exactly **one** publish session at the stand-in for the whole run (MediaMTX log).
   - Output timestamps strictly monotonic; no gap between frames over 1.5 frame durations; no audio gap over 25 ms.
   - Holding screen visible within `buffer_target` + 2 s of the outage starting.
   - Every source second from 0 to 30:00 appears, in order, none twice (frame-counter blocks; dropped frames inside a second are allowed during catch-up).
   - During catch-up the tone is still 1 kHz ±2% (pitch preserved) and beeps arrive every 1/1.25 s.
   - Back in `live` within 25 minutes of reconnection (5 min backlog at 1.25× = 20 min, plus upload time).
4. Also passes: the same run with segments uploaded out of order, with 10% duplicate uploads, and with one corrupted upload (rejected by checksum, re-sent).
5. Kill the playout process at minute 12: the stand-in still shows one session or a single reconnect under 5 s, and no footage is lost or repeated. (Records how forwarders bridge a playout restart; input for risk R3.)

**Decision gate.** CPU use at real time on a 4-vCPU machine (≤ 70% of one core for the encoder), and a listening test of 1.25× and 1.5× speech. If either fails, switch engine (ADR 0005) before M2.

## M2: iPhone recording engine (Device)

**Goal.** The phone records 2 s segments to disk, whatever the network does, and recovers its queue after a restart.

**Built.** Forge app (SwiftUI + Rust core). `AVCaptureSession` → `AVAssetWriter` (fMP4, 2 s) → files; Rust core manifest (SQLite) with epochs and sequence numbers; storage estimate and low-space warning; interruption handling (background, call, thermal) that closes the epoch cleanly; minimal UI: preview, record/stop, timer, queue size. No uploading yet. The segments can be pulled off the phone with `forge device` tooling for inspection.

**Acceptance test.**
- Airplane mode on, record 30 minutes: 900 segments, each starting with an IDR frame, each 2.0 s ± 1 frame; joining them with the init plays without a visible or audible seam (checked on Linux with `tools/verify` in file mode).
- Force-quit the app during recording, reopen: the queue lists every completed segment; the partial one is either complete or cleanly discarded; a new epoch starts.
- Lock the screen for 30 s during recording: the epoch ends with reason `interrupted`, a new one starts on unlock, and the timeline gap is 30 s.

**Phone note.** Needs a slot decision first (risk R11).

## M3: Reliable upload

**Goal.** Protocol v1 complete on both sides: auth, pairing, idempotency, status reconciliation, background upload.

**Built.** Server: device pairing and tokens, ownership checks, rate limits, all response codes, crash repair at startup. Rust core: upload manager (ordering, concurrency, backoff, network-change retry, reconcile on launch); Swift: foreground and background `URLSession`.

**Acceptance test.**
- Linux chaos test (fake phone): 2 hours of random 5–120 s outages, 0–10% loss, server restarts every 20 minutes. At the end every segment is stored once with the right checksum, and the server log shows no 409.
- iPhone: record 20 minutes while toggling airplane mode every 1–3 minutes; background the app for the last 5 minutes after stopping. Status shows every segment received; none missing, none duplicated.
- Security: a second account's device token gets 404 on the first account's broadcast for every endpoint (automated).

## M4: Hybrid recovery, end to end (Device)

**Goal.** M1's playout driven by a real phone.

**Built.** Playout supervisor started by broadcast creation; heartbeats; recovery status shown on the phone (Live / Holding / Catching up, minutes behind); catch-up mode setting; finishing and end card.

**Acceptance test.** Walk out of coverage (or airplane mode) for 5 minutes during a 30-minute broadcast; viewer on OBS/ffplay watching the stand-in sees: footage, holding screen, footage again at 1.25×, then live; one session throughout; `tools/verify` passes the M1 checks on the recording. Repeat in skip-to-live mode: footage jumps to near-live within 15 s of reconnection; the archive still holds all footage.

## M5: Multi-destination

**Built.** Destinations (encrypted keys), per-destination forwarders with reconnect and health, dashboard-free control through the API.

**Acceptance test.**
- Two stand-in destinations; block one with `iptables` for 2 minutes: the other's session never drops; the blocked one reconnects by itself.
- Real Twitch and YouTube test channels, one 45-minute broadcast with a 5-minute outage: both stay live without a viewer refresh; record what each platform shows (stream health warnings, VOD continuity). This is where risk R3 is settled.

## M6: Product-quality iPhone app (Device)

**Built.** Broadcast setup (title, destinations, quality, catch-up mode), camera preview with switch and zoom, audio meter, connection and backlog status, storage estimate, pocket mode (dimmed screen), Bluetooth microphones, ForgeUI styling, accessibility identifiers and VoiceOver labels.

**Acceptance test.** A person who has not seen the app before starts a broadcast to a test channel, walks through a dead zone, and ends it, with no help; afterwards they can say what the "behind live" number meant.

## M7: Web dashboard

**Built.** Sign-in, devices and pairing, destinations, live monitor (state, delay, backlog, destination health, phone battery/thermal, preview), history with event timeline, recording download, storage usage and deletion.

**Acceptance test.** Create destinations, pair a phone, monitor a broadcast through an outage, download its recording and delete it, all from the dashboard.

## M8: Self-hostable release

**Built.** Docker Compose for API, Postgres, MediaMTX, playout; S3-compatible storage option; install guide, API docs (generated), config examples, Prometheus metrics + a Grafana dashboard, backups, a security review (threat model, dependency audit, secrets handling), licence choice.

**Acceptance test.** Someone other than Reuben follows the guide on a fresh Ubuntu VPS with a domain and gets a phone broadcasting through it.
