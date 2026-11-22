# Risks and unproven assumptions

Ordered by how badly each would damage the architecture if wrong. Each names the test that settles it.

| # | Assumption or risk | If wrong | Settled by |
|---|---|---|---|
| R1 | **One playout process holds one RTMP session through source switches, on one clock, in real time** | The core promise fails; engine choice changes | Spike S1, then M1 |
| R2 | **AVAssetWriter segments are IDR-aligned, ~2 s, with continuous audio, and join cleanly** | Need our own segmenter from `CMSampleBuffer`s with VideoToolbox (more work, same design) | M2 segment validity and reconstruction tests |
| R3 | **iPhone stops the camera when locked or backgrounded** (documented) | Known limit: recording only while the app is on screen. The product must say so plainly; pocket mode keeps the screen on but dimmed | M2 condition matrix records the real behaviour, including calls, Low Power Mode, thermal and storage |
| R4 | **Upload headroom, not playback speed, often limits catch-up** | At 4 Mbps recording on a 4.5 Mbps uplink, 5 minutes of backlog takes 40 minutes to upload | M4 arithmetic shown to the user; M3/M4 netem caps; lower cellular default bitrate |
| R5 | **`atempo` speech at 1.25–1.5× is intelligible** | Cap at 1.25× or use `rubberband` (GPL) | S1 listening note, M4 |
| R6 | **"2xx means durably stored" holds through crashes** | Phone deletes footage the server lost | M3 chaos test comparing client ack log with server files and rows after `kill -9` |
| R7 | **Twitch/YouTube tolerate long holding periods and forwarder reconnects** | Viewers see "stream ended" or the VOD splits | M5 real-platform runs with a forced playout restart |
| R8 | **Battery and heat over hours** | Long field broadcasts fail | M2 thermal observations; M6 field use |
| R9 | **Platform terms** for delayed/replayed footage and holding screens | Account action | Read before M5 |
| R10 | **Untrusted media crashes or exhausts the server** | Denial of service | M3 validation tests with fuzzed and truncated segments |
| R11 | **iPhone slots** (free team: 3, all used) | No way to run the M2 spike | Decided 2026-11-22: Reuben frees a slot per sitting; one bundle id |
| R12 | **Focus**: a new product competing with ixOS, Relay and VPN | Slows all of them | One milestone at a time, only when Reuben asks |
| R13 | **Name** "StreamRelay" likely taken | Rename before release | Check before M7 |

## Guarantees we will and won't make

- **Will:** recording never waits on the network; acknowledged segments are durable; every acknowledged segment reaches the archive; in catch-up mode every segment acknowledged in time is broadcast once in order.
- **Won't:** exactly-once RTMP delivery across a playout crash (≤ about 1 s may repeat); recording while iOS has taken the camera away (lock, background, call); live completeness for footage that arrives after the drain wait (it goes to the archive).
