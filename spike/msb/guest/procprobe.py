#!/usr/bin/env python3
"""procprobe.py PID... -- try every /proc and ptrace route to another process's
secrets and say which ones open, without printing the secret itself.

For each PID it reports one line per route: `ok` (opened/read), `ok+CAP` (and
the bytes contained a tether cap, plain or base64), or the errno name. Routes: cmdline,
environ, maps, fd (list + reopen each /proc/PID/fd/N), mem (scan writable
mappings), process_vm_readv, ptrace(PTRACE_ATTACH).

A PID may be given as PID@ADDR to supply a readable address (from a reader
that can see maps) for process_vm_readv when maps itself is refused."""

from __future__ import annotations

import ctypes
import errno
import os
import sys

# A cap in plain text (env, npmrc), or as git's extraHeader carries it: base64("agent:tth").
CAPS = (b"tth_", b"YWdlbnQ6dHRo")
MAX_SCAN = 64 << 20
libc = ctypes.CDLL(None, use_errno=True)


class IoVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


def err(e: OSError | int) -> str:
    n = e if isinstance(e, int) else (e.errno or 0)
    return errno.errorcode.get(n, str(n))


def tag(data: bytes) -> str:
    return "ok+CAP" if any(c in data for c in CAPS) else "ok"


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def regions(pid: int) -> list[tuple[int, int]]:
    out = []
    for line in read(f"/proc/{pid}/maps").decode().splitlines():
        rng, perms = line.split()[:2]
        if perms.startswith("rw"):
            lo, hi = (int(x, 16) for x in rng.split("-"))
            out.append((lo, hi))
    return out


def probe(arg: str) -> None:
    pid_s, _, addr_s = arg.partition("@")
    pid = int(pid_s)
    try:
        comm = read(f"/proc/{pid}/comm").decode().strip()
    except OSError as e:
        comm = f"?{err(e)}"
    res: dict[str, str] = {}
    for name in ("cmdline", "environ", "maps"):
        try:
            res[name] = tag(read(f"/proc/{pid}/{name}"))
        except OSError as e:
            res[name] = err(e)
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
        got, opened = [], 0
        for fd in fds:
            # Non-blocking, so a reopened pipe or fifo with no writer cannot hang the probe.
            try:
                h = os.open(f"/proc/{pid}/fd/{fd}", os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            opened += 1
            try:
                got.append(os.read(h, 1 << 20))
            except OSError:
                pass
            finally:
                os.close(h)
        res["fd"] = f"{tag(b''.join(got))}({len(fds)} fds, {opened} reopened)"
    except OSError as e:
        res["fd"] = err(e)
    try:
        regs = regions(pid)
    except OSError:
        regs = []
    try:
        found, total = False, 0
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as m:
            for lo, hi in regs:
                if total >= MAX_SCAN:
                    break
                try:
                    m.seek(lo)
                    chunk = m.read(min(hi - lo, MAX_SCAN - total))
                except OSError:
                    continue
                total += len(chunk)
                found = found or any(c in chunk for c in CAPS)
        res["mem"] = ("ok+CAP" if found else "ok") + f"({total >> 10} KiB)" if regs else "open-ok(no maps)"
    except OSError as e:
        res["mem"] = err(e)
    addr = regs[0][0] if regs else (int(addr_s, 16) if addr_s else 0)
    if addr:
        buf = ctypes.create_string_buffer(4096)
        local, remote = IoVec(ctypes.addressof(buf), 4096), IoVec(addr, 4096)
        n = libc.process_vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
        res["vm_readv"] = "ok" if n >= 0 else err(ctypes.get_errno())
    else:
        res["vm_readv"] = "no-addr"
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    if libc.ptrace(16, pid, None, None) == 0:  # PTRACE_ATTACH
        os.waitpid(pid, 0)
        libc.ptrace(17, pid, None, None)  # PTRACE_DETACH
        res["ptrace"] = "ok"
    else:
        res["ptrace"] = err(ctypes.get_errno())
    print(f"{pid} {comm}: " + " ".join(f"{k}={v}" for k, v in res.items()), flush=True)


if __name__ == "__main__":
    for a in sys.argv[1:]:
        probe(a)
