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
///
/// Threading: every field below is touched only on `queue`, except each
/// writer's `Run`, which the writer's delegate callback updates under the
/// run's own lock. A callback therefore always stores its segment under the
/// epoch and sequence of the writer that produced it, whatever epoch the
/// recorder has moved on to.
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
    public var onEvent: (@Sendable (Event) -> Void)?

    private let queue = DispatchQueue(label: "streamrelay.capture")
    private let videoOutput = AVCaptureVideoDataOutput()
    private let audioOutput = AVCaptureAudioDataOutput()
    private let origin: Date

    /// One writer's epoch. The delegate callback reads and advances it under `lock`.
    private final class Run: @unchecked Sendable {
        let epoch: Int
        let writer: AVAssetWriter
        let video: AVAssetWriterInput
        let audio: AVAssetWriterInput
        let offsetMicros: Int64
        let lock = NSLock()
        var sequence = 0
        var firstPTS: CMTime?
        var lastSegmentEnd: CMTime?
        var storeFailed = false       // a segment could not be stored: take nothing more from this writer

        init(epoch: Int, writer: AVAssetWriter, video: AVAssetWriterInput, audio: AVAssetWriterInput, offsetMicros: Int64) {
            self.epoch = epoch
            self.writer = writer
            self.video = video
            self.audio = audio
            self.offsetMicros = offsetMicros
        }
    }

    private enum State {
        case idle
        case recording(Run)
        case closing(Run, reason: EpochEndReason, waiting: [@Sendable () -> Void])
    }

    // all on `queue`
    private var state = State.idle
    private var active = false            // the person wants to be recording (Start pressed, Stop not yet)
    private var resumeAfterClose = false  // an epoch start asked for while the previous one was closing
    private var generation = 0            // bumped by Stop, so a late quality change can't restart
    private var writerFailures = 0
    private var runs: [ObjectIdentifier: Run] = [:]
    private let runsLock = NSLock()

    private let qualityLock = NSLock()
    private var _quality: Quality
    /// The quality the next epoch uses (safe to read from any thread).
    public var quality: Quality {
        qualityLock.lock()
        defer { qualityLock.unlock() }
        return _quality
    }

    /// `origin`: the recording's start; every segment's start_us counts from it, across epochs.
    public init(manifest: Manifest, origin: Date, quality: Quality = .full) {
        self.manifest = manifest
        self.origin = origin
        self._quality = quality
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

    /// Picks a format of the wanted size whose frame-rate ranges include the wanted rate, and sets the
    /// frame duration only when the active format supports it (an unsupported duration raises).
    private func setFormat(_ camera: AVCaptureDevice, _ q: Quality) throws {
        let fps = Double(q.fps)
        func supports(_ f: AVCaptureDevice.Format) -> Bool {
            f.videoSupportedFrameRateRanges.contains { $0.minFrameRate <= fps && fps <= $0.maxFrameRate }
        }
        let format = camera.formats.first { f in
            let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
            return Int(d.width) == q.width && Int(d.height) == q.height && supports(f)
        }
        try camera.lockForConfiguration()
        defer { camera.unlockForConfiguration() }
        if let format { camera.activeFormat = format }
        if supports(camera.activeFormat) {
            camera.activeVideoMinFrameDuration = CMTime(value: 1, timescale: CMTimeScale(q.fps))
            camera.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: CMTimeScale(q.fps))
        }
    }

    public enum RecorderError: Error {
        case noCamera
        case writer(String)
    }

    private var camera: AVCaptureDevice? {
        session.inputs.compactMap { ($0 as? AVCaptureDeviceInput)?.device }.first { $0.hasMediaType(.video) }
    }

    // MARK: Recording control

    /// Start pressed: record until `finish`.
    public func begin() {
        queue.async { [self] in
            active = true
            writerFailures = 0
            startEpochOnQueue()
        }
        if !session.isRunning {
            DispatchQueue.global(qos: .userInitiated).async { self.session.startRunning() }
        }
    }

    /// An interruption ended or the camera came back: a new epoch, if still recording.
    public func resume() {
        queue.async { [self] in
            guard active else { return }
            startEpochOnQueue()
        }
    }

    /// Ends the current epoch (interruption, camera lost). Recording stays active, so `resume` restarts it.
    public func endEpoch(_ reason: EpochEndReason, completion: (@Sendable () -> Void)? = nil) {
        queue.async { [self] in closeOnQueue(reason, completion: completion) }
    }

    /// Stop pressed: ends the epoch `stopped`. `completion` runs once the epoch's close is in the manifest.
    public func finish(completion: @escaping @Sendable () -> Void) {
        queue.async { [self] in
            active = false
            resumeAfterClose = false
            generation += 1
            closeOnQueue(.stopped, completion: completion)
        }
    }

    /// Ends the epoch and restarts with another quality (thermal step-down, Low Power Mode).
    public func change(to q: Quality) {
        queue.async { [self] in
            guard q != quality else { return }
            guard case .recording = state else {          // not recording: the next epoch uses it
                setQuality(q)
                return
            }
            let gen = generation
            closeOnQueue(.interrupted) { [self] in
                queue.async { [self] in
                    setQuality(q)
                    // Stop pressed meanwhile: this recording is over, don't start another epoch
                    if generation == gen && active { startEpochOnQueue() }
                }
            }
        }
    }

    public func stopSession() {
        DispatchQueue.global(qos: .userInitiated).async { self.session.stopRunning() }
    }

    private func setQuality(_ q: Quality) {
        qualityLock.lock()
        _quality = q
        qualityLock.unlock()
        if let camera { try? setFormat(camera, q) }
    }

    // MARK: Epochs (on `queue`)

    private func startEpochOnQueue() {
        switch state {
        case .recording:
            return
        case .closing:
            resumeAfterClose = true          // start once the close completes
            return
        case .idle:
            break
        }
        do {
            let epoch = try manifest.nextEpoch()
            let (w, v, a) = try makeWriter()
            // the epoch row only after the writer exists, so a failed setup leaves no open epoch
            try manifest.openEpoch(epoch)
            let run = Run(epoch: epoch, writer: w, video: v, audio: a,
                          offsetMicros: Int64(Date().timeIntervalSince(origin) * 1_000_000))
            runsLock.lock()
            runs[ObjectIdentifier(w)] = run
            runsLock.unlock()
            state = .recording(run)
            onEvent?(.epochStarted(epoch))
        } catch {
            onEvent?(.failed("start epoch: \(error)"))
            retryAfterWriterFailure()
        }
    }

    private func closeOnQueue(_ reason: EpochEndReason, completion: (@Sendable () -> Void)?) {
        switch state {
        case .idle:
            completion?()
        case .closing(let run, let r, var waiting):
            // one finishWriting per writer: later callers wait for the close already under way
            if let completion { waiting.append(completion) }
            state = .closing(run, reason: r, waiting: waiting)
        case .recording(let run):
            state = .closing(run, reason: reason, waiting: completion.map { [$0] } ?? [])
            run.video.markAsFinished()
            run.audio.markAsFinished()
            if run.writer.status == .writing {
                run.writer.finishWriting { [self] in
                    queue.async { [self] in
                        if run.writer.status == .failed {
                            onEvent?(.failed("finish writing: \(run.writer.error?.localizedDescription ?? "failed")"))
                        }
                        commitClose(run)
                    }
                }
            } else {
                commitClose(run)
            }
        }
    }

    /// Records the close in the manifest, retrying until it sticks, then runs the waiting completions.
    private func commitClose(_ run: Run) {
        guard case .closing(let r, let reason, let waiting) = state, r === run else { return }
        run.lock.lock()
        let last = run.sequence - 1
        run.lock.unlock()
        do {
            try manifest.closeEpoch(run.epoch, lastSequence: last, reason: reason)
        } catch {
            // the epoch must close, or the broadcast can never finish: keep the state and try again
            onEvent?(.failed("close epoch \(run.epoch): \(error); retrying"))
            queue.asyncAfter(deadline: .now() + 1) { [self] in commitClose(run) }
            return
        }
        runsLock.lock()
        runs[ObjectIdentifier(run.writer)] = nil
        runsLock.unlock()
        state = .idle
        onEvent?(.epochClosed(run.epoch, lastSequence: last, reason: reason))
        waiting.forEach { $0() }
        if resumeAfterClose && active {
            resumeAfterClose = false
            startEpochOnQueue()
        }
    }

    /// The writer failed (would not start, an append failed, a segment could not be stored): close the
    /// epoch at what was stored and, while still recording, try a new epoch a little later.
    private func writerFailed(_ run: Run, _ why: String) {
        guard case .recording(let r) = state, r === run else { return }
        onEvent?(.failed(why))
        closeOnQueue(.interrupted) { [self] in
            queue.async { [self] in retryAfterWriterFailure() }
        }
    }

    private func retryAfterWriterFailure() {
        writerFailures += 1
        guard active, writerFailures <= 5 else {
            if active { onEvent?(.failed("recording stopped: the camera writer keeps failing")) }
            return
        }
        let gen = generation
        queue.asyncAfter(deadline: .now() + Double(writerFailures)) { [self] in
            if generation == gen && active { startEpochOnQueue() }
        }
    }

    private func makeWriter() throws -> (AVAssetWriter, AVAssetWriterInput, AVAssetWriterInput) {
        let q = quality
        let w = AVAssetWriter(contentType: UTType(AVFileType.mp4.rawValue)!)
        w.outputFileTypeProfile = .mpeg4AppleHLS
        w.preferredOutputSegmentInterval = CMTime(seconds: 2, preferredTimescale: 1)
        w.delegate = self
        let v = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            // portrait: the rotated buffers are height x width
            AVVideoWidthKey: q.height,
            AVVideoHeightKey: q.width,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: q.videoBitrate,
                AVVideoMaxKeyFrameIntervalKey: q.fps * 2,
                AVVideoMaxKeyFrameIntervalDurationKey: 2,
                AVVideoAllowFrameReorderingKey: false,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                AVVideoExpectedSourceFrameRateKey: q.fps,
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
        return (w, v, a)
    }

    private static func micros(_ t: CMTime) -> Int64 {
        Int64((CMTimeGetSeconds(t) * 1_000_000).rounded())
    }

    private static func valid(_ t: CMTime?) -> CMTime? {
        guard let t, t.isValid, t.isNumeric, !t.isIndefinite else { return nil }
        return t
    }
}

extension SegmentRecorder: AVCaptureVideoDataOutputSampleBufferDelegate, AVCaptureAudioDataOutputSampleBufferDelegate {
    public func captureOutput(_ output: AVCaptureOutput, didOutput sample: CMSampleBuffer, from connection: AVCaptureConnection) {
        // on `queue`
        guard case .recording(let run) = state else { return }
        let w = run.writer
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        if w.status == .unknown {
            guard output === videoOutput else { return }          // start on a video frame
            w.initialSegmentStartTime = pts
            guard w.startWriting() else {
                writerFailed(run, "writer: \(w.error?.localizedDescription ?? "did not start")")
                return
            }
            w.startSession(atSourceTime: pts)
            run.lock.lock()
            run.firstPTS = pts
            run.lock.unlock()
        }
        guard w.status == .writing else {
            if w.status == .failed { writerFailed(run, "writer: \(w.error?.localizedDescription ?? "failed")") }
            return
        }
        var ok = true
        if output === videoOutput, run.video.isReadyForMoreMediaData {
            ok = run.video.append(sample)
        } else if output === audioOutput, run.audio.isReadyForMoreMediaData {
            ok = run.audio.append(sample)
        }
        if !ok { writerFailed(run, "append: \(w.error?.localizedDescription ?? "failed")") }
    }
}

extension SegmentRecorder: AVAssetWriterDelegate {
    public func assetWriter(_ writer: AVAssetWriter, didOutputSegmentData segmentData: Data,
                            segmentType: AVAssetSegmentType, segmentReport: AVAssetSegmentReport?) {
        // Any thread. Everything this needs is in the writer's own Run, under its lock, so a segment is
        // stored under its own writer's epoch and sequence even while the recorder moves on.
        runsLock.lock()
        let found = runs[ObjectIdentifier(writer)]
        runsLock.unlock()
        guard let run = found else { return }
        run.lock.lock()
        defer { run.lock.unlock() }
        guard !run.storeFailed else { return }
        do {
            switch segmentType {
            case .initialization:
                try manifest.addInit(id: run.epoch, data: segmentData)
            case .separable:
                let video = segmentReport?.trackReports.first { $0.mediaType == .video }
                // a report can hold kCMTimeInvalid: treat it as missing and use the fallbacks
                let start = Self.valid(video?.earliestPresentationTimeStamp) ?? run.lastSegmentEnd ?? run.firstPTS ?? .zero
                let duration = Self.valid(video?.duration) ?? CMTime(seconds: 2, preferredTimescale: 600)
                let rel = Self.micros(CMTimeSubtract(start, run.firstPTS ?? start))
                let key = SegmentKey(epoch: run.epoch, sequence: run.sequence)
                try manifest.addSegment(key, initID: run.epoch, startMicros: run.offsetMicros + rel,
                                        durationMicros: Self.micros(duration), data: segmentData)
                run.lastSegmentEnd = CMTimeAdd(start, duration)
                run.sequence += 1
                onEvent?(.segmentStored(key, bytes: segmentData.count))
            @unknown default:
                break
            }
        } catch {
            // never reuse the key for other media: stop taking segments from this writer and end the epoch
            run.storeFailed = true
            queue.async { [self] in writerFailed(run, "store segment: \(error)") }
        }
    }
}
#endif
