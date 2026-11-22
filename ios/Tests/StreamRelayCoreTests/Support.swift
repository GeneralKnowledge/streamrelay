import Foundation
import XCTest
@testable import StreamRelayCore

/// A virtual clock: sleeping moves time forward at once, so backoff waits cost nothing.
final class VirtualClock: @unchecked Sendable {
    private let lock = NSLock()
    private var t = 1_000_000.0

    var now: Double { lock.lock(); defer { lock.unlock() }; return t }

    func sleep(_ d: Double) async throws {
        try Task.checkCancellation()
        advance(d)
        await Task.yield()
    }

    private func advance(_ d: Double) {
        lock.lock()
        t += max(d, 0.001)
        lock.unlock()
    }
}

/// An in-memory protocol v1 server with switchable faults.
final class FakeServer: HTTPTransport, @unchecked Sendable {
    struct Faults {
        var offline = 0.0          // chance a request gets no answer
        var putsOffline = false    // control calls answer, segment PUTs get no answer
        var busy = 0.0             // chance of 503 with Retry-After 2
        var corrupt = 0.0          // chance a segment's first attempt arrives damaged (422)
        var alwaysReject: Set<String> = []   // "epoch/sequence" always answered 422
        var refuseWith: Int?       // every request answered with this status
        var createReply: (status: Int, retryAfter: String?)?   // answer broadcast creation with this
    }

    private let lock = NSLock()
    var faults = Faults()
    var rng = SplitMix(seed: 7)
    var broadcastID = "0193f0c4-0000-7000-8000-000000000001"
    var created = 0
    var inits: [Int: String] = [:]
    var segments: [SegmentKey: (sha: String, flags: String?, durationUs: Int64)] = [:]
    var epochEnds: [Int: Int] = [:]
    var finished: Int?
    var log: [String] = []          // "PUT 0/3", "END 0", ...
    var corrupted: Set<SegmentKey> = []

    func locked<T>(_ f: () throws -> T) rethrows -> T { lock.lock(); defer { lock.unlock() }; return try f() }

    func send(_ r: HTTPRequest) async throws -> HTTPReply {
        try locked { try handle(r) }
    }

    private func json(_ status: Int, _ obj: [String: Any]) -> HTTPReply {
        HTTPReply(status: status, headers: ["content-type": "application/json"],
                  body: try! JSONSerialization.data(withJSONObject: obj))
    }

    private func handle(_ r: HTTPRequest) throws -> HTTPReply {
        if rng.next() < faults.offline { throw URLError(.networkConnectionLost) }
        if faults.putsOffline, r.method == "PUT", r.path.contains("/segments/") { throw URLError(.notConnectedToInternet) }
        if let s = faults.refuseWith { return json(s, ["error": "refused"]) }
        if r.method == "PUT", rng.next() < faults.busy { return HTTPReply(status: 503, headers: ["retry-after": "2"]) }
        let parts = r.path.split(separator: "/").map(String.init)
        switch (r.method, parts.count) {
        case ("POST", 2) where parts == ["v1", "broadcasts"]:
            if let c = faults.createReply {
                return HTTPReply(status: c.status, headers: c.retryAfter.map { ["retry-after": $0] } ?? [:],
                                 body: Data(#"{"error": "create refused"}"#.utf8))
            }
            created += 1
            return json(created == 1 ? 201 : 200, ["broadcast_id": broadcastID])
        case ("GET", 4) where parts[3] == "status":
            let eps = Set(segments.keys.map(\.epoch)).union(epochEnds.keys).sorted()
            let list: [[String: Any]] = eps.map { e in
                let seqs = segments.keys.filter { $0.epoch == e }.map(\.sequence).sorted()
                var through = -1
                for s in seqs { if s != through + 1 { break }; through = s }
                let top = epochEnds[e] ?? (seqs.last ?? -1)
                let missing = (max(through + 1, 0)...max(top, through + 1)).filter { $0 <= top && !seqs.contains($0) }
                return ["epoch": e, "received_through": through, "missing": missing, "ended": epochEnds[e] != nil]
            }
            return json(200, ["state": "live", "epochs": list, "inits": inits.keys.sorted()])
        case ("PUT", 5) where parts[3] == "inits":
            let id = Int(parts[4])!
            let sha = SHA256.hex(r.body ?? Data())
            guard sha == r.headers["X-SR-SHA256"] else { return json(422, ["error": "sha256 mismatch"]) }
            if let have = inits[id] { return json(have == sha ? 200 : 409, [:]) }
            inits[id] = sha
            log.append("INIT \(id)")
            return json(201, [:])
        case ("PUT", 6) where parts[3] == "segments":
            let key = SegmentKey(epoch: Int(parts[4])!, sequence: Int(parts[5])!)
            if faults.alwaysReject.contains(key.description) { return json(422, ["error": "bad media"]) }
            var body = r.body ?? Data()
            if !corrupted.contains(key), rng.next() < faults.corrupt, !body.isEmpty {
                corrupted.insert(key)
                body[body.count / 2] ^= 0xff
            }
            let sha = SHA256.hex(body)
            guard sha == r.headers["X-SR-SHA256"] else { return json(422, ["error": "sha256 mismatch"]) }
            if let have = segments[key] { return json(have.sha == sha ? 200 : 409, [:]) }
            // the real server's duration rule: 0.5-4 s, or any positive length for the final segment
            let dur = Int64(r.headers["X-SR-Duration-Us"] ?? "") ?? 0
            let final = (r.headers["X-SR-Flags"] ?? "").split(separator: ",").contains("final")
            guard final ? dur > 0 : (500_000...4_000_000).contains(dur) else { return json(422, ["error": "duration_us out of range"]) }
            guard inits[Int(r.headers["X-SR-Init"]!)!] != nil else { return json(412, ["error": "init missing"]) }
            if let last = epochEnds[key.epoch], key.sequence > last { return json(422, ["error": "after epoch end"]) }
            segments[key] = (sha, r.headers["X-SR-Flags"], Int64(r.headers["X-SR-Duration-Us"]!)!)
            log.append("PUT \(key)")
            return json(201, [:])
        case ("POST", 6) where parts[5] == "end":
            let e = Int(parts[4])!
            let body = try JSONSerialization.jsonObject(with: r.body!) as! [String: Any]
            let last = body["last_sequence"] as! Int
            if let have = epochEnds[e] { return json(have == last ? 200 : 409, [:]) }
            epochEnds[e] = last
            log.append("END \(e)")
            return json(200, [:])
        case ("POST", 4) where parts[3] == "finish":
            let body = try JSONSerialization.jsonObject(with: r.body!) as! [String: Any]
            let last = body["last_epoch"] as! Int
            guard (0...last).allSatisfy({ epochEnds[$0] != nil }) else { return json(409, ["error": "open epochs"]) }
            finished = last
            log.append("FINISH \(last)")
            return json(200, ["state": "finishing"])
        default:
            return json(404, ["error": "no route \(r.method) \(r.path)"])
        }
    }
}

/// Deterministic random numbers for the fakes.
struct SplitMix {
    var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> Double {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        z ^= z >> 31
        return Double(z >> 11) / Double(1 << 53)
    }
}

func tempDir() -> URL {
    let u = FileManager.default.temporaryDirectory.appendingPathComponent("sr-\(UUID().uuidString)", isDirectory: true)
    try! FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
    return u
}

/// A manifest with an init and `count` 2 s segments in epoch 0 (closed when `close`).
func makeRecording(mode: CatchUpMode = .catchUp, count: Int, close: Bool = true) throws -> Manifest {
    let m = try Manifest.create(in: tempDir(), title: "test", mode: mode)
    try m.addInit(id: 0, data: Data("init".utf8))
    try m.openEpoch(0)
    for i in 0..<count {
        try m.addSegment(SegmentKey(epoch: 0, sequence: i), initID: 0, startMicros: Int64(i) * 2_000_000,
                         durationMicros: 2_000_000, data: Data("segment \(i) ".utf8) + Data(repeating: UInt8(i % 251), count: 64))
    }
    if close {
        try m.closeEpoch(0, lastSequence: count - 1, reason: .stopped)
        try m.requestFinish()
    }
    return m
}

func queue(_ m: Manifest, _ server: FakeServer, _ clock: VirtualClock, concurrency: Int = 3,
           events: (@Sendable (QueueEvent) -> Void)? = nil) -> UploadQueue {
    var o = UploadQueue.Options()
    o.concurrency = concurrency
    let rng = Locked(SplitMix(seed: 3))
    return UploadQueue(manifest: m, client: ProtocolClient(transport: server), options: o,
                       onEvent: events ?? { _ in }, sleep: { try await clock.sleep($0) }, now: { clock.now },
                       random: { rng.with { $0.next() } })
}

final class Locked<T>: @unchecked Sendable {
    private var value: T
    private let lock = NSLock()
    init(_ v: T) { value = v }
    func with<R>(_ f: (inout T) -> R) -> R { lock.lock(); defer { lock.unlock() }; return f(&value) }
}

/// Waits (real time, bounded) for a condition while the queue runs; fails instead of hanging.
func eventually(_ what: String, timeout: Double = 10, file: StaticString = #filePath, line: UInt = #line,
                _ cond: () -> Bool) async {
    let until = Date().addingTimeInterval(timeout)
    while !cond() {
        if Date() > until {
            XCTFail("timed out waiting for \(what)", file: file, line: line)
            return
        }
        await Task.yield()
    }
}
