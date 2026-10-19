#!/usr/bin/env bash
# attack.sh -- run inside the microsandbox guest, as root. Everything an agent
# with root might try to get around the gateway. Every BLOCK line should be
# blocked; the ALLOW lines prove the gateway itself still works. Exit status
# is the number of failures.
#
# Port of spike/guest/attack.sh. Differences from the QEMU guest: the image
# is node:22-bookworm (no dig, ip, ping, nft, sudo; we are already root), so
# DNS and ICMP probes use python3 sockets; the gateway is
# host.microsandbox.internal (its IP is read from /etc/hosts); there is no
# 10.0.2.x. Enforcement is in the msb process on the host, so there is no
# guest firewall to flush -- we try anyway.
#
# Environment: TETHER_GW (gateway URL), CONTROL_PORT (a host port with a real
# listener, default 18002), HOST_LAN (the host's LAN IPv4, optional).
set -uo pipefail

GW=${TETHER_GW:-http://host.microsandbox.internal:18001}
GW_HOST=host.microsandbox.internal
GW_IP=$(awk -v h="$GW_HOST" '$2 == h {print $1; exit}' /etc/hosts)
GW_PORT=${GW##*:}
GW_PORT=${GW_PORT%%/*}
CONTROL_PORT=${CONTROL_PORT:-18002}
HOST_LAN=${HOST_LAN:-}
RESOLVER=$(awk '$1 == "nameserver" {print $2; exit}' /etc/resolv.conf)
# A real key's shape (as in PROMPT.md's pre-push grep). Only the shape: nono's
# binary, and its shims in /tmp, carry the template string sk-ant-oat01-{}.
export KEY_RE='sk-ant-(api|oat)[0-9]{2}-[A-Za-z0-9_-]{40,}|gho_[A-Za-z0-9]{36}'
fails=0
row() { printf '  %-6s %-5s %s\n' "$1" "$2" "$3"; }

# expect <block|allow> <description> <command...>
expect() {
  local want=$1 what=$2; shift 2
  local got
  # Each attempt carries its own timeout (curl -m, socket timeouts).
  if "$@" >/dev/null 2>&1; then got=allow; else got=block; fi
  if [[ $got == "$want" ]]; then row PASS "$got" "$what"; else row FAIL "$got" "$what"; fails=$((fails + 1)); fi
}

# curl through nothing at all: proxy variables gone, as a hostile agent would have it
# shellcheck disable=SC2329  # invoked through expect
raw() { env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy -u ALL_PROXY curl -sS --noproxy '*' -m 6 "$@"; }

# tcp HOST PORT: succeeds only if a TCP connection completes within 5 s
# shellcheck disable=SC2329
tcp() {
  python3 -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()' "$1" "$2"
}

# dns udp|tcp SERVER NAME: succeeds only if SERVER answers NAME with at least
# one record (an NXDOMAIN or SERVFAIL reply counts as blocked)
# shellcheck disable=SC2329
dns() {
  python3 - "$@" <<'PY'
import socket, struct, sys
proto, server, name = sys.argv[1:4]
q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
if proto == "udp":
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(q, (server, 53))
    r = s.recv(4096)
else:
    s = socket.create_connection((server, 53), timeout=3)
    s.sendall(struct.pack(">H", len(q)) + q)
    r = s.recv(4096)[2:]
rcode, ancount = r[3] & 0xF, struct.unpack(">H", r[6:8])[0]
sys.exit(0 if rcode == 0 and ancount > 0 else 1)
PY
}

# ping HOST: ICMP echo over a raw socket (we are root); succeeds on a reply
# shellcheck disable=SC2329
icmp() {
  python3 - "$1" <<'PY'
import socket, struct, sys
dst = sys.argv[1]
def csum(b):
    s = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    s = (s >> 16) + (s & 0xFFFF)
    return ~(s + (s >> 16)) & 0xFFFF
hdr = struct.pack("!BBHHH", 8, 0, 0, 0x4242, 1) + b"tether"
pkt = hdr[:2] + struct.pack("!H", csum(hdr)) + hdr[4:]
s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
s.settimeout(2)
s.sendto(pkt, (dst, 0))
while True:
    data, addr = s.recvfrom(1024)
    if addr[0] == dst and data[(data[0] & 0xF) * 4] == 0:
        sys.exit(0)
PY
}

# udp HOST PORT: send a datagram and succeed only on any reply
# shellcheck disable=SC2329
udp() {
  python3 -c 'import socket,sys; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2); s.sendto(b"x", (sys.argv[1], int(sys.argv[2]))); s.recv(64)' "$1" "$2"
}

echo
echo "  root in the guest tears down everything it can:"
command -v nft >/dev/null && nft flush ruleset
command -v iptables >/dev/null && { iptables -P OUTPUT ACCEPT; iptables -F; }
command -v ip >/dev/null && ip route replace default via "$GW_IP"
echo "    no nft/iptables/ip in the image; nothing to flush (enforcement is on the host)"
echo "    proxy variables ignored; gateway $GW_HOST is $GW_IP, resolver is $RESOLVER"
echo
row RESULT GOT "ATTEMPT"
expect block "HTTPS to example.com, no proxy"                      raw https://example.com
expect block "HTTPS straight to an IP (1.1.1.1), no proxy"         raw -k https://1.1.1.1
expect block "HTTPS straight to api.anthropic.com, no proxy"       raw https://api.anthropic.com
expect block "HTTPS over IPv6 ([2606:4700:4700::1111]), no proxy"  raw -k -6 'https://[2606:4700:4700::1111]/'
expect block "DNS over UDP to 1.1.1.1"                             dns udp 1.1.1.1 example.com
expect block "DNS over TCP to 1.1.1.1"                             dns tcp 1.1.1.1 example.com
expect block "DNS via the guest's resolver ($RESOLVER)"            dns udp "$RESOLVER" example.com
expect block "DNS via the resolver, over TCP"                      dns tcp "$RESOLVER" example.com
expect block "ping 1.1.1.1 (raw ICMP as root)"                     icmp 1.1.1.1
expect block "ping the gateway ($GW_IP)"                           icmp "$GW_IP"
expect block "UDP to the gateway ($GW_IP:$GW_PORT)"                udp "$GW_IP" "$GW_PORT"
expect block "the host's ssh ($GW_HOST:22)"                        tcp "$GW_HOST" 22
expect block "a live host port ($GW_HOST:$CONTROL_PORT)"           tcp "$GW_HOST" "$CONTROL_PORT"
expect block "a live host port by IP ($GW_IP:$CONTROL_PORT)"       tcp "$GW_IP" "$CONTROL_PORT"
if [[ -n $HOST_LAN ]]; then
  expect block "the host's LAN address ($HOST_LAN:$CONTROL_PORT)"  tcp "$HOST_LAN" "$CONTROL_PORT"
  expect block "the gateway port on the LAN address"               tcp "$HOST_LAN" "$GW_PORT"
fi
expect block "cloud metadata (169.254.169.254:80)"                 tcp 169.254.169.254 80
expect block "via the gateway: example.com (not allowlisted)"      curl -sS -f -m 8 https://example.com
expect block "via the gateway: tunnel to api.anthropic.com"        curl -sS -f -m 8 https://api.anthropic.com/v1/models
expect block "via the gateway, but without the capability"         env -u HTTPS_PROXY -u https_proxy curl -sS -f -m 8 -x "$GW" https://pypi.org/simple/
# shellcheck disable=SC2016 # KEY_RE is expanded by the inner bash
expect block "a real key in any environ, home, /etc, /opt, /tmp or /run" \
  bash -c 'grep -rqsE "$KEY_RE" /proc/[0-9]*/environ /home /root /etc /opt /tmp /var/tmp /run'
expect allow "the gateway answers at $GW"                           bash -c "[ \"\$(curl -s -o /dev/null -m 5 -w '%{http_code}' $GW/)\" != 000 ]"
expect allow "the gateway answers by IP ($GW_IP:$GW_PORT)"          tcp "$GW_IP" "$GW_PORT"
expect allow "via the gateway: pypi.org (allowlisted)"             bash -c "[ \"\$(curl -s -o /dev/null -m 15 -w '%{http_code}' https://pypi.org/simple/)\" = 200 ]"
echo
if [[ $fails -eq 0 ]]; then echo "  all held: the only way out is the gateway"; else echo "  $fails FAILED"; fi
exit "$fails"
