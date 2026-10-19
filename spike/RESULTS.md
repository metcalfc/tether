# Spike results: a Mac VM whose only way out is leash

Fill this in as you go. Numbers beat adjectives.

## Pre-flight (Linux cloud workspace, Oct 19, 2026)

Run before handoff, on x86_64 under QEMU 8 emulation (no KVM, so timings
are meaningless), with a fake Anthropic key.

- `provision` 467 s, `up` to ssh 61 s (emulated).
- `attack.sh`: all 11 block attempts held, including root flushing
  nftables/iptables, resetting the default route, DNS through QEMU's own
  resolver at 10.0.2.3, and the host's real ports at 10.0.2.2.
- The pypi.org allow test failed for an environmental reason: that
  workspace intercepts outbound TLS with its own CA, which the guest doesn't
  trust. With `curl -k` it returns 200 through leash. This shouldn't happen on
  the Mac.
- The anthropic route reached the real API: 401 for the fake key.
- Revoke worked: gateway stopped gave nothing to talk to; restarted gave the
  same capability working again with no change to the VM.
- **Bug found and fixed:** `CONNECT` to an allowlisted host worked with no
  capability, so any process in the VM got out unattributed. Now 407, with a
  unit test and an `attack.sh` line.
- Not run: Claude Code in the guest, `cap.sh`, `works.sh`, the recording.

## Setup

- Machine: (model, chip, RAM, macOS version)
- QEMU version: (`qemu-system-aarch64 --version`)
- Guest: Ubuntu 24.04 minimal cloud image, arm64
- leash commit: (`git rev-parse --short HEAD`)

## Timings

| Step | Time |
| --- | --- |
| `vm.sh provision` (first boot, tools installed) | |
| `vm.sh up` to ssh ready | |
| `claude -p` through leash (median of 5) | |
| Same call from the host, direct (median of 5) | |
| `pip download requests` through leash | |

## Acceptance

| Check | Expected | Result |
| --- | --- | --- |
| `make test` | 13 pass | |
| `works.sh`: Claude Code returns LEASH_OK | works | |
| `works.sh`: pip from pypi.org | works | |
| `works.sh`: git clone via the github route (if a token is set) | works | |
| `attack.sh`: every BLOCK line | blocked | |
| `attack.sh`: DNS via 10.0.2.3 | blocked (if not: note it, it's a DNS channel) | |
| No `sk-ant-` anywhere in the guest | none | |
| `cap.sh` trips the cap | REFUSED within a few calls | |
| `leash report` attributes everything to `demo` | yes | |
| Ctrl-C leash, then `claude` in the VM | fails | |
| Restart leash, `claude` again | works, same capability | |

## Stretch: Lima

- Lima with `vmType: qemu` and a `QEMU_SYSTEM_AARCH64` wrapper that adds
  `restrict=on` and the `guestfwd`: (works / breaks what)
- Lima with `vmType: vz`: is there a way to cut its user-mode network's
  internet access while keeping ssh? (findings, one hour max)

## Findings

- What surprised you:
- What a real implementation in Ringleader would need:
- Estimate for Ringleader's QEMU driver, Lima driver, WSL2:

## PoC: microsandbox + nono + tether (Mac, Oct 18-19, 2026)

The design is `design/tether-poc.md`. Run-by-run notes, with logs and the
exact commands, are in `overnight/PROGRESS.md`; "run N" below refers to
them. Every number here was measured on this machine; anything not
measured says "not run".

### Setup

- Machine: Mac16,1, Apple M4 (10 cores), 32 GB, macOS 26.6.2.
- Cage: **msb 0.7.8** (release binary), image `node:22-bookworm`, 4 vCPU,
  4 GiB, `--net-default deny --net-rule allow@host:tcp:18001`.
- Kernel: microsandbox's libkrunfw fork (`superradcompany/libkrunfw` @
  `21f169f`, 5.6.1, Linux 6.12.111), rebuilt with Landlock
  (`spike/msb/kernel/`) and loaded with `MSB_LIBKRUNFW_PATH`.
- **nono 0.80.0** in the guest (linux aarch64 glibc), Landlock ABI **V6**.
- Gateway: `./tether` (stdlib Python, grown from `./leash`), on the host.
  Claude Code **2.1.303** in the guest, `claude-haiku-4-5` for every
  functional test.
- One policy file, `spike/msb/poc-policy.json`, compiled by `tether compile`
  into the msb flags, the nono profiles and the tether config.

### Success criteria

| Criterion (design) | Result | Evidence |
| --- | --- | --- |
| Every cage attack is blocked when run as root | **pass** | `attack.sh` as guest root: 24/24 (21 BLOCK, 3 ALLOW) on the stock kernel (run 3), on the Landlock kernel behind tether (run 11), and after root killed nono (run 24). Negative control with msb's open network flips 11 lines to FAIL, so the probes do see egress when it exists |
| No real credential in the guest's disk or in nono's memory | **pass** | Disk and every `/proc/*/environ`, real-key shape (run 24). Run 27: `memscan.py` as root read every userspace process's memory (94 MiB, including nono's supervisor, a live claude session and msb's agentd) and found no real key; a fake key-shaped string in a control process was found. The keys never enter the guest: tether reads them from the host keychain |
| Every credentialed call is attributed to a (session, agent, tool) | **pass, with a caveat** | The audit names `claude git`, `claude npm`, `codex git`, `claude -` (the agent's own cap) for every agent process (runs 17, 20, 22-25), including the refusals. The caveat: attribution follows the cap, so root (or anything holding a cap file) is attributed as that cap's agent and tool (run 24). The bootstrap's session cap is attributed `(session, -, -)` |
| A cap replayed from another sandbox fails | **pass** | Run 25: claude's agent cap and git cap, copied into `tether-cage-2`, get 403 (`cap:identify`) on models, CONNECT and git; the same caps get 200 on their own channel; neither sandbox can reach the other's listener (000) |
| With 4 concurrent streams, overshoot stays within one reservation | **pass** | Run 26: reservations on, overshoot **$0** (2 of 4 admitted, next call 403). Off: all 4 admitted, overshoot **+$0.003084, 62 % of a $0.005 cap** |
| tether adds ≤ 5 ms to TTFB at p50 | **pass, after a fix** | In-process cost, network removed (`overhead.py`, fake upstream, n=200): **+0.17 ms p50, +0.26 ms p95**. Against the real API, n=40: before the fix **+6.0 ms** (n=20: +7.9 ms), because tether loaded the CA store per request (2.4 ms); after it, -9.5 ms and -25.7 ms in two runs, i.e. within the API's own jitter |
| No stream aborts during a 30-minute session | **pass** | Run 27, `soak.py` in the guest through msb and tether: one streaming haiku call every 6 s for 30.0 min, **300 calls, 0 aborted** (each read to `message_stop`), TTFB p50 495 / p95 640 / max 749 ms. Short streams (max_tokens 120, up to 2.0 s each), not one 30-minute stream |
| One file produces the configuration for all three layers | **pass** | Run 19: `tether compile spike/msb/poc-policy.json` writes `msb.args`, `nono/<agent>.json` and `tether.json`; the cage boots from them (runs 19-27). Compiled nono profiles equal the hand-built ones |

Kill criteria: none triggered. Landlock was in the guest on day 2; no
escape from the cage was found; Claude Code worked with plain forwarding
(no rewriting); nothing came near +50 ms or stalled.

### Measurements

| Measurement | Result |
| --- | --- |
| Boot, `msb create` to first `exec` (image cached) | 0.26-0.42 s over runs 2-27; 0.37 s in run 27 |
| Boot to agent-ready, tether cage (run 27) | **20.7 s**: `up` 0.89 s (tether init, create, push), users 0.92 s, nono setup plus its 19 checks 6.0 s, `npm install -g @anthropic-ai/claude-code` + `claude --version` 12.9 s. A pre-baked image would remove the 12.9 s install (not built) |
| TTFB through the gateway vs direct, host client, real API | leash (run 5, n=20): +3.3 ms p50, p95 within noise. tether before the TLS fix (n=40): +6.0 ms p50, -3.0 ms p95. After (n=40, two runs): -9.5 / -25.7 ms p50 (noise) |
| tether's own added latency (fake upstream, n=200) | +0.17 ms p50, +0.26 ms p95 |
| TTFB from the guest (msb netstack, then the gateway) | p50 516.8 / p95 561.5 ms, about +0.5 ms over the host client (run 5, leash) |
| SSE inter-chunk gap, direct vs gateway | 15.7 vs 15.9 ms p50 (run 5); 8.1 vs 8.0 ms p50 (run 27). No buffering |
| CONNECT setup (TLS ready, registry.npmjs.org) | Host direct 31.2 ms p50 / 45.7 p95; host through tether 32.2 / 36.5 (run 27, n=10). **From the guest through the proxy: ~86 ms p50** (run 5), about +50 ms per new tunnel through msb's userspace netstack |
| `npm install` on guest disk vs virtio-fs | typescript + eslint + webpack (5,156 files), warm cache, `--prefer-offline`, 3 runs: **guest disk 1.24 s, virtio-fs 2.69 s** (2.2x) (run 27) |
| Large `git push` through the `/github/` route | **100 MiB**, not 200 MB: 104,890,010 bytes chunked in 34.2 s (~3 MB/s, the uplink), gateway RSS 38 MB (run 12). 200 MB not run |
| Overshoot, 4 concurrent streams | Reservations on: $0. Off: +62 % of the cap (run 26) |
| Cost of reservations | A Claude-Code-shaped call (max_tokens 4096, short answer) reserves ~230x its cost: 2 of 4 refused with the cap 99.5 % unspent (run 26) |
| Memory | msb process: 2.27 GB phys footprint, 2.15 GB RSS (it counts guest RAM and page cache); guest itself 113 MB used, 857 MB cache of 3.9 GB; tether 29 MB RSS (run 27). Run 5: msb 1.1 GB RSS, leash 36 MB |
| nono launch cost | Session start (`nono run -- true`) 165 ms; a mediated `git --version` 24 ms/call vs < 1 ms plain (run 17) |
| Kernel build | ~11 min wall, one attempt (run 6) |

### Fallbacks taken

None. The day-2 gate passed (Landlock V6), so nono and the per-tool
claims stay; no escape meant no switch to QEMU; Claude Code needed no
rewriting; latency stayed far under the 50 ms kill line.

### Corrections and findings

The design's "Corrections" section has the full list. The ones that change
a claim:

- libkrunfw is microsandbox's fork, not `containers/libkrunfw`.
- Landlock has no deny, so `/work/.git` cannot be carved out of `/work`.
  nono's exec gate stops planted git config from running anything, and
  mediated git ignores hooks (`core.hooksPath=/dev/null`). Writing a hook
  is not refused.
- nono 0.80 has no templated env promotion, and a launcher cannot exec the
  tool. Caps go to tools as their own config files (a gitconfig
  `extraHeader`, an npmrc). The cap rides in a header, because
  `/proc/<pid>/cmdline` is readable by everyone.
- Per-tool caps do not hide a tool's cap from the agent driving it
  (`git config --get-regexp extraheader`). They keep it from other tools and
  give attribution.
- npm lifecycle scripts fail closed under the exec gate. The opt-in
  `scripts: true` edge gives `sh` npm's files but no cap, no network and no
  `npm_config_*` (npm exports its cap-bearing proxy URL there).
- Killing nono's supervisor leaves the session's Landlock domain in place.
  Its mediated tools fail closed.
- `/api/hello` is answered by tether itself. `count_tokens` and models are
  not metered.
- Run 27: tether's +6-8 ms at p50 came from building a TLS context per
  upstream request. Fixed (one shared verifying context, unit-tested).
  Leash has the same cost.
- Run 27: msb's agentd (guest pid 1) holds copies of the caps in memory
  (16 matches), because caps are delivered through `msb exec` stdin. Root
  can read caps anyway (they are root-owned files). Only root can read
  agentd's memory, and the caps are not real keys.
- "Gain root with `sudo`" and "flush nftables" are moot here: `msb exec` is
  root, the image has neither sudo nor nft, and the network policy lives in
  the host msb process, not in the guest.
- Haiku refused the injected README on its own (run 22). The enforcement
  results come from running the same steps as plain commands.
