# 0001: iPhone first

## Context
The original spec chose Android/Kotlin. Reuben wants iPhone first (2026-11-22), confirmed in his follow-up review the same day: native Swift and AVFoundation, protocol and server kept platform-independent, no Android work now. He builds iPhone apps from Linux with xtool, has an iPhone 15 Pro Max for testing, and his Android phone is in for repair.

## Decision
The first capture client is an iPhone app. Android comes after M7 and implements the same protocol; the server does not change for it.

## Consequences
- Background camera capture is not available on iPhone (see 0002). The product promise becomes: "keeps recording while the app is open, even with no signal; finishes uploading in the background."
- Phone slots: the free Apple team allows 3 sideloaded apps and all 3 are used. Decided 2026-11-22: Reuben deletes an app for each phone sitting and StreamRelay installs as its own app, always with the same bundle id.
- A paid Apple account is not needed for development, only for TestFlight/App Store distribution later.

## The later Android client can do more (Reuben, 2026-11-22)
Android allows camera and microphone capture from a foreground service (`foregroundServiceType="camera|microphone"`; Android 14+ also needs `FOREGROUND_SERVICE_CAMERA`/`_MICROPHONE` and the runtime permissions). The Android client can therefore **keep recording with the screen off or in another app**, which is the promise the iPhone can't make and matters for body-cam use. Plan for these:
- The service must be started while the app is visible (the user taps Start, then can leave).
- A persistent notification is required; it carries Stop and the status line.
- Uploads run inside the same service while recording. After Stop, the backlog drains in a user-initiated data transfer job (API 34+) or WorkManager, not a `dataSync` service (capped at about 6 h a day on Android 15).
- OEM background killers: ask for the battery-optimisation exemption at first run.
- Extras worth having: live bitrate changes and keyframes on demand through MediaCodec, and USB/UVC cameras.

The server and protocol stay the same; only the capture client differs.

## Revisit if
Reuben wants Android in parallel, or iPhone background limits make the product unworkable for his use (long hikes with the phone in a pocket).
