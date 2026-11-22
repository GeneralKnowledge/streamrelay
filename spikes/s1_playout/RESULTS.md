# Spike S1 results: continuous playout

**Verdict: pass on every measured gate, at both 1.25× and 1.5×.** One process held a single RTMP publish for the whole run. That run went live, lost its feed for 3 minutes, showed a holding card, received the 3-minute backlog at once, caught up at speed and returned to live, all on one clock with no timestamp gaps. Each gate below was measured on an independent recording of what MediaMTX served. One item is not measured: the listening note needs a person (see the last row).

Plan: [docs/plan/spike-s1-playout.md](../../docs/plan/spike-s1-playout.md). Run 2026-11-22 in the cloud session.

## Setup

| | |
|---|---|
| Machine | Cloud container, 4 vCPU Intel Xeon @ 2.30 GHz, no GPU |
| Software | Python 3.13, PyAV 19.0.2 (bundled FFmpeg libraries, libavcodec 63.1, libx264), system FFmpeg 6.1.1 for the viewer, MediaMTX v1.9.3 (binary; Docker wasn't available here) |
| Source | 20-minute 720p30 H.264 + AAC clip, cut into fMP4 init + 2 s segments with FFmpeg's HLS muxer (AVAssetWriter's shape). B-frames on, closed 2 s GOPs. Every frame carries its number as a 24-bit barcode and as text; audio is a continuous 1 kHz tone, with 30 s of synthetic speech (espeak-ng) at source 200–230 s |
| Network script | Real time for 2 min; nothing for 3 min (source 120–300 s); then the 90 missed segments at once; then real time again |
| Output | 720p30 x264 `veryfast`/`zerolatency`, CBR 3 Mbps, 2 s GOP, AAC 128 kbps 48 kHz, to `rtmp://127.0.0.1:1935/s1` |
| Thresholds | Start/resume at 4 s of footage buffered; catch up above 8 s of backlog, back to live at 4 s |
| Viewer | `ffmpeg -c copy` reading from MediaMTX into `rec.flv`; `verify.py` decodes it |

Reproduce: `python3 make_clip.py clip.mp4 && ./make_segments.sh clip.mp4 segs && MEDIAMTX=mediamtx ./run.sh segs work 1.25 1200 && python3 verify.py work --speed 1.25`.

## Pass gate

| Check | Pass if | 1.25× (20 min run) | 1.5× (13 min run) |
|---|---|---|---|
| RTMP sessions at MediaMTX for the whole run | exactly 1 | **1** | **1** |
| Output video PTS | strictly increasing, no gap > 1.5 frames | **0** non-increasing, max gap 34 ms, 0 gaps > 50 ms (36,240 frames) | **0**, max 34 ms, 0 gaps (24,239 frames) |
| Output audio | no gap > 25 ms, no viewer warnings | max packet gap 1 ms, longest digital silence 0.1 ms, **0** viewer warnings | same: 1 ms, 0.1 ms, **0** |
| Holding media | within 10 s of the feed stopping | **4.2 s** (the 4 s buffer running out) | **4.2 s** |
| Every source second appears once, in order | all seconds, no repeats, no reordering | seconds 0–1199 all present, **0** order violations, largest frame step 2 (frame drop at speed) | seconds 0–799 all present, **0** violations, largest step 2 |
| Tone during catch-up | 1 kHz ± 2% | **1000.0 Hz** in all 665 one-second windows (speech window excluded) | **1000.0 Hz** in all 323 windows |
| Return to live | within B/(s−1) + 60 s of the backlog arriving | **698 s** (bound 780 s) | **350 s** (bound 420 s) |
| CPU | ≤ 70% of one core | mean **27.5%**; by state: live 24%, catch-up 25%, holding 41%; worst second 60% | mean **30.1%**; live 25%, catch-up 28%, holding 42%; worst second 57% |
| Listening | speech still intelligible | **Not judged: needs a person.** Samples: `speech_1.25x.wav`, `speech_1.5x.wav` (below) | |

The viewer measured a catch-up speed of 1.241× and 1.482×. That is the frame advance per output second averaged over the catch-up, slightly under nominal because the first and last seconds are part live.

State timeline (seconds from the first output frame, 1.25×): starting −1.9 → live 4.2 → holding 124.2 (source 120) → catching_up 300.2 (backlog 180 s) → live 997.8 (source 992) → holding 1205.8 (clip ended). At 1.5× catch-up ended at 649.5.

## What else the run showed

- **Real time held.** Over 20 minutes, 8 ticks were sent more than one frame late, the worst by 118 ms: 5 at encoder start-up and 3 during catch-up. Timestamps come from the output frame counter, so a late tick delays delivery slightly but never makes a gap or reorders anything.
- **The holding card is the most expensive state** (about 41% of a core) because it draws text with PIL every frame. Caching the card and only drawing the clock once a second would bring it under live. This doesn't matter for a spike, but it's worth doing in M1.
- **A/V offset is not yet measured precisely.** Playout's own estimate stays within ±30 ms at 1× and ±67 ms during catch-up. The catch-up figure includes audio sitting inside `atempo`, so the real offset is likely smaller. M1 should add a flash + beep marker to the clip and measure the offset from the recording.
- **Persistent decoders matter.** Audio uses one AAC decoder across segments, which avoids a click every 2 s. Video is drained and flushed at each segment end, which is safe because every segment starts with an IDR. The flush is needed because the source has B-frames.
- **No GStreamer fallback needed.** PyAV + libx264 `veryfast` had plenty of headroom at 720p30 on this machine.

## Samples for listening and watching

In the project files, not the repo because of size: `/mnt/project-files/streamrelay/s1-samples/run125/` and `/run150/`:
- `speech_1.25x.wav`, `speech_1.5x.wav`: the spoken passage as viewers hear it during catch-up. **Please listen to these and say whether 1.5× is acceptable.**
- `switchNN_<state>_<t>s.flv`: 60 s of the output around each state change (30 s either side), straight from the recording.

`results/holding.png` and `results/catch-up-start.png` are frames from the holding → catch-up switch. `results/run125/` and `results/run150/` hold the verify output and all event logs.

## Not covered by S1 (by design)

- Real Twitch or YouTube ingest: M5 (and the M4 end-to-end proof goes iPhone → relay → OBS).
- Playout crash and restart: M1 (checkpoints).
- Init-segment changes mid-broadcast (new epoch, new encoder settings): M1.
- Real iPhone segments: M2. Ours come from FFmpeg in the same fMP4 shape.
