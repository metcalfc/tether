# Progress

Read this first on every run. Tick items as they are verified, not when the
code is written. Add notes under each milestone: what was done, the
measurements, the decisions and why, and the exact next step.

## Next step

Milestone 2, first item: build libkrunfw with Landlock and load it with
`MSB_LIBKRUNFW_PATH`. Plan:
- Find the libkrunfw version msb 0.7.8 ships (`~/.microsandbox/lib/libkrunfw.5.dylib`,
  kernel 6.12.111 per `uname -r` in the guest; check `strings` for a
  version, or the microsandbox repo's pinned libkrunfw tag). Clone that tag
  of `containers/libkrunfw` into `overnight/run/libkrunfw/` (gitignored).
- Add `CONFIG_SECURITY=y`, `CONFIG_SECURITY_LANDLOCK=y` and
  `CONFIG_LSM="landlock,..."` to its kernel config (`config-libkrunfw_aarch64`).
- macOS build: libkrunfw's README builds the kernel in a Linux container.
  Docker 29.9.1 is available (`/usr/local/bin/docker`) and so is
  `limactl`. Build the kernel in an arm64 Linux container, then produce the
  dylib on the host (`make` on macOS links the generated `kernel.c`). Put
  every long step under `perl -e 'alarm shift; exec @ARGV' N`, run it in the
  background with a log under `overnight/run/`.
- Test: `MSB_LIBKRUNFW_PATH=<dylib> msb run --mount-dir ~/.microsandbox/tether-guest/bin:/opt/nono:ro debian:bookworm-slim -- /opt/nono/nono setup --check-only`
  should report a Landlock ABI (want V4+), and `cat /sys/kernel/security/lsm`
  should list landlock.
- Gate: about two runs of real effort, then take the nftables fallback.

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
- [x] Port `spike/guest/attack.sh` and `works.sh` to the msb guest; every BLOCK line holds
- [x] Claude Code end to end in the guest through leash (cheap model)
- [x] Measure boot to agent-ready, and time to first byte through leash versus direct

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

Run 3 (2026-10-18) notes:

- New: `spike/msb/guest/attack.sh` and `works.sh` (pushed to `/opt/leash/` by
  `cage.sh up`/`push`), and `cage.sh push|attack|works`. `cage.sh attack`
  starts a real `python3 -m http.server` on host `0.0.0.0:18002` as a
  control (checks it answers 200 from the host first), passes the host LAN IP
  (`ipconfig getifaddr en0`) in, and runs attack.sh as guest root.
- The DNS, ICMP, TCP and UDP probes are python3 socket code. The image has no
  dig/ping/ip/nft, so the "flush the firewall" step is a no-op. That's moot
  anyway, since enforcement is in the host msb process.
- **attack.sh in the cage: 24/24 PASS**, with 21 BLOCK lines and 3 ALLOW lines.
  - Blocked:
    - direct HTTPS to a name, to an IP literal, to api.anthropic.com and over
      IPv6;
    - DNS over UDP and TCP, both to 1.1.1.1 and to the guest resolver
      (172.16.0.5; NXDOMAIN or no answer counts as blocked);
    - raw ICMP to 1.1.1.1 and to the gateway;
    - UDP to the gateway port;
    - host :22, plus live host :18002 by name, by IP and by LAN IP;
    - the gateway port on the LAN IP;
    - 169.254.169.254;
    - through leash: example.com, a tunnel to api.anthropic.com, and a request
      with no capability;
    - no real key pattern in env or on disk.
  - Allowed: the gateway by name and by IP, and pypi via the proxy.
- **Negative control:** the same attack.sh in a sandbox with msb's default
  (open) network.
  - The direct HTTPS, IP literal, DNS (all 4) and ICMP-to-1.1.1.1 lines
    flipped to "allow" (11 FAILs). So those probes do detect egress when it
    exists.
  - Host ports stayed blocked even there: **msb's default policy denies the
    `host` group unless a rule allows it**. A third control
    (`--net-default deny --net-rule allow@host:tcp:18002`) showed the tcp
    probe does reach host :18002 when allowed. So the cage's host-port blocks
    are real.
  - IPv6 is blocked even when open (no global v6 in the guest). ICMP and UDP
    to the gateway get no reply even when open, so those two lines are weak
    evidence on their own.
- **works.sh in the cage: all ok.**
  - A requests wheel from files.pythonhosted.org (73 KB).
  - `npm view left-pad`.
  - `git clone metcalfc/tether-scratch` with the token injected on the host.
  - A clone of an unrouted repo (`metcalfc/imp`) is refused.
  - The Claude Code step was skipped, because it isn't installed yet (next
    item).
- No model calls this run.

Run 4 (2026-10-18) notes:

- `npm install -g @anthropic-ai/claude-code` in the guest, through the CONNECT
  allowlist: **16 s**, installs **Claude Code 2.1.303**. It needed nothing but
  registry.npmjs.org; no other host was refused during the install.
- `ANTHROPIC_MODEL=claude-haiku-4-5 claude -p "Reply with exactly: TETHER_OK"`
  in the guest prints `TETHER_OK`. The audit shows **exactly one request**:
  `POST /anthropic/v1/messages`, 200, 1.8 s, 114 KB up (system prompt and
  tools), model `claude-haiku-4-5-20251001`, 29,672 cache-write tokens,
  $0.0375. With `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, Claude Code made
  no calls to statsig, sentry, `/api/hello` or anything else. Plain
  forwarding works and no rewriting was needed, so that gate doesn't trigger.
- `cage.sh works` now exports `ANTHROPIC_MODEL=${CAGE_MODEL:-claude-haiku-4-5}`.
  The full works suite passes in the cage, Claude step included (second call
  $0.0032, cache read).
- Spend this run: 2 model calls, $0.0407 (leash report, session `cage`).

Run 5 (2026-10-18) notes:

- New: `spike/msb/bench.py` (stdlib). `ttfb` sends the same streaming
  haiku `/v1/messages` (max_tokens 32) N times to each target, interleaved,
  each on a fresh connection, and records time to headers, time to the first
  SSE `data:` line, and the mean SSE inter-chunk gap. Direct reads the key
  from the keychain inside the bench process only. `--gateway-only` runs it
  in the guest. `connect` times curl's `time_appconnect` to
  registry.npmjs.org via the proxy versus direct. Raw results in
  `overnight/run/bench-*.json` (not committed).
- **TTFB, host client, n=20 each:** direct p50 513.0 / p95 583.9 ms;
  through leash p50 516.3 / p95 567.9 ms. **Added at p50: +3.3 ms** (p95
  -16 ms, i.e. noise). Under the design's 5 ms target. SSE inter-chunk
  gap p50 15.7 ms direct vs 15.9 ms through leash (p95 16.6 vs 19.6): leash
  does not buffer the stream. Headers arrive together with the first event,
  so headers time = TTFB here.
- **TTFB from inside the guest** (through msb's netstack, then leash), n=20:
  p50 516.8 / p95 561.5 ms, so the msb hop adds about 0.5 ms on top.
- **CONNECT setup** (TLS ready to registry.npmjs.org): host direct p50 37.0 /
  p95 48.5 ms; host through leash's CONNECT p50 32.7 / p95 38.6 ms (n=10;
  leash being slightly faster is likely address-selection noise). **From the
  guest through the proxy: p50 ~86 ms** (range 80-91, n=10): about +50 ms per
  new tunnel, i.e. each round trip through msb's userspace netstack costs
  on the order of 10-15 ms of the TCP + CONNECT + TLS handshakes. A plain
  HTTP request to leash from the guest pays it once (the 0.5 ms above is
  within noise of that being small for one already-ACKed request); tunnels
  pay it per handshake RTT. Worth re-measuring in milestone 7.
- **Boot to agent-ready** (node:22-bookworm cached, 4 vCPU, 4 GiB):
  `cage.sh up` (leash init + `msb create` + first exec + pushing env and
  scripts) **0.63 s**, of which create-to-first-exec 0.38 s. Claude Code is
  not in the image: `npm install -g @anthropic-ai/claude-code` plus `claude
  --version` through the CONNECT allowlist took **10.8 s** (16 s in run 4).
  So **~11.4 s to agent-ready with install, ~0.7 s with a pre-baked image**
  (the 0.7 s is create+push plus `claude --version` at 0.08 s; I did not build
  a pre-baked image).
- **Memory:** `msb machine` RSS 1.1 GB after the npm install (guest page cache
  is counted; the guest itself reports 84 MB used, 504 MB buff/cache of 3.9
  GB); leash 36 MB RSS.
- Spend this run: 44 calls via leash, $0.0057 (leash report, session `cage`
  now $0.0464 total); plus 22 direct calls, not metered by leash, estimated
  at under $0.005 from the same token counts.
- No gate triggered (+3.3 ms << 50 ms, no stalled streams).

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
| 4 | 2 (haiku, via leash) | $0.0407 |
| 5 | 44 via leash + 22 direct (haiku, bench) | $0.0057 via leash; direct est. < $0.005 |

## Decisions and corrections
