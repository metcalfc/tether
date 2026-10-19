#!/usr/bin/env python3
"""memscan.py -- scan every process's memory for a real credential (run as guest root).

For each pid, reads every readable mapping in /proc/PID/mem and searches it
for the real-key shapes from PROMPT.md's pre-push grep (an Anthropic key
with 40+ key characters, a GitHub token) and, for context, tether caps.
Prints counts and pid/comm, never a match. Exits 1 if any real key is found.

  memscan.py            scan every process
  memscan.py --control  also start a child holding a fake key-shaped string,
                        and require the scan to find it (positive control)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REAL = re.compile(rb"sk-ant-(?:api|oat)[0-9]{2}-[A-Za-z0-9_-]{40,}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}")
CAP = re.compile(rb"tth_[A-Za-z0-9_-]{16,}")
CHUNK = 1 << 22


def scan(pid: int) -> tuple[int, int, int] | None:
    """(real hits, cap hits, bytes read) for one pid, or None if unreadable."""
    real = caps = total = 0
    try:
        maps = open(f"/proc/{pid}/maps").read().splitlines()
        mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
    except OSError:
        return None
    with mem:
        for line in maps:
            parts = line.split()
            if "r" not in parts[1] or (len(parts) > 5 and parts[5] in ("[vvar]", "[vsyscall]")):
                continue
            lo, hi = (int(x, 16) for x in parts[0].split("-"))
            tail = b""
            for off in range(lo, hi, CHUNK):
                try:
                    mem.seek(off)
                    buf = mem.read(min(CHUNK, hi - off))
                except (OSError, OverflowError, ValueError):
                    break
                total += len(buf)
                data = tail + buf
                real += len(REAL.findall(data))
                caps += len(CAP.findall(data))
                tail = buf[-128:]
    return real, caps, total


def main() -> int:
    child = None
    if "--control" in sys.argv:
        fake = "sk-ant-api03-" + "F" * 45  # assembled at run time, so this file is not a match
        child = subprocess.Popen([sys.executable, "-c", "import sys,time; k=sys.stdin.readline(); time.sleep(60)"],
                                 stdin=subprocess.PIPE)
        assert child.stdin is not None
        child.stdin.write((fake + "\n").encode())
        child.stdin.flush()
        import time

        time.sleep(0.5)
    me = os.getpid()
    found = unreadable = scanned = 0
    gib = 0
    for name in sorted(os.listdir("/proc"), key=lambda s: (len(s), s)):
        if not name.isdigit() or int(name) == me:
            continue
        pid = int(name)
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
        except OSError:
            continue
        r = scan(pid)
        if r is None:
            unreadable += 1
            print(f"  ----  pid {pid:<6} {comm:<16} unreadable")
            continue
        scanned += 1
        real, caps, total = r
        gib += total
        tag = "REAL" if real else "ok"
        if real and child is not None and pid == child.pid:
            tag = "ctrl"
        elif real:
            found += 1
        print(f"  {tag:<4}  pid {pid:<6} {comm:<16} real={real} caps={caps} read={total // 1024} KiB")
    ok = True
    if child is not None:
        r = scan(child.pid)
        hit = bool(r and r[0])
        print(f"  control: fake key in pid {child.pid} {'found' if hit else 'NOT FOUND'}")
        ok = hit
        child.kill()
    print(f"memscan: {scanned} processes scanned ({gib / 2**20:.0f} MiB), {unreadable} unreadable, "
          f"{found} with a real key")
    return 0 if ok and found == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
