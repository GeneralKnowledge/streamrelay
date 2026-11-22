"""Make the S1 test clip: 720p30 H.264 + AAC 48 kHz stereo.

Every frame carries its frame number twice: as a 24-bit barcode in the top row
(read by verify.py) and as text (for people). Audio is a continuous 1 kHz tone
at -12 dBFS, with a spoken passage mixed in from SPEECH_AT for 30 s (tone
lowered to -36 dBFS there) so catch-up speech can be listened to.
"""
import argparse, subprocess, sys, tempfile, os
from fractions import Fraction
import av, numpy as np
from PIL import Image, ImageDraw, ImageFont

FPS, SR, W, H = 30, 48000, 1280, 720
BITS, BLOCK = 24, 40
SPEECH_AT = 200.0
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
TEXT = ("This is the StreamRelay catch up listening test. The stream fell behind "
        "while the phone had no signal, and the relay is now playing the missed "
        "footage a little faster than real time so that viewers return to live "
        "without missing anything. Can you still follow what is being said? "
        "Numbers are a good test: four, seven, twelve, thirty one, one hundred and six.")

def barcode(arr, value):
    for i in range(BITS):
        bit = (value >> (BITS - 1 - i)) & 1
        arr[0:BLOCK, i * BLOCK:(i + 1) * BLOCK, :] = 255 if bit else 0

def speech_samples():
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "s.wav")
        subprocess.run(["espeak-ng", "-s", "160", "-w", wav, TEXT], check=True)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", wav, "-ar", str(SR), "-ac", "1",
                              "-f", "f32le", "-"], check=True, capture_output=True).stdout
    s = np.frombuffer(raw, np.float32)[: 30 * SR]
    return s / max(1e-6, np.abs(s).max()) * 0.5

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=int, default=1200)
    a = ap.parse_args()
    font = ImageFont.truetype(FONT, 48)
    out = av.open(a.out, "w")
    vs = out.add_stream("libx264", rate=FPS)
    vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
    # iPhone-like: 2 s closed GOP, B-frames allowed (AVAssetWriter may reorder).
    vs.options = {"preset": "veryfast", "crf": "20", "x264-params": "keyint=60:min-keyint=60:scenecut=0:open-gop=0"}
    as_ = out.add_stream("aac", rate=SR)
    as_.layout = "stereo"
    as_.bit_rate = 128000
    speech = speech_samples()
    base = np.zeros((H, W, 3), np.uint8)
    base[:, :, 0] = np.linspace(20, 90, W, dtype=np.uint8)[None, :]
    base[:, :, 2] = np.linspace(60, 120, H, dtype=np.uint8)[:, None]
    phase = 0
    for n in range(a.seconds * FPS):
        arr = base.copy()
        barcode(arr, n)
        x = int((n * 8) % (W - 80))
        arr[300:380, x:x + 80, :] = (240, 200, 40)
        img = Image.fromarray(arr)
        t = n / FPS
        ImageDraw.Draw(img).text((40, 600), f"frame {n:6d}   source {int(t // 60):02d}:{t % 60:06.3f}", font=font, fill=(255, 255, 255))
        vf = av.VideoFrame.from_ndarray(np.asarray(img), format="rgb24")
        vf.pts = n
        for p in vs.encode(vf):
            out.mux(p)
        idx = np.arange(phase, phase + SR // FPS)
        phase += SR // FPS
        ts = idx / SR
        in_speech = (ts >= SPEECH_AT) & (ts < SPEECH_AT + 30)
        tone = np.sin(2 * np.pi * 1000 * ts) * np.where(in_speech, 10 ** (-36 / 20), 10 ** (-12 / 20))
        sp = np.zeros_like(tone)
        si = idx - int(SPEECH_AT * SR)
        ok = (si >= 0) & (si < len(speech))
        sp[ok] = speech[si[ok]]
        mono = (tone + sp).astype(np.float32)
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
