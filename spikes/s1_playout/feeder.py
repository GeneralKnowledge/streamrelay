"""Deliver segments into the playout's input folder like a phone on a bad network.

Segment i (2 s) is complete at source time 2*(i+1). Normally it is delivered at
that moment. Segments completing inside the outage window are held back and all
delivered at once when the outage ends; delivery is then real time again.
Writes are atomic (temp name, then rename). Events go to a JSON-lines log.
"""
import argparse, json, os, shutil, time

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("dst"); ap.add_argument("--log", required=True)
    ap.add_argument("--gap-start", type=float, default=120.0)
    ap.add_argument("--gap-len", type=float, default=180.0)
    ap.add_argument("--duration", type=float, default=1200.0)
    ap.add_argument("--t0", type=float, help="shared wall-clock start (epoch seconds)")
    a = ap.parse_args()
    os.makedirs(a.dst, exist_ok=True)
    segs = sorted(f for f in os.listdir(a.src) if f.endswith(".m4s"))
    log = open(a.log, "a")
    def ev(kind, **kw):
        log.write(json.dumps({"wall": time.time(), "event": kind, **kw}) + "\n"); log.flush()
    def put(name):
        tmp = os.path.join(a.dst, "." + name + ".tmp")
        shutil.copyfile(os.path.join(a.src, name), tmp)
        os.rename(tmp, os.path.join(a.dst, name))
    put("init.mp4")
    t0 = a.t0 or time.time()
    gap_end = a.gap_start + a.gap_len
    ev("start", t0=t0, gap_start=a.gap_start, gap_len=a.gap_len)
    gap_logged = False
    for i, name in enumerate(segs):
        done_at = 2.0 * (i + 1)
        if done_at > a.duration:
            break
        deliver_at = gap_end if a.gap_start < done_at <= gap_end else done_at
        if a.gap_start < done_at and not gap_logged:
            ev("outage_start", source_time=a.gap_start)
            gap_logged = True
        wait = t0 + deliver_at - time.time()
        if wait > 0:
            time.sleep(wait)
        put(name)
        if done_at == gap_end:
            ev("backlog_delivered", last_segment=i)
    ev("end")

if __name__ == "__main__":
    main()
