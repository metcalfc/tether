#!/usr/bin/env bash
# demo.sh -- the PoC demo (design/tether-poc.md, "Demo script"), one
# subcommand per step, so a VHS tape (spike/record/poc-demo.tape) or a person
# can drive it. Run from the repo root; each step ends with "== step N done".
#
#   demo.sh setup      off camera: fresh tether state, compile, the two-sandbox
#                      policy, gateway up
#   demo.sh 1          the policy file, tether compile and its three outputs
#   demo.sh 2          boot the cage (timed), users, nono, Claude Code
#   demo.sh 3          the guest holds caps, not keys
#   demo.sh 4          Claude Code fixes a failing test, npm install, git push
#   demo.sh 5          tether report --by tool
#   demo.sh 6          an injected README: paste site and push to evil/x
#   demo.sh 7          a malicious postinstall
#   demo.sh 8          root kills nono and tries every way out; cap replay
#   demo.sh 9          four parallel streams against a spend cap
#   demo.sh 10         tether cap revoke, and the next call fails
#   demo.sh teardown   cage down, gateway down
#
# Full output of each step goes to overnight/run/demo/step-N.log; the screen
# gets the part worth reading. Environment: DEMO_MODEL (empty: Claude Code's
# default model, for step 4 only), SCRATCH_REPO (from overnight/config.env).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
RUN=$REPO/overnight/run
LOGS=$RUN/demo
export MSB_LIBKRUNFW_PATH=$RUN/krunfw-landlock/libkrunfw.5.dylib
export CAGE_OUT=$RUN/compiled
export CAGE_POLICY=$CAGE_OUT/tether-replay.json
CAGE=$HERE/cage.sh
T=$REPO/tether
MSB=${MSB:-$HOME/.microsandbox/bin/msb}
NAME=tether-cage
# shellcheck source=/dev/null
[[ -f $REPO/overnight/config.env ]] && . "$REPO/overnight/config.env"
SCRATCH=${SCRATCH_REPO:-metcalfc/tether-scratch}

limit() { perl -e 'alarm shift; exec @ARGV' "$@"; }
now() { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }
since() { perl -e 'printf "%.1f", $ARGV[1]-$ARGV[0]' "$1" "$(now)"; }
title() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
show() { printf '\033[2m$ %s\033[0m\n' "$*"; } # the command a step runs, as text
guest() { limit "$1" "$MSB" exec "$NAME" --no-stdin -- env SCRATCH_REPO="$SCRATCH" DEMO_MODEL="${DEMO_MODEL:-}" \
  /opt/leash/demo.sh "${@:2}"; }
mkdir -p "$LOGS"
redact() { sed -E 's/tth_[A-Za-z0-9_-]+/tth_REDACTED/g'; }

step_setup() {
  mkdir -p "$LOGS"
  local state
  state=$RUN/tether-msb/state
  # audit verify fails on the older audit file (pre-chain lines), so the demo
  # starts from an empty state dir. The old one is kept, not deleted.
  [[ ! -d $state ]] || mv "$state" "$state.pre-demo-$(date +%Y%m%d%H%M%S)"
  "$CAGE" compile >/dev/null
  "$CAGE" replay-policy
  "$CAGE" gw-up
}

step_1() {
  title "1. one policy file"
  show "spike/msb/poc-policy.json (abridged)"
  python3 - "$HERE/poc-policy.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
m = p["machine"]
print(f"machine  {m['name']}: {m['image']}, port {m['port']}, humans {m['humans']}")
for n, r in p["gateway"]["routes"].items():
    paths = r["paths"] if len(r["paths"]) <= 4 else r["paths"][:2] + [f"... {len(r['paths'])} paths"]
    print(f"route    {n}: {r['upstream']} with {p['gateway']['credentials'][r['credential']]}  {paths}")
print(f"spend    ${p['gateway']['spend']['session_usd_per_day']}/session/day, max_inflight {p['gateway']['spend']['max_inflight']}")
for a, d in p["agents"].items():
    print(f"agent    {a}: routes {d.get('routes', [])}, egress {d['egress']}, writes {d['fs']['write'][:2]}...")
    for t, td in d["tools"].items():
        extra = ", scripts" if td.get("scripts") else ""
        print(f"           tool {t}: routes {td.get('routes', [])}, egress {td.get('egress', [])}{extra}")
print(f"ceiling  routes {p['ceiling']['routes']}, egress {p['ceiling']['egress']}")
PY
  show "tether compile spike/msb/poc-policy.json -o overnight/run/compiled"
  "$T" compile "$HERE/poc-policy.json" -o "$CAGE_OUT" 2>&1 | sed "s#$REPO/##g"
  "$CAGE" replay-policy 2>/dev/null
  (cd "$CAGE_OUT" && find msb.args nono tether.json -type f | sort | sed 's/^/  /')
  show "msb flags (machine layer)"
  paste -sd' ' "$CAGE_OUT/msb.args" | sed "s#$HOME#~#g" | fold -s -w 110 | sed 's/^/  /'
}

step_2() {
  title "2. boot the cage"
  local t0 t1
  t0=$(now)
  show "msb create ... (the compiled flags: deny all, allow the host on :18001)"
  "$CAGE" up 2>&1 | grep -E 'boot to first exec' | sed 's/^\[cage\] /  /'
  show "per-agent users, nono profiles and launchers, caps into /run/tether"
  "$CAGE" users >"$LOGS/step-2-users.log" 2>&1
  "$CAGE" nono >"$LOGS/step-2-nono.log" 2>&1
  printf '  users: %s   nono: %s\n' "$(grep -cE '^ *PASS|ok' "$LOGS/step-2-users.log" || true) checks" \
    "$(grep -E 'failed' "$LOGS/step-2-nono.log" | tail -1 | sed 's/^ *//')"
  show "npm install -g @anthropic-ai/claude-code (as root, through tether's CONNECT allowlist)"
  "$CAGE" exec 'npm install -g --no-fund --no-audit @anthropic-ai/claude-code >/dev/null 2>&1; claude --version' 2>&1 | sed 's/^/  /'
  t1=$(since "$t0")
  printf '  boot to agent-ready: %ss\n' "$t1"
}

step_3() {
  title "3. the guest holds caps, not keys"
  guest 300 env 2>&1 | redact
}

step_4() {
  title "4. Claude Code fixes a failing test and pushes it"
  guest 700 fix "$(date +%Y%m%d%H%M%S)" 2>&1 | redact
}

step_5() {
  title "5. who did what, and what it cost"
  show "tether report --by tool"
  "$T" -c "$CAGE_POLICY" report --by tool
}

step_6() {
  title "6. a README injection: a paste site and a push to evil/x"
  ADV_MODEL_RUN=0 "$CAGE" adv readme 2>&1 | grep -vE '^\[cage\]' >"$LOGS/step-6.log" || true
  grep -E 'paste:|push:|direct:|PASS|FAIL|advcheck' "$LOGS/step-6.log" | cut -c1-190
}

step_7() {
  title "7. a malicious package's postinstall"
  "$CAGE" adv pkg >"$LOGS/step-7.log" 2>&1 || true
  grep -E 'postinstall|PROBE (read|cap|exec)|rc=243|spawn sh|hook ran|webhook|model call|PASS|FAIL' "$LOGS/step-7.log" |
    grep -v '^\[adv\] fixture' | cut -c1-150
}

step_8() {
  title "8. root in the guest kills nono, then tries every way out"
  "$CAGE" adv root >"$LOGS/step-8.log" 2>&1 || true
  grep -E 'kill -9|nono left|t=(1|8) |BLOCK|failures|evil/x|another repo|webhook|example.com|advcheck|[0-9]+/[0-9]+ PASS|FAIL' \
    "$LOGS/step-8.log" | grep -vE '^ *PASS +(allow|block)' | cut -c1-150 | head -24
  printf '  attack.sh: %s blocked, %s failed\n' "$(grep -cE '^ *PASS +block' "$LOGS/step-8.log" || true)" \
    "$(grep -cE '^ *FAIL +(allow|block)' "$LOGS/step-8.log" || true)"
  title "8b. a cap copied to a second sandbox"
  "$CAGE" replay >"$LOGS/step-8b.log" 2>&1 || true
  grep -E '^== |^(models|connect|git|own|port) |PASS|FAIL' "$LOGS/step-8b.log" | grep -v '^  PASS' | cut -c1-150
  grep -cE '^ *PASS' "$LOGS/step-8b.log" | sed 's/^/  advcheck PASS lines: /'
}

step_9() {
  title "9. four streams at once against a spend cap"
  show "spike/msb/overshoot.sh (a host tether; four streaming haiku calls per scenario, reservations on, then off)"
  "$HERE/overshoot.sh" >"$LOGS/step-9.log" 2>&1 || true
  grep -E '^===|four:|^reserve=' "$LOGS/step-9.log" | sed -E 's/ \(max reserve_usd.*//'
}

step_10() {
  title "10. revoke the agent's cap"
  guest 60 call
  local id
  id=$("$T" -c "$CAGE_POLICY" cap list --json | python3 -c '
import json, sys
caps = [c for c in json.load(sys.stdin) if c.get("session") == "cage" and c.get("agent") == "claude"
        and not c.get("tool") and c.get("state", "live") == "live"]
print(caps[-1]["cap_id"] if caps else "")')
  [[ -n $id ]] || { echo "no live claude agent cap"; return 1; }
  show "tether cap revoke $id"
  "$T" -c "$CAGE_POLICY" cap revoke "$id"
  guest 60 call
}

step_teardown() {
  "$CAGE" down
}

case "${1:-}" in
  setup | teardown | [1-9] | 10)
    s=$1
    "step_$s" 2>&1 | tee "$LOGS/screen-$s.log"
    printf '\n== step %s done\n' "$s"
    ;;
  *) sed -n '2,22p' "$0"; exit 2 ;;
esac
