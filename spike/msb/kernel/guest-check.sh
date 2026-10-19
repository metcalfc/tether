#!/bin/bash
# Runs inside the guest (as root) from check.sh: is Landlock there, which ABI,
# and does a Landlock TCP connect rule actually hold?
set -uo pipefail
NONO=/opt/nono/nono

echo "uname: $(uname -r)"
mountpoint -q /sys/kernel/security || mount -t securityfs securityfs /sys/kernel/security 2>/dev/null
echo "lsm: $(cat /sys/kernel/security/lsm 2>&1)"

# landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION); 444 on aarch64.
python3 - <<'PY'
import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
abi = libc.syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
print("landlock_abi:", abi if abi >= 0 else "error " + os.strerror(ctypes.get_errno()))
PY

echo "--- nono setup --check-only"
"$NONO" setup --check-only 2>&1 | sed 's/^/  /'

# A listener inside the guest on a port nono does not allow.
python3 -m http.server 8080 --bind 127.0.0.1 >/dev/null 2>&1 &
srv=$!
cd /tmp || exit 1
sleep 1
probe='import socket,sys
s=socket.socket(); s.settimeout(3)
try:
    s.connect(("127.0.0.1", int(sys.argv[1]))); print("connect", sys.argv[1], "ok")
except OSError as e:
    print("connect", sys.argv[1], "refused:", e)'
echo "--- no sandbox"
python3 -c "$probe" 8080
echo "--- nono run --allow-connect-port 18001 (8080 should be refused)"
"$NONO" run -s --allow-cwd --allow-connect-port 18001 -- python3 -c "$probe" 8080 2>&1 | sed 's/^/  /'
echo "--- nono run --allow-connect-port 8080 (8080 should connect)"
"$NONO" run -s --allow-cwd --allow-connect-port 8080 -- python3 -c "$probe" 8080 2>&1 | sed 's/^/  /'
kill "$srv" 2>/dev/null
