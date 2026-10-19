#!/usr/bin/env python3
"""bench.py -- time to first byte through the gateway versus direct.

Sends the same small streaming /v1/messages request N times to each target,
interleaved (direct, gateway, direct, ...) so drift hits both equally. Every
request opens a fresh connection, which is what the gateway itself does
upstream, so the comparison is like for like.

  direct   https://api.anthropic.com, key read from the keychain in this
           process (never printed, never written anywhere)
  gateway  http://127.0.0.1:PORT/anthropic, with the session capability
           from the gateway's cage.env (the gateway holds the key)

For each request it records: headers (time to response headers), ttfb (time
to the first SSE data line), and the mean gap between SSE data lines.

  bench.py ttfb    --env overnight/run/leash-msb/cage.env [-n 20] [--port 18001]
  bench.py ttfb    --env /root/.leash.env --gateway-only --gw-host host.microsandbox.internal  (in the guest)
  bench.py connect --env ... [-n 20] [--host registry.npmjs.org]

`connect` times CONNECT setup with curl: time_appconnect for a HEAD through
the gateway's proxy versus direct.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import statistics
import subprocess
import sys
import time
from pathlib import Path

MODEL = "claude-haiku-4-5"
API = "api.anthropic.com"
BODY = json.dumps(
    {
        "model": MODEL,
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "Count from 1 to 10, separated by spaces."}],
    }
).encode()


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def keychain(service: str) -> str:
    return subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, check=True
    ).stdout.strip()


def one(conn: http.client.HTTPConnection, path: str, headers: dict[str, str]) -> dict[str, float]:
    t0 = time.perf_counter()
    conn.request("POST", path, body=BODY, headers=headers)
    resp = conn.getresponse()
    t_head = time.perf_counter() - t0
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}: {resp.read(300)!r}")
    stamps: list[float] = []
    while True:
        line = resp.readline()
        if not line:
            break
        if line.startswith(b"data:"):
            stamps.append(time.perf_counter() - t0)
    conn.close()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    return {
        "headers": t_head,
        "ttfb": stamps[0] if stamps else float("nan"),
        "total": stamps[-1] if stamps else float("nan"),
        "gap": statistics.mean(gaps) if gaps else float("nan"),
    }


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(name: str, rows: list[dict[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in ("headers", "ttfb", "total", "gap"):
        xs = [r[key] for r in rows]
        out[f"{key}_p50_ms"] = round(pct(xs, 0.5) * 1000, 1)
        out[f"{key}_p95_ms"] = round(pct(xs, 0.95) * 1000, 1)
    print(
        f"  {name:8s} n={len(rows):3d}  ttfb p50 {out['ttfb_p50_ms']:7.1f} ms  p95 {out['ttfb_p95_ms']:7.1f} ms"
        f"  | headers p50 {out['headers_p50_ms']:7.1f}  | sse gap p50 {out['gap_p50_ms']:6.1f} ms"
    )
    return out


def cmd_ttfb(a: argparse.Namespace) -> int:
    env = read_env(Path(a.env))
    cap = env["ANTHROPIC_AUTH_TOKEN"]
    key = "" if a.gateway_only else keychain(a.keychain)
    ctx = ssl.create_default_context()
    common = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    direct_h = {**common, "x-api-key": key}
    gw_h = {**common, "authorization": f"Bearer {cap}"}
    rows: dict[str, list[dict[str, float]]] = {"direct": [], "gateway": []}
    for i in range(a.n):
        order = ["direct", "gateway"] if i % 2 == 0 else ["gateway", "direct"]
        for which in order:
            if which == "direct" and a.gateway_only:
                continue
            if which == "direct":
                c: http.client.HTTPConnection = http.client.HTTPSConnection(API, timeout=30, context=ctx)
                rows[which].append(one(c, "/v1/messages", direct_h))
            else:
                c = http.client.HTTPConnection(a.gw_host, a.port, timeout=30)
                rows[which].append(one(c, "/anthropic/v1/messages", gw_h))
    del key
    res = {name: summary(name, r) for name, r in rows.items() if r}
    if a.gateway_only:
        if a.json:
            Path(a.json).write_text(json.dumps({"n": a.n, "model": MODEL, **res}, indent=2) + "\n")
        return 0
    added = res["gateway"]["ttfb_p50_ms"] - res["direct"]["ttfb_p50_ms"]
    print(
        f"  added ttfb at p50: {added:+.1f} ms; at p95: "
        f"{res['gateway']['ttfb_p95_ms'] - res['direct']['ttfb_p95_ms']:+.1f} ms"
    )
    if a.json:
        Path(a.json).write_text(json.dumps({"n": a.n, "model": MODEL, **res}, indent=2) + "\n")
    return 0


def curl_appconnect(host: str, proxy: str | None) -> float:
    cmd = ["curl", "-s", "-o", "/dev/null", "-I", "-m", "15", "-w", "%{time_appconnect}", f"https://{host}/"]
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("https_proxy", "http_proxy", "all_proxy")}
    if proxy:
        cmd[1:1] = ["--proxy", proxy]
    else:
        cmd[1:1] = ["--noproxy", "*"]
    out = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=20).stdout
    return float(out)


def cmd_connect(a: argparse.Namespace) -> int:
    env = read_env(Path(a.env))
    proxy = env["HTTPS_PROXY"]
    # The env file advertises the guest's name for the host; on the host it is loopback.
    proxy = proxy.replace("host.microsandbox.internal", "127.0.0.1")
    rows: dict[str, list[float]] = {"direct": [], "gateway": []}
    for i in range(a.n):
        order = ["direct", "gateway"] if i % 2 == 0 else ["gateway", "direct"]
        for which in order:
            rows[which].append(curl_appconnect(a.host, None if which == "direct" else proxy))
    res = {}
    for name, xs in rows.items():
        res[name] = {"p50_ms": round(pct(xs, 0.5) * 1000, 1), "p95_ms": round(pct(xs, 0.95) * 1000, 1)}
        print(
            f"  {name:8s} n={len(xs):3d}  TLS ready (time_appconnect) p50 {res[name]['p50_ms']:7.1f} ms"
            f"  p95 {res[name]['p95_ms']:7.1f} ms"
        )
    if a.json:
        Path(a.json).write_text(json.dumps({"n": a.n, "host": a.host, **res}, indent=2) + "\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("ttfb", "connect"):
        s = sub.add_parser(name)
        s.add_argument("--env", required=True)
        s.add_argument("-n", type=int, default=20)
        s.add_argument("--json")
    sub.choices["ttfb"].add_argument("--port", type=int, default=18001)
    sub.choices["ttfb"].add_argument("--keychain", default="tether-anthropic")
    sub.choices["ttfb"].add_argument("--gw-host", default="127.0.0.1")
    sub.choices["ttfb"].add_argument("--gateway-only", action="store_true", help="no direct runs (in the guest)")
    sub.choices["connect"].add_argument("--host", default="registry.npmjs.org")
    a = p.parse_args()
    return cmd_ttfb(a) if a.cmd == "ttfb" else cmd_connect(a)


if __name__ == "__main__":
    sys.exit(main())
