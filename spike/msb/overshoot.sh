#!/bin/bash
# Four concurrent streams against a spend cap, with reservations on and off.
# Runs a live tether on the host (the spend path does not depend on the
# cage), real haiku calls, a fresh sub-capped cap per scenario. Costs about
# $0.015. Output and state go to overnight/run/overshoot/.
#
#   spike/msb/overshoot.sh
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
T=$ROOT/tether
OUT=$ROOT/overnight/run/overshoot
PORT=18141
GW=http://127.0.0.1:$PORT
rm -rf "$OUT"
mkdir -p "$OUT"

policy() { # reserve(true|false) file
  cat > "$2" <<EOF
{
  "state_dir": "state-$1",
  "sandboxes": {"ov": {"listen": "127.0.0.1:$PORT"}},
  "routes": {
    "anthropic": {"upstream": "https://api.anthropic.com",
                  "credential": "cmd:security find-generic-password -s tether-anthropic -w",
                  "inject": "anthropic", "paths": ["/v1/messages"], "meter": true}
  },
  "egress": {"allow": [], "ports": [443]},
  "spend": {"per_session_usd_per_day": 1.0, "max_inflight": 8, "reserve": $1},
  "prices_per_mtok": {"claude-haiku-4-5": [1, 5], "default": [5, 25]}
}
EOF
}

serve_up() { # policy
  "$T" -c "$1" serve > "$OUT/serve.log" 2>&1 &
  echo $! > "$OUT/serve.pid"
  local i=0
  until curl -s -o /dev/null --max-time 1 "$GW/"; do
    i=$((i + 1))
    if [ "$i" -gt 30 ]; then echo "tether did not come up" >&2; cat "$OUT/serve.log" >&2; exit 1; fi
    sleep 0.2
  done
}

serve_down() {
  if [ -f "$OUT/serve.pid" ]; then
    kill "$(cat "$OUT/serve.pid")" 2>/dev/null || true
    wait "$(cat "$OUT/serve.pid")" 2>/dev/null || true
    rm -f "$OUT/serve.pid"
  fi
}
trap serve_down EXIT

msg() { # cap max_tokens prompt outfile -> prints the status code
  local body
  body=$(printf '{"model":"claude-haiku-4-5","max_tokens":%s,"stream":true,"messages":[{"role":"user","content":"%s"}]}' "$2" "$3")
  curl -s -o "$4" -w '%{http_code}' --max-time 90 "$GW/anthropic/v1/messages" \
    -H "authorization: Bearer $1" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -d "$body" || true
}

four() { # label cap max_tokens prompt: four at once, then one more after they settle
  local i pids=""
  for i in 1 2 3 4; do
    msg "$2" "$3" "$4" "$OUT/$1-$i.txt" > "$OUT/$1-$i.code" &
    pids="$pids $!"
  done
  # shellcheck disable=SC2086
  wait $pids
  msg "$2" "$3" "$4" "$OUT/$1-next.txt" > "$OUT/$1-next.code"
  printf '%-14s four: %s %s %s %s  next: %s\n' "$1" \
    "$(cat "$OUT/$1-1.code")" "$(cat "$OUT/$1-2.code")" "$(cat "$OUT/$1-3.code")" "$(cat "$OUT/$1-4.code")" \
    "$(cat "$OUT/$1-next.code")"
}

LONG="Count from 1 to 300, one number per line."
SHORT="Say hi."
CAP=0.005 # fits two max_tokens-400 reservations ($0.002035 each), not three

for mode in true false; do
  P=$OUT/policy-$mode.json
  policy "$mode" "$P"
  serve_up "$P"
  echo "=== reserve: $mode"
  four "long-$mode" "$("$T" -c "$P" cap mint --session ov --agent claude --spend $CAP 2>/dev/null)" 400 "$LONG"
  # Claude Code's shape: a large max_tokens on a call that costs little
  four "short-$mode" "$("$T" -c "$P" cap mint --session ov --agent claude --spend 0.05 2>/dev/null)" 4096 "$SHORT"
  sleep 0.5
  serve_down
done

python3 - "$OUT" <<'EOF'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
caps = {"long": 0.005, "short": 0.05}
for mode in ("true", "false"):
    recs = [json.loads(line) for line in (out / f"state-{mode}" / "audit.jsonl").open()]
    api = [r for r in recs if r.get("kind") == "api" and r.get("route") == "anthropic"]
    for order, (label, cap) in enumerate(caps.items()):
        # caps were minted in order, so the nth distinct cap_id is the nth scenario
        ids = list(dict.fromkeys(r["cap_id"] for r in api))
        mine = [r for r in api if r["cap_id"] == ids[order]]
        allowed = [r for r in mine if r["decision"] == "allow"]
        spent = sum(r.get("usd", 0) for r in allowed)
        reserve = max((r.get("reserve_usd") or 0) for r in mine)
        est = [r.get("reserve_usd") for r in allowed]
        print(
            f"reserve={mode:5} {label:5} cap ${cap:.4f}: allowed {len(allowed)}/{len(mine)}"
            f" spent ${spent:.6f} overshoot ${spent - cap:+.6f}"
            f" (max reserve_usd {reserve:.6f}; reserved {est}; usd {[r.get('usd') for r in allowed]})"
        )
        for r in mine:
            if r["decision"] == "deny":
                print("   deny", r["rule"], (r.get("reason") or "")[:120])
EOF
