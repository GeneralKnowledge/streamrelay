# Spike S1: continuous playout

The first implementation task. Smallest piece of code that tests the assumption most likely to sink the architecture: **one process can keep a single RTMP publish alive, on one monotonic clock, while its source switches between decoded fMP4 segments, generated holding media and sped-up footage, in real time on modest hardware.**

Runs entirely on Linux. No API, no database, no phone.

## Scope

A single Python script (`spikes/s1_playout/playout.py`, PyAV) plus a shell harness:

1. `make_segments.sh`: FFmpeg makes a 10-minute test clip (frame counter, timecode, 1 kHz tone, 1 s beeps) and cuts it into fMP4 init + 2 s segments with the HLS muxer, so segment structure matches what AVAssetWriter produces.
2. `feeder.py`: copies segments into an input folder in real time, following a schedule: normal for 2 min, nothing for 3 min, then the 90 missed segments at once, then real time again.
3. `playout.py`: watches the folder; one output at 720p30 x264 CBR 2 s GOP + AAC 48 kHz to `rtmp://localhost:1935/s1` (MediaMTX in Docker); states `live` → `holding` (generated moving clock + comfort noise) → `catching_up` at 1.25× (frame drop + `atempo`) → `live`. Speed from a command-line flag so 1.5× can be tried.
4. `verify.py`: a second process records the stream from MediaMTX with FFmpeg and checks the recording.

Roughly 300–500 lines in total. Throwaway code: what survives goes into `services/playout` in M1 with tests.

## Pass gate (all must hold)

| Check | Pass |
|---|---|
| RTMP sessions at MediaMTX for the whole run | exactly 1 |
| Output video PTS | strictly increasing, no gap > 1.5 frames |
| Output audio | no gap > 25 ms, no discontinuity warnings from `ffmpeg -v warning` |
| Holding media | appears within 10 s of the feed stopping |
| Every source second (frame counter) | appears once, in order |
| Tone during catch-up | 1 kHz ±2% (pitch preserved) |
| Return to live | within B / (s − 1) + 60 s of the backlog arriving (12 + 1 min at 1.25×) |
| CPU | playout ≤ 70% of one core on a 4-vCPU machine at 720p30 |
| Listening | short note on speech intelligibility at 1.25× and 1.5× (a speech sample is mixed into the test clip for 30 s) |

## If it fails

- Real time not held: try x264 `superfast`/`ultrafast`, then a GStreamer pipeline (`input-selector`, `scaletempo`) with the scheduler kept in Python (ADR 0005's fallback).
- Discontinuities at switches: switch only at output GOP boundaries; keep audio resampler state across sources.
- `atempo` poor at 1.5×: try `rubberband`; else cap catch-up at 1.25×.

## Output

PR with the spike code, the harness, and `spikes/s1_playout/RESULTS.md` (each gate row with the measured value, machine used, logs and a 60 s sample of output around each switch). The spike runs in the cloud session (no laptop or phone needed).
