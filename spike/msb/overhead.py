#!/usr/bin/env python3
"""overhead.py -- tether's own added latency, with the network taken out.

Starts a local fake Anthropic upstream (plain HTTP, a short SSE stream with
usage) and a tether serve in front of it, then times N streaming
/v1/messages calls to each, interleaved, on fresh connections: time to the
first SSE data line. The difference is what tether itself adds on a metered
call (cap lookup, reservation, audit, the extra local hop), without
api.anthropic.com's jitter, which is larger than the effect.

  overhead.py [-n 200] [--tether ./tether] [--dir overnight/run/ovh]
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

SSE = (
    b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-haiku-4-5",'
    b'"usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
)
BODY = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 32, "stream": True,
                   "messages": [{"role": "user", "content": "hi"}]}).encode()


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(SSE)))
        self.end_headers()
        self.wfile.write(SSE)

    def log_message(self, *a: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ttfb(port: int, path: str, headers: dict[str, str]) -> float:
    t0 = time.perf_counter()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, BODY, {"content-type": "application/json", **headers})
    r = c.getresponse()
    while True:
        line = r.readline()
        if not line or line.startswith(b"data:"):
            break
    t = time.perf_counter() - t0
    r.read()
    c.close()
    if r.status != 200:
        raise SystemExit(f"{path}: status {r.status}")
    return t


def pct(xs: list[float], q: float) -> float:
    return sorted(xs)[min(len(xs) - 1, int(q * len(xs)))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--tether", default="./tether")
    ap.add_argument("--dir", default="overnight/run/ovh")
    a = ap.parse_args()
    d = Path(a.dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    gw_port = free_port()
    policy = {
        "listen": f"127.0.0.1:{gw_port}", "sessions": ["bench"], "state_dir": str(d / "state"),
        "routes": {"anthropic": {"upstream": f"http://127.0.0.1:{fake.server_address[1]}", "credential": "env:OVH_KEY",
                                 "inject": "anthropic", "paths": ["/v1/messages"], "meter": True}},
        "egress": {"allow": []}, "spend": {"per_session_usd_per_day": 1000.0},
        "prices_per_mtok": {"default": [1, 5]},
    }
    (d / "policy.json").write_text(json.dumps(policy))
    env = {**os.environ, "OVH_KEY": "sk-ant-api03-fake"}
    log = open(d / "serve.log", "w")
    gw = subprocess.Popen([a.tether, "-c", str(d / "policy.json"), "serve"], env=env, stdout=log, stderr=log)
    try:
        for _ in range(50):
            if (d / "state" / "bench.env").exists():
                break
            time.sleep(0.1)
        cap = next(x.split("=", 1)[1] for x in (d / "state" / "bench.env").read_text().splitlines()
                   if x.startswith("ANTHROPIC_AUTH_TOKEN="))
        for _ in range(50):
            try:
                ttfb(gw_port, "/anthropic/v1/messages", {"authorization": f"Bearer {cap}"})
                break
            except (OSError, SystemExit):
                time.sleep(0.1)
        direct: list[float] = []
        via: list[float] = []
        for _ in range(a.n):
            direct.append(ttfb(fake.server_address[1], "/v1/messages", {}))
            via.append(ttfb(gw_port, "/anthropic/v1/messages", {"authorization": f"Bearer {cap}"}))
    finally:
        gw.terminate()
        gw.wait(10)
        fake.shutdown()
    for name, xs in (("direct", direct), ("tether", via)):
        print(f"  {name:7s} n={len(xs)}  ttfb p50 {pct(xs, .5) * 1e3:6.2f} ms  p95 {pct(xs, .95) * 1e3:6.2f} ms"
              f"  mean {statistics.mean(xs) * 1e3:6.2f} ms")
    print(f"  added at p50: +{(pct(via, .5) - pct(direct, .5)) * 1e3:.2f} ms;"
          f" at p95: +{(pct(via, .95) - pct(direct, .95)) * 1e3:.2f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
