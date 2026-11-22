# StreamRelay for iPhone

Native Swift (SwiftUI + AVFoundation), built and installed from Linux with xtool. Bundle id `org.forgemobile.streamrelay`, always reused.

| Target | What | Builds on |
|---|---|---|
| `StreamRelayCore` | Manifest (SQLite, WAL, `synchronous=FULL`), protocol v1 client, upload queue, backoff, SHA-256 | iOS and Linux |
| `StreamRelayCapture` | `SegmentRecorder` (camera + mic → `AVAssetWriter` 2 s fMP4 segments → manifest), `DeviceConditions` (interruptions, thermal, Low Power Mode, storage reserve), `BackgroundHandOff` (background `URLSession`) | iOS |
| `StreamRelayApp` | SwiftUI: pair, record, settings | iOS |
| `sr-upload` | The upload core from the command line, to test it against the real server | Linux, macOS |

## The rules the code keeps

- Capture never waits on the network. A segment's bytes are written and fsynced, then its manifest row is committed, then the queue may upload it.
- One capture run is one epoch with its own init (`init_id` = epoch). An interruption, a camera failure, a quality step-down or Stop ends it; the last (possibly short) segment carries the `final` flag, and the queue holds a short segment back until its epoch is closed.
- The queue keeps all its state in the manifest, so it can be killed anywhere. On launch it closes open epochs (`app_restart`), deletes files without rows, marks rows without files damaged, and reconciles with `GET status` before uploading.
- Order: `catch_up` and `hold` oldest first; `skip_to_live` the live edge first, then backfill oldest first at 1 segment per second.

## Checking it on Linux

```bash
cd ios
swift test                       # 15 tests: manifest, crash recovery, reconcile, queue against a fake server
```

Without a local Swift 6 toolchain, the official image works (add `libsqlite3-dev`):

```bash
docker run --rm -v "$PWD":/src -w /src swift:6.0-noble bash -c 'apt-get update -qq && apt-get install -yqq libsqlite3-dev && swift test'
```

Against the real server through the bad-network proxy, with an API `kill -9` halfway:

```bash
tests/ios/core_vs_server.sh data/ios-core-run 20     # minutes of segments, real time
```

## Building for the iPhone

From this folder on the laptop, through the build queue: `forge queue run --note streamrelay -- xtool dev build`, then install with `xtool dev run` when the phone is plugged in. `StreamRelayCapture` and `StreamRelayApp` compile only with the iOS SDK, so the laptop build is their first compile.

Pulled recordings (Files → On My iPhone → StreamRelay → Recordings → one folder per recording) are checked with `tools/ios_check/check_recording.py <folder>`.
