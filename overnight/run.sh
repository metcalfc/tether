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
LOOP_MODEL=${LOOP_MODEL:-}

# The loop's own Claude Code runs on your subscription. A stray API key in the
# environment would silently switch it to billing that key instead.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL

mkdir -p "$OV/logs" "$OV/run"
touch "$OV/STATUS"
deadline=$(( $(date +%s) + MAX_HOURS * 3600 ))
stalls=0

fingerprint() { printf '%s %s' "$(git rev-parse HEAD 2>/dev/null || echo none)" "$(shasum "$OV/PROGRESS.md" | cut -c1-12)"; }

model_args=()
[[ -n $LOOP_MODEL ]] && model_args=(--model "$LOOP_MODEL")

for run in $(seq 1 "$MAX_RUNS"); do
  status=$(head -1 "$OV/STATUS" || true)
  case $status in
    DONE*)    echo "[loop] DONE after $((run - 1)) runs"; exit 0 ;;
    BLOCKED*) echo "[loop] $status"; exit 3 ;;
  esac
  if (( $(date +%s) > deadline )); then echo "[loop] time limit reached"; exit 4; fi

  before=$(fingerprint)
  log="$OV/logs/run-$(printf '%03d' "$run")-$(date +%H%M).log"
  echo "[loop] run $run -> $log"
  # One run = one fresh session. A crash or context limit just ends the run.
  # ${a[@]+...}: macOS ships bash 3.2, where an empty array trips set -u.
  claude -p "$(cat "$OV/PROMPT.md")" --dangerously-skip-permissions ${model_args[@]+"${model_args[@]}"} \
    > "$log" 2>&1 || echo "[loop] run $run exited $?" | tee -a "$log"

  if [[ $(fingerprint) == "$before" ]]; then
    stalls=$((stalls + 1))
    echo "[loop] no progress ($stalls/$STALL_RUNS)"
    if (( stalls >= STALL_RUNS )); then
      echo "STALLED: $STALL_RUNS runs with no commit or PROGRESS.md change, see $log" > "$OV/STATUS"
      exit 5
    fi
  else
    stalls=0
  fi
  sleep 5
done
echo "[loop] run limit reached"
exit 6
