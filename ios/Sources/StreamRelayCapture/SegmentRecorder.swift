#if os(iOS)
import AVFoundation
import Foundation
import StreamRelayCore
import UniformTypeIdentifiers

/// Records the camera and microphone straight into 2 s fMP4 segments
/// (AVAssetWriter's segment delegate, the same container HLS uses) and puts
/// each one into the manifest the moment it exists: file fsynced, then row.
/// Uploading is the queue's business; nothing here waits on the network.
///
/// One capture run is one epoch with its own writer and init segment
/// (init_id = epoch). An interruption, a camera failure, a quality step-down
/// or Stop ends the epoch: the writer is finished, its last (possibly short)
/// segment is stored, and the epoch is closed at that sequence.
public final class SegmentRecorder: NSObject, @unchecked Sendable {
    public struct Quality: Equatable, Sendable {
        public var width: Int
        public var height: Int
        public var fps: Int
        public var videoBitrate: Int
        public static let full = Quality(width: 1920, height: 1080, fps: 30, videoBitrate: 6_000_000)
        public static let reduced = Quality(width: 1280, height: 720, fps: 30, videoBitrate: 3_000_000)
        public static let low = Quality(width: 1280, height: 720, fps: 30, videoBitrate: 1_500_000)
    }

    public enum Event: Sendable {
        case epochStarted(Int)
        case segmentStored(SegmentKey, bytes: Int)
        case epochClosed(Int, lastSequence: Int, reason: EpochEndReason)
        case failed(String)
    }

    public let session = AVCaptureSession()
    public let manifest: Manifest
    public private(set) var quality: Quality
    public var onEvent: (@Sendable (Event) -> Void)?

    private let queue = DispatchQueue(label: "streamrelay.capture")
    private let videoOutput = AVCaptureVideoDataOutput()
    private let audioOutput = AVCaptureAudioDataOutput()
    private let origin: Date

    // epoch state, touched only on `queue`
    private var writer: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private var audioInput: AVAssetWriterInput?
    private var epoch = -1
    private var sequence = 0
    private var firstPTS: CMTime?
    private var epochOffsetMicros: Int64 = 0
    private var pendingClose: EpochEndReason?
    private var lastSegmentEnd: CMTime?

    /// `origin`: the recording's start; every segment's start_us counts from it, across epochs.
    public init(manifest: Manifest, origin: Date, quality: Quality = .full) {
        self.manifest = manifest
        self.origin = origin
        self.quality = quality
        super.init()
    }

    // MARK: Session

    public func configure() throws {
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        session.sessionPreset = .inputPriority
        guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            throw RecorderError.noCamera
        }
        let cameraInput = try AVCaptureDeviceInput(device: camera)
        guard session.canAddInput(cameraInput) else { throw RecorderError.noCamera }
        session.addInput(cameraInput)
        try setFormat(camera, quality)
        if let mic = AVCaptureDevice.default(for: .audio), let micInput = try? AVCaptureDeviceInput(device: mic),
           session.canAddInput(micInput) {
            session.addInput(micInput)
        }
        videoOutput.alwaysDiscardsLateVideoFrames = false
        videoOutput.setSampleBufferDelegate(self, queue: queue)
        audioOutput.setSampleBufferDelegate(self, queue: queue)
        if session.canAddOutput(videoOutput) { session.addOutput(videoOutput) }
        if session.canAddOutput(audioOutput) { session.addOutput(audioOutput) }
        if let c = videoOutput.connection(with: .video) {
            if c.isVideoRotationAngleSupported(90) { c.videoRotationAngle = 90 }   // portrait
            if c.isVideoStabilizationSupported { c.preferredVideoStabilizationMode = .standard }
        }
    }

    private func setFormat(_ camera: AVCaptureDevice, _ q: Quality) throws {
        let want = q.width * q.height
        let format = camera.formats.first { f in
            let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
            return Int(d.width) * Int(d.height) == want
                && f.videoSupportedFrameRateRanges.contains { $0.maxFrameRate >= Double(q.fps) }
        }
        try camera.lockForConfiguration()
        if let format { camera.activeFormat = format }
        camera.activeVideoMinFrameDuration = CMTime(value: 1, timescale: CMTimeScale(q.fps))
        camera.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: CMTimeScale(q.fps))
        camera.unlockForConfiguration()
    }

    public enum RecorderError: Error {
        case noCamera
        case writer(String)
    }

    // MARK: Epochs

    /// Starts a new epoch; the writer starts on the first video frame.
    public func startEpoch() {
        queue.async { [self] in
            guard writer == nil else { return }
            do {
                epoch = try manifest.nextEpoch()
                try manifest.openEpoch(epoch)
                sequence = 0
                firstPTS = nil
                lastSegmentEnd = nil
                pendingClose = nil
                epochOffsetMicros = Int64(Date().timeIntervalSince(origin) * 1_000_000)
                try makeWriter()
                onEvent?(.epochStarted(epoch))
            } catch {
                onEvent?(.failed("start epoch: \(error)"))
            }
        }
        if !session.isRunning {
            DispatchQueue.global(qos: .userInitiated).async { self.session.startRunning() }
        }
    }

    /// Ends the current epoch: finishes the writer (its last segment arrives
    /// through the delegate), then closes the epoch at the last stored sequence.
    public func endEpoch(_ reason: EpochEndReason, completion: (@Sendable () -> Void)? = nil) {
        queue.async { [self] in
            guard let w = writer else { completion?(); return }
            pendingClose = reason
            videoInput?.markAsFinished()
            audioInput?.markAsFinished()
            if w.status == .writing {
                w.finishWriting { [self] in
                    queue.async { [self] in
                        closeCurrentEpoch(reason)
                        completion?()
                    }
                }
            } else {
                closeCurrentEpoch(reason)
                completion?()
            }
        }
    }

    /// Ends the epoch and restarts with another quality (thermal step-down, Low Power Mode).
    public func change(to q: Quality) {
        queue.async { [self] in
            guard q != quality else { return }
            let camera = session.inputs.compactMap { ($0 as? AVCaptureDeviceInput)?.device }.first { $0.hasMediaType(.video) }
            guard writer != nil else {              // not recording: the next epoch uses it
                quality = q
                if let camera { try? setFormat(camera, q) }
                return
            }
            endEpoch(.interrupted) { [self] in
                queue.async { [self] in
                    quality = q
                    if let camera { try? setFormat(camera, q) }
                    startEpoch()
                }
            }
        }
    }

    public func stopSession() {
        DispatchQueue.global(qos: .userInitiated).async { self.session.stopRunning() }
    }

    private func closeCurrentEpoch(_ reason: EpochEndReason) {
        guard epoch >= 0, writer != nil else { return }
        let last = sequence - 1
        do {
            try manifest.closeEpoch(epoch, lastSequence: last, reason: reason)
            onEvent?(.epochClosed(epoch, lastSequence: last, reason: reason))
        } catch {
            onEvent?(.failed("close epoch: \(error)"))
        }
        writer = nil
        videoInput = nil
        audioInput = nil
    }

    private func makeWriter() throws {
        let w = AVAssetWriter(contentType: UTType(AVFileType.mp4.rawValue)!)
        w.outputFileTypeProfile = .mpeg4AppleHLS
        w.preferredOutputSegmentInterval = CMTime(seconds: 2, preferredTimescale: 1)
        w.delegate = self
        let fps = quality.fps
        let v = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            // portrait: the rotated buffers are height x width
            AVVideoWidthKey: quality.height,
            AVVideoHeightKey: quality.width,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: quality.videoBitrate,
                AVVideoMaxKeyFrameIntervalKey: fps * 2,
                AVVideoMaxKeyFrameIntervalDurationKey: 2,
                AVVideoAllowFrameReorderingKey: false,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                AVVideoExpectedSourceFrameRateKey: fps,
            ] as [String: Any],
        ])
        v.expectsMediaDataInRealTime = true
        let a = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 128_000,
        ])
        a.expectsMediaDataInRealTime = true
        guard w.canAdd(v), w.canAdd(a) else { throw RecorderError.writer("cannot add inputs") }
        w.add(v)
        w.add(a)
        writer = w
        videoInput = v
        audioInput = a
    }

    private func micros(_ t: CMTime) -> Int64 {
        Int64((CMTimeGetSeconds(t) * 1_000_000).rounded())
    }
}

extension SegmentRecorder: AVCaptureVideoDataOutputSampleBufferDelegate, AVCaptureAudioDataOutputSampleBufferDelegate {
    public func captureOutput(_ output: AVCaptureOutput, didOutput sample: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let w = writer, pendingClose == nil else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        if w.status == .unknown {
            guard output === videoOutput else { return }          // start on a video frame
            w.initialSegmentStartTime = pts
            guard w.startWriting() else {
                onEvent?(.failed("writer: \(w.error?.localizedDescription ?? "did not start")"))
                return
            }
            w.startSession(atSourceTime: pts)
            firstPTS = pts
        }
        guard w.status == .writing else { return }
        if output === videoOutput, let v = videoInput, v.isReadyForMoreMediaData {
            v.append(sample)
        } else if output === audioOutput, let a = audioInput, a.isReadyForMoreMediaData {
            a.append(sample)
        }
    }
}

extension SegmentRecorder: AVAssetWriterDelegate {
    public func assetWriter(_ writer: AVAssetWriter, didOutputSegmentData segmentData: Data,
                            segmentType: AVAssetSegmentType, segmentReport: AVAssetSegmentReport?) {
        // Called on the writer's own queue; the manifest is thread-safe and the
        // sequence counter is only advanced here, one segment at a time.
        do {
            switch segmentType {
            case .initialization:
                try manifest.addInit(id: epoch, data: segmentData)
            case .separable:
                let video = segmentReport?.trackReports.first { $0.mediaType == .video }
                let start = video?.earliestPresentationTimeStamp ?? lastSegmentEnd ?? firstPTS ?? .zero
                let duration = video?.duration ?? CMTime(seconds: 2, preferredTimescale: 600)
                lastSegmentEnd = CMTimeAdd(start, duration)
                let rel = micros(CMTimeSubtract(start, firstPTS ?? start))
                let key = SegmentKey(epoch: epoch, sequence: sequence)
                try manifest.addSegment(key, initID: epoch, startMicros: epochOffsetMicros + rel,
                                        durationMicros: micros(duration), data: segmentData)
                sequence += 1
                onEvent?(.segmentStored(key, bytes: segmentData.count))
            @unknown default:
                break
            }
        } catch {
            onEvent?(.failed("store segment: \(error)"))
        }
    }
}
#endif
