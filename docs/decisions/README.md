# Architecture decision records

Each ADR: context, decision, consequences, and **revisit if** (the evidence that would change it). Status is Proposed until Reuben approves the plan.

| # | Decision | Status |
|---|---|---|
| [0001](0001-iphone-first.md) | iPhone first; Android later | Accepted (Reuben, 2026-11-22) |
| [0002](0002-iphone-capture-stack.md) | iPhone capture: AVFoundation + VideoToolbox, AVAssetWriter fMP4 segments, Forge app (SwiftUI + Rust core) | Proposed |
| [0003](0003-segment-format.md) | H.264 + AAC, fMP4, 2 s segments, init segment per encoder configuration | Proposed |
| [0004](0004-resumable-http-upload.md) | Segmented HTTPS uploads, not RTMP/WebSocket from the phone | Proposed |
| [0005](0005-playout-reencode-once.md) | One playout process per broadcast decodes, schedules and re-encodes once; one stable output | Proposed |
| [0006](0006-mediamtx-fanout.md) | MediaMTX as local hub; one copy-only forwarder per destination | Proposed |
| [0007](0007-fastapi-postgres.md) | FastAPI + PostgreSQL; no Redis in v1 | Proposed |
| [0008](0008-storage.md) | Filesystem segment store first, S3-compatible behind the same interface | Proposed |
| [0009](0009-dashboard.md) | Dashboard: Next.js, after the API is stable (M7) | Proposed |
