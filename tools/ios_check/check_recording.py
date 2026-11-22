"""Checks a recording folder pulled off the iPhone (Files app: On My iPhone >
StreamRelay > Recordings > <id>), for the M2 condition matrix.

For every epoch in manifest.sqlite:
  files      every row's file exists and matches its SHA-256 and size; no stray files
  codecs     h264 video (+ at most one aac audio) in the init
  keyframe   each segment starts with a keyframe (IDR)
  duration   each segment's video lasts 2.0 s within one frame, except the epoch's
             final one; the declared duration matches what is in the file
  timing     start_us of each segment = previous start + duration (within one frame)
  decode     init + all segments of the epoch decode with no errors
  total      decoded length within 0.1 % of the sum of declared durations
  a/v        no audio gap over 50 ms inside an epoch
and across epochs: start_us never goes backwards, each closed epoch's final flag
sits on its last sequence, and the recording's state (pending/acknowledged/damaged).

    python3 tools/ios_check/check_recording.py <folder> [--json out.json]
Exit status 1 if any check fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

FRAME_TOL = 1.0 / 30 + 0.001


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    epochs: list[dict] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.problems.append(msg)


def probe_packets(path: str, stream: str) -> list[tuple[float, float, bool]]:
    """(pts, duration, keyframe) per packet of the first stream of a kind."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                          "packet=pts_time,duration_time,flags", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=120)
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        if len(parts) < 3 or parts[0] in ("", "N/A"):
            continue
        rows.append((float(parts[0]), float(parts[1]) if parts[1] not in ("", "N/A") else 0.0, "K" in parts[2]))
    return rows


def frame_step(packets: list[tuple[float, float, bool]]) -> float:
    """One packet's length when ffprobe doesn't give it: the smallest positive step between sorted starts."""
    starts = sorted(p[0] for p in packets)
    steps = [b - a for a, b in zip(starts, starts[1:], strict=False) if b - a > 1e-6]
    return min(steps, default=0.0)


def span(packets: list[tuple[float, float, bool]]) -> float:
    """Presentation-time length: earliest start to latest end (packets may arrive out of order)."""
    if not packets:
        return 0.0
    step = frame_step(packets)
    return max(p[0] + (p[1] or step) for p in packets) - min(p[0] for p in packets)


def max_gap(packets: list[tuple[float, float, bool]]) -> float:
    """The largest hole between one packet's end and the next packet's start, in time order."""
    step = frame_step(packets)
    ordered = sorted(packets)
    return max(((b[0] - (a[0] + (a[1] or step))) for a, b in zip(ordered, ordered[1:], strict=False)), default=0.0)


def inside(folder: str, name: str) -> str | None:
    """The file's path if it stays inside the folder (no ../, no absolute path, no symlink out)."""
    root = os.path.realpath(folder)
    path = os.path.realpath(os.path.join(root, name))
    return path if os.path.commonpath([root, path]) == root and os.path.dirname(path) == root else None


def codecs(path: str) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=60)
    return [c.strip() for c in out.stdout.split() if c.strip()]


def check(folder: str) -> Report:
    rep = Report()
    manifest = pathlib.Path(folder, "manifest.sqlite").resolve()
    db = sqlite3.connect(manifest.as_uri() + "?mode=ro", uri=True)
    inits = {r[0]: r[1:] for r in db.execute("SELECT init_id, file, sha256, bytes FROM inits")}
    epochs = db.execute("SELECT epoch, last_sequence, end_reason FROM epochs ORDER BY epoch").fetchall()
    known = {r[0] for r in db.execute("SELECT file FROM segments")} | {v[0] for v in inits.values()}
    for name in os.listdir(folder):
        if (name.startswith("seg-") or name.startswith("init-")) and name not in known:
            rep.fail(f"stray file {name} (no manifest row)")

    def file_ok(name: str, sha: str, size: int) -> bytes | None:
        p = inside(folder, name)
        if p is None:
            rep.fail(f"{name}: path leaves the recording folder")
            return None
        if not os.path.exists(p):
            rep.fail(f"{name}: missing")
            return None
        data = open(p, "rb").read()
        if hashlib.sha256(data).hexdigest() != sha or len(data) != size:
            rep.fail(f"{name}: checksum or size differs from the manifest")
        return data

    epoch_ids = {e[0] for e in epochs}
    for epoch, seq, name, sha, size in db.execute("SELECT epoch, sequence, file, sha256, bytes FROM segments"):
        if epoch not in epoch_ids:
            rep.fail(f"{epoch}/{seq}: segment row with no epoch row")
            file_ok(name, sha, size)
    used_inits = {r[0] for r in db.execute("SELECT DISTINCT init_id FROM segments")}
    for iid, (name, sha, size) in inits.items():
        if iid not in used_inits:
            file_ok(name, sha, size)             # an init no segment uses must still be intact

    prev_end = None
    for epoch, last, reason in epochs:
        segs = db.execute("SELECT sequence, init_id, start_us, duration_us, bytes, sha256, file, final, state"
                          " FROM segments WHERE epoch = ? ORDER BY sequence", (epoch,)).fetchall()
        info = {"epoch": epoch, "last_sequence": last, "end_reason": reason, "segments": len(segs),
                "states": {}, "seconds_declared": 0.0}
        rep.epochs.append(info)
        if not segs:
            if last is not None and last >= 0:
                rep.fail(f"epoch {epoch}: closed at {last} but has no segment rows")
            continue
        for s in segs:
            info["states"][s[8]] = info["states"].get(s[8], 0) + 1
        if [s[0] for s in segs] != list(range(len(segs))):
            rep.fail(f"epoch {epoch}: sequences are not 0..{len(segs) - 1}")
        if last is not None and segs[-1][0] != last:
            rep.fail(f"epoch {epoch}: closed at {last} but the last segment is {segs[-1][0]}")
        finals = [s[0] for s in segs if s[7]]
        if last is not None and finals != [last]:
            rep.fail(f"epoch {epoch}: final flag on {finals}, expected [{last}]")
        init_ids = {s[1] for s in segs}
        if len(init_ids) != 1 or next(iter(init_ids)) not in inits:
            rep.fail(f"epoch {epoch}: init ids {sorted(init_ids)} (expected one stored init)")
            continue
        ifile, isha, ibytes = inits[next(iter(init_ids))]
        init_data = file_ok(ifile, isha, ibytes)
        if init_data is None:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            ip = os.path.join(tmp, "init.mp4")
            open(ip, "wb").write(init_data)
            cs = codecs(ip)
            info["codecs"] = cs
            if cs.count("h264") != 1 or cs.count("aac") != 1 or len(cs) != 2:
                rep.fail(f"epoch {epoch}: codecs {cs} (want one h264 and one aac track)")
            joined = os.path.join(tmp, "epoch.mp4")
            by_seq = {r[0]: r for r in segs}
            with open(joined, "wb") as j:
                j.write(init_data)
                for seq, _, start, dur, size, sha, name, final, _state in segs:
                    data = file_ok(name, sha, size)
                    if data is None:
                        continue
                    j.write(data)
                    one = os.path.join(tmp, "one.mp4")
                    open(one, "wb").write(init_data + data)
                    video = probe_packets(one, "v:0")
                    if not video:
                        rep.fail(f"{epoch}/{seq}: no video packets")
                        continue
                    if not video[0][2]:
                        rep.fail(f"{epoch}/{seq}: does not start with a keyframe")
                    vdur = span(video)
                    if abs(vdur - dur / 1e6) > FRAME_TOL:
                        rep.fail(f"{epoch}/{seq}: video lasts {vdur:.3f} s, manifest says {dur / 1e6:.3f} s")
                    if not final and abs(dur / 1e6 - 2.0) > FRAME_TOL:
                        rep.fail(f"{epoch}/{seq}: {dur / 1e6:.3f} s, not 2.0 s (and not final)")
                    if prev_end is not None and seq == 0 and start < prev_end - 50_000:
                        rep.fail(f"{epoch}/0: start_us {start} goes back before the previous epoch's end {prev_end}")
                    ps = by_seq.get(seq - 1)
                    if ps is not None:              # a missing sequence is reported above
                        if abs(start - (ps[2] + ps[3])) > FRAME_TOL * 1e6:
                            rep.fail(f"{epoch}/{seq}: start_us {start} != previous start + duration {ps[2] + ps[3]}")
                    info["seconds_declared"] += dur / 1e6
            prev_end = segs[-1][2] + segs[-1][3]
            dec = subprocess.run(["ffmpeg", "-v", "error", "-i", joined, "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=600)
            if dec.returncode != 0 or dec.stderr.strip():
                rep.fail(f"epoch {epoch}: decode errors: {dec.stderr.strip()[:300]}")
            video = probe_packets(joined, "v:0")
            if video:
                total = span(video)
                info["seconds_decoded"] = round(total, 3)
                want = info["seconds_declared"]
                if want and abs(total - want) / want > 0.001 and abs(total - want) > FRAME_TOL:
                    rep.fail(f"epoch {epoch}: decodes to {total:.3f} s, declared {want:.3f} s")
            gap = max_gap(probe_packets(joined, "a:0"))
            info["max_audio_gap_ms"] = round(gap * 1000, 1)
            if gap > 0.05:
                rep.fail(f"epoch {epoch}: audio gap of {gap * 1000:.0f} ms")
        info["seconds_declared"] = round(info["seconds_declared"], 3)
    return rep


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--json")
    a = ap.parse_args()
    rep = check(a.folder)
    out = {"folder": os.path.abspath(a.folder), "epochs": rep.epochs, "problems": rep.problems}
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)
    for e in rep.epochs:
        print(f"epoch {e['epoch']}: {e['segments']} segments, {e['seconds_declared']} s declared,"
              f" {e.get('seconds_decoded', '-')} s decoded, end {e['end_reason']}, states {e['states']},"
              f" codecs {e.get('codecs', '-')}, max audio gap {e.get('max_audio_gap_ms', '-')} ms")
    print(f"{len(rep.problems)} problems")
    for p in rep.problems[:50]:
        print("  ", p)
    sys.exit(1 if rep.problems else 0)


if __name__ == "__main__":
    main()
