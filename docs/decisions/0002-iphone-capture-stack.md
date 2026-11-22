# 0002: iPhone capture stack

## Decision
- **Capture:** `AVCaptureSession` (camera + microphone), `AVCaptureVideoDataOutput` / `AVCaptureAudioDataOutput`.
- **Encoding and segmenting:** `AVAssetWriter` with `outputFileTypeProfile = .mpeg4AppleHLS`, `preferredOutputSegmentInterval = 2 s`, and `AVAssetWriterDelegate`, which hands us an **initialization segment** and then **separable fMP4 segments** as `Data`, with hardware H.264 (VideoToolbox) underneath. This is Apple's supported path for exactly our segment format; no custom muxer.
- **App:** a Forge app. SwiftUI and AVFoundation in Swift (capture, preview, UI); a Rust core over UniFFI that owns the segment queue, manifest, upload state machine and protocol (the ixOS rule: Rust owns state and rules, Swift presents them). Built on Linux with xtool.
- **Local queue:** segment files in the app's Application Support directory (excluded from backup), with a SQLite manifest owned by the Rust core.
- **Uploads:** foreground: `URLSession` driven by the Rust upload manager (see the protocol). After the app leaves the foreground: a background `URLSession` upload task per remaining segment, so the backlog drains with the app suspended.

## iPhone limits we design around
- **No camera in the background.** When the app backgrounds or the screen locks, `AVCaptureSession` is interrupted (`videoDeviceNotAvailableInBackground`). Multitasking camera access is for iPad and picture-in-picture video calls, not this. Mitigation: keep the app on screen with `isIdleTimerDisabled`, offer a dimmed "pocket mode" overlay, and record an explicit **gap** (new epoch) if capture is interrupted, so the server knows footage is missing rather than late.
- **Audio can continue in the background** with the `audio` background mode. Option for later: keep audio-only recording through an interruption. Not in v1.
- **Phone calls, Siri and other camera apps** interrupt the session; handled the same way (gap + resume).
- **Thermal state:** watch `ProcessInfo.thermalState`; at `.serious`, drop to a lower preset; at `.critical`, warn and stop cleanly so files are complete.
- **Storage:** check free space before starting (≈1.8 GB/hour at 4 Mbps) and warn.

## Consequences
fMP4 from AVAssetWriter is the same format the server stores, so the iPhone needs no remuxing. The Android client later needs its own fMP4 writer (Media3's `FragmentedMp4Muxer` or MediaCodec + MediaMuxer); the original CameraX `Recorder` cannot produce seamless 2 s segments.

## Revisit if
M1 or M2 shows `AVAssetWriter` segments drift from 2 s or split audio badly at boundaries; then segment ourselves from `CMSampleBuffer`s with VideoToolbox.
