# Overnight run

Claude Code builds the PoC in `design/poc-microsandbox-nono-leash.md`
unattended. `run.sh` restarts it against `PROMPT.md` until `STATUS` says
`DONE` or `BLOCKED`, or a limit is hit. `PROGRESS.md` is its memory between
runs.

## Before bed (about 20 minutes)

1. **Put the repo on GitHub, private.**
   - `design/` restates Ringleader's unreleased roadmap, so keep it private.
   - Run: `gh repo create metcalfc/leash --private --source . --push`, then
     `git push origin spike/mac-lockdown`.
2. **Create the scratch repo for push tests.**
   - Run: `gh repo create metcalfc/leash-scratch --private --add-readme`
   - Make a fine-grained token with contents read/write on that repo only.
   - Store it: `security add-generic-password -a "$USER" -s leash-gh-scratch -w`
3. **Store the test key.** Create a dedicated Anthropic API key with a low
   hard spend limit in the console, then store it:
   `security add-generic-password -a "$USER" -s leash-anthropic -w`
4. **Install the tools.** Run `brew install shellcheck vhs tmux ruff`, and
   have Docker Desktop or Colima running so the Landlock kernel can be built
   in a Linux container.
5. **Configure.** Run `cp overnight/config.env.example overnight/config.env`
   and edit it.
6. **Check.** Run `overnight/preflight.sh` and fix any MISS lines.
7. **Start.** Plug the Mac in and run `caffeinate -dis overnight/run.sh`.

It runs Claude Code with `--dangerously-skip-permissions`, which means any
command as your user. The safer setup is a dedicated macOS user with its own
keychain items, though Homebrew installs are awkward there. If you run it in
your own account, the prompt forbids `sudo`, system settings changes and
writes outside the repo and `~/.microsandbox`, but those are instructions,
not enforcement.

## In the morning

- **`overnight/STATUS`:** says `DONE`, `BLOCKED: …` or `STALLED: …`.
- **`overnight/REPORT.md`** and the draft PR.
- **`overnight/PROGRESS.md`:** decisions, fallbacks and the spend log.
- **`overnight/logs/`:** one log per run.
- **`overnight/run/poc-demo.mp4`:** the recording, if VHS managed it.
