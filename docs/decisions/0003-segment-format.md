# 0003: Segment format

## Decision
- H.264 (High, level 3.1/4.0), AAC-LC 48 kHz stereo, 720p30 default, 2.5–4 Mbps video, 128 kbps audio.
- Keyframe every 2 s; every segment starts with an IDR frame and is independently decodable given its init segment.
- fMP4: one **init segment** (`ftyp`+`moov`) per encoder configuration, then media segments (`moof`+`mdat`). An init segment has its own id; each media segment names the init it needs. A configuration change (resolution, bitrate preset, camera switch that changes format) starts a new init.
- **Timeline:** presentation timestamps in the segment are the truth. The manifest also carries `start_us` relative to the broadcast's timeline origin, from the capture clock (`CMClock` host time), never the wall clock.
- **Epochs:** an epoch is one unbroken capture run. App restarts, interruptions and camera loss end an epoch; the next one starts at sequence 0 of a new epoch with a `start_us` that reflects the real elapsed time, so the server sees a genuine gap rather than a missing segment.

## Consequences
The server can detect missing segments (a hole in sequence within an epoch) separately from lost footage (between epochs). Segment size at defaults: about 1 MB.

## Revisit if
Platforms or the playout need something other than H.264/AAC (HEVC would halve upload size; worth testing in a later milestone because the playout re-encodes anyway).
