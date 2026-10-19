#!/usr/bin/env bash
# works.sh -- run inside the microsandbox guest. The useful half: real tools,
# real work, through the gateway, on credentials the guest never holds.
#
# Port of spike/guest/works.sh. node:22-bookworm has no pip, so the PyPI step
# fetches a wheel with curl; npm and git are in the image. The git step clones
# SCRATCH_REPO (default metcalfc/tether-scratch), the only repo the policy
# routes.
set -uo pipefail

SCRATCH_REPO=${SCRATCH_REPO:-metcalfc/tether-scratch}
fails=0
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok() { echo "  ok: $*"; }
bad() { echo "  FAIL: $*"; fails=$((fails + 1)); }

step "What this machine holds: capabilities, not keys"
env | grep -E '^(ANTHROPIC|HTTPS_PROXY|OPENAI)' | sed -E 's/(lsh_[A-Za-z0-9_-]{6})[A-Za-z0-9_-]*/\1.../g'

step "fetch a wheel from PyPI (allowlisted)"
url=$(curl -sS -m 20 https://pypi.org/simple/requests/ | grep -oE 'https://files.pythonhosted.org/[^"#]+\.whl' | tail -1)
if [[ -n $url ]] && curl -sS -f -m 30 -o /tmp/requests.whl "$url"; then
  ok "$(basename "$url") $(wc -c </tmp/requests.whl) bytes"
else
  bad "PyPI fetch"
fi

step "npm view from registry.npmjs.org (allowlisted)"
if v=$(npm view --fetch-timeout=20000 left-pad version 2>/dev/null) && [[ -n $v ]]; then ok "left-pad@$v"; else bad "npm view"; fi

if [[ -n ${GIT_CONFIG_COUNT:-} ]]; then
  step "git clone $SCRATCH_REPO over the gateway (token injected outside)"
  rm -rf /tmp/scratch
  if git clone -q --depth 1 "https://github.com/$SCRATCH_REPO" /tmp/scratch; then
    ok "$(git -C /tmp/scratch log --oneline -1 2>/dev/null || echo 'empty repo')"
  else
    bad "git clone"
  fi
  step "git clone of a repo the policy does not route (should fail)"
  if git clone -q --depth 1 https://github.com/metcalfc/imp /tmp/imp 2>/dev/null; then bad "clone of metcalfc/imp succeeded"; else ok "refused"; fi
fi

if [[ -n ${ANTHROPIC_BASE_URL:-} ]] && command -v claude >/dev/null; then
  step "Claude Code, on a key it never sees"
  out=$(claude -p "Reply with exactly: TETHER_OK" 2>&1)
  echo "  $out"
  [[ $out == *TETHER_OK* ]] || bad "claude"
else
  echo; echo "(skipping Claude Code: no anthropic route on the gateway, or claude not installed)"
fi

echo
if [[ $fails -eq 0 ]]; then echo "  all worked"; else echo "  $fails FAILED"; fi
exit "$fails"
