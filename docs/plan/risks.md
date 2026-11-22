# Risks

Honest list, most serious first. Each names the milestone that finds out.

| # | Risk | Why it matters | Mitigation | Settled by |
|---|---|---|---|---|
| R1 | **iPhone stops the camera in the background or when the screen locks** | Breaks "records regardless" for someone who pockets the phone | Keep app on screen with idle timer off and a dimmed pocket mode; record interruptions as explicit gaps; tell the user plainly. Later option: audio-only recording continues in the background | M1 (first feel), M2 |
| R2 | **One RTMP session across holding screen and backlog** | A reconnect or timestamp jump shows viewers an ended stream | Playout re-encodes everything through one encoder on its own monotonic clock (ADR 0005), so the platform never sees a source switch | M1 |
| R3 | **Twitch/YouTube behaviour on reconnects and odd streams** | If the playout restarts, a forwarder may reconnect; platforms differ in how long they keep a stream "live", and YouTube flags low bitrate or silent audio | Forwarders decoupled from playout; holding screen keeps bitrate and quiet audio up; measure real platforms | M1 (Twitch), M5 (YouTube) |
| R4 | **Upload headroom, not playback speed, limits catch-up** | At 4 Mbps recording on a 5 Mbps uplink, backlog uploads at only 1 Mbps: a 5-minute outage takes 20 minutes just to upload | Default to 2.5–3 Mbps on cellular; HEVC trial; skip-to-live mode; later adaptive bitrate / lower broadcast rendition | M1 (bench caps, first real cellular run), M4 |
| R5 | **Time-stretch quality at 1.25–1.5×** | Music and overlapping speech sound bad with WSOLA; viewers may find 1.5× unpleasant anyway | `atempo` first, `rubberband` if needed, ramp speed changes, default 1.25× | M1 decision gate |
| R6 | **Segment seams** | AAC priming and encoder delay can add clicks or tiny offsets at segment joins | Playout decodes each epoch as one continuous stream (init + segments in order), not segment by segment | M1 |
| R7 | **Battery and heat over hours** | Camera + encoder + radio for 3–6 hours overheats or drains phones | Thermal-aware presets, 720p30 default, warn early; power bank expected for long sessions | M1 (30 min), M2/M6 (hours) |
| R8 | **Phone storage** | ≈1.8 GB/hour at 4 Mbps | Pre-flight estimate, low-space warning, optional deletion after acknowledgement | M2 |
| R9 | **Platform terms** | Delayed or replayed footage, holding screens and stream-key handling must fit Twitch and YouTube terms | Read the current terms before the M1 phone run; broadcaster's own content only | Before M1 step C |
| R10 | **Focus** | A big new product competes with ixOS, Relay and VPN for one person's time | M1 only after plan approval; one milestone at a time; no work on it unless Reuben asks | Ongoing |
| R11 | **iPhone app slots** | Free Apple team: 3 apps, all used (Device Lab, WebDriverAgent, VPN) | Decide before M1 step B: a StreamRelay screen inside Device Lab for development, or swap a slot | Before M1 step B |
| R12 | **Name** | "StreamRelay" is likely already used by others | Check before any public release or store listing | Before M8 |

Earlier spec items that are not risks any more: Android foreground-service limits on camera (still real, but Android is now later; when it starts, the service must be type `camera|microphone`, started while the app is visible, and uploads after a broadcast cannot rely on a `dataSync` service, which Android 15 limits to 6 hours a day).
