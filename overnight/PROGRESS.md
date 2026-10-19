# Progress

Read this first on every run. Tick items as they are verified, not when the
code is written. Add notes under each milestone: what was done, the
measurements, the decisions and why, and the exact next step.

## Next step

Milestone 5 is done (run 18). **Next: milestone 6, item 1: `tether
compile`.** This starts a new milestone, so re-read `design/tether-poc.md`
first (the policy model and compiler sections). One policy file should emit
(a) the msb flags `cage.sh up` passes today (`--net-default deny --net-rule
allow@host:tcp:P --tmpfs /run/tether ...`), (b) the nono profiles that
`spike/msb/guest/nono.sh write_profile` builds in python today (move that
logic into tether, so nono.sh only installs the emitted JSON), and (c)
tether's own config (sandboxes, routes, egress, per-agent and per-tool cap
grants that `cage.sh agent_grants`/`tool_grants` hard-code today). Subset
checks: every cap's routes/egress must be inside the ceiling, every tool's
grants inside its agent's sandbox; wildcard lints (e.g. `*.github.com` in
egress, `**` routes). Unit tests in `tests/test_tether.py`, then prove it in
the cage by booting from compiled output and rerunning `cage.sh nono` and
`cage.sh proc` (both should stay 19/19 and 11/11).

Cage env used in runs 11 and 16 (tether as the gateway, Landlock kernel):

```sh
export MSB_LIBKRUNFW_PATH=$PWD/overnight/run/krunfw-landlock/libkrunfw.5.dylib \
  CAGE_GW=$PWD/tether CAGE_POLICY=$PWD/overnight/run/tether-msb/policy.json
spike/msb/cage.sh gw-up && spike/msb/cage.sh up && spike/msb/cage.sh users
# ... and always: spike/msb/cage.sh down
```

`overnight/run/tether-msb/policy.json` is gitignored; it is
`spike/msb/policy.json` with `state_dir` pointing at
`overnight/run/tether-msb/state`. If it is missing, recreate it that way.

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

- [x] Build libkrunfw with `CONFIG_SECURITY=y`, `CONFIG_SECURITY_LANDLOCK=y` and landlock in `CONFIG_LSM`; load with `MSB_LIBKRUNFW_PATH`
- [x] `nono` in the guest reports Landlock ABI V4 or later
- [x] GATE: if this fails, record it and switch to the nftables fallback (see PROMPT.md) -- **gate passed, no fallback**

Run 6 (2026-10-18) notes:

- **Source:** microsandbox's fork is `github.com/superradcompany/libkrunfw`
  (the `zerocore-ai` and `microsandbox` org URLs redirect to it), not
  `containers/libkrunfw`. Commit `21f169f` ("reject stale kernel bundles")
  exists there: `FULL_VERSION = 5.6.1`, `linux-6.12.111`, matching the
  shipped dylib's `Linux version 6.12.111 (root@libkrunfw) ... Fri Jul 24
  13:01:52 WAT 2026` banner. Upstream containers/libkrunfw v5.6.2 is still on
  6.12.109 and lacks microsandbox's patches (0031-0035), so the fork matters.
  Cloned into `overnight/run/libkrunfw` (gitignored).
- **New:** `spike/msb/kernel/landlock.config` (the fragment: `SECURITY`,
  `SECURITYFS`, `SECURITY_NETWORK`, `SECURITY_PATH`, `SECURITY_LANDLOCK`,
  `LSM="landlock"`; no Yama/AppArmor/SELinux), `build.sh` (patches the
  aarch64 config, builds `kernel.c` in a `fedora:latest` arm64 Docker
  container, links the dylib on the host with the fork's Makefile),
  `check.sh` + `guest-check.sh` (boot a throwaway sandbox on a given dylib
  and probe Landlock).
- **Decision:** build inside the container's own filesystem (rsync `/src` to
  `/build`), not on the bind mount as the fork's `build_in_docker.sh` does:
  the kernel tree has case-only filename clashes (e.g. netfilter
  `xt_TCPMSS.c`/`xt_tcpmss.c`) that a default case-insensitive APFS volume
  can't hold, and virtiofs is slow for a kernel build. Only `kernel.c` and the
  final `.config` come back.
- **Build:** one attempt, exit 0, **~11 min** wall (dnf builddep ~5 min,
  kernel ~6 min on 10 vCPUs). The resulting `.config` has `CONFIG_SECURITY=y`,
  `CONFIG_SECURITY_LANDLOCK=y`, `CONFIG_LSM="landlock"`. Dylib 24.2 MB,
  ad-hoc linker-signed, sha256 `c9d1d272...0742`. ld warns about reducing the
  `__DATA` alignment from 64K to 16K; the guest boots fine.
- **Check (stock dylib, negative control):** no `/sys/kernel/security/lsm`,
  `landlock_create_ruleset` returns ENOSYS, `nono setup --check-only` fails
  with "No supported Landlock ABI detected".
- **Check (`MSB_LIBKRUNFW_PATH=.../krunfw-landlock/libkrunfw.5.dylib`):**
  `lsm: capability,landlock`; `landlock_create_ruleset(..., VERSION)` returns
  **6**; `nono setup --check-only` says **Landlock V6** with TCP network
  filtering, "Installation verified". The Landlock TCP rule holds: with a
  python listener on guest 127.0.0.1:8080, unsandboxed connect is ok, under
  `nono run --allow-connect-port 18001` it gets **EACCES** (immediate), and
  under `--allow-connect-port 8080` it connects. msb loads the ad-hoc dylib
  with no prompt (the `disable-library-validation` entitlement is on msb, as
  the design says).
- **Cage regression on the new kernel:** `cage.sh gw-up && up && attack`
  with `MSB_LIBKRUNFW_PATH` exported: "all held: the only way out is the
  gateway" (boot to first exec 0.26 s). The kernel change doesn't touch the
  cage: enforcement is in the host msb process.
- **Note for milestone 5:** inside an msb sandbox, agentd does not mount
  securityfs, so `/sys/kernel/security/lsm` is absent unless you
  `mount -t securityfs securityfs /sys/kernel/security`. nono doesn't need it
  (it probes with the syscall).
- nono needs `--allow-cwd` (or a profile) in non-interactive `nono run`.
- No model calls this run.

## 3–4. tether core

- [x] Copy `./leash` to `./tether` and `tests/test_leash.py` to `tests/test_tether.py`; rename inside; both suites pass
- [x] Channel binding: one listener per sandbox
- [x] Capability registry: `tether cap mint|revoke|list`, minted per boot
- [x] Per-cap policy intersected with the ceiling
- [x] CONNECT hardening: resolve on the host; refuse private addresses; connect to the checked IP; match SNI; refuse IP literals
- [x] Streamed request bodies (large `git push`)
- [x] `Upgrade` passthrough on routes
- [x] Spend reservations and a max-inflight limit; non-retryable refusal
- [x] Hash-chained audit with the new fields and `x-tether-request-id`
- [x] Unit tests for each item; all tests pass

Run 7 (2026-10-18) notes:

- Re-read the tether section, policy model, plan and success criteria.
- `./tether` is a copy of `./leash` with: prog name, docstring and usage,
  log prefix `[tether]`, `server_version`, Basic realm and error-message
  prefix `tether`, cap prefix **`tth_`** (was `lsh_`), env var
  **`TETHER_CAPABILITY`** (was `LEASH_CAPABILITY`), default advertise
  `http://tether:8080`, and default state dir **`run/tether`** (relative to
  the policy file; was `run`). `tests/test_tether.py` is the renamed copy plus
  `test_tether_names` (cap prefix, env var, default state dir).
- `x-tether-request-id` is **not** added yet: it belongs to the audit item
  (item 9), with its own test.
- `spike/msb/guest/works.sh` now masks both `lsh_` and `tth_` caps.
- Makefile: `make tether-test`, `make leash-test`; `make lint` now covers
  `tether`. `make test` discovers both suites: **29 tests OK** (leash 14
  unchanged, tether 15). ruff clean.
- No model calls this run, no VM booted.

Run 8 (2026-10-18) notes:

- **Policy:** new optional `sandboxes: {name: {listen, advertise?,
  sessions?}}`. `advertise` defaults to
  `http://host.microsandbox.internal:<listen port>`; `sessions` defaults to
  `[name]`. Without `sandboxes`, the top-level `listen`/`advertise`/`sessions`
  are one sandbox called `default` (leash's shape, so `spike/msb/policy.json`
  still loads). A session listed in two sandboxes, or `sessions` at the top
  level next to `sandboxes`, is a `ValueError`.
- **Gateway:** `build_servers(policy) -> ({sandbox: Server}, Gateway)`
  replaces `build_server`; every listener shares one Gateway (ledger, audit,
  caps). `make_handler(gw, sandbox)` bakes the sandbox name into the handler.
  `Gateway.identify(presented, sandbox) -> (session, refusal)` replaces
  `session_for`: a real cap bound to another sandbox is refused with **403**
  (CONNECT too: 403, not 407, since the cap is real but on the wrong
  channel), and never reaches the upstream. The guest sees only "capability
  is not valid on this sandbox's channel"; the audit line has the detail.
- **Audit:** every line now carries `sandbox`; the stderr log shows
  `[sandbox/session]`. Env files are per sandbox advertise URL.
- **Tests:** `test_audit_names_the_sandbox` plus a `ChannelBindingTest` class
  (two listeners; cap A 200 on A and 403 on B for routes and CONNECT, with
  no upstream call; env advertises each listener; duplicate session refused).
  `make test`: **35 tests OK** (leash 14 unchanged, tether 21). ruff clean.
- **Real run:** `tether serve` with two sandboxes on 127.0.0.1:18101/18102
  (policy in `overnight/run/cb/`), curl through each port to
  registry.npmjs.org: own cap on own port 200 (both directions), the other
  sandbox's cap CONNECT **403**, audited as e.g. `DENY [other/-] egress
  registry.npmjs.org:443 -- capability of session chad-dev is bound to
  sandbox chad-dev, used on other`. Not yet tried from inside an msb guest
  (that is milestone 7 item 4).
- Note: the Bash tool's shell is zsh, which doesn't word-split `$var`; run
  ad-hoc loops under `/bin/bash -c`.
- No model calls this run, no VM booted.

Run 9 (2026-10-18) notes:

- **Registry:** `state_dir/caps.json` (0600), `{"caps": {sha256(cap):
  record}}`; record = `cap_id` (`cap_` + 12 hex), `sandbox`, `session`,
  `agent`, `tool`, `human`, `routes`, `egress`, `spend_usd`, `minted`,
  `expires`, `revoked` (epoch seconds or null). **Decision: keyed by hash**, so
  the registry and `cap list --json` never hold a usable cap; the cap is
  printed once by `mint` (or written with `--env PATH`) and lives only in env
  files / the guest tmpfs. Writers take `flock(caps.lock)`, rewrite to a temp
  file and `os.replace`; `serve` stats the file on each lookup (inode, mtime_ns,
  size) and reloads on change. That is how revoke bites with no restart.
- `sessions.json` is gone; there is **no migration** from it (only throwaway
  tether state dirs existed). leash and `spike/msb/cage.sh` are unaffected
  (cage.sh still runs `./leash`).
- **CLI:** `tether cap mint --session S [--sandbox] [--agent] [--tool]
  [--human] [--route R]... [--egress H]... [--spend USD] [--ttl 30m] [--env
  PATH]`; `cap revoke [CAP_ID...] [--sandbox S] [--session S]` (exit 1 if
  nothing live matched); `cap list [--all] [--json]`; `cap boot SANDBOX`
  (revoke every cap of that sandbox, mint fresh default session caps, rewrite
  `<session>.env`: the "minted per boot" step). `tether init` and `serve`
  call `init_caps`: one default cap per session, idempotent (keeps a live cap
  found in the env file; re-mints if it was revoked/expired). `serve` no
  longer rewrites env files with existing caps (it can't: only hashes are
  stored).
- **identify** returns `(record, (audit_reason, guest_message))`. Revoked or
  expired caps get **403** "capability is revoked/expired" (not 401/407: the
  cap is real, a retry won't help); wrong sandbox 403 as before; a record
  whose session is no longer in that sandbox's policy is refused too. Audit
  lines now carry `cap_id`, `agent`, `tool`, `human` when set (item 9 adds the
  rest); the stderr log shows `[sandbox/session/agent/tool]`.
- **Tests:** shared `TwoSandboxes` fixture; `RegistryTest` (7): mint prints a
  working cap, registry holds no cap and is 0600; list shows records never
  caps; revoke via the CLI (its own Registry, as a separate process would) on
  a running server, routes and CONNECT, no upstream call; revoke by sandbox;
  expired cap refused (clock shifted); cap minted for A refused on B, mint
  outside the sandbox or with an unknown route refused; init idempotent and
  boot rotates one sandbox only. `make test`: **42 tests OK** (leash 14
  unchanged, tether 28). ruff and shellcheck clean.
- **Real run** (`overnight/run/cb/real.sh`, two listeners 18101/18102, CONNECT
  to registry.npmjs.org, CLI in separate processes against a live `serve`):
  own cap 200; on the other port refused; minted claude/npm cap (ttl 30m) 200;
  after `cap revoke <id>` refused while the session cap still 200; after
  `cap boot chad-dev` the old session cap is refused and the new one 200.
  Audit e.g. `DENY [chad-dev/-] egress registry.npmjs.org:443 -- capability
  cap_c07fe1b17409 of session chad-dev is revoked`.
- No model calls this run, no VM booted.

Run 10 (2026-10-18) notes:

- **Ceiling** = the policy's `routes` (with a resolved credential),
  `egress.allow` and `spend` caps. A cap record's `routes`, `egress` and
  `spend_usd` narrow it; None = no narrowing. Enforced in serve on every
  request, so a hand-edited `caps.json` still can't widen the ceiling:
  - **routes:** `Gateway.route_ok` after `identify`; an ungranted route gets
    **403** "route /x/ is not granted to this capability", audited as
    `route github not granted to capability cap_...`, never reaches upstream.
  - **egress:** `egress_ok(host, port, cap)` checks port, credentialed host,
    the ceiling allowlist, then the cap's grant ("host is not in capability
    cap_...'s egress grant"). 403 before any connect.
  - **spend:** `Ledger` has a second map `caps` (`{cap_id: usd}`) next to
    `spent`, kept separate so the team total doesn't double-count; only caps
    with a `spend_usd` are tracked there. `cap_check(cap)` checks session,
    team, then the sub-cap. Refusal is 403 (non-retryable), like the others.
- **Mint-time subset check** (`grant_errors`): unknown route; an egress
  pattern not inside some ceiling pattern (`pattern_within`: equal, or a
  literal host the ceiling matches, or a `*` pattern the ceiling matches as a
  string *when the ceiling has no `?`/`[`*, since a `?` in the ceiling would
  match one `*`); `?`/`[]` in a cap pattern is refused outright; a negative
  sub-cap, or one above the session cap. Error: `tether: cap must fit inside
  the ceiling: ...`.
- **Tests:** `TwoSandboxes.configure()` hook; `CapPolicyTest` (4): route not
  granted (403, no upstream call; session cap still gets github); npm cap
  egress [registry.npmjs.org] refused pypi.org (ceiling allows it) and
  localhost, a localhost-granted cap tunnels, and a `["*"]` grant can't widen
  the ceiling; sub-cap $0.50 refuses call 2 while the session ($5 cap) still
  succeeds, ledger `caps` = {cid: 1.0}, `spent` = {o1: 2.0}; mint refuses `*`,
  `*.npmjs.org` (ceiling has only the literal), `example.com`, `?ypi.org`,
  disabled route `openai`, spend 6 > 5, and accepts `files.pythonhosted.org`,
  `*.pythonhosted.org`, spend 5. `make test`: **46 tests OK** (leash 14
  unchanged, tether 32). ruff and shellcheck clean.
- **Real run** (`overnight/run/cp/real.sh`, live `tether serve` on
  127.0.0.1:18111, CLI as separate processes, real upstreams): session cap
  CONNECT pypi.org 200; npm cap registry.npmjs.org 200, pypi.org refused
  (DENY "not in capability cap_574e1148db77's egress grant"); session cap
  GET `/github/metcalfc/tether-scratch.git/info/refs` 200, claude cap
  (`--route anthropic`) 403; claude cap with `--spend 0.00001`: haiku message
  1 200 ($0.000033), message 2 403 "sub-cap reached", session cap message
  200. Mint `--egress '*.org'` and `--spend 3` (session cap $2) refused.
- Spend this run: 2 haiku calls via tether (max_tokens 5), **$0.000066**
  (from `state/spend.json` of the real run).
- No VM booted.

Run 11 (2026-10-19) notes:

- **do_CONNECT order now:** identify -> 403 refusal -> `egress_ok` (port,
  credentialed host, **IP literal refused** "IP literal; CONNECT by name",
  ceiling, cap grant) -> 407 without a cap -> `Gateway.resolve` (one
  `getaddrinfo` on the host; **if any** resolved address is non-public, the
  CONNECT is refused 403 and the audit carries `addrs`) -> dial the checked
  addresses in order (never the name again) -> 200 -> read the guest's first
  TLS records (10 s timeout, handshake may span records, 64 KiB cap) ->
  `client_hello_sni` must equal the CONNECT host, else close and audit
  `deny` -> forward the hello bytes, then `pump`. Allow lines carry `addr`.
- **Address rule** (`addr_refusal`): allowed iff `is_global and not
  is_multicast` (IPv4-mapped v6 unwrapped first), or inside policy
  `egress.allow_addresses: [CIDR...]` (new). Python's `is_global` already
  excludes loopback, RFC 1918, link-local/metadata, CGNAT 100.64/10, ULA
  (fd00:ec2::254), unspecified, benchmarking 198.18/15 and documentation
  ranges; multicast needed the explicit check (224.0.0.1 `is_global` is True).
- **SNI parse fails closed:** not TLS, not a ClientHello, truncated/malformed,
  no SNI, or an ECH extension (0xfe0d) present -> refused. After the 200 there
  is no HTTP status to send, so the guest just sees the connection close (an
  RST if its bytes are unread).
- **Tests:** test policies get `allow_addresses: ["127.0.0.1/32", "::1/128"]`
  (macOS `localhost` resolves to both). Echo tests now send real ClientHello
  bytes from `ssl.MemoryBIO` + `wrap_bio(server_hostname=...)`; `TwoSandboxes.connect`
  sends one after a 200. New: IP literal refused (wire + `egress_ok`); name
  resolving to 127.0.0.1 refused without the allowlist, audit has `addrs`;
  SNI mismatch refused with nothing reaching the upstream; plain HTTP bytes
  refused; parser unit tests (case-folded SNI, no SNI, ECH, truncated, not a
  ClientHello, a real no-SNI hello); a hello split over three records;
  `addr_refusal` table. `make test`: **53 tests OK** (leash 14 unchanged,
  tether 39). ruff clean. The suite takes ~50 s (~1 s per test) -- it already
  did before this change (45 s for 46); not investigated.
- **Real run** (tether as the cage gateway, Landlock kernel): from the guest,
  registry.npmjs.org 200, pypi.org 200, example.com refused, `--connect-to
  registry.npmjs.org:443:pypi.org:443` (a fronting attempt: CONNECT pypi.org,
  SNI registry.npmjs.org) refused with `SNI registry.npmjs.org does not match
  CONNECT host pypi.org`, `https://104.16.0.35/` refused as IP literal.
  `cage.sh attack`: 24 PASS, "all held". `npm install -g
  @anthropic-ai/claude-code` through the hardened CONNECT: ok (2.1.303).
  `cage.sh works`: all worked, incl. Claude Code (haiku) `TETHER_OK`. Resolved
  addresses audited: 104.16.7.34, 151.101.{0,64,192}.223 -- all public, so the
  host's resolver returns nothing the rule trips on.
- Spend this run: 1 Claude Code call via tether (haiku), **$0.0374** (tether
  report, session `cage`; mostly the cache write of Claude Code's prompt).

Run 12 (2026-10-19) notes:

- **`RequestBody`** (new class) replaces `_read_body`. Non-metered routes
  stream: a Content-Length body is read in 64 KiB pieces and forwarded with
  the same Content-Length; a chunked body is de-chunked as it arrives and
  re-chunked upstream (`http.client` `encode_chunked=True` with an iterator).
  Trailers are dropped. No size limit on streamed routes.
- **Decision: metered routes still buffer**, bounded by `MAX_BODY` (64 MiB),
  and go upstream with a Content-Length even if the client sent chunked. A
  model call is small JSON, and spend reservations (item 8) need its
  `max_tokens` and size at admission. Nothing reads a non-metered body.
- Body errors: bad Content-Length or malformed chunk framing -> 400 and an
  audit `deny`; client stops mid-body (EOF or 120 s idle, `BODY_IDLE_SECS`)
  -> upstream connection closed, audit `allow` with **status 499**, reason
  `client stopped mid-body after N bytes`, `bytes_up` N, no response.
  `bytes_up` is now bytes actually taken from the client (also on 502).
- **Tests** (5 new): 106 MiB chunked body (over MAX_BODY) reaches a sink
  intact (sha256) with `tracemalloc` peak < 16 MiB across client, gateway
  and sink; Content-Length body forwarded with its length, not chunked;
  client hang-up after 1000 of 1000000 bytes audited 499 / bytes_up 1000;
  bad chunk size refused and audited; metered route refuses a body over
  MAX_BODY (patched to 1000) while a streamed route passes it, and a chunked
  model call goes upstream with Content-Length. `make test`: **58 tests OK**
  (leash 14 unchanged, tether 44). ruff and shellcheck clean.
- **Real run** (tether cage gateway, Landlock kernel, 4 GiB guest): in the
  guest, a commit of 100 MiB of `/dev/urandom` pushed with plain `git push`
  to `agent/stream-test-20261019001321` on `metcalfc/tether-scratch` through
  the `/github/` route: ok. Audit: `git-receive-pack` 200, **bytes_up
  104,890,010** (chunked; git's postBuffer is 1 MiB), 34.2 s (~3 MB/s,
  likely the uplink). Gateway RSS after the push: 38 MB. Branch then
  deleted through the same route (`git push --delete`); `ls-remote` shows
  no `stream-test` branch.
- No model calls this run.

Run 13 (2026-10-19) notes:

- **`Upgrade` passthrough:** `_proxy` checks `wants_upgrade(hdrs)`
  (`Connection` lists `upgrade` and `Upgrade:` is set) **after** the
  identify / path allowlist / cap / route-grant checks, so a refused upgrade
  never opens an upstream connection. Then `_upgrade` opens a raw socket
  (`ssl.create_default_context()` with SNI for https upstreams, 30 s connect
  timeout), sends the request line and the usual forwarded headers (hop and
  credential headers stripped, credential injected by `route.apply`) plus
  `Upgrade: <proto>` and `Connection: Upgrade`, reads the response head
  (`read_head`, 64 KiB cap). **101:** relay the head and any bytes after it,
  forward bytes the client pipelined past its request head (drained from
  `rfile`'s buffer without blocking, `buffered_bytes`), then `pump` both
  sockets as CONNECT does; audit `status 101`, `upgrade`, `bytes_up`,
  `bytes_down`, `ms` at close. **Other status:** parse it with
  `http.client.HTTPResponse` over a `ReplaySocket` (the bytes already read
  put back in front), relay as a chunked response with `Connection: close`,
  audit "upstream refused the upgrade".
- **Decision: metered routes refuse upgrades** (403 "upgrade is not offered
  on metered route", audited, before any upstream connect). The meter reads
  usage out of HTTP bodies and can't parse WebSocket frames, so a WebSocket
  model call would step around the spend cap. Codex falls back to its
  SSE/HTTPS transport (design R2). An upgrade carrying a request body is
  refused 400.
- `pump` now drains `SSLSocket.pending()` after each recv, so decrypted
  bytes select() can't see are not stranded on a TLS upstream.
- **Tests** (5 new, raw-socket `WsUpstream` fake that answers 101 with the
  RFC 6455 accept and echoes): 101 splice with a 200 KB frame echoed byte for
  byte, pipelined early bytes delivered, upstream sees `GET /base/mcp/socket?x=1`
  with `Upgrade`/`Connection` kept, the injected Basic credential, no
  `X-Api-Key` and no cap; no cap 401 / ungranted route 403 / off-allowlist
  path 403 with **no** upstream connection; metered route 403 with nothing
  upstream; upstream 426 relayed with its headers and body; `wants_upgrade`
  table. `make test`: **63 tests OK** (leash 14 unchanged, tether 49). ruff
  clean. (The `ConnectionAbortedError` tracebacks in the test output come
  from `echo_server`'s accept thread at teardown and appear on the
  pre-change tree too: 58 of them. Cosmetic, not fixed.)
- **Real run** (`overnight/run/ws/`, live `tether serve` on 127.0.0.1:18121,
  temporary route `echo` -> `https://echo.websocket.org`, not in any
  committed policy): handshake `HTTP/1.1 101 Switching Protocols`, server
  greeting received, a masked text frame `tether-ws-ok` echoed. Audit:
  `allow status 101 upgrade websocket bytes_up 18 bytes_down 328 ms 428`.
  Not tried from inside the msb guest (no WebSocket client there yet; the
  path is the same `/route/` channel the guest already uses).
- Known gap, not this item: `do_CONNECT` reads the ClientHello from the raw
  socket and would lose bytes a client pipelined into `rfile`'s buffer;
  clients wait for the 200, so it hasn't mattered.
- No model calls this run, no VM booted.

Run 14 (2026-10-19) notes:

- **Reservations:** on a metered route the body is read (buffered, as
  before) **before** admission, then `reserve_usd(prices, body)` =
  `(len(body)/4 * input + max_tokens * output) / 1e6` at `price_for(model)`.
  The output limit is the first int of `max_tokens`, `max_output_tokens`,
  `max_completion_tokens`; **decision: a JSON body naming none reserves
  `DEFAULT_MAX_TOKENS` = 4096** (Messages requires max_tokens; OpenAI's
  APIs don't). A body that isn't a JSON object (or no body) reserves input
  only. `Gateway.admit(cap, est)` -> `Ledger.reserve(...)` checks and holds
  under the ledger lock: max_inflight first, then session, team and the
  cap's sub-cap, each `spent >= limit` -> "daily spend X reached for Y",
  else `spent + held + est > limit` -> "daily spend X for Y would be crossed
  ($spent spent + $held reserved in flight + $est for this call > $limit)".
  All refusals 403 (non-retryable), audited with `reserve_usd`; admitted
  lines carry `reserve_usd` too. `Gateway.cap_check` is gone.
- **Settlement:** `_forward` (split out of `_proxy`) calls
  `Ledger.settle(res, usd)` in the relay's `finally`, before the client sees
  the last chunk; `_proxy` calls `settle(res, 0)` in an outer `finally`
  (idempotent) so 502s, 499s and exceptions release the hold. Holds,
  `cap_held` and `inflight` are memory-only; `spend.json` stays charged
  totals. Holds survive the day roll.
- **Aborted streams:** if the relay raises (upstream cut the chunked stream:
  `IncompleteRead`; or the client went away) on a 2xx, usd =
  `max(metered, floor_usd)` where the floor is 4 chars/token on `bytes_up`
  (input price) and `bytes_down` (output price); the audit line gets
  `aborted: true` and reason "stream aborted before its usage; billed at the
  floor" when the floor wins. Non-2xx responses are never floored.
- **Policy:** `spend.max_inflight` (default 4), per session.
- **Tests** (`ReservationTest`, 6, with a `HoldUpstream` fake that sends
  message_start then holds the stream until the test releases it): a second
  opus call (reserve ~$0.60, cap $1) refused while the first is in flight
  with nothing spent yet, no upstream request; first settles at $0.00765,
  hold back to 0, then the same call fits again; max_inflight 2 refuses the
  third (another session unaffected); aborted stream billed at the floor
  (> metered), ledger matches the audit, hold released; `reserve_usd` table
  (max_tokens, no limit -> 4096, max_output_tokens, non-JSON, None);
  `settle` idempotent. `make test`: **68 tests OK** (leash 14 unchanged,
  tether 54). ruff clean.
- **Real run** (`overnight/run/rs/real.sh`, live `tether serve` on
  127.0.0.1:18131, haiku at [1, 5] per M, streaming): (A) cap `--spend
  0.00002`, max_tokens 5: **403 at admission** (reserve $0.00005), nothing
  sent upstream -- in run 10 the same kind of cap let message 1 through and
  only refused message 2. (B) cap `--spend 0.003`, two concurrent calls,
  max_tokens 400 (reserve $0.002035 each): call 2, started 0.4 s into call
  1, **403 "would be crossed ($0.0000 spent + $0.0020 reserved in flight +
  $0.0020 ...)"**; call 1 200, settled at **$0.002021** (21 in / 400 out);
  call 3 afterwards 403 ($0.0020 spent + $0.0020 > $0.0030). (C) session
  max_inflight 2: third concurrent call 403 "2 calls already in flight",
  the two 200s settled at $0.001521 each. Ledger total $0.005063 = the sum
  of the three allowed lines. **The estimate was within 0.7 % of the
  metered cost** here (4 bytes/token on a tiny body; out hit max_tokens).
- **Note for milestone 7:** Claude Code sends a large `max_tokens` (check
  the value in an audit `reserve_usd` line) and ~114 KB bodies, so each of
  its calls reserves far more than it costs (cache reads bill at 0.1x but
  are reserved at 1x). With the cage policy's $2 session cap that's fine
  for haiku; an opus-priced session reserves enough per call that few fit
  concurrently. That's the intended trade (bounded overshoot), but measure
  it in the four-stream test.
- The `$%.4f` in refusal messages shows a $0.00002 cap as $0.0000; cosmetic.

Run 15 (2026-10-19) notes:

- **Request id:** `Handler.parse_request` mints `req_` + 16 hex per
  request; `send_response` adds `x-tether-request-id` to **every** response
  the guest gets: route responses, refusals (`_error`), the CONNECT 200, and
  stdlib `send_error`s. `x-tether-request-id` is in `HOP`, so a copy from the
  upstream is dropped and the guest's copy never goes upstream. Every audit
  write in the handler goes through `self._audit`, which adds `request_id`.
- **New audit fields:** `request_id`; `policy_version` (sha256 of the policy
  file bytes, first 12 hex, `Policy.version`, added by `Audit`);
  `upstream_request_id` (first of `request-id`, `x-request-id`,
  `x-github-request-id`; also on refused upgrades and 101s); `rule`; and
  client IDs from a **fixed allowlist** of headers, kept only if they match
  `[A-Za-z0-9._:-]{1,128}`: `x-claude-code-session-id` -> `client_session_id`,
  `x-claude-code-agent-id`, `x-claude-code-parent-agent-id`,
  `x-claude-code-prompt-id`, `x-client-request-id`. Names checked with
  `strings` on the 2.1.303 binary (`~/.local/share/claude/versions/2.1.303`),
  which has `X-Claude-Code-Session-Id`, `x-claude-code-agent-id`,
  `x-claude-code-parent-agent-id`, `x-claude-code-prompt-id` and
  `x-client-request-id`. **Not yet seen on the wire from Claude Code**:
  confirm `client_session_id` on the first Claude Code audit line in
  milestone 6/7 (conformance).
- **Rules** (which check decided): `cap:identify`, `cap:missing`,
  `cap:route-grant`, `cap:egress-grant`, `route:none`, `route:<r>:path`,
  `route:metered-upgrade`, `upgrade:body`, `body`, `plain-http`,
  `egress:port`, `egress:credentialed-host`, `egress:ip-literal`,
  `egress:allowlist`, `addr`, `sni`, `spend:max-inflight`, `spend:session`,
  `spend:team`, `spend:sub-cap`; allows: `route:<r>`, `egress:allow[i]` (index
  into `egress.allow`). New `Gateway.egress_check` returns (rule, refusal);
  `egress_ok` wraps it. `Ledger.reserve` and `Gateway.admit` now return
  `(res, why, rule)`.
- **Chain:** each line ends with `prev_hash` = sha256 of the previous line's
  bytes (no newline); the first line's is sha256("") (`e3b0c4...`). Computed
  under the audit lock. `Audit.__init__` reads the file's last line from the
  end (`last_line`) to continue the chain after a restart. `tether -c P audit
  verify` walks it (`verify_chain`) and exits 1 at the first break: an edit
  to line k shows at line k+1 ("prev_hash ... does not match line k").
  **Limits, recorded:** an edit to the *last* line is undetectable without a
  checkpoint, and there must be one writer per audit file (the head is read
  once at start; a second `serve` on the same state dir forks the chain,
  which verify then reports). **Signed checkpoints over OTLP/HTTPS are team
  mode: out of scope tonight.**
- **Tests** (4 new in `TetherTest`; fake upstream now sends `request-id`,
  `x-github-request-id` and a spoofed `x-tether-request-id`): request id on
  a 200, a 403 and on CONNECT refused/200, each matching its audit line, one
  `x-tether-request-id` only, none upstream; `upstream_request_id` from both
  fakes; client ids kept, a malformed one dropped; rules
  `route:anthropic`, `route:anthropic:path`, `egress:allowlist`,
  `egress:allow[0]`, `spend:session`; chain verifies (lib and CLI), an edit to
  line 1 breaks at line 2 (CLI exit 1), a deletion breaks; restart continues
  the chain; `policy_version` changes with the file. `make test`: **72 tests
  OK** (leash 14 unchanged, tether 58). ruff clean.
- **Real run** (`overnight/run/au/real.sh`, live `tether serve` on
  127.0.0.1:18141, real upstreams): haiku call 200 with `x-tether-request-id
  req_743c...`, audit `upstream_request_id req_011CgBDACij7D1XNbZuwMjTB`
  (Anthropic's `request-id`) and `client_session_id` from the header; GitHub
  info/refs 200 with `upstream_request_id D67B:2DE5B6:...`
  (`x-github-request-id`); off-allowlist path 403 `route:github:path`;
  CONNECT pypi.org `egress:allow[0]`, example.com `egress:allowlist`; all 6
  lines carry `policy_version 312323e06bc2`; `audit verify`: 6 lines, intact.
  Editing line 2's `usd` by hand: `chain broken at line 3`, exit 1.
- Spend this run: 2 haiku calls (max_tokens 5), **$0.000066**.

## 5. Identity in the guest

- [x] Per-agent Unix users; shared `/work` (group, setgid, default ACLs)
- [x] nono session profile per agent; command policies for git and npm; capability delivery via tmpfs
- [x] Verify one tool can't read another's capability (`/proc/<pid>/environ`)

Run 16 (2026-10-19) notes:

- New: `spike/msb/guest/users.sh` (`setup`, `check`; pushed to
  `/opt/leash/` like the other guest scripts) and `cage.sh users` (runs both
  as guest root). Cage: tether as gateway, Landlock kernel, env block in
  "Next step".
- **Users:** group `work` (gid 2000); agents `claude` (2001) and `codex`
  (2002), each with its own group, in `work`, home mode 0700; the human `dev`
  (1500) is in `work` too, with no sudo (the image has none).
- **`/work`:** `root:work`, mode 2775 (setgid), access and default ACL
  `user::rwx,group::rwx,group:work:rwx,mask::rwx,other::r-x`.
- **Decision: no `setfacl`.** `node:22-bookworm` ships `libacl1` but not
  the `acl` package, and installing it would need deb.debian.org on the
  egress allowlist (and apt switched to https, since tether is not a
  plain-HTTP proxy). Instead `users.sh` writes the
  `system.posix_acl_access`/`_default` xattrs directly from python3
  (version 2 header plus (tag, perm, id) entries) and reads them back to
  print them. The kernel has `CONFIG_FS_POSIX_ACL`, `EXT4_FS_POSIX_ACL` and
  `TMPFS_POSIX_ACL`, and the overlay root honours them. So it is a real
  default ACL, not the setgid + `umask 002` fallback.
- **`/run/tether`:** the msb `--tmpfs` mount, set to `root:root 0700`.
- **`cage.sh users` check: 18/18 PASS**, with every agent write under
  `umask 022`:
  - Sharing: claude creates `/work/.../f`; codex appends to it, creates a
    file in claude's directory; claude edits and renames codex's file; dev
    edits claude's file; new files get group `work` and mode `rw-rw-r--`;
    new directories inherit setgid.
  - Separation: neither agent can read or list the other's home, codex can't
    write into claude's home, and neither can list, read or write
    `/run/tether`.
  - **Control:** in a setgid-only dir (`/work-noacl`, no ACL) under the same
    umask, codex **cannot** append to claude's file. So the ACL, not
    setgid, is what makes sharing work.
- **Finding (git across agent UIDs):** in `/work/repo` created by claude,
  codex's `git status` fails with `fatal: detected dubious ownership`
  (git 2.39.5). And because of the shared ACL, codex **can write claude's
  `.git/`** (`touch .git/x` succeeded). That is a cross-agent code-execution
  path: whatever one agent writes into `.git/config` (`core.fsmonitor`,
  `core.pager`, filter/diff drivers, aliases) or `.git/hooks` runs as the
  other agent the next time it runs git. `core.hooksPath=/dev/null` alone
  does not cover the config keys. **Decision:** do not set
  `safe.directory=*` (it would switch off git's own guard against exactly
  this). Handle it in item 2: nono grants `.git/` writes only to the git
  command policy of the agent that owns the repo; other agents and tools get
  `/work` with `/work/**/.git` denied, as the design already does for npm.
  Reading `.git` across agents remains possible (git still refuses on
  ownership).
- Boot to first exec: 0.40 s. No model calls this run.

Run 17 (2026-10-19) notes:

- New: `spike/msb/guest/nono.sh` (`setup`, `put`, `run`, `check`) and
  `cage.sh caps` / `cage.sh nono`. `cage.sh up` now mounts the guest nono
  build at `/opt/nono` (`--mount-dir ...tether-guest/bin:/opt/nono:ro`).
  tether: `cap mint --env -` (env file to stdout, so a cap goes host ->
  pipe -> guest tmpfs and never touches host disk), `--format
  env|gitconfig|npmrc`, `--no-route`/`--no-egress`, and `env_for` now
  names only the cap's own grants (no proxy vars without egress, no git
  rewrite without github, no `ANTHROPIC_*` without anthropic). 2 new unit
  tests; tether 60/60, leash 14/14, ruff clean.
- **Caps per boot:** for each agent, `agent` (env file; claude: route
  anthropic + egress pypi/pythonhosted; codex: no route, egress pypi),
  `git` (gitconfig; route github, no egress), `npm` (npmrc; no route, egress
  registry.npmjs.org), all `--ttl 12h`. Stored as
  `/run/tether/<agent>/<name>`, **`root:<agent>` 0440** in a `root:<agent>`
  0750 dir. Deviation from "owner = agent, 0400": an owner can chmod its own
  file (Landlock does not mediate chmod), root ownership with group read
  cannot. `/run/tether` is now **0711** (was 0700): nono's supervisor runs as
  the agent and must traverse to its own directory; listing is still denied
  (`users.sh check` 18/18 PASS again).
- **nono 0.80 findings (see the design's new Corrections section):**
  - `command_policies` exist, with named credentials of type `local-socket`,
    `raw-file`, `proxy`, `ambient`. No `GrantSet`/templated env. A
    `raw-file` credential is a **file grant to one command sandbox**; it
    does not set `env_var` (tried, including with `allow_vars`).
  - **Exec gate:** a command sandbox may exec only its pinned executable
    (and mediated commands). The design's "three-line launcher" therefore
    fails (`exec: /usr/bin/git: Permission denied`); so do git's
    `remote-https` and every `!alias`, pager, fsmonitor and filter.
  - So each cap is a file in the tool's own format. git: pinned
    `/usr/bin/git`, `argv_prepend -c core.hooksPath=/dev/null -c
    core.fsmonitor=false -c include.path=/run/tether/<a>/git`; the file is
    `[url "<gw>/github/"] insteadOf = https://github.com/` plus
    `[http "<gw>/github/"] extraHeader = Authorization: Basic
    base64(agent:<cap>)`. `git-remote-http` is a chained command
    (`from.session: deny`, `from.git` with the same raw-file and
    `allow_raw_file_credentials_in_chained_policy`), `GIT_EXEC_PATH` is an
    empty dir so the helper resolves via the shim, and git's
    `GIT_DIR`/`GIT_SHALLOW_FILE`/`GIT_CONFIG_PARAMETERS`/... pass `allow_vars`
    (clone runs `index-pack` as a second git, which failed with
    `--stdin requires a git repository` without them). npm: pinned
    `/usr/local/bin/node` with `argv_prepend npm-cli.js --userconfig
    /run/tether/<a>/npm` (`https-proxy=`/`proxy=` with the cap).
  - nono refuses a grant of the whole home (overlaps
    `~/.local/state/nono`) and of `~/.config` (overlaps its built-in
    credential denies); session grants `/work`, `/tmp`, `~/.claude`,
    `~/.cache`, `~/.npm`, `~/.claude.json`, read `/usr /etc /opt/nono
    /opt/tether`, `network.connect_port: [18001]`.
- **`cage.sh nono` check: 19/19 PASS** (claude; plus a codex sanity run):
  - Session: Landlock V6 on, writes `/work`, cannot write the rest of its
    home, **cannot read any file in its own `/run/tether/claude/`** (Unix
    perms would allow; Landlock refuses), holds its own cap in env and no
    git rewrite, **EACCES connecting to a real listener on guest
    127.0.0.1:8080**, reaches the gateway, and its own cap is refused a
    CONNECT to registry.npmjs.org (`cap:egress-grant`).
  - git: clone of `metcalfc/tether-scratch` works; `metcalfc/imp` gets 403
    (route allowlist); hooksPath is `/dev/null`; **planted `core.pager`,
    `alias.x=!...`, `core.fsmonitor` and a `filter.*.clean` driver in
    `.git/config` all fail to run** (exec gate) -- this closes run 16's
    cross-agent `.git` code-execution path for mediated git; **direct
    `/usr/bin/git` from the session: Permission denied**.
  - npm: `npm view left-pad` = 1.3.0 through the npm cap; **interpreter
    bypass** (`node .../npm-cli.js view`) gets `403` -- tether's audit shows
    it arrived with the agent's cap and was refused by its egress grant
    (seam 4 fails closed). Plain `node` still runs in the session (Claude
    Code needs it); only `npm` by name is mediated.
  - codex cannot read claude's git cap; codex's own session clones with
    its own git cap.
  - **tether's audit attributes each request:** `claude git`, `claude npm`,
    `codex git`, and `claude -` for the agent-cap refusals.
- **Known gap (recorded, not fixed):** the agent can print its git cap
  through git itself (`git config --get-regexp extraheader`). Per-tool caps
  keep a tool's cap from *other tools* (by policy an npm script can neither read it nor run
  git, as there is no `from.npm` edge -- untested until milestone 7) and give attribution; they do not hide it from
  the agent that drives the tool. The cap only buys what git's grant allows.
- **Not done here:** npm lifecycle scripts (`sh -c` from node) will hit the
  exec gate; `npm install` of a package with a postinstall will fail rather
  than run it. That is milestone 7's fixture; add a `sh` edge
  (`commands.sh.from.npm`, no cap, no network) only if a legit install needs
  it.
- **Launch cost:** session start (`nono run -- true`) **165 ms**; mediated
  `git --version` **24 ms/call** vs <1 ms plain (10 runs each).
- Boot to first exec 0.42 s. No model calls this run.

Run 18 (2026-10-19) notes:

- New: `spike/msb/guest/procprobe.py` (for each pid, tries cmdline,
  environ, maps, list + non-blocking reopen of every `/proc/PID/fd/N`, a
  scan of `/proc/PID/mem`, `process_vm_readv` and `PTRACE_ATTACH`; prints
  `ok`, `ok+CAP` or the errno, never the cap; matches `tth_` and git's
  base64 `agent:tth`), `nono.sh proc`, `cage.sh proc`. `cage.sh push` now
  copies `guest/*.py` too.
- Setup: claude's session holds open a mediated `git remote-http` (waits on
  stdin for helper commands, config and cap already loaded) and `npm init`
  (waits for a package name). Process tree: the session's `git`/`npm` are
  nono shims; the real `git -c include.path=/run/tether/claude/git`, the
  real `git-remote-http` and `npm init` are **children of the nono
  supervisor**, each in its own command-sandbox Landlock domain, a sibling
  of the session's. Root's ground truth: the session's bash/sleep/shims
  have the agent cap in `environ`; real git, git-remote-http and npm have
  **no cap in environ but have it in memory** (git's base64 header, npm's
  proxy URL). nono's supervisor is non-dumpable (even claude's own
  unsandboxed UID gets EACCES on it).
- **`cage.sh proc`: 11/11 PASS.**
  - claude's session (Landlock) against all 10 claude-owned processes,
    including its own tools and a sibling session's bash: environ, maps,
    mem → EACCES; ptrace → EPERM; process_vm_readv has no address to try
    (maps refused). cmdline readable, no cap in it.
  - codex's session against claude's processes: everything but cmdline
    refused.
  - **Control 1:** claude's UID with no sandbox reads environ, mem, maps,
    and ptraces every one of those processes (except nono's supervisor), so
    the refusals above come from Landlock domain scoping, not Unix perms.
    codex's unsandboxed UID is refused by Unix perms (different UID).
  - **Control 2:** leash-style delivery (`GIT_CONFIG_KEY_0=url.http://agent:<cap>@...`,
    fake cap, hung listener on guest 127.0.0.1:8081): `/proc/<git-remote-http>/cmdline`
    shows the cap, **readable even from codex's Landlock session** (cmdline
    has no ptrace check). Confirms the gitconfig `extraHeader` choice.
  - **fd finding:** `/proc/<pid>/fd/` of a same-UID process can be
    **listed** from inside the session (generic permission, no ptrace
    check), so fd targets are visible (readlink). Reopening is refused: the
    claude session reopened **0** fds of any of its tools or of an
    unsandboxed claude process holding `/run/tether/claude/git` on fd 3,
    not even pipes or /dev/null. Root reopens that fd and finds the cap.
    codex cannot list it at all.
- No design correction needed: the two "(verify)" claims (Landlock blocks
  reading other domains' `/proc/*/environ`; reading git's cap from
  `/proc/<git>/environ`) hold, and also cover mem, ptrace and fd reopen.
  Caveat for the write-up: the cap's safety from the agent's *own* other
  processes relies on every agent process being inside nono; a claude-UID
  process outside nono can read everything above.
- Tests: tether 60 + leash 14 = 74 OK (no tether change). shellcheck,
  `/bin/bash -n` and ruff clean. Boot to first exec 0.38 s. No model calls.

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
| 10 | 2 (haiku, via tether, max_tokens 5) | $0.000066 |
| 11 | 1 (haiku, Claude Code via tether, works.sh) | $0.0374 |
| 14 | 3 allowed + 4 refused at admission (haiku, via tether, streaming) | $0.005063 |
| 15 | 2 (haiku, via tether, max_tokens 5) | $0.000066 |

## Decisions and corrections

- Run 6: the design's libkrunfw is microsandbox's fork
  (`superradcompany/libkrunfw` @ `21f169f`, 5.6.1, kernel 6.12.111), not
  `containers/libkrunfw`; build the kernel on the container's own filesystem
  because the macOS volume is case-insensitive. The Day-2 gate passed with
  Landlock ABI V6, so nono stays in the plan.
- Run 16: default ACLs on `/work` are written as raw `system.posix_acl_*`
  xattrs (no `setfacl` in the image, no deb.debian.org egress). Shared
  `/work` lets one agent write another's `.git/`, so `.git` writes must be
  limited to the owning agent's git (item 2); `safe.directory=*` is not used.
- Run 17: Landlock cannot carve `.git` out of `/work` (nono refuses the
  overlapping deny); instead nono's exec gate stops planted git config from
  running anything. nono 0.80 `raw-file` credentials are file grants, not
  env; launchers cannot exec the tool; caps go to tools as their own config
  files (gitconfig with an `http.extraHeader`, npmrc). Cap files are
  `root:<agent>` 0440, `/run/tether` 0711. Design doc now has a
  Corrections section.
