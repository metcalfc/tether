#!/usr/bin/env bash
# run.sh -- restart Claude Code against overnight/PROMPT.md until STATUS says
# DONE or BLOCKED, or a limit is hit. Each run is a fresh context; PROGRESS.md
# carries state between runs.
#
#   overnight/preflight.sh && caffeinate -dis overnight/run.sh
#
# Runs Claude Code with --dangerously-skip-permissions: it can run any command
# as this user. Use a dedicated macOS user, or accept that risk knowingly.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
OV=overnight
# shellcheck disable=SC1091
[[ -f $OV/config.env ]] && . "$OV/config.env"
MAX_RUNS=${MAX_RUNS:-60}
MAX_HOURS=${MAX_HOURS:-10}
STALL_RUNS=${STALL_RUNS:-4}
RUN_MINUTES=${RUN_MINUTES:-90}
BACKOFF_SECONDS=${BACKOFF_SECONDS:-900}
LOOP_MODEL=${LOOP_MODEL:-}

# The loop's own Claude Code runs on your subscription. A stray API key in the
# environment would silently switch it to billing that key instead.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
# No MCP servers or claude.ai connectors (mail, Slack, calendars) in an
# unattended, permission-skipping session that builds prompt-injection fixtures.
export ENABLE_CLAUDEAI_MCP_SERVERS=false

mkdir -p "$OV/logs" "$OV/run"
touch "$OV/STATUS"
deadline=$(( $(date +%s) + MAX_HOURS * 3600 ))
stalls=0

fingerprint() { printf '%s %s' "$(git rev-parse HEAD 2>/dev/null || echo none)" "$(shasum "$OV/PROGRESS.md" | cut -c1-12)"; }

model_args=()
[[ -n $LOOP_MODEL ]] && model_args=(--model "$LOOP_MODEL")

# Job control gives each run its own process group, so a run that overstays
# RUN_MINUTES can be stopped along with the VMs and services it started.
set -m

# run_once LOG: one fresh session, stopped after RUN_MINUTES.
run_once() {
  local log=$1 pid waited=0 rc=0
  # ${a[@]+...}: macOS ships bash 3.2, where an empty array trips set -u.
  claude -p "$(cat "$OV/PROMPT.md")" --dangerously-skip-permissions \
    --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
    ${model_args[@]+"${model_args[@]}"} > "$log" 2>&1 &
  pid=$!
  while sleep 10 && kill -0 "$pid" 2>/dev/null; do
    waited=$((waited + 10))
    if (( waited > RUN_MINUTES * 60 )); then
      echo "[loop] run exceeded ${RUN_MINUTES}m; stopping it" | tee -a "$log"
      kill -TERM -- "-$pid" 2>/dev/null || true
      sleep 15
      kill -KILL -- "-$pid" 2>/dev/null || true
      break
    fi
  done
  wait "$pid" 2>/dev/null || rc=$?
  (( rc == 0 )) || echo "[loop] run exited $rc" | tee -a "$log"
}

run=0
while (( run < MAX_RUNS )); do
  status=$(head -1 "$OV/STATUS" || true)
  case $status in
    DONE*)    echo "[loop] DONE after $run runs"; exit 0 ;;
    BLOCKED*) echo "[loop] $status"; exit 3 ;;
  esac
  if (( $(date +%s) > deadline )); then echo "[loop] time limit reached"; exit 4; fi

  run=$((run + 1))
  before=$(fingerprint)
  started=$(date +%s)
  log="$OV/logs/run-$(printf '%03d' "$run")-$(date +%H%M%S).log"
  echo "[loop] run $run -> $log"
  run_once "$log"

  if [[ $(fingerprint) != "$before" ]]; then
    stalls=0
  elif grep -qiE 'usage limit|hit your limit|rate.?limit|overloaded|resets (at )?[0-9]' "$log" \
       || (( $(date +%s) - started < 120 )); then
    # A usage limit or outage fails every run instantly; four of those would
    # end the night in a minute. Wait it out instead of counting a stall.
    echo "[loop] run $run failed fast or hit a limit; waiting ${BACKOFF_SECONDS}s"
    run=$((run - 1))
    sleep "$BACKOFF_SECONDS"
    continue
  else
    stalls=$((stalls + 1))
    echo "[loop] no progress ($stalls/$STALL_RUNS)"
    if (( stalls >= STALL_RUNS )); then
      echo "STALLED: $STALL_RUNS runs with no commit or PROGRESS.md change, see $log" > "$OV/STATUS"
      exit 5
    fi
  fi
  sleep 5
done
echo "[loop] run limit reached"
exit 6
