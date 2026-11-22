"""Check a media-continuity run from an independent recording of the output.

Reads, from a run folder made by tests/media-continuity/run_upload.sh:
  rec.flv         what a viewer (ffmpeg -c copy) received from MediaMTX
  mediamtx.log    the receiver's log: publish and read sessions
  reader.log      the viewer's warnings
  phone.jsonl     the fake phone's log: offline windows, uploads
  playout.jsonl   the playout's log: output clock start, what it showed, CPU

Every check is computed from the recording, except two that need wall-clock
time (when holding appeared, when footage came back): those map a recorded
frame to the playout's output clock (output frame n is sent at t0 + n/30),
anchored on the first footage frame, and the mapping is cross-checked against
every switch the playout logged.

Writes verify.json and verify.md into the run folder; exits 1 if a gate fails.

    python3 tools/verify/verify.py RUN_DIR [--buffer-target 8] [--resume-threshold 10]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass

import av
import numpy as np

FPS = 30
BITS, BLOCK = 24, 40
HOLD = (1 << BITS) - 1
CENTERS = [i * BLOCK + BLOCK // 2 for i in range(BITS)]


def jl(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def read_codes(path: str) -> list[int]:
    """Barcode value of every decoded frame, in decode-output order."""
    out = []
    with av.open(path) as c:
        for f in c.decode(video=0):
            y = f.planes[0]
            row = np.frombuffer(y, np.uint8).reshape(-1, y.line_size)[BLOCK // 2]
            v = 0
            for b in row[CENTERS] > 128:
                v = (v << 1) | int(b)
            out.append(v)
    return out


def read_packets(path: str, kind: str) -> list[tuple[float, float]]:
    with av.open(path) as c:
        st = c.streams.video[0] if kind == "video" else c.streams.audio[0]
        return [(float(p.pts * p.time_base), float((p.duration or 0) * p.time_base))
                for p in c.demux(st) if p.pts is not None]


# ---- checks: pure functions, unit-tested in tests/unit/test_verify.py ----------------------------

@dataclass
class Check:
    name: str
    passed: bool
    value: object
    gate: str

    def row(self) -> str:
        return f"| {self.name} | {self.gate} | {self.value} | {'pass' if self.passed else '**FAIL**'} |"


def check_video_pts(pts: list[float]) -> list[Check]:
    d = np.diff(pts) if len(pts) > 1 else np.array([1 / FPS])
    bad = int((d <= 0).sum())
    gaps = int((d > 1.5 / FPS + 0.001).sum())
    return [Check("video PTS strictly increasing", bad == 0, f"{bad} non-increasing of {len(pts)}", "0"),
            Check("video gaps over 1.5 frames", gaps == 0, f"{gaps} (max {round(float(d.max()) * 1000, 1)} ms)", "0")]


def check_audio(packets: list[tuple[float, float]]) -> Check:
    gaps = [packets[i + 1][0] - (packets[i][0] + packets[i][1]) for i in range(len(packets) - 1)]
    big = sum(g > 0.025 for g in gaps)
    return Check("audio gaps over 25 ms", big == 0, f"{big} (max {round(max(gaps or [0]) * 1000, 1)} ms)", "0")


def check_order(codes: list[int]) -> Check:
    """Footage frames (not generated cards) appear in source order, none twice."""
    src = [c for c in codes if c != HOLD]
    d = np.diff(src) if len(src) > 1 else np.array([1])
    repeats = int((d == 0).sum())
    backwards = int((d < 0).sum())
    return Check("footage frames in source order, none twice", repeats == 0 and backwards == 0,
                 f"{len(src)} frames, {repeats} repeated, {backwards} out of order", "0 repeated, 0 out of order")


def runs(codes: list[int]) -> list[tuple[str, int, int]]:
    """(kind, first index, last index) for each run of footage or generated frames."""
    out: list[tuple[str, int, int]] = []
    for i, c in enumerate(codes):
        k = "card" if c == HOLD else "footage"
        if out and out[-1][0] == k:
            out[-1] = (k, out[-1][1], i)
        else:
            out.append((k, i, i))
    return out


def gap_timing(codes: list[int], offset: int, t0: float, windows: list[tuple[float, float]],
               buffer_target: float, resume_threshold: float) -> list[Check]:
    """For each upload outage: when holding became visible and when footage came back.

    Recorded frame k was sent at wall time t0 + (k + offset) / 30.
    """
    wall = [t0 + (k + offset) / FPS for k in range(len(codes))]
    rs = runs(codes)
    checks = []
    hold_gate, back_gate = f"<= {buffer_target + 2:.0f} s", f"<= {resume_threshold + 2:.0f} s"
    for i, (start, end) in enumerate(windows, 1):
        hold = next(((a, b) for kind, a, b in rs if kind == "card" and wall[b] >= start), None)
        if hold is None or wall[hold[0]] > end + resume_threshold + 30:
            checks.append(Check(f"gap {i}: holding visible after uploads stopped", False, "never", hold_gate))
            continue
        appear = wall[hold[0]] - start
        checks.append(Check(f"gap {i}: holding visible after uploads stopped", appear <= buffer_target + 2,
                            f"{appear:.1f} s", hold_gate))
        if hold[1] + 1 >= len(codes):
            checks.append(Check(f"gap {i}: footage back after uploads resumed", False, "never", back_gate))
            continue
        back = wall[hold[1] + 1] - end
        checks.append(Check(f"gap {i}: footage back after uploads resumed", back <= resume_threshold + 2,
                            f"{back:.1f} s", back_gate))
    return checks


def count_sessions(mtx_log: str, path: str) -> tuple[int, int]:
    pub = len(re.findall(rf"is publishing to path '{re.escape(path)}'", mtx_log))
    read = len(re.findall(rf"is reading from path '{re.escape(path)}'", mtx_log))
    return pub, read


def viewer_closed_after_publisher(mtx_log: str) -> bool:
    """MediaMTX closes the reader only when (or after) the publisher's connection ends."""
    conns: dict[str, str] = {}
    closed: dict[str, str] = {}
    for line in mtx_log.splitlines():
        m = re.match(r"(\S+ \S+) .*\[conn ([\d.:]+)\] (is publishing|is reading|closed)", line)
        if not m:
            continue
        t, conn, what = m.groups()
        if what == "closed":
            closed[conn] = t
        else:
            conns[conn] = what
    pubs = [closed.get(c) for c, w in conns.items() if w == "is publishing"]
    reads = [closed.get(c) for c, w in conns.items() if w == "is reading"]
    if not pubs or not reads or any(t is None for t in pubs + reads):
        return False
    return min(t for t in reads if t) >= max(t for t in pubs if t)


# ---- main ----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--buffer-target", type=float, default=8.0)
    ap.add_argument("--resume-threshold", type=float, default=10.0)
    a = ap.parse_args(argv)
    W = a.run
    rec = os.path.join(W, "rec.flv")
    phone = jl(os.path.join(W, "phone.jsonl"))
    play = jl(os.path.join(W, "playout.jsonl"))
    start = next(e for e in phone if e["event"] == "start")
    bid = start["broadcast_id"]
    ptstart = next(e for e in play if e["event"] == "start")
    t0 = ptstart["t0"] - ptstart["n"] / FPS           # wall time of output frame 0 on this process's clock
    windows = []
    for e in phone:
        if e["event"] == "offline_start":
            windows.append([e["wall"], None])
        elif e["event"] == "offline_end":
            windows[-1][1] = e["wall"]
    windows_t = [(s, e) for s, e in windows if e is not None]
    checks: list[Check] = []
    R: dict[str, object] = {"broadcast_id": bid, "offline_windows_rel_s": start["offline"]}

    mtx = open(os.path.join(W, "mediamtx.log")).read()
    pub, read = count_sessions(mtx, f"live/{bid}")
    checks.append(Check("publish sessions at the receiver", pub == 1, pub, "1"))
    checks.append(Check("viewer sessions (no reconnect)", read == 1, read, "1"))
    closed = viewer_closed_after_publisher(mtx)
    checks.append(Check("viewer disconnected only after the broadcast ended", closed, closed, "True"))
    # ffmpeg reports the end of the stream as an I/O error on its last read: not a warning
    reader_lines = [ln for ln in open(os.path.join(W, "reader.log")).read().splitlines()
                    if ln.strip() and "Input/output error" not in ln]
    checks.append(Check("viewer warnings", not reader_lines, len(reader_lines), "0"))
    R["viewer_warning_lines"] = reader_lines[:20]

    vp = [p[0] for p in read_packets(rec, "video")]
    checks += check_video_pts(vp)
    checks.append(check_audio(read_packets(rec, "audio")))
    R["recording_seconds"] = round(vp[-1] - vp[0], 1)

    codes = read_codes(rec)
    checks.append(check_order(codes))
    src = [c for c in codes if c != HOLD]
    seen = sorted({c // FPS for c in src})
    R["source_seconds_shown"] = len(seen)
    R["source_seconds_range"] = [seen[0], seen[-1]] if seen else None
    R["source_frame_largest_step"] = int(np.diff(src).max()) if len(src) > 1 else None

    # map recorded frames to the output clock: first footage frame <-> first 'showing footage' event
    shows = [e for e in play if e["event"] == "showing"]
    first_footage_n = next(e["n"] for e in shows if e["what"] == "footage")
    k0 = next(i for i, c in enumerate(codes) if c != HOLD)
    offset = first_footage_n - k0
    rec_switches = [b + 1 for _, _, b in runs(codes)[:-1]]
    log_switches = [e["n"] - offset for e in shows if e["what"] in ("footage", "holding", "ended", "interrupted")]
    mism = [min((abs(k - n) for n in log_switches), default=999) for k in rec_switches]
    R["clock_mapping"] = {"offset_frames": offset, "recorded_switches": len(rec_switches),
                          "max_mismatch_frames": max(mism, default=0)}
    checks.append(Check("recorded switches match the playout log", max(mism, default=0) <= 1,
                        f"{len(rec_switches)} switches, max {max(mism, default=0)} frame off", "<= 1 frame"))
    checks += gap_timing(codes, offset, t0, windows_t, a.buffer_target, a.resume_threshold)

    # informative: CPU, lateness, what the phone and the store say
    secs = [e for e in play if e["event"] == "second"]
    by: dict[str, list[float]] = {}
    for s in secs:
        by.setdefault(s["showing"], []).append(s["cpu"])
    end = next((e for e in play if e["event"] == "end"), {})
    R["cpu_mean_core"] = round(float(np.mean([s["cpu"] for s in secs])), 3) if secs else None
    R["cpu_by_showing"] = {k: round(float(np.mean(v)), 3) for k, v in by.items()}
    R["cpu_max_1s"] = max((s["cpu"] for s in secs), default=None)
    R["late_ticks"], R["late_max_s"] = end.get("late_ticks"), end.get("late_max")
    puts = [e for e in phone if e["event"] == "put"]
    R["phone_puts"] = {str(k): sum(1 for p in puts if p["status"] == k) for k in sorted({p["status"] for p in puts})}
    fin = next((e for e in phone if e["event"] == "finish"), {})
    R["phone_acknowledged"], R["phone_segments"] = fin.get("acked"), start["segments"]
    archive = os.path.join(W, "segments_played.txt")
    if os.path.exists(archive):
        R["store_play_results"] = dict(ln.split("|") for ln in open(archive).read().split())
    R["checks"] = [c.__dict__ for c in checks]
    ok = all(c.passed for c in checks)
    R["verdict"] = "pass" if ok else "fail"
    json.dump(R, open(os.path.join(W, "verify.json"), "w"), indent=1)
    md = ["| Check | Gate | Measured | Result |", "|---|---|---|---|"] + [c.row() for c in checks]
    md.append("")
    md.append(f"**Verdict: {R['verdict']}.**")
    open(os.path.join(W, "verify.md"), "w").write("\n".join(md) + "\n")
    print("\n".join(md))
    print(json.dumps({k: v for k, v in R.items() if k != "checks"}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
