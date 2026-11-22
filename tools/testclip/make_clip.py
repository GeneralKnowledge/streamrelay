"""Test source for the media-continuity tests: 720p30 H.264 + AAC 48 kHz stereo.

Every frame carries its frame number (= broadcast time x 30) as a 24-bit
barcode in the top row, read by tools/verify, and as text with the broadcast
time for people. Audio is a continuous 1 kHz tone at -12 dBFS with a 50 ms
2 kHz beep at the start of every second (A/V alignment marker). 2 s closed
GOPs with B-frames, like the iPhone's AVAssetWriter output.

    python3 tools/testclip/make_clip.py clip.mp4 --seconds 1200
"""
import argparse

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FPS, SR, W, H = 30, 48000, 1280, 720
BITS, BLOCK = 24, 40
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


def barcode(arr: np.ndarray, value: int) -> None:
    for i in range(BITS):
        bit = (value >> (BITS - 1 - i)) & 1
        arr[0:BLOCK, i * BLOCK:(i + 1) * BLOCK, :] = 255 if bit else 0


def audio_for(idx: np.ndarray) -> np.ndarray:
    ts = idx / SR
    beep = (idx % SR) < SR // 20
    tone = np.where(beep, np.sin(2 * np.pi * 2000 * ts) * 10 ** (-6 / 20), np.sin(2 * np.pi * 1000 * ts) * 10 ** (-12 / 20))
    return tone.astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=int, default=1200)
    a = ap.parse_args()
    try:
        font = ImageFont.truetype(FONT, 48)
    except OSError:
        font = ImageFont.load_default(48)
    out = av.open(a.out, "w")
    vs = out.add_stream("libx264", rate=FPS)
    vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
    vs.options = {"preset": "veryfast", "crf": "20", "x264-params": "keyint=60:min-keyint=60:scenecut=0:open-gop=0"}
    as_ = out.add_stream("aac", rate=SR)
    as_.layout = "stereo"
    as_.bit_rate = 128000
    base = np.zeros((H, W, 3), np.uint8)
    base[:, :, 0] = np.linspace(20, 90, W, dtype=np.uint8)[None, :]
    base[:, :, 2] = np.linspace(60, 120, H, dtype=np.uint8)[:, None]
    for n in range(a.seconds * FPS):
        arr = base.copy()
        barcode(arr, n)
        x = int((n * 8) % (W - 80))
        arr[300:380, x:x + 80, :] = (240, 200, 40)
        img = Image.fromarray(arr)
        t = n / FPS
        ImageDraw.Draw(img).text((40, 600), f"frame {n:6d}   broadcast {int(t // 3600):d}:{int(t // 60) % 60:02d}:{t % 60:06.3f}",
                                 font=font, fill=(255, 255, 255))
        vf = av.VideoFrame.from_ndarray(np.asarray(img), format="rgb24")
        vf.pts = n
        for p in vs.encode(vf):
            out.mux(p)
        idx = np.arange(n * SR // FPS, (n + 1) * SR // FPS)
        mono = audio_for(idx)
        af = av.AudioFrame.from_ndarray(np.stack([mono, mono]), format="fltp", layout="stereo")
        af.sample_rate = SR
        af.pts = int(idx[0])
        for p in as_.encode(af):
            out.mux(p)
    for s in (vs, as_):
        for p in s.encode(None):
            out.mux(p)
    out.close()


if __name__ == "__main__":
    main()
