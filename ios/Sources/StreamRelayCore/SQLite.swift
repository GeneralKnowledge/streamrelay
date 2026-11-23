import Foundation
#if canImport(SQLite3)
import SQLite3
#else
import CSQLite
#endif

/// The smallest SQLite wrapper the manifest needs. Not thread-safe by itself:
/// the Manifest serialises access.
final class SQLiteDB {
    enum Failure: Error, CustomStringConvertible {
        case sqlite(String)
        var description: String { if case let .sqlite(m) = self { return m }; return "" }
    }

    enum Value {
        case int(Int64), text(String), null
    }

    private var db: OpaquePointer?
    private static let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    init(path: String) throws {
        if sqlite3_open_v2(path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nil) != SQLITE_OK {
            throw Failure.sqlite("open \(path): \(String(cString: sqlite3_errmsg(db)))")
        }
        // WAL + FULL sync: a row that was committed survives a crash or a power cut.
        try exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;")
    }

    deinit { sqlite3_close(db) }

    func exec(_ sql: String) throws {
        var err: UnsafeMutablePointer<CChar>?
        if sqlite3_exec(db, sql, nil, nil, &err) != SQLITE_OK {
            let msg = err.map { String(cString: $0) } ?? "unknown"
            sqlite3_free(err)
            throw Failure.sqlite(msg)
        }
    }

    @discardableResult
    func run(_ sql: String, _ args: [Value] = []) throws -> Int {
        try withStatement(sql, args) { stmt in
            let rc = sqlite3_step(stmt)
            guard rc == SQLITE_DONE || rc == SQLITE_ROW else { throw Failure.sqlite(self.message) }
            return Int(sqlite3_changes(self.db))
        }
    }

    func query(_ sql: String, _ args: [Value] = []) throws -> [[Value]] {
        try withStatement(sql, args) { stmt in
            var rows: [[Value]] = []
            while true {
                let rc = sqlite3_step(stmt)
                if rc == SQLITE_DONE { break }
                guard rc == SQLITE_ROW else { throw Failure.sqlite(self.message) }
                var row: [Value] = []
                for i in 0..<sqlite3_column_count(stmt) {
                    switch sqlite3_column_type(stmt, i) {
                    case SQLITE_INTEGER: row.append(.int(sqlite3_column_int64(stmt, i)))
                    case SQLITE_NULL: row.append(.null)
                    default: row.append(.text(String(cString: sqlite3_column_text(stmt, i))))
                    }
                }
                rows.append(row)
            }
            return rows
        }
    }

    func transaction<T>(_ body: () throws -> T) throws -> T {
        try exec("BEGIN IMMEDIATE")
        do {
            let v = try body()
            try exec("COMMIT")
            return v
        } catch {
            try? exec("ROLLBACK")
            throw error
        }
    }

    private var message: String { String(cString: sqlite3_errmsg(db)) }

    private func withStatement<T>(_ sql: String, _ args: [Value], _ body: (OpaquePointer?) throws -> T) throws -> T {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { throw Failure.sqlite(message) }
        defer { sqlite3_finalize(stmt) }
        for (i, a) in args.enumerated() {
            let idx = Int32(i + 1)
            switch a {
            case let .int(v): sqlite3_bind_int64(stmt, idx, v)
            case let .text(v): sqlite3_bind_text(stmt, idx, v, -1, SQLiteDB.transient)
            case .null: sqlite3_bind_null(stmt, idx)
            }
        }
        return try body(stmt)
    }
}

extension SQLiteDB.Value {
    var int: Int64? { if case let .int(v) = self { return v }; return nil }
    var text: String? { if case let .text(v) = self { return v }; return nil }
}
