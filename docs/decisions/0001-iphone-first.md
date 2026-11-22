# 0001: iPhone first

## Context
The original spec chose Android/Kotlin. Reuben wants iPhone first (2026-11-22), confirmed in his follow-up review the same day: native Swift and AVFoundation, protocol and server kept platform-independent, no Android work now. He builds iPhone apps from Linux with xtool, has an iPhone 15 Pro Max for testing, and his Android phone is in for repair.

## Decision
The first capture client is an iPhone app. Android comes after M7 and implements the same protocol; the server does not change for it.

## Consequences
- Background camera capture is not available on iPhone (see 0002). The product promise becomes: "keeps recording while the app is open, even with no signal; finishes uploading in the background."
- Phone slots: the free Apple team allows 3 sideloaded apps and all 3 are used. Decided 2026-11-22: Reuben deletes an app for each phone sitting and StreamRelay installs as its own app, always with the same bundle id.
- A paid Apple account is not needed for development, only for TestFlight/App Store distribution later.

## Revisit if
Reuben wants Android in parallel, or iPhone background limits make the product unworkable for his use (long hikes with the phone in a pocket).
