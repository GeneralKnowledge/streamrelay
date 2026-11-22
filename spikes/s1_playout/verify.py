"""Check one S1 run against the pass gate in docs/plan/spike-s1-playout.md.

Reads the independent recording (rec.flv, made by an ffmpeg viewer of MediaMTX),
the MediaMTX log, the reader's warnings, and the feeder/playout event logs.
Writes verify.json and prints a Markdown table. Also cuts 60 s samples around
each switch and a speech sample for listening into <work>/samples/.
"""
import argparse, json, os, re, subprocess
import av, numpy as np

FPS, SR = 30, 48000
BITS, BLOCK = 24, 40
HOLD = (1 << BITS) - 1
CENTERS = [i * BLOCK + BLOCK // 2 for i in range(BITS)]
SPEECH = (195.0, 235.0)          # source seconds around the spoken passage


def jl(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def read_video(path):
    """Per frame: (time in recording, barcode value)."""
    out = []
    with av.open(path) as c:
        for f in c.decode(video=0):
            y = f.planes[0]
            row = np.frombuffer(y, np.uint8).reshape(-1, y.line_size)[BLOCK // 2]
            bits = row[CENTERS] > 128
            v = 0
            for b in bits:
                v = (v << 1) | int(b)
            out.append((float(f.time), v))
    return out


def read_packets(path, kind):
    with av.open(path) as c:
        st = c.streams.video[0] if kind == "video" else c.streams.audio[0]
        return [(float(p.pts * p.time_base), float((p.duration or 0) * p.time_base))
                for p in c.demux(st) if p.pts is not None]


def read_audio(path):
    with av.open(path) as c:
        chunks, start = [], None
        rs = av.AudioResampler(format="flt", layout="mono", rate=SR)
        for f in c.decode(audio=0):
            if start is None:
                start = float(f.time)
            for r in rs.resample(f):
                chunks.append(r.to_ndarray()[0])
    return start, np.concatenate(chunks)


def peak_hz(x):
    w = np.hanning(len(x))
    s = np.abs(np.fft.rfft(x * w))
    k = int(np.argmax(s[1:])) + 1
    if 1 <= k < len(s) - 1:      # parabolic interpolation
        a, b, c = np.log(s[k - 1] + 1e-12), np.log(s[k] + 1e-12), np.log(s[k + 1] + 1e-12)
        k = k + 0.5 * (a - c) / (a - 2 * b + c)
    return k * SR / len(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("--speed", type=float, required=True)
    a = ap.parse_args()
    W = a.work
    rec = os.path.join(W, "rec.flv")
    feed = jl(os.path.join(W, "feeder.jsonl"))
    play = jl(os.path.join(W, "playout.jsonl"))
    secs = json.load(open(os.path.join(W, "playout.seconds.json")))
    start = next(e for e in feed if e["event"] == "start")
    t0, gap_start, gap_len = start["t0"], start["gap_start"], start["gap_len"]
    delivered = next(e["wall"] for e in feed if e["event"] == "backlog_delivered")
    states = [e for e in play if e["event"] == "state"]
    end = next(e for e in play if e["event"] == "end")
    R = {}

    # 1. One RTMP session for the whole run
    mtx = open(os.path.join(W, "mediamtx.log")).read()
    R["rtmp_publish_sessions"] = len(re.findall(r"is publishing to path 's1'", mtx))

    # 2. Video PTS
    vp = read_packets(rec, "video")
    vt = [p[0] for p in vp]
    d = np.diff(vt)
    R["video_packets"] = len(vt)
    R["video_pts_non_increasing"] = int((d <= 0).sum())
    R["video_max_gap_ms"] = round(float(d.max()) * 1000, 1)
    R["video_gaps_over_1_5_frames"] = int((d > 1.5 / FPS + 0.001).sum())
    R["recording_seconds"] = round(vt[-1] - vt[0], 1)

    # 3. Audio continuity
    ap_ = read_packets(rec, "audio")
    gaps = [ap_[i + 1][0] - (ap_[i][0] + ap_[i][1]) for i in range(len(ap_) - 1)]
    R["audio_max_packet_gap_ms"] = round(max(gaps) * 1000, 1)
    R["audio_packet_gaps_over_25ms"] = int(sum(g > 0.025 for g in gaps))
    a0, audio = read_audio(rec)
    z = np.abs(audio) < 1e-5
    run = best = 0
    for v in z:
        run = run + 1 if v else 0
        best = max(best, run)
    R["audio_longest_silence_ms"] = round(best / SR * 1000, 1)
    reader = [l for l in open(os.path.join(W, "reader.log")).read().splitlines()
              if l.strip() and "Input/output error" not in l]
    R["reader_warnings"] = len(reader)
    R["reader_warning_lines"] = reader[:20]

    # 4. Frames: order, coverage, states seen by the viewer
    frames = read_video(rec)
    codes = [v for _, v in frames if v != HOLD]
    dc = np.diff(codes)
    R["source_frames_seen"] = len(codes)
    R["source_order_violations"] = int((dc <= 0).sum())
    R["source_frame_max_jump"] = int(dc.max())
    first_sec, last_sec = codes[0] // FPS, codes[-1] // FPS
    seen = {c // FPS for c in codes}
    R["source_seconds_range"] = [int(first_sec), int(last_sec)]
    R["source_seconds_missing"] = sorted(set(range(first_sec, last_sec + 1)) - seen)
    R["holding_frames_seen"] = sum(1 for _, v in frames if v == HOLD)

    # per-second speed as the viewer sees it
    windows = []
    for k in range(0, len(frames) - FPS, FPS):
        w = frames[k:k + FPS]
        vals = [v for _, v in w if v != HOLD]
        if len(vals) < FPS:
            windows.append((w[0][0], "holding", None, None))
            continue
        spd = (vals[-1] - vals[0]) / (FPS - 1)
        windows.append((w[0][0], "catch_up" if spd > 1.1 else "live", spd, vals[0] / FPS))
    cu = [w for w in windows if w[1] == "catch_up"]
    R["viewer_catch_up_seconds"] = len(cu)
    R["viewer_catch_up_speed_mean"] = round(float(np.mean([w[2] for w in cu])), 3) if cu else None

    # 5. Tone pitch during catch-up (1 s windows, away from the speech)
    hz = []
    for t, _, _, src in cu:
        if SPEECH[0] <= src <= SPEECH[1]:
            continue
        i = int((t - a0) * SR)
        if 0 <= i and i + SR <= len(audio):
            hz.append(peak_hz(audio[i:i + SR]))
    R["catch_up_tone_hz_min"] = round(min(hz), 1) if hz else None
    R["catch_up_tone_hz_max"] = round(max(hz), 1) if hz else None
    R["catch_up_tone_windows"] = len(hz)
    live_hz = []
    for t, s, _, src in windows:
        if s == "live" and not (SPEECH[0] <= src <= SPEECH[1]):
            i = int((t - a0) * SR)
            if 0 <= i and i + SR <= len(audio):
                live_hz.append(peak_hz(audio[i:i + SR]))
    R["live_tone_hz_min"] = round(min(live_hz), 1) if live_hz else None
    R["live_tone_hz_max"] = round(max(live_hz), 1) if live_hz else None

    # 6. Holding within 10 s of the feed stopping (playout log)
    outage_wall = t0 + gap_start
    hold = next((e for e in states if e["state"] == "holding" and e["wall"] > outage_wall - 5), None)
    R["holding_after_feed_stop_s"] = round(hold["wall"] - outage_wall, 2) if hold else None

    # 7. Return to live
    back = [e for e in states if e["state"] == "live" and e["wall"] > delivered]
    R["return_to_live_s"] = round(back[0]["wall"] - delivered, 1) if back else None
    R["return_to_live_bound_s"] = round(gap_len / (a.speed - 1) + 60, 1)
    R["state_changes"] = [{"t": round(e["wall"] - t0, 2), "state": e["state"], "src_s": round(e["src"] / FPS, 2),
                           "backlog": e.get("backlog")} for e in states]

    # 8. CPU and real time
    cpu = [s["cpu"] for s in secs]
    R["cpu_mean_core"] = round(end["cpu_total"] / end["wall_total"], 3)
    by = {}
    for s in secs:
        by.setdefault(s["state"], []).append(s["cpu"])
    R["cpu_by_state_mean"] = {k: round(float(np.mean(v)), 3) for k, v in by.items()}
    R["cpu_by_state_p95"] = {k: round(float(np.percentile(v, 95)), 3) for k, v in by.items()}
    R["cpu_max_1s"] = round(max(cpu), 3)
    R["late_max_s"], R["late_ticks"] = end["late_max"], end["late_ticks"]
    drift = [abs(s["av_drift_ms"]) for s in secs]
    R["av_drift_max_ms"] = max(drift)
    R["machine"] = subprocess.run("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2; nproc", shell=True,
                                  capture_output=True, text=True).stdout.split("\n")[:2]

    # 9. Samples for people: 60 s around each switch, speech during catch-up
    sd = os.path.join(W, "samples"); os.makedirs(sd, exist_ok=True)
    rec_start = frames[0][0]
    # recording time of output frame n: anchor on the first footage frame (source frame 0),
    # which playout sent at the n of its first 'live' state
    first_live = next(e["n"] for e in states if e["state"] == "live")
    t_first = next(t for t, v in frames if v == 0)
    offset = t_first - first_live / FPS
    for i, e in enumerate(states[1:], 1):
        ss = max(0.0, e["n"] / FPS + offset - 30 - rec_start)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ss:.2f}", "-i", rec, "-t", "60", "-c", "copy",
                        os.path.join(sd, f"switch{i:02d}_{e['state']}_{int(e['n'] / FPS)}s.flv")])
    sp = [t for t, s, _, src in windows if src is not None and SPEECH[0] <= src <= SPEECH[1]]
    if sp:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{sp[0] - rec_start:.2f}", "-i", rec, "-t",
                        f"{sp[-1] - sp[0] + 1:.1f}", "-vn", os.path.join(sd, f"speech_{a.speed}x.wav")])

    json.dump(R, open(os.path.join(W, "verify.json"), "w"), indent=1)
    print(json.dumps(R, indent=1))


if __name__ == "__main__":
    main()
