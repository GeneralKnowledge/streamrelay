# Milestones

Order from Reuben's follow-up review (2026-11-22): prove the media pipeline, then the phone, then upload, then recovery end to end, and only then destinations, app polish and dashboard. A milestone is complete only when its **evidence** exists in the repository (test output, logs, media samples, a results file), never because its documentation exists. **Device** marks milestones that need an iPhone sitting.

| | Milestone | Needs | Exit evidence |
|---|---|---|---|
| M1 | Media pipeline proof | Linux only | One unbroken RTMP output through gaps, holding media and back, verified by script |
| M2 | iOS capture proof | **iPhone**, laptop build | Real phone segments that pass `ffprobe` and rebuild into one clean recording; measured behaviour under 7 conditions |
| M3 | Reliable upload | Linux, then **iPhone** | Chaos run: every segment stored once, durable acks, reconciliation after restarts, cross-account isolation |
| M4 | Hybrid recovery and **end-to-end proof** | Linux, then **iPhone**, a reachable server, OBS | The 10-step outage scenario, iPhone → relay → OBS, repeatable, with logs and media kept |
| M5 | Destination fanout | Linux, Twitch and YouTube test channels | Independent failure of destinations; real-platform behaviour recorded |
| M6 | Product-quality iPhone client | **iPhone** | Unassisted field use |
| M7 | Dashboard and self-hosted release | Browser, fresh VPS | Third-party install; security checks |

The first major result Reuben asked for, iPhone → local recording → interrupted network → recovered upload → continuous relay broadcast → viewer, is the **M4 exit test**. M1–M3 each remove one risk that would otherwise make that test impossible to debug.

Later, not scheduled: Android client (same protocol, no server change), adaptive bitrate, separate archive and broadcast quality, audio-only capture through interruptions, own viewer page with per-viewer catch-up choice, OAuth destination connections.

---

## M1: Media pipeline proof (Linux, no phone)

**Goal.** Show that the server can turn independently arriving fMP4 segments into one continuous RTMP output, switching to holding media during gaps and back, without the output ever reconnecting.

**Starts with** [spike S1](spike-s1-playout.md) (the riskiest assumption). If S1 fails its gate, the playout engine choice (ADR 0005) is revisited before anything else is built.

**Built.**
- Repository skeleton: `services/`, `tools/`, `tests/`, Docker Compose (Postgres, MediaMTX, API, playout), CI running lint, unit tests and the SQL tests.
- `tools/testclip`: test source with a frame counter and broadcast time burned in as a machine-readable block, a 1 kHz tone and a beep every second.
- `tools/fake-phone`: makes fMP4 init + 2 s segments with FFmpeg's HLS muxer and uploads them in real time over protocol v1; can pause, reorder, duplicate and corrupt.
- API subset: create broadcast, PUT init, PUT segment, epoch end, finish, status; static token; filesystem store with the durable write sequence.
- Playout: `starting`, `live`, `holding`, `finishing`; one encoder; monotonic output clock; holding screen with motion and quiet audio.
- `tools/verify`: reads the recorded output and checks it.

**Acceptance test** (`tests/media-continuity/m1_gaps.sh`, one command): a 20-minute test clip uploaded in real time with three gaps (10 s, 60 s, 5 min). In skip-to-live mode for M1 (catch-up arrives in M4):
- one publish session at the receiving MediaMTX for the whole run (its log);
- output PTS strictly increasing; no video gap over 1.5 frame durations; no audio gap over 25 ms;
- holding media visible within `buffer_target` + 2 s of each gap starting; footage back within `resume_threshold` + 2 s of uploads resuming;
- every frame shown is in source order; none shown twice (frame-counter blocks);
- OBS (or `ffplay`) pulling the stream shows no disconnect.

**Evidence kept:** `tests/results/m1/<date>/` with the verify report (JSON + summary), MediaMTX and playout logs, the first and last 60 s of output media, CPU usage of the playout.

## M2: iOS capture proof (Device)

**Goal.** Prove the iPhone produces correct, independently recoverable segments and keeps them, and measure what iOS actually allows.

**Built.** Native Swift spike app (SwiftUI + AVFoundation), built on Linux with xtool: `AVCaptureSession` → `AVAssetWriter` (`.mpeg4AppleHLS`, 2 s interval) → init + segment files, written to disk **before** anything else touches them; a persistent ordered manifest (SQLite) with epochs and sequences; interruption handling that ends the epoch cleanly (final segment flagged); free-space check and reserve; thermal and Low Power Mode observers that log and (for thermal) step down the preset. No upload. Segments are copied off the phone for checking (Files sharing or `forge device`).

**Acceptance tests.**
- *Segment validity (Linux, on pulled files):* every segment starts with an IDR frame; durations 2.0 s ± 1 frame except a flagged final; `ffprobe` shows H.264 + AAC with the init's codec data; audio and video PTS continuous across boundaries (gap ≤ one AAC frame, A/V drift ≤ 20 ms over 30 min).
- *Reconstruction:* init + all segments of a 30-minute airplane-mode recording concatenate into one file that `ffmpeg -v error -i … -f null -` decodes with no errors, with duration within 0.1% of wall-clock recording time.
- *Condition matrix,* each recorded as observed, never assumed:

| Condition | Expected from Apple's rules | Measured |
|---|---|---|
| Screen locked | Capture stops; epoch ends `interrupted` | M2 |
| App backgrounded | Capture stops; epoch ends `interrupted` | M2 |
| Incoming call / Siri | Session interrupted; resumes after; new epoch | M2 |
| Network disconnected | No effect on capture | M2 |
| Low Power Mode | Capture continues; possible frame-rate/thermal effects | M2 |
| Thermal pressure (`.serious`/`.critical`) | Preset steps down / clean stop | M2 (heat the phone with a long 4K preview, or a warm room; record `thermalState` transitions) |
| Low storage (< reserve) | Clean stop with final segment and warning | M2 (fill storage with a large file) |
| Force-quit during recording | Completed segments survive; partial one discarded or complete; manifest consistent on relaunch | M2 |

**Evidence kept:** `tests/results/m2/<date>/` with ffprobe reports, the reconstruction log, a 2-minute sample of real segments, and `ios-capture-findings.md` filling the "Measured" column.

**Slot:** StreamRelay installs as its own app; Reuben frees a slot for the sitting (decided 2026-11-22).

## M3: Reliable upload

**Goal.** Protocol v1 complete and trustworthy: a `201`/`200` means durably stored, and nothing is lost or duplicated across disconnects and restarts on either side.

**Built.** Server: device pairing and tokens, ownership checks, all response codes including `507`, the durable write sequence (fsync file, rename, fsync directory, commit, then respond), startup orphan cleanup, media validation, rate limits, storage caps. Phone: upload manager reading the M2 manifest (pending → uploading → acknowledged / failed), oldest first, up to 3 in flight, backoff with jitter, network-change retry, background `URLSession` hand-off, reconcile with `GET status` on launch. Upload code never blocks the capture path (separate queue; capture writes only to disk and the manifest).

**Acceptance tests.**
- *Chaos (Linux, fake phone, 2 h):* random 5–120 s outages, 0–10% loss, 300 ms latency, uplink capped at 3 and 6 Mbps, connection resets, server `kill -9` every 20 min, duplicated and reordered uploads, one corrupted upload per 100. Result: every segment stored exactly once with the right checksum; no `409`; every corrupted upload rejected and re-sent; no acknowledgement for a segment missing after a crash (checked by comparing client ack log with server rows and files).
- *Storage full:* server cap reached mid-run → `507`, client holds segments, resumes when space is freed, nothing lost.
- *Late uploads:* segments uploaded after `finish` are archived (archive completeness) and marked `skipped` for live.
- *iPhone:* 20 minutes with airplane mode toggled every 1–3 min, then force-quit with a backlog, relaunch: status shows every segment; capture never paused during uploads (frame-counter check on the recording).
- *Security:* account B's device gets `404` on every endpoint of A's broadcast; revoked token `401`; path traversal and symlink attempts rejected; `tests/sql/cross_account.sql` passes.

**Evidence kept:** chaos report (per-segment ledger), server logs, iPhone run results file.

## M4: Hybrid recovery and end-to-end proof

**Goal.** Catch-up playback and the full scenario from the iPhone.

**Built.** `catching_up` state with frame-drop video and `atempo` audio at a configurable speed (default 1.25×, tested up to 1.5×), speed ramps, `catch_up` / `hold` (optional `max_delay_s`) / `skip_to_live` setting, `finishing` with drain wait, playout checkpoints and restart, heartbeats; phone shows Live / Holding / Catching up and minutes behind live.

**Backlog arithmetic** (shown in the phone and dashboard, and checked in tests). With outage length B, catch-up speed s, recording bitrate r and uplink u:
- upload of the backlog needs at least B × r / (u − r) after reconnecting (u must exceed r);
- playback catch-up needs B / (s − 1);
- the broadcast is near-live again after roughly the larger of the two, plus `buffer_target`.

Example: 5 min outage, 1.25×, 4 Mbps recording, 6 Mbps uplink → upload 10 min, playback 20 min → back near live after ≈ 20 min. Same with a 4.5 Mbps uplink → upload 40 min, so upload is the limit.

**Acceptance tests.**
- *Bench:* M1 test with catch-up mode and a 5-minute gap: tone stays 1 kHz ±2% during catch-up (pitch preserved), beeps every 1/s seconds, every source second appears once and in order, back to `live` within the arithmetic's prediction + 2 min. Same at 1.5×. Listening test notes for speech at 1.25× and 1.5×.
- *Hold mode:* same clip and gaps in `hold` mode with no limit → frozen last frame within 10 s of each gap, then 1× with every source second once and in order, and the final delay equals the total gap time (±2 s). With `max_delay_s` = 120 → speed-up only while the delay is over 120 s, then 1×.
- *Restarts:* playout killed mid-catch-up → one session or one reconnect under 5 s at the receiver, nothing skipped, ≤ 1 s repeated. Client restarted mid-outage → reconciliation, nothing lost.
- *End-to-end proof (iPhone → relay → OBS), semi-automated:* a script on the server records the receiver output and the playout state log while the person follows `tests/e2e/phone-outage.md`:
  1. start a broadcast on the iPhone;
  2. the script confirms the relay is receiving and publishing;
  3. turn on airplane mode (or disconnect Wi-Fi with mobile data off);
  4. keep recording for 5 minutes;
  5. the script confirms holding media is being published;
  6. reconnect;
  7. the script confirms the missing segments arrive (status shows no holes);
  8. the script confirms catch-up at the configured speed;
  9. the script confirms one receiver session throughout;
  10. the script confirms `live` is reached, then the person stops.
  `tools/verify` then checks the recording as in M1/M4 bench, against the phone's own manifest (every recorded segment broadcast once, in order).

**Evidence kept:** `tests/results/m4/<date>/` with the state log, verify report, receiver recording excerpts, the phone's manifest export and the person's step notes. The run can be repeated with one command plus the phone steps.

## M5: Destination fanout

**Built.** Destination CRUD with encrypted keys and the rtmps rule, one supervised forwarder per destination with reconnect and health, per-destination status in the API.

**Acceptance tests.** Two local receivers, one blocked with `iptables` for 2 min: the other never drops; the blocked one reconnects itself. Real Twitch and YouTube test channels, 45-minute broadcast with a 5-minute outage and one forced playout restart: both stay live without a viewer refresh; record platform health warnings and VOD continuity. Stream keys absent from all logs and API responses. Platform terms checked first (risk R9).

## M6: Product-quality iPhone client

**Built.** Broadcast setup (title, destinations chosen on the server, quality, catch-up mode), preview with camera switch and zoom, audio meter, connection / upload / backlog / storage / battery / thermal indicators, clear outage and recovery states, pocket mode (dimmed screen), Bluetooth microphones, accessibility labels.

**Acceptance test.** A person new to the app runs a broadcast through a real dead zone and ends it without help, and can explain what "behind live" meant.

## M7: Dashboard and self-hosted release

**Built.** Dashboard (broadcasts, destinations, devices and pairing, live monitor with preview, history and events, archive download, storage), Compose production profile with TLS, S3-compatible store option, install / config / API / operations docs, Prometheus metrics and a Grafana dashboard, backups, security review.

**Acceptance tests.** Someone else installs it on a fresh VPS from the docs and broadcasts from a phone; port scan shows only HTTPS and RTMP(S) ingest; load test with `SR_MAX_ACTIVE_BROADCASTS` + 1 broadcasts gets the right `429`.
