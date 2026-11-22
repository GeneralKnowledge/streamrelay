import Foundation
import XCTest
@testable import StreamRelayCore

final class SHA256Tests: XCTestCase {
    func testKnownVectors() {
        XCTAssertEqual(SHA256.hex(Data()), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        XCTAssertEqual(SHA256.hex(Data("abc".utf8)), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        XCTAssertEqual(SHA256.hex(Data("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq".utf8)),
                       "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1")
        // the portable code matches whatever `hex` uses on this platform, across block boundaries
        for n in [55, 56, 63, 64, 65, 1000] {
            let d = Data((0..<n).map { UInt8($0 % 256) })
            XCTAssertEqual(SHA256.portable(d), SHA256.digest(d), "length \(n)")
        }
    }
}

final class BackoffTests: XCTestCase {
    func testDoublesToCapAndResets() {
        var b = Backoff()
        XCTAssertEqual(b.fail(random: 1), 1)
        XCTAssertEqual(b.fail(random: 1), 2)
        XCTAssertEqual(b.fail(random: 1), 4)
        for _ in 0..<10 { _ = b.fail(random: 1) }
        XCTAssertEqual(b.fail(random: 1), 30)
        XCTAssertEqual(b.fail(random: 0.5), 15, "full jitter scales the ceiling")
        XCTAssertEqual(b.fail(retryAfter: 7, random: 1), 7, "Retry-After wins")
        b.succeed()
        XCTAssertEqual(b.fail(random: 1), 1)
    }
}

final class StatusTests: XCTestCase {
    func testDecodesAndAnswersHolds() throws {
        let json = """
            {"state": "catching_up", "epochs": [
              {"epoch": 0, "received_through": 899, "missing": [], "ended": true},
              {"epoch": 1, "received_through": 151, "missing": [152, 160], "ended": false},
              {"epoch": 2, "received_through": 3, "missing": [5], "ended": false, "missing_truncated": true}],
             "inits": [0], "playout": null, "playout_delay_ms": null}
            """
        let s = try JSONDecoder().decode(BroadcastStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.inits, [0])
        let e0 = s.epochs[0], e1 = s.epochs[1], e2 = s.epochs[2]
        XCTAssertEqual(e0.holds(899), true)
        XCTAssertEqual(e0.holds(900), true, "ended, nothing missing: the epoch ends at 899 on both sides")
        XCTAssertEqual(e1.holds(151), true)
        XCTAssertEqual(e1.holds(152), false)
        XCTAssertEqual(e1.holds(155), true, "between holes")
        XCTAssertEqual(e1.holds(160), false)
        XCTAssertNil(e1.holds(161), "above the last hole of an open epoch: unknown")
        XCTAssertEqual(e2.holds(3), true)
        XCTAssertEqual(e2.holds(5), false)
        XCTAssertNil(e2.holds(4), "list truncated: unknown")
    }
}

final class ManifestTests: XCTestCase {
    func testOrderFinalFlagAndBacklog() throws {
        let m = try makeRecording(count: 3, close: false)
        try m.openEpoch(1)
        try m.addSegment(SegmentKey(epoch: 1, sequence: 0), initID: 0, startMicros: 9_000_000, durationMicros: 2_000_000, data: Data([1]))
        XCTAssertEqual(try m.pending().map(\.key.description), ["0/0", "0/1", "0/2", "1/0"])
        XCTAssertEqual(try m.newestPending()?.key, SegmentKey(epoch: 1, sequence: 0))
        try m.closeEpoch(0, lastSequence: 2, reason: .interrupted)
        XCTAssertEqual(try m.segment(SegmentKey(epoch: 0, sequence: 2))?.isFinal, true)
        XCTAssertEqual(try m.segment(SegmentKey(epoch: 0, sequence: 1))?.isFinal, false)
        try m.markAcknowledged(SegmentKey(epoch: 0, sequence: 0))
        let b = try m.backlog()
        XCTAssertEqual(b.segments, 3)
        XCTAssertEqual(b.seconds, 6, accuracy: 0.001)
        XCTAssertEqual(try m.nextEpoch(), 2)
        // a second close keeps the first answer
        try m.closeEpoch(0, lastSequence: 1, reason: .stopped)
        XCTAssertEqual(try m.epochs().first?.lastSequence, 2)
        XCTAssertEqual(try m.segment(SegmentKey(epoch: 0, sequence: 1))?.isFinal, false, "a repeated close moves no flag")
        XCTAssertEqual(try m.epochs().first?.endReason, .interrupted)
    }

    func testFilesAreWrittenAndHashed() throws {
        let m = try makeRecording(count: 1)
        let s = try XCTUnwrap(m.segment(SegmentKey(epoch: 0, sequence: 0)))
        let data = try Data(contentsOf: m.url(of: s.file))
        XCTAssertEqual(SHA256.hex(data), s.sha256)
        XCTAssertEqual(Int64(data.count), s.bytes)
        XCTAssertFalse(FileManager.default.fileExists(atPath: m.url(of: s.file).path + ".tmp"))
    }

    func testRecoveryAfterCrash() throws {
        let m = try makeRecording(count: 3, close: false)
        // a crash between the file write and the row: a file with no row, plus a temp file
        try Data([9]).write(to: m.folder.appendingPathComponent("seg-0-3.m4s"))
        try Data([9]).write(to: m.folder.appendingPathComponent("seg-0-4.m4s.tmp"))
        // a row whose file vanished
        try FileManager.default.removeItem(at: m.url(of: "seg-0-1.m4s"))

        let reopened = try Manifest(folder: m.folder)
        XCTAssertEqual(try reopened.recoverAfterRelaunch(), [0])
        let e = try XCTUnwrap(reopened.epochs().first)
        XCTAssertEqual(e.lastSequence, 2)
        XCTAssertEqual(e.endReason, .appRestart)
        XCTAssertEqual(try reopened.segment(SegmentKey(epoch: 0, sequence: 2))?.isFinal, true)
        XCTAssertEqual(try reopened.segment(SegmentKey(epoch: 0, sequence: 1))?.state, .damaged)
        XCTAssertFalse(FileManager.default.fileExists(atPath: m.folder.appendingPathComponent("seg-0-3.m4s").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: m.folder.appendingPathComponent("seg-0-4.m4s.tmp").path))
        XCTAssertEqual(try reopened.nextEpoch(), 1)
        XCTAssertEqual(try reopened.recoverAfterRelaunch(), [], "a second relaunch has nothing to do")
    }

    func testReconcile() throws {
        let m = try makeRecording(count: 6, close: false)
        try m.markAcknowledged(SegmentKey(epoch: 0, sequence: 3))   // the server lost it (restored from backup)
        try m.markAcknowledged(SegmentKey(epoch: 0, sequence: 5))   // unknown to this answer: stays
        let status = BroadcastStatus(state: "live", epochs: [
            BroadcastStatus.Epoch(epoch: 0, receivedThrough: 1, missing: [2, 3], ended: false),
        ], inits: [0])
        try m.reconcile(with: status)
        func state(_ i: Int) throws -> UploadState? { try m.segment(SegmentKey(epoch: 0, sequence: i))?.state }
        XCTAssertEqual(try state(0), .acknowledged)
        XCTAssertEqual(try state(1), .acknowledged)
        XCTAssertEqual(try state(2), .pending)
        XCTAssertEqual(try state(3), .pending)
        XCTAssertEqual(try state(4), .pending, "above the last hole of an open epoch: unknown, so it is sent again")
        XCTAssertEqual(try state(5), .acknowledged)
        XCTAssertEqual(try m.inits().first?.uploaded, true)

        // an epoch the server has never heard of: everything acknowledged in it goes back to pending
        try m.reconcile(with: BroadcastStatus(state: "live", epochs: [], inits: []))
        XCTAssertEqual(try state(0), .pending)
    }
}

final class UploadQueueTests: XCTestCase {
    func testCatchUpDeliversEverythingInOrder() async throws {
        let m = try makeRecording(count: 20)
        let server = FakeServer(), clock = VirtualClock()
        let ending = await queue(m, server, clock, concurrency: 1).run()
        XCTAssertEqual(ending, .finished)
        let puts = server.log.filter { $0.hasPrefix("PUT") }
        XCTAssertEqual(puts, (0..<20).map { "PUT 0/\($0)" }, "oldest first")
        XCTAssertEqual(server.log.first, "INIT 0")
        XCTAssertEqual(server.log.last, "FINISH 0")
        XCTAssertEqual(server.epochEnds, [0: 19])
        XCTAssertEqual(server.segments[SegmentKey(epoch: 0, sequence: 19)]?.flags, "final")
        XCTAssertNil(server.segments[SegmentKey(epoch: 0, sequence: 18)]?.flags)
        XCTAssertEqual(try m.backlog().segments, 0)
        XCTAssertEqual(try m.recording().broadcastID, server.broadcastID)
    }

    func testSurvivesABadNetwork() async throws {
        let m = try makeRecording(count: 60)
        let server = FakeServer(), clock = VirtualClock()
        server.faults = .init(offline: 0.3, busy: 0.1, corrupt: 0.05)
        let ending = await queue(m, server, clock).run()
        XCTAssertEqual(ending, .finished)
        XCTAssertEqual(server.segments.count, 60)
        for s in try m.pending() { XCTFail("still pending: \(s.key)") }
        for i in 0..<60 {
            let key = SegmentKey(epoch: 0, sequence: i)
            XCTAssertEqual(server.segments[key]?.sha, try m.segment(key)?.sha256, "segment \(i)")
            XCTAssertEqual(try m.segment(key)?.state, .acknowledged)
        }
        XCTAssertEqual(server.finished, 0)
    }

    func testRestartMidwayCarriesOnFromStatus() async throws {
        let m = try makeRecording(count: 30)
        let server = FakeServer(), clock = VirtualClock()
        let counter = Locked(0)
        let q = queue(m, server, clock, concurrency: 1, events: { e in
            if case .stored = e { counter.with { $0 += 1 } }
        })
        let task = Task { await q.run() }
        await eventually("10 segments stored") { counter.with { $0 } >= 10 }
        await q.stop()
        let first = await task.value
        XCTAssertEqual(first, .stopped)
        XCTAssertGreaterThan(try m.backlog().segments, 0, "the first run stopped with work left")
        // the app died before recording five acknowledgements: the server holds 0/3-0/7, the phone says pending
        for i in 3..<8 { try m.markPending(SegmentKey(epoch: 0, sequence: i)) }
        let reopened = try Manifest(folder: m.folder)
        try reopened.recoverAfterRelaunch()
        server.locked { server.log.removeAll() }
        let ending = await queue(reopened, server, clock).run()
        XCTAssertEqual(ending, .finished)
        XCTAssertEqual(server.segments.count, 30)
        XCTAssertEqual(server.created, 1, "the broadcast id was kept; no second create")
        for i in 0..<8 {
            XCTAssertFalse(server.log.contains("PUT 0/\(i)"), "reconciled: 0/\(i), held by the server, is not sent again")
            XCTAssertEqual(try reopened.segment(SegmentKey(epoch: 0, sequence: i))?.state, .acknowledged)
        }
    }

    func testReuploadsTheInitOn412() async throws {
        let m = try makeRecording(count: 4)
        let server = FakeServer(), clock = VirtualClock()
        try m.setBroadcastID(server.broadcastID)
        try m.markInitUploaded(0)            // the phone thinks so; the server doesn't have it
        server.created = 1
        // reconciliation only ever adds acknowledgements, so the stale flag survives it and the PUT gets 412
        let ending = await queue(m, server, clock).run()
        XCTAssertEqual(ending, .finished)
        XCTAssertNotNil(server.inits[0])
        XCTAssertEqual(server.segments.count, 4)
    }

    func testTwo422sMarkDamagedAndTheRestGoesOn() async throws {
        let m = try makeRecording(count: 5)
        let server = FakeServer(), clock = VirtualClock()
        server.faults.alwaysReject = ["0/2"]
        let damaged = Locked<[SegmentKey]>([])
        let ending = await queue(m, server, clock, events: { e in
            if case let .damaged(k, _) = e { damaged.with { $0.append(k) } }
        }).run()
        XCTAssertEqual(ending, .finished)
        XCTAssertEqual(damaged.with { $0 }, [SegmentKey(epoch: 0, sequence: 2)])
        XCTAssertEqual(try m.segment(SegmentKey(epoch: 0, sequence: 2))?.state, .damaged)
        XCTAssertEqual(try m.backlog().damaged, 1)
        XCTAssertEqual(server.segments.count, 4)
    }

    func testRevokedTokenStops() async throws {
        let m = try makeRecording(count: 2)
        let server = FakeServer(), clock = VirtualClock()
        server.faults.refuseWith = 401
        let ending = await queue(m, server, clock).run()
        guard case .refused(401, _) = ending else { return XCTFail("\(ending)") }
        XCTAssertEqual(try m.backlog().segments, 2, "nothing is lost on the phone")
    }

    func testShortLastSegmentWaitsForItsEpochToClose() async throws {
        let m = try makeRecording(count: 3, close: false)
        try m.addSegment(SegmentKey(epoch: 0, sequence: 3), initID: 0, startMicros: 6_000_000,
                         durationMicros: 700_000, data: Data([3]))
        let server = FakeServer(), clock = VirtualClock()
        let q = queue(m, server, clock)
        let task = Task { await q.run() }
        await eventually("three segments stored") { server.locked { server.segments.count } >= 3 }
        for _ in 0..<200 { await Task.yield() }
        XCTAssertNil(server.locked { server.segments[SegmentKey(epoch: 0, sequence: 3)] }, "held back while the epoch is open")
        try m.closeEpoch(0, lastSequence: 3, reason: .stopped)
        try m.requestFinish()
        await q.nudge()
        let ending = await task.value
        XCTAssertEqual(ending, .finished)
        XCTAssertEqual(server.segments[SegmentKey(epoch: 0, sequence: 3)]?.flags, "final")
    }

    func testSkipToLiveSendsTheLiveEdgeFirst() async throws {
        let m = try makeRecording(mode: .skipToLive, count: 10, close: false)
        let server = FakeServer(), clock = VirtualClock()
        let q = queue(m, server, clock, concurrency: 1)
        let task = Task { await q.run() }
        await eventually("the first segment stored") { server.locked { server.segments.count } >= 1 }
        XCTAssertEqual(server.locked { server.log.first(where: { $0.hasPrefix("PUT") }) }, "PUT 0/9",
                       "the newest segment goes first")
        try m.closeEpoch(0, lastSequence: 9, reason: .stopped)
        try m.requestFinish()
        await q.nudge()
        let ending = await task.value
        XCTAssertEqual(ending, .finished)
        // 0/8 is still within one segment of the live edge, so it counts as live too
        let rest = server.log.filter { $0.hasPrefix("PUT") }.dropFirst()
        XCTAssertEqual(Array(rest), ["PUT 0/8"] + (0..<8).map { "PUT 0/\($0)" }, "then backfill oldest first")
    }

    func testNewSegmentsDoNotCutABackoffShort() async throws {
        let m = try makeRecording(count: 3)
        let server = FakeServer(), clock = VirtualClock()
        server.faults.putsOffline = true
        let waits = Locked<[Double]>([])
        let q = queue(m, server, clock, events: { e in
            if case let .waiting(s, _) = e { waits.with { $0.append(s) } }
        })
        let task = Task { await q.run() }
        let until = Date().addingTimeInterval(10)
        while waits.with({ $0.count }) < 12 && Date() < until {
            await q.nudge()                      // new segments keep arriving while recording
            for _ in 0..<20 { await Task.yield() }
        }
        XCTAssertGreaterThanOrEqual(waits.with { $0.count }, 12)
        XCTAssertGreaterThan(waits.with { $0.suffix(6).max() ?? 0 }, 4, "the backoff kept growing")
        server.locked { server.faults.putsOffline = false }
        await q.networkChanged()
        let ending = await task.value
        XCTAssertEqual(ending, .finished)
    }

    func testCreateHonoursRetryAfterAndStopsOnARefusedRecording() async throws {
        let server = FakeServer(), clock = VirtualClock()
        server.faults.createReply = (429, "60")           // too many active broadcasts
        let waits = Locked<[Double]>([])
        let q = queue(try makeRecording(count: 1), server, clock, events: { e in
            if case let .waiting(s, _) = e { waits.with { $0.append(s) } }
        })
        let task = Task { await q.run() }
        await eventually("a wait") { waits.with { !$0.isEmpty } }
        XCTAssertEqual(waits.with { $0.first }, 60, "Retry-After is honoured, not shortened")
        server.locked { server.faults.createReply = (422, nil) }    // the recording's own settings refused
        let ending = await task.value
        guard case .refused(422, _) = ending else { return XCTFail("\(ending)") }
    }

    func testAMissingInitBlocksOnlyItsOwnEpoch() async throws {
        let m = try makeRecording(count: 2, close: false)
        try m.closeEpoch(0, lastSequence: 1, reason: .interrupted)
        try m.addInit(id: 1, data: Data("init one".utf8))
        try m.openEpoch(1)
        try m.addSegment(SegmentKey(epoch: 1, sequence: 0), initID: 1, startMicros: 9_000_000, durationMicros: 2_000_000, data: Data([1]))
        try m.closeEpoch(1, lastSequence: 0, reason: .stopped)
        try m.requestFinish()
        try FileManager.default.removeItem(at: m.url(of: "init-0.mp4"))
        let server = FakeServer(), clock = VirtualClock()
        let ending = await queue(m, server, clock).run()
        XCTAssertEqual(ending, .finished)
        XCTAssertNotNil(server.segments[SegmentKey(epoch: 1, sequence: 0)], "epoch 1 uploads")
        XCTAssertEqual(try m.segment(SegmentKey(epoch: 0, sequence: 0))?.state, .blocked)
        XCTAssertEqual(try m.backlog().damaged, 2)
        // the file comes back: the next launch makes those segments uploadable again
        try Data("init".utf8).write(to: m.url(of: "init-0.mp4"))
        try m.recoverAfterRelaunch()
        XCTAssertEqual(try m.segment(SegmentKey(epoch: 0, sequence: 0))?.state, .pending)
    }

    func testNoNewRequestAfterStop() async throws {
        let m = try makeRecording(count: 3)
        let server = FakeServer(), clock = VirtualClock()
        let q = queue(m, server, clock)
        await q.stop()
        let ending = await q.run()
        XCTAssertEqual(ending, .stopped)
        XCTAssertEqual(server.locked { server.log }, [], "nothing sent once stopped")
    }

    func testLiveEdgeSkipsAShortTailAndTakesTheSegmentBefore() async throws {
        let m = try makeRecording(mode: .skipToLive, count: 10, close: false)
        try m.addSegment(SegmentKey(epoch: 0, sequence: 10), initID: 0, startMicros: 20_000_000,
                         durationMicros: 600_000, data: Data([10]))
        let server = FakeServer(), clock = VirtualClock()
        let q = queue(m, server, clock, concurrency: 1)
        let task = Task { await q.run() }
        await eventually("the first segment stored") { server.locked { server.segments.count } >= 1 }
        XCTAssertEqual(server.locked { server.log.first(where: { $0.hasPrefix("PUT") }) }, "PUT 0/9",
                       "the newest ready segment, not the open epoch's short tail and not old backfill")
        await q.stop()
        _ = await task.value
    }
}

final class ManifestSafetyTests: XCTestCase {
    func testConflictingBytesNeverReplaceAStoredFile() throws {
        let m = try makeRecording(count: 1)
        let key = SegmentKey(epoch: 0, sequence: 0)
        let before = try Data(contentsOf: m.url(of: "seg-0-0.m4s"))
        XCTAssertThrowsError(try m.addSegment(key, initID: 0, startMicros: 0, durationMicros: 2_000_000, data: Data("other".utf8)))
        XCTAssertEqual(try Data(contentsOf: m.url(of: "seg-0-0.m4s")), before)
        try m.addSegment(key, initID: 0, startMicros: 0, durationMicros: 2_000_000, data: before)   // identical: fine
        XCTAssertThrowsError(try m.addInit(id: 0, data: Data("another init".utf8)))
        XCTAssertEqual(try Data(contentsOf: m.url(of: "init-0.mp4")), Data("init".utf8))
    }

    func testReconcileResendsAnEpochEndTheServerLost() throws {
        let m = try makeRecording(count: 2)
        try m.markEpochEndSent(0)
        try m.markFinishSent()
        try m.reconcile(with: BroadcastStatus(state: "live", epochs: [
            BroadcastStatus.Epoch(epoch: 0, receivedThrough: 1, ended: false),
        ], inits: [0]))
        XCTAssertEqual(try m.epochs().first?.endSent, false)
        XCTAssertEqual(try m.recording().finishSent, false, "finish goes again after the end")
    }

    func testOnlyHTTPSOrTailnetCarriesCredentials() {
        for ok in ["https://relay.example.com", "http://127.0.0.1:8080", "http://localhost:8080", "http://100.72.135.124:8080"] {
            XCTAssertTrue(ServerURL.isAllowed(URL(string: ok)!), ok)
        }
        for bad in ["http://relay.example.com", "http://192.168.1.169:8080", "http://100.128.0.1", "ftp://x", "http://127.0.0.1.evil.com"] {
            XCTAssertFalse(ServerURL.isAllowed(URL(string: bad)!), bad)
        }
        XCTAssertEqual(ServerURL.origin(URL(string: "HTTPS://Relay.Example.com/x")!), "https://relay.example.com:443")
    }
}
