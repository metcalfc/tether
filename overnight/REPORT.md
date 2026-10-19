# Overnight report: microsandbox + nono + tether PoC

28 runs, Oct 18-19 2026, milestones 1-8 of `design/tether-poc.md`. The
details and every number are in `spike/RESULTS.md` (section "PoC"); the
run-by-run log, with commands, is `overnight/PROGRESS.md`. Model spend for
the night: **about $0.73** (spend log in PROGRESS.md), nearly all haiku.

## What works

- **Cage:** msb 0.7.8, `--net-default deny` plus one host port. attack.sh
  as guest root blocks every probe (21 BLOCK, 3 ALLOW, 0 FAIL), including
  after root kills nono. No escape found.
- **Kernel:** microsandbox's libkrunfw fork rebuilt with Landlock (ABI V6),
  loaded with `MSB_LIBKRUNFW_PATH`. The day-2 gate passed, so nono stays.
- **tether** (stdlib Python, grown from leash): a listener per sandbox,
  per-boot caps (`tether cap mint|revoke|list`), per-cap policy intersected
  with the ceiling, hardened CONNECT, streamed bodies, `Upgrade`, spend
  reservations, a hash-chained audit, `tether compile`, `tether report --by
  tool`. 95 unit tests pass (tether 81, leash 14).
- **Identity:** per-agent Unix users, nono profiles, caps delivered as
  per-tool config files. Every credentialed call is attributed to (session,
  agent, tool).
- **Demo:** all ten steps in one unattended VHS take
  (`overnight/run/poc-demo.mp4`, 183 s). Claude Code on the default model
  fixed a failing test and pushed `agent/demo-20261019030805` to the
  scratch repo, for $0.21.

## Acceptance

| Criterion | Result |
| --- | --- |
| Every cage attack blocked as root | pass |
| No real credential on guest disk or in memory | pass (memscan, with a control) |
| Every credentialed call attributed | pass, caveat: attribution follows the cap |
| Replayed cap from another sandbox fails | pass (403 off-channel, 200 on) |
| 4 streams: overshoot within one reservation | pass ($0 on; +62 % of cap off) |
| ≤ 5 ms added TTFB at p50 | pass after a fix (+0.17 ms in-process; was +6.0 ms) |
| No stream aborts in 30 min | pass (300 calls, 0 aborted) |
| One file configures all three layers | pass |

Numbers: boot to first exec 0.37 s; boot to agent-ready 20.7 s (12.9 s
of it is `npm install` of Claude Code); CONNECT setup 32.2 vs 31.2 ms
direct on the host, ~86 ms from the guest; msb 2.27 GB footprint, tether
29 MB RSS; npm install guest disk 1.24 s vs virtio-fs 2.69 s.

## Faked, skipped, or weaker than it sounds

- **Fallbacks taken: none.**
- The 200 MB push was not run; 100 MiB was (run 12, before nono). No
  pre-baked image was built.
- The soak is 300 short streams, not one 30-minute stream.
- Haiku refused the injected README itself, so the enforcement evidence
  comes from running the same steps as plain commands.
- The demo pushes to `SCRATCH_REPO`, not `acme/app`, and sets
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS=16000` (see 1 below).
- Stretch items (cloud VM, gateway bake-off), Windows and team mode were
  out of scope and not touched.

## Look at these first

1. **Reservations versus the default model.** Claude Code sends
   `max_tokens` 128000, so tether reserves about $3.20 a call, more than the
   $2 session cap, and refuses the first call. Reservations are also ~230x
   the real cost of a typical call. Reservations need to be sized from
   something other than `max_tokens` (run 26, run 28).
2. **The `git-remote-http -> git` edge.** Pushes under nono were broken
   until run 28. Runs 17-27 only cloned and fetched, so they missed it. The
   edge gives `send-pack` the git cap file. Check it's no wider than it
   needs to be (`tether compile`, `test_git_push_edge`).
3. **What per-tool caps really buy.** They give attribution and keep a cap
   away from other tools, but the agent driving git can read git's cap
   (`git config --get-regexp extraheader`), and msb's agentd keeps copies of
   every cap in memory, because caps are delivered through `msb exec` stdin
   (runs 17, 27).

## How to run it

On an Apple Silicon Mac with msb 0.7.8 in `~/.microsandbox`, nono's guest
build in `~/.microsandbox/tether-guest/bin`, the Landlock libkrunfw built by
`spike/msb/kernel/build.sh`, and the keychain items `tether-anthropic` and
`tether-gh-scratch`:

```sh
make test                                 # unit tests, no keys
vhs spike/record/poc-demo.tape            # records the whole demo, or by hand:
spike/msb/demo.sh setup                   # fresh state, compile, gateway up
spike/msb/demo.sh 1                       # ... through 10, one step each
spike/msb/demo.sh teardown                # cage down, gateway down
```

Each step's full output goes to `overnight/run/demo/step-N.log`. For the
individual cage steps (`compile`, `gw-up`, `up`, `users`, `nono`, `adv`,
`replay`, `down`), see the top of `overnight/PROGRESS.md`.
