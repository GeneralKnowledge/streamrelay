"""A stand-in for the phone: uploads fMP4 segments over protocol v1 in real time.

Segment i counts as captured once its last frame would have been recorded
(t0 + its end time) and joins the upload queue then. Uploads follow the
client rules of protocol v1: up to 3 PUTs in flight, retries with backoff,
412 -> upload the init, 422 -> retry once, idempotent duplicates.

Faults, for the continuity tests:
  --offline START:LEN,...   no request starts during these windows (wall seconds
                            from t0); segments keep being captured and queue up
  --reorder P               hold a segment back until the next one is captured
  --duplicate P             PUT an acknowledged segment again (expects 200)
  --corrupt P               send one attempt with a flipped byte (expects 422)

Order (protocol v1): skip_to_live uploads the newest segment first and backfills
older ones oldest-first, at most --backfill-rate per second; catch_up and hold
upload oldest first.

    python3 tools/fake_phone/fake_phone.py --api http://127.0.0.1:8080 --token T \
        --segments segs/ --mode skip_to_live --offline 120:10,300:60 --log phone.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

import httpx


class Log:
    def __init__(self, path: str):
        self.f = open(path, "a")
        self.lock = threading.Lock()

    def __call__(self, event: str, **kw: object) -> None:
        with self.lock:
            self.f.write(json.dumps({"wall": round(time.time(), 3), "event": event, **kw}) + "\n")
            self.f.flush()


def durations(folder: str) -> list[float]:
    text = open(os.path.join(folder, "index.m3u8")).read()
    return [float(x) for x in re.findall(r"#EXTINF:([0-9.]+),", text)]


def require_safe_url(url: str) -> None:
    """Credentials travel over HTTPS, or plain HTTP only to loopback or a Tailscale address
    (100.64.0.0/10, already encrypted by WireGuard)."""
    u = httpx.URL(url)
    if u.scheme == "https":
        return
    host = u.host
    try:
        ip = ipaddress.ip_address(host)
        if u.scheme == "http" and (ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")):
            return
    except ValueError:
        if u.scheme == "http" and host == "localhost":
            return
    raise SystemExit(f"{url}: use https (plain http only for loopback or a Tailscale address)")


def parse_windows(spec: str) -> list[tuple[float, float]]:
    out = []
    for part in filter(None, spec.split(",")):
        s, n = part.split(":")
        out.append((float(s), float(s) + float(n)))
    return out


class Phone:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.log = Log(a.log)
        self.rng = random.Random(a.seed)
        self.http = httpx.Client(base_url=a.api, headers={"Authorization": f"Bearer {a.token}"}, timeout=30)
        self.durs = durations(a.segments)
        if a.limit:
            self.durs = self.durs[: a.limit]
        self.starts: list[int] = []
        t = 0
        for d in self.durs:
            self.starts.append(t)
            t += round(d * 1_000_000)
        self.offline = parse_windows(a.offline)
        self.acked: set[int] = set()
        self.damaged: set[int] = set()
        self.lock = threading.Lock()
        self.bid = ""

    def is_offline(self, rel: float) -> bool:
        return any(s <= rel < e for s, e in self.offline)

    def body(self, i: int) -> bytes:
        with open(os.path.join(self.a.segments, f"{i:05d}.m4s"), "rb") as f:
            return f.read()

    def call(self, method: str, url: str, deadline_s: float = 900.0, **kw: object) -> httpx.Response:
        """A control request, retried with backoff (or the server's Retry-After) through outages and
        restarts, for at most `deadline_s`; then it stops and reports the last answer or error."""
        backoff, until, last = 1.0, time.time() + deadline_s, "no attempt"
        while True:
            delay = None
            try:
                r = self.http.request(method, url, **kw)  # type: ignore[arg-type]
                if r.status_code < 500 and r.status_code != 429:
                    return r
                last = f"HTTP {r.status_code} {r.text[:200]}"
                if r.headers.get("retry-after", "").isdigit():
                    delay = float(r.headers["retry-after"])
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {str(e)[:200]}"
                self.log("call_error", url=url, error=last)
            if delay is None:
                delay = self.rng.uniform(0, backoff)
                backoff = min(30.0, backoff * 2)
            if time.time() + delay > until:
                raise SystemExit(f"{method} {url}: gave up after {deadline_s:.0f} s; last: {last}")
            time.sleep(delay)

    def pair(self, code: str, api: str) -> None:
        """Pairing spends the code, so it is retried only when the request surely never reached the
        server (no connection, or 429/503); any other failure stops with a note to make a new code."""
        with httpx.Client(base_url=api, timeout=30) as c:     # no bearer token on the pairing request
            for attempt in range(10):
                try:
                    r = c.post("/v1/devices/pair", json={"code": code, "name": "fake phone", "platform": "test"})
                except httpx.ConnectError as e:
                    self.log("call_error", url="/v1/devices/pair", error=str(e)[:200])
                    time.sleep(min(30, 2 ** attempt))
                    continue
                except httpx.HTTPError as e:
                    raise SystemExit(f"pairing: no answer ({e}); the code may be used up, make a new one") from None
                if r.status_code in (429, 503):
                    time.sleep(float(r.headers.get("retry-after", 2 ** attempt)))
                    continue
                if r.status_code != 201:
                    raise SystemExit(f"pairing: HTTP {r.status_code} {r.text[:200]}")
                self.http.headers["Authorization"] = f"Bearer {r.json()['token']}"
                self.log("paired", device_id=r.json()["device_id"])
                return
        raise SystemExit("pairing: server unreachable")

    def put_init(self) -> None:
        data = open(os.path.join(self.a.segments, "init.mp4"), "rb").read()
        while True:
            try:
                r = self.http.put(f"/v1/broadcasts/{self.bid}/inits/0", content=data,
                                  headers={"X-SR-SHA256": hashlib.sha256(data).hexdigest(), "Content-Type": "video/mp4"})
                self.log("init", status=r.status_code)
                if r.status_code in (200, 201):
                    return
            except httpx.HTTPError as e:
                self.log("init_error", error=str(e))
            time.sleep(1)

    def put(self, i: int, kind: str) -> int:
        """One segment until acknowledged (or given up as damaged). Returns the final status."""
        data = self.body(i)
        digest = hashlib.sha256(data).hexdigest()
        backoff, repeats_422 = 1.0, 0
        corrupt = kind != "duplicate" and self.rng.random() < self.a.corrupt
        while True:
            send = data
            if corrupt:
                b = bytearray(data)
                b[len(b) // 2] ^= 0xFF
                send, corrupt = bytes(b), False
            h = {"X-SR-SHA256": digest, "X-SR-Init": "0", "X-SR-Start-Us": str(self.starts[i]),
                 "X-SR-Duration-Us": str(round(self.durs[i] * 1_000_000)), "Content-Type": "video/mp4"}
            if i == len(self.durs) - 1:
                h["X-SR-Flags"] = "final"
            t = time.time()
            try:
                r = self.http.put(f"/v1/broadcasts/{self.bid}/segments/0/{i}", content=send, headers=h)
                status = r.status_code
            except httpx.HTTPError as e:
                status = 0
                self.log("put", seq=i, kind=kind, status=0, error=str(e)[:200],
                         **({"corrupt": True} if send is not data else {}))
            if status:
                self.log("put", seq=i, kind=kind, status=status, ms=round((time.time() - t) * 1000), bytes=len(send),
                         **({"corrupt": True} if send is not data else {}),
                         **({"error": r.text[:200]} if status >= 400 else {}))
            if status in (200, 201):
                return status
            if status == 412:
                self.put_init()
                continue
            if status == 422:
                repeats_422 += 1
                if repeats_422 >= 2:
                    with self.lock:
                        self.damaged.add(i)
                    return status
                continue
            if status in (404, 409, 410):
                raise SystemExit(f"segment {i}: {status} {r.text}")
            delay = self.rng.uniform(0, backoff)
            if status in (429, 503, 507) and r.headers.get("retry-after"):
                delay = float(r.headers["retry-after"])
            time.sleep(delay)
            backoff = min(30.0, backoff * 2)

    def run(self) -> None:
        a = self.a
        ref = a.ref or str(uuid.uuid4())
        if a.pair:
            self.pair(a.pair, a.pair_api or a.api)
        r = self.call("POST", "/v1/broadcasts", json={"title": a.title, "client_broadcast_ref": ref,
                                                      "catch_up": {"mode": a.mode}})
        r.raise_for_status()
        self.bid = r.json()["broadcast_id"]
        t0 = time.time()
        self.log("start", broadcast_id=self.bid, t0=t0, segments=len(self.durs), mode=a.mode, offline=self.offline,
                 duration_s=round(sum(self.durs), 3))
        print(self.bid, flush=True)
        self.put_init()

        pool = ThreadPoolExecutor(a.concurrency)
        inflight: dict[int, Future[int]] = {}
        captured, held = 0, None
        queue: list[int] = []
        tokens, last_tok = 0.0, time.time()
        was_offline = False
        n = len(self.durs)
        while True:
            now = time.time()
            rel = now - t0
            # capture
            while captured < n and t0 + (self.starts[captured] / 1e6 + self.durs[captured]) <= now:
                i = captured
                captured += 1
                self.log("captured", seq=i)
                if held is not None:
                    queue.append(i)
                    queue.append(held)
                    held = None
                elif self.rng.random() < a.reorder and captured < n:
                    held = i
                else:
                    queue.append(i)
            if captured == n and held is not None:
                queue.append(held)
                held = None
            # finished requests
            for i, f in list(inflight.items()):
                if f.done():
                    del inflight[i]
                    status = f.result()
                    if status in (200, 201):
                        with self.lock:
                            self.acked.add(i)
                        if self.rng.random() < a.duplicate:
                            pool.submit(self.put, i, "duplicate")
            off = self.is_offline(rel)
            if off != was_offline:
                self.log("offline_start" if off else "offline_end", rel=round(rel, 3))
                was_offline = off
            tokens = min(tokens + (now - last_tok) * a.backfill_rate, 2.0)
            last_tok = now
            if not off:
                while len(inflight) < a.concurrency and queue:
                    pick = None
                    if a.mode == "skip_to_live":
                        newest = max(queue)
                        if newest >= captured - 2:          # the latest capture: live
                            pick, kind = newest, "live"
                        elif tokens >= 1:                   # older: backfill, oldest first, rate-limited
                            pick, kind, tokens = min(queue), "backfill", tokens - 1
                    else:
                        pick, kind = min(queue), "live"
                    if pick is None:
                        break
                    queue.remove(pick)
                    inflight[pick] = pool.submit(self.put, pick, kind)
            if captured == n and not queue and not inflight and held is None:
                break
            time.sleep(0.02)
        pool.shutdown(wait=True)
        last = n - 1
        r = self.call("POST", f"/v1/broadcasts/{self.bid}/epochs/0/end", json={"last_sequence": last, "reason": "stopped"})
        self.log("epoch_end", status=r.status_code)
        r = self.call("POST", f"/v1/broadcasts/{self.bid}/finish", json={"last_epoch": 0})
        self.log("finish", status=r.status_code, acked=len(self.acked), damaged=sorted(self.damaged))
        st = self.call("GET", f"/v1/broadcasts/{self.bid}/status").json()
        self.log("status", **st)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8080")
    ap.add_argument("--token", default=os.environ.get("SR_TOKEN"))
    ap.add_argument("--segments", required=True)
    ap.add_argument("--mode", default="skip_to_live", choices=["catch_up", "hold", "skip_to_live"])
    ap.add_argument("--title", default="fake phone")
    ap.add_argument("--ref")
    ap.add_argument("--pair", help="pairing code: pair first and use the issued token")
    ap.add_argument("--pair-api", help="pair against this URL instead of --api (e.g. straight to the server,\
 not through the chaos proxy, so a dropped answer can't spend the code)")
    ap.add_argument("--offline", default="")
    ap.add_argument("--reorder", type=float, default=0.0)
    ap.add_argument("--duplicate", type=float, default=0.0)
    ap.add_argument("--corrupt", type=float, default=0.0)
    ap.add_argument("--backfill-rate", type=float, default=1.0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="upload only the first N segments")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--log", required=True)
    a = ap.parse_args()
    for url in (a.api, a.pair_api):
        if url:
            require_safe_url(url)
    Phone(a).run()


if __name__ == "__main__":
    main()
