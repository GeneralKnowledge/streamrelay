# M3 results, 2026-11-22 (Linux part)

Two runs, both passed. The iPhone run waits for the phone sitting ([plan](../../../../docs/plan/phone-sitting-m2-m3.md)), so M3 is not complete yet.

## 1. Chaos run: 2 hours, fake phone (`tests/chaos/m3_chaos.sh`, `chaos/`)

The fake phone uploaded 120 minutes of real segments (3,600 × 2 s) in real time through `tools/chaos/proxy.py`. While it ran:

- 28 outages of 7 to 118 s, 1,542 s in total. Every open connection was cut and new ones were reset.
- 67 random connection resets, plus 6 resets partway through a request. These stand in for loss, which was drawn between 0 and 10 % for each outage cycle.
- 300 ms latency each way, and the uplink capped at 3 Mbps for the first 30 minutes and 6 Mbps after.
- The API was killed with `kill -9` every 20 minutes, 6 times, and restarted.
- On the phone side, 5 % of segments were reordered, 5 % were uploaded a second time, and 1 % were corrupted.

| Check | Result |
|---|---|
| Segments stored exactly once, right checksum, file on disk | **3600 / 3600** |
| Acknowledged by the phone but missing on the server | 0 |
| `409` answers | 0 |
| Corrupted uploads | 24: 23 answered `422`; 1 lost to a connection reset (status 0); all 24 sent again and stored |
| Duplicates answered `200` | 215 (179 deliberate, the rest were lost acknowledgements) |
| Requests with no answer (status 0) | 745 |
| Startup scans after each kill | 5 removed temp files in total, 0 orphans, every committed file kept |
| Finish | 200, 122 min after the start (the backlog drained 2 minutes after recording ended) |
| Verdict (`tools/chaos/ledger.py`) | **pass**, 0 problems |

## 2. The iPhone app's upload core against the real server (`tests/ios/core_vs_server.sh 20`, `ios-core/`)

`sr-upload` runs the same Swift code the app uses for the manifest, the protocol client and the upload queue. It was built on Linux and uploaded 20 minutes of segments at recording pace, starting a new epoch every 60 segments as interruptions would. It went through the same proxy, at a harsher setting: 19 outages (1,354 s in total), resets every 30 s on average, and the API killed with `kill -9` once halfway.

| Check | Result |
|---|---|
| Segments in the database with the source file's checksum, file on disk, `start_us` continuous | **600 / 600** |
| Epoch ends (10 epochs) and finish | all present, `last_sequence` right |
| Duplicates answered `200` (lost acknowledgements) | 29 |
| Manifest folder (`tools/ios_check/check_recording.py`) | 0 problems: every epoch decodes, 120.0 s declared = 120.0 s decoded |
| Verdict | **pass**, 0 problems; the queue ended `finished` with 0 pending and 0 damaged |

A first attempt at this run found a bug, fixed before the passing run. Each new segment reset the upload backoff, so while the network was down the queue retried about every second instead of backing off to 30 s. Now only a network change resets it, as protocol v1 says. `testNewSegmentsDoNotCutABackoffShort` fails on the old code and passes on the fix.

## Also covered by unit tests (CI)

These are in `tests/unit/test_m3.py`:

- Storage cap reached: `507`, and the upload resumes once space is freed.
- Late uploads after `finish`: stored and marked `skipped`.
- Another account's device gets `404` on every endpoint.
- A revoked token gets `401`.
- Path traversal and symlink attempts are rejected.
- Media that is truncated, random, fuzzed, of the wrong duration or the wrong codec gets `422`.

`tests/sql/cross_account.sql` passes.
