import Foundation

/// The phone's record of one recording: every init and media segment written to
/// disk, every epoch, and what the server has acknowledged. SQLite with WAL and
/// synchronous=FULL, so a committed row survives a crash.
///
/// Write order, the whole point (milestones M2): a segment's bytes are written
/// and fsynced to its file *before* its row is committed, and the row is
/// committed before anything uploads it. After a crash a file with no row is
/// deleted (its metadata was never recorded; at most one 2 s segment), and a row
/// whose file is missing is marked damaged.
public final class Manifest: @unchecked Sendable {
    public let folder: URL
    private let db: SQLiteDB
    private let lock = NSLock()

    public struct Recording: Equatable, Sendable {
        public var clientRef: UUID
        public var title: String
        public var mode: CatchUpMode
        public var holdMaxDelaySeconds: Int?
        public var broadcastID: String?
        public var finishRequested: Bool     // Stop pressed: send finish after the last epoch end
        public var finishSent: Bool
    }

    // MARK: Opening

    /// Creates a new recording folder with an empty manifest.
    public static func create(in parent: URL, title: String, mode: CatchUpMode, holdMaxDelaySeconds: Int? = nil,
                              clientRef: UUID = UUID()) throws -> Manifest {
        let folder = parent.appendingPathComponent(clientRef.uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let m = try Manifest(folder: folder)
        _ = try m.locked {
            try m.db.run("INSERT INTO recording (id, client_ref, title, mode, hold_max_delay_s) VALUES (1, ?, ?, ?, ?)",
                         [.text(clientRef.uuidString), .text(title), .text(mode.rawValue),
                          holdMaxDelaySeconds.map { .int(Int64($0)) } ?? .null])
        }
        return m
    }

    /// Opens an existing recording folder.
    public init(folder: URL) throws {
        self.folder = folder
        db = try SQLiteDB(path: folder.appendingPathComponent("manifest.sqlite").path)
        try db.exec("""
            CREATE TABLE IF NOT EXISTS recording (
                id INTEGER PRIMARY KEY CHECK (id = 1), client_ref TEXT NOT NULL, title TEXT NOT NULL,
                mode TEXT NOT NULL, hold_max_delay_s INTEGER, broadcast_id TEXT,
                finish_requested INTEGER NOT NULL DEFAULT 0, finish_sent INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS inits (
                init_id INTEGER PRIMARY KEY, file TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
                uploaded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS epochs (
                epoch INTEGER PRIMARY KEY, last_sequence INTEGER, end_reason TEXT, end_sent INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS segments (
                epoch INTEGER NOT NULL REFERENCES epochs(epoch), sequence INTEGER NOT NULL,
                init_id INTEGER NOT NULL REFERENCES inits(init_id),
                start_us INTEGER NOT NULL, duration_us INTEGER NOT NULL, bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL, file TEXT NOT NULL, final INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (epoch, sequence));
            CREATE INDEX IF NOT EXISTS segments_pending ON segments (state, epoch, sequence);
            """)
    }

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    // MARK: Recording

    public func recording() throws -> Recording {
        try locked {
            guard let r = try db.query("SELECT client_ref, title, mode, hold_max_delay_s, broadcast_id, finish_requested,"
                                       + " finish_sent FROM recording WHERE id = 1").first else {
                throw SQLiteDB.Failure.sqlite("manifest has no recording row")
            }
            return Recording(clientRef: UUID(uuidString: r[0].text ?? "") ?? UUID(), title: r[1].text ?? "",
                             mode: CatchUpMode(rawValue: r[2].text ?? "") ?? .catchUp,
                             holdMaxDelaySeconds: r[3].int.map(Int.init), broadcastID: r[4].text,
                             finishRequested: r[5].int == 1, finishSent: r[6].int == 1)
        }
    }

    public func setBroadcastID(_ id: String) throws {
        try locked { _ = try db.run("UPDATE recording SET broadcast_id = ? WHERE id = 1", [.text(id)]) }
    }

    public func requestFinish() throws {
        try locked { _ = try db.run("UPDATE recording SET finish_requested = 1 WHERE id = 1") }
    }

    public func markFinishSent() throws {
        try locked { _ = try db.run("UPDATE recording SET finish_sent = 1 WHERE id = 1") }
    }

    // MARK: Writing (capture side)

    /// Stores an init segment: file first (fsynced), then the row.
    /// An identical repeat is a no-op; different bytes for a stored id throw and leave the file alone.
    public func addInit(id: Int, data: Data) throws {
        let name = "init-\(id).mp4"
        let sha = SHA256.hex(data)
        try locked {
            if let have = try db.query("SELECT sha256 FROM inits WHERE init_id = ?", [.int(Int64(id))]).first {
                if have[0].text == sha { return }
                throw Conflict(what: "init \(id)")
            }
            try Self.writeDurably(data, to: folder.appendingPathComponent(name))
            _ = try db.run("INSERT INTO inits (init_id, file, sha256, bytes) VALUES (?, ?, ?, ?)",
                           [.int(Int64(id)), .text(name), .text(sha), .int(Int64(data.count))])
        }
    }

    /// Different bytes offered for an init id or segment key the manifest already holds.
    public struct Conflict: Error, Equatable {
        public var what: String
    }

    /// Opens an epoch (idempotent).
    public func openEpoch(_ epoch: Int) throws {
        try locked { _ = try db.run("INSERT OR IGNORE INTO epochs (epoch) VALUES (?)", [.int(Int64(epoch))]) }
    }

    /// Stores a media segment: file first (fsynced), then the row.
    public func addSegment(_ key: SegmentKey, initID: Int, startMicros: Int64, durationMicros: Int64, data: Data) throws {
        let name = "seg-\(key.epoch)-\(key.sequence).m4s"
        let sha = SHA256.hex(data)
        try locked {
            if let have = try db.query("SELECT sha256 FROM segments WHERE epoch = ? AND sequence = ?",
                                       [.int(Int64(key.epoch)), .int(Int64(key.sequence))]).first {
                if have[0].text == sha { return }
                throw Conflict(what: "segment \(key)")
            }
            try Self.writeDurably(data, to: folder.appendingPathComponent(name))
            _ = try db.run("""
                INSERT INTO segments (epoch, sequence, init_id, start_us, duration_us, bytes, sha256, file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [.int(Int64(key.epoch)), .int(Int64(key.sequence)), .int(Int64(initID)), .int(startMicros),
                      .int(durationMicros), .int(Int64(data.count)), .text(sha), .text(name)])
        }
    }

    /// Closes an epoch: its last segment is flagged final. lastSequence -1 = no segments.
    public func closeEpoch(_ epoch: Int, lastSequence: Int, reason: EpochEndReason) throws {
        try locked {
            try db.transaction {
                try db.run("INSERT OR IGNORE INTO epochs (epoch) VALUES (?)", [.int(Int64(epoch))])
                let first = try db.run("UPDATE epochs SET last_sequence = ?, end_reason = ? WHERE epoch = ? AND last_sequence IS NULL",
                                       [.int(Int64(lastSequence)), .text(reason.rawValue), .int(Int64(epoch))])
                if first == 1 {                 // only the first close decides the final segment
                    try db.run("UPDATE segments SET final = 1 WHERE epoch = ? AND sequence = ?",
                               [.int(Int64(epoch)), .int(Int64(lastSequence))])
                }
            }
        }
    }

    /// The highest sequence stored in an epoch, or -1.
    public func lastStoredSequence(epoch: Int) throws -> Int {
        try locked {
            let r = try db.query("SELECT max(sequence) FROM segments WHERE epoch = ?", [.int(Int64(epoch))])
            return r.first?.first?.int.map(Int.init) ?? -1
        }
    }

    public func nextEpoch() throws -> Int {
        try locked {
            let r = try db.query("SELECT max(epoch) FROM epochs")
            return (r.first?.first?.int.map(Int.init) ?? -1) + 1
        }
    }

    /// After a crash or force-quit: close every epoch that was still open with
    /// `app_restart` at its last complete segment, delete files that never got a
    /// row, and mark rows whose file is gone as damaged. Returns the epochs closed.
    @discardableResult
    public func recoverAfterRelaunch() throws -> [Int] {
        let open = try locked {
            try db.query("SELECT epoch FROM epochs WHERE last_sequence IS NULL").compactMap { $0[0].int.map(Int.init) }
        }
        for e in open {
            try closeEpoch(e, lastSequence: try lastStoredSequence(epoch: e), reason: .appRestart)
        }
        let known: Set<String> = try locked {
            let segs = try db.query("SELECT file FROM segments").compactMap { $0[0].text }
            let inits = try db.query("SELECT file FROM inits").compactMap { $0[0].text }
            return Set(segs + inits)
        }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
        for name in names where (name.hasPrefix("seg-") || name.hasPrefix("init-") || name.hasSuffix(".tmp")) && !known.contains(name) {
            try? FileManager.default.removeItem(at: folder.appendingPathComponent(name))
        }
        try locked {
            // an init whose file is back: its segments can go again, whatever its upload state says
            for row in try db.query("SELECT init_id, file, uploaded FROM inits") {
                guard let f = row[1].text, FileManager.default.fileExists(atPath: folder.appendingPathComponent(f).path) else { continue }
                if row[2].int == -1 { try db.run("UPDATE inits SET uploaded = 0 WHERE init_id = ?", [row[0]]) }
                try db.run("UPDATE segments SET state = 'pending' WHERE init_id = ? AND state = 'blocked'", [row[0]])
            }
            for row in try db.query("SELECT epoch, sequence, file FROM segments WHERE state = 'pending'") {
                guard let f = row[2].text, !FileManager.default.fileExists(atPath: folder.appendingPathComponent(f).path) else { continue }
                try db.run("UPDATE segments SET state = 'damaged' WHERE epoch = ? AND sequence = ?", [row[0], row[1]])
            }
        }
        return open
    }

    // MARK: Reading (upload side)

    public func segment(_ key: SegmentKey) throws -> SegmentRecord? {
        try locked {
            try db.query(Self.segmentColumns + " WHERE epoch = ? AND sequence = ?",
                         [.int(Int64(key.epoch)), .int(Int64(key.sequence))]).first.map(Self.record)
        }
    }

    /// Segments still to upload, oldest first (protocol v1 order for catch_up and hold).
    public func pending(limit: Int = 1000) throws -> [SegmentRecord] {
        try locked {
            try db.query(Self.segmentColumns + " WHERE state = 'pending' ORDER BY epoch, sequence LIMIT ?",
                         [.int(Int64(limit))]).map(Self.record)
        }
    }

    public func newestPending() throws -> SegmentRecord? {
        try newestPending(limit: 1).first
    }

    /// The newest pending segments, newest first.
    public func newestPending(limit: Int) throws -> [SegmentRecord] {
        try locked {
            try db.query(Self.segmentColumns + " WHERE state = 'pending' ORDER BY epoch DESC, sequence DESC LIMIT ?",
                         [.int(Int64(limit))]).map(Self.record)
        }
    }

    /// `lost`: the file vanished from the phone; its segments were marked damaged.
    public func inits() throws -> [(id: Int, file: String, sha256: String, uploaded: Bool, lost: Bool)] {
        try locked {
            try db.query("SELECT init_id, file, sha256, uploaded FROM inits ORDER BY init_id").map {
                (Int($0[0].int ?? 0), $0[1].text ?? "", $0[2].text ?? "", $0[3].int == 1, $0[3].int == -1)
            }
        }
    }

    /// The init's file is gone: mark it lost and hold back its pending segments (`blocked`). Returns them.
    /// If the file comes back, `recoverAfterRelaunch` makes it uploadable again.
    @discardableResult
    public func markInitLost(_ id: Int) throws -> [SegmentKey] {
        try locked {
            try db.transaction {
                let keys = try db.query("SELECT epoch, sequence FROM segments WHERE init_id = ? AND state = 'pending'",
                                        [.int(Int64(id))]).map { SegmentKey(epoch: Int($0[0].int ?? 0), sequence: Int($0[1].int ?? 0)) }
                try db.run("UPDATE inits SET uploaded = -1 WHERE init_id = ?", [.int(Int64(id))])
                try db.run("UPDATE segments SET state = 'blocked' WHERE init_id = ? AND state = 'pending'", [.int(Int64(id))])
                return keys
            }
        }
    }

    public func markInitUploaded(_ id: Int) throws {
        try locked { _ = try db.run("UPDATE inits SET uploaded = 1 WHERE init_id = ?", [.int(Int64(id))]) }
    }

    public func markAcknowledged(_ key: SegmentKey) throws {
        try setState(key, .acknowledged)
    }

    /// Back to pending (tests: an acknowledgement lost in a crash).
    func markPending(_ key: SegmentKey) throws {
        try setState(key, .pending)
    }

    public func markDamaged(_ key: SegmentKey) throws {
        try setState(key, .damaged)
    }

    public func countAttempt(_ key: SegmentKey) throws {
        try locked {
            _ = try db.run("UPDATE segments SET attempts = attempts + 1 WHERE epoch = ? AND sequence = ?",
                           [.int(Int64(key.epoch)), .int(Int64(key.sequence))])
        }
    }

    private func setState(_ key: SegmentKey, _ s: UploadState) throws {
        try locked {
            _ = try db.run("UPDATE segments SET state = ? WHERE epoch = ? AND sequence = ?",
                           [.text(s.rawValue), .int(Int64(key.epoch)), .int(Int64(key.sequence))])
        }
    }

    public func epochs() throws -> [EpochRecord] {
        try locked {
            try db.query("SELECT epoch, last_sequence, end_reason, end_sent FROM epochs ORDER BY epoch").map {
                EpochRecord(epoch: Int($0[0].int ?? 0), lastSequence: $0[1].int.map(Int.init),
                            endReason: $0[2].text.flatMap(EpochEndReason.init(rawValue:)), endSent: $0[3].int == 1)
            }
        }
    }

    public func markEpochEndSent(_ epoch: Int) throws {
        try locked { _ = try db.run("UPDATE epochs SET end_sent = 1 WHERE epoch = ?", [.int(Int64(epoch))]) }
    }

    public struct Backlog: Equatable, Sendable {
        public var segments: Int
        public var bytes: Int64
        public var seconds: Double
        public var damaged: Int          // rejected by the server, or blocked by a missing init
        public var blocked: Int          // of those, the ones blocked by a missing init (they may still go)

        public init(segments: Int = 0, bytes: Int64 = 0, seconds: Double = 0, damaged: Int = 0, blocked: Int = 0) {
            self.segments = segments
            self.bytes = bytes
            self.seconds = seconds
            self.damaged = damaged
            self.blocked = blocked
        }
    }

    public func backlog() throws -> Backlog {
        try locked {
            let r = try db.query("SELECT count(*), coalesce(sum(bytes), 0), coalesce(sum(duration_us), 0) FROM segments WHERE state = 'pending'")
            let d = try db.query("SELECT count(*), coalesce(sum(state = 'blocked'), 0) FROM segments"
                                 + " WHERE state IN ('damaged', 'blocked')")
            return Backlog(segments: Int(r[0][0].int ?? 0), bytes: r[0][1].int ?? 0,
                           seconds: Double(r[0][2].int ?? 0) / 1e6, damaged: Int(d[0][0].int ?? 0),
                           blocked: Int(d[0][1].int ?? 0))
        }
    }

    /// Applies the server's `GET status` answer (protocol v1, reconciliation):
    /// segments the server holds are acknowledged; ones it lacks go back to pending.
    public func reconcile(with status: BroadcastStatus) throws {
        try locked {
            try db.transaction {
                let known = Dictionary(uniqueKeysWithValues: status.epochs.map { ($0.epoch, $0) })
                // An acknowledged segment in an epoch the server doesn't list at all is missing there.
                for row in try db.query("SELECT DISTINCT epoch FROM segments WHERE state = 'acknowledged'") {
                    let ep = Int(row[0].int ?? 0)
                    if known[ep] == nil {
                        try db.run("UPDATE segments SET state = 'pending' WHERE epoch = ? AND state = 'acknowledged'",
                                   [.int(Int64(ep))])
                    }
                }
                for e in status.epochs {
                    let rows = try db.query("SELECT sequence, state FROM segments WHERE epoch = ?", [.int(Int64(e.epoch))])
                    for row in rows {
                        let seq = Int(row[0].int ?? 0)
                        let state = row[1].text ?? ""
                        // Unknown answers change nothing: a pending one is re-sent (a stored
                        // duplicate costs one 200), an acknowledged one stays acknowledged.
                        let held = e.holds(seq)
                        if held == true && state == UploadState.pending.rawValue {
                            try db.run("UPDATE segments SET state = 'acknowledged' WHERE epoch = ? AND sequence = ?",
                                       [.int(Int64(e.epoch)), .int(Int64(seq))])
                        } else if held == false && state == UploadState.acknowledged.rawValue {
                            try db.run("UPDATE segments SET state = 'pending' WHERE epoch = ? AND sequence = ?",
                                       [.int(Int64(e.epoch)), .int(Int64(seq))])
                        }
                    }
                    if e.ended {
                        try db.run("UPDATE epochs SET end_sent = 1 WHERE epoch = ?", [.int(Int64(e.epoch))])
                    }
                }
                // an end the server no longer has (restored from a backup): send it again
                let ended = Set(status.epochs.filter(\.ended).map(\.epoch))
                for row in try db.query("SELECT epoch FROM epochs WHERE end_sent = 1") {
                    let ep = Int(row[0].int ?? 0)
                    if !ended.contains(ep) {
                        try db.run("UPDATE epochs SET end_sent = 0 WHERE epoch = ?", [.int(Int64(ep))])
                    }
                }
                // finish is recorded apart from the epoch ends: a server without it hasn't heard Stop
                let finishHeard = ["finishing", "finished", "failed"].contains(status.state)
                let endUnsent = try db.query("SELECT 1 FROM epochs WHERE end_sent = 0").first != nil
                if !finishHeard || endUnsent {
                    try db.run("UPDATE recording SET finish_sent = 0 WHERE id = 1")
                }
                for id in status.inits {
                    // the server holds this init, so segments blocked on its lost file can go
                    try db.run("UPDATE inits SET uploaded = 1 WHERE init_id = ?", [.int(Int64(id))])
                    try db.run("UPDATE segments SET state = 'pending' WHERE init_id = ? AND state = 'blocked'",
                               [.int(Int64(id))])
                }
            }
        }
    }

    // MARK: Files

    public func url(of file: String) -> URL { folder.appendingPathComponent(file) }

    /// Write to a temp file, fsync it, rename it into place, fsync the folder.
    static func writeDurably(_ data: Data, to url: URL) throws {
        let tmp = url.appendingPathExtension("tmp")
        _ = FileManager.default.createFile(atPath: tmp.path, contents: nil)
        let h = try FileHandle(forWritingTo: tmp)
        try h.write(contentsOf: data)
        try h.synchronize()
        try h.close()
        if FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.removeItem(at: url)
        }
        try FileManager.default.moveItem(at: tmp, to: url)
        // the directory entry must be durable before the caller commits a row that points at it
        let dir = open(url.deletingLastPathComponent().path, O_RDONLY)
        guard dir >= 0 else { throw CocoaError(.fileWriteUnknown) }
        defer { close(dir) }
        guard fsync(dir) == 0 else { throw CocoaError(.fileWriteUnknown) }
    }

    private static let segmentColumns =
        "SELECT epoch, sequence, init_id, start_us, duration_us, bytes, sha256, file, final, state, attempts FROM segments"

    private static func record(_ r: [SQLiteDB.Value]) -> SegmentRecord {
        SegmentRecord(key: SegmentKey(epoch: Int(r[0].int ?? 0), sequence: Int(r[1].int ?? 0)), initID: Int(r[2].int ?? 0),
                      startMicros: r[3].int ?? 0, durationMicros: r[4].int ?? 0, bytes: r[5].int ?? 0,
                      sha256: r[6].text ?? "", file: r[7].text ?? "", isFinal: r[8].int == 1,
                      state: UploadState(rawValue: r[9].text ?? "") ?? .pending, attempts: Int(r[10].int ?? 0))
    }
}
