"""tools/ios_check/check_recording.py on a manifest folder like the iPhone app writes."""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools/ios_check/check_recording.py"
pytestmark = pytest.mark.skipif(shutil.which("ffprobe") is None, reason="needs ffmpeg")

SCHEMA = """
CREATE TABLE recording (id INTEGER PRIMARY KEY, client_ref TEXT, title TEXT, mode TEXT, hold_max_delay_s INTEGER,
  broadcast_id TEXT, finish_requested INTEGER DEFAULT 0, finish_sent INTEGER DEFAULT 0);
CREATE TABLE inits (init_id INTEGER PRIMARY KEY, file TEXT, sha256 TEXT, bytes INTEGER, uploaded INTEGER DEFAULT 0);
CREATE TABLE epochs (epoch INTEGER PRIMARY KEY, last_sequence INTEGER, end_reason TEXT, end_sent INTEGER DEFAULT 0);
CREATE TABLE segments (epoch INTEGER, sequence INTEGER, init_id INTEGER, start_us INTEGER, duration_us INTEGER,
  bytes INTEGER, sha256 TEXT, file TEXT, final INTEGER DEFAULT 0, state TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, PRIMARY KEY (epoch, sequence));
"""


def recording(dst: Path, segs: Path) -> Path:
    """Two epochs from the 6 s test clip: 0 = segments 0-1, 1 = segment 2 (final)."""
    dst.mkdir()
    db = sqlite3.connect(dst / "manifest.sqlite")
    db.executescript(SCHEMA)
    init = (segs / "init.mp4").read_bytes()
    for e in (0, 1):
        (dst / f"init-{e}.mp4").write_bytes(init)
        db.execute("INSERT INTO inits VALUES (?, ?, ?, ?, 1)", (e, f"init-{e}.mp4", hashlib.sha256(init).hexdigest(), len(init)))
    start = 0
    for e, seqs in ((0, [0, 1]), (1, [2])):
        for n, i in enumerate(seqs):
            data = (segs / f"{i:05d}.m4s").read_bytes()
            name = f"seg-{e}-{n}.m4s"
            (dst / name).write_bytes(data)
            db.execute("INSERT INTO segments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'acknowledged', 1)",
                       (e, n, e, start, 2_000_000, len(data), hashlib.sha256(data).hexdigest(), name,
                        int(n == len(seqs) - 1)))
            start += 2_000_000
        db.execute("INSERT INTO epochs VALUES (?, ?, ?, 1)", (e, len(seqs) - 1, "interrupted" if e == 0 else "stopped"))
    db.commit()
    return dst


def run(folder: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECK), str(folder)], capture_output=True, text=True, timeout=300)


def test_clean_recording_passes(media, tmp_path):
    r = run(recording(tmp_path / "rec", media))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 problems" in r.stdout


def test_damage_is_found(media, tmp_path):
    rec = recording(tmp_path / "rec", media)
    (rec / "seg-0-1.m4s").write_bytes((rec / "seg-0-1.m4s").read_bytes()[:-5000])     # truncated
    (rec / "seg-9-9.m4s").write_bytes(b"x")                                         # stray
    db = sqlite3.connect(rec / "manifest.sqlite")
    db.execute("UPDATE segments SET start_us = 1000 WHERE epoch = 1")              # goes backwards
    db.execute("UPDATE segments SET duration_us = 1500000 WHERE epoch = 0 AND sequence = 0")  # wrong duration
    db.commit()
    r = run(rec)
    assert r.returncode == 1
    out = r.stdout
    assert "seg-0-1.m4s: checksum or size differs" in out
    assert "stray file seg-9-9.m4s" in out
    assert "goes back before the previous epoch" in out
    assert "0/0: video lasts" in out
