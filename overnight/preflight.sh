#!/usr/bin/env bash
# shellcheck disable=SC2015  # ok/bad/warn always succeed, so A && ok || bad is a safe if/else
# preflight.sh -- check everything the overnight run needs before you go to
# bed. Prints a line per check; exits non-zero if anything required is missing.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1
# shellcheck disable=SC1091
[[ -f overnight/config.env ]] && . overnight/config.env
fail=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  MISS  %s\n' "$1"; fail=1; }
warn() { printf '  warn  %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "tether overnight preflight"
[[ $(uname -s)/$(uname -m) == Darwin/arm64 ]] && ok "Apple Silicon Mac" || bad "not an Apple Silicon Mac"
[[ -f overnight/config.env ]] && ok "overnight/config.env" || bad "overnight/config.env (copy config.env.example)"
have claude && ok "claude $(claude --version 2>/dev/null | head -1)" || bad "claude not on PATH"
have git && ok "git" || bad "git"
if have gh && gh auth status >/dev/null 2>&1; then ok "gh signed in"; else bad "gh not signed in (gh auth login)"; fi
git remote get-url origin >/dev/null 2>&1 && ok "origin: $(git remote get-url origin)" || bad "no git remote 'origin' for the PR"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null && ok "python3 >= 3.10" || bad "python3 3.10+"
have ruff && ok "ruff" || warn "ruff missing (pip install ruff); the run can install it"
have shellcheck && ok "shellcheck" || warn "shellcheck missing (brew install shellcheck)"
have vhs && ok "vhs" || warn "vhs missing (brew install vhs); demo falls back to DEMO.md"
have tmux && ok "tmux" || warn "tmux missing (brew install tmux)"
if have docker && docker info >/dev/null 2>&1; then ok "docker running (for the Linux kernel build)"
elif have limactl; then ok "lima available (for the Linux kernel build)"
else warn "no running Docker and no Lima: the Landlock kernel build will likely fall back"; fi
have rustup && ok "rustup" || warn "rustup missing; the run will try to install it per-user"
security find-generic-password -s "${ANTHROPIC_KEYCHAIN:-tether-anthropic}" -w >/dev/null 2>&1 \
  && ok "keychain: ${ANTHROPIC_KEYCHAIN:-tether-anthropic}" || bad "keychain item ${ANTHROPIC_KEYCHAIN:-tether-anthropic}"
security find-generic-password -s "${GITHUB_KEYCHAIN:-tether-gh-scratch}" -w >/dev/null 2>&1 \
  && ok "keychain: ${GITHUB_KEYCHAIN:-tether-gh-scratch}" || bad "keychain item ${GITHUB_KEYCHAIN:-tether-gh-scratch}"
if [[ -n ${SCRATCH_REPO:-} ]] && gh repo view "$SCRATCH_REPO" >/dev/null 2>&1; then ok "scratch repo $SCRATCH_REPO"
else bad "scratch repo SCRATCH_REPO=${SCRATCH_REPO:-unset} not reachable"; fi
[[ -z ${ANTHROPIC_API_KEY:-} ]] && ok "ANTHROPIC_API_KEY not exported" || warn "ANTHROPIC_API_KEY is exported here; run.sh unsets it"
free_gb=$(df -g . | awk 'NR==2 {print $4}')
(( free_gb >= 40 )) && ok "${free_gb} GB free" || warn "only ${free_gb} GB free; kernel build plus images want ~40 GB"
python3 -m unittest discover -s tests >/dev/null 2>&1 && ok "unit tests pass" || bad "unit tests fail"
[[ -f design/tether-poc.md ]] && ok "design doc present" || bad "design/tether-poc.md missing"
[[ $(pmset -g 2>/dev/null | awk '$1 == "sleep" {print $2}') == 0 ]] && ok "system sleep disabled" || warn "Mac may sleep: run under caffeinate -dis (run.sh usage shows it)"
echo
if (( fail )); then echo "fix the MISS lines first"; exit 1; fi
echo "ready: caffeinate -dis overnight/run.sh"
