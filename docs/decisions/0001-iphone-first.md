# 0001: iPhone first

## Context
The original spec chose Android/Kotlin. Reuben wants iPhone first (2026-11-22). He builds iPhone apps from Linux with Forge (ixOS: SwiftUI + Rust, xtool), has an iPhone 15 Pro Max for testing, and his Android phone is in for repair.

## Decision
The first capture client is an iPhone app. Android is a later milestone (after M8) and reuses the same protocol and Rust core where it can.

## Consequences
- Background camera capture is not available on iPhone (see 0002). The product promise becomes: "keeps recording while the app is open, even with no signal; finishes uploading in the background."
- Phone slots: the free Apple team allows 3 sideloaded apps and all 3 are used (Device Lab, WebDriverAgent, VPN). M1 step B needs a decision: a StreamRelay screen inside Device Lab for development, or a slot swapped for the sitting.
- A paid Apple account is not needed for development, only for TestFlight/App Store distribution later.

## Revisit if
Reuben wants Android in parallel, or iPhone background limits make the product unworkable for his use (long hikes with the phone in a pocket).
