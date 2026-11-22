import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// Protocol v1 client (docs/protocol/upload-v1.md). Requests are plain HTTPS;
// the transport is a protocol so the queue tests run against a fake server.

public struct HTTPReply: Sendable {
    public var status: Int
    public var headers: [String: String]   // lower-cased names
    public var body: Data

    public init(status: Int, headers: [String: String] = [:], body: Data = Data()) {
        self.status = status
        self.headers = headers
        self.body = body
    }

    /// Retry-After in seconds, when present, finite and not negative.
    public var retryAfter: Double? {
        guard let v = headers["retry-after"].flatMap(Double.init), v.isFinite, v >= 0 else { return nil }
        return v
    }
    public var errorText: String {
        (try? JSONDecoder().decode([String: String].self, from: body))?["error"]
            ?? (try? JSONDecoder().decode([String: String].self, from: body))?["detail"]
            ?? String(decoding: body.prefix(200), as: UTF8.self)
    }
}

public struct HTTPRequest: Sendable {
    public var method: String
    public var path: String
    public var headers: [String: String]
    public var body: Data?
}

public protocol HTTPTransport: Sendable {
    /// Sends one request. Throws only when no HTTP answer arrived (offline, reset, timeout).
    func send(_ request: HTTPRequest) async throws -> HTTPReply
}

/// Where credentials may be sent: https anywhere; plain http only to this device
/// (loopback) or a Tailscale address (100.64.0.0/10, encrypted by WireGuard),
/// for development servers on the tailnet.
public enum ServerURL {
    public static func isAllowed(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), let host = url.host?.lowercased(), !host.isEmpty else { return false }
        if scheme == "https" { return true }
        guard scheme == "http" else { return false }
        if host == "localhost" || host == "::1" || host == "[::1]" { return true }
        let parts = host.split(separator: ".").compactMap { UInt8($0) }
        guard parts.count == 4, host.split(separator: ".").count == 4 else { return false }
        return parts[0] == 127 || (parts[0] == 100 && (64...127).contains(parts[1]))
    }

    /// scheme://host:port, the identity a token belongs to.
    public static func origin(_ url: URL) -> String {
        let scheme = url.scheme?.lowercased() ?? ""
        let port = url.port ?? (scheme == "https" ? 443 : 80)
        return "\(scheme)://\(url.host?.lowercased() ?? ""):\(port)"
    }
}

public struct InsecureServer: Error, CustomStringConvertible {
    public var url: URL
    public var description: String { "\(url.absoluteString): use https (plain http only for loopback or a Tailscale address)" }
}

/// URLSession transport. `token` is read on every request; the caller binds it to
/// this transport's server, so a token is never sent to a server it wasn't issued by.
public final class URLSessionTransport: HTTPTransport, @unchecked Sendable {
    public let base: URL
    private let session: URLSession
    private let token: @Sendable () -> String?

    public init(base: URL, session: URLSession = .shared, token: @escaping @Sendable () -> String?) {
        self.base = base
        self.session = session
        self.token = token
    }

    public func send(_ r: HTTPRequest) async throws -> HTTPReply {
        guard ServerURL.isAllowed(base) else { throw InsecureServer(url: base) }
        var req = URLRequest(url: base.appendingPathComponent(r.path))
        req.httpMethod = r.method
        req.timeoutInterval = 30
        for (k, v) in r.headers { req.setValue(v, forHTTPHeaderField: k) }
        if let t = token() { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
        let (data, response): (Data, URLResponse)
        if let body = r.body {
            (data, response) = try await session.upload(for: req, from: body)
        } else {
            (data, response) = try await session.data(for: req)
        }
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        var headers: [String: String] = [:]
        for (k, v) in http.allHeaderFields {
            if let k = k as? String, let v = v as? String { headers[k.lowercased()] = v }
        }
        return HTTPReply(status: http.statusCode, headers: headers, body: data)
    }
}

/// `GET /v1/broadcasts/{id}/status`.
public struct BroadcastStatus: Decodable, Equatable, Sendable {
    public struct Epoch: Decodable, Equatable, Sendable {
        public var epoch: Int
        public var receivedThrough: Int
        public var missing: [Int]
        public var ended: Bool
        public var missingTruncated: Bool

        public init(epoch: Int, receivedThrough: Int, missing: [Int] = [], ended: Bool, missingTruncated: Bool = false) {
            self.epoch = epoch
            self.receivedThrough = receivedThrough
            self.missing = missing
            self.ended = ended
            self.missingTruncated = missingTruncated
        }

        enum CodingKeys: String, CodingKey {
            case epoch, missing, ended
            case receivedThrough = "received_through"
            case missingTruncated = "missing_truncated"
        }

        public init(from d: Decoder) throws {
            let c = try d.container(keyedBy: CodingKeys.self)
            epoch = try c.decode(Int.self, forKey: .epoch)
            receivedThrough = try c.decode(Int.self, forKey: .receivedThrough)
            missing = try c.decodeIfPresent([Int].self, forKey: .missing) ?? []
            ended = try c.decodeIfPresent(Bool.self, forKey: .ended) ?? false
            missingTruncated = try c.decodeIfPresent(Bool.self, forKey: .missingTruncated) ?? false
        }

        /// Whether the server holds a sequence: true, false, or nil when the answer can't tell.
        ///
        /// The server lists holes between `received_through` and its top (the
        /// epoch's last sequence once ended, else its highest stored one), but
        /// does not send that top. So below the highest hole a sequence not
        /// listed is stored; in an ended epoch every sequence not listed is
        /// stored (the phone and server agree on its last sequence); above
        /// that the answer is unknown.
        public func holds(_ seq: Int) -> Bool? {
            if seq <= receivedThrough { return true }
            if missing.contains(seq) { return false }
            if missingTruncated { return nil }
            if let top = missing.max(), seq < top { return true }
            if ended { return true }
            return nil
        }
    }

    public var state: String
    public var epochs: [Epoch]
    public var inits: [Int]

    public init(state: String, epochs: [Epoch], inits: [Int]) {
        self.state = state
        self.epochs = epochs
        self.inits = inits
    }
}

public struct HeartbeatBody: Encodable, Sendable {
    public var epoch: Int?
    public var capturedThrough: Int?
    public var queueDepth: Int?
    public var queueBytes: Int64?
    public var battery: Double?
    public var thermal: String?
    public var freeBytes: Int64?
    public var network: String?

    public init(epoch: Int? = nil, capturedThrough: Int? = nil, queueDepth: Int? = nil, queueBytes: Int64? = nil,
                battery: Double? = nil, thermal: String? = nil, freeBytes: Int64? = nil, network: String? = nil) {
        self.epoch = epoch
        self.capturedThrough = capturedThrough
        self.queueDepth = queueDepth
        self.queueBytes = queueBytes
        self.battery = battery
        self.thermal = thermal
        self.freeBytes = freeBytes
        self.network = network
    }

    enum CodingKeys: String, CodingKey {
        case epoch, battery, thermal, network
        case capturedThrough = "captured_through"
        case queueDepth = "queue_depth"
        case queueBytes = "queue_bytes"
        case freeBytes = "free_bytes"
    }
}

/// What the queue should do with a PUT's answer (protocol v1, client rules).
public enum PutOutcome: Equatable, Sendable {
    case stored                     // 201 or 200: acknowledged
    case needInit                   // 412: upload the init, then this again
    case rejected(String)           // 422: retry once, then mark damaged
    case retry(after: Double?)      // 429/503/507 (Retry-After), other 5xx, no answer
    case stop(Int, String)          // 401/403/404/409/410: stop and tell the person

    public static func classify(_ reply: HTTPReply) -> PutOutcome {
        switch reply.status {
        case 200, 201: return .stored
        case 412: return .needInit
        case 422: return .rejected(reply.errorText)
        case 429, 503, 507: return .retry(after: reply.retryAfter)
        case 401, 403, 404, 409, 410: return .stop(reply.status, reply.errorText)
        default: return .retry(after: nil)
        }
    }
}

public struct ProtocolError: Error, Equatable, CustomStringConvertible {
    public var status: Int
    public var message: String
    public var retryAfter: Double?        // the server's Retry-After, when it sent one

    public init(status: Int, message: String, retryAfter: Double? = nil) {
        self.status = status
        self.message = message
        self.retryAfter = retryAfter
    }

    public var description: String { "HTTP \(status): \(message)" }
}

public struct ProtocolClient: Sendable {
    public let transport: HTTPTransport

    public init(transport: HTTPTransport) {
        self.transport = transport
    }

    private static let json = ["Content-Type": "application/json"]

    private func post<T: Encodable>(_ path: String, _ body: T) async throws -> HTTPReply {
        let data = try JSONEncoder().encode(body)
        return try await transport.send(HTTPRequest(method: "POST", path: path, headers: Self.json, body: data))
    }

    private func expect(_ reply: HTTPReply, _ ok: Set<Int>) throws -> HTTPReply {
        guard ok.contains(reply.status) else {
            throw ProtocolError(status: reply.status, message: reply.errorText, retryAfter: reply.retryAfter)
        }
        return reply
    }

    /// `POST /v1/devices/pair`: trades a one-time code for a device token.
    public func pair(code: String, name: String) async throws -> (deviceID: String, token: String) {
        struct Body: Encodable { var code, name, platform: String }
        struct Answer: Decodable { var device_id, token: String }
        let r = try expect(try await post("v1/devices/pair", Body(code: code, name: name, platform: "ios")), [201])
        let a = try JSONDecoder().decode(Answer.self, from: r.body)
        return (a.device_id, a.token)
    }

    /// `POST /v1/broadcasts`, idempotent on `clientRef`.
    public func createBroadcast(title: String, clientRef: UUID, mode: CatchUpMode, holdMaxDelaySeconds: Int?) async throws -> String {
        struct CatchUp: Encodable { var mode: String; var max_delay_s: Int? }
        struct Body: Encodable { var title: String; var client_broadcast_ref: String; var catch_up: CatchUp }
        struct Answer: Decodable { var broadcast_id: String }
        let body = Body(title: title, client_broadcast_ref: clientRef.uuidString.lowercased(),
                        catch_up: CatchUp(mode: mode.rawValue, max_delay_s: mode == .hold ? holdMaxDelaySeconds : nil))
        let r = try expect(try await post("v1/broadcasts", body), [200, 201])
        return try JSONDecoder().decode(Answer.self, from: r.body).broadcast_id
    }

    public func putInit(broadcast: String, initID: Int, data: Data, sha256: String) async throws -> HTTPReply {
        try await transport.send(HTTPRequest(method: "PUT", path: "v1/broadcasts/\(broadcast)/inits/\(initID)",
                                             headers: ["X-SR-SHA256": sha256, "Content-Type": "video/mp4"], body: data))
    }

    public static func segmentHeaders(_ s: SegmentRecord) -> [String: String] {
        var h = ["X-SR-SHA256": s.sha256, "X-SR-Init": String(s.initID), "X-SR-Start-Us": String(s.startMicros),
                 "X-SR-Duration-Us": String(s.durationMicros), "Content-Type": "video/mp4"]
        if s.isFinal { h["X-SR-Flags"] = "final" }
        return h
    }

    public func putSegment(broadcast: String, segment s: SegmentRecord, data: Data) async throws -> HTTPReply {
        try await transport.send(HTTPRequest(method: "PUT",
                                             path: "v1/broadcasts/\(broadcast)/segments/\(s.key.epoch)/\(s.key.sequence)",
                                             headers: Self.segmentHeaders(s), body: data))
    }

    public func endEpoch(broadcast: String, epoch: Int, lastSequence: Int, reason: EpochEndReason) async throws -> HTTPReply {
        struct Body: Encodable { var last_sequence: Int; var reason: String }
        return try await post("v1/broadcasts/\(broadcast)/epochs/\(epoch)/end", Body(last_sequence: lastSequence, reason: reason.rawValue))
    }

    public func finish(broadcast: String, lastEpoch: Int) async throws -> HTTPReply {
        struct Body: Encodable { var last_epoch: Int }
        return try await post("v1/broadcasts/\(broadcast)/finish", Body(last_epoch: lastEpoch))
    }

    public func status(broadcast: String) async throws -> BroadcastStatus {
        let r = try expect(try await transport.send(HTTPRequest(method: "GET", path: "v1/broadcasts/\(broadcast)/status",
                                                                 headers: [:], body: nil)), [200])
        return try JSONDecoder().decode(BroadcastStatus.self, from: r.body)
    }

    public func heartbeat(broadcast: String, _ body: HeartbeatBody) async throws -> HTTPReply {
        try await post("v1/broadcasts/\(broadcast)/heartbeat", body)
    }
}
