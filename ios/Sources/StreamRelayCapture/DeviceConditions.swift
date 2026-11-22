#if os(iOS)
import AVFoundation
import Foundation
import StreamRelayCore
import UIKit

/// Watches what the phone does to a recording and tells the recorder
/// (milestones M2 condition matrix):
/// - capture interrupted (call, another app took the camera, Control Centre
///   on some models, background) -> end the epoch `interrupted`; resumed -> new epoch
/// - capture runtime error -> end the epoch `camera_lost`, try a new one
/// - thermal serious -> step down to 720p; critical -> stop recording
/// - Low Power Mode -> lower bitrate
/// - free space under the reserve (500 MB) -> stop recording, keep everything
public final class DeviceConditions: @unchecked Sendable {
    public static let reserveBytes: Int64 = 500_000_000

    public enum Stop: Sendable, Equatable {
        case thermalCritical
        case storageReserve
    }

    private let recorder: SegmentRecorder
    private let folder: URL
    private var tokens: [NSObjectProtocol] = []
    public var onStop: (@Sendable (Stop) -> Void)?

    public init(recorder: SegmentRecorder, folder: URL) {
        self.recorder = recorder
        self.folder = folder
    }

    public func start() {
        let nc = NotificationCenter.default
        tokens.append(nc.addObserver(forName: AVCaptureSession.wasInterruptedNotification, object: recorder.session,
                                     queue: .main) { [weak self] _ in
            self?.recorder.endEpoch(.interrupted)
        })
        tokens.append(nc.addObserver(forName: AVCaptureSession.interruptionEndedNotification, object: recorder.session,
                                     queue: .main) { [weak self] _ in
            self?.recorder.resume()               // a no-op once Stop was pressed
        })
        tokens.append(nc.addObserver(forName: AVCaptureSession.runtimeErrorNotification, object: recorder.session,
                                     queue: .main) { [weak self] _ in
            guard let self else { return }
            let recorder = self.recorder
            recorder.endEpoch(.cameraLost) {
                // resume() checks, when it runs, that recording wasn't stopped in the meantime
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) { recorder.resume() }
            }
        })
        tokens.append(nc.addObserver(forName: ProcessInfo.thermalStateDidChangeNotification, object: nil,
                                     queue: .main) { [weak self] _ in self?.applyPower() })
        tokens.append(nc.addObserver(forName: .NSProcessInfoPowerStateDidChange, object: nil,
                                     queue: .main) { [weak self] _ in self?.applyPower() })
        applyPower()
    }

    public func stop() {
        tokens.forEach(NotificationCenter.default.removeObserver)
        tokens.removeAll()
    }

    /// Thermal and Low Power Mode decide the quality.
    func applyPower() {
        let info = ProcessInfo.processInfo
        switch info.thermalState {
        case .critical:
            onStop?(.thermalCritical)
        case .serious:
            recorder.change(to: info.isLowPowerModeEnabled ? .low : .reduced)
        default:
            recorder.change(to: info.isLowPowerModeEnabled ? .low : .full)
        }
    }

    /// Called after each stored segment: stops at the storage reserve.
    public func checkSpace() {
        if let free = Self.freeBytes(at: folder), free < Self.reserveBytes {
            onStop?(.storageReserve)
        }
    }

    public static func freeBytes(at url: URL) -> Int64? {
        let v = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return v?.volumeAvailableCapacityForImportantUsage
    }

    /// Hours of recording left at the current quality before the reserve.
    public func hoursRemaining() -> Double? {
        guard let free = Self.freeBytes(at: folder) else { return nil }
        let bytesPerSecond = Double(recorder.quality.videoBitrate + 128_000) / 8
        return max(0, Double(free - Self.reserveBytes) / bytesPerSecond / 3600)
    }

    public static var thermalName: String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }
}
#endif
