# Progress

Read this first on every run. Tick items as they are verified, not when the
code is written. Add notes under each milestone: what was done, the
measurements, the decisions and why, and the exact next step.

## Next step

Milestone 1, second item: port `spike/guest/attack.sh` and `works.sh` to the
msb guest as new files under `spike/msb/guest/` (cage.sh pushes
`spike/msb/guest/*.sh` to `/opt/leash/` on `up`). Run them as root with
`./spike/msb/cage.sh gw-up && ./spike/msb/cage.sh up && ./spike/msb/cage.sh exec /opt/leash/attack.sh`.
Differences from the QEMU guest to handle: the image is `node:22-bookworm`
(no `dig`, `ip`, `ping`, `nft`, `sudo`; you are already root), the gateway is
`http://host.microsandbox.internal:18001` (`$LEASH_GW`), the msb gateway IP is
`172.16.0.5` (read it from `/etc/hosts` rather than hard-coding it), and there's
no 10.0.2.x. For DNS probes, use bash `/dev/udp` or a python3 socket rather than
installing dig. Finish with `./spike/msb/cage.sh down`.

## 0. Setup

- [x] Read `design/tether-poc.md` in full; note any questions here
- [x] Create branch `poc/tether` from `spike/mac-lockdown`
- [x] `make test` passes (14 tests); `overnight/preflight.sh` passes
- [x] Install microsandbox (`msb`) per-user; record the version and commit
- [x] Install nono per-user; record the version

Run 1 (2026-10-18) notes:

- Design read in full. No blocking questions. Open points to settle while
  building: (a) the design pins microsandbox `44b0a8f` (Oct 17) but the latest
  release is **v0.7.8** (tag commit `7b7b9dc`, Oct 9); I use the release
  binary, since building msb from source isn't needed for the cage. Upstream
  main is at `03d1a1e` tonight. (b) The demo's "Claude fixes a failing test
  ... push to `acme/app`" becomes a push to `SCRATCH_REPO` on an `agent/*`
  branch.
- Branch `poc/tether` created from `spike/mac-lockdown` (f17bc60).
- `make test`: 14 tests OK. `overnight/preflight.sh`: all ok (one warn: sleep
  not disabled; the loop runs under caffeinate).
- **msb 0.7.8** installed by hand from the release tarball (sha256 checked
  against `checksums.sha256`) into `~/.microsandbox/bin/msb` and
  `~/.microsandbox/lib/libkrunfw.5.dylib` (+ `libkrunfw.dylib` symlink).
  I did not run `install.sh`, because it also links into `~/.local/bin` and
  can edit shell rc files. Not on PATH: call it as `~/.microsandbox/bin/msb`.
  Smoke test: `msb run alpine -- uname -a` boots and prints
  `Linux ... 6.12.111 ... aarch64`, with no GUI prompt.
- **nono 0.80.0** (sha256 checked against `SHA256SUMS.txt`): the host build is
  in `~/.local/bin/nono`, and the guest build (linux aarch64 glibc) is in
  `~/.microsandbox/tether-guest/bin/nono`. It is dynamically linked against
  glibc, so use a Debian/Ubuntu guest image, not alpine.
- **Design seam confirmed:** `msb run --mount-dir ~/.microsandbox/tether-guest/bin:/opt/nono:ro debian:bookworm-slim -- /opt/nono/nono setup --check-only`
  prints `Landlock is not available: ... No supported Landlock ABI detected`,
  and `/sys/kernel/security/lsm` doesn't exist on kernel 6.12.111. Milestone 2
  is required.
- msb flags seen in `msb run --help` that the design relies on: `--net-default`,
  `--net-rule`, `--tmpfs`, `-u/--user`, `--mount-dir` (all present). There is
  also `--no-net` (sugar for `--net-default deny`).

## 1. Cage (design day 1)

- [x] An msb sandbox with `--net-default deny --net-rule "allow@host:tcp:P"`, with today's leash on the host
- [ ] Port `spike/guest/attack.sh` and `works.sh` to the msb guest; every BLOCK line holds
- [ ] Claude Code end to end in the guest through leash (cheap model)
- [ ] Measure boot to agent-ready, and time to first byte through leash versus direct

Run 2 (2026-10-18) notes:

- New files: `spike/msb/policy.json` (leash policy: listen `127.0.0.1:18001`,
  advertise `http://host.microsandbox.internal:18001`, both keys via
  `cmd:security find-generic-password ...`, github paths limited to
  `metcalfc/tether-scratch`, spend cap $2/session/day, state in
  `overnight/run/leash-msb/`) and `spike/msb/cage.sh` (`gw-up`, `gw-down`,
  `up`, `exec`, `check`, `down`, `status`). The gateway runs in the background
  with a pidfile at `overnight/run/cage-gw.pid` and a log at `cage-gw.log`.
- Image: **`node:22-bookworm`**, not debian-slim, because it ships git, curl,
  python3 and CA certs, and Claude Code can be `npm install`ed through the
  CONNECT allowlist (registry.npmjs.org). The image can't be provisioned with
  an open network first: `msb modify` can't change network rules.
- `cage.sh check` (run as guest root), all PASS:
  gateway reachable (404 from leash on `/`); host port 22 and host port
  18002 blocked, **with a real `python3 -m http.server` listening on
  127.0.0.1:18002 as a control** (the host saw 200, the guest got 000);
  direct `https://example.com`, `https://1.1.1.1` and `https://api.anthropic.com`
  blocked; `getent hosts example.com` returns nothing (NXDOMAIN). Through
  the proxy, `pypi.org` returned 200 and `example.com` was refused.
- Guest view: `/etc/hosts` maps `host.microsandbox.internal` to `172.16.0.5`
  (the /30 gateway); the guest has only a link-local IPv6 address and no IPv6
  default route under `--net-default deny`, which partly answers the design's
  IPv6 open question: there's no global v6 to try.
- Boot: `msb create` until the first `msb exec` returns took **0.37 s**, with
  the image cached. That's a create-to-exec time, not boot to agent-ready
  (item 4).
- No model calls this run.

## 2. Kernel (design day 2, gate)

- [ ] Build libkrunfw with `CONFIG_SECURITY=y`, `CONFIG_SECURITY_LANDLOCK=y` and landlock in `CONFIG_LSM`; load with `MSB_LIBKRUNFW_PATH`
- [ ] `nono` in the guest reports Landlock ABI V4 or later
- [ ] GATE: if this fails, record it and switch to the nftables fallback (see PROMPT.md)

## 3–4. tether core

- [ ] Copy `./leash` to `./tether` and `tests/test_leash.py` to `tests/test_tether.py`; rename inside; both suites pass
- [ ] Channel binding: one listener per sandbox
- [ ] Capability registry: `tether cap mint|revoke|list`, minted per boot
- [ ] Per-cap policy intersected with the ceiling
- [ ] CONNECT hardening: resolve on the host; refuse private addresses; connect to the checked IP; match SNI; refuse IP literals
- [ ] Streamed request bodies (large `git push`)
- [ ] `Upgrade` passthrough on routes
- [ ] Spend reservations and a max-inflight limit; non-retryable refusal
- [ ] Hash-chained audit with the new fields and `x-tether-request-id`
- [ ] Unit tests for each item; all tests pass

## 5. Identity in the guest

- [ ] Per-agent Unix users; shared `/work` (group, setgid, default ACLs)
- [ ] nono session profile per agent; command policies for git and npm; capability delivery via tmpfs
- [ ] Verify one tool can't read another's capability (`/proc/<pid>/environ`)

## 6. Compiler and conformance

- [ ] `tether compile`: one policy file emits msb flags, nono profiles and tether config, with subset checks and wildcard lints
- [ ] `tether report --by tool`
- [ ] Claude Code conformance: betas forwarded, SSE pings, `count_tokens`, error bodies, `HEAD /api/hello`, `skipWebFetchPreflight`

## 7. Adversarial suite

- [ ] Injected README (paste site, push to another org)
- [ ] Malicious npm package fixture (postinstall)
- [ ] Root in the guest: kill nono, flush nftables, try DNS, raw IPs and host ports
- [ ] Capability replayed from a second sandbox gets a 403
- [ ] Four concurrent streams against the cap; record the overshoot

## 8. Demo and write-up

- [ ] `spike/RESULTS.md` has a PoC section with every success criterion and measurement
- [ ] Demo recorded to `overnight/run/poc-demo.mp4` (or `overnight/DEMO.md`)
- [ ] `overnight/REPORT.md` written
- [ ] The real-key `git grep` from PROMPT.md is clean; branch pushed; draft PR opened
- [ ] `DONE` written to `overnight/STATUS`

## Spend log

| Run | Model calls | USD (from tether report) |
| --- | --- | --- |

## Decisions and corrections
