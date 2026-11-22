"""Playout for one broadcast: one RTMP publish on one clock, whatever uploads do.

States (docs/architecture/playout.md):
  starting   "Starting soon" card until buffer_target of footage is stored
  live       footage at 1x, about buffer_target behind the newest stored footage
  holding    footage ran out: "Reconnecting…" card and comfort noise
  finishing  the client called finish: play what is left, wait for missing
             segments up to the drain wait, then the end card for 10 s
  finished   output closed

M1 implements skip-to-live: leaving holding jumps to the newest contiguous
footage minus buffer_target, once resume_threshold of it is stored. Catch-up
and hold modes arrive in M4; until then they play as skip-to-live.

Output timestamps come only from the output frame counter, so nothing the
input does reaches the encoder's clock or the RTMP session. Progress is
checkpointed every second; a restarted process resumes from the checkpoint
(at most about a second repeats).

    python -m streamrelay.playout.engine --broadcast <id> [--log playout.jsonl]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from collections import deque
from fractions import Fraction
from typing import Any

import av
import numpy as np
import psycopg

from ..config import Settings
from .media import FPS, SPF, SR, AudioFifo, Cards, Decoded, Decoder, H, Key, W

log = logging.getLogger("streamrelay.playout")
END_CARD_S = 10
INTERRUPTED_FRAMES = 2 * FPS


class Index:
    """What is stored, refreshed from Postgres by a poller thread four times a second."""

    def __init__(self, db_url: str, bid: uuid.UUID, store_root: str):
        self.db_url, self.bid, self.root = db_url, bid, store_root
        self.lock = threading.Lock()
        self.segments: dict[Key, tuple[str, int, int, int]] = {}  # key -> (storage_key, init_id, start_us, duration_us)
        self.inits: dict[int, str] = {}
        self.epochs: dict[int, int | None] = {}                     # epoch -> last_sequence (None: not ended)
        self.finish_at: float | None = None
        self.deleted = False
        self.buffer_target_ms = 8000
        self.mode = "skip_to_live"
        self.low: Key = (0, 0)                                      # forget keys below this (already passed)
        self.loaded = threading.Event()
        self.stop = False

    def poll_forever(self) -> None:
        while not self.stop:
            try:
                with psycopg.connect(self.db_url, autocommit=True) as conn:
                    while not self.stop:
                        self.refresh(conn)
                        self.loaded.set()
                        time.sleep(0.25)
            except psycopg.Error as e:
                log.warning("index refresh failed: %s", e)
                time.sleep(1)

    def refresh(self, conn: psycopg.Connection) -> None:
        with self.lock:
            low = self.low
        segs = conn.execute(
            "SELECT epoch, sequence, storage_key, init_id, start_us, duration_us FROM segments"
            " WHERE broadcast_id = %s AND (epoch, sequence) >= (%s, %s)", (self.bid, *low)).fetchall()
        inits = conn.execute("SELECT init_id, storage_key FROM inits WHERE broadcast_id = %s", (self.bid,)).fetchall()
        eps = conn.execute("SELECT epoch, last_sequence FROM epochs WHERE broadcast_id = %s", (self.bid,)).fetchall()
        b = conn.execute("SELECT extract(epoch FROM finish_requested_at), deleted_at IS NOT NULL, buffer_target_ms,"
                         " catch_up_mode FROM broadcasts WHERE id = %s", (self.bid,)).fetchone()
        with self.lock:
            self.segments = {(e, s): (k, i, st, d) for e, s, k, i, st, d in segs}
            self.inits = dict(inits)
            self.epochs = dict(eps)
            if b:
                self.finish_at = float(b[0]) if b[0] is not None else None
                self.deleted, self.buffer_target_ms, self.mode = b[1], b[2], b[3]

    # --- queries used by the playout and the decoder (all under the lock) ---
    def succ(self, key: Key) -> Key:
        e, s = key
        with self.lock:
            last = self.epochs.get(e)
            if last is None or s < last:
                return (e, s + 1)
            e += 1
            while self.epochs.get(e) == -1:           # empty epochs: nothing to play
                e += 1
            return (e, 0)

    def pred(self, key: Key) -> Key | None:
        e, s = key
        with self.lock:
            if s > 0:
                return (e, s - 1)
            e -= 1
            while e >= 0 and self.epochs.get(e) == -1:
                e -= 1
            if e < 0 or self.epochs.get(e) is None:
                return None
            return (e, self.epochs[e])  # type: ignore[return-value]

    def has(self, key: Key) -> bool:
        with self.lock:
            return key in self.segments and self.segments[key][1] in self.inits

    def duration_s(self, key: Key) -> float:
        with self.lock:
            return self.segments[key][3] / 1e6

    def newest(self) -> Key | None:
        with self.lock:
            return max(self.segments) if self.segments else None

    def end_us(self, key: Key) -> int:
        with self.lock:
            _, _, st, d = self.segments[key]
            return st + d

    def start_us(self, key: Key) -> int | None:
        with self.lock:
            v = self.segments.get(key)
            return v[2] if v else None

    def complete_after(self, key: Key) -> bool:
        """True when `key` is past the last segment of a finished broadcast."""
        with self.lock:
            if self.finish_at is None or not self.epochs or any(v is None for v in self.epochs.values()):
                return False
            top = max(self.epochs)
            return key[0] > top or (key[0] == top and key[1] > (self.epochs[top] or -1))

    def read(self, key: Key) -> tuple[bytes, int, bytes] | None:
        with self.lock:
            v = self.segments.get(key)
            init_key = self.inits.get(v[1]) if v else None
        if v is None or init_key is None:
            return None
        with open(os.path.join(self.root, init_key), "rb") as f:
            init = f.read()
        with open(os.path.join(self.root, v[0]), "rb") as f:
            body = f.read()
        return init, v[1], body


class Writer(threading.Thread):
    """Database writes off the real-time loop: marks, checkpoints, state, events."""

    def __init__(self, db_url: str, bid: uuid.UUID):
        super().__init__(daemon=True, name="writer")
        self.db_url, self.bid = db_url, bid
        self.q: queue.Queue[tuple[str, tuple[Any, ...]] | None] = queue.Queue()

    def put(self, sql: str, *args: Any) -> None:
        self.q.put((sql, args))

    def close(self) -> None:
        self.q.put(None)
        self.join(timeout=10)

    def run(self) -> None:
        conn: psycopg.Connection | None = None
        while True:
            item = self.q.get()
            if item is None:
                break
            for attempt in range(5):
                try:
                    if conn is None or conn.closed:
                        conn = psycopg.connect(self.db_url, autocommit=True)
                    conn.execute(item[0], item[1])  # type: ignore[arg-type]
                    break
                except psycopg.Error as e:
                    log.warning("write failed (%s): %s", attempt, e)
                    conn = None
                    time.sleep(0.5)
        if conn is not None:
            conn.close()


class Playout:
    def __init__(self, settings: Settings, bid: uuid.UUID, log_path: str | None = None):
        self.s, self.bid = settings, bid
        self.index = Index(settings.database_url, bid, settings.store_dir)
        self.writer = Writer(settings.database_url, bid)
        self.decoder = Decoder(self.index.read, self.index.succ)
        self.cards = Cards()
        self.fifo = AudioFifo()               # source audio, decoded, not yet played
        self.cur: deque[tuple[Key, int, av.VideoFrame]] = deque()   # source frames taken, not yet played
        self.next_key: Key = (0, 0)           # first segment not yet taken from the decoder
        self.pos: Key | None = None           # key of the last frame played
        self.pos_frame = 0                    # frames of that segment played
        self.shown_epoch: int | None = None
        self.interstitial = 0                 # frames of the "Camera interrupted" card still to show
        self.resume_skip: tuple[Key, int] | None = None   # (key, frames) to drop when resuming from a checkpoint
        self.state = "starting"
        self.showing = ""
        self.hold_since = time.time()
        self.end_frames = 0
        self.n = 0
        self.logf = open(log_path, "a") if log_path else None

    # ---- logging -----------------------------------------------------------
    def ev(self, kind: str, **kw: Any) -> None:
        if self.logf:
            self.logf.write(json.dumps({"wall": round(time.time(), 4), "event": kind, "n": self.n, **kw}) + "\n")
            self.logf.flush()

    def show(self, what: str) -> None:
        if what != self.showing:
            self.ev("showing", what=what, key=self.pos if what == "footage" else self.next_key)
            self.showing = what

    def set_state(self, s: str, **kw: Any) -> None:
        if s == self.state:
            return
        log.info("%s: %s -> %s %s", self.bid, self.state, s, kw)
        self.ev("state", state=s, prev=self.state, key=self.next_key, **kw)
        db_state = s if s in ("starting", "live", "holding", "finished") else "finishing"
        if self.index.finish_at is not None and s != "finished":
            db_state = "finishing"
        self.writer.put("UPDATE broadcasts SET state = %s, finished_at = CASE WHEN %s = 'finished' THEN now() END"
                        " WHERE id = %s", db_state, db_state, self.bid)
        self.writer.put("INSERT INTO events (broadcast_id, kind, detail) VALUES (%s, 'state_change', %s)",
                        self.bid, json.dumps({"state": s, "prev": self.state, "n": self.n, **kw}))
        if s == "holding":
            self.hold_since = time.time()
        self.state = s

    # ---- source handling ---------------------------------------------------
    def contiguous_from(self, key: Key, limit_s: float = 1e9) -> float:
        total, k = 0.0, key
        while total < limit_s and self.index.has(k):
            total += self.index.duration_s(k)
            k = self.index.succ(k)
        return total

    def ahead_s(self) -> float:
        return len(self.cur) / FPS + self.contiguous_from(self.next_key, 60)

    def jump_target(self, threshold_s: float) -> Key | None:
        """Skip to live: the key buffer_target behind the newest footage, once the
        contiguous run ending at the newest footage holds threshold_s."""
        newest = self.index.newest()
        if newest is None or newest < self.next_key or not self.index.has(newest):
            return None
        run, k, target = 0.0, newest, newest
        buffer_s = self.index.buffer_target_ms / 1000
        while True:
            run += self.index.duration_s(k)
            if run <= buffer_s + 0.01:
                target = k
            p = self.index.pred(k)
            if p is None or not self.index.has(p):
                break
            k = p
        if run + 1e-6 < threshold_s:
            return None
        return max(target, self.next_key)

    def seek(self, key: Key) -> None:
        if key != self.next_key:
            self.ev("skip", frm=self.next_key, to=key)
            self.writer.put("UPDATE segments SET played_at = now(), play_result = 'skipped' WHERE broadcast_id = %s"
                            " AND played_at IS NULL AND (epoch, sequence) >= (%s, %s) AND (epoch, sequence) < (%s, %s)",
                            self.bid, *self.next_key, *key)
        self.next_key = key
        self.decoder.seek(key)
        self.cur.clear()
        self.fifo.clear()

    def next_ready(self) -> bool:
        with self.decoder.lock:
            return bool(self.decoder.ready) and self.decoder.ready[0].key == self.next_key

    def next_segment(self) -> bool:
        """Take the next decoded segment's video and audio onto the play queues. False if it isn't ready."""
        while True:
            d: Decoded | None = self.decoder.pop(self.next_key)
            if d is None:
                return False
            self.next_key = self.index.succ(d.key)
            with self.index.lock:
                self.index.low = d.key
            if not d.ok:
                log.warning("%s: segment %s damaged: %s", self.bid, d.key, d.error)
                self.ev("damaged", key=d.key, error=d.error)
                self.writer.put("UPDATE segments SET played_at = now(), play_result = 'damaged' WHERE broadcast_id = %s"
                                " AND epoch = %s AND sequence = %s AND played_at IS NULL", self.bid, *d.key)
                continue
            frames, audio = d.frames, d.audio
            if self.resume_skip and self.resume_skip[0] == d.key:      # resuming mid-segment after a restart
                k = self.resume_skip[1]
                frames, audio = frames[k:], audio[:, k * SPF:]
            self.resume_skip = None
            self.cur.extend((d.key, i, f) for i, f in enumerate(frames, start=len(d.frames) - len(frames)))
            self.fifo.push(audio)
            self.writer.put("UPDATE segments SET played_at = now(), play_result = 'played' WHERE broadcast_id = %s"
                            " AND epoch = %s AND sequence = %s AND played_at IS NULL", self.bid, *d.key)
            return True

    def take_frame(self) -> av.VideoFrame | None:
        """The next source frame, or None if footage has run out."""
        if not self.cur and not self.next_segment():
            return None
        key, i, f = self.cur[0]
        if self.shown_epoch is not None and key[0] != self.shown_epoch:
            self.shown_epoch = key[0]
            self.interstitial = INTERRUPTED_FRAMES     # a new epoch: "Camera interrupted" first
            return None
        self.shown_epoch = key[0]
        self.cur.popleft()
        self.pos, self.pos_frame = key, i + 1
        return f

    def take_audio(self) -> np.ndarray:
        au = self.fifo.pop(SPF)
        while au.shape[1] < SPF and self.next_segment():   # this segment's audio ran short: the next one continues it
            au = np.concatenate([au, self.fifo.pop(SPF - au.shape[1])], axis=1)
        if au.shape[1] < SPF:
            au = np.concatenate([au, self.cards.audio(SPF - au.shape[1])], axis=1)
        return au

    # ---- checkpoint ----------------------------------------------------------
    def checkpoint(self) -> None:
        if self.cur:
            key, frame = self.cur[0][0], self.cur[0][1]
        else:
            key, frame = self.next_key, 0
        start = self.index.start_us(key)
        pos = start + frame * 1_000_000 // FPS if start is not None else None
        newest = self.index.newest()
        behind = None
        if newest is not None and pos is not None and self.index.has(newest):
            behind = (self.index.end_us(newest) - pos) // 1000
        state = self.state if self.state in ("starting", "live", "holding", "finished") else "finishing"
        self.writer.put(
            "INSERT INTO playout_checkpoints (broadcast_id, state, epoch, sequence, frame_offset, speed, output_frames,"
            " position_us, behind_live_ms, updated_at) VALUES (%s, %s, %s, %s, %s, 1.0, %s, %s, %s, now())"
            " ON CONFLICT (broadcast_id) DO UPDATE SET state = EXCLUDED.state, epoch = EXCLUDED.epoch,"
            " sequence = EXCLUDED.sequence, frame_offset = EXCLUDED.frame_offset, output_frames = EXCLUDED.output_frames,"
            " position_us = EXCLUDED.position_us, behind_live_ms = EXCLUDED.behind_live_ms, updated_at = now()",
            self.bid, state, key[0], key[1], frame, self.n, pos, behind)
        # anything below the play point that is still unplayed arrived too late for live
        low = self.cur[0][0] if self.cur else self.next_key
        self.writer.put("UPDATE segments SET played_at = now(), play_result = 'skipped' WHERE broadcast_id = %s"
                        " AND played_at IS NULL AND (epoch, sequence) < (%s, %s)", self.bid, *low)

    def restore(self) -> None:
        """Resume from the last checkpoint after a restart: same position, output clock continues."""
        with psycopg.connect(self.s.database_url) as conn:
            row = conn.execute("SELECT state, epoch, sequence, frame_offset, output_frames FROM playout_checkpoints"
                               " WHERE broadcast_id = %s", (self.bid,)).fetchone()
        if row is None:
            return
        state, e, s, offset, frames = row
        self.n = int(frames)
        self.next_key = (e, s)
        self.resume_skip = ((e, s), int(offset)) if offset else None
        self.decoder.seek(self.next_key)
        with self.index.lock:
            self.index.low = self.next_key
        # footage states resume in place (nothing skipped); the rest resume as they were
        self.state = {"live": "resuming", "catching_up": "resuming"}.get(state, str(state))
        self.ev("restored", checkpoint={"state": state, "key": [e, s], "offset": offset, "output_frames": frames})
        log.info("%s: resumed from checkpoint %s", self.bid, row)

    # ---- main loop -----------------------------------------------------------
    def run(self) -> int:
        threading.Thread(target=self.index.poll_forever, daemon=True, name="index").start()
        if not self.index.loaded.wait(15):
            raise RuntimeError("database not reachable")
        self.writer.start()
        self.restore()
        if self.decoder.want is None:
            self.decoder.seek(self.next_key)
        if self.state == "finished" or self.index.deleted:
            self.writer.close()
            return 0
        self.decoder.start()
        url = self.s.rtmp_url_template.format(broadcast_id=self.bid)
        out = av.open(url, "w", format="flv")
        vs = out.add_stream("libx264", rate=FPS)
        vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
        vs.options = {"preset": "veryfast", "tune": "zerolatency", "profile": "high",
                      "x264-params": "keyint=60:min-keyint=60:scenecut=0:bitrate=4000:vbv-maxrate=4000:"
                                     "vbv-bufsize=4000:nal-hrd=cbr"}
        as_ = out.add_stream("aac", rate=SR)
        as_.layout, as_.bit_rate = "stereo", 128000
        t0 = time.time()
        self.ev("start", t0=t0, url=url, state=self.state)
        base_n = self.n
        cpu_mark, wall_mark = time.process_time(), time.time()
        late_max, late_ticks = 0.0, 0
        while True:
            target = t0 + (self.n - base_n) / FPS
            now = time.time()
            if target > now:
                time.sleep(target - now)
            else:
                late = now - target
                late_max = max(late_max, late)
                late_ticks += late > 1 / FPS
            if self.index.deleted:
                self.ev("deleted")
                break
            frame, au, card = self.tick()
            if card == "done":
                break
            if frame is None:
                frame = self.cards.video(card, self.n, self.card_line(card))
            else:
                frame.pict_type = av.video.frame.PictureType.NONE
            frame.pts, frame.time_base = self.n, Fraction(1, FPS)
            for p in vs.encode(frame):
                out.mux(p)
            af = av.AudioFrame.from_ndarray(np.ascontiguousarray(au, np.float32), format="fltp", layout="stereo")
            af.sample_rate, af.pts, af.time_base = SR, self.n * SPF, Fraction(1, SR)
            for p in as_.encode(af):
                out.mux(p)
            self.n += 1
            if self.n % FPS == 0:
                self.checkpoint()
                cpu, wall = time.process_time(), time.time()
                self.ev("second", state=self.state, showing=self.showing, cpu=round((cpu - cpu_mark) / (wall - wall_mark), 3),
                        ahead_s=round(self.ahead_s(), 2), key=self.pos, late_max=round(late_max, 3), late_ticks=late_ticks)
                cpu_mark, wall_mark = cpu, wall
        for s in (vs, as_):
            for p in s.encode(None):
                out.mux(p)
        out.close()
        self.set_state("finished")
        self.checkpoint()
        self.ev("end", cpu_total=round(time.process_time(), 1), wall_total=round(time.time() - t0, 1),
                late_max=round(late_max, 3), late_ticks=late_ticks)
        self.writer.close()
        self.decoder.stop = True
        self.index.stop = True
        return 0

    def card_line(self, card: str) -> str:
        if card == "holding":
            el = int(time.time() - self.hold_since)
            return f"{el // 60}:{el % 60:02d}"
        return ""

    def tick(self) -> tuple[av.VideoFrame | None, np.ndarray, str]:
        """Decide this output frame: (footage frame or None, audio, card kind for a generated frame)."""
        idx = self.index
        if idx.finish_at is not None and self.state in ("starting", "live", "holding", "resuming"):
            self.set_state("finishing")

        if self.state == "ended":
            self.end_frames += 1
            return None, self.cards.audio(SPF), "done" if self.end_frames > END_CARD_S * FPS else "ended"

        if self.state in ("starting", "holding"):
            threshold = idx.buffer_target_ms / 1000 if self.state == "starting" else self.s.resume_threshold_ms / 1000
            t = self.jump_target(threshold)
            if t is not None and t != self.next_key:
                self.seek(t)
            if t is None or not self.next_ready():      # wait for the decoder before switching
                self.show(self.state)
                return None, self.cards.audio(SPF), self.state
            self.set_state("live", jumped_to=t)
        elif self.state == "resuming":                  # after a restart: continue exactly where the checkpoint was
            if not self.next_ready():
                self.show("holding")
                return None, self.cards.audio(SPF), "holding"
            self.set_state("live", resumed=True)

        # live or finishing: footage if there is any
        if self.interstitial:
            self.interstitial -= 1
            self.show("interrupted")
            return None, self.cards.audio(SPF), "interrupted"
        frame = self.take_frame()
        if frame is None and self.interstitial:
            self.interstitial -= 1
            self.show("interrupted")
            return None, self.cards.audio(SPF), "interrupted"
        if frame is not None:
            self.show("footage")
            return frame, self.take_audio(), "footage"

        if self.state == "finishing":
            if idx.complete_after(self.next_key):
                self.set_state("ended")
                self.show("ended")
                return None, self.cards.audio(SPF), "ended"
            if idx.finish_at is not None and time.time() - idx.finish_at > self.s.drain_wait_s:
                nxt = self.next_stored_after(self.next_key)
                self.ev("drain_wait_expired", key=self.next_key, next=nxt)
                if nxt is None:
                    self.set_state("ended")
                    return None, self.cards.audio(SPF), "ended"
                self.seek(nxt)
        else:
            self.set_state("holding")
        self.show("holding")
        return None, self.cards.audio(SPF), "holding"

    def next_stored_after(self, key: Key) -> Key | None:
        with self.index.lock:
            later = [k for k in self.index.segments if k >= key]
        return min(later) if later else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--broadcast", required=True, type=uuid.UUID)
    ap.add_argument("--log")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return Playout(Settings.from_env(), a.broadcast, a.log).run()


if __name__ == "__main__":
    sys.exit(main())
