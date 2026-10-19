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
#                      CASE: readme
#   cage.sh down       stop and remove the sandbox, stop the gateway
#   cage.sh status
#
# Environment: CAGE_NAME (tether-cage), CAGE_IMAGE (node:22-bookworm),
# CAGE_GW (./leash), CAGE_POLICY (spike/msb/policy.json), CAGE_PORT (18001),
# CAGE_MODEL (claude-haiku-4-5, for works), CAGE_AGENTS ("claude codex"),
# CAGE_NONO_DIR (~/.microsandbox/tether-guest/bin, mounted at /opt/nono).
# With CAGE_OUT (a `tether compile` output directory) the machine name, port,
# agents, msb flags, tether policy and every grant come from there instead,
# and CAGE_GW defaults to ./tether. CAGE_SOURCE is the policy `compile` reads
# (spike/msb/poc-policy.json).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
MSB=${MSB:-$HOME/.microsandbox/bin/msb}
OUT=${CAGE_OUT:-}
SOURCE=${CAGE_SOURCE:-$HERE/poc-policy.json}
if [[ -n $OUT && -f $OUT/machine.env ]]; then
  # shellcheck source=/dev/null # written by tether compile
  . "$OUT/machine.env"
  CAGE_GW=${CAGE_GW:-$REPO/tether}
  CAGE_POLICY=$OUT/tether.json
fi
SESSION=${CAGE_SESSION:-cage}
NAME=${CAGE_NAME:-tether-cage}
IMAGE=${CAGE_IMAGE:-node:22-bookworm}
GW=${CAGE_GW:-$REPO/leash}
POLICY=${CAGE_POLICY:-$HERE/policy.json}
PORT=${CAGE_PORT:-18001}
RUN=$REPO/overnight/run
GW_PID=$RUN/cage-gw.pid
GW_LOG=$RUN/cage-gw.log
GUEST_ENV=/root/.leash.env
GUEST_DIR=/opt/leash
# The guest build of nono (linux aarch64, glibc), mounted read-only at /opt/nono.
NONO_DIR=${CAGE_NONO_DIR:-$HOME/.microsandbox/tether-guest/bin}

log() { printf '[cage] %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }
need_out() { [[ -n $OUT && -f $OUT/machine.env ]] || die "set CAGE_OUT to a tether compile output (cage.sh compile)"; }

cmd_compile() {
  [[ -n $OUT ]] || die "set CAGE_OUT (e.g. overnight/run/compiled)"
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
  "$GW" -c "$POLICY" init
  local env
  env="$(state_dir)/$SESSION.env"
  [[ -f $env ]] || die "no $env after init"
  local args=() l t0 t1
  if [[ -n $OUT ]]; then
    need_out
    while IFS= read -r l; do args+=("$l"); done <"$OUT/msb.args"
  else
    args=(create "$IMAGE" --name "$NAME" --cpus 4 --memory 4G
      --net-default deny --net-rule "allow@host:tcp:$PORT"
      --tmpfs /run/tether --mount-dir "$NONO_DIR:/opt/nono:ro")
  fi
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
  if [[ -n $OUT ]]; then
    # The compiled nono profiles and Claude settings; nono.sh setup installs them.
    limit 30 "$MSB" exec "$NAME" -- mkdir -p "$GUEST_DIR/nono" "$GUEST_DIR/settings"
    for f in "$OUT"/nono/*.json "$OUT"/settings/*.json; do
      [[ -f $f ]] || continue
      limit 60 "$MSB" copy "$f" "$NAME:$GUEST_DIR/$(basename "$(dirname "$f")")/$(basename "$f")"
    done
  fi
  log "pushed $GUEST_ENV and $GUEST_DIR/*.{sh,py}${OUT:+, nono/ and settings/ from $OUT}"
}

cmd_exec() {
  [[ $# -gt 0 ]] || die "usage: cage.sh exec CMD..."
  local inner
  inner="set -a; . $GUEST_ENV; set +a; export LEASH_GW=http://host.microsandbox.internal:$PORT; $*"
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

cmd_attack() {
  local ctl=$((PORT + 1)) lan pid rc=0
  lan=$(ipconfig getifaddr en0 2>/dev/null || true)
  # A real listener on the next port, on loopback and the LAN, so "blocked"
  # cannot mean "nothing was listening".
  python3 -m http.server "$ctl" --bind 0.0.0.0 >"$RUN/cage-control.log" 2>&1 &
  pid=$!
  echo "$pid" >"$RUN/cage-control.pid"
  sleep 0.5
  if [[ $(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$ctl/") != 200 ]]; then
    kill "$pid" 2>/dev/null || true; die "control listener on $ctl did not come up"
  fi
  log "control listener on 0.0.0.0:$ctl answers 200 from the host; host LAN ${lan:-unknown}"
  limit 300 "$MSB" exec "$NAME" --no-stdin -- bash -lc \
    "set -a; . $GUEST_ENV; set +a; export LEASH_GW=http://host.microsandbox.internal:$PORT CONTROL_PORT=$ctl HOST_LAN=$lan; $GUEST_DIR/attack.sh" || rc=$?
  kill "$pid" 2>/dev/null || true
  rm -f "$RUN/cage-control.pid"
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
  limit 600 "$MSB" exec "$NAME" --no-stdin -- env AGENTS="$CAGE_AGENTS" CAGE_PORT="$PORT" \
    CAGE_MODEL="${CAGE_MODEL:-claude-haiku-4-5}" ADV_MODEL_RUN="${ADV_MODEL_RUN:-1}" "$GUEST_DIR/adv.sh" "$1" || rc=$?
  [[ $rc == 0 ]] || log "adv.sh $1 exited $rc"
  python3 "$HERE/advcheck.py" "$audit" "$from" "$1"
}

cmd_down() {
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
  compile | gw-up | gw-down | up | check | push | attack | works | users | caps | nono | proc | down | status) "cmd_${1//-/_}" ;;
  exec) shift; cmd_exec "$@" ;;
  adv) shift; cmd_adv "$@" ;;
  *) sed -n '2,46p' "$0"; exit 2 ;;
esac
