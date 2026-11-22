#if os(iOS)
import Foundation
import StreamRelayCore

/// When the app leaves the foreground, the remaining queue goes to a
/// background URLSession (protocol v1, client rules): iOS keeps uploading the
/// files while the app is suspended. Each task carries its segment key in
/// `taskDescription`; an answer of 200/201 acknowledges it in the manifest.
/// Anything else is left pending, and on the next launch the foreground queue
/// reconciles with `GET status` and carries on, so a lost answer costs one
/// duplicate PUT (answered 200).
public final class BackgroundHandOff: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    public static let identifier = "org.forgemobile.streamrelay.upload"

    private let manifestFor: @Sendable (String) -> Manifest?     // recording folder name -> manifest
    private let base: URL
    private let token: @Sendable () -> String?
    private lazy var session: URLSession = {
        let c = URLSessionConfiguration.background(withIdentifier: Self.identifier)
        c.isDiscretionary = false
        c.sessionSendsLaunchEvents = true
        c.allowsCellularAccess = true
        return URLSession(configuration: c, delegate: self, delegateQueue: nil)
    }()
    public var finishedEvents: (() -> Void)?

    public init(base: URL, token: @escaping @Sendable () -> String?, manifestFor: @escaping @Sendable (String) -> Manifest?) {
        self.base = base
        self.token = token
        self.manifestFor = manifestFor
        super.init()
    }

    /// Reattach after a relaunch so finished tasks are delivered.
    public func resume() {
        _ = session
    }

    /// Hands every pending segment of the recording (and its inits) to iOS.
    public func handOff(_ manifest: Manifest, limit: Int = 300) {
        guard let bid = try? manifest.recording().broadcastID, let t = token() else { return }
        let folder = manifest.folder.lastPathComponent
        session.getAllTasks { [self] running in
            let already = Set(running.compactMap(\.taskDescription))
            for i in (try? manifest.inits()) ?? [] where !i.uploaded {
                let desc = "\(folder)|init|\(i.id)"
                guard !already.contains(desc) else { continue }
                var req = URLRequest(url: base.appendingPathComponent("v1/broadcasts/\(bid)/inits/\(i.id)"))
                req.httpMethod = "PUT"
                req.setValue(i.sha256, forHTTPHeaderField: "X-SR-SHA256")
                req.setValue("video/mp4", forHTTPHeaderField: "Content-Type")
                req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization")
                let task = session.uploadTask(with: req, fromFile: manifest.url(of: i.file))
                task.taskDescription = desc
                task.resume()
            }
            for s in (try? manifest.pending(limit: limit)) ?? [] {
                let desc = "\(folder)|seg|\(s.key.epoch)|\(s.key.sequence)"
                guard !already.contains(desc) else { continue }
                var req = URLRequest(url: base.appendingPathComponent(
                    "v1/broadcasts/\(bid)/segments/\(s.key.epoch)/\(s.key.sequence)"))
                req.httpMethod = "PUT"
                for (k, v) in ProtocolClient.segmentHeaders(s) { req.setValue(v, forHTTPHeaderField: k) }
                req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization")
                let task = session.uploadTask(with: req, fromFile: manifest.url(of: s.file))
                task.taskDescription = desc
                task.resume()
            }
        }
    }

    /// Cancels the hand-off when the app is back in the foreground and its own queue takes over.
    public func cancelAll() {
        session.getAllTasks { $0.forEach { $0.cancel() } }
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard error == nil, let http = task.response as? HTTPURLResponse, [200, 201].contains(http.statusCode),
              let parts = task.taskDescription?.split(separator: "|").map(String.init), parts.count >= 3,
              let m = manifestFor(parts[0]) else { return }
        if parts[1] == "init", let id = Int(parts[2]) {
            try? m.markInitUploaded(id)
        } else if parts[1] == "seg", parts.count == 4, let e = Int(parts[2]), let s = Int(parts[3]) {
            try? m.markAcknowledged(SegmentKey(epoch: e, sequence: s))
        }
    }

    public func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        DispatchQueue.main.async { self.finishedEvents?() }
    }
}
#endif
