"""End-to-end tests against fake upstreams on loopback. No network, no keys.

python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import http.client
import http.server
import importlib.machinery
import importlib.util
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("tether", str(HERE / "tether"))
spec = importlib.util.spec_from_loader("tether", loader)
assert spec
tether = importlib.util.module_from_spec(spec)
loader.exec_module(tether)

REAL_KEY = "sk-ant-api03-REAL-SECRET"
GH_TOKEN = "ghp_REAL_TOKEN"


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    """Plays api.anthropic.com and github.com. Records what it was sent."""

    protocol_version = "HTTP/1.1"
    seen: list[dict[str, Any]] = []

    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        FakeUpstream.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if body.get("stream"):
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "model": "claude-sonnet-4-5",
                        "usage": {"input_tokens": 1000, "cache_read_input_tokens": 10000, "output_tokens": 1},
                    },
                },
                {"type": "content_block_delta", "delta": {"text": "hi"}},
                {"type": "message_delta", "usage": {"output_tokens": 500}},
                {"type": "message_stop"},
            ]
            out = b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)
            ctype = "text/event-stream"
        else:
            out = json.dumps(
                {"model": "claude-opus-4-1", "usage": {"input_tokens": 100000, "output_tokens": 20000}}
            ).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> None:
        FakeUpstream.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        out = b"001e# service=git-upload-pack\n0000"
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def serve(handler: type, port: int = 0) -> http.server.HTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def echo_server() -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()

    def run() -> None:
        while True:
            c, _ = s.accept()
            data = c.recv(1024)
            c.sendall(b"echo:" + data)
            c.close()

    threading.Thread(target=run, daemon=True).start()
    return s


class TetherTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeUpstream.seen = []
        self.up = serve(FakeUpstream)
        self.echo = echo_server()
        self.echo_port = self.echo.getsockname()[1]
        self.tmp = tempfile.TemporaryDirectory()
        up = f"http://127.0.0.1:{self.up.server_address[1]}"
        cfg = {
            "listen": "127.0.0.1:0",
            "advertise": "http://tether:8080",
            "sessions": ["alice", "bob"],
            "routes": {
                "anthropic": {
                    "upstream": up,
                    "credential": "env:T_ANTHROPIC",
                    "inject": "anthropic",
                    "paths": ["/v1/messages"],
                    "meter": True,
                },
                "github": {
                    "upstream": up,
                    "credential": "env:T_GH",
                    "inject": "basic:x-access-token",
                    "paths": ["/*/info/refs"],
                },
                "openai": {"upstream": up, "credential": "env:T_UNSET", "inject": "bearer"},
            },
            "egress": {"allow": ["localhost"], "ports": [self.echo_port, 443]},
            "spend": {"per_session_usd_per_day": 1.0},
            "prices_per_mtok": {"claude-opus": [15, 75], "claude-sonnet": [3, 15], "default": [1, 1]},
        }
        os.environ["T_ANTHROPIC"] = REAL_KEY
        os.environ["T_GH"] = GH_TOKEN
        p = Path(self.tmp.name) / "policy.json"
        p.write_text(json.dumps(cfg))
        self.policy = tether.Policy(p)
        self.srv, self.gw = tether.build_server(self.policy)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.caps = {name: cap for cap, name in self.gw.sessions.items()}

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.up.shutdown()
        self.up.server_close()
        self.echo.close()
        self.tmp.cleanup()

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json", **headers})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out

    def audit(self) -> list[dict[str, Any]]:
        f = self.policy.state_dir / "audit.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    # --- credentials

    def test_capability_swapped_for_real_key(self) -> None:
        st, _ = self.post("/anthropic/v1/messages", {"stream": True}, {"Authorization": "Bearer " + self.caps["alice"]})
        self.assertEqual(st, 200)
        h = FakeUpstream.seen[-1]["headers"]
        self.assertEqual(h["x-api-key"], REAL_KEY)
        self.assertNotIn("authorization", h)
        self.assertEqual(FakeUpstream.seen[-1]["path"], "/v1/messages")

    def test_oauth_token_gets_bearer_and_beta(self) -> None:
        self.gw.p.routes["anthropic"].credential = "sk-ant-oat01-MAX"
        self.post("/anthropic/v1/messages", {}, {"x-api-key": self.caps["alice"], "anthropic-beta": "foo"})
        h = FakeUpstream.seen[-1]["headers"]
        self.assertEqual(h["authorization"], "Bearer sk-ant-oat01-MAX")
        self.assertIn("oauth-2025-04-20", h["anthropic-beta"])
        self.assertIn("foo", h["anthropic-beta"])

    def test_unknown_capability_refused_before_upstream(self) -> None:
        st, body = self.post("/anthropic/v1/messages", {}, {"Authorization": "Bearer " + REAL_KEY})
        self.assertEqual(st, 401)
        self.assertEqual(FakeUpstream.seen, [])
        self.assertIn("capability", json.loads(body)["error"]["message"])

    def test_path_allowlist(self) -> None:
        st, _ = self.post("/anthropic/v1/organizations/me", {}, {"Authorization": "Bearer " + self.caps["alice"]})
        self.assertEqual(st, 403)
        st, _ = self.post("/anthropic/v1/../v1/messages/../../admin", {}, {})
        self.assertEqual(st, 400)
        self.assertEqual(FakeUpstream.seen, [])

    def test_route_without_credential_is_disabled(self) -> None:
        self.assertIn("openai", self.policy.disabled_routes)
        st, _ = self.post("/openai/v1/responses", {}, {"Authorization": "Bearer " + self.caps["alice"]})
        self.assertEqual(st, 404)

    def test_git_basic_auth_challenge_then_inject(self) -> None:
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/github/acme/app.git/info/refs?service=git-upload-pack")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 401)
        self.assertIn("Basic", r.getheader("WWW-Authenticate") or "")
        c.close()
        auth = base64.b64encode(f"agent:{self.caps['bob']}".encode()).decode()
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request(
            "GET", "/github/acme/app.git/info/refs?service=git-upload-pack", headers={"Authorization": "Basic " + auth}
        )
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn(b"git-upload-pack", r.read())
        sent = FakeUpstream.seen[-1]["headers"]["authorization"]
        self.assertEqual(base64.b64decode(sent[6:]).decode(), f"x-access-token:{GH_TOKEN}")

    # --- spend

    def test_streaming_usage_is_metered(self) -> None:
        self.post("/anthropic/v1/messages", {"stream": True}, {"Authorization": "Bearer " + self.caps["alice"]})
        rec = [r for r in self.audit() if r.get("kind") == "api"][-1]
        self.assertEqual(rec["model"], "claude-sonnet-4-5")
        self.assertEqual(rec["tokens"], {"in": 1000, "cr": 10000, "out": 500})
        # 1000*3 + 10000*3*0.1 + 500*15 = 13500 per M
        self.assertAlmostEqual(rec["usd"], 0.0135, places=6)

    def test_cap_refuses_next_call_per_session(self) -> None:
        h = {"Authorization": "Bearer " + self.caps["alice"]}
        # opus non-stream: 100k*15 + 20k*75 = 3.0M/M -> $3.00, over the $1 cap
        st, _ = self.post("/anthropic/v1/messages", {}, h)
        self.assertEqual(st, 200)
        st, body = self.post("/anthropic/v1/messages", {}, h)
        self.assertEqual(st, 403)
        self.assertIn("spend cap", json.loads(body)["error"]["message"])
        # bob has his own budget
        st, _ = self.post("/anthropic/v1/messages", {}, {"Authorization": "Bearer " + self.caps["bob"]})
        self.assertEqual(st, 200)
        # and the cap survives a restart
        self.assertGreaterEqual(tether.Ledger(self.policy.state_dir / "spend.json").totals("alice")[0], 3.0)

    # --- egress

    def connect(self, target: str, cap: str | None = None) -> tuple[bytes, socket.socket]:
        s = socket.create_connection(("127.0.0.1", self.port))
        req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if cap:
            req += "Proxy-Authorization: Basic " + base64.b64encode(f"agent:{cap}".encode()).decode() + "\r\n"
        s.sendall((req + "\r\n").encode())
        return s.recv(4096), s

    def test_egress_allowlisted_host_tunnels(self) -> None:
        head, s = self.connect(f"localhost:{self.echo_port}", self.caps["alice"])
        self.assertIn(b"200", head.split(b"\r\n")[0])
        s.sendall(b"ping")
        self.assertEqual(s.recv(100), b"echo:ping")
        s.close()

    def test_egress_needs_a_capability(self) -> None:
        # Found in the VM spike: an allowlisted host was reachable with no capability at all.
        head, s = self.connect(f"localhost:{self.echo_port}")
        s.close()
        self.assertIn(b"407", head.split(b"\r\n")[0])
        self.assertIn(b"Proxy-Authenticate", head)
        head, s = self.connect(f"localhost:{self.echo_port}", "tth_not-a-real-capability")
        s.close()
        self.assertIn(b"407", head.split(b"\r\n")[0])

    def test_egress_denied_host_and_port(self) -> None:
        head, s = self.connect("evil.example:443")
        s.close()
        self.assertIn(b"403", head.split(b"\r\n")[0])
        head, s = self.connect("localhost:22")
        s.close()
        self.assertIn(b"403", head.split(b"\r\n")[0])
        # the fake model API lives on 127.0.0.1: a credentialed host is never a tunnel
        head, s = self.connect(f"127.0.0.1:{self.echo_port}")
        s.close()
        self.assertIn(b"403", head.split(b"\r\n")[0])
        reasons = [r["reason"] for r in self.audit() if r.get("decision") == "deny"]
        self.assertTrue(any("allowlist" in x for x in reasons))
        self.assertTrue(any("port 22" in x for x in reasons))
        self.assertTrue(any("credentialed host" in x for x in reasons))

    def test_credentialed_host_cannot_be_tunnelled(self) -> None:
        self.gw.p.egress_allow.append("api.anthropic.com")
        self.gw.cred_hosts["api.anthropic.com"] = "anthropic"
        self.gw.p.egress_ports.add(443)
        head, s = self.connect("api.anthropic.com:443")
        s.close()
        self.assertIn(b"403", head.split(b"\r\n")[0])

    # --- env

    def test_env_file_holds_no_credentials(self) -> None:
        files = tether.write_env_files(self.policy, self.gw.sessions)
        text = "".join(f.read_text() for f in files)
        self.assertNotIn(REAL_KEY, text)
        self.assertNotIn(GH_TOKEN, text)
        self.assertIn("ANTHROPIC_BASE_URL=http://tether:8080/anthropic", text)
        self.assertIn("insteadOf", text)
        self.assertEqual(oct(files[0].stat().st_mode & 0o777), "0o600")

    def test_tether_names(self) -> None:
        # Caps, env and the default state dir carry tether's name, not leash's.
        self.assertTrue(all(c.startswith("tth_") for c in self.caps.values()))
        env = tether.env_for(self.policy, self.caps["alice"])
        self.assertEqual(env["TETHER_CAPABILITY"], self.caps["alice"])
        self.assertNotIn("LEASH_CAPABILITY", env)
        p = Path(self.tmp.name) / "bare.json"
        p.write_text("{}")
        self.assertEqual(tether.Policy(p).state_dir, (Path(self.tmp.name) / "run" / "tether").resolve())

    def test_report_runs(self) -> None:
        self.post("/anthropic/v1/messages", {"stream": True}, {"Authorization": "Bearer " + self.caps["alice"]})
        self.connect("evil.example:443")[1].close()
        tether.cmd_report(self.policy, as_json=False)


if __name__ == "__main__":
    unittest.main()
