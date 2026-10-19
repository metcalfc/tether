#!/usr/bin/env bash
# demo.sh -- the guest half of the PoC demo (spike/msb/demo.sh), run as guest
# root. The cage, users and nono sessions must already be set up.
#
#   demo.sh env      what claude's session sees (caps, never keys), then a
#                    root grep of the guest disk and a scan of every
#                    process's memory for a real key
#   demo.sh fix TS   a repo with a failing test, cloned with claude's mediated
#                    git; Claude Code fixes it, runs npm install, and pushes
#                    branch agent/demo-TS to SCRATCH_REPO
#   demo.sh call     one model call from claude's session (GET /v1/models)
#
# Environment: SCRATCH_REPO (metcalfc/tether-scratch), DEMO_MODEL (empty:
# Claude Code's default model), DEMO_TURNS (25), DEMO_MAX_OUT (16000).
set -euo pipefail

SCRATCH=${SCRATCH_REPO:-metcalfc/tether-scratch}
BIN=/opt/tether/bin

sess() { # sess SCRIPT: SCRIPT in claude's nono session; stdout+stderr
  setpriv --reuid=claude --regid=claude --init-groups --reset-env -- "$BIN/agent-session" bash -c "$1" 2>&1 || true
}
short() { sed -E 's/(tth_[A-Za-z0-9_-]{4})[A-Za-z0-9_-]*/\1.../g'; }

cmd_env() {
  echo "claude's session environment (credentials and proxies only):"
  sess 'env | grep -E "^(ANTHROPIC_|TETHER_|HTTPS?_PROXY|https?_proxy|GIT_|NPM_)" | sort' | short | sed 's/^/  /'
  echo "claude's session reading its tools' caps:"
  # shellcheck disable=SC2016 # expanded in the session
  sess 'for f in /run/tether/claude/git /run/tether/claude/npm; do head -c1 "$f" >/dev/null 2>&1 && echo "  $f READ" || echo "  $f denied"; done'
  echo
  echo "root greps the whole guest disk for a real key (sk-ant-..., gho_...):"
  local n
  n=$(grep -rIlsE 'sk-ant-(api|oat)[0-9]{2}-[A-Za-z0-9_-]{40,}|gho_[A-Za-z0-9]{36}' / \
    --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev 2>/dev/null | wc -l || true)
  echo "  files with a real key: $n"
  echo "root scans every process's memory (with a planted fake key as a positive control):"
  /opt/tether/memscan.py --control 2>&1 | tail -3 | sed 's/^/  /'
}

cmd_fix() {
  local ts=${1:?usage: demo.sh fix TS} repo=/work/app out branch
  branch=agent/demo-$ts
  rm -rf "$repo"
  echo "claude clones $SCRATCH into $repo (mediated git) and a failing test is committed"
  out=$(sess "git clone -q --depth 1 https://github.com/$SCRATCH $repo && echo clone-ok")
  [[ $out == *clone-ok* ]] || { printf '%s\n' "$out"; exit 1; }
  # shellcheck disable=SC2016 # written verbatim into the session
  sess 'cd /work/app && mkdir -p test && cat >package.json <<"EOF"
{"name": "demo-app", "version": "1.0.0", "dependencies": {"ms": "2.1.3"}}
EOF
cat >duration.js <<"EOF"
const ms = require("ms");
// Formats a number of seconds for a log line, e.g. 90 -> "2m".
module.exports = function duration(seconds) {
  return ms(seconds);
};
EOF
cat >test/duration.test.js <<"EOF"
const test = require("node:test");
const assert = require("node:assert");
const duration = require("../duration");
test("formats seconds", () => {
  assert.strictEqual(duration(90), "2m");
  assert.strictEqual(duration(3600), "1h");
});
EOF
printf "node_modules/\n" >.gitignore
git add -A && git -c user.email=demo@tether -c user.name=demo commit -q -m "duration helper with a failing test" && echo fixture-ok' |
    sed 's/^/  /'
  echo
  # The default model asks for max_tokens 128000, which tether reserves at
  # about $3.20 a call: more than the whole $2 session cap (run 28). A lower
  # output limit is what makes a capped session usable.
  echo "Claude Code (${DEMO_MODEL:-default model}, max output ${DEMO_MAX_OUT:-16000} tokens) as claude, in its nono session:"
  # Not npm test: npm would run the script through sh, and the sh edge cannot exec node.
  out=$(sess "cd $repo && CLAUDE_CODE_MAX_OUTPUT_TOKENS=${DEMO_MAX_OUT:-16000} ${DEMO_MODEL:+ANTHROPIC_MODEL=$DEMO_MODEL} perl -e 'alarm shift; exec @ARGV' 600 claude -p \
    'This repo has a failing test. Run npm install, then node --test to see the failure. Fix the bug in duration.js \
(not the test), rerun node --test until it passes, commit, and push with: git push origin HEAD:$branch. \
Then reply in at most three lines: the bug, the fix, and the pushed branch.' \
    --max-turns ${DEMO_TURNS:-25} --dangerously-skip-permissions --output-format text")
  printf '%s\n' "$out" | short | sed 's/^/  claude> /' | tail -12
  echo
  echo "checks, outside Claude Code:"
  out=$(sess "cd $repo && node --test 2>&1 | grep -E '^# (pass|fail)' | tr '\n' ' '")
  echo "  node --test: $out"
  out=$(sess "cd $repo && git log --oneline -3 | tr '\n' ';'")
  echo "  git log: $out"
  out=$(sess "git ls-remote https://github.com/$SCRATCH refs/heads/$branch | cut -c1-12")
  echo "  remote $branch: ${out:-not found}"
}

cmd_call() {
  # shellcheck disable=SC2016 # expanded in the session
  sess 'printf "  claude session, GET /anthropic/v1/models: http=%s\n" "$(curl -s -o /dev/null -m 15 -w "%{http_code}" \
    -H "x-api-key: $ANTHROPIC_AUTH_TOKEN" -H "anthropic-version: 2023-06-01" "$ANTHROPIC_BASE_URL/v1/models")"'
}

case "${1:-}" in
  env) cmd_env ;;
  fix) shift; cmd_fix "$@" ;;
  call) cmd_call ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
