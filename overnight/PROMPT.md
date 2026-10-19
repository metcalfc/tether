# Overnight: build the microsandbox + nono + tether PoC

You are running unattended on Chad's Apple Silicon Mac, inside a loop. Each
run starts with a fresh context, so `overnight/PROGRESS.md` is your memory.
Read it first, every time. Chad is asleep: there is nobody to answer
questions, and nothing will be approved for you.

## The job

Implement the PoC in `design/tether-poc.md`. That file is the
spec: the architecture, where each function lives, the tether changes, the
policy model, the milestones, the gates and the demo. Read it in full on your
first run, and again whenever PROGRESS.md says you're starting a new
milestone. Scope is the **core plan, milestones 1–8**. The stretch items
(cloud VM, gateway bake-off), Windows, team mode and everything listed under
"Out" in the design are out of scope.

Background you can read but must not change: `README.md` (leash today),
`spike/` (the QEMU `restrict=on` spike and its attack/works/cap scripts),
`spike/RESULTS.md` (pre-flight results), and `design/managed-agents-feasibility.md`
(unrelated; ignore it).

## Naming

The gateway you build is **tether**, not leash. Chad already has leash, and
`./leash` with `tests/test_leash.py` stays as it is: the baseline that
milestone 1 runs against. On the first milestone 3 step, copy `./leash` to
`./tether` and `tests/test_leash.py` to `tests/test_tether.py`, rename inside
them, and grow tether from there. Use the tether name for everything new: the
CLI (`tether compile`, `tether cap`, `tether report`), its state directory,
`/run/tether` in the guest, the `x-tether-request-id` header, Makefile
targets, and the new section of `spike/RESULTS.md`. Write "leash" only when
you mean today's script or the spike.

## How each run works

1. Read `overnight/PROGRESS.md` and `overnight/STATUS`.
2. Pick the first unchecked item. Do one milestone sub-step per run, not the
   whole night in one go: small runs survive crashes and context limits.
3. Do the work. Verify it by running it: tests, the attack suite, a real
   command. Never mark an item done on the strength of code you haven't run.
4. Commit: one conventional commit per finished sub-step, on the branch
   `poc/tether`. Create the branch from `spike/mac-lockdown` on your
   first run if it doesn't exist.
5. Update PROGRESS.md before you stop: tick the item, then add what you did,
   what you measured, any decision you made and why, and the exact next step.
   Write it so a stranger could continue from it.
6. Stop. The loop starts the next run.

When every item is ticked, the PR is open and `overnight/REPORT.md` is
written, put `DONE` on the first line of `overnight/STATUS`.

If you hit something only Chad can fix, put `BLOCKED: <one line>` on the
first line of `overnight/STATUS`, explain it at the top of PROGRESS.md, and
stop. Examples: a missing keychain item, an expired token, a step that needs
`sudo`, or a macOS permission dialog. Blocked is an honest result, and
looping on it all night is not.

## Gates and fallbacks

At a gate, take the design's fallback, record it in PROGRESS.md and in
`spike/RESULTS.md`, and keep going. Don't stop at a gate.

- **Day 2: no guest kernel with Landlock.** Drop nono. Use per-agent Unix
  users plus in-guest nftables (`meta skuid` → one tether port per agent).
  Drop the per-tool claims from the demo and the report.
  - Give the kernel build a real try before you fall back: build libkrunfw
    with Landlock, with the Linux kernel compiled in a Linux container or Lima
    VM on this Mac (follow libkrunfw's own macOS instructions), and load it
    with `MSB_LIBKRUNFW_PATH`.
  - Stop trying after about two runs of real effort.
- **A real escape from the microsandbox cage.** Switch the cage to the QEMU
  `restrict=on` setup from `spike/` and continue. Write up the escape
  precisely; it's the most important finding of the night.
- **Claude Code needs its requests rewritten, not just forwarded.** Note it
  and continue with forwarding where it works. Moving the model hop is
  stretch work.
- **More than 50 ms added at p50, or stalled streams.** Record the
  measurements and continue. Don't rewrite tether in another language
  tonight.

## Credentials and spend

- **Anthropic key:** `security find-generic-password -s tether-anthropic -w`.
  Use it only as tether's credential source (`cmd:` in the policy), so it
  enters no other process. Never print it, never put it in a VM, a log, a
  file in the repo or a commit. Before every push, `git grep -nE 'sk-ant-(api|oat)[0-9]{2}-[A-Za-z0-9_-]{40,}'`
  must print nothing, and `git grep -n gho_` likewise (the GitHub token).
  Short fake keys in tests and the spike's grep patterns are expected.
- **Claude Code's own login:** this session runs on Chad's subscription. Never
  export `ANTHROPIC_API_KEY` in your own shell, or this Claude Code session
  will start billing the test key. The loop unsets it; keep it that way.
- **GitHub token for push tests:** `security find-generic-password -s tether-gh-scratch -w`.
  It is a copy of Chad's own `gh` token, so it can reach every repo he can,
  including `admin:org`. Nothing but you limits it: push only to the scratch
  repo named in `overnight/config.env` (`SCRATCH_REPO`), only to `agent/*`
  branches, and give tether a route policy that allows only that repo. Never
  print it or put it in the guest.
- **Spend:** the key has a hard limit in the Anthropic console, but be frugal.
  - Inside the VM, use `ANTHROPIC_MODEL=claude-haiku-4-5` (or the cheapest
    current model) for functional tests.
  - Use the default model only for the final demo run.
  - Set the PoC's per-session cap low (a few dollars) so the cap test is
    cheap.
  - Log the night's model spend in PROGRESS.md from `tether report`.

## Rules for working unattended

- **Commands you can't use:** no `sudo`; if a step needs it, you're blocked.
  No changes to macOS security settings (SIP, Gatekeeper, TCC, the firewall).
  Nothing that pops a GUI dialog.
- **Where you can install:** per-user only (rustup, `~/.local`, Homebrew if
  this user can write to it), plus `~/.microsandbox`.
- **Where you can write:** this repo and `~/.microsandbox` only.
- **Killing processes:** use the pidfiles you write, never `pkill -f`. The
  spike learned this the hard way: `pkill -f` matched its own shell.
- **Running services:** start long-running services in the background with a
  pidfile and a log under `overnight/run/` (gitignored). Stop them at the end
  of every run, so the next run starts clean.
- **Time limits:** put a limit on anything that can hang: boots, network
  calls, the attack suite. This is macOS with bash 3.2 and BSD tools, and
  GNU `timeout` is **not installed**: calling it exits 127, which looks like
  a hang or a failed check rather than a missing command. Use
  `perl -e 'alarm shift; exec @ARGV' 60 cmd args…` instead (it exits 142 on
  expiry). See `~/.claude/CLAUDE.md` for the other GNU/BSD differences
  (`sed -i ''`, `stat -f`, `date -v`, no `grep -P`), and run `/bin/bash -n`
  on every script you write.
- **The whole run** is also capped by the loop (`RUN_MINUTES` in
  `overnight/config.env`). Commit and update PROGRESS.md as soon as a
  sub-step works, not at the end.
- **Git:** never push to `main`. Push only `poc/tether`, and only
  after tests pass.
- **Code style:**
  - bash with `set -euo pipefail`, clean under shellcheck;
  - Python 3.10+ with type hints, clean under
    `ruff check --select E,F,W,I,B,UP --line-length 120`;
  - tether stays stdlib Python for the PoC.
- **Tests:** every tether change gets a unit test in `tests/test_tether.py`,
  and the existing 14 leash tests keep passing.
- **Honesty:** if a claim in the design turns out wrong when you test it
  (a flag, a path, a behavior), the code wins. Record the correction in
  PROGRESS.md and in a short "Corrections" section at the end of the design
  doc. Don't silently work around it.

## Finishing

On the last runs:

1. **`spike/RESULTS.md`:** add a new section for this PoC, with every success
   criterion from the design marked pass, fail or not run. Include the
   measurements (boot to agent-ready; time to first byte through tether
   versus direct, at p50 and p95; CONNECT setup; overshoot with and without
   reservations; memory) and every fallback taken.
2. **The demo recording:** adapt `spike/record/demo.tape` to the design's demo
   script and record it with VHS to `overnight/run/poc-demo.mp4`; recordings
   aren't committed. Watch the frames for anything secret before you keep it.
   If VHS can't drive it unattended, write the exact commands to
   `overnight/DEMO.md` instead.
3. **`overnight/REPORT.md`:** at most one page.
   - What works, what doesn't, and what was faked or skipped.
   - The acceptance table and the numbers.
   - Every fallback taken.
   - The three things Chad should look at first.
4. **The PR:** push the branch and open a draft PR with `gh pr create --draft`.
   Its description is REPORT.md plus a "how to run it" section.
5. **Finish:** write `DONE` to `overnight/STATUS`.
