# Issues to create

One tracking issue per milestone, with the task issues below as sub-issues. Labels: `milestone:M1`…`M8`, `area:server`, `area:iphone`, `area:protocol`, `area:dashboard`, `area:infra`, `needs-device`. Create them once the plan is approved.

## M1: First run, iPhone to Twitch on unstable internet
Step A, server and bench (no phone):
- [ ] Repository skeleton: `services/`, `packages/protocol/`, `tools/`, `tests/`, Docker Compose (Postgres, MediaMTX, API, playout), CI (ruff, mypy, pytest)
- [ ] Postgres schema v1 + Alembic baseline
- [ ] API subset: create broadcast, PUT init, PUT segment, epoch end, finish, status; static token; filesystem store with fsync-then-ack
- [ ] `tools/testclip`, `tools/fake-phone`, `tools/verify`, netem helpers
- [ ] Playout: scheduler + states; PyAV decode/encode with monotonic output clock; holding screen; frame drop + `atempo` at 1.25×
- [ ] Forwarder to Twitch (key from server config) and to a local stand-in
- [ ] `m1_outage.sh` and its variants pass; one Twitch bandwidth-test run
- [ ] Decision gate report: CPU headroom, listening test

Step B, iPhone app (`needs-device` for the run only):
- [ ] Slot decision (Device Lab screen or swap a slot)
- [ ] Forge app skeleton: SwiftUI + Rust core crate
- [ ] AVCaptureSession + AVAssetWriter fMP4 2 s segments to disk
- [ ] Rust core queue (SQLite manifest, epochs, sequences) and foreground uploader with retry, backoff, reconcile
- [ ] Preview, Start/Stop, status line, settings (server URL, token)

Step C, the run:
- [ ] Platform terms check (R9), before deploying and the run
- [ ] Server deployed where the phone can reach it (Hetzner recommended) with TLS
- [ ] 30-minute phone run to Twitch with a 3–5 minute outage; results recorded per check

## M2: Recording engine hardening (`needs-device`)
- [ ] Interruptions: lock, background, call, Siri, camera taken, thermal → clean epoch end with reason
- [ ] Force-quit/crash recovery; partial-segment cleanup
- [ ] Storage estimate and low-space warning; thermal presets
- [ ] `tools/verify` file mode for pulled segments
- [ ] Phone run: 30 min airplane mode, force-quit, lock-screen gap

## M3: Upload hardening
- [ ] Pairing codes + device tokens + revocation
- [ ] Ownership checks and 404 behaviour, rate limits
- [ ] All protocol response codes; startup repair scan
- [ ] Background URLSession hand-off and relaunch reconciliation; concurrency; network-change retry
- [ ] OpenAPI/JSON Schema generated into `packages/protocol/`, checked in CI
- [ ] 2-hour Linux chaos test; iPhone airplane-toggle run; cross-account security test

## M4: Recovery hardening
- [ ] Skip-to-live mode
- [ ] Playout checkpoint + restart recovery
- [ ] Epoch-gap card, speed ramps, 1.5× option
- [ ] Heartbeats, offline detection, phone recovery status UI
- [ ] Bench playout-kill and long-outage tests; phone skip-to-live run

## M5: Multi-destination
- [ ] Encrypted stream keys; destination CRUD API
- [ ] Per-destination forwarder supervision, health, reconnect
- [ ] Stand-in isolation test; real Twitch + YouTube 45-minute run with a forced playout restart

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
