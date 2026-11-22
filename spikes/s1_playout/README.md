# Spike S1: continuous playout

Throwaway code that tests one assumption: one process can keep one RTMP publish alive on one clock while its source switches between fMP4 segments, a holding card and sped-up footage. Results: [RESULTS.md](RESULTS.md). Plan: [docs/plan/spike-s1-playout.md](../../docs/plan/spike-s1-playout.md).

| File | What |
|---|---|
| `make_clip.py` | Test clip with barcode frame numbers, 1 kHz tone, speech |
| `make_segments.sh` | Cuts it into init.mp4 + 2 s fMP4 segments |
| `feeder.py` | Delivers segments like a phone on a bad network |
| `playout.py` | The playout under test |
| `run.sh` | MediaMTX + feeder + playout + an independent recorder |
| `verify.py` | Checks the recording against the pass gate |
| `mediamtx.yml` | Minimal MediaMTX config (RTMP only) |

Needs Python 3.11+, `pip install av numpy pillow`, FFmpeg, espeak-ng and a MediaMTX binary (`MEDIAMTX=/path/to/mediamtx`). What survives goes into `services/playout` in M1, with tests.
