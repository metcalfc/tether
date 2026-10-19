# Progress

Read this first on every run. Tick items as they are verified, not when the
code is written. Add notes under each milestone: what was done, the
measurements, the decisions and why, and the exact next step.

## Next step

Start at milestone 0.

## 0. Setup

- [ ] Read `design/poc-microsandbox-nono-leash.md` in full; note any questions here
- [ ] Create branch `poc/msb-nono-leash` from `spike/mac-lockdown`
- [ ] `make test` passes (14 tests); `overnight/preflight.sh` passes
- [ ] Install microsandbox (`msb`) per-user; record the version and commit
- [ ] Install nono per-user; record the version

## 1. Cage (design day 1)

- [ ] An msb sandbox with `--net-default deny --net-rule "allow@host:tcp:P"`, with today's leash on the host
- [ ] Port `spike/guest/attack.sh` and `works.sh` to the msb guest; every BLOCK line holds
- [ ] Claude Code end to end in the guest through leash (cheap model)
- [ ] Measure boot to agent-ready, and time to first byte through leash versus direct

## 2. Kernel (design day 2, gate)

- [ ] Build libkrunfw with `CONFIG_SECURITY=y`, `CONFIG_SECURITY_LANDLOCK=y` and landlock in `CONFIG_LSM`; load with `MSB_LIBKRUNFW_PATH`
- [ ] `nono` in the guest reports Landlock ABI V4 or later
- [ ] GATE: if this fails, record it and switch to the nftables fallback (see PROMPT.md)

## 3–4. leash++ core

- [ ] Channel binding: one listener per sandbox
- [ ] Capability registry: `leash cap mint|revoke|list`, minted per boot
- [ ] Per-cap policy intersected with the ceiling
- [ ] CONNECT hardening: resolve on the host; refuse private addresses; connect to the checked IP; match SNI; refuse IP literals
- [ ] Streamed request bodies (large `git push`)
- [ ] `Upgrade` passthrough on routes
- [ ] Spend reservations and a max-inflight limit; non-retryable refusal
- [ ] Hash-chained audit with the new fields and `x-leash-request-id`
- [ ] Unit tests for each item; all tests pass

## 5. Identity in the guest

- [ ] Per-agent Unix users; shared `/work` (group, setgid, default ACLs)
- [ ] nono session profile per agent; command policies for git and npm; capability delivery via tmpfs
- [ ] Verify one tool can't read another's capability (`/proc/<pid>/environ`)

## 6. Compiler and conformance

- [ ] `leash compile`: one policy file emits msb flags, nono profiles and leash++ config, with subset checks and wildcard lints
- [ ] `leash report --by tool`
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
- [ ] `git grep -n sk-ant` is clean; branch pushed; draft PR opened
- [ ] `DONE` written to `overnight/STATUS`

## Spend log

| Run | Model calls | USD (from leash report) |
| --- | --- | --- |

## Decisions and corrections
