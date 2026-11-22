# 0006: MediaMTX as the local hub; one forwarder per destination

## Decision
Playout publishes once to a local MediaMTX path (`rtmp://127.0.0.1/b/<broadcast>`). Each destination is a separate copy-only forwarder (`ffmpeg -c copy -f flv <destination>`) supervised by the destination manager, so one destination failing or reconnecting never affects another, and nothing is encoded twice. MediaMTX also gives us a local preview (HLS/WebRTC) for the dashboard and OBS/ffplay for tests.

MediaMTX does **not** own segments, ordering, holding media or recovery; StreamRelay does. Its own always-available/fallback feature is not used, because our playout already guarantees a continuous publish.

## Revisit if
MediaMTX adds latency or a failure mode we can't control; the forwarders can read straight from the playout over a local pipe instead.
