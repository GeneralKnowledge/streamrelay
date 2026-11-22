# StreamRelay

Resilient mobile livestreaming: a network outage becomes a delay in the broadcast, not the end of it. Working name.

Status: M1 (media pipeline proof, Linux) built. Start with the [implementation plan](docs/README.md).

| Component | What |
|---|---|
| StreamRelay Mobile | iPhone app (Android later): records 2 s segments locally whatever the network does, uploads when it can |
| StreamRelay Server | Receives segments, plays one continuous stream out with a holding screen and catch-up, fans out to Twitch, YouTube and custom RTMP |
| StreamRelay Control | Web dashboard |

## Layout

| Path | What |
|---|---|
| `services/streamrelay/` | Python package: upload API (`api/`), segment store (`store.py`), playout (`playout/`), migrations |
| `tools/testclip/` | Test source: frame-number barcode, broadcast time, 1 kHz tone with a beep every second; cut into fMP4 init + 2 s segments |
| `tools/fake_phone/` | Uploads segments in real time over protocol v1, with outages, reordering, duplicates and corruption |
| `tools/verify/` | Checks an independent recording of the output against the continuity gates |
| `tests/unit/` | API, store and verify tests (need PostgreSQL: `SR_TEST_DATABASE_URL`) |
| `tests/media-continuity/` | `m1_gaps.sh` (the M1 acceptance test, 22 min), `smoke.sh` (2 min, CI) |
| `spikes/s1_playout/` | Spike S1, kept as it ran |

## Running it

With Docker:

```bash
SR_STATIC_TOKEN=$(openssl rand -hex 32) docker compose up -d --build --wait
```

Without Docker (what the M1 run used): PostgreSQL 16, FFmpeg, and the MediaMTX binary on `PATH`, then

```bash
pip install -e ".[dev]"
SR_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres pytest
tests/media-continuity/smoke.sh          # 2 minutes
tests/media-continuity/m1_gaps.sh        # the M1 acceptance test, about 22 minutes
```

The harness starts its own throwaway PostgreSQL cluster unless `SR_DATABASE_URL` is set, then MediaMTX, the API, the playout supervisor, the fake phone and an independent viewer, and runs `tools/verify` on the recording.
