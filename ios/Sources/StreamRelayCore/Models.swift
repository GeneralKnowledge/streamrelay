import Foundation

/// A segment's key on the server: (broadcast, epoch, sequence). Within one
/// recording the broadcast is fixed, so the manifest keys by (epoch, sequence).
public struct SegmentKey: Hashable, Comparable, Codable, Sendable, CustomStringConvertible {
    public var epoch: Int
    public var sequence: Int

    public init(epoch: Int, sequence: Int) {
        self.epoch = epoch
        self.sequence = sequence
    }

    public static func < (a: SegmentKey, b: SegmentKey) -> Bool {
        (a.epoch, a.sequence) < (b.epoch, b.sequence)
    }

    public var description: String { "\(epoch)/\(sequence)" }
}

/// Upload state of a segment in the manifest. Only the queue moves it.
public enum UploadState: String, Codable, Sendable {
    case pending        // on disk, not yet acknowledged
    case acknowledged   // the server answered 201/200: durably stored
    case damaged        // the server rejected it twice with 422; kept on disk, reported
}

/// Recovery mode chosen by the broadcaster (protocol v1, create broadcast).
public enum CatchUpMode: String, Codable, Sendable, CaseIterable {
    case catchUp = "catch_up"
    case hold
    case skipToLive = "skip_to_live"
}

/// Why an epoch ended (protocol v1, epoch end).
public enum EpochEndReason: String, Codable, Sendable {
    case interrupted
    case appRestart = "app_restart"
    case cameraLost = "camera_lost"
    case stopped
}

public struct SegmentRecord: Equatable, Sendable {
    public var key: SegmentKey
    public var initID: Int
    public var startMicros: Int64
    public var durationMicros: Int64
    public var bytes: Int64
    public var sha256: String
    public var file: String           // path relative to the recording folder
    public var isFinal: Bool
    public var state: UploadState
    public var attempts: Int
}

public struct EpochRecord: Equatable, Sendable {
    public var epoch: Int
    public var lastSequence: Int?     // set when the epoch is closed; -1 = empty
    public var endReason: EpochEndReason?
    public var endSent: Bool          // the server accepted the epoch end call
}
