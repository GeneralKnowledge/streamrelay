"""tools/verify passes a clean stream and fails a broken one with the right reason."""
from __future__ import annotations

import importlib.util
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
spec = importlib.util.spec_from_file_location("verify", os.path.join(ROOT, "tools/verify/verify.py"))
assert spec and spec.loader
verify = importlib.util.module_from_spec(spec)
sys.modules["verify"] = verify
spec.loader.exec_module(verify)
HOLD, FPS = verify.HOLD, verify.FPS


def clean_codes() -> list[int]:
    # 10 s of footage, 5 s of holding, then footage again from source frame 600 (skip to live)
    return list(range(300)) + [HOLD] * 150 + list(range(600, 900))


def test_clean_order_passes():
    assert verify.check_order(clean_codes()).passed


def test_repeated_frame_fails():
    codes = clean_codes()
    codes[100] = codes[99]
    c = verify.check_order(codes)
    assert not c.passed and "1 repeated" in c.value


def test_out_of_order_fails():
    codes = clean_codes()
    codes[100], codes[101] = codes[101], codes[100]
    c = verify.check_order(codes)
    assert not c.passed and "1 out of order" in c.value


def test_pts_gap_and_reorder_fail():
    pts = [i / FPS for i in range(100)]
    assert all(c.passed for c in verify.check_video_pts(pts))
    gap = pts[:50] + [p + 0.1 for p in pts[50:]]
    res = verify.check_video_pts(gap)
    assert res[0].passed and not res[1].passed
    back = pts[:50] + [pts[48]] + pts[51:]
    assert not verify.check_video_pts(back)[0].passed


def test_audio_gap_fails():
    pk = [(i * 0.02133, 0.02133) for i in range(100)]
    assert verify.check_audio(pk).passed
    pk[50] = (pk[50][0] + 0.03, 0.02133)
    assert not verify.check_audio(pk).passed


def test_gap_timing():
    codes = clean_codes()            # holding from recorded frame 300 (10 s) to 449, footage back at 450 (15 s)
    t0 = 1000.0
    ok = verify.gap_timing(codes, 0, t0, [(t0 + 5, t0 + 12)], buffer_target=8, resume_threshold=10)
    assert [c.passed for c in ok] == [True, True]
    assert ok[0].value == "5.0 s" and ok[1].value == "3.0 s"
    late = verify.gap_timing(codes, 0, t0, [(t0 + 0, t0 + 2)], buffer_target=8, resume_threshold=10)
    assert [c.passed for c in late] == [True, False]          # holding at 10 s is fine, footage at +13 s is late
    never = verify.gap_timing(list(range(900)), 0, t0, [(t0 + 5, t0 + 12)], buffer_target=8, resume_threshold=10)
    assert not never[0].passed and never[0].value == "never"


MTX_OK = """2026/11/22 17:02:50 INF [RTMP] [conn 127.0.0.1:1] is publishing to path 'live/b', 2 tracks
2026/11/22 17:02:50 INF [RTMP] [conn 127.0.0.1:2] is reading from path 'live/b', 2 tracks
2026/11/22 17:04:08 INF [RTMP] [conn 127.0.0.1:1] closed: EOF
2026/11/22 17:04:08 INF [RTMP] [conn 127.0.0.1:2] closed: terminated
"""

MTX_RECONNECT = """2026/11/22 17:02:50 INF [RTMP] [conn 127.0.0.1:1] is publishing to path 'live/b', 2 tracks
2026/11/22 17:02:50 INF [RTMP] [conn 127.0.0.1:2] is reading from path 'live/b', 2 tracks
2026/11/22 17:03:00 INF [RTMP] [conn 127.0.0.1:1] closed: EOF
2026/11/22 17:03:00 INF [RTMP] [conn 127.0.0.1:2] closed: terminated
2026/11/22 17:03:02 INF [RTMP] [conn 127.0.0.1:3] is publishing to path 'live/b', 2 tracks
2026/11/22 17:03:03 INF [RTMP] [conn 127.0.0.1:4] is reading from path 'live/b', 2 tracks
2026/11/22 17:04:08 INF [RTMP] [conn 127.0.0.1:3] closed: EOF
2026/11/22 17:04:08 INF [RTMP] [conn 127.0.0.1:4] closed: terminated
"""


def test_sessions():
    assert verify.count_sessions(MTX_OK, "live/b") == (1, 1)
    assert verify.viewer_closed_after_publisher(MTX_OK)
    assert verify.count_sessions(MTX_RECONNECT, "live/b") == (2, 2)          # fails the "1 session" gates
    assert not verify.viewer_closed_after_publisher(MTX_RECONNECT)           # the viewer was cut mid-broadcast
