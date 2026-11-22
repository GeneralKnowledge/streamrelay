# Playout engine

The part that decides what viewers see, and the hardest part of StreamRelay. Spike S1 and M1 prove it on Linux before any phone work.

## Shape

```
segments on disk ──► Scheduler ──► Decoder ──► Speed stage ──► Encoder ──► RTMP to MediaMTX ──► forwarders ──► Twitch / YouTube / custom
                       ▲              ▲                         (one, constant settings,
 holding screen ───────┘              │                          monotonic timestamps)
 (generated frames + quiet audio) ────┘
```

One process per active broadcast, supervised by the server. It runs on its own clock: one output frame every 1/30 s, whatever happens upstream.

## States

The spec's state machine, made concrete for the playout (the phone has its own simpler connection state):

| State | Viewers see | Enter when | Leave when |
|---|---|---|---|
| `starting` | Holding screen ("Starting soon") | Broadcast created | First `buffer_target` of footage received → `live` |
| `live` | Footage at 1×, `playout_delay` behind capture | Buffer healthy | Next segment not on disk at its deadline → `holding` |
| `holding` | Holding screen with "Reconnecting… back shortly" and elapsed time; in `hold` mode the last frame, frozen, with a small "Reconnecting…" badge | Underrun | Enough contiguous footage after the play point (`resume_threshold`, default 10 s) → `catching_up` (`live` in skip-to-live, and in `hold` while the delay is within its limit) |
| `catching_up` | Backlog at `speed` (1.25× default, 1.5× max) | Resumed with backlog > `buffer_target`; in `hold` mode only when the delay exceeds `max_delay_s` (never if it is unset) | Behind-live ≤ `buffer_target` (in `hold`: ≤ `max_delay_s`) → `live`; underrun → `holding` |
| `finishing` | Remaining footage (at the catch-up speed if behind), then an end card for 10 s | Client called `finish`, from any state; start and resume thresholds no longer apply | All stored footage the mode plays has played and the backlog is complete (protocol: [finish](../protocol/upload-v1.md#end-an-epoch-or-the-broadcast)); empty epochs need only their end call, so a broadcast with no footage ends at once. If the **drain wait** expires first (both modes), missing segments are marked `skipped` and the stored footage still drains. A stored segment that fails to decode (any segment, including the final one) is marked `damaged`, counts as played for this rule, and the playout moves on to the next one; it never waits on it. Then end card; outputs closed → `finished` |

Hysteresis (a resume threshold larger than one segment) stops flapping between holding and footage on a connection that delivers in bursts.

**Skip to live:** on leaving `holding`, the scheduler jumps to the newest contiguous footage minus `buffer_target`. The skipped footage is still stored and archived; it is just not broadcast.

**Hold and continue:** on leaving `holding`, playout continues at 1× from the frame it froze on, so the broadcast simply runs later than capture by the total outage time. With `max_delay_s` unset that delay is never reduced and the broadcast ends that much after the phone stops; with a limit, playout speeds up only while the delay is above the limit. The phone and dashboard show the current delay. The holding screen in this mode is the frozen last frame with a badge, not the full card.

**Gaps between epochs** (real lost footage, e.g. a phone call): never shown as holding forever. The scheduler plays up to the end of the epoch, shows a 2 s "Camera interrupted" card, then continues with the next epoch.

## Timing

- `buffer_target` (Live delay) default 8 s: four segments. Absorbs normal upload jitter.
- The output clock is monotonic and never pauses or jumps. Source timestamps are only used to order and pace source frames; output PTS = output frame count / 30.
- Audio: output AAC is generated from a continuous PCM stream; the holding screen sends low-level comfort noise rather than digital silence (some platforms flag silent streams; to verify in M5).
- Catch-up arithmetic: with backlog B and speed s, catch-up takes B / (s − 1) of real time **provided uploads keep up** (risk R4; full arithmetic in [milestones M4](../plan/milestones.md#m4-hybrid-recovery-and-end-to-end-proof)). 5 minutes at 1.25× = 20 minutes; at 1.5× = 10 minutes.

## Speed stage

- Video: consume 30·s source frames per second, emit 30: drop evenly (1.25× drops 1 in 5; 1.5× drops 1 in 3). No frame interpolation.
- Audio: FFmpeg `atempo` filter (pitch preserved). Candidate upgrade: `rubberband` (better at 1.5×, GPL). Speed changes ramp over 2 s to avoid audible jumps.

## Encoder

x264 `veryfast`, CBR (4 Mbps at 720p30 default; destinations get what they accept, see ADR 0006), 2 s GOP with no scene-cut keyframes, `profile high`, AAC-LC 128 kbps 48 kHz. Settings fixed for the life of the broadcast.

Holding screen: a slowly moving element (clock, progress ring) so the encoder does not drop to a tiny bitrate; YouTube warns about very low bitrates.

## Crash recovery

The playout checkpoints `(state, epoch, sequence, frame offset)` to Postgres every second. On restart it reconnects to MediaMTX and resumes from the checkpoint; the RTMP session to platforms is held by the forwarders, which see a short input gap rather than a reconnect. Whether the forwarders bridge that gap cleanly is measured on the bench in M4 and against real Twitch and YouTube in M5 (risk R7).

**Guarantee after a crash: at most about one second repeated, nothing skipped.** Frames already sent to MediaMTX cannot be part of a Postgres transaction, so a crash between sending and the next checkpoint replays the frames since that checkpoint (up to the checkpoint interval plus whatever the encoder had buffered). Without a crash, footage is never repeated. We accept this rather than build a delivery protocol to the platforms, which RTMP does not support. `segments.played_at` records that a segment was handed to the encoder, not that a viewer saw it.

## Implementation plan

M1 builds this in Python with PyAV. Decision gate in spike S1 ([spec](../plan/spike-s1-playout.md)): if a 720p30 decode + speed + encode can't hold real time with 30% headroom on a 4-vCPU VPS-class machine, switch the engine to GStreamer (`input-selector`, `scaletempo`) and keep the scheduler in Python.
