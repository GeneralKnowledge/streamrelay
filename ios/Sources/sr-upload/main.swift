// sr-upload: the iPhone app's upload core (manifest + queue + protocol client)
// driven from the command line, so it can be tested against the real server
// on Linux. Imports a folder made by tools/media (init.mp4, 00000.m4s...,
// index.m3u8) into a manifest, either all at once or at recording pace, and
// uploads it until the broadcast is finished.
//
//   sr-upload --api http://127.0.0.1:8080 --token T --segments segs/ [--mode catch_up]
//             [--pair CODE] [--realtime] [--limit N] [--work DIR] [--epoch-every N]
import Foundation
import StreamRelayCore

func arg(_ name: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: name), i + 1 < a.count else { return nil }
    return a[i + 1]
}
func flag(_ name: String) -> Bool { CommandLine.arguments.contains(name) }
func say(_ s: String) {
    let line = "\(String(format: "%.3f", Date().timeIntervalSince1970)) \(s)\n"
    FileHandle.standardError.write(Data(line.utf8))
}

guard let api = arg("--api").flatMap(URL.init(string:)), let segs = arg("--segments") else {
    say("usage: sr-upload --api URL (--token T | --pair CODE) --segments DIR [--mode M] [--realtime] [--limit N]")
    exit(2)
}
let folder = URL(fileURLWithPath: segs)
let index = try String(contentsOf: folder.appendingPathComponent("index.m3u8"), encoding: .utf8)
var durations = index.split(separator: "\n").compactMap { line -> Double? in
    guard line.hasPrefix("#EXTINF:") else { return nil }
    return Double(line.dropFirst(8).split(separator: ",")[0])
}
if let n = arg("--limit").flatMap(Int.init) { durations = Array(durations.prefix(n)) }
let mode = CatchUpMode(rawValue: arg("--mode") ?? "catch_up") ?? .catchUp
let epochEvery = arg("--epoch-every").flatMap(Int.init)   // start a new epoch every N segments (interruptions)

final class Token: @unchecked Sendable {
    var value: String?
    init(_ v: String?) { value = v }
}
let token = Token(arg("--token") ?? ProcessInfo.processInfo.environment["SR_TOKEN"])
let client = ProtocolClient(transport: URLSessionTransport(base: api, token: { token.value }))
if let code = arg("--pair") {
    let (device, t) = try await client.pair(code: code, name: "sr-upload")
    token.value = t
    say("paired as \(device)")
}

let work = URL(fileURLWithPath: arg("--work") ?? NSTemporaryDirectory())
let m = try Manifest.create(in: work, title: arg("--title") ?? "sr-upload", mode: mode)
try m.addInit(id: 0, data: try Data(contentsOf: folder.appendingPathComponent("init.mp4")))

let queue = UploadQueue(manifest: m, client: client, onEvent: { e in
    switch e {
    case .stored(let k, let status): say("stored \(k) \(status)")
    default: say("\(e)")
    }
})
let runner = Task { await queue.run() }

// record: segments join the manifest at their capture time (or all at once)
let t0 = Date()
var start: Int64 = 0, epoch = 0, seq = 0
try m.openEpoch(0)
for (i, d) in durations.enumerated() {
    if let every = epochEvery, i > 0, i % every == 0 {
        try m.closeEpoch(epoch, lastSequence: seq - 1, reason: .interrupted)
        epoch += 1
        seq = 0
        try m.openEpoch(epoch)
    }
    let micros = Int64((d * 1_000_000).rounded())
    if flag("--realtime") {
        let due = Double(start + micros) / 1e6 - Date().timeIntervalSince(t0)
        if due > 0 { try await Task.sleep(nanoseconds: UInt64(due * 1e9)) }
    }
    let data = try Data(contentsOf: folder.appendingPathComponent(String(format: "%05d.m4s", i)))
    try m.addSegment(SegmentKey(epoch: epoch, sequence: seq), initID: 0, startMicros: start, durationMicros: micros, data: data)
    start += micros
    seq += 1
    await queue.nudge()
}
try m.closeEpoch(epoch, lastSequence: seq - 1, reason: .stopped)
try m.requestFinish()
await queue.nudge()
let ending = await runner.value
let backlog = try m.backlog()
say("ending \(ending); pending \(backlog.segments); damaged \(backlog.damaged); broadcast \(try m.recording().broadcastID ?? "-")")
print(try m.recording().broadcastID ?? "")
exit(ending == .finished && backlog.segments == 0 ? 0 : 1)
