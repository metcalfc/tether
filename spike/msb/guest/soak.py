#!/usr/bin/env python3
"""soak.py -- stream model calls through tether for a while; count aborts (in the guest).

Every INTERVAL seconds, sends one streaming haiku /v1/messages through
ANTHROPIC_BASE_URL with the agent cap from ANTHROPIC_AUTH_TOKEN, and reads
the SSE stream to the end. A call counts as complete only if it ends with
message_stop; anything else (a cut stream, a stall past 60 s, a non-200) is
an abort. Prints one line per call and a summary; exits 1 on any abort.

  soak.py [--minutes 30] [--interval 6] [--max-tokens 120]
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.parse


def one(base: urllib.parse.SplitResult, cap: str, max_tokens: int) -> tuple[str, float, float, int]:
    body = json.dumps({"model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"), "max_tokens": max_tokens,
                       "stream": True, "messages": [{"role": "user", "content": "Write a short poem about rivers."}]})
    t0 = time.monotonic()
    c = http.client.HTTPConnection(base.hostname or "", base.port, timeout=60)
    try:
        c.request("POST", base.path.rstrip("/") + "/v1/messages", body,
                  {"content-type": "application/json", "authorization": f"Bearer {cap}",
                   "anthropic-version": "2023-06-01"})
        r = c.getresponse()
        if r.status != 200:
            return f"status {r.status}", 0.0, time.monotonic() - t0, 0
        first = 0.0
        events = 0
        stop = False
        while True:
            line = r.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                events += 1
                if not first:
                    first = time.monotonic() - t0
                if b'"message_stop"' in line:
                    stop = True
        return ("ok" if stop else "cut"), first, time.monotonic() - t0, events
    except (OSError, http.client.HTTPException) as e:
        return f"error {type(e).__name__}", 0.0, time.monotonic() - t0, 0
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--interval", type=float, default=6)
    ap.add_argument("--max-tokens", type=int, default=120)
    a = ap.parse_args()
    base = urllib.parse.urlsplit(os.environ["ANTHROPIC_BASE_URL"])
    cap = os.environ["ANTHROPIC_AUTH_TOKEN"]
    end = time.monotonic() + a.minutes * 60
    n = bad = 0
    ttfbs: list[float] = []
    start = time.monotonic()
    while time.monotonic() < end:
        t = time.monotonic()
        res, first, total, events = one(base, cap, a.max_tokens)
        n += 1
        if res == "ok":
            ttfbs.append(first)
        else:
            bad += 1
        print(f"{time.monotonic() - start:7.1f}s  #{n:<4} {res:<10} ttfb {first * 1e3:6.0f} ms  "
              f"total {total:5.2f} s  events {events}", flush=True)
        time.sleep(max(0.0, a.interval - (time.monotonic() - t)))
    ttfbs.sort()
    p50 = ttfbs[len(ttfbs) // 2] * 1e3 if ttfbs else 0
    print(f"soak: {n} calls over {(time.monotonic() - start) / 60:.1f} min, {bad} aborted, ttfb p50 {p50:.0f} ms")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
