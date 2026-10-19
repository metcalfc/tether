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
#   cage.sh gw-up      start the gateway on 127.0.0.1:18001 (pidfile, log)
#   cage.sh gw-down    stop it
#   cage.sh up         mint caps, boot the locked sandbox, push env + scripts
#   cage.sh exec CMD   run CMD in the guest (bash -lc, env loaded)
#   cage.sh check      the milestone-1 reachability probes
#   cage.sh down       stop and remove the sandbox, stop the gateway
#   cage.sh status
#
# Environment: CAGE_NAME (tether-cage), CAGE_IMAGE (node:22-bookworm),
# CAGE_GW (./leash), CAGE_POLICY (spike/msb/policy.json), CAGE_PORT (18001).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
MSB=${MSB:-$HOME/.microsandbox/bin/msb}
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

log() { printf '[cage] %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

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
  env="$(state_dir)/cage.env"
  [[ -f $env ]] || die "no $env after init"
  log "booting $NAME from $IMAGE: --net-default deny --net-rule allow@host:tcp:$PORT"
  local t0 t1
  t0=$(perl -MTime::HiRes=time -e 'printf "%.3f", time')
  limit 300 "$MSB" create "$IMAGE" --name "$NAME" -q \
    --cpus 4 --memory 4G \
    --net-default deny --net-rule "allow@host:tcp:$PORT" \
    --tmpfs /run/tether
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
  for f in "$HERE"/guest/*.sh; do
    [[ -f $f ]] || continue
    limit 60 "$MSB" copy "$f" "$NAME:$GUEST_DIR/$(basename "$f")"
    limit 30 "$MSB" exec "$NAME" -- chmod +x "$GUEST_DIR/$(basename "$f")"
  done
  log "pushed $GUEST_ENV and $GUEST_DIR/*.sh"
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
  gw-up | gw-down | up | check | down | status) "cmd_${1//-/_}" ;;
  exec) shift; cmd_exec "$@" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
