# 0006: MediaMTX as the local hub; one forwarder per destination

## Decision
Playout publishes once to a MediaMTX path on the Compose network (`rtmp://mediamtx:1935/b/<broadcast>`, where `mediamtx` is the Compose service name). Each destination is a separate copy-only forwarder (`ffmpeg -i rtmp://mediamtx:1935/b/<broadcast> -c copy -f flv <destination URL>/<key>`) supervised by the destination manager, so one destination failing or reconnecting never affects another, and nothing is encoded twice. MediaMTX also gives us a local preview (HLS/WebRTC) for the dashboard and OBS/ffplay for tests.

**Stream keys travel encrypted, with one opt-in exception.** Twitch and YouTube destinations must use `rtmps://` ingest URLs; the destination manager rejects an `rtmp://` URL for them when the destination is saved. A custom destination may use plain `rtmp://` only if the user ticks an explicit "unencrypted" option (for local tools such as OBS on the same network), and the dashboard marks it as such; keys for such a destination cross the network in clear text, which the opt-in states. 

**Keys never reach logs.** FFmpeg prints output URLs, which contain the key, on stderr. The destination manager reads each forwarder's stderr line by line and replaces the key (and its URL-encoded form) with `***` before anything is logged or stored, for successful and failed runs alike; error lines are kept otherwise intact. The key is passed to ffmpeg as an argument inside the server's container only, never through a shell. Test (M5): run forwarders against a refusing and a working receiver and grep all logs and the `events` table for the key.

**Certificates are verified.** For `rtmps://`, the forwarder runs ffmpeg with TLS peer verification on (`-tls_verify 1` with the system CA bundle) so the ingest hostname must match its certificate. The FFmpeg build, its version and TLS library are pinned in the server image and recorded in this ADR when the image is built in M5, not assumed now. Test (M5): a receiver with a self-signed certificate is refused.

MediaMTX does **not** own segments, ordering, holding media or recovery; StreamRelay does. Its own always-available/fallback feature is not used, because our playout already guarantees a continuous publish.

## Revisit if
MediaMTX adds latency or a failure mode we can't control; the forwarders can read straight from the playout over a local pipe instead.
