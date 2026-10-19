#!/usr/bin/env bash
# cage.sh -- a microsandbox VM whose only way out is the gateway.
#
# msb's netstack (smoltcp, in the host process) runs with a deny-all policy
# and one allow rule: the `host` group on TCP 18001. agentd writes
# host.microsandbox.internal into the guest's /etc/hosts, and msb dials that
# address as the host's loopback, where the gateway listens. No DNS rule, so
# every guest query gets NXDOMAIN. Enforcement lives in the msb process on
# the host, so root in the guest has nothing to flush.
#
#   cage.sh compile    tether compile CAGE_SOURCE -o CAGE_OUT (msb flags,
#                      nono profiles, tether config, all from one file)
#   cage.sh gw-up      start the gateway on 127.0.0.1:18001 (pidfile, log)
#   cage.sh gw-down    stop it
#   cage.sh up         mint caps, boot the locked sandbox, push env + scripts
#   cage.sh exec CMD   run CMD in the guest (bash -lc, env loaded)
#   cage.sh check      the milestone-1 reachability probes
#   cage.sh push       re-push env + guest scripts to a running sandbox
#   cage.sh attack     run guest/attack.sh as root, with a live control
#                      listener on host port CAGE_PORT+1 and the host LAN IP
#   cage.sh works      run guest/works.sh
#   cage.sh users      per-agent users and a shared /work (guest/users.sh
#                      setup), then prove it (users.sh check)
#   cage.sh caps       mint one cap per agent and per (agent, tool), with the
#                      compiled grants, and pipe each into the guest's
#                      /run/tether (never on host disk); needs CAGE_OUT
#   cage.sh nono       install the compiled nono profiles + launchers
#                      (guest/nono.sh setup), caps, then prove them
#                      (nono.sh check); run `users` first; needs CAGE_OUT
#   cage.sh proc       /proc and ptrace routes to another tool's or agent's
#                      cap (guest/nono.sh proc); run `nono` first
#   cage.sh adv CASE   milestone 7's adversarial cases (guest/adv.sh CASE as
#                      guest root), then check tether's audit lines from
#                      this run (advcheck.py); run `users` and `nono` first.
#                      CASE: readme, pkg, root (root also starts the
#                      control listener, as attack does)
#   cage.sh replay-policy
#                      write CAGE_OUT/tether-replay.json: the compiled
#                      tether config plus a second sandbox (NAME2, port
#                      CAGE_PORT+10, session cage2). Export CAGE_POLICY to it
#                      before gw-up and up, so one gateway has both listeners
#   cage.sh replay     milestone 7: boot NAME2, copy claude's caps out of the
#                      first cage as root, replay them from NAME2 on its own
#                      listener (expect 403), with controls on both channels,
#                      then check the audit (advcheck.py replay); removes NAME2
#   cage.sh down       stop and remove the sandbox(es), stop the gateway
#   cage.sh status
#
# Environment: CAGE_OUT (overnight/run/compiled), the `tether compile` output
# directory the machine name, port, agents, msb flags, tether policy and every
# grant come from; CAGE_SOURCE (spike/msb/poc-policy.json), the policy
# `compile` reads; CAGE_GW (./tether); CAGE_POLICY (CAGE_OUT/tether.json,
# overridden by replay-policy's file); CAGE_MODEL (claude-haiku-4-5, for works).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
MSB=${MSB:-$HOME/.microsandbox/bin/msb}
RUN=$REPO/overnight/run
OUT=${CAGE_OUT:-$RUN/compiled}
SOURCE=${CAGE_SOURCE:-$HERE/poc-policy.json}
# shellcheck source=/dev/null # written by tether compile
[[ -f $OUT/machine.env ]] && . "$OUT/machine.env"
SESSION=${CAGE_SESSION:-cage}
NAME=${CAGE_NAME:-tether-cage}
GW=${CAGE_GW:-$REPO/tether}
POLICY=${CAGE_POLICY:-$OUT/tether.json}
PORT=${CAGE_PORT:-18001}
GW_PID=$RUN/cage-gw.pid
GW_LOG=$RUN/cage-gw.log
GUEST_ENV=/root/.tether.env
GUEST_DIR=/opt/tether

log() { printf '[cage] %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }
need_out() { [[ -f $OUT/machine.env ]] || die "no tether compile output in $OUT (cage.sh compile)"; }

cmd_compile() {
  "$REPO/tether" compile "$SOURCE" -o "$OUT"
}

# perl alarm stands in for GNU timeout, which macOS does not have.
limit() { perl -e 'alarm shift; exec @ARGV' "$@"; }

gw_running() { [[ -f $GW_PID ]] && kill -0 "$(cat "$GW_PID")" 2>/dev/null; }

state_dir() {
  python3 -c 'import json,sys,pathlib; p=pathlib.Path(sys.argv[1]); print((p.parent/json.loads(p.read_text()).get("state_dir","run")).resolve())' "$POLICY"
}

cmd_gw_up() {
  mkdir -p "$RUN"
  gw_running && { log "gateway already running, pid $(cat "$GW_PID")"; return; }
  "$GW" -c "$POLICY" serve >"$GW_LOG" 2>&1 &
  echo $! >"$GW_PID"
  local _
  for _ in $(seq 1 50); do
    if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then log "gateway up on 127.0.0.1:$PORT (log $GW_LOG)"; return; fi
    gw_running || break
    sleep 0.1
  done
  die "gateway did not come up -- see $GW_LOG"
}

cmd_gw_down() {
  if gw_running; then kill "$(cat "$GW_PID")"; log "gateway stopped"; fi
  rm -f "$GW_PID"
}

sandbox_exists() { "$MSB" list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$NAME"; }

cmd_up() {
  sandbox_exists && die "sandbox $NAME exists; cage.sh down first"
  need_out
  "$GW" -c "$POLICY" init
  local env
  env="$(state_dir)/$SESSION.env"
  [[ -f $env ]] || die "no $env after init"
  local args=() l t0 t1
  while IFS= read -r l; do args+=("$l"); done <"$OUT/msb.args"
  log "booting: msb ${args[*]}"
  t0=$(perl -MTime::HiRes=time -e 'printf "%.3f", time')
  limit 300 "$MSB" "${args[@]}" -q
  limit 60 "$MSB" exec "$NAME" -- true
  t1=$(perl -MTime::HiRes=time -e 'printf "%.3f", time')
  log "boot to first exec: $(perl -e 'printf "%.2f", $ARGV[1]-$ARGV[0]' "$t0" "$t1")s"
  push_files "$env"
}

push_files() {
  local env=$1 f
  limit 60 "$MSB" copy "$env" "$NAME:$GUEST_ENV"
  limit 30 "$MSB" exec "$NAME" -- chmod 600 "$GUEST_ENV"
  limit 30 "$MSB" exec "$NAME" -- mkdir -p "$GUEST_DIR"
  for f in "$HERE"/guest/*.sh "$HERE"/guest/*.py; do
    [[ -f $f ]] || continue
    limit 60 "$MSB" copy "$f" "$NAME:$GUEST_DIR/$(basename "$f")"
    limit 30 "$MSB" exec "$NAME" -- chmod +x "$GUEST_DIR/$(basename "$f")"
  done
  # The compiled nono profiles and Claude settings; nono.sh setup installs them.
  limit 30 "$MSB" exec "$NAME" -- mkdir -p "$GUEST_DIR/nono" "$GUEST_DIR/settings"
  for f in "$OUT"/nono/*.json "$OUT"/settings/*.json; do
    [[ -f $f ]] || continue
    limit 60 "$MSB" copy "$f" "$NAME:$GUEST_DIR/$(basename "$(dirname "$f")")/$(basename "$f")"
  done
  log "pushed $GUEST_ENV and $GUEST_DIR/*.{sh,py}, nono/ and settings/ from $OUT"
}

cmd_exec() {
  [[ $# -gt 0 ]] || die "usage: cage.sh exec CMD..."
  local inner
  inner="set -a; . $GUEST_ENV; set +a; export TETHER_GW=http://host.microsandbox.internal:$PORT; $*"
  "$MSB" exec "$NAME" --no-stdin -- bash -lc "$inner"
}

cmd_check() {
  local fails=0 got
  probe() { # probe <want 000|non-000> <what> <curl args...>
    local want=$1 what=$2; shift 2
    got=$(limit 20 "$MSB" exec "$NAME" --no-stdin -- \
      curl -s -o /dev/null -m 6 --noproxy '*' -w '%{http_code}' "$@" 2>/dev/null || true)
    got=${got:-000}
    if { [[ $want == reach ]] && [[ $got != 000 ]]; } || { [[ $want == block ]] && [[ $got == 000 ]]; }; then
      printf '  PASS  %-6s %-4s %s\n' "$want" "$got" "$what"
    else
      printf '  FAIL  %-6s %-4s %s\n' "$want" "$got" "$what"; fails=$((fails + 1))
    fi
  }
  probe reach "gateway at host.microsandbox.internal:$PORT" "http://host.microsandbox.internal:$PORT/"
  probe block "host.microsandbox.internal:22 (host ssh)" "http://host.microsandbox.internal:22/"
  probe block "host.microsandbox.internal:$((PORT + 1)) (other host port)" "http://host.microsandbox.internal:$((PORT + 1))/"
  probe block "https://example.com direct" "https://example.com/"
  probe block "https://1.1.1.1 direct (IP literal)" -k "https://1.1.1.1/"
  probe block "https://api.anthropic.com direct" "https://api.anthropic.com/"
  echo "  $fails failed"
  return "$fails"
}

cmd_push() { push_files "$(state_dir)/$SESSION.env"; }

# A real listener on CAGE_PORT+1, on loopback and the LAN, so "blocked"
# cannot mean "nothing was listening". Sets CTL_PORT and HOST_LAN.
control_up() {
  local pid
  CTL_PORT=$((PORT + 1))
  HOST_LAN=$(ipconfig getifaddr en0 2>/dev/null || true)
  python3 -m http.server "$CTL_PORT" --bind 0.0.0.0 >"$RUN/cage-control.log" 2>&1 &
  pid=$!
  echo "$pid" >"$RUN/cage-control.pid"
  sleep 0.5
  if [[ $(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$CTL_PORT/") != 200 ]]; then
    control_down; die "control listener on $CTL_PORT did not come up"
  fi
  log "control listener on 0.0.0.0:$CTL_PORT answers 200 from the host; host LAN ${HOST_LAN:-unknown}"
}

control_down() {
  [[ -f $RUN/cage-control.pid ]] && kill "$(cat "$RUN/cage-control.pid")" 2>/dev/null || true
  rm -f "$RUN/cage-control.pid"
}

cmd_attack() {
  local rc=0
  control_up
  limit 300 "$MSB" exec "$NAME" --no-stdin -- bash -lc \
    "set -a; . $GUEST_ENV; set +a; export TETHER_GW=http://host.microsandbox.internal:$PORT CONTROL_PORT=$CTL_PORT HOST_LAN=$HOST_LAN; $GUEST_DIR/attack.sh" || rc=$?
  control_down
  return "$rc"
}

# Functional runs use the cheap model; CAGE_MODEL overrides it.
cmd_works() {
  limit 600 "$MSB" exec "$NAME" --no-stdin -- bash -lc \
    "set -a; . $GUEST_ENV; set +a; export ANTHROPIC_MODEL=${CAGE_MODEL:-claude-haiku-4-5}; $GUEST_DIR/works.sh"
}

CAGE_AGENTS=${CAGE_AGENTS:-claude codex}
HUMANS=${CAGE_HUMANS:-dev}
HUMAN=${HUMANS%% *}

cmd_users() {
  limit 120 "$MSB" exec "$NAME" --no-stdin -- env AGENTS="$CAGE_AGENTS" HUMAN="${HUMAN:-dev}" "$GUEST_DIR/users.sh" setup
  limit 120 "$MSB" exec "$NAME" --no-stdin -- env AGENTS="$CAGE_AGENTS" HUMAN="${HUMAN:-dev}" "$GUEST_DIR/users.sh" check
}

# One cap per line of the compiled caps.list ("AGENT agent" or "AGENT TOOL"),
# minted with the grants tether compile wrote for it. The agent's own cap rides
# in its session's environment; a tool's cap is a file in the tool's own format
# (gitconfig, npmrc) that only that tool's command sandbox can read.
cmd_caps() {
  need_out
  local a t grant
  while read -r a t; do
    grant=$a
    [[ $t == agent ]] || grant=$a/$t
    "$GW" -c "$POLICY" cap mint --session "$SESSION" --grant "$grant" --ttl 12h --env - |
      limit 60 "$MSB" exec "$NAME" -- "$GUEST_DIR/nono.sh" put "$a" "$t"
  done <"$OUT/caps.list"
}

cmd_nono() {
  need_out
  limit 120 "$MSB" exec "$NAME" --no-stdin -- env AGENTS="$CAGE_AGENTS" CAGE_PORT="$PORT" "$GUEST_DIR/nono.sh" setup
  cmd_caps
  limit 600 "$MSB" exec "$NAME" --no-stdin -- env AGENTS="$CAGE_AGENTS" CAGE_PORT="$PORT" "$GUEST_DIR/nono.sh" check
}

# Can one tool, or another agent, read a cap through /proc or ptrace? Run `nono` first.
cmd_proc() {
  limit 300 "$MSB" exec "$NAME" --no-stdin -- env AGENTS="$CAGE_AGENTS" CAGE_PORT="$PORT" "$GUEST_DIR/nono.sh" proc
}

cmd_adv() {
  need_out
  [[ $# -eq 1 ]] || die "usage: cage.sh adv CASE"
  local audit from rc=0
  audit="$(state_dir)/audit.jsonl"
  from=$(wc -l <"$audit" | tr -d ' ')
  CTL_PORT='' HOST_LAN=''
  [[ $1 != root ]] || control_up
  limit 600 "$MSB" exec "$NAME" --no-stdin -- env AGENTS="$CAGE_AGENTS" CAGE_PORT="$PORT" \
    CONTROL_PORT="$CTL_PORT" HOST_LAN="$HOST_LAN" \
    CAGE_MODEL="${CAGE_MODEL:-claude-haiku-4-5}" ADV_MODEL_RUN="${ADV_MODEL_RUN:-1}" "$GUEST_DIR/adv.sh" "$1" |
    sed 's/tth_[A-Za-z0-9_-]*/tth_REDACTED/g' || rc=$?
  [[ $1 != root ]] || control_down
  [[ $rc == 0 ]] || log "adv.sh $1 exited $rc"
  sleep 1 # tether writes a CONNECT's line when the tunnel closes
  python3 "$HERE/advcheck.py" "$audit" "$from" "$1" "$POLICY"
}

NAME2=$NAME-2
PORT2=$((PORT + 10))
SESSION2=${SESSION}2

cmd_replay_policy() {
  need_out
  python3 - "$OUT/tether.json" "$OUT/tether-replay.json" "$NAME2" "$PORT2" "$SESSION2" <<'PY'
import json, sys
src, dst, name, port, session = sys.argv[1:]
cfg = json.load(open(src))
cfg["_note"] += "; plus a second sandbox for the cap-replay case (cage.sh replay-policy)"
cfg["sandboxes"][name] = {"listen": f"127.0.0.1:{port}", "advertise": f"http://host.microsandbox.internal:{port}",
                          "sessions": [session]}
json.dump(cfg, open(dst, "w"), indent=2)
PY
  log "wrote $OUT/tether-replay.json (sandboxes: $NAME on $PORT, $NAME2 on $PORT2); export CAGE_POLICY to it"
}

# The cap value from a cap file or env file: the first tth_ token in it, or
# in a Basic header (the gitconfig format base64s it into http.extraHeader).
cap_of() {
  python3 -c 'import base64,re,sys
t = sys.stdin.read()
t += "".join(" " + base64.b64decode(b).decode(errors="replace") for b in re.findall(r"Basic ([A-Za-z0-9+/=]+)", t))
m = re.search(r"tth_[A-Za-z0-9_-]+", t)
print(m.group(0) if m else "")'
}

# Run a probe script in a sandbox as root, caps on stdin (never in argv or on
# disk). The script reads AGENT_CAP, GIT_CAP and OWN_CAP, one per line.
probe_in() { # probe_in SANDBOX PORT SCRIPT
  limit 120 "$MSB" exec "$1" -- env GW="http://host.microsandbox.internal:$2" OTHER_GW="http://host.microsandbox.internal:$3" \
    bash -c 'set -u; read -r AGENT_CAP; read -r GIT_CAP; read -r OWN_CAP; '"$4"
}

# The probes: a model route (free GET), a CONNECT and a git clone's first
# request, each with claude's agent or git cap, plus OWN_CAP (this sandbox's
# own session cap) as a control and the other sandbox's listener port.
# shellcheck disable=SC2016 # expanded in the guest
REPLAY_PROBES='
code() { curl -s -o /dev/null -m 15 -w "%{http_code}" "$@" 2>/dev/null || true; }
conn() { curl -s -o /dev/null -m 15 -w "%{http_connect}" "$@" 2>/dev/null || true; } # the proxy answer to CONNECT
echo "models   $(code -H "x-api-key: $AGENT_CAP" -H "anthropic-version: 2023-06-01" "$GW/anthropic/v1/models")   claude agent cap, GET /anthropic/v1/models"
echo "connect  $(conn -x "http://agent:$AGENT_CAP@${GW#http://}" https://pypi.org/simple/)   claude agent cap, CONNECT pypi.org"
echo "git      $(code -u "x:$GIT_CAP" "$GW/github/metcalfc/tether-scratch/info/refs?service=git-upload-pack")   claude git cap, info/refs"
echo "own      $(conn -x "http://agent:$OWN_CAP@${GW#http://}" https://pypi.org/simple/)   own session cap, CONNECT pypi.org (control)"
echo "port     $(code "$OTHER_GW/")   the other sandbox listener, no cap"
'

cmd_replay() {
  need_out
  python3 -c 'import json,sys; sys.exit(sys.argv[2] not in json.load(open(sys.argv[1]))["sandboxes"])' "$POLICY" "$NAME2" ||
    die "$POLICY has no sandbox $NAME2: cage.sh replay-policy, export CAGE_POLICY, then gw-up and up"
  sandbox_exists || die "sandbox $NAME is not up"
  local audit from caps own rc=0 args=() l t0 t1 state
  state=$(state_dir)
  audit="$state/audit.jsonl"
  from=$(wc -l <"$audit" | tr -d ' ')
  # The first cage's machine flags, renamed and re-pointed at the second listener.
  while IFS= read -r l; do
    case $l in
      "$NAME") args+=("$NAME2") ;;
      "allow@host:tcp:$PORT") args+=("allow@host:tcp:$PORT2") ;;
      *) args+=("$l") ;;
    esac
  done <"$OUT/msb.args"
  "$MSB" list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$NAME2" && die "sandbox $NAME2 exists; cage.sh down first"
  log "booting the second sandbox: msb ${args[*]}"
  t0=$(perl -MTime::HiRes=time -e 'printf "%.3f", time')
  limit 300 "$MSB" "${args[@]}" -q
  limit 60 "$MSB" exec "$NAME2" -- true
  t1=$(perl -MTime::HiRes=time -e 'printf "%.3f", time')
  log "$NAME2 boot to first exec: $(perl -e 'printf "%.2f", $ARGV[1]-$ARGV[0]' "$t0" "$t1")s"
  # Root in the first cage reads claude's caps (run 24: root reads every cap).
  caps="$(limit 30 "$MSB" exec "$NAME" -- cat /run/tether/claude/agent | cap_of)
$(limit 30 "$MSB" exec "$NAME" -- cat /run/tether/claude/git | cap_of)"
  [[ $(printf '%s\n' "$caps" | grep -c '^tth_') == 2 ]] || die "could not read claude's agent and git caps from $NAME"
  log "copied claude's agent and git caps out of $NAME (values not shown)"
  own=$(cap_of <"$state/$SESSION2.env")
  printf '\n== replayed from %s, on its own listener :%s ==\n' "$NAME2" "$PORT2"
  printf '%s\n%s\n' "$caps" "$own" | probe_in "$NAME2" "$PORT2" "$PORT" "$REPLAY_PROBES" || rc=$?
  own=$(cap_of <"$state/$SESSION.env")
  printf '\n== control: the same caps in %s, on its listener :%s ==\n' "$NAME" "$PORT"
  printf '%s\n%s\n' "$caps" "$own" | probe_in "$NAME" "$PORT" "$PORT2" "$REPLAY_PROBES" || rc=$?
  caps='' own=''
  limit 60 "$MSB" stop "$NAME2" >/dev/null 2>&1 || true
  limit 60 "$MSB" rm "$NAME2" >/dev/null 2>&1 || true
  log "sandbox $NAME2 removed"
  [[ $rc == 0 ]] || log "a probe script exited $rc"
  sleep 1 # tether writes a CONNECT's line when the tunnel closes
  python3 "$HERE/advcheck.py" "$audit" "$from" replay "$POLICY"
}

cmd_down() {
  if "$MSB" list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$NAME2"; then
    limit 60 "$MSB" stop "$NAME2" >/dev/null 2>&1 || true
    limit 60 "$MSB" rm "$NAME2" >/dev/null 2>&1 || true
    log "sandbox $NAME2 removed"
  fi
  if sandbox_exists; then
    limit 60 "$MSB" stop "$NAME" >/dev/null 2>&1 || true
    limit 60 "$MSB" rm "$NAME" >/dev/null 2>&1 || true
    log "sandbox $NAME removed"
  fi
  cmd_gw_down
}

cmd_status() {
  if gw_running; then log "gateway running, pid $(cat "$GW_PID")"; else log "gateway not running"; fi
  "$MSB" list
}

case "${1:-}" in
  compile | gw-up | gw-down | up | check | push | attack | works | users | caps | nono | proc | replay-policy | replay | down | status) "cmd_${1//-/_}" ;;
  exec) shift; cmd_exec "$@" ;;
  adv) shift; cmd_adv "$@" ;;
  *) sed -n '2,57p' "$0"; exit 2 ;;
esac
