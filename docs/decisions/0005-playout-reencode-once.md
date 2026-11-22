# 0005: One playout process per broadcast, re-encoding once

## Context
Viewers must never need to refresh. Twitch and YouTube see one RTMP publish; anything that resets timestamps, changes SPS/PPS or reconnects risks a visible break, a "stream ended", or a new VOD. We also need a holding screen (generated) and catch-up at 1.25–1.5× with pitch-preserved audio (requires decoding and re-encoding).

## Decision
A long-running **playout** process per broadcast owns **one encoder** and **one output timeline**:

1. Scheduler picks the source for each output tick: recovered segments (1×), recovered segments (catch-up speed), or the holding screen.
2. Decode the chosen source to raw frames and PCM.
3. Video at speed s: keep output at 30 fps and drop source frames evenly (1.25× drops every 5th frame). Audio at speed s: `atempo` (WSOLA, pitch-preserving). `rubberband` is the higher-quality alternative if `atempo` sounds poor at 1.5× (GPL; acceptable for a self-hosted server, decide in M1).
4. Encode once (x264, CBR, 2 s GOP, constant settings), stamp with a monotonic output clock, publish to MediaMTX locally.

Implementation: Python with **PyAV** (FFmpeg's libraries with frame-level control) in M1. If PyAV can't hold real time, fall back to a **GStreamer** pipeline (`input-selector`, `scaletempo`), which is built for live source switching. The FFmpeg CLI alone is not suitable: it cannot switch inputs mid-run without restarting the output.

## Consequences
- Output to platforms never changes codec parameters and timestamps never jump: platform discontinuity tolerance mostly stops mattering (risk R7 then only covers playout crashes).
- One 720p30 x264 `veryfast` encode per broadcast: about one CPU core. Fine for one VPS and a handful of broadcasts.
- A passthrough mode (copy segments in Live state) is a later optimisation, not v1.

## Revisit if
M1 cannot sustain real time on target hardware, or audio quality at 1.5× fails the listening test.
