#!/usr/bin/env bash
# nono.sh -- one nono session sandbox per agent, a command policy per tool,
# and caps delivered through the /run/tether tmpfs. Run as guest root.
#
#   nono.sh setup            profiles in /etc/tether/nono, launchers in /opt/tether/bin
#   nono.sh put AGENT NAME   store stdin (a tether env file) as /run/tether/AGENT/NAME
#   nono.sh run AGENT CMD..  run CMD as AGENT inside its session sandbox
#   nono.sh check            prove who holds which cap, and what each can reach
#
# Each agent's session sandbox (Landlock V6, as the agent's UID) gets /work,
# a few dot-directories in its home and TCP connect to the gateway port only.
# The agent's own cap is in the session's environment. A tool's cap is not:
# it sits in /run/tether/<agent>/<tool> (root:<agent> 0440), and nono 0.80's
# `raw-file` command credential grants that one file to that one tool's
# command sandbox. nono does not put a raw-file in the environment, and a
# command sandbox may exec nothing but its pinned executable (a launcher
# script cannot exec the real tool), so each cap is in a format the tool
# reads itself: git gets `-c include.path=<file>` (a url rewrite plus an
# http.extraHeader), npm gets `--userconfig <file>` (its proxy). git's
# remote helper is a second, chained command (git -> git-remote-http) with
# the same file, and GIT_EXEC_PATH points at an empty directory so git finds
# the helper through nono's shim rather than exec'ing it from git-core.
#
# Environment: AGENTS ("claude codex"), CAGE_PORT (18001).
set -euo pipefail

AGENTS=${AGENTS:-claude codex}
PORT=${CAGE_PORT:-18001}
PROFILES=/etc/tether/nono
BIN=/opt/tether/bin
NONO=/opt/nono/nono

log() { printf '[nono] %s\n' "$*"; }

write_launchers() {
  install -d -m 0755 "$BIN"
  # Empty on purpose: git looks for git-remote-http here first, misses, and
  # falls through to PATH, where nono's shim mediates it.
  install -d -m 0755 /opt/tether/git-exec
  cat >"$BIN/agent-session" <<EOF
#!/bin/sh
# Run as an agent: load the agent's own cap into the environment, then enter
# its nono session sandbox. The session cannot read /run/tether again.
a=\$(id -un)
while IFS= read -r l; do case \$l in '' | '#'*) ;; *) export "\$l" ;; esac; done <"/run/tether/\$a/agent"
cd /work
exec $NONO run -s --profile "$PROFILES/\$a.json" -- "\$@"
EOF
  chmod 0755 "$BIN/agent-session"
}

write_profile() { # write_profile AGENT
  python3 - "$1" "$PORT" "$PROFILES/$1.json" <<'EOF'
import json, sys

a, port, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
home = f"/home/{a}"
base_env = ["HOME", "USER", "LOGNAME", "LANG", "TERM", "TZ"]
# git sets these for the git it runs next (clone -> index-pack needs GIT_DIR).
git_env = base_env + [
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX", "GIT_SHALLOW_FILE", "GIT_CONFIG_PARAMETERS", "GIT_PROTOCOL",
]
read = ["/usr", "/etc"]
net = {"tcp_connect_ports": [port]}
git_cap, npm_cap = f"/run/tether/{a}/git", f"/run/tether/{a}/npm"

profile = {
    "meta": {"name": f"tether-{a}", "description": f"tether session sandbox for agent {a}"},
    "filesystem": {
        # Not the whole home: nono refuses a grant over its own state root
        # (~/.local/state/nono), and ~/.config collides with its built-in
        # credential denies (~/.config/gcloud, ...), so grant what the agent uses.
        "allow": ["/work", "/tmp", f"{home}/.claude", f"{home}/.cache", f"{home}/.npm"],
        "allow_file": [f"{home}/.claude.json"],
        "read": ["/usr", "/etc", "/opt/nono", "/opt/tether"],
    },
    "network": {"connect_port": [port]},
    "workdir": {"access": "readwrite"},
    "command_policies": {
        "credentials": {
            "git-cap": {"type": "raw-file", "path": git_cap},
            "npm-cap": {"type": "raw-file", "path": npm_cap},
        },
        "commands": {
            "git": {
                "executable": "/usr/bin/git",
                "can_use": ["git-remote-http"],
                "export_env": ["GIT_*"],
                "sandbox": {
                    "fs_read": read,
                    "fs_write": ["/work", "/tmp"],
                    "use_credentials": ["git-cap"],
                    "argv_prepend": [
                        "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", f"include.path={git_cap}",
                    ],
                    "network": net,
                    "environment": {"allow_vars": git_env, "set_vars": {"GIT_EXEC_PATH": "/opt/tether/git-exec"}},
                },
            },
            "git-remote-http": {
                "executable": "/usr/lib/git-core/git-remote-http",
                "from": {
                    "session": "deny",
                    "git": {
                        "sandbox": {
                            "fs_read": read,
                            "fs_write": ["/work", "/tmp"],
                            "use_credentials": ["git-cap"],
                            "allow_raw_file_credentials_in_chained_policy": True,
                            "network": net,
                        }
                    },
                },
            },
            "npm": {
                # npm is a node script; the command sandbox may exec only this file.
                "executable": "/usr/local/bin/node",
                "sandbox": {
                    "fs_read": read,
                    "fs_write": ["/work", "/tmp", f"{home}/.npm"],
                    "use_credentials": ["npm-cap"],
                    "argv_prepend": ["/usr/local/lib/node_modules/npm/bin/npm-cli.js", "--userconfig", npm_cap],
                    "network": net,
                    "environment": {"allow_vars": base_env},
                },
            },
        },
    },
}
with open(out, "w") as f:
    json.dump(profile, f, indent=2)
EOF
}

cmd_setup() {
  install -d -m 0755 "$PROFILES"
  write_launchers
  chmod 711 /run/tether
  local a d
  for a in $AGENTS; do
    for d in .claude .cache .npm; do install -d -m 0700 -o "$a" -g "$a" "/home/$a/$d"; done
    [[ -f /home/$a/.claude.json ]] || install -m 0600 -o "$a" -g "$a" /dev/null "/home/$a/.claude.json"
    install -d -m 0750 -o root -g "$a" "/run/tether/$a"
    write_profile "$a"
    $NONO profile validate "$PROFILES/$a.json" 2>&1 | grep -E 'Result|error' | sed "s/^/  $a: /"
  done
  log "profiles in $PROFILES, launchers in $BIN"
}

cmd_put() { # stdin -> /run/tether/AGENT/NAME, root:AGENT 0440, written atomically
  local a=$1 name=$2 f tmp
  [[ $name =~ ^[a-z0-9-]+$ ]] || { log "bad cap name $name"; exit 2; }
  install -d -m 0750 -o root -g "$a" "/run/tether/$a"
  f=/run/tether/$a/$name
  tmp=$(mktemp "/run/tether/$a/.$name.XXXXXX")
  cat >"$tmp"
  chown "root:$a" "$tmp"
  chmod 0440 "$tmp"
  mv -f "$tmp" "$f"
  log "$f: $(stat -c '%U:%G %a %s bytes' "$f")"
}

as_agent() { # as_agent AGENT CMD..: a clean login-ish env, the agent's UID and groups
  local a=$1; shift
  setpriv --reuid="$a" --regid="$a" --init-groups --reset-env -- "$@"
}

cmd_run() {
  local a=$1; shift
  as_agent "$a" "$BIN/agent-session" "$@"
}

FAILS=0
sess() { # sess AGENT SCRIPT: SCRIPT in AGENT's session sandbox; stdout+stderr
  as_agent "$1" "$BIN/agent-session" bash -c "$2" 2>&1 || true
}
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); [[ -z ${2:-} ]] || printf '        %s\n' "$2"; }
short() { printf '%s' "$1" | tail -3 | tr '\n' '|'; }
expect() { # expect WHAT WANT GOT
  if [[ $3 == *"$2"* ]]; then pass "$1"; else fail "$1" "want '$2', got: $(short "$3")"; fi
}
expect_not() { # expect_not WHAT WANT BAD GOT: WANT shows the probe ran, BAD must be absent
  if [[ $4 == *"$2"* && $4 != *"$3"* ]]; then pass "$1"; else fail "$1" "want '$2' and no '$3', got: $(short "$4")"; fi
}

cmd_check() {
  local a b out scratch=${SCRATCH_REPO:-metcalfc/tether-scratch} lpid t0 t1
  # shellcheck disable=SC2086 # split the agent list on purpose
  set -- $AGENTS
  a=$1 b=$2

  # A real listener inside the guest, so "refused" cannot mean "nothing there".
  python3 -m http.server 8080 --bind 127.0.0.1 >/dev/null 2>&1 &
  lpid=$!
  sleep 0.5

  log "$a's session sandbox"
  out=$(sess "$a" 'echo landlock-ok; touch /work/.nono-check && echo work-ok')
  expect "session starts under Landlock, writes /work" "work-ok" "$out"
  out=$(sess "$a" 'touch ~/planted 2>&1 || echo denied')
  expect "cannot write the rest of its home" "denied" "$out"
  out=$(sess "$a" "for f in /run/tether/$a/agent /run/tether/$a/git /run/tether/$a/npm; do
    head -c1 \$f >/dev/null 2>&1 && echo read \$f || echo denied \$f; done")
  expect "cannot read its own agent cap file again" "denied /run/tether/$a/agent" "$out"
  expect "cannot read its git cap (Landlock; Unix perms would allow)" "denied /run/tether/$a/git" "$out"
  expect "cannot read its npm cap" "denied /run/tether/$a/npm" "$out"
  out=$(sess "$a" 'env | cut -d= -f1 | sort | tr "\n" " "')
  expect "holds its own cap in the environment" "ANTHROPIC_AUTH_TOKEN" "$out"
  expect_not "no git rewrite in the agent's env" "ANTHROPIC_AUTH_TOKEN" "GIT_CONFIG_KEY_0" "$out"
  out=$(sess "$a" 'python3 -c "import socket; socket.create_connection((\"127.0.0.1\", 8080), 3); print(\"connected\")" 2>&1 | tail -1')
  expect "cannot reach a listener on guest 127.0.0.1:8080" "Permission denied" "$out"
  out=$(sess "$a" "curl -s -o /dev/null -w '%{http_code}' -m 10 --noproxy '*' http://host.microsandbox.internal:$PORT/")
  expect "reaches the gateway on :$PORT" "404" "$out"
  out=$(sess "$a" "curl -s -o /dev/null -w '%{http_code}' -m 15 https://registry.npmjs.org/left-pad 2>&1; echo")
  expect "its own cap cannot CONNECT to registry.npmjs.org (npm's grant, not $a's)" "000" "$out"

  log "git as $a (command policy; the cap is a file only git and its remote helper read)"
  rm -rf /work/nono-git /work/nono-imp
  out=$(sess "$a" "git clone -q --depth 1 https://github.com/$scratch /work/nono-git && git -C /work/nono-git log --oneline -1 && echo clone-ok")
  expect "git clone $scratch through the gateway" "clone-ok" "$out"
  out=$(sess "$a" "git clone -q --depth 1 https://github.com/metcalfc/imp /work/nono-imp 2>&1 || echo refused")
  expect "git clone of an unrouted repo is refused" "refused" "$out"
  out=$(sess "$a" 'git config --get core.hooksPath; git config --get-regexp "^url\." ')
  expect "git runs with core.hooksPath=/dev/null" "/dev/null" "$out"
  expect "git reads its cap file through include.path" "insteadof https://github.com/" "$out"
  # shellcheck disable=SC2016 # $f expands inside the session, not here
  out=$(sess "$a" 'cd /work/nono-git && git config core.pager "touch /tmp/pwned-pager" && git config alias.x "!touch /tmp/pwned-alias" &&
    git config core.fsmonitor "touch /tmp/pwned-fsmonitor" && git config filter.f.clean "touch /tmp/pwned-filter" &&
    echo "* filter=f" >.gitattributes && touch y && git -p log -1 >/dev/null 2>&1; git x 2>&1; git add y 2>&1; git status --short 2>&1;
    for f in /tmp/pwned-*; do [ -e "$f" ] && echo "PLANTED-RAN $f"; done; echo "[ran]"')
  expect_not "planted pager, alias, fsmonitor and filter cannot run (nono exec gate)" "[ran]" "PLANTED-RAN" "$out"
  out=$(sess "$a" "/usr/bin/git --version 2>&1; echo")
  expect "direct /usr/bin/git (bypassing the policy) is refused" "Permission denied" "$out"
  out=$(sess "$a" 'git config --get-regexp extraheader | cut -d" " -f1-3')
  printf '  NOTE  the agent can still print its git cap through git: %s\n' "${out:-nothing}"

  log "npm as $a"
  out=$(sess "$a" 'npm view --fetch-timeout=20000 left-pad version 2>&1 | tail -1')
  expect "npm view left-pad through its own cap" "1.3.0" "$out"
  out=$(sess "$a" 'node /usr/local/lib/node_modules/npm/bin/npm-cli.js view --fetch-timeout=8000 --fetch-retries=0 left-pad version 2>&1 | tail -3 | tr "\n" " "; echo "[ran]"')
  expect_not "interpreter bypass (node npm-cli.js) has no npm cap" "[ran]" "1.3.0" "$out"

  log "$b against $a's caps"
  out=$(as_agent "$b" bash -c "cat /run/tether/$a/git 2>&1; ls /run/tether 2>&1" || true)
  expect "$b cannot read $a's git cap" "Permission denied" "$out"

  log "launch cost (git --version, 10 runs inside $a's session)"
  t0=$(date +%s%N)
  for _ in 1 2 3 4 5 6 7 8 9 10; do /usr/bin/git --version >/dev/null; done
  t1=$(date +%s%N)
  printf '  plain /usr/bin/git:  %d ms per call (root, no nono)\n' $(((t1 - t0) / 10000000))
  t0=$(date +%s%N)
  sess "$a" 'for i in 1 2 3 4 5 6 7 8 9 10; do git --version >/dev/null; done' >/dev/null
  t1=$(date +%s%N)
  printf '  mediated git:        %d ms per call (shim, command sandbox)\n' $(((t1 - t0) / 10000000))
  t0=$(date +%s%N)
  sess "$a" true >/dev/null
  t1=$(date +%s%N)
  printf '  session start:       %d ms (nono run -- true)\n' $(((t1 - t0) / 1000000))

  kill "$lpid" 2>/dev/null || true
  rm -rf /work/nono-git /work/nono-imp /work/.nono-check /tmp/pwned-*
  echo "  $FAILS failed"
  return "$FAILS"
}

case "${1:-}" in
  setup) cmd_setup ;;
  put) shift; cmd_put "$@" ;;
  run) shift; cmd_run "$@" ;;
  check) cmd_check ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
