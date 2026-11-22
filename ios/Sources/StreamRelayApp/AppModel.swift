#if os(iOS)
import Foundation
import Network
import Observation
import StreamRelayCapture
import StreamRelayCore

/// The paired server: its URL and the origin its token is filed under. Readable from any thread.
enum Pairing {
    static var server: URL? {
        UserDefaults.standard.string(forKey: "pairedServer").flatMap(URL.init(string:))
    }

    static func token(for server: URL) -> String? {
        Keychain.token(for: ServerURL.origin(server))
    }

    /// The origin a recording belongs to: the server paired when it was recorded. Its files go there or nowhere.
    static func owner(of m: Manifest) -> String? {
        (try? String(contentsOf: m.folder.appendingPathComponent("server-origin"), encoding: .utf8))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
    }

    static func setOwner(_ m: Manifest, _ server: URL) throws {
        try Data(ServerURL.origin(server).utf8).write(to: m.folder.appendingPathComponent("server-origin"), options: .atomic)
    }

    /// Whether the recording may go to this server. One without an owner file is adopted only while no
    /// server has given it a broadcast yet; once it has one, its owner is unknown and it goes nowhere.
    static func belongs(_ m: Manifest, to server: URL) -> Bool {
        guard let o = owner(of: m) else {
            guard let rec = try? m.recording(), rec.broadcastID == nil else { return false }
            return (try? setOwner(m, server)) != nil
        }
        return o == ServerURL.origin(server)
    }

    static var recordingsFolder: URL {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let f = docs.appendingPathComponent("Recordings", isDirectory: true)
        try? FileManager.default.createDirectory(at: f, withIntermediateDirectories: true)
        return f
    }

    /// The background hand-off for the paired server, shared by the app and the app delegate (which
    /// iOS may wake for finished transfers without any scene).
    @MainActor static var handOff: BackgroundHandOff? = makeHandOff()

    @MainActor static func makeHandOff() -> BackgroundHandOff? {
        guard let server, ServerURL.isAllowed(server) else { return nil }
        let folder = recordingsFolder
        let h = BackgroundHandOff(base: server, token: { Pairing.token(for: server) }) { name in
            // a finished task is only acknowledged in a recording that belongs to this server
            guard let m = try? Manifest(folder: folder.appendingPathComponent(name)),
                  Pairing.owner(of: m) == ServerURL.origin(server) else { return nil }
            return m
        }
        h.resume()
        return h
    }
}

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

    enum Phase: Equatable { case idle, recording, stopping }

    // state shown on screen
    private(set) var paired = Pairing.server.map { Pairing.token(for: $0) != nil } ?? false
    private(set) var phase = Phase.idle
    private(set) var epoch: Int?
    private(set) var captured = 0
    private(set) var backlog = Manifest.Backlog()
    private(set) var uploadLine = "Idle"
    private(set) var hoursLeft: Double?
    private(set) var problem: String?
    /// The recording Start would continue (a new epoch in the same broadcast), if any.
    private(set) var resumable: Manifest?

    var recording: Bool { phase == .recording }
    /// Whether Start would resume: only a recording of the server paired now.
    var canResume: Bool {
        guard let r = resumable, let server = Pairing.server else { return false }
        return Pairing.owner(of: r) == ServerURL.origin(server)
    }

    // machinery
    private(set) var recorder: SegmentRecorder?
    private var conditions: DeviceConditions?
    private var uploads: [Manifest] = []        // recordings still to deliver, oldest first
    private var queue: UploadQueue?
    private var queueManifest: Manifest?
    private var queueTask: Task<Void, Never>?
    private var ticker: Task<Void, Never>?
    private var foreground = true
    private var transition = 0                  // bumped on each background/foreground change
    private var launched = false
    private var current: Manifest?
    private let path = NWPathMonitor()

    // MARK: Launch

    /// After a crash or force-quit: close open epochs, tidy files, queue every recording that hasn't
    /// finished uploading (oldest first), and offer the newest unfinished one for resuming.
    func launch() {
        guard !launched else { return }           // a second scene must not recover a recording in progress
        launched = true
        let folders = (try? FileManager.default.contentsOfDirectory(
            at: Pairing.recordingsFolder, includingPropertiesForKeys: [.creationDateKey])) ?? []
        func created(_ u: URL) -> Date { (try? u.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast }
        for f in folders.sorted(by: { created($0) < created($1) }) {
            guard let m = try? Manifest(folder: f) else { continue }
            try? m.recoverAfterRelaunch()
            guard let r = try? m.recording() else { continue }
            // blocked segments may still go once a reconcile finds their init on the server: not done yet
            let b = try? m.backlog()
            let done = r.finishSent && b != nil && b!.segments == 0 && b!.blocked == 0
            if !done { uploads.append(m) }
            if !r.finishRequested { resumable = m }           // the newest such wins (sorted oldest first)
        }
        current = resumable ?? uploads.last
        path.pathUpdateHandler = { [weak self] p in
            guard p.status == .satisfied else { return }
            Task { @MainActor in await self?.queue?.networkChanged() }   // retry at once
        }
        path.start(queue: DispatchQueue(label: "streamrelay.path"))
        pumpUploads()
        startTicker()
    }

    // MARK: Pairing

    func pair(code: String) async {
        problem = nil
        guard let base = URL(string: serverURL.trimmingCharacters(in: .whitespaces)), ServerURL.isAllowed(base) else {
            problem = "Enter the server address with https:// (plain http only for a Tailscale address)."
            return
        }
        do {
            let c = ProtocolClient(transport: URLSessionTransport(base: base, token: { nil }))
            let (_, token) = try await c.pair(code: code.trimmingCharacters(in: .whitespaces), name: deviceName())
            try Keychain.save(token, for: ServerURL.origin(base))
            await retireUploadsForOldServer(base)
            UserDefaults.standard.set(base.absoluteString, forKey: "pairedServer")
            Pairing.handOff?.invalidate()
            Pairing.handOff = Pairing.makeHandOff()
            paired = true
            pumpUploads()
        } catch let e as ProtocolError where e.status == 403 {
            problem = "That code is unknown, used or expired. Make a new one with streamrelay-admin pair-code."
        } catch let e as Keychain.Failure {
            problem = "Couldn't save the pairing on this iPhone (\(e)). Try again."
        } catch {
            problem = "Couldn't reach the server: \(error.localizedDescription)"
        }
    }

    func unpair() {
        Task {
            await retireUploadsForOldServer(nil)
            if let s = Pairing.server, !Keychain.delete(for: ServerURL.origin(s)) {
                problem = "Couldn't remove the pairing from this iPhone's Keychain. Try again."
                return
            }
            UserDefaults.standard.removeObject(forKey: "pairedServer")
            Pairing.handOff?.invalidate()
            Pairing.handOff = nil
            paired = false
        }
    }

    /// Uploads belong to the server that paired them: stop the queue and the background
    /// transfers before another server's token can come into play.
    private func retireUploadsForOldServer(_ next: URL?) async {
        guard let old = Pairing.server, next.map({ ServerURL.origin($0) }) != ServerURL.origin(old) else { return }
        await queue?.stop()
        Pairing.handOff?.cancelAll()
    }

    private func deviceName() -> String {
        ProcessInfo.processInfo.hostName.isEmpty ? "iPhone" : String(ProcessInfo.processInfo.hostName.prefix(100))
    }

    // MARK: Recording

    func start() {
        guard phase == .idle else { return }        // Stop must finish before another Start
        problem = nil
        do {
            let m: Manifest
            guard let server = Pairing.server else { problem = "Pair this iPhone first."; return }
            if let r = resumable, (try? r.recording().finishRequested) == false, Pairing.belongs(r, to: server) {
                m = r                                   // resume: a new epoch in the same broadcast, same owner
            } else {
                // a recording of another server is never resumed here: it waits for that server
                m = try Manifest.create(in: Pairing.recordingsFolder, title: title, mode: mode,
                                        holdMaxDelaySeconds: mode == .hold ? holdMaxDelayMinutes * 60 : nil)
                try Pairing.setOwner(m, server)
                uploads.append(m)
            }
            current = m
            resumable = m
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
            r.begin()
            phase = .recording
            if queueTask != nil, queueManifest !== m {
                // another recording holds the upload slot: it gives way, and the pump picks this one next
                Task { await queue?.stop() }
            } else {
                pumpUploads()
            }
        } catch {
            problem = "Couldn't start the camera: \(error)"
        }
    }

    func stop() {
        guard phase == .recording, let r = recorder, let m = current else { return }
        phase = .stopping
        conditions?.stop()
        r.finish { [weak self] in
            r.stopSession()
            Task { @MainActor in await self?.saveFinish(m) }
        }
    }

    /// The epoch's close is in the manifest: now the broadcast may finish. Stop is done only once the
    /// finish request is saved, or a relaunch would offer to resume a recording the user stopped.
    private func saveFinish(_ m: Manifest) async {
        let saving = "Couldn't save Stop on this iPhone. Retrying…"
        let reason = problem                        // why it stopped, if anything said so: shown again after the retry
        while (try? m.requestFinish()) == nil {
            if problem == reason || problem == nil { problem = saving }   // never cover a newer error
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        if problem == saving { problem = reason }   // keep the stop reason (or a newer message) until the next Start
        if resumable === m { resumable = nil }
        phase = .idle
        await queue?.nudge()
        pumpUploads()
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
        case .gaveUp(let why):
            problem = why
            stop()
        }
    }

    // MARK: Uploading

    /// Runs one queue at a time over the recordings still to deliver, the one being recorded first.
    /// Called whenever something changes (launch, Start, Stop, foreground, a queue ending), so a
    /// request that arrives while an old queue is still winding down is picked up when it exits.
    private func pumpUploads() {
        guard foreground, queueTask == nil, paired, let server = Pairing.server, ServerURL.isAllowed(server) else { return }
        // only recordings of this server: another server's stay queued until it is paired again
        let mine = uploads.filter { Pairing.belongs($0, to: server) }
        guard let m = (current.flatMap { c in mine.first { $0 === c } }) ?? mine.first else { return }
        let c = ProtocolClient(transport: URLSessionTransport(base: server, token: { Pairing.token(for: server) }))
        let q = UploadQueue(manifest: m, client: c, onEvent: { [weak self] e in
            Task { @MainActor in self?.queueEvent(e) }
        })
        queue = q
        queueManifest = m
        queueTask = Task { [weak self] in
            let ending = await q.run()
            await MainActor.run {
                guard let self else { return }
                self.queueTask = nil
                self.queue = nil
                self.queueManifest = nil
                switch ending {
                case .finished:
                    self.uploadLine = "Everything uploaded"
                    self.uploads.removeAll { $0 === m }
                    self.pumpUploads()                   // the next recording, if any
                case .stopped:
                    self.uploadLine = "Paused"
                    self.pumpUploads()                   // back in the foreground meanwhile? carry on
                case .refused(let status, let why):
                    self.problem = "The server refused (\(status)): \(why)"
                }
            }
        }
    }

    private func queueEvent(_ e: QueueEvent) {
        switch e {
        case .stored(let k, _): uploadLine = "Uploaded \(k)"
        case .waiting(let s, _): uploadLine = String(format: "Offline, retrying in %.0f s", s)
        case .damaged(let k, let why): problem = "Segment \(k) could not be delivered: \(why)"
        case .finished: uploadLine = "Finished"
        default: break
        }
    }

    /// Leaving the foreground: the camera stops (iOS), the queue goes to the background session.
    func enteredBackground() {
        foreground = false
        transition += 1
        let mine = transition
        let pending = uploads.filter { m in Pairing.server.map { Pairing.belongs(m, to: $0) } ?? false }
        Task {
            await queue?.stop()
            // back in the foreground while the queue wound down: its own queue carries on instead
            guard transition == mine, !foreground else { return }
            // pairing may have changed during the await: check ownership again against the server now paired
            guard let server = Pairing.server else { return }
            for m in pending where Pairing.belongs(m, to: server) {
                Pairing.handOff?.handOff(m)                   // a later cancelAll() voids any still being scheduled
            }
        }
    }

    func enteredForeground() {
        foreground = true
        transition += 1
        Pairing.handOff?.cancelAll()
        pumpUploads()
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
        guard let server = Pairing.server, ServerURL.isAllowed(server), Pairing.belongs(m, to: server),
              let bid = try? m.recording().broadcastID else { return }
        let c = ProtocolClient(transport: URLSessionTransport(base: server, token: { Pairing.token(for: server) }))
        let body = HeartbeatBody(epoch: epoch, capturedThrough: (try? m.lastStoredSequence(epoch: epoch ?? 0)),
                                 queueDepth: backlog.segments, queueBytes: backlog.bytes,
                                 thermal: DeviceConditions.thermalName,
                                 freeBytes: DeviceConditions.freeBytes(at: m.folder))
        _ = try? await c.heartbeat(broadcast: bid, body)
    }
}
#endif
