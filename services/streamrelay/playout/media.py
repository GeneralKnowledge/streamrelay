"""Media pieces of the playout: segment decoder, audio FIFO, generated cards.

Adapted from spike S1 (spikes/s1_playout/playout.py), which passed its gate.
"""
from __future__ import annotations

import io
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FPS, SR, W, H = 30, 48000, 1280, 720
SPF = SR // FPS                      # audio samples per output frame (1600)
BITS, BLOCK = 24, 40                 # barcode layout of tools/testclip
HOLD_CODE = (1 << BITS) - 1          # barcode value on generated frames
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"]

Key = tuple[int, int]                # (epoch, sequence)
log = logging.getLogger("streamrelay.playout")


@dataclass
class Decoded:
    key: Key
    frames: list[av.VideoFrame] = field(default_factory=list)
    audio: np.ndarray = field(default_factory=lambda: np.zeros((2, 0), np.float32))
    ok: bool = True
    error: str = ""


class Decoder(threading.Thread):
    """Decodes segments in play order, a few ahead of the playout, on its own thread.

    One persistent audio decoder (no AAC restart clicks at segment joins); the
    video decoder is drained and flushed at each segment end, which is safe
    because every segment starts with an IDR frame. Decoders are rebuilt when
    a segment needs a different init segment.
    """

    def __init__(self, read: Callable[[Key], tuple[bytes, int, bytes] | None], succ: Callable[[Key], Key], ahead: int = 3):
        super().__init__(daemon=True, name="decoder")
        self.read, self.succ, self.ahead = read, succ, ahead
        self.lock = threading.Lock()
        self.ready: deque[Decoded] = deque()
        self.want: Key | None = None
        self.gen = 0
        self.init_id: int | None = None
        self.vdec: Any = None
        self.adec: Any = None
        self.resampler = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
        self.stop = False

    def seek(self, key: Key) -> None:
        with self.lock:
            self.want, self.gen = key, self.gen + 1
            self.ready.clear()

    def pop(self, key: Key) -> Decoded | None:
        with self.lock:
            if self.ready and self.ready[0].key == key:
                return self.ready.popleft()
        return None

    def run(self) -> None:
        while not self.stop:
            with self.lock:
                key, gen, full = self.want, self.gen, len(self.ready) >= self.ahead
            src = None if key is None or full else self.read(key)
            if src is None:
                time.sleep(0.02)
                continue
            assert key is not None
            item = self.decode(key, *src)
            with self.lock:
                if gen == self.gen:
                    self.ready.append(item)
                    self.want = self.succ(key)

    def decode(self, key: Key, init: bytes, init_id: int, body: bytes) -> Decoded:
        try:
            c: Any = av.open(io.BytesIO(init + body), format="mp4")
        except av.FFmpegError as e:
            return Decoded(key, ok=False, error=str(e))
        try:
            vst, ast = c.streams.video[0], c.streams.audio[0]
            if self.vdec is None or self.init_id != init_id:
                self.vdec = av.CodecContext.create(vst.codec_context.name, "r")
                self.vdec.extradata = vst.codec_context.extradata
                self.adec = av.CodecContext.create(ast.codec_context.name, "r")
                self.adec.extradata = ast.codec_context.extradata
                self.adec.sample_rate = ast.codec_context.sample_rate
                self.adec.layout = ast.codec_context.layout
                self.init_id = init_id
            assert self.adec is not None
            frames: list[av.VideoFrame] = []
            audio: list[np.ndarray] = []
            for pkt in c.demux(vst, ast):
                if pkt.size == 0:
                    continue
                if pkt.stream is vst:
                    frames.extend(self.vdec.decode(pkt))
                else:
                    for af in self.adec.decode(pkt):
                        for r in self.resampler.resample(af):
                            audio.append(r.to_ndarray())
            frames.extend(self.vdec.decode(None))
            self.vdec.flush_buffers()
        except (av.FFmpegError, IndexError, ValueError) as e:
            self.vdec = None                     # rebuild decoders after a bad segment
            return Decoded(key, ok=False, error=str(e)[:200])
        finally:
            c.close()
        if not frames:
            return Decoded(key, ok=False, error="no video frames")
        frames = [f if (f.width, f.height, f.format.name) == (W, H, "yuv420p") else f.reformat(W, H, "yuv420p")
                  for f in frames]
        a = np.concatenate(audio, axis=1) if audio else np.zeros((2, 0), np.float32)
        return Decoded(key, frames, a)


class AudioFifo:
    def __init__(self) -> None:
        self.chunks: deque[np.ndarray] = deque()
        self.n = 0

    def clear(self) -> None:
        self.chunks.clear()
        self.n = 0

    def push(self, a: np.ndarray) -> None:
        if a.shape[1]:
            self.chunks.append(a)
            self.n += a.shape[1]

    def pop(self, k: int) -> np.ndarray:
        """Up to k samples (fewer if the FIFO runs short)."""
        out, need = [], k
        while need and self.chunks:
            c = self.chunks[0]
            if c.shape[1] <= need:
                out.append(c)
                need -= c.shape[1]
                self.chunks.popleft()
            else:
                out.append(c[:, :need])
                self.chunks[0] = c[:, need:]
                need = 0
        got = np.concatenate(out, axis=1) if out else np.zeros((2, 0), np.float32)
        self.n -= got.shape[1]
        return got


CARD_TEXT = {
    "starting": ("Starting soon", "The broadcast will begin in a moment"),
    "holding": ("Reconnecting…", "The stream will continue in a moment"),
    "interrupted": ("Camera interrupted", "Continuing…"),
    "ended": ("Broadcast ended", "Thanks for watching"),
}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


class Cards:
    """Generated frames (holding, starting, interrupted, ended) and comfort noise.

    The text is drawn once per second and cached as a YUV image; each frame only
    copies it and moves a bar, so the encoder never sees a static picture and
    the card costs less than decoding footage (S1 drew text every frame).
    """

    def __init__(self) -> None:
        self.big, self.small = _font(64), _font(36)
        self.rng = np.random.default_rng(1)
        self.cache: dict[tuple[str, str], np.ndarray] = {}

    def _base(self, kind: str, line3: str) -> np.ndarray:
        k = (kind, line3)
        if k not in self.cache:
            if len(self.cache) > 8:
                self.cache.clear()
            img = Image.new("RGB", (W, H), (25, 28, 40))
            d = ImageDraw.Draw(img)
            a, b = CARD_TEXT[kind]
            d.text((W // 2, 300), a, font=self.big, fill=(240, 240, 240), anchor="mm")
            d.text((W // 2, 390), b, font=self.small, fill=(180, 180, 190), anchor="mm")
            if line3:
                d.text((W // 2, 470), line3, font=self.small, fill=(180, 180, 190), anchor="mm")
            arr = np.asarray(img).copy()
            arr[0:BLOCK, 0:BITS * BLOCK, :] = 255        # barcode = HOLD_CODE
            self.cache[k] = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuv420p").to_ndarray()
        return self.cache[k]

    def video(self, kind: str, n: int, line3: str = "") -> av.VideoFrame:
        y = self._base(kind, line3).copy()
        x = int((n * 12) % (W - 60))
        y[560:580, x:x + 60] = 200                       # moving bar (luma plane): the output is alive
        return av.VideoFrame.from_ndarray(y, format="yuv420p")

    def audio(self, k: int) -> np.ndarray:
        noise = (self.rng.standard_normal(k) * 10 ** (-55 / 20)).astype(np.float32)
        return np.stack([noise, noise])
