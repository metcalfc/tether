# Progress

Read this first on every run. Tick items as they are verified, not when the
code is written. Add notes under each milestone: what was done, the
measurements, the decisions and why, and the exact next step.

## Next step

Milestone 3, item 8: **spend reservations and a max-inflight limit;
non-retryable refusal** (design "Changes from today's leash" item 7). Today
`Ledger` checks before a metered call and charges after, so overshoot equals
the calls in flight. At admission on a metered route (the body is already
buffered there, see run 12), reserve `len(body)/4 * input price +
max_tokens * output price` (price from `price_for` on the body's `model`)
against the session, the team total and the cap's sub-cap; refuse with 403
(non-retryable) if `spent + reserved + this > cap`. At stream end, replace
the reservation with the metered cost; if the stream aborted with no usage,
bill a floor of ~4 chars per token on the bytes seen. Add a per-session
`max_inflight` (policy `spend.max_inflight`, default e.g. 4) refused 403 the
same way. Reservations live in memory (lost on restart is fine: no calls
are in flight then); keep `spend.json` the charged totals. Unit tests:
reservation refuses a call that would cross the cap while an earlier call is
in flight (a fake upstream that holds the stream open), reconcile releases
it, max_inflight refusal, the aborted-stream floor, and a `max_tokens`-less
body (pick a default, record it). Then rerun the run-10 real check (haiku,
max_tokens 5, sub-cap) to see the reservation bite on real traffic. Cage
setup as in run 12.

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
- [ ] Spend reservations and a max-inflight limit; non-retryable refusal
- [ ] Hash-chained audit with the new fields and `x-tether-request-id`
- [ ] Unit tests for each item; all tests pass

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

## Spend log

| Run | Model calls | USD (from tether report) |
| --- | --- | --- |
| 4 | 2 (haiku, via leash) | $0.0407 |
| 5 | 44 via leash + 22 direct (haiku, bench) | $0.0057 via leash; direct est. < $0.005 |
| 10 | 2 (haiku, via tether, max_tokens 5) | $0.000066 |
| 11 | 1 (haiku, Claude Code via tether, works.sh) | $0.0374 |

## Decisions and corrections

- Run 6: the design's libkrunfw is microsandbox's fork
  (`superradcompany/libkrunfw` @ `21f169f`, 5.6.1, kernel 6.12.111), not
  `containers/libkrunfw`; build the kernel on the container's own filesystem
  because the macOS volume is case-insensitive. The Day-2 gate passed with
  Landlock ABI V6, so nono stays in the plan.
