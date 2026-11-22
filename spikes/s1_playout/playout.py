"""Spike S1 playout: one RTMP publish on one clock, whatever the input does.

Reads fMP4 segments (init.mp4, 00000.m4s, ...) from a folder as they appear and
publishes a single 720p30 H.264 + AAC stream. States:

  starting     holding media until BUFFER_TARGET of footage is available
  live         footage at 1x
  holding      footage ran out: generated holding card + comfort noise
  catching_up  backlog above CATCHUP_ON: footage at SPEED (frame drop + atempo)
               until the backlog is back to BUFFER_TARGET

Output timestamps come only from the output frame counter, so source switches
never reach the encoder or the RTMP session. Throwaway spike code.
"""
import argparse, io, json, os, threading, time
from collections import deque
from fractions import Fraction

import av, numpy as np
from PIL import Image, ImageDraw, ImageFont

FPS, SR, W, H = 30, 48000, 1280, 720
SPF = SR // FPS                      # audio samples per output frame (1600)
SEG_FRAMES = 60                      # 2 s segments
BUFFER_TARGET = 4.0                  # seconds of footage ahead of the play position
CATCHUP_ON = 8.0                     # start catching up above this backlog
BITS, BLOCK = 24, 40                 # barcode, same layout as make_clip.py
HOLD_CODE = (1 << BITS) - 1          # barcode value on holding frames
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


class Decoder(threading.Thread):
    """Decodes segments in order, a few ahead of playout, on its own thread.

    One persistent audio decoder across segments (no AAC restart clicks); the
    video decoder is drained and flushed at each segment end, which is safe
    because every segment starts with an IDR frame.
    """

    def __init__(self, folder, ahead=3):
        super().__init__(daemon=True)
        self.folder, self.ahead = folder, ahead
        self.ready = deque()          # (index, [VideoFrame], np.ndarray (2, n))
        self.lock = threading.Lock()
        self.next = 0
        self.init = None
        self.vdec = self.adec = None
        self.resampler = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
        self.stop = False

    def path(self, i):
        return os.path.join(self.folder, f"{i:05d}.m4s")

    def run(self):
        while not self.stop:
            with self.lock:
                n = len(self.ready)
            if n >= self.ahead or not os.path.exists(self.path(self.next)):
                time.sleep(0.01)
                continue
            if self.init is None:
                with open(os.path.join(self.folder, "init.mp4"), "rb") as f:
                    self.init = f.read()
            item = self.decode(self.next)
            with self.lock:
                self.ready.append(item)
            self.next += 1

    def decode(self, i):
        with open(self.path(i), "rb") as f:
            data = self.init + f.read()
        c = av.open(io.BytesIO(data), format="mp4")
        vst, ast = c.streams.video[0], c.streams.audio[0]
        if self.vdec is None:
            self.vdec = av.CodecContext.create(vst.codec_context.name, "r")
            self.vdec.extradata = vst.codec_context.extradata
            self.adec = av.CodecContext.create(ast.codec_context.name, "r")
            self.adec.extradata = ast.codec_context.extradata
            self.adec.sample_rate = ast.codec_context.sample_rate
            self.adec.layout = ast.codec_context.layout
        frames, audio = [], []
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
        c.close()
        frames = [f if (f.width, f.height, f.format.name) == (W, H, "yuv420p")
                  else f.reformat(W, H, "yuv420p") for f in frames]
        a = np.concatenate(audio, axis=1) if audio else np.zeros((2, 0), np.float32)
        return i, frames, a


class AudioFifo:
    def __init__(self):
        self.chunks, self.n = deque(), 0

    def push(self, a):
        if a.shape[1]:
            self.chunks.append(a); self.n += a.shape[1]

    def pop(self, k):
        """Up to k samples (fewer if the fifo runs short)."""
        out, need = [], k
        while need and self.chunks:
            c = self.chunks[0]
            if c.shape[1] <= need:
                out.append(c); need -= c.shape[1]; self.chunks.popleft()
            else:
                out.append(c[:, :need]); self.chunks[0] = c[:, need:]; need = 0
        got = np.concatenate(out, axis=1) if out else np.zeros((2, 0), np.float32)
        self.n -= got.shape[1]
        return got


class Tempo:
    """atempo through a libavfilter graph; pitch is preserved."""

    def __init__(self, speed):
        self.g = av.filter.Graph()
        self.src = self.g.add_abuffer(format="fltp", sample_rate=SR, layout="stereo", time_base=Fraction(1, SR))
        at = self.g.add("atempo", f"{speed}")
        fmt = self.g.add("aformat", "sample_fmts=fltp:channel_layouts=stereo")
        self.sink = self.g.add("abuffersink")
        self.src.link_to(at); at.link_to(fmt); fmt.link_to(self.sink)
        self.g.configure()
        self.pts = 0

    def _frame(self, a):
        f = av.AudioFrame.from_ndarray(np.ascontiguousarray(a, np.float32), format="fltp", layout="stereo")
        f.sample_rate, f.pts, f.time_base = SR, self.pts, Fraction(1, SR)
        self.pts += a.shape[1]
        return f

    def push(self, a):
        self.src.push(self._frame(a))
        return self._pull()

    def flush(self):
        self.src.push(None)
        return self._pull(eof=True)

    def _pull(self, eof=False):
        out = []
        while True:
            try:
                out.append(self.sink.pull().to_ndarray())
            except (av.error.BlockingIOError, av.error.EOFError):
                break
        return out


class Holding:
    def __init__(self):
        self.font = ImageFont.truetype(FONT, 64)
        self.small = ImageFont.truetype(FONT, 36)
        self.rng = np.random.default_rng(1)

    def video(self, n):
        img = Image.new("RGB", (W, H), (25, 28, 40))
        d = ImageDraw.Draw(img)
        d.text((W // 2, 300), "Reconnecting…", font=self.font, fill=(240, 240, 240), anchor="mm")
        d.text((W // 2, 390), "The stream will continue in a moment", font=self.small, fill=(180, 180, 190), anchor="mm")
        t = time.strftime("%H:%M:%S")
        d.text((W // 2, 470), t, font=self.small, fill=(180, 180, 190), anchor="mm")
        arr = np.asarray(img).copy()
        x = int((n * 12) % (W - 60))
        arr[560:580, x:x + 60, :] = (90, 160, 255)       # moving bar: the output is alive
        arr[0:BLOCK, 0:BITS * BLOCK, :] = 255             # barcode = HOLD_CODE
        return av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuv420p")

    def audio(self, k):
        noise = (self.rng.standard_normal(k) * 10 ** (-55 / 20)).astype(np.float32)
        return np.stack([noise, noise])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--url", default="rtmp://127.0.0.1:1935/s1")
    ap.add_argument("--speed", type=float, default=1.25)
    ap.add_argument("--duration", type=float, default=1200.0)
    ap.add_argument("--log", required=True)
    ap.add_argument("--t0", type=float)
    a = ap.parse_args()

    log = open(a.log, "a")
    def ev(kind, **kw):
        log.write(json.dumps({"wall": time.time(), "event": kind, **kw}) + "\n"); log.flush()

    out = av.open(a.url, "w", format="flv")
    vs = out.add_stream("libx264", rate=FPS)
    vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
    vs.options = {"preset": "veryfast", "tune": "zerolatency",
                  "x264-params": "keyint=60:min-keyint=60:scenecut=0:bitrate=3000:vbv-maxrate=3000:vbv-bufsize=3000:nal-hrd=cbr"}
    as_ = out.add_stream("aac", rate=SR)
    as_.layout, as_.bit_rate = "stereo", 128000

    dec = Decoder(a.folder); dec.start()
    hold = Holding()
    fifo = AudioFifo()            # source audio at 1x, decoded, not yet played
    tempo_out = AudioFifo()       # audio already through atempo, not yet played
    tempo = None
    cur = deque()                 # decoded source video frames, in order, not yet played
    seg_index = -1                # index of the segment in `cur`
    vpos = 0.0                    # fractional advance in catch-up
    src_frames_played = 0         # source position (frames) of the next frame
    src_audio_used = 0            # source audio samples consumed
    state = "starting"
    ev("state", state=state, n=0, src=0)

    t0 = a.t0 or time.time()
    stats = {"late_max": 0.0, "late_ticks": 0}
    cpu_mark, wall_mark = time.process_time(), time.time()
    sec_stats = []

    def edge_seconds():
        """Seconds of contiguous footage on disk, from the start."""
        i = dec.next
        while os.path.exists(dec.path(i)):
            i += 1
        return i * SEG_FRAMES / FPS
    edge_cache = [0.0, 0]

    def backlog():
        if edge_cache[1] != n // 5:              # refresh 6 times a second
            edge_cache[0], edge_cache[1] = edge_seconds(), n // 5
        return edge_cache[0] - src_frames_played / FPS

    def next_segment():
        """Move the next decoded segment's video and audio onto the play queues."""
        nonlocal seg_index
        with dec.lock:
            if not dec.ready:
                return False
            i, frames, au = dec.ready.popleft()
        assert i == seg_index + 1, (i, seg_index)
        seg_index = i
        cur.extend(frames)
        fifo.push(au)
        return True

    def take_frame():
        """Next source frame, or None if footage has run out."""
        nonlocal src_frames_played
        if not cur and not next_segment():
            return None
        f = cur.popleft(); src_frames_played += 1
        return f

    def have_footage():
        if cur:
            return True
        with dec.lock:
            return bool(dec.ready)

    def set_state(s):
        nonlocal state, tempo
        if s == state:
            return
        if state == "catching_up" and tempo is not None:
            for c in tempo.flush():
                tempo_out.push(c)
            tempo = None
        if s == "catching_up":
            tempo = Tempo(a.speed)
        ev("state", state=s, n=n, src=src_frames_played, backlog=round(backlog(), 2))
        state = s

    n = 0
    last = None
    total = int(a.duration * FPS)
    while n < total:
        target = t0 + n / FPS
        now = time.time()
        if target > now:
            time.sleep(target - now)
        else:
            late = now - target
            stats["late_max"] = max(stats["late_max"], late)
            if late > 1 / FPS:
                stats["late_ticks"] += 1

        b = backlog()
        if state in ("starting", "holding"):
            if b >= BUFFER_TARGET and have_footage():
                set_state("catching_up" if b > CATCHUP_ON else "live")
        elif state == "live":
            if b > CATCHUP_ON:
                set_state("catching_up")
        elif state == "catching_up":
            if b <= BUFFER_TARGET:
                set_state("live")

        frame = None
        if state == "live":
            frame = take_frame()
        elif state == "catching_up":
            vpos += a.speed
            steps = int(vpos); vpos -= steps
            for _ in range(steps):
                f = take_frame()
                if f is None:
                    break
                frame = f
        if state in ("live", "catching_up") and frame is None:
            set_state("holding")

        # audio for this tick
        if state in ("live", "catching_up") or (state == "holding" and tempo_out.n):
            if state == "catching_up":
                while tempo_out.n < SPF:
                    chunk = fifo.pop(1024)
                    if chunk.shape[1] == 0:
                        if not next_segment():
                            break
                        continue
                    src_audio_used += chunk.shape[1]
                    for c in tempo.push(chunk):
                        tempo_out.push(c)
                au = tempo_out.pop(SPF)
            else:
                au = tempo_out.pop(SPF)
                if au.shape[1] < SPF and state != "holding":
                    if fifo.n < SPF - au.shape[1]:
                        next_segment()
                    more = fifo.pop(SPF - au.shape[1])
                    src_audio_used += more.shape[1]
                    au = np.concatenate([au, more], axis=1)
            if au.shape[1] < SPF:
                au = np.concatenate([au, hold.audio(SPF - au.shape[1])], axis=1)
        else:
            au = hold.audio(SPF)

        if frame is None:
            frame = hold.video(n)
        else:
            frame.pict_type = av.video.frame.PictureType.NONE
        frame.pts, frame.time_base = n, Fraction(1, FPS)
        for p in vs.encode(frame):
            out.mux(p)
        af = av.AudioFrame.from_ndarray(np.ascontiguousarray(au, np.float32), format="fltp", layout="stereo")
        af.sample_rate, af.pts, af.time_base = SR, n * SPF, Fraction(1, SR)
        for p in as_.encode(af):
            out.mux(p)

        n += 1
        if n % FPS == 0:
            cpu, wall = time.process_time(), time.time()
            sec_stats.append({"n": n, "state": state, "cpu": round((cpu - cpu_mark) / (wall - wall_mark), 3),
                              "backlog": round(b, 2), "src": src_frames_played,
                              "av_drift_ms": round((src_audio_used / SR - src_frames_played / FPS) * 1000, 1)})
            cpu_mark, wall_mark = cpu, wall
            if n % (10 * FPS) == 0:
                ev("tick", **sec_stats[-1], late_max=round(stats["late_max"], 3), late_ticks=stats["late_ticks"])

    for s in (vs, as_):
        for p in s.encode(None):
            out.mux(p)
    out.close()
    dec.stop = True
    ev("end", n=n, cpu_total=round(time.process_time(), 1), wall_total=round(time.time() - t0, 1),
       late_max=round(stats["late_max"], 3), late_ticks=stats["late_ticks"])
    with open(a.log.replace(".jsonl", ".seconds.json"), "w") as f:
        json.dump(sec_stats, f)


if __name__ == "__main__":
    main()
