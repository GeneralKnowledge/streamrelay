#if os(iOS)
import Foundation
import Network
import Observation
import StreamRelayCapture
import StreamRelayCore

/// The app's state: pairing, the current recording, the recorder and the
/// upload queue. Views only read it and call its methods.
@MainActor
@Observable
final class AppModel {
    // settings
    var serverURL: String = UserDefaults.standard.string(forKey: "serverURL") ?? "" {
        didSet { UserDefaults.standard.set(serverURL, forKey: "serverURL") }
    }
    var mode: CatchUpMode = CatchUpMode(rawValue: UserDefaults.standard.string(forKey: "mode") ?? "") ?? .catchUp {
        didSet { UserDefaults.standard.set(mode.rawValue, forKey: "mode") }
    }
    var holdMaxDelayMinutes: Int = max(1, UserDefaults.standard.integer(forKey: "holdMinutes")) {
        didSet { UserDefaults.standard.set(holdMaxDelayMinutes, forKey: "holdMinutes") }
    }
    var title: String = "StreamRelay broadcast"

    // state shown on screen
    private(set) var paired = Keychain.token != nil
    private(set) var recording = false
    private(set) var epoch: Int?
    private(set) var captured = 0
    private(set) var backlog = Manifest.Backlog(segments: 0, bytes: 0, seconds: 0, damaged: 0)
    private(set) var uploadLine = "Idle"
    private(set) var hoursLeft: Double?
    private(set) var problem: String?
    private(set) var unfinished: Manifest?

    // machinery
    private(set) var recorder: SegmentRecorder?
    private var conditions: DeviceConditions?
    private var queue: UploadQueue?
    private var queueTask: Task<Void, Never>?
    private var ticker: Task<Void, Never>?
    private var handOff: BackgroundHandOff?
    private let path = NWPathMonitor()
    private var current: Manifest?

    static var recordingsFolder: URL {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let f = docs.appendingPathComponent("Recordings", isDirectory: true)
        try? FileManager.default.createDirectory(at: f, withIntermediateDirectories: true)
        return f
    }

    var base: URL? { URL(string: serverURL.trimmingCharacters(in: .whitespaces)) }

    private func client() -> ProtocolClient? {
        guard let base else { return nil }
        return ProtocolClient(transport: URLSessionTransport(base: base, token: { Keychain.token }))
    }

    // MARK: Launch

    /// After a crash or force-quit: close open epochs, tidy files, and pick up
    /// the newest recording that has not finished uploading.
    func launch() {
        let folders = (try? FileManager.default.contentsOfDirectory(at: Self.recordingsFolder, includingPropertiesForKeys: [.creationDateKey])) ?? []
        for f in folders.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            guard let m = try? Manifest(folder: f) else { continue }
            try? m.recoverAfterRelaunch()
            if let r = try? m.recording(), !(r.finishSent && ((try? m.backlog().segments) ?? 0) == 0) {
                unfinished = m
            }
        }
        if let base {
            let h = BackgroundHandOff(base: base, token: { Keychain.token }) { name in
                try? Manifest(folder: AppModel.recordingsFolder.appendingPathComponent(name))
            }
            h.resume()
            handOff = h
        }
        if let m = unfinished {
            current = m
            startQueue(m)
        }
        path.pathUpdateHandler = { [weak self] p in
            guard p.status == .satisfied else { return }
            Task { @MainActor in await self?.queue?.networkChanged() }   // retry at once
        }
        path.start(queue: DispatchQueue(label: "streamrelay.path"))
        startTicker()
    }

    // MARK: Pairing

    func pair(code: String) async {
        problem = nil
        guard let base, base.scheme == "https" || base.scheme == "http" else {
            problem = "Enter the server address, for example https://relay.example.com"
            return
        }
        do {
            let c = ProtocolClient(transport: URLSessionTransport(base: base, token: { nil }))
            let (_, token) = try await c.pair(code: code.trimmingCharacters(in: .whitespaces), name: deviceName())
            Keychain.token = token
            paired = true
        } catch let e as ProtocolError where e.status == 403 {
            problem = "That code is unknown, used or expired. Make a new one with streamrelay-admin pair-code."
        } catch {
            problem = "Couldn't reach the server: \(error.localizedDescription)"
        }
    }

    func unpair() {
        Keychain.token = nil
        paired = false
    }

    private func deviceName() -> String {
        ProcessInfo.processInfo.hostName.isEmpty ? "iPhone" : String(ProcessInfo.processInfo.hostName.prefix(100))
    }

    // MARK: Recording

    func start() {
        problem = nil
        do {
            let m: Manifest
            if let u = unfinished, (try? u.recording().finishRequested) == false {
                m = u                                   // resume: a new epoch in the same broadcast
            } else {
                m = try Manifest.create(in: Self.recordingsFolder, title: title, mode: mode,
                                        holdMaxDelaySeconds: mode == .hold ? holdMaxDelayMinutes * 60 : nil)
            }
            current = m
            unfinished = m
            let origin = ((try? m.folder.resourceValues(forKeys: [.creationDateKey]))?.creationDate) ?? Date()
            let r = SegmentRecorder(manifest: m, origin: origin)
            try r.configure()
            let cond = DeviceConditions(recorder: r, folder: m.folder)
            r.onEvent = { [weak self] e in Task { @MainActor in self?.recorderEvent(e) } }
            cond.onStop = { [weak self] why in
                Task { @MainActor in
                    self?.problem = why == .thermalCritical ? "Stopped: the phone is too hot." : "Stopped: storage is nearly full."
                    self?.stop()
                }
            }
            recorder = r
            conditions = cond
            cond.start()
            r.startEpoch()
            recording = true
            startQueue(m)
        } catch {
            problem = "Couldn't start the camera: \(error)"
        }
    }

    func stop() {
        guard recording, let r = recorder, let m = current else { return }
        recording = false
        conditions?.stop()
        r.endEpoch(.stopped) { [weak self] in
            try? m.requestFinish()
            r.stopSession()
            Task { @MainActor in
                await self?.queue?.nudge()
            }
        }
    }

    private func recorderEvent(_ e: SegmentRecorder.Event) {
        switch e {
        case .epochStarted(let n): epoch = n
        case .segmentStored:
            captured += 1
            conditions?.checkSpace()
            Task { await queue?.nudge() }
        case .epochClosed: break
        case .failed(let why): problem = why
        }
    }

    // MARK: Uploading

    private func startQueue(_ m: Manifest) {
        guard queueTask == nil, let c = client() else { return }
        let q = UploadQueue(manifest: m, client: c, onEvent: { [weak self] e in
            Task { @MainActor in self?.queueEvent(e) }
        })
        queue = q
        queueTask = Task { [weak self] in
            let ending = await q.run()
            await MainActor.run {
                self?.queueTask = nil
                self?.queue = nil
                switch ending {
                case .finished:
                    self?.uploadLine = "Everything uploaded"
                    if self?.unfinished === m { self?.unfinished = nil }
                case .stopped: self?.uploadLine = "Paused"
                case .refused(let status, let why): self?.problem = "The server refused (\(status)): \(why)"
                }
            }
        }
    }

    private func queueEvent(_ e: QueueEvent) {
        switch e {
        case .stored(let k, _): uploadLine = "Uploaded \(k)"
        case .waiting(let s, _): uploadLine = String(format: "Offline, retrying in %.0f s", s)
        case .damaged(let k, let why): problem = "Segment \(k) was rejected: \(why)"
        case .finished: uploadLine = "Finished"
        default: break
        }
    }

    /// Leaving the foreground: the camera stops (iOS), the queue goes to the background session.
    func enteredBackground() {
        guard let m = current else { return }
        Task {
            await queue?.stop()
            handOff?.handOff(m)
        }
    }

    func enteredForeground() {
        handOff?.cancelAll()
        if let m = current { startQueue(m) }
    }

    // MARK: Status, heartbeat

    private func startTicker() {
        ticker?.cancel()
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    private func tick() async {
        guard let m = current else { return }
        if let b = try? m.backlog() { backlog = b }
        hoursLeft = conditions?.hoursRemaining()
        guard let bid = try? m.recording().broadcastID, let c = client() else { return }
        let body = HeartbeatBody(epoch: epoch, capturedThrough: (try? m.lastStoredSequence(epoch: epoch ?? 0)),
                                 queueDepth: backlog.segments, queueBytes: backlog.bytes,
                                 thermal: DeviceConditions.thermalName,
                                 freeBytes: DeviceConditions.freeBytes(at: m.folder))
        _ = try? await c.heartbeat(broadcast: bid, body)
    }
}
#endif
