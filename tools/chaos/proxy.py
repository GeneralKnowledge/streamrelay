"""Bad-network TCP proxy between the fake phone and the API, for the M3 chaos test.

Runs in user space (no netem or root needed), so it works on any CI runner:
  outages     random 5-120 s windows: every open connection is cut and new
              ones are reset at once
  latency     each chunk is delivered `--latency` later, in both directions
  uplink cap  phone->server bytes limited to a rate that alternates between
              the given values every `--cap-period` seconds
  loss        TCP hides packet loss from applications, so loss is modelled by
              its visible effect: with probability p (drawn 0-10% per outage
              cycle) a connection is reset partway through a request
  resets      plus a random reset of any open connection every `--reset-every` s

    python3 tools/chaos/proxy.py --listen 8090 --upstream 127.0.0.1:8080 --log proxy.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

CHUNK = 16384


class Chaos:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.rng = random.Random(a.seed)
        self.log = open(a.log, "a")
        self.offline_until = 0.0
        self.loss = 0.0
        self.conns: set[asyncio.StreamWriter] = set()
        self.caps = [float(x) * 1_000_000 / 8 for x in a.caps.split(",")]
        self.tokens = 0.0
        self.tok_time = time.monotonic()
        self.t0 = time.time()

    def ev(self, kind: str, **kw: object) -> None:
        self.log.write(json.dumps({"wall": round(time.time(), 3), "event": kind, **kw}) + "\n")
        self.log.flush()

    def cap(self) -> float:
        return self.caps[int((time.time() - self.t0) // self.a.cap_period) % len(self.caps)]

    async def take(self, n: int) -> None:
        """Wait until n uplink bytes may pass (token bucket shared by all connections)."""
        while True:
            now = time.monotonic()
            rate = self.cap()
            # the bucket holds at least one full chunk, so a low cap still lets a chunk through
            self.tokens = min(max(rate, CHUNK), self.tokens + (now - self.tok_time) * rate)
            self.tok_time = now
            if self.tokens >= n:
                self.tokens -= n
                return
            await asyncio.sleep(max(0.005, (n - self.tokens) / rate))

    def offline(self) -> bool:
        return time.time() < self.offline_until

    async def outages(self) -> None:
        while True:
            await asyncio.sleep(self.rng.uniform(self.a.online_min, self.a.online_max))
            length = self.rng.uniform(5, 120)
            self.offline_until = time.time() + length
            self.loss = self.rng.uniform(0, 0.10)
            self.ev("outage", seconds=round(length, 1), cut=len(self.conns), next_loss=round(self.loss, 3))
            for w in list(self.conns):
                w.transport.abort()
            await asyncio.sleep(length)
            self.ev("online")

    async def resets(self) -> None:
        while True:
            await asyncio.sleep(self.rng.expovariate(1 / self.a.reset_every))
            if self.conns:
                w = self.rng.choice(list(self.conns))
                w.transport.abort()
                self.ev("reset")

    async def pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter, up: bool, peer: asyncio.StreamWriter) -> None:
        lossy = up and self.rng.random() < self.loss
        cut_after = self.rng.randint(1, 2_000_000) if lossy else None
        sent = 0
        try:
            while True:
                chunk = await r.read(CHUNK)
                if not chunk:
                    # a normal half-close: pass it on and let the other direction finish its reply
                    try:
                        if w.can_write_eof():
                            w.write_eof()
                    except OSError:
                        pass
                    return
                if up:
                    await self.take(len(chunk))
                await asyncio.sleep(self.a.latency)
                if self.offline():
                    raise ConnectionResetError
                sent += len(chunk)
                if cut_after is not None and sent >= cut_after:
                    self.ev("loss_reset", after_bytes=sent)
                    raise ConnectionResetError
                w.write(chunk)
                await w.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        # a fault (outage, injected loss, reset) or a broken connection: cut both sides at once
        for x in (w, peer):
            x.transport.abort()

    async def handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        if self.offline():
            cw.transport.abort()
            return
        host, port = self.a.upstream.rsplit(":", 1)
        try:
            sr, sw = await asyncio.open_connection(host, int(port))
        except OSError:
            cw.transport.abort()                      # server down (killed): the phone sees a reset
            return
        if self.offline():                            # an outage began while connecting
            sw.transport.abort()
            cw.transport.abort()
            return
        self.conns.add(cw)
        try:
            await asyncio.gather(self.pipe(cr, sw, True, cw), self.pipe(sr, cw, False, sw))
        finally:
            self.conns.discard(cw)
            for x in (cw, sw):
                x.close()

    async def main(self) -> None:
        srv = await asyncio.start_server(self.handle, "127.0.0.1", self.a.listen)
        self.ev("start", **{k: v for k, v in vars(self.a).items() if k != "log"})
        async with srv:
            await asyncio.gather(srv.serve_forever(), self.outages(), self.resets())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", type=int, default=8090)
    ap.add_argument("--upstream", default="127.0.0.1:8080")
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--caps", default="3,6", help="uplink caps in Mbps, alternating")
    ap.add_argument("--cap-period", type=float, default=1800)
    ap.add_argument("--online-min", type=float, default=60)
    ap.add_argument("--online-max", type=float, default=300)
    ap.add_argument("--reset-every", type=float, default=90)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--log", required=True)
    asyncio.run(Chaos(ap.parse_args()).main())


if __name__ == "__main__":
    main()
