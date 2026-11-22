# 0006: MediaMTX as the local hub; one forwarder per destination

## Decision
Playout publishes once to a MediaMTX path on the Compose network (`rtmp://mediamtx:1935/b/<broadcast>`, where `mediamtx` is the Compose service name). Each destination is a separate copy-only forwarder (`ffmpeg -i rtmp://mediamtx:1935/b/<broadcast> -c copy -f flv <destination URL>/<key>`) supervised by the destination manager, so one destination failing or reconnecting never affects another, and nothing is encoded twice. MediaMTX also gives us a local preview (HLS/WebRTC) for the dashboard and OBS/ffplay for tests.

MediaMTX does **not** own segments, ordering, holding media or recovery; StreamRelay does. Its own always-available/fallback feature is not used, because our playout already guarantees a continuous publish.

## Revisit if
MediaMTX adds latency or a failure mode we can't control; the forwarders can read straight from the playout over a local pipe instead.
