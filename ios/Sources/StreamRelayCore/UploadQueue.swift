import Foundation

/// What the queue tells the screen.
public enum QueueEvent: Equatable, Sendable {
    case broadcastCreated(String)
    case reconciled
    case stored(SegmentKey, status: Int)
    case damaged(SegmentKey, reason: String)
    case waiting(seconds: Double, reason: String)
    case epochEnded(Int)
    case finished
    case stopped(status: Int, reason: String)
}

/// Uploads one recording's segments over protocol v1 until the broadcast is
/// finished and every segment is acknowledged (or given up as damaged).
///
/// It reads everything from the manifest and writes every result back to it,
/// so it can be stopped at any point (app killed, phone off) and a new queue
/// on the same manifest carries on after reconciling with `GET status`.
///
/// Client rules (docs/protocol/upload-v1.md):
/// - order: catch_up and hold oldest first; skip_to_live the live edge first,
///   then backfill oldest first at `backfillPerSecond`
/// - up to `concurrency` PUTs in flight, dropping to 1 after a failure until
///   `recoverAfter` successes in a row
/// - backoff 1-30 s with full jitter, reset on success; Retry-After honoured
/// - 412: upload the init, then the segment again; 422: once more, then damaged;
///   401/403/404/409/410: stop and report
/// - a segment shorter than `shortSegment` in a still-open epoch waits until the
///   epoch is closed, so the last one of a run goes out with its final flag
public actor UploadQueue {
    public struct Options: Sendable {
        public var concurrency = 3
        public var backfillPerSecond = 1.0
        public var shortSegmentMicros: Int64 = 1_750_000
        public var recoverAfter = 5
        public var idleWait = 0.25
        public var round = 10.0
        public init() {}
    }

    public let manifest: Manifest
    public let client: ProtocolClient
    public let options: Options
    private let onEvent: @Sendable (QueueEvent) -> Void
    private let sleep: @Sendable (Double) async throws -> Void
    private let now: @Sendable () -> Double
    private let random: @Sendable () -> Double

    private var backoff = Backoff()
    private var limit: Int
    private var successesInRow = 0
    private var rejections: [SegmentKey: Int] = [:]
    private var tokens = 0.0
    private var tokensAt: Double
    private var pausedUntil = 0.0
    private var nudged = false
    private var stopRequested = false

    public init(manifest: Manifest, client: ProtocolClient, options: Options = Options(),
                onEvent: @escaping @Sendable (QueueEvent) -> Void = { _ in },
                sleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(nanoseconds: UInt64(max(0, $0) * 1e9)) },
                now: @escaping @Sendable () -> Double = { ProcessInfo.processInfo.systemUptime },   // monotonic
                random: @escaping @Sendable () -> Double = { Double.random(in: 0...1) }) {
        self.manifest = manifest
        self.client = client
        self.options = options
        self.onEvent = onEvent
        self.sleep = sleep
        self.now = now
        self.random = random
        self.limit = options.concurrency
        self.tokensAt = now()
    }

    /// A new segment was recorded (or Stop pressed): stop idling. Does not cut a backoff short.
    public func nudge() {
        nudged = true
    }

    /// The network path changed (NWPathMonitor): retry at once (protocol v1 client rules).
    public func networkChanged() {
        nudged = true
        pausedUntil = 0
        backoff.succeed()
    }

    /// Stop after the requests in flight (the app is going to the background).
    public func stop() {
        stopRequested = true
    }

    public enum Ending: Equatable, Sendable {
        case finished                     // finish sent and nothing pending
        case stopped                      // stop() was called
        case refused(Int, String)         // the server refused for good (revoked, deleted, conflict)
    }

    /// Runs until the recording is fully delivered, `stop()` is called, or the server refuses.
    public func run() async -> Ending {
        var reconciled = false
        while !stopRequested {
            do {
                let bid = try await ensureBroadcast()
                if !reconciled {
                    try manifest.reconcile(with: try await client.status(broadcast: bid))
                    reconciled = true
                    onEvent(.reconciled)
                }
                try await uploadInits(bid)
                try await sendEpochEnds(bid)
                try await uploadSegments(bid)
                try await sendEpochEnds(bid)
                if try await finishIfDone(bid) {
                    onEvent(.finished)
                    return .finished
                }
                try await idle()
            } catch let e as Refusal {
                onEvent(.stopped(status: e.status, reason: e.reason))
                return .refused(e.status, e.reason)
            } catch let e as ProtocolError where [401, 403, 404, 409, 410].contains(e.status) {
                onEvent(.stopped(status: e.status, reason: e.message))
                return .refused(e.status, e.message)
            } catch is CancellationError {
                return .stopped
            } catch {
                // no answer, a 429/5xx/507 on a control call, or a local read error: wait and try again
                let wait = backoff.fail(retryAfter: (error as? ProtocolError)?.retryAfter, random: random())
                onEvent(.waiting(seconds: wait, reason: String(describing: error)))
                try? await sleep(wait)
            }
        }
        return .stopped
    }

    struct Refusal: Error {
        var status: Int
        var reason: String
    }

    // MARK: Steps

    /// Throws CancellationError once stop() was called, so no new request starts.
    private func checkStop() throws {
        if stopRequested { throw CancellationError() }
    }

    private func ensureBroadcast() async throws -> String {
        let r = try manifest.recording()
        if let id = r.broadcastID { return id }
        try checkStop()
        let id: String
        do {
            id = try await client.createBroadcast(title: r.title, clientRef: r.clientRef, mode: r.mode,
                                                  holdMaxDelaySeconds: r.holdMaxDelaySeconds)
        } catch let e as ProtocolError where (400..<500).contains(e.status) && ![408, 429].contains(e.status) {
            // the recording's own settings were refused: retrying the same request can't help
            throw Refusal(status: e.status, reason: "create broadcast: \(e.message)")
        }
        try manifest.setBroadcastID(id)
        onEvent(.broadcastCreated(id))
        return id
    }

    private func uploadInits(_ bid: String) async throws {
        for i in try manifest.inits() where !i.uploaded && !i.lost {
            try checkStop()
            try await uploadInit(bid, i.id)
        }
    }

    private func uploadInit(_ bid: String, _ id: Int) async throws {
        guard let i = try manifest.inits().first(where: { $0.id == id }) else {
            throw Refusal(status: 412, reason: "init \(id) is not on the phone")
        }
        let data: Data
        do {
            data = try Data(contentsOf: manifest.url(of: i.file))
        } catch let e as CocoaError where e.code == .fileReadNoSuchFile {
            // the init is gone from the phone: its epoch's segments can't be played; other epochs go on
            for key in try manifest.markInitLost(id) { onEvent(.damaged(key, reason: "init \(id) missing on the phone")) }
            return
        }
        let reply = try await client.putInit(broadcast: bid, initID: id, data: data, sha256: i.sha256)
        switch reply.status {
        case 200, 201: try manifest.markInitUploaded(id)
        case 401, 403, 404, 409, 410, 422: throw Refusal(status: reply.status, reason: "init \(id): \(reply.errorText)")
        default: throw ProtocolError(status: reply.status, message: reply.errorText, retryAfter: reply.retryAfter)
        }
    }

    /// The next segments to send, in protocol order, at most `n`, skipping `busy`.
    func pick(_ n: Int, busy: Set<SegmentKey>) throws -> [SegmentRecord] {
        guard n > 0 else { return [] }
        let open = Set(try manifest.epochs().filter { $0.lastSequence == nil }.map(\.epoch))
        func ready(_ s: SegmentRecord) -> Bool {
            !busy.contains(s.key) && !(open.contains(s.key.epoch) && s.durationMicros < options.shortSegmentMicros)
        }
        let pending = try manifest.pending(limit: n + busy.count + 8).filter(ready)
        guard try manifest.recording().mode == .skipToLive else { return Array(pending.prefix(n)) }

        // skip_to_live: the live edge first, then backfill oldest first, rate-limited.
        var out: [SegmentRecord] = []
        // the newest ready segment within the live window (a short tail of an open epoch waits)
        if let live = try manifest.newestPending(limit: 3).first(where: { ready($0) }), try isLiveEdge(live) {
            out.append(live)
        }
        let t = now()
        tokens = min(2, tokens + (t - tokensAt) * options.backfillPerSecond)
        tokensAt = t
        for s in pending where out.count < n && !out.contains(where: { $0.key == s.key }) {
            guard tokens >= 1 else { break }
            tokens -= 1
            out.append(s)
        }
        return out
    }

    /// The newest pending segment is "live" if nothing newer has been recorded after it.
    private func isLiveEdge(_ s: SegmentRecord) throws -> Bool {
        let last = try manifest.lastStoredSequence(epoch: s.key.epoch)
        let newestEpoch = try manifest.nextEpoch() - 1
        return s.key.epoch == newestEpoch && s.key.sequence >= last - 1
    }

    private func uploadSegments(_ bid: String) async throws {
        try await withThrowingTaskGroup(of: (SegmentRecord, Result<HTTPReply, Error>).self) { group in
            var busy = Set<SegmentKey>()
            // A round lasts at most `round` seconds so epoch ends and finish go out while a long backlog drains.
            let roundEnds = now() + options.round
            func fill() throws {
                guard !stopRequested, now() >= pausedUntil, now() < roundEnds else { return }
                for s in try pick(limit - busy.count, busy: busy) {
                    busy.insert(s.key)
                    let client = client, url = manifest.url(of: s.file)
                    group.addTask {
                        do {
                            let data = try Data(contentsOf: url)
                            return (s, .success(try await client.putSegment(broadcast: bid, segment: s, data: data)))
                        } catch {
                            return (s, .failure(error))
                        }
                    }
                }
            }
            try fill()
            while let (s, result) = try await group.next() {
                busy.remove(s.key)
                try await handle(bid, s, result)
                try fill()
            }
        }
    }

    private func handle(_ bid: String, _ s: SegmentRecord, _ result: Result<HTTPReply, Error>) async throws {
        try manifest.countAttempt(s.key)
        let outcome: PutOutcome
        switch result {
        case .success(let reply): outcome = PutOutcome.classify(reply)
        case .failure(let error as CocoaError) where error.code == .fileReadNoSuchFile:
            try manifest.markDamaged(s.key)
            onEvent(.damaged(s.key, reason: "file missing on the phone"))
            return
        case .failure: outcome = .retry(after: nil)
        }
        switch outcome {
        case .stored:
            try manifest.markAcknowledged(s.key)
            rejections[s.key] = nil
            backoff.succeed()
            successesInRow += 1
            if successesInRow >= options.recoverAfter { limit = options.concurrency }
            if case .success(let r) = result { onEvent(.stored(s.key, status: r.status)) }
        case .needInit:
            if !stopRequested { try await uploadInit(bid, s.initID) }
        case .rejected(let reason):
            let n = (rejections[s.key] ?? 0) + 1
            rejections[s.key] = n
            if n >= 2 {
                try manifest.markDamaged(s.key)
                rejections[s.key] = nil
                onEvent(.damaged(s.key, reason: reason))
            }
        case .retry(let after):
            successesInRow = 0
            limit = 1
            let wait = backoff.fail(retryAfter: after, random: random())
            pausedUntil = max(pausedUntil, now() + wait)
            onEvent(.waiting(seconds: wait, reason: "segment \(s.key)"))
        case .stop(let status, let reason):
            throw Refusal(status: status, reason: "segment \(s.key): \(reason)")
        }
    }

    private func sendEpochEnds(_ bid: String) async throws {
        for e in try manifest.epochs() where !e.endSent {
            guard let last = e.lastSequence, let reason = e.endReason else { continue }
            try checkStop()
            let r = try await client.endEpoch(broadcast: bid, epoch: e.epoch, lastSequence: last, reason: reason)
            switch r.status {
            case 200, 201:
                try manifest.markEpochEndSent(e.epoch)
                onEvent(.epochEnded(e.epoch))
            case 401, 403, 404, 409, 410, 422: throw Refusal(status: r.status, reason: "epoch \(e.epoch) end: \(r.errorText)")
            default: throw ProtocolError(status: r.status, message: r.errorText, retryAfter: r.retryAfter)
            }
        }
    }

    /// Sends finish once Stop was pressed and every epoch has its end call;
    /// done when finish is sent and nothing is pending.
    private func finishIfDone(_ bid: String) async throws -> Bool {
        let rec = try manifest.recording()
        guard rec.finishRequested else { return false }
        let epochs = try manifest.epochs()
        guard epochs.allSatisfy(\.endSent) else { return false }
        if !rec.finishSent {
            let lastEpoch = epochs.map(\.epoch).max() ?? -1
            if lastEpoch >= 0 {
                try checkStop()
                let r = try await client.finish(broadcast: bid, lastEpoch: lastEpoch)
                switch r.status {
                case 200, 201: break
                case 401, 403, 404, 409, 410, 422: throw Refusal(status: r.status, reason: "finish: \(r.errorText)")
                default: throw ProtocolError(status: r.status, message: r.errorText, retryAfter: r.retryAfter)
                }
            }
            try manifest.markFinishSent()
        }
        return try manifest.backlog().segments == 0
    }

    private func idle() async throws {
        let until = max(now() + options.idleWait, pausedUntil)
        // a nudge ends an ordinary idle, but not a backoff: new segments mustn't spin the loop while paused
        while !stopRequested && now() < until && (!nudged || now() < pausedUntil) {
            try await sleep(min(options.idleWait, until - now()))
        }
        nudged = false
    }
}
