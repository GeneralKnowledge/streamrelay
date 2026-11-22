# Milestones

Each milestone ends with a demonstration and an acceptance test that a script or a person can run. **Device** marks milestones with an iPhone sitting.

| | Milestone | Needs | Proves |
|---|---|---|---|
| M1 | **First run: iPhone to Twitch on unstable internet** | Linux bench, then **iPhone**, a reachable server, a Twitch account | The whole idea works end to end, roughly |
| M2 | Recording engine hardening | **iPhone** | The phone never loses footage: restarts, interruptions, storage |
| M3 | Upload hardening | Linux, then **iPhone** | No segment lost or played twice through hours of chaos; pairing and security |
| M4 | Recovery hardening | Linux, then **iPhone** | Skip-to-live, playout restarts, long outages, cellular uplink limits |
| M5 | Multi-destination | Linux + Twitch/YouTube | Destinations fail independently; YouTube as well as Twitch |
| M6 | Product-quality iPhone app | **iPhone** | An ordinary person can broadcast without understanding it |
| M7 | Web dashboard | Browser | A whole broadcast managed from the dashboard |
| M8 | Self-hostable release | Fresh Linux VPS | A third party deploys it unaided |

M1 is a thin slice through every layer (Reuben, 2026-11-22: "the first run … end to end … livestream from the phone on unstable internet and streaming on Twitch"). Later milestones harden each layer; none of them adds a new layer before M7.

Later (not scheduled): Android client, adaptive bitrate, separate archive/broadcast quality, audio-only capture through interruptions, own viewer page with per-viewer catch-up choice, OAuth destination connections, several broadcasters per server.

---

## M1: First run, iPhone to Twitch on unstable internet

**Goal.** Livestream from the iPhone through a connection that drops, and watch it on Twitch without the stream ending: footage, a holding screen while the phone is offline, then the missed footage at 1.25× until it is live again. Rough edges are fine; lost or repeated footage is not.

**Built, in three steps.** Step A is done on Linux first so the phone sitting only tests the phone.

*A. Server and Linux bench (no phone).*
- Repository skeleton, Docker Compose (API, Postgres, MediaMTX, playout).
- API subset of protocol v1: create broadcast, PUT init, PUT segment, status. One static device token from the server's config file (pairing comes in M3).
- Filesystem segment store, fsync before acknowledging.
- Playout (ADR 0005) with `starting`, `live`, `holding`, `catching_up`, `finishing`: PyAV decode, frame-drop + `atempo` at 1.25×, one x264 encoder, holding screen with a moving clock and quiet audio.
- One forwarder to Twitch (stream key in the server's config file, never in the repo), plus a local stand-in MediaMTX for tests.
- `tools/testclip`, `tools/fake-phone` (fMP4 segments as AVAssetWriter makes them, real-time upload, pause/reorder/duplicate/corrupt), `tools/verify`, netem helpers.

*B. iPhone app, minimum.*
- Forge app: SwiftUI preview, Start/Stop, status line (Recording · Uploading · N segments waiting · state from the server).
- `AVCaptureSession` → `AVAssetWriter` fMP4 2 s segments → files; Rust core keeps the queue (SQLite manifest, epochs, sequences) and uploads oldest first with retry, backoff and a status reconcile on launch. Foreground only; screen kept awake.
- Server address and token typed into a settings screen.

*C. A server the phone can reach over mobile data.* Choice for Reuben before step C (see "Needs from Reuben").

**Acceptance test.**
1. *Bench (step A, scripted):* `tests/media-continuity/m1_outage.sh` (30-minute test clip, 5-minute total outage at minute 5, uplink capped at 6 Mbps) passes against the stand-in: one publish session throughout; strictly monotonic timestamps with no video gap over 1.5 frames or audio gap over 25 ms; holding screen within `buffer_target` + 2 s; every source second appears once and in order; tone stays 1 kHz ±2% during catch-up; back to `live` within 25 minutes. Variants pass with out-of-order, duplicated and corrupted uploads. Then the same run once to Twitch using Twitch's bandwidth-test mode (`?bandwidthtest=true` on the key), which reaches the ingest without going public.
2. *Phone run (the M1 result, ≈30 minutes, iPhone):* start a broadcast to Twitch; a second device watches the Twitch page and never refreshes. At about minute 5, turn on airplane mode (or walk out of coverage) for 3–5 minutes, then reconnect. Pass when:
   - the Twitch player shows footage, then the holding screen, then footage again without the stream ending or the page needing a refresh;
   - after reconnecting, the missed minutes play at 1.25× with understandable audio, then the "behind live" figure returns to about 8 s;
   - the Twitch VOD (or the server's archive, if the VOD is split) contains every minute recorded on the phone, none twice;
   - the server's event log shows one Twitch session for the whole broadcast.
   Results are written down per check; a check that wasn't seen is recorded as not run.

**Not in M1** (comes later): pairing and per-device tokens, background uploads, interruptions beyond "end the epoch", skip-to-live, playout crash recovery, YouTube, more than one destination, dashboard, polished UI.

**Decision gate after step A.** Real-time CPU headroom on the target server and a listening test at 1.25× and 1.5×. If either fails, switch the playout engine (ADR 0005) before the phone run.

**Needs from Reuben.**
- A Twitch account to test on and its stream key (into the server config file on the server, not into chat or the repo).
- Where the server runs for the phone run: the existing Hetzner box (reachable over mobile data, already has Caddy for TLS), or the laptop over Tailscale (phone needs Tailscale on; the laptop must stay awake). Recommended: Hetzner, because the test is about mobile networks and the laptop sleeps.
- An iPhone slot (risk R11): a StreamRelay screen inside Device Lab, or swapping out the VPN or WebDriverAgent slot for the sitting.
- One phone sitting of about 30 minutes, on mobile data.

## M2: Recording engine hardening (Device)

**Goal.** The phone never loses footage it has recorded.

**Built.** Interruption handling (lock, background, phone call, Siri, other camera apps, thermal) that closes the epoch cleanly with a reason; crash and force-quit recovery of the queue; partial-segment cleanup; storage estimate before start and low-space warning; thermal-aware presets; `tools/verify` file mode for segments pulled off the phone.

**Acceptance test.**
- Airplane mode on, record 30 minutes: 900 segments, each starting with an IDR frame, each 2.0 s ± 1 frame; joined with the init they play without a visible or audible seam (checked on Linux).
- Force-quit during recording, reopen: every completed segment is in the queue; the partial one is complete or cleanly discarded; a new epoch starts.
- Lock the screen for 30 s: the epoch ends with reason `interrupted`, a new one starts on unlock, and the timeline gap is 30 s.

## M3: Upload hardening

**Goal.** Protocol v1 complete and secure on both sides.

**Built.** Server: pairing codes, device tokens, revocation, ownership checks (404 for other people's broadcasts), rate limits, every response code, startup repair scan. Phone: background `URLSession` hand-off after the app leaves the foreground and reconciliation on relaunch; concurrency and network-change retry. OpenAPI/JSON Schema generated into `packages/protocol/` and checked in CI.

**Acceptance test.**
- Linux chaos test (fake phone): 2 hours of random 5–120 s outages, 0–10% loss, server restarts every 20 minutes. Every segment stored once with the right checksum; no 409 in the log.
- iPhone: record 20 minutes while toggling airplane mode every 1–3 minutes, then stop and background the app with a backlog: status shows every segment received, none missing or duplicated.
- A second account's device gets 404 on the first account's broadcast for every endpoint (automated).

## M4: Recovery hardening

**Goal.** Recovery that holds up beyond the M1 happy path.

**Built.** Skip-to-live mode; playout checkpoint and restart; epoch gaps shown as a short "Camera interrupted" card; speed ramps; 1.5× option; heartbeats and offline detection; phone shows Live / Holding / Catching up and minutes behind; lower default bitrate on cellular.

**Acceptance test.**
- Bench: kill the playout at minute 12 of the M1 bench run; the stand-in sees one session or one reconnect under 5 s, and no footage is lost or repeated. 20-minute outage with a 4 Mbps uplink cap: catches up, or the report says how long it would take.
- Phone: repeat the M1 phone run in skip-to-live mode: footage jumps to near-live within 15 s of reconnecting; the archive still holds every minute.

## M5: Multi-destination

**Built.** Destinations (encrypted keys), per-destination forwarders with reconnect and health, dashboard-free control through the API.

**Acceptance test.**
- Two stand-in destinations; block one with `iptables` for 2 minutes: the other's session never drops; the blocked one reconnects by itself.
- Real Twitch and YouTube test channels, one 45-minute broadcast with a 5-minute outage: both stay live without a viewer refresh; record what each platform shows (stream health warnings, VOD continuity). M1 settles risk R3 for Twitch; this settles it for YouTube.

## M6: Product-quality iPhone app (Device)

**Built.** Broadcast setup (title, destinations, quality, catch-up mode), camera preview with switch and zoom, audio meter, connection and backlog status, storage estimate, pocket mode (dimmed screen), Bluetooth microphones, ForgeUI styling, accessibility identifiers and VoiceOver labels.

**Acceptance test.** A person who has not seen the app before starts a broadcast to a test channel, walks through a dead zone, and ends it, with no help; afterwards they can say what the "behind live" number meant.

## M7: Web dashboard

**Built.** Sign-in, devices and pairing, destinations, live monitor (state, delay, backlog, destination health, phone battery/thermal, preview), history with event timeline, recording download, storage usage and deletion.

**Acceptance test.** Create destinations, pair a phone, monitor a broadcast through an outage, download its recording and delete it, all from the dashboard.

## M8: Self-hostable release

**Built.** Docker Compose for API, Postgres, MediaMTX, playout; S3-compatible storage option; install guide, API docs (generated), config examples, Prometheus metrics + a Grafana dashboard, backups, a security review (threat model, dependency audit, secrets handling), licence choice.

**Acceptance test.** Someone other than Reuben follows the guide on a fresh Ubuntu VPS with a domain and gets a phone broadcasting through it.
