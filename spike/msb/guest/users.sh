#!/usr/bin/env bash
# users.sh -- per-agent Unix users and a shared /work, run as guest root.
#
#   users.sh setup    create group work, one user per agent, /work and homes
#   users.sh check    prove the sharing and the separation, as each user
#
# Each agent gets its own UID, because the guest kernel has no Yama and
# same-UID isolation would rest on Landlock scoping alone. They share files
# through /work: root:work, mode 2775 (setgid, so new files get group work),
# plus a default ACL g:work:rwX, so a file stays group-writable whatever the
# creating tool's umask. node:22-bookworm ships no setfacl (only libacl1),
# and installing acl would need deb.debian.org on the egress allowlist, so
# the ACLs are written as the raw system.posix_acl_* xattrs from python3.
#
# Environment: AGENTS ("claude codex"), HUMAN (dev).
set -euo pipefail

AGENTS=${AGENTS:-claude codex}
HUMAN=${HUMAN:-dev}
WORK_GID=2000
FIRST_UID=2001

log() { printf '[users] %s\n' "$*"; }

# acl PATH {set|show}: default + access ACL u::rwx g::rwx g:work:rwx m::rwx o::r-x
acl() {
  python3 - "$@" <<'EOF'
import grp, os, struct, sys

TAGS = {1: "user::", 2: "user:", 4: "group::", 8: "group:", 0x10: "mask::", 0x20: "other::"}
NOID = 0xFFFFFFFF


def pack(entries):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", t, p, i) for t, p, i in entries)


def unpack(raw):
    out = []
    for off in range(4, len(raw), 8):
        t, p, i = struct.unpack_from("<HHI", raw, off)
        name = "" if i == NOID else (grp.getgrgid(i).gr_name if t == 8 else str(i))
        perm = "".join(c if p & b else "-" for c, b in (("r", 4), ("w", 2), ("x", 1)))
        out.append(f"{TAGS.get(t, t)}{name}{'' if t in (1, 4, 0x10, 0x20) else ':'}{perm}")
    return ",".join(out)


path, mode = sys.argv[1], sys.argv[2]
if mode == "set":
    work = grp.getgrnam("work").gr_gid
    entries = [(1, 7, NOID), (4, 7, NOID), (8, 7, work), (0x10, 7, NOID), (0x20, 5, NOID)]
    os.setxattr(path, "system.posix_acl_access", pack(entries))
    os.setxattr(path, "system.posix_acl_default", pack(entries))
for name in ("system.posix_acl_access", "system.posix_acl_default"):
    try:
        print(f"  {name.split('.')[-1]:18} {unpack(os.getxattr(path, name))}")
    except OSError:
        print(f"  {name.split('.')[-1]:18} (none)")
EOF
}

cmd_setup() {
  getent group work >/dev/null || groupadd -g "$WORK_GID" work
  local uid=$FIRST_UID a
  for a in $AGENTS; do
    if ! id "$a" >/dev/null 2>&1; then
      useradd -u "$uid" -U -G work -m -s /bin/bash "$a"
    fi
    chmod 700 "/home/$a"
    uid=$((uid + 1))
  done
  # The human: in group work so they can edit what agents write; no sudo here.
  id "$HUMAN" >/dev/null 2>&1 || useradd -u 1500 -U -G work -m -s /bin/bash "$HUMAN"
  chmod 700 "/home/$HUMAN"

  mkdir -p /work
  chown root:work /work
  chmod 2775 /work
  acl /work set >/dev/null

  # Caps live in /run/tether/<agent>/ (nono.sh). 0711: an agent may pass
  # through to its own directory, where nono's supervisor (running as the
  # agent) reads a tool's cap, but may not list or write the parent.
  mkdir -p /run/tether
  chown root:root /run/tether
  chmod 711 /run/tether
  log "users: $(for a in $AGENTS $HUMAN; do printf '%s ' "$(id "$a")"; done)"
  log "/work: $(stat -c '%U:%G %a' /work)"
  acl /work show
  log "/run/tether: $(stat -c '%U:%G %a' /run/tether) ($(findmnt -no FSTYPE /run/tether 2>/dev/null || echo 'not a mount'))"
}

FAILS=0
as() { local u=$1; shift; setpriv --reuid="$u" --regid="$u" --init-groups --reset-env -- bash -c "umask 022; $*"; }
ok() { # ok WHAT USER CMD: CMD must succeed
  if as "$2" "$3" >/dev/null 2>&1; then printf '  PASS  %s\n' "$1"; else printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); fi
}
no() { # no WHAT USER CMD: CMD must fail
  if as "$2" "$3" >/dev/null 2>&1; then printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); else printf '  PASS  %s\n' "$1"; fi
}

cmd_check() {
  local a b
  # shellcheck disable=SC2086 # split the agent list on purpose
  set -- $AGENTS
  a=$1 b=$2
  rm -rf /work/.users-check /work-noacl
  echo "root-only probe" >/run/tether/probe; chmod 600 /run/tether/probe
  echo "secret of $a" >"/home/$a/secret"; chown "$a:$a" "/home/$a/secret"
  echo "secret of $b" >"/home/$b/secret"; chown "$b:$b" "/home/$b/secret"

  log "sharing through /work (every write under umask 022)"
  ok "$a creates /work/.users-check/x/f" "$a" "mkdir -p /work/.users-check/x && echo one >/work/.users-check/x/f"
  ok "$b appends to $a's file" "$b" "echo two >>/work/.users-check/x/f"
  ok "$b creates a file in $a's directory" "$b" "echo three >/work/.users-check/x/g"
  ok "$a edits $b's file" "$a" "echo four >>/work/.users-check/x/g"
  ok "$a renames $b's file" "$a" "mv /work/.users-check/x/g /work/.users-check/x/h"
  ok "$HUMAN edits $a's file" "$HUMAN" "echo five >>/work/.users-check/x/f"
  ok "new files have group work" "$a" "test \$(stat -c %G /work/.users-check/x/f) = work"
  ok "new dirs inherit setgid" "$a" "test -g /work/.users-check/x"
  stat -c '    %A %U:%G %n' /work/.users-check/x /work/.users-check/x/f /work/.users-check/x/h

  log "separation"
  no "$a cannot read $b's home" "$a" "cat /home/$b/secret"
  no "$a cannot list $b's home" "$a" "ls /home/$b"
  no "$b cannot read $a's home" "$b" "cat /home/$a/secret"
  no "$b cannot write into $a's home" "$b" "touch /home/$a/planted"
  no "$a cannot list /run/tether" "$a" "ls /run/tether"
  no "$a cannot read /run/tether/probe" "$a" "cat /run/tether/probe"
  no "$b cannot read /run/tether/probe" "$b" "cat /run/tether/probe"
  no "$a cannot write /run/tether" "$a" "touch /run/tether/x"

  log "control: setgid without the default ACL, same umask"
  mkdir -p /work-noacl && chown root:work /work-noacl && chmod 2775 /work-noacl
  ok "$a creates /work-noacl/f" "$a" "echo one >/work-noacl/f"
  no "$b cannot append to it (umask 022 wins without the ACL)" "$b" "echo two >>/work-noacl/f"
  rm -rf /work-noacl /work/.users-check /run/tether/probe "/home/$a/secret" "/home/$b/secret"

  echo "  $FAILS failed"
  return "$FAILS"
}

case "${1:-}" in
  setup) cmd_setup ;;
  check) cmd_check ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
