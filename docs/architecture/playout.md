# Playout engine

The part that decides what viewers see, and the hardest part of StreamRelay. M1 step A proves it on Linux before the phone run.

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
| `holding` | Holding screen with "Reconnecting… back shortly" and elapsed time | Underrun | Enough contiguous footage after the play point (`resume_threshold`, default 10 s) → `catching_up` (or `live` in skip-to-live) |
| `catching_up` | Backlog at `speed` (1.25× default, 1.5× max) | Resumed with backlog > `buffer_target` | Behind-live ≤ `buffer_target` → `live`; underrun → `holding` |
| `finishing` | Remaining footage, then an end card for 10 s | Client called `finish` and all received footage played | Outputs closed → `finished` |

Hysteresis (a resume threshold larger than one segment) stops flapping between holding and footage on a connection that delivers in bursts.

**Skip to live:** on leaving `holding`, the scheduler jumps to the newest contiguous footage minus `buffer_target`. The skipped footage is still stored and archived; it is just not broadcast.

**Gaps between epochs** (real lost footage, e.g. a phone call): never shown as holding forever. The scheduler plays up to the end of the epoch, shows a 2 s "Camera interrupted" card, then continues with the next epoch.

## Timing

- `buffer_target` (Live delay) default 8 s: four segments. Absorbs normal upload jitter.
- The output clock is monotonic and never pauses or jumps. Source timestamps are only used to order and pace source frames; output PTS = output frame count / 30.
- Audio: output AAC is generated from a continuous PCM stream; the holding screen sends low-level comfort noise rather than digital silence (some platforms flag silent streams; to verify in M5).
- Catch-up arithmetic: with backlog B and speed s, catch-up takes B / (s − 1) of real time **provided uploads keep up** (see risk R4). 5 minutes at 1.25× = 20 minutes; at 1.5× = 10 minutes.

## Speed stage

- Video: consume 30·s source frames per second, emit 30: drop evenly (1.25× drops 1 in 5; 1.5× drops 1 in 3). No frame interpolation.
- Audio: FFmpeg `atempo` filter (pitch preserved). Candidate upgrade: `rubberband` (better at 1.5×, GPL). Speed changes ramp over 2 s to avoid audible jumps.

## Encoder

x264 `veryfast`, CBR (4 Mbps at 720p30 default; destinations get what they accept, see ADR 0006), 2 s GOP with no scene-cut keyframes, `profile high`, AAC-LC 128 kbps 48 kHz. Settings fixed for the life of the broadcast.

Holding screen: a slowly moving element (clock, progress ring) so the encoder does not drop to a tiny bitrate; YouTube warns about very low bitrates.

## Crash recovery

The playout checkpoints `(state, epoch, sequence, frame offset)` to Postgres every second. On restart it reconnects to MediaMTX and resumes from the checkpoint; the RTMP session to platforms is held by the forwarders, which see a short input gap rather than a reconnect. Whether the forwarders bridge that gap cleanly is a measured item in M1 (risk R3).

## Implementation plan

M1 builds this in Python with PyAV. Decision gate after M1 step A: if a 720p30 decode + speed + encode can't hold real time with 30% headroom on a 4-vCPU VPS-class machine, switch the engine to GStreamer (`input-selector`, `scaletempo`) and keep the scheduler in Python.
