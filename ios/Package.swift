// swift-tools-version: 6.0
// StreamRelay for iPhone: records 2 s fMP4 segments to disk whatever the
// network does and uploads them over protocol v1 (docs/protocol/upload-v1.md).
//
//   StreamRelayCore     Foundation only: manifest (SQLite), protocol client,
//                       upload queue. Builds and tests on Linux.
//   StreamRelayCapture  AVFoundation recorder and device-condition observers (iOS).
//   StreamRelayApp      SwiftUI app; xtool builds the `StreamRelay` product.
//   sr-upload           the upload core from the command line, for testing it
//                       against the real server on Linux.
//
// Build for an iPhone from Linux with xtool, from this folder: `xtool dev`.
// Core tests on Linux: `swift test` (needs libsqlite3-dev).

import PackageDescription

let package = Package(
    name: "StreamRelay",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "StreamRelayCore", targets: ["StreamRelayCore"]),
        .library(name: "StreamRelay", targets: ["StreamRelayApp"]),
    ],
    targets: [
        .systemLibrary(name: "CSQLite", path: "Sources/CSQLite", providers: [.apt(["libsqlite3-dev"])]),
        .target(
            name: "StreamRelayCore",
            dependencies: [.target(name: "CSQLite", condition: .when(platforms: [.linux]))],
            path: "Sources/StreamRelayCore",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .target(
            name: "StreamRelayCapture",
            dependencies: ["StreamRelayCore"],
            path: "Sources/StreamRelayCapture",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .target(
            name: "StreamRelayApp",
            dependencies: ["StreamRelayCore", "StreamRelayCapture"],
            path: "Sources/StreamRelayApp",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .executableTarget(
            name: "sr-upload",
            dependencies: ["StreamRelayCore"],
            path: "Sources/sr-upload",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .testTarget(
            name: "StreamRelayCoreTests",
            dependencies: ["StreamRelayCore"],
            path: "Tests/StreamRelayCoreTests",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
