# StreamRelay: implementation plan

Working name. A network outage becomes a delay in the broadcast, not the end of it.

Nothing here is product code yet. Spike S1 starts once this plan is approved.

## Components

| Component | What | First platform |
|---|---|---|
| StreamRelay Mobile | Records continuously, cuts 2 s segments, queues them on disk, uploads when it can | **iPhone** (Reuben, 2026-11-22). Android later |
| StreamRelay Server | Receives segments, orders them, plays one continuous stream out, fans it out to platforms | Linux, one VPS, Docker Compose |
| StreamRelay Control | Web dashboard for broadcasts, destinations, backlog, storage | Browser |

The rule everything else follows: **recording and broadcasting are independent.** The recorder never waits on an upload, and an upload failure never touches the recorder.

## Read in this order

1. [Decisions (ADRs)](decisions/README.md): the choices, each with what would make us change it.
2. [Upload protocol v1](protocol/upload-v1.md): the phone/server contract.
3. [Playout engine](architecture/playout.md): how one RTMP session survives holding screens and catch-up. The hardest part.
4. [Database schema](architecture/database-schema.md) and [schema.sql](architecture/schema.sql).
5. [Milestones M1–M7](plan/milestones.md), each with the evidence that completes it. The M4 exit test is the end-to-end proof: iPhone → outage → recovered upload → continuous broadcast → OBS.
6. [Risks](plan/risks.md): unproven assumptions in order of danger, and the guarantees we will and won't make.
7. [Storage and security](architecture/storage-and-security.md): limits, retention, ownership, untrusted media.
8. [Repository status](plan/status.md): what exists, what is only written down, and decisions needed.
9. [Spike S1](plan/spike-s1-playout.md): the first implementation task.
10. [Issue and PR sequence](plan/issues.md) with acceptance criteria.

## What changed from the original spec

- **iPhone first.** The capture client is an iPhone app; the Android/Kotlin/CameraX stack becomes the later Android client (ADR 0001, 0002).
- **The iPhone cannot record with the camera in the background.** Capture runs while StreamRelay is on screen; the screen can dim but not lock. Uploads do continue in the background. This narrows "records regardless" and the app must say so (ADR 0002, risk R3).
- **The playout always re-encodes, once.** Catch-up at 1.25–1.5× and the holding screen both need encoding anyway, and a single encoder is what keeps timestamps continuous for Twitch and YouTube (ADR 0005).
- **One PUT per segment instead of POST + PUT.** Fewer round trips on a bad connection; idempotent by checksum (protocol doc).
- **"Catch up or skip to live" is a broadcaster setting, not a viewer one.** Twitch and YouTube viewers all see the same stream; a per-viewer choice needs our own player (later).
- **Redis is dropped from v1.** Postgres covers job coordination for one server.
- **Media pipeline first, then the phone, then the end-to-end proof with OBS** (Reuben's follow-up review). Twitch and YouTube come in M5.
- **Filesystem storage first, S3-compatible second.** Same interface, so self-hosters need nothing extra.
