# Issues to create

One tracking issue per milestone, with the task issues below as sub-issues. Labels: `milestone:M1`…`M8`, `area:server`, `area:iphone`, `area:protocol`, `area:dashboard`, `area:infra`, `needs-device`. Create them once the plan is approved.

## M1: Server media pipeline prototype
- [ ] Repository skeleton: `services/`, `packages/protocol/`, `tools/`, `tests/`, Docker Compose with Postgres + MediaMTX, CI (ruff, mypy, pytest)
- [ ] `tools/testclip`: frame-counter + timecode + 1 kHz tone/beeps generator
- [ ] `tools/fake-phone`: fMP4 segmenter + real-time protocol v1 uploader with pause/reorder/duplicate/corrupt controls
- [ ] API: create broadcast, PUT init, PUT segment, status; filesystem store with fsync-then-ack
- [ ] Postgres schema v1 + Alembic baseline
- [ ] Playout: scheduler + state machine (`starting/live/holding/catching_up/finishing`)
- [ ] Playout: PyAV decode/encode with monotonic output clock, publish to MediaMTX
- [ ] Playout: holding screen and end card (moving element, comfort noise)
- [ ] Playout: speed stage (frame drop + `atempo`), speed ramping
- [ ] Playout: checkpoint + restart recovery
- [ ] Forwarder to stand-in platform (second MediaMTX)
- [ ] `tools/verify`: session count, PTS monotonic, gaps, frame-counter order, tone pitch, beep timing
- [ ] `tests/network-failure`: netem namespace helpers (outage, latency, loss, uplink cap)
- [ ] `tests/media-continuity/m1_outage.sh` and variants (out of order, duplicates, corruption, playout kill)
- [ ] Decision gate report: CPU, listening test, engine decision

## M2: iPhone recording engine (`needs-device`)
- [ ] Slot decision (Device Lab screen or own slot)
- [ ] Forge app skeleton (SwiftUI + Rust core crate, bundle id reused per slot decision)
- [ ] Capture session + AVAssetWriter fMP4 2 s segments
- [ ] Rust core: manifest (SQLite), epochs, sequences, storage estimate
- [ ] Interruptions: background, lock, call, thermal → clean epoch end
- [ ] Restart recovery of the queue
- [ ] `tools/verify` file mode for pulled segments
- [ ] Phone run: 30 min airplane mode, force-quit, lock-screen gap

## M3: Reliable upload
- [ ] Pairing codes + device tokens + revocation
- [ ] Ownership checks and 404 behaviour, rate limits
- [ ] All protocol response codes; startup repair scan
- [ ] Rust core upload manager (ordering, concurrency, backoff, reconcile)
- [ ] Background URLSession hand-off and relaunch reconciliation
- [ ] OpenAPI/JSON Schema generated into `packages/protocol/`, checked in CI
- [ ] 2-hour Linux chaos test; iPhone airplane-toggle run; cross-account security test

## M4: Hybrid recovery end to end (`needs-device`)
- [ ] Playout supervisor tied to broadcast lifecycle
- [ ] Heartbeats and offline detection
- [ ] Phone recovery status UI; catch-up mode setting
- [ ] Phone run: 5-minute outage in both modes

## M5: Multi-destination
- [ ] Encrypted stream keys; destination CRUD API
- [ ] Per-destination forwarder supervision, health, reconnect
- [ ] Platform terms check (R9)
- [ ] Stand-in isolation test; real Twitch + YouTube 45-minute run

## M6: Product-quality iPhone app (`needs-device`)
- [ ] Broadcast setup, preview controls, audio meter, Bluetooth mics
- [ ] Status: connection, backlog, behind-live, storage, battery/thermal
- [ ] Pocket mode
- [ ] Accessibility identifiers + VoiceOver labels
- [ ] Unassisted first-use test

## M7: Web dashboard
- [ ] Next.js app on the public API; auth
- [ ] Devices/pairing, destinations, live monitor with preview, history/events, downloads, storage
- [ ] Full-broadcast-from-dashboard test

## M8: Self-hostable release
- [ ] Compose production profile, TLS, backups
- [ ] S3-compatible store backend
- [ ] Docs: install, configuration, API, operations; Prometheus metrics + Grafana
- [ ] Security review; licence; name check (R12)
- [ ] Third-party install test
