"""Untrusted media checks before a segment is accepted (protocol v1, server rules).

`ffprobe` runs in a separate process with a 5 s wall-clock timeout, a CPU-time
limit and an address-space limit, and reads every packet, so truncated or
fuzzed data is caught here rather than in the playout. Accepted: H.264 video
no larger than 1920x1080 (either orientation) and AAC audio; a media segment's
video length must match the duration the client declared.
"""
from __future__ import annotations

import asyncio
import json
import os
import resource
from dataclasses import dataclass
from fractions import Fraction

from .store import Rejected

PROBE_TIMEOUT_S = 5.0
PROBE_MEMORY_BYTES = 512 * 1024 * 1024
DURATION_TOLERANCE_S = 0.25


@dataclass
class Probe:
    video_codec: str
    audio_codec: str | None
    width: int
    height: int
    fps: float | None
    video_packets: int
    audio_packets: int

    @property
    def codec(self) -> str:
        return ",".join(c for c in (self.video_codec, self.audio_codec) if c)


def _limits() -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_AS, (PROBE_MEMORY_BYTES, PROBE_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))


async def probe(path: str, ffprobe: str = "ffprobe") -> Probe:
    """Probe one file; Rejected(422) for anything that isn't clean H.264 (+ AAC)."""
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-count_packets",
        "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,nb_read_packets",
        "-of", "json", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, preexec_fn=_limits)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise Rejected(422, "media check timed out") from None
    if proc.returncode != 0 or err.strip():
        raise Rejected(422, f"media check failed: {err.decode(errors='replace').strip()[:200] or proc.returncode}")
    try:
        streams = json.loads(out).get("streams", [])
    except ValueError:
        raise Rejected(422, "media check failed: unreadable probe output") from None
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(video) != 1 or video[0].get("codec_name") != "h264":
        raise Rejected(422, "expected exactly one H.264 video stream")
    if len(audio) > 1 or (audio and audio[0].get("codec_name") != "aac"):
        raise Rejected(422, "expected at most one AAC audio stream")
    if len(streams) != len(video) + len(audio):
        raise Rejected(422, "unexpected extra streams")
    v = video[0]
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if not (0 < max(w, h) <= 1920 and 0 < min(w, h) <= 1080):
        raise Rejected(422, f"resolution {w}x{h} outside 1920x1080")
    fps = None
    try:
        r = Fraction(v.get("avg_frame_rate", "0/0"))
        fps = float(r) if r > 0 else None
    except (ValueError, ZeroDivisionError):
        pass
    return Probe("h264", audio[0]["codec_name"] if audio else None, w, h, fps,
                 int(v.get("nb_read_packets") or 0), int(audio[0].get("nb_read_packets") or 0) if audio else 0)


async def check_init(path: str, ffprobe: str = "ffprobe") -> Probe:
    return await probe(path, ffprobe)


async def check_segment(init_path: str, body_path: str, duration_us: int, final: bool, scratch_dir: str,
                        ffprobe: str = "ffprobe") -> Probe:
    """Probe init + segment joined, as the playout will read them."""
    joined = os.path.join(scratch_dir, os.path.basename(body_path) + ".probe.mp4")
    try:
        with open(init_path, "rb") as a, open(body_path, "rb") as b, open(joined, "wb") as out:
            out.write(a.read())
            while chunk := b.read(1 << 20):
                out.write(chunk)
        p = await probe(joined, ffprobe)
    finally:
        try:
            os.unlink(joined)
        except FileNotFoundError:
            pass
    if p.video_packets == 0:
        raise Rejected(422, "segment has no video")
    if p.fps and not final:
        got = p.video_packets / p.fps
        if abs(got - duration_us / 1e6) > DURATION_TOLERANCE_S:
            raise Rejected(422, f"segment holds {got:.2f} s of video, X-SR-Duration-Us says {duration_us / 1e6:.2f} s")
    return p
