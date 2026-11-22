# 0004: Segmented HTTPS uploads from the phone

## Context
RTMP/SRT/WebSocket from the phone ties "is the stream alive" to one connection. We want every 2 s of footage to be an independent, retryable, verifiable unit.

## Decision
Each segment is uploaded with one idempotent `PUT` over HTTPS, verified by SHA-256, acknowledged only once durably stored. See [upload-v1](../protocol/upload-v1.md). Whole-segment retry (≈1 MB) instead of byte-range resume in v1; byte-range resume (`Content-Range`) is reserved in the protocol for large segments later.

## Consequences
- Upload headroom is what clears the backlog: at 4 Mbps recording, a 5 Mbps uplink only drains backlog at 1 Mbps. See risks R4.
- HTTP/2 lets several segments upload concurrently on one connection; the client sends up to 3 at once, oldest first (or newest first in skip-to-live mode).
- Higher latency than RTMP in the normal state: the playout delay is at least one segment plus upload time. Target 6–10 s glass-to-glass in Live.

## Revisit if
M3 shows per-request overhead dominates on bad networks; then batch several segments per request or move to QUIC/HTTP/3.
