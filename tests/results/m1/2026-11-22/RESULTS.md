# M1 acceptance run, 2026-11-22

**Verdict: pass on every gate.** `tests/media-continuity/m1_gaps.sh`: a 20-minute clip uploaded in real time by the fake phone over protocol v1, with upload outages of 10 s, 60 s and 5 minutes, skip-to-live. Every gate below is measured on an independent recording (`ffmpeg -c copy` reading what MediaMTX served), not on the playout's own log.

Machine: the cloud container (4 vCPU Xeon @ 2.30 GHz, no GPU, no Docker), PostgreSQL 16.15, MediaMTX v1.9.3 binary, Python 3.13, PyAV 19.0.2. Services ran directly, not under Compose (Compose is checked in CI).

## Gates ([milestones M1](../../../../docs/plan/milestones.md#m1-media-pipeline-proof-linux-no-phone))

| Check | Gate | Measured | Result |
|---|---|---|---|
| Publish sessions at the receiver for the whole run | 1 | 1 | pass |
| Viewer sessions (no reconnect) | 1 | 1 | pass |
| Viewer disconnected only after the broadcast ended | yes | yes | pass |
| Viewer warnings | 0 | 0 | pass |
| Video PTS strictly increasing | 0 | 0 non-increasing of 36,451 frames | pass |
| Video gaps over 1.5 frames | 0 | 0 (max 34 ms) | pass |
| Audio gaps over 25 ms | 0 | 0 (max 1 ms) | pass |
| Footage frames in source order, none shown twice | 0, 0 | 25,020 footage frames: 0 repeated, 0 out of order | pass |
| Holding visible after uploads stopped | ≤ buffer_target + 2 s = 10 s | 6.2 s, 6.3 s, 6.3 s | pass |
| Footage back after uploads resumed | ≤ resume_threshold + 2 s = 12 s | 2.3 s, 6.3 s, 6.2 s | pass |

How the two timing gates are measured: recorded frame k was sent at `t0 + (k + offset)/30` on the playout's output clock; the offset is anchored on the first footage frame and cross-checked against all 8 switches between footage and cards the playout logged (0 frames off). Outage times come from the fake phone's log.

## What else the run showed

- **Protocol faults were absorbed.** The phone sent 654 PUTs: 600 `201`, 33 `200` (duplicates on purpose), 21 `422` (corrupted bodies on purpose, each retried once and then stored). All 600 segments acknowledged and stored.
- **Skip-to-live did what it says.** 417 segments played, 183 marked `skipped` (the outage backlog, uploaded later and kept for the archive). Footage seconds 0–1199 all ordered; the 5-minute outage jumped 150 segments.
- **Holding appears about 6 s after uploads stop, not 8.** The last segment before an outage was captured up to 2 s before it was uploaded, so the stored buffer is about buffer_target − 2 s when uploads stop.
- **Footage returned faster after the 10 s outage (2.3 s)** because the phone backfilled the 5 missing segments within seconds, making the run of contiguous footage long enough at once; after the long outages it waits for resume_threshold (10 s) of new footage, about 6 s after the first live segment arrives (live segments arrive 2 s apart, the first one already counts).
- **CPU:** mean 23% of one core (footage 24%, holding 20%, the cached card is now cheaper than footage; S1 drew it every frame at 41%). Worst second 52%. 7 ticks over one frame late, worst 125 ms (at encoder start and at jumps); timestamps come from the frame counter, so lateness never makes a gap.

## Playout restart (checkpoints), `restart/`

A separate 60 s run where the playout process was killed with SIGKILL 31 s in. The supervisor restarted it after 1 s; it resumed from its last checkpoint (segment 0/11, frame 29, output frame 900), continued the output clock and finished normally. All 30 segments were played, none skipped. As designed (playout.md, crash recovery), the restart is a second publish to MediaMTX, which ended the viewer's session: in production the forwarders must bridge that gap (risk R7, measured in M4/M5), so the viewer-side gates are expected to fail for this run and do (`restart/verify.md`). Repeated footage after the restart could not be measured with this viewer because it disconnected; M4's bench adds a reconnecting viewer.

## Files

Here: `verify.md`, `verify.json`, MediaMTX, API and playout logs, the phone's upload log, state events and play results from the database.

Media (too large for the repo), in the project files at `streamrelay/m1-run/`: the first and last 60 s of the recording, and 40 s around each outage.
