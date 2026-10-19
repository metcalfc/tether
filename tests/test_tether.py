"""End-to-end tests against fake upstreams on loopback. No network, no keys.

python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import queue
import socket
import ssl
import tempfile
import threading
import time
import tracemalloc
import types
import unittest
import urllib.parse
from collections.abc import Callable
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
SSE_PING = b"event: ping\ndata: {\"type\": \"ping\"}\n\n"
ERROR_BODY = {
    "type": "error",
    "error": {"type": "overloaded_error", "message": "Overloaded"},
    "request_id": "req_upstream_err",
}


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
        if body.get("fail"):
            out = json.dumps(ERROR_BODY).encode()
            self.send_response(body["fail"])
            self.send_header("Content-Type", "application/json")
            self.send_header("retry-after", "7")
            self.send_header("anthropic-ratelimit-tokens-remaining", "0")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if self.path.split("?")[0].endswith("/count_tokens"):
            out = json.dumps({"input_tokens": 42}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
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
            out = SSE_PING + b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)
            ctype = "text/event-stream"
        else:
            out = json.dumps(
                {"model": "claude-opus-4-1", "usage": {"input_tokens": 100000, "output_tokens": 20000}}
            ).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("request-id", "req_upstream_123")
        self.send_header("x-tether-request-id", "spoofed-by-upstream")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> None:
        FakeUpstream.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.path.startswith("/v1/models"):
            out = json.dumps({"data": [{"id": "claude-haiku-4-5", "type": "model"}], "has_more": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        out = b"001e# service=git-upload-pack\n0000"
        self.send_response(200)
        self.send_header("x-github-request-id", "C0FF:EE:1234")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Sink(http.server.BaseHTTPRequestHandler):
    """Plays github.com's git-receive-pack: reads a body as it arrives, never holds it."""

    protocol_version = "HTTP/1.1"
    got: list[dict[str, Any]] = []

    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        h = hashlib.sha256()
        n = 0
        te = self.headers.get("Transfer-Encoding")
        try:
            if te == "chunked":
                while size := int(self.rfile.readline().split(b";")[0], 16):
                    piece = self.rfile.read(size)
                    h.update(piece)
                    n += len(piece)
                    self.rfile.readline()
                self.rfile.readline()
            else:
                left = int(self.headers.get("Content-Length") or 0)
                while left:
                    piece = self.rfile.read(min(left, 65536))
                    if not piece:
                        raise ConnectionError("short body")
                    h.update(piece)
                    n += len(piece)
                    left -= len(piece)
        except (ConnectionError, ValueError) as e:
            Sink.got.append({"bytes": n, "error": str(e)})
            self.close_connection = True
            return
        Sink.got.append({"bytes": n, "sha256": h.hexdigest(), "te": te, "cl": self.headers.get("Content-Length")})
        out = json.dumps(Sink.got[-1]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class HoldUpstream(http.server.BaseHTTPRequestHandler):
    """Plays a model API whose stream stays open until the test lets it go:
    message_start, then wait, then either finish with usage or abort mid-stream."""

    protocol_version = "HTTP/1.1"
    started: queue.Queue[threading.Event] = queue.Queue()
    mode = "finish"
    posts = 0

    def log_message(self, *a: Any) -> None:
        pass

    def event(self, e: dict[str, Any]) -> None:
        data = b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n"
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        HoldUpstream.posts += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        usage = {"input_tokens": 10, "output_tokens": 1}
        self.event({"type": "message_start", "message": {"model": "claude-opus-4-1", "usage": usage}})
        go = threading.Event()
        HoldUpstream.started.put(go)
        go.wait(10)
        if HoldUpstream.mode == "abort":
            self.event({"type": "content_block_delta", "delta": {"text": "x" * 40000}})
            self.close_connection = True  # no terminating chunk: the stream is cut short
            return
        self.event({"type": "message_delta", "usage": {"output_tokens": 100}})
        self.event({"type": "message_stop"})
        self.wfile.write(b"0\r\n\r\n")


class WsUpstream:
    """Plays a WebSocket server on a raw socket: answers 101 and echoes bytes, or
    refuses with `refuse` as the status. Records each request head it was sent."""

    def __init__(self, refuse: int | None = None) -> None:
        self.refuse = refuse
        self.heads: list[dict[str, str]] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(c,), daemon=True).start()

    def one(self, c: socket.socket) -> None:
        with c:
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = c.recv(65536)
                if not data:
                    return
                buf += data
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            h = {"_line": lines[0]}
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                h[k.strip().lower()] = v.strip()
            self.heads.append(h)
            if self.refuse:
                body = b"upgrade refused"
                c.sendall(
                    f"HTTP/1.1 {self.refuse} Nope\r\nContent-Length: {len(body)}\r\nX-Up: 1\r\n\r\n".encode() + body
                )
                return
            accept = base64.b64encode(
                hashlib.sha1((h["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            c.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
                + b"hello-from-server"
            )
            if rest:
                c.sendall(rest)
            while data := c.recv(65536):
                c.sendall(data)

    def close(self) -> None:
        self.sock.close()


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


def tls_hello(host: str | None) -> bytes:
    """Real ClientHello bytes from the ssl module, as a guest's client sends them."""
    ctx = ssl.create_default_context()
    if host is None:
        ctx.check_hostname = False
    out = ssl.MemoryBIO()
    obj = ctx.wrap_bio(ssl.MemoryBIO(), out, server_hostname=host)
    with contextlib.suppress(ssl.SSLWantReadError):
        obj.do_handshake()
    return out.read()


def handshake(exts: bytes) -> bytes:
    """A minimal hand-built ClientHello handshake message with these extensions."""
    body = b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
    body += len(exts).to_bytes(2, "big") + exts
    return b"\x01" + len(body).to_bytes(3, "big") + body


def ext(etype: int, data: bytes) -> bytes:
    return etype.to_bytes(2, "big") + len(data).to_bytes(2, "big") + data


def sni_ext(name: bytes) -> bytes:
    entry = b"\x00" + len(name).to_bytes(2, "big") + name
    return ext(0, len(entry).to_bytes(2, "big") + entry)


LOOPBACK = ["127.0.0.1/32", "::1/128"]


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
                    "paths": ["/v1/messages", "/v1/messages/count_tokens", "/v1/models", "/v1/models/*"],
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
            "egress": {"allow": ["localhost"], "ports": [self.echo_port, 443], "allow_addresses": LOOPBACK},
            "spend": {"per_session_usd_per_day": 1.0},
            "prices_per_mtok": {"claude-opus": [15, 75], "claude-sonnet": [3, 15], "default": [1, 1]},
        }
        os.environ["T_ANTHROPIC"] = REAL_KEY
        os.environ["T_GH"] = GH_TOKEN
        p = Path(self.tmp.name) / "policy.json"
        p.write_text(json.dumps(cfg))
        self.policy = tether.Policy(p)
        srvs, self.gw = tether.build_servers(self.policy)
        self.srv = srvs["default"]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.caps = tether.init_caps(self.policy, self.gw.caps)

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

    def test_https_upstreams_share_one_verifying_tls_context(self) -> None:
        ctx = tether.tls_context()
        self.assertIs(ctx, tether.tls_context())
        self.assertEqual((ctx.verify_mode, ctx.check_hostname), (ssl.CERT_REQUIRED, True))
        seen: list[Any] = []
        up_port = self.up.server_address[1]

        class Recording(http.client.HTTPConnection):  # plain HTTP to the fake, records the context it was given
            def __init__(self, host: str, port: int | None = None, *, timeout: float = 10, context: Any = None) -> None:
                seen.append((host, context))
                super().__init__("127.0.0.1", up_port, timeout=timeout)

        self.gw.p.routes["anthropic"].upstream = urllib.parse.urlsplit("https://api.example.test")
        orig = http.client.HTTPSConnection
        http.client.HTTPSConnection = Recording  # type: ignore[misc]
        try:
            auth = {"Authorization": "Bearer " + self.caps["alice"]}
            for _ in range(2):
                st, _ = self.post("/anthropic/v1/messages/count_tokens", {}, auth)
                self.assertEqual(st, 200)
        finally:
            http.client.HTTPSConnection = orig  # type: ignore[misc]
        self.assertEqual(seen, [("api.example.test", ctx), ("api.example.test", ctx)])

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

    # --- Claude Code conformance (design section 9)

    def request(self, method: str, path: str, headers: dict[str, str]) -> tuple[http.client.HTTPResponse, bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request(method, path, headers=headers)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r, out

    def test_anthropic_headers_and_beta_query_forwarded_verbatim(self) -> None:
        h = {
            "x-api-key": self.caps["alice"],
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14,context-1m-2025-08-07",
            "anthropic-dangerous-direct-browser-access": "true",
            "x-stainless-retry-count": "0",
            "user-agent": "claude-cli/2.1.303 (external, cli)",
        }
        st, _ = self.post("/anthropic/v1/messages?beta=true", {"stream": True}, h)
        self.assertEqual(st, 200)
        seen = FakeUpstream.seen[-1]
        self.assertEqual(seen["path"], "/v1/messages?beta=true")
        for k, v in h.items():
            if k != "x-api-key":
                self.assertEqual(seen["headers"][k], v, k)
        self.assertEqual(seen["headers"]["x-api-key"], REAL_KEY)

    def test_sse_relayed_byte_for_byte_pings_included(self) -> None:
        h = {"Authorization": "Bearer " + self.caps["alice"]}
        st, out = self.post("/anthropic/v1/messages", {"stream": True}, h)
        self.assertEqual(st, 200)
        self.assertTrue(out.startswith(SSE_PING), out[:80])
        self.assertIn(b'"message_stop"', out)
        # metered as before: the ping is skipped, not miscounted
        rec = [r for r in self.audit() if r.get("kind") == "api"][-1]
        self.assertEqual(rec["tokens"], {"in": 1000, "cr": 10000, "out": 500})

    def test_count_tokens_and_models_allowed_and_free(self) -> None:
        h = {"Authorization": "Bearer " + self.caps["alice"]}
        st, out = self.post("/anthropic/v1/messages/count_tokens", {"model": "claude-opus-4-1", "messages": []}, h)
        self.assertEqual((st, json.loads(out)), (200, {"input_tokens": 42}))
        r, out = self.request("GET", "/anthropic/v1/models?limit=1000", h)
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(out)["data"][0]["id"], "claude-haiku-4-5")
        r, _ = self.request("GET", "/anthropic/v1/models/claude-haiku-4-5", h)
        self.assertEqual(r.status, 200)
        recs = [x for x in self.audit() if x.get("kind") == "api"]
        self.assertEqual(len(recs), 3)
        for rec in recs:
            self.assertNotIn("reserve_usd", rec)
            self.assertIsNone(rec.get("usd"))
        self.assertEqual(self.gw.ledger.totals("alice")[0], 0)
        # a spent cap still refuses model calls, but not the free calls
        self.post("/anthropic/v1/messages", {}, h)  # $3.00, over the $1 cap
        st, _ = self.post("/anthropic/v1/messages", {}, h)
        self.assertEqual(st, 403)
        st, _ = self.post("/anthropic/v1/messages/count_tokens", {"messages": []}, h)
        self.assertEqual(st, 200)

    def test_upstream_error_bodies_and_headers_forwarded_unchanged(self) -> None:
        h = {"Authorization": "Bearer " + self.caps["alice"]}
        for code in (400, 429, 500, 529):
            c = http.client.HTTPConnection("127.0.0.1", self.port)
            c.request("POST", "/anthropic/v1/messages", json.dumps({"fail": code}), h)
            r = c.getresponse()
            out = r.read()
            c.close()
            self.assertEqual(r.status, code)
            self.assertEqual(json.loads(out), ERROR_BODY)
            self.assertEqual(r.getheader("retry-after"), "7")
            self.assertEqual(r.getheader("anthropic-ratelimit-tokens-remaining"), "0")
            self.assertEqual(r.getheader("request-id"), None)
        statuses = [x["status"] for x in self.audit() if x.get("kind") == "api"]
        self.assertEqual(statuses, [400, 429, 500, 529])
        self.assertEqual(self.gw.ledger.totals("alice")[0], 0)

    def test_api_hello_answered_without_upstream_or_capability(self) -> None:
        for method in ("HEAD", "GET"):
            r, out = self.request(method, "/anthropic/api/hello", {"User-Agent": "claude-cli/2.1.303"})
            self.assertEqual((r.status, out), (200, b""))
            self.assertTrue(r.getheader("x-tether-request-id"))
        self.assertEqual(FakeUpstream.seen, [])
        self.assertEqual([x["rule"] for x in self.audit()], ["route:anthropic:hello"] * 2)
        # only on the model route, and only the probe
        r, _ = self.request("HEAD", "/github/api/hello", {})
        self.assertEqual(r.status, 403)
        r, _ = self.request("POST", "/anthropic/api/hello", {"Content-Length": "0"})
        self.assertEqual(r.status, 403)

    def test_compiled_settings_skip_the_webfetch_preflight(self) -> None:
        out, lints = tether.compile_policy(HERE / "spike/msb/poc-policy.json")
        self.assertEqual(lints.errors, [])
        for agent, settings in out["settings"].items():
            self.assertIs(settings.get("skipWebFetchPreflight"), True, agent)

    # --- egress

    def connect(self, target: str, cap: str | None = None) -> tuple[bytes, socket.socket]:
        s = socket.create_connection(("127.0.0.1", self.port))
        req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if cap:
            req += "Proxy-Authorization: Basic " + base64.b64encode(f"agent:{cap}".encode()).decode() + "\r\n"
        s.sendall((req + "\r\n").encode())
        return s.recv(4096), s

    def tunnel(self, first: bytes, host: str = "localhost") -> bytes:
        """CONNECT to the echo server, send `first`, return what came back."""
        head, s = self.connect(f"{host}:{self.echo_port}", self.caps["alice"])
        self.assertIn(b"200", head.split(b"\r\n")[0])
        s.sendall(first)
        s.settimeout(5)
        got = b""
        with contextlib.suppress(ConnectionResetError):  # refused with our bytes unread: RST
            while chunk := s.recv(65536):
                got += chunk
        s.close()
        return got

    def wait_audit(self, n: int) -> list[dict[str, Any]]:
        """The tunnel's audit line is written as its handler ends."""
        for _ in range(100):
            recs = self.audit()
            if len(recs) >= n:
                return recs
            time.sleep(0.02)
        self.fail("no audit line")

    def test_egress_allowlisted_host_tunnels(self) -> None:
        hello = tls_hello("localhost")
        self.assertEqual(self.tunnel(hello)[:20], b"echo:" + hello[:15])
        rec = self.wait_audit(1)[-1]
        self.assertEqual((rec["decision"], rec["addr"]), ("allow", "127.0.0.1"))

    # --- CONNECT hardening

    def test_ip_literal_refused(self) -> None:
        self.gw.p.egress_allow.append("::1")
        head, s = self.connect(f"[::1]:{self.echo_port}", self.caps["alice"])
        s.close()
        self.assertIn(b"403", head.split(b"\r\n")[0])
        self.assertIn("IP literal", self.audit()[-1]["reason"])
        self.assertEqual(self.gw.egress_ok("10.0.0.1", 443)[1], "IP literal; CONNECT by name")

    def test_name_resolving_to_loopback_needs_an_address_allowlist(self) -> None:
        self.gw.p.egress_addresses = []
        head, s = self.connect(f"localhost:{self.echo_port}", self.caps["alice"])
        s.close()
        self.assertIn(b"403", head.split(b"\r\n")[0])
        rec = self.audit()[-1]
        self.assertEqual(rec["decision"], "deny")
        self.assertIn("non-public address", rec["reason"])
        self.assertIn("127.0.0.1", rec["addrs"])

    def test_sni_must_match_the_connect_host(self) -> None:
        self.assertEqual(self.tunnel(tls_hello("example.com")), b"", "nothing reaches the upstream")
        self.assertIn("SNI example.com does not match CONNECT host localhost", self.wait_audit(1)[-1]["reason"])

    def test_not_tls_is_refused(self) -> None:
        self.assertEqual(self.tunnel(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"), b"")
        rec = self.wait_audit(1)[-1]
        self.assertEqual(rec["decision"], "deny")
        self.assertIn("not a TLS handshake", rec["reason"])

    def test_client_hello_sni_fails_closed(self) -> None:
        self.assertEqual(tether.client_hello_sni(handshake(sni_ext(b"Registry.NPMjs.org"))), "registry.npmjs.org")
        for hs, why in (
            (handshake(b""), "no SNI"),
            (handshake(sni_ext(b"x.org") + ext(0xFE0D, b"\x00" * 8)), "encrypted ClientHello"),
            (handshake(sni_ext(b"x.org"))[:-3], "malformed"),
            (b"\x02" + handshake(b"")[1:], "not a ClientHello"),
        ):
            with self.assertRaisesRegex(ValueError, why):
                tether.client_hello_sni(hs)
        with self.assertRaisesRegex(ValueError, "no SNI"):
            tether.client_hello_sni(tether.read_client_hello(self.pair(tls_hello(None)))[1])

    def pair(self, data: bytes) -> socket.socket:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(data)
        return b

    def test_client_hello_split_over_records(self) -> None:
        hs = handshake(sni_ext(b"pypi.org"))
        recs = b"".join(b"\x16\x03\x01" + len(c).to_bytes(2, "big") + c for c in (hs[:10], hs[10:30], hs[30:]))
        raw, got = tether.read_client_hello(self.pair(recs + b"trailing"))
        self.assertEqual((raw, got), (recs, hs))
        self.assertEqual(tether.client_hello_sni(got), "pypi.org")

    def test_addr_refusal(self) -> None:
        for a in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254", "100.64.0.1", "fd00:ec2::254",
                  "fe80::1", "::1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1", "ff02::1"):
            self.assertIsNotNone(tether.addr_refusal(a, []), a)
        for a in ("104.16.0.1", "2606:4700::1"):
            self.assertIsNone(tether.addr_refusal(a, []), a)
        import ipaddress

        self.assertIsNone(tether.addr_refusal("10.1.2.3", [ipaddress.ip_network("10.0.0.0/8")]))
        self.assertIsNone(tether.addr_refusal("::ffff:10.1.2.3", [ipaddress.ip_network("10.0.0.0/8")]))

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
        files = [self.policy.state_dir / f"{s}.env" for s in self.caps]
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

    # --- streamed request bodies

    def sink_route(self) -> str:
        Sink.got = []
        sink = serve(Sink)
        self.addCleanup(sink.server_close)
        self.addCleanup(sink.shutdown)
        r = self.gw.p.routes["github"]
        r.upstream = urllib.parse.urlsplit(f"http://127.0.0.1:{sink.server_address[1]}")
        r.paths = ["/*/info/refs", "/*/git-receive-pack"]
        return "/github/o/r.git/git-receive-pack"

    def git_auth(self) -> dict[str, str]:
        return {"Authorization": "Basic " + base64.b64encode(f"x:{self.caps['alice']}".encode()).decode()}

    def test_large_chunked_body_streams_with_bounded_memory(self) -> None:
        path = self.sink_route()
        piece = os.urandom(65536)
        count = 1700  # ~106 MiB: over MAX_BODY, so buffering would have refused it
        want = hashlib.sha256()
        for _ in range(count):
            want.update(piece)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        tracemalloc.start()
        try:
            c.request("POST", path, (piece for _ in range(count)), self.git_auth(), encode_chunked=True)
            r = c.getresponse()
            got = json.loads(r.read())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(got["bytes"], count * 65536)
        self.assertEqual(got["sha256"], want.hexdigest())
        self.assertEqual(got["te"], "chunked")
        self.assertLess(peak, 16 * 1024 * 1024)  # client, gateway and sink together
        self.assertEqual(self.audit()[-1]["bytes_up"], count * 65536)

    def test_content_length_body_streams_with_its_length(self) -> None:
        path = self.sink_route()
        body = os.urandom(300_000)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request("POST", path, io.BytesIO(body), {"Content-Length": str(len(body)), **self.git_auth()})
        got = json.loads(c.getresponse().read())
        c.close()
        self.assertEqual(got["cl"], str(len(body)))
        self.assertIsNone(got["te"])
        self.assertEqual(got["sha256"], hashlib.sha256(body).hexdigest())

    def test_client_stopping_mid_body_is_audited(self) -> None:
        path = self.sink_route()
        s = socket.create_connection(("127.0.0.1", self.port))
        auth = self.git_auth()["Authorization"]
        s.sendall(
            f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: {auth}\r\nContent-Length: 1000000\r\n\r\n".encode()
            + b"x" * 1000
        )
        time.sleep(0.2)
        s.close()
        rec: dict[str, Any] = {}
        for _ in range(50):
            rec = self.audit()[-1] if self.audit() else {}
            if rec.get("status") == 499:
                break
            time.sleep(0.1)
        self.assertEqual(rec.get("status"), 499)
        self.assertIn("mid-body", rec["reason"])
        self.assertEqual(rec["bytes_up"], 1000)

    def test_malformed_chunk_is_refused(self) -> None:
        path = self.sink_route()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        auth = self.git_auth()["Authorization"]
        s.sendall(
            f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: {auth}\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
            + b"zz\r\nhello\r\n0\r\n\r\n"
        )
        s.recv(65536)
        s.close()
        self.assertIn("malformed chunk", self.audit()[-1]["reason"])

    def test_metered_route_still_buffers_with_a_bound(self) -> None:
        old = tether.MAX_BODY
        tether.MAX_BODY = 1000
        try:
            h = {"Authorization": "Bearer " + self.caps["alice"]}
            st, out = self.post("/anthropic/v1/messages", {"pad": "x" * 2000}, h)
            self.assertEqual(st, 400)
            self.assertIn("too large", json.loads(out)["error"]["message"])
            self.assertEqual(FakeUpstream.seen, [])
            # Same size on a streamed route goes through.
            path = self.sink_route()
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            c.request("POST", path, iter([b"y" * 2000]), self.git_auth(), encode_chunked=True)
            self.assertEqual(json.loads(c.getresponse().read())["bytes"], 2000)
            c.close()
            # A chunked model call is buffered and forwarded with a length.
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            c.request("POST", "/anthropic/v1/messages", iter([b'{"stream":', b" true}"]), h, encode_chunked=True)
            self.assertEqual(c.getresponse().status, 200)
            c.close()
            self.assertEqual(FakeUpstream.seen[-1]["headers"]["content-length"], "16")
        finally:
            tether.MAX_BODY = old

    # --- Upgrade passthrough

    def ws_route(self, refuse: int | None = None) -> WsUpstream:
        ws = WsUpstream(refuse)
        self.addCleanup(ws.close)
        r = self.gw.p.routes["github"]
        r.upstream = urllib.parse.urlsplit(f"http://127.0.0.1:{ws.port}/base")
        r.paths = ["/*/info/refs", "/mcp/*"]
        return ws

    def ws_handshake(self, path: str, auth: str, early: bytes = b"") -> tuple[socket.socket, bytes]:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: tether\r\nAuthorization: {auth}\r\nConnection: keep-alive, Upgrade\r\n"
            "Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "X-Api-Key: tth_leak\r\n\r\n".encode()
            + early
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            data = s.recv(65536)
            if not data:
                break
            buf += data
        return s, buf

    def recv_until(self, s: socket.socket, want: bytes, have: bytes = b"") -> bytes:
        while want not in have:
            data = s.recv(65536)
            if not data:
                break
            have += data
        return have

    def test_upgrade_splices_after_101(self) -> None:
        ws = self.ws_route()
        auth = self.git_auth()["Authorization"]
        s, buf = self.ws_handshake("/github/mcp/socket?x=1", auth, early=b"early-frame")
        self.assertTrue(buf.startswith(b"HTTP/1.1 101 "), buf)
        self.assertIn(b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", buf)
        got = self.recv_until(s, b"early-frame", buf)
        self.assertIn(b"hello-from-server", got)
        frame = os.urandom(200_000)
        s.sendall(frame)
        echoed = b""
        while len(echoed) < len(frame):
            echoed += s.recv(65536)
        self.assertEqual(echoed, frame)
        s.close()
        h = ws.heads[-1]
        self.assertEqual(h["_line"], "GET /base/mcp/socket?x=1 HTTP/1.1")
        self.assertEqual((h["upgrade"], h["connection"]), ("websocket", "Upgrade"))
        self.assertEqual(h["sec-websocket-key"], "dGhlIHNhbXBsZSBub25jZQ==")
        self.assertEqual(h["host"], f"127.0.0.1:{ws.port}")
        self.assertEqual(
            h["authorization"], "Basic " + base64.b64encode(f"x-access-token:{GH_TOKEN}".encode()).decode()
        )
        self.assertNotIn("x-api-key", h)
        self.assertNotIn(self.caps["alice"], json.dumps(h))
        rec = self.wait_audit_status(101)
        self.assertEqual((rec["decision"], rec["route"], rec["upgrade"]), ("allow", "github", "websocket"))
        self.assertEqual(rec["bytes_up"], len(b"early-frame") + len(frame))
        self.assertGreater(rec["bytes_down"], len(frame))

    def wait_audit_status(self, status: int) -> dict[str, Any]:
        for _ in range(50):
            recs = [r for r in self.audit() if r.get("status") == status]
            if recs:
                return recs[-1]
            time.sleep(0.1)
        self.fail(f"no audit line with status {status}: {self.audit()}")

    def test_upgrade_refused_before_upstream(self) -> None:
        ws = self.ws_route()
        # no cap
        s, buf = self.ws_handshake("/github/mcp/socket", "Bearer nope")
        s.close()
        self.assertTrue(buf.startswith(b"HTTP/1.1 401 "), buf)
        # a cap without the route
        cap, _ = self.gw.caps.mint("default", "alice", routes=["anthropic"])
        s, buf = self.ws_handshake("/github/mcp/socket", "Bearer " + cap)
        s.close()
        self.assertTrue(buf.startswith(b"HTTP/1.1 403 "), buf)
        self.assertIn("not granted", self.audit()[-1]["reason"])
        # a path off the allowlist
        s, buf = self.ws_handshake("/github/other", self.git_auth()["Authorization"])
        s.close()
        self.assertTrue(buf.startswith(b"HTTP/1.1 403 "), buf)
        self.assertEqual(ws.heads, [], "a refused upgrade must not reach the upstream")

    def test_upgrade_not_offered_on_metered_route(self) -> None:
        s, buf = self.ws_handshake("/anthropic/v1/messages", "Bearer " + self.caps["alice"])
        s.close()
        self.assertTrue(buf.startswith(b"HTTP/1.1 403 "), buf)
        self.assertEqual(FakeUpstream.seen, [])
        self.assertIn("metered", self.audit()[-1]["reason"])

    def test_upgrade_refused_by_upstream_is_relayed(self) -> None:
        self.ws_route(refuse=426)
        s, buf = self.ws_handshake("/github/mcp/socket", self.git_auth()["Authorization"])
        got = self.recv_until(s, b"0\r\n\r\n", buf)
        s.close()
        self.assertTrue(got.startswith(b"HTTP/1.1 426 "), got)
        self.assertIn(b"X-Up: 1", got)
        self.assertIn(b"upgrade refused", got)
        rec = self.wait_audit_status(426)
        self.assertEqual(rec["reason"], "upstream refused the upgrade")

    def test_wants_upgrade(self) -> None:
        h = {"connection": "keep-alive, Upgrade", "upgrade": "websocket"}
        self.assertEqual(tether.wants_upgrade(h), "websocket")
        self.assertIsNone(tether.wants_upgrade({"upgrade": "websocket"}))
        self.assertIsNone(tether.wants_upgrade({"connection": "upgrade"}))

    def test_audit_names_the_sandbox(self) -> None:
        self.post("/anthropic/v1/messages", {"stream": True}, {"Authorization": "Bearer " + self.caps["alice"]})
        self.assertEqual(self.audit()[-1]["sandbox"], "default")

    # --- audit: request ids, rules, hash chain

    def test_request_id_on_responses_and_audit_lines(self) -> None:
        cap = self.caps["alice"]
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        client = {
            "Authorization": "Bearer " + cap,
            "X-Claude-Code-Session-Id": "0b5e-sess-1",
            "x-client-request-id": "cli-req-9",
            "x-claude-code-agent-id": "has spaces; not an id",
        }
        c.request("POST", "/anthropic/v1/messages", json.dumps({"stream": True}), client)
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 200)
        rid = r.getheader("x-tether-request-id") or ""
        self.assertRegex(rid, r"^req_[0-9a-f]{16}$")
        self.assertEqual(r.msg.get_all("x-tether-request-id"), [rid])  # the upstream's copy is dropped
        self.assertNotIn("x-tether-request-id", FakeUpstream.seen[-1]["headers"])
        rec = self.audit()[-1]
        self.assertEqual(rec["request_id"], rid)
        self.assertEqual(rec["upstream_request_id"], "req_upstream_123")
        self.assertEqual(rec["rule"], "route:anthropic")
        self.assertEqual(rec["policy_version"], self.policy.version)
        self.assertEqual((rec["client_session_id"], rec["client_request_id"]), ("0b5e-sess-1", "cli-req-9"))
        self.assertNotIn("client_agent_id", rec)
        self.assertNotIn("headers", rec)

        # a refusal
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("POST", "/anthropic/v1/organizations/me", "{}", {"Authorization": "Bearer " + cap})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 403)
        rec = self.audit()[-1]
        self.assertEqual(rec["request_id"], r.getheader("x-tether-request-id"))
        self.assertEqual(rec["rule"], "route:anthropic:path")

        # git: the GitHub request id
        auth = base64.b64encode(f"agent:{self.caps['bob']}".encode()).decode()
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/github/acme/app.git/info/refs", headers={"Authorization": "Basic " + auth})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(self.audit()[-1]["upstream_request_id"], "C0FF:EE:1234")

        # CONNECT: refused, then allowed
        head, s = self.connect("evil.example:443", cap)
        s.close()
        rid = head.split(b"x-tether-request-id: ", 1)[1].split(b"\r\n", 1)[0].decode()
        rec = self.audit()[-1]
        self.assertEqual((rec["request_id"], rec["rule"]), (rid, "egress:allowlist"))
        head, s = self.connect(f"localhost:{self.echo_port}", cap)
        self.assertIn(b"200", head.split(b"\r\n")[0])
        rid = head.split(b"x-tether-request-id: ", 1)[1].split(b"\r\n", 1)[0].decode()
        s.sendall(tls_hello("localhost"))
        s.settimeout(5)
        s.recv(65536)
        s.close()
        rec = self.wait_audit(5)[-1]
        self.assertEqual((rec["request_id"], rec["rule"], rec["decision"]), (rid, "egress:allow[0]", "allow"))
        self.assertEqual(len({x["request_id"] for x in self.audit()}), 5)

    def test_audit_chain_verifies_and_breaks_at_an_edit(self) -> None:
        h = {"Authorization": "Bearer " + self.caps["alice"]}
        self.post("/anthropic/v1/messages", {}, h)  # $3, over the $1 cap
        st, _ = self.post("/anthropic/v1/messages", {}, h)
        self.assertEqual(st, 403)
        self.assertEqual(self.audit()[-1]["rule"], "spend:session")
        self.connect("evil.example:443")[1].close()
        self.post("/openai/v1/responses", {}, h)
        f = self.policy.state_dir / "audit.jsonl"
        self.assertEqual(tether.verify_chain(f), (4, None))
        lines = f.read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["prev_hash"], hashlib.sha256(b"").hexdigest())
        cfg = ["-c", str(self.policy.path), "audit", "verify"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            tether.main(cfg)
        self.assertIn("4 lines, chain intact", out.getvalue())

        # quietly lower what line 1 cost: line 2's link no longer matches
        rec = json.loads(lines[0])
        rec["usd"] = 0.01
        edited = [json.dumps(rec, separators=(",", ":"))] + lines[1:]
        f.write_text("\n".join(edited) + "\n")
        n, broken = tether.verify_chain(f)
        self.assertEqual(n, 2)
        self.assertIn("does not match line 1", broken or "")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as ex:
            tether.main(cfg)
        self.assertEqual(ex.exception.code, 1)
        self.assertIn("chain broken at line 2", err.getvalue())

        # deleting a line breaks it too
        f.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
        self.assertEqual(tether.verify_chain(f)[0], 2)

    def test_restart_continues_the_chain(self) -> None:
        self.post("/anthropic/v1/messages", {"stream": True}, {"Authorization": "Bearer " + self.caps["alice"]})
        f = self.policy.state_dir / "audit.jsonl"
        last = f.read_text().splitlines()[-1]
        _, gw2 = tether.build_servers(self.policy)  # a second process start reads the tail
        self.assertEqual(gw2.audit.prev, hashlib.sha256(last.encode()).hexdigest())
        gw2.audit.write(kind="egress", decision="deny", host="x:443")
        self.post("/anthropic/v1/messages", {"stream": True}, {"Authorization": "Bearer " + self.caps["alice"]})
        # two writers on one file would fork the chain: the old gateway's
        # next line links to its own last line, not gw2's
        self.assertEqual(tether.verify_chain(f)[0], 3)
        f.write_text("\n".join(f.read_text().splitlines()[:2]) + "\n")
        self.assertEqual(tether.verify_chain(f), (2, None))

    def test_policy_version_tracks_the_file(self) -> None:
        v = self.policy.version
        self.assertRegex(v, r"^[0-9a-f]{12}$")
        self.policy.path.write_text(self.policy.path.read_text() + " ")
        self.assertNotEqual(tether.Policy(self.policy.path).version, v)

    def test_report_runs(self) -> None:
        self.post("/anthropic/v1/messages", {"stream": True}, {"Authorization": "Bearer " + self.caps["alice"]})
        self.connect("evil.example:443")[1].close()
        tether.cmd_report(self.policy, as_json=False)


class ReportByTool(unittest.TestCase):
    LINES = [
        {"ts": "2026-10-19T01:00:00-07:00", "decision": "allow", "session": "cage", "agent": "claude",
         "model": "claude-haiku-4-5", "tokens": {"in": 100, "cr": 50, "out": 10}, "usd": 0.25},
        {"ts": "2026-10-18T23:00:00-07:00", "decision": "allow", "session": "cage", "agent": "claude",
         "tokens": {"in": 1, "out": 1}, "usd": 0.5},
        {"ts": "2026-10-19T01:00:01-07:00", "decision": "allow", "session": "cage", "agent": "claude", "tool": "git"},
        {"ts": "2026-10-19T01:00:02-07:00", "decision": "deny", "rule": "cap:route-grant", "session": "cage",
         "agent": "claude", "tool": "git"},
        {"ts": "2026-10-19T01:00:03-07:00", "decision": "deny", "rule": "egress:default", "session": "cage",
         "agent": "codex", "tool": "npm", "host": "evil.example:443"},
        {"ts": "2026-10-19T01:00:04-07:00", "decision": "allow", "session": "other"},
    ]

    def lines(self) -> list[str]:
        return [json.dumps(r) for r in self.LINES] + ["not json", "[1]"]

    def test_one_row_per_agent_and_tool(self) -> None:
        rows = tether.report_rows(self.lines(), "tool", "2026-10-19")
        self.assertEqual(
            sorted(rows),
            [("cage", "claude", "-"), ("cage", "claude", "git"), ("cage", "codex", "npm"), ("other", "-", "-")],
        )
        model = rows[("cage", "claude", "-")]
        self.assertEqual((model["requests"], model["in"], model["out"]), (2, 151, 11))
        self.assertAlmostEqual(model["usd"], 0.75)
        self.assertAlmostEqual(model["usd_today"], 0.25)
        git = rows[("cage", "claude", "git")]
        self.assertEqual((git["requests"], git["denied"], git["usd"]), (2, 1, 0.0))
        self.assertEqual(dict(git["denied_by"]), {"cap:route-grant": 1})

    def test_by_session_folds_tools(self) -> None:
        rows = tether.report_rows(self.lines(), "session", "2026-10-19")
        self.assertEqual(sorted(rows), [("cage",), ("other",)])
        self.assertEqual((rows[("cage",)]["requests"], rows[("cage",)]["denied"]), (5, 2))

    def test_cli(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state"
            state.mkdir()
            (state / "audit.jsonl").write_text("\n".join(self.lines()) + "\n")
            cfg = Path(d) / "policy.json"
            cfg.write_text(json.dumps({"state_dir": str(state), "sessions": {}, "routes": {}}))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                tether.main(["-c", str(cfg), "report", "--by", "tool", "--json"])
            got = json.loads(out.getvalue())
            self.assertEqual(len(got), 4)
            self.assertEqual(got[2]["agent"], "codex")
            self.assertEqual(got[2]["tool"], "npm")
            self.assertEqual(got[2]["denied_by"], {"egress:default": 1})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                tether.main(["-c", str(cfg), "report", "--by", "tool"])
            text = out.getvalue()
            self.assertRegex(text, r"cage\s+claude\s+git\s+2\s+1\b")
            self.assertIn("evil.example:443", text)


class TwoSandboxes(unittest.TestCase):
    """Fixture: two sandboxes, two listeners, one gateway."""

    def setUp(self) -> None:
        FakeUpstream.seen = []
        self.up = serve(FakeUpstream)
        self.echo = echo_server()
        self.echo_port = self.echo.getsockname()[1]
        self.tmp = tempfile.TemporaryDirectory()
        up = f"http://127.0.0.1:{self.up.server_address[1]}"
        cfg = {
            "sandboxes": {
                "chad-dev": {"listen": "127.0.0.1:0", "advertise": "http://host.microsandbox.internal:18001"},
                "other": {"listen": "127.0.0.1:0", "sessions": ["o1", "o2"]},
            },
            "routes": {
                "anthropic": {"upstream": up, "credential": "env:T_ANTHROPIC", "inject": "anthropic", "meter": True},
            },
            "egress": {"allow": ["localhost"], "ports": [self.echo_port], "allow_addresses": LOOPBACK},
        }
        self.configure(cfg, up)
        os.environ["T_ANTHROPIC"] = REAL_KEY
        p = Path(self.tmp.name) / "policy.json"
        p.write_text(json.dumps(cfg))
        self.policy = tether.Policy(p)
        self.srvs, self.gw = tether.build_servers(self.policy)
        for s in self.srvs.values():
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.ports = {n: s.server_address[1] for n, s in self.srvs.items()}
        self.caps = tether.init_caps(self.policy, self.gw.caps)

    def configure(self, cfg: dict[str, Any], up: str) -> None:
        """Subclasses widen the fixture's policy here."""

    def tearDown(self) -> None:
        for s in self.srvs.values():
            s.shutdown()
            s.server_close()
        self.up.shutdown()
        self.up.server_close()
        self.echo.close()
        self.tmp.cleanup()

    def post(self, sandbox: str, cap: str) -> int:
        c = http.client.HTTPConnection("127.0.0.1", self.ports[sandbox])
        c.request("POST", "/anthropic/v1/messages", "{}", {"Authorization": "Bearer " + cap})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def connect(self, sandbox: str, cap: str, host: str = "localhost") -> bytes:
        s = socket.create_connection(("127.0.0.1", self.ports[sandbox]))
        auth = base64.b64encode(f"agent:{cap}".encode()).decode()
        s.sendall(f"CONNECT {host}:{self.echo_port} HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode())
        head = s.recv(4096)
        if b" 200 " in head:
            s.sendall(tls_hello(host))
            s.recv(4096)
        s.close()
        return head.split(b"\r\n")[0]

    def audit(self) -> list[dict[str, Any]]:
        f = self.policy.state_dir / "audit.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()]

    def cli(self, *args: str) -> str:
        """Run the tether CLI in-process, as a separate process would: its own
        Registry, talking to the running server only through caps.json."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            tether.main(["-c", str(self.policy.path), *args])
        return out.getvalue()


class ChannelBindingTest(TwoSandboxes):
    """A cap only works on its own sandbox's port."""

    def test_two_listeners_one_per_sandbox(self) -> None:
        self.assertEqual(set(self.srvs), {"chad-dev", "other"})
        self.assertNotEqual(self.ports["chad-dev"], self.ports["other"])
        # a sandbox with no session list gets one session named after it
        self.assertEqual(self.policy.session_sandbox, {"chad-dev": "chad-dev", "o1": "other", "o2": "other"})

    def test_cap_works_only_on_its_own_listener(self) -> None:
        self.assertEqual(self.post("chad-dev", self.caps["chad-dev"]), 200)
        self.assertEqual(self.post("other", self.caps["o1"]), 200)
        n = len(FakeUpstream.seen)
        self.assertEqual(self.post("other", self.caps["chad-dev"]), 403)
        self.assertEqual(self.post("chad-dev", self.caps["o2"]), 403)
        self.assertEqual(len(FakeUpstream.seen), n, "a replayed cap must not reach the upstream")
        denies = [r for r in self.audit() if r["decision"] == "deny"]
        self.assertEqual([r["sandbox"] for r in denies], ["other", "chad-dev"])
        self.assertIn("bound to sandbox chad-dev", denies[0]["reason"])

    def test_connect_cap_works_only_on_its_own_listener(self) -> None:
        self.assertIn(b"200", self.connect("chad-dev", self.caps["chad-dev"]))
        # 403, not 407: the cap is real, it is on the wrong channel
        self.assertIn(b"403", self.connect("other", self.caps["chad-dev"]))
        rec = self.audit()[-1]
        self.assertEqual((rec["decision"], rec["sandbox"]), ("deny", "other"))

    def test_env_advertises_each_sandbox_listener(self) -> None:
        files = {s: (self.policy.state_dir / f"{s}.env").read_text() for s in self.caps}
        self.assertIn("ANTHROPIC_BASE_URL=http://host.microsandbox.internal:18001/anthropic", files["chad-dev"])
        # default advertise is the msb host alias on the sandbox's listen port
        self.assertIn(
            f"host.microsandbox.internal:{self.policy.sandboxes['other'].listen.rpartition(':')[2]}", files["o1"]
        )

    def test_session_in_two_sandboxes_is_refused(self) -> None:
        p = Path(self.tmp.name) / "dup.json"
        p.write_text(
            json.dumps(
                {"sandboxes": {"a": {"listen": "127.0.0.1:0"}, "b": {"listen": "127.0.0.1:0", "sessions": ["a"]}}}
            )
        )
        with self.assertRaises(ValueError):
            tether.Policy(p)


class RegistryTest(TwoSandboxes):
    """tether cap mint|revoke|list|boot, against a running gateway."""

    def test_mint_prints_a_working_cap_once(self) -> None:
        mint = ["cap", "mint", "--session", "o1", "--agent", "claude", "--tool", "git", "--human", "chad"]
        cap = self.cli(*mint).strip()
        self.assertTrue(cap.startswith("tth_"))
        self.assertEqual(self.post("other", cap), 200)
        rec = self.audit()[-1]
        self.assertEqual((rec["session"], rec["agent"], rec["tool"], rec["human"]), ("o1", "claude", "git", "chad"))
        self.assertTrue(rec["cap_id"].startswith("cap_"))
        # the registry is keyed by hash: it never holds a usable cap
        reg = (self.policy.state_dir / "caps.json").read_text()
        self.assertNotIn(cap, reg)
        self.assertNotIn(self.caps["o1"], reg)
        self.assertEqual(oct((self.policy.state_dir / "caps.json").stat().st_mode & 0o777), "0o600")

    def test_tool_env_names_only_its_grants(self) -> None:
        # A routes-only tool gets no proxy; a CONNECT-only tool gets no route vars.
        before = sorted(self.policy.state_dir.glob("*.env"))
        out = self.cli("cap", "mint", "--session", "o1", "--tool", "x", "--no-egress", "--env", "-")
        env = dict(line.split("=", 1) for line in out.splitlines() if not line.startswith("#"))
        self.assertTrue(out.startswith("# tether cap_"))
        self.assertIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(self.post("other", env["TETHER_CAPABILITY"]), 200)
        self.assertIn(b"403", self.connect("other", env["TETHER_CAPABILITY"]))
        out = self.cli("cap", "mint", "--session", "o1", "--tool", "npm", "--no-route", "--env", "-")
        env = dict(line.split("=", 1) for line in out.splitlines() if not line.startswith("#"))
        self.assertIn("HTTPS_PROXY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", env)
        self.assertEqual(self.post("other", env["TETHER_CAPABILITY"]), 403)
        # --env - writes to stdout only: no env file lands on the host
        self.assertEqual(sorted(self.policy.state_dir.glob("*.env")), before)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.cli("cap", "mint", "--session", "o1", "--no-route", "--route", "anthropic")
        # git's rewrite follows the github grant, not the policy's route list
        pol = types.SimpleNamespace(advertise="http://gw:1", routes={"anthropic": 0, "github": 0})
        self.assertIn("GIT_CONFIG_KEY_0", tether.env_for(pol, "tth_x", routes=["github"]))  # type: ignore[arg-type]
        self.assertNotIn("GIT_CONFIG_KEY_0", tether.env_for(pol, "tth_x", routes=["anthropic"]))  # type: ignore[arg-type]
        self.assertIn("GIT_CONFIG_KEY_0", tether.env_for(pol, "tth_x"))  # type: ignore[arg-type]

    def test_tool_native_cap_files(self) -> None:
        sb = types.SimpleNamespace(advertise="http://gw.internal:18001")
        pol = types.SimpleNamespace(sandboxes={"s": sb}, routes={"github": 0, "anthropic": 0})
        rec = {"sandbox": "s", "routes": ["github"], "egress": []}
        git = tether.cap_file_body(pol, "tth_secret", rec, "gitconfig")  # type: ignore[arg-type]
        self.assertIn('[url "http://gw.internal:18001/github/"]\n\tinsteadOf = https://github.com/', git)
        # the cap rides in a header, never in the URL git passes to its remote helper's argv
        self.assertNotIn("tth_secret@", git)
        b64 = base64.b64encode(b"agent:tth_secret").decode()
        self.assertIn(f'[http "http://gw.internal:18001/github/"]\n\textraHeader = Authorization: Basic {b64}', git)
        npm = tether.cap_file_body(pol, "tth_secret", {"sandbox": "s", "routes": [], "egress": ["r.example"]}, "npmrc")  # type: ignore[arg-type]
        self.assertIn("https-proxy=http://agent:tth_secret@gw.internal:18001\n", npm)
        self.assertIn("noproxy=gw.internal\n", npm)
        with self.assertRaises(ValueError):
            tether.cap_file_body(pol, "c", {"sandbox": "s", "routes": ["anthropic"], "egress": None}, "gitconfig")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            tether.cap_file_body(pol, "c", rec, "npmrc")  # type: ignore[arg-type]
        # the CLI refuses before minting: this policy has no github route
        n = len(json.loads(self.cli("cap", "list", "--json")))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.cli("cap", "mint", "--session", "o1", "--tool", "git", "--format", "gitconfig", "--env", "-")
        self.assertEqual(len(json.loads(self.cli("cap", "list", "--json"))), n)
        out = self.cli("cap", "mint", "--session", "o1", "--tool", "npm", "--format", "npmrc", "--env", "-")
        self.assertTrue(out.startswith("; tether cap_"))
        self.assertIn("https-proxy=http://agent:tth_", out)

    def test_list_shows_records_never_caps(self) -> None:
        cap = self.cli("cap", "mint", "--session", "o2", "--tool", "npm").strip()
        listing = self.cli("cap", "list")
        self.assertIn("npm", listing)
        self.assertEqual(len(listing.splitlines()), 1 + 3 + 1)  # header, three session caps, the new one
        rows = json.loads(self.cli("cap", "list", "--json"))
        self.assertEqual({r["state"] for r in rows}, {"live"})
        for c in [cap, *self.caps.values()]:
            self.assertNotIn(c, listing + json.dumps(rows))

    def test_revoke_takes_effect_on_a_running_server(self) -> None:
        cap = self.caps["chad-dev"]
        self.assertEqual(self.post("chad-dev", cap), 200)
        self.assertIn(b"200", self.connect("chad-dev", cap))
        cid = self.gw.caps.lookup(cap)["cap_id"]
        self.cli("cap", "revoke", cid)
        n = len(FakeUpstream.seen)
        self.assertEqual(self.post("chad-dev", cap), 403)
        self.assertIn(b"403", self.connect("chad-dev", cap))
        self.assertEqual(len(FakeUpstream.seen), n)
        self.assertIn(f"{cid} of session chad-dev is revoked", self.audit()[-1]["reason"])
        # the other sandbox's caps are untouched
        self.assertEqual(self.post("other", self.caps["o1"]), 200)
        rows = json.loads(self.cli("cap", "list", "--all", "--json"))
        self.assertEqual([r["state"] for r in rows if r["cap_id"] == cid], ["revoked"])
        self.assertNotIn(cid, self.cli("cap", "list"))

    def test_revoke_by_sandbox(self) -> None:
        self.cli("cap", "revoke", "--sandbox", "other")
        self.assertEqual(self.post("other", self.caps["o1"]), 403)
        self.assertEqual(self.post("other", self.caps["o2"]), 403)
        self.assertEqual(self.post("chad-dev", self.caps["chad-dev"]), 200)
        with self.assertRaises(SystemExit):
            self.cli("cap", "revoke", "--sandbox", "other")  # nothing live is left to revoke

    def test_expired_cap_is_refused(self) -> None:
        cap, rec = self.gw.caps.mint("other", "o1", ttl_secs=60)
        self.assertEqual(self.post("other", cap), 200)
        real = time.time
        try:
            tether.time.time = lambda: real() + 61
            self.assertEqual(self.post("other", cap), 403)
        finally:
            tether.time.time = real
        self.assertIn("is expired", self.audit()[-1]["reason"])
        self.assertEqual(tether.parse_ttl("8h"), 8 * 3600)
        with self.assertRaises(ValueError):
            tether.parse_ttl("8")

    def test_cap_minted_for_a_is_refused_on_b(self) -> None:
        cap = self.cli("cap", "mint", "--session", "chad-dev", "--tool", "git").strip()
        self.assertEqual(self.post("chad-dev", cap), 200)
        self.assertEqual(self.post("other", cap), 403)
        self.assertIn(b"403", self.connect("other", cap))
        self.assertIn("bound to sandbox chad-dev, used on other", self.audit()[-1]["reason"])
        # and a cap can't be minted for a session outside its sandbox
        with self.assertRaises(SystemExit):
            self.cli("cap", "mint", "--session", "o1", "--sandbox", "chad-dev")
        with self.assertRaises(SystemExit):
            self.cli("cap", "mint", "--session", "o1", "--route", "github")

    def test_init_is_idempotent_and_boot_rotates(self) -> None:
        env = self.policy.state_dir / "o1.env"
        before = env.read_text()
        self.assertEqual(tether.init_caps(self.policy, self.gw.caps), {})
        self.assertEqual(env.read_text(), before)
        self.cli("cap", "boot", "other")
        self.assertNotEqual(env.read_text(), before)
        fresh = tether.env_file_cap(env)
        self.assertEqual(self.post("other", self.caps["o1"]), 403, "the last boot's cap is dead")
        self.assertEqual(self.post("other", fresh), 200)
        self.assertEqual(self.post("chad-dev", self.caps["chad-dev"]), 200, "boot touches one sandbox only")
        # an env file whose cap was revoked is re-minted by init
        self.cli("cap", "revoke", "--session", "o1")
        self.assertIn("o1", tether.init_caps(self.policy, self.gw.caps))
        self.assertEqual(self.post("other", tether.env_file_cap(env)), 200)


class CapPolicyTest(TwoSandboxes):
    """A cap's routes, egress and spend narrow the ceiling; they never widen it."""

    CEILING = ["localhost", "registry.npmjs.org", "pypi.org", "*.pythonhosted.org"]

    def configure(self, cfg: dict[str, Any], up: str) -> None:
        cfg["routes"]["github"] = {
            "upstream": up,
            "credential": "env:T_GH",
            "inject": "basic:x-access-token",
            "paths": ["/*/info/refs"],
        }
        cfg["egress"]["allow"] = self.CEILING
        cfg["spend"] = {"per_session_usd_per_day": 5.0}
        os.environ["T_GH"] = GH_TOKEN

    def get(self, sandbox: str, cap: str, path: str) -> int:
        c = http.client.HTTPConnection("127.0.0.1", self.ports[sandbox])
        c.request("GET", path, headers={"Authorization": "Bearer " + cap})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_route_not_granted_to_the_cap(self) -> None:
        cap = self.cli("cap", "mint", "--session", "o1", "--agent", "claude", "--route", "anthropic").strip()
        self.assertEqual(self.post("other", cap), 200)
        n = len(FakeUpstream.seen)
        self.assertEqual(self.get("other", cap, "/github/x/info/refs"), 403)
        self.assertEqual(len(FakeUpstream.seen), n, "an ungranted route must not reach the upstream")
        rec = self.audit()[-1]
        self.assertEqual((rec["decision"], rec["route"]), ("deny", "github"))
        self.assertIn(f"route github not granted to capability {rec['cap_id']}", rec["reason"])
        # the session's own cap has no narrowing: every route the ceiling has
        self.assertEqual(self.get("other", self.caps["o1"], "/github/x/info/refs"), 200)

    def test_cap_egress_narrower_than_the_ceiling(self) -> None:
        npm = self.cli("cap", "mint", "--session", "o1", "--tool", "npm", "--egress", "registry.npmjs.org").strip()
        rec = self.gw.caps.lookup(npm)
        # the ceiling allows pypi.org; the npm cap does not
        self.assertEqual(self.gw.egress_ok("pypi.org", self.echo_port), (True, ""))
        ok, why = self.gw.egress_ok("pypi.org", self.echo_port, rec)
        self.assertFalse(ok)
        self.assertIn("egress grant", why)
        self.assertTrue(self.gw.egress_ok("registry.npmjs.org", self.echo_port, rec)[0])
        # over the wire: refused before any connect, on a host the ceiling allows
        self.assertIn(b"403", self.connect("other", npm, "pypi.org"))
        self.assertIn(f"not in capability {rec['cap_id']}'s egress grant", self.audit()[-1]["reason"])
        self.assertIn(b"403", self.connect("other", npm))  # localhost: in the ceiling, not in the grant
        local = self.cli("cap", "mint", "--session", "o1", "--egress", "localhost").strip()
        self.assertIn(b"200", self.connect("other", local))
        # a grant can't widen the ceiling either: serve checks both layers
        wide = {**rec, "egress": ["*"]}
        self.assertFalse(self.gw.egress_ok("example.com", self.echo_port, wide)[0])

    def test_spend_sub_cap_refuses_while_the_session_has_budget(self) -> None:
        cap = self.cli("cap", "mint", "--session", "o1", "--spend", "0.5").strip()
        cid = self.gw.caps.lookup(cap)["cap_id"]
        self.assertEqual(self.post("other", cap), 200)  # $1.00 at the default price
        n = len(FakeUpstream.seen)
        self.assertEqual(self.post("other", cap), 403)
        self.assertEqual(len(FakeUpstream.seen), n)
        self.assertIn(f"sub-cap reached for capability {cid}", self.audit()[-1]["reason"])
        self.assertEqual(self.post("other", self.caps["o1"]), 200, "the session still has $4 left")
        led = json.loads((self.policy.state_dir / "spend.json").read_text())
        self.assertEqual(led["caps"], {cid: 1.0})
        self.assertEqual(led["spent"], {"o1": 2.0}, "a sub-capped call counts once toward the session")

    def test_mint_must_fit_inside_the_ceiling(self) -> None:
        for bad in (
            ["--egress", "*"],
            ["--egress", "*.npmjs.org"],  # the ceiling has only registry.npmjs.org
            ["--egress", "example.com"],
            ["--egress", "?ypi.org"],
            ["--route", "openai"],
            ["--spend", "6"],  # above the $5 session cap
        ):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.cli("cap", "mint", "--session", "o1", *bad)
        for good in (["--egress", "files.pythonhosted.org"], ["--egress", "*.pythonhosted.org"], ["--spend", "5"]):
            with self.subTest(good=good):
                self.assertTrue(self.cli("cap", "mint", "--session", "o1", *good).startswith("tth_"))
        self.assertTrue(tether.pattern_within("a.b.example.com", "*.example.com"))
        self.assertTrue(tether.pattern_within("*.a.example.com", "*.example.com"))
        self.assertFalse(tether.pattern_within("*.com", "*.example.com"))
        self.assertFalse(tether.pattern_within("*.com", "?.com"), "a ? in the ceiling matches one char, not a *")


if __name__ == "__main__":
    unittest.main()


class ReservationTest(TwoSandboxes):
    """A metered call reserves its worst case at admission and settles at the
    end of its stream, so calls in flight can't all slip under the cap."""

    OPUS = {"model": "claude-opus-4-1", "max_tokens": 8000, "stream": True}  # reserves ~$0.60

    def configure(self, cfg: dict[str, Any], up: str) -> None:
        HoldUpstream.started = queue.Queue()
        HoldUpstream.mode = "finish"
        HoldUpstream.posts = 0
        self.hold = serve(HoldUpstream)
        self.held: list[threading.Event] = []
        cfg["routes"]["slow"] = {
            "upstream": f"http://127.0.0.1:{self.hold.server_address[1]}",
            "credential": "env:T_ANTHROPIC",
            "inject": "anthropic",
            "meter": True,
        }
        cfg["spend"] = {"per_session_usd_per_day": 1.0, "max_inflight": 2}
        cfg["prices_per_mtok"] = {"claude-opus": [15, 75], "default": [1, 1]}

    def tearDown(self) -> None:
        for go in self.held:
            go.set()
        self.hold.shutdown()
        self.hold.server_close()
        super().tearDown()

    def slow_post(self, body: dict[str, Any]) -> tuple[int, bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.ports["other"], timeout=10)
        c.request("POST", "/slow/v1/messages", json.dumps(body), {"Authorization": "Bearer " + self.caps["o1"]})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out

    def start(self, body: dict[str, Any]) -> tuple[threading.Thread, dict[str, Any], threading.Event]:
        """Start a call and return once the upstream holds it open."""
        out: dict[str, Any] = {}

        def run() -> None:
            try:
                out["status"], out["body"] = self.slow_post(body)
            except (OSError, http.client.HTTPException) as e:
                out["error"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        go = HoldUpstream.started.get(timeout=5)
        self.held.append(go)
        return t, out, go

    def finish(self, t: threading.Thread, go: threading.Event) -> None:
        go.set()
        t.join(10)
        self.assertFalse(t.is_alive())

    def ledger(self) -> tether.Ledger:
        return self.gw.ledger

    def last_api(self) -> dict[str, Any]:
        for _ in range(100):
            recs = [r for r in self.audit() if r.get("kind") == "api" and r.get("decision") == "allow"]
            if recs and "bytes_down" in recs[-1]:
                return recs[-1]
            time.sleep(0.02)
        self.fail("no audit line")

    def test_reservation_refuses_a_call_that_would_cross_the_cap(self) -> None:
        t, out, go = self.start(self.OPUS)
        self.assertAlmostEqual(
            self.ledger().held["o1"], tether.reserve_usd(self.policy.prices, json.dumps(self.OPUS).encode())
        )
        # nothing is spent yet, so check-then-charge would admit this one too
        st, body = self.slow_post(self.OPUS)
        self.assertEqual(st, 403)
        msg = json.loads(body)["error"]["message"]
        self.assertIn("would be crossed", msg)
        self.assertIn("reserved in flight", msg)
        self.assertEqual(HoldUpstream.posts, 1, "a refused call must not reach the upstream")
        self.assertEqual(self.audit()[-1]["decision"], "deny")
        self.finish(t, go)
        self.assertEqual(out["status"], 200)
        # settled at the metered cost: 10*15 + 100*75 per M
        self.assertEqual(self.ledger().held["o1"], 0.0)
        self.assertEqual(self.ledger().inflight["o1"], 0)
        self.assertAlmostEqual(self.ledger().spent["o1"], 0.00765, places=8)
        rec = self.last_api()
        self.assertAlmostEqual(rec["usd"], 0.00765, places=8)
        self.assertGreater(rec["reserve_usd"], 0.6)
        # with the reservation released, the same call fits again
        t, out, go = self.start(self.OPUS)
        self.finish(t, go)
        self.assertEqual(out["status"], 200)
        self.assertAlmostEqual(self.ledger().spent["o1"], 0.0153, places=8)

    def test_max_inflight_refuses_the_next_call(self) -> None:
        cheap = {"model": "claude-opus-4-1", "max_tokens": 10, "stream": True}
        a = self.start(cheap)
        b = self.start(cheap)
        st, body = self.slow_post(cheap)
        self.assertEqual(st, 403)
        self.assertIn("2 calls already in flight for session o1 (max_inflight 2)", json.loads(body)["error"]["message"])
        self.assertEqual(HoldUpstream.posts, 2)
        # another session has its own count
        self.assertEqual(self.post("other", self.caps["o2"]), 200)
        for t, _, go in (a, b):
            self.finish(t, go)
        self.assertEqual(self.ledger().inflight["o1"], 0)
        t, out, go = self.start(cheap)
        self.finish(t, go)
        self.assertEqual(out["status"], 200)

    def test_aborted_stream_is_billed_at_the_floor(self) -> None:
        HoldUpstream.mode = "abort"
        t, out, go = self.start(self.OPUS)
        self.finish(t, go)
        rec = self.last_api()
        self.assertTrue(rec["aborted"])
        self.assertIn("floor", rec["reason"])
        metered = (10 * 15 + 1 * 75) / 1_000_000
        floor = tether.floor_usd(self.policy.prices, "claude-opus-4-1", rec["bytes_up"], rec["bytes_down"])
        self.assertGreater(rec["bytes_down"], 40000)
        self.assertGreater(floor, metered)
        self.assertAlmostEqual(rec["usd"], round(floor, 6), places=8)
        self.assertAlmostEqual(self.ledger().spent["o1"], rec["usd"], places=8)
        self.assertEqual((self.ledger().held["o1"], self.ledger().inflight["o1"]), (0.0, 0))

    def test_reserve_usd(self) -> None:
        prices = {"claude-opus": [15, 75], "default": [1, 1]}
        body = json.dumps({"model": "claude-opus-4-1", "max_tokens": 100}).encode()
        self.assertAlmostEqual(tether.reserve_usd(prices, body), (len(body) / 4 * 15 + 100 * 75) / 1e6)
        # no output limit in the body: DEFAULT_MAX_TOKENS
        body = json.dumps({"model": "claude-opus-4-1"}).encode()
        want = (len(body) / 4 * 15 + tether.DEFAULT_MAX_TOKENS * 75) / 1e6
        self.assertAlmostEqual(tether.reserve_usd(prices, body), want)
        # OpenAI Responses names it max_output_tokens
        body = json.dumps({"model": "gpt-x", "max_output_tokens": 7}).encode()
        self.assertAlmostEqual(tether.reserve_usd(prices, body), (len(body) / 4 + 7) / 1e6)
        # not JSON, or no body: input only
        self.assertAlmostEqual(tether.reserve_usd(prices, b"abcd" * 10), 10 / 1e6)
        self.assertEqual(tether.reserve_usd(prices, None), 0.0)

    def test_reserve_off_admits_every_call_in_flight(self) -> None:
        # check-then-charge: nothing is spent while both are in flight, so both
        # get in and the session ends above its cap; the next call is refused
        self.policy.reserve = False
        a = self.start(self.OPUS)
        b = self.start(self.OPUS)
        self.assertEqual(HoldUpstream.posts, 2)
        self.assertEqual(self.ledger().held["o1"], 0.0)
        for t, out, go in (a, b):
            self.finish(t, go)
            self.assertEqual(out["status"], 200)
        self.assertAlmostEqual(self.ledger().spent["o1"], 0.0153, places=8)
        self.ledger().spent["o1"] = 1.2  # as if the calls had cost their worst case
        st, body = self.slow_post(self.OPUS)
        self.assertEqual(st, 403)
        self.assertIn("reached", json.loads(body)["error"]["message"])
        # max_inflight still holds with reservations off
        self.ledger().spent["o1"] = 0.0
        a = self.start(self.OPUS)
        b = self.start(self.OPUS)
        st, _ = self.slow_post(self.OPUS)
        self.assertEqual(st, 403)
        for t, _, go in (a, b):
            self.finish(t, go)

    def test_settle_is_idempotent(self) -> None:
        led = self.ledger()
        r, why, _ = led.reserve("o1", 0.5, 1.0, None, 4)
        self.assertIsNone(why)
        assert r is not None
        led.settle(r, 0.1)
        led.settle(r, 0.1)
        self.assertEqual((led.spent["o1"], led.held["o1"], led.inflight["o1"]), (0.1, 0.0, 0))


class CompileTest(unittest.TestCase):
    """tether compile: one rl/poc-1 file -> msb flags, nono profiles, tether config."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        src = json.loads((HERE / "spike/msb/poc-policy.json").read_text())
        # Never touch the keychain from a test; the compiler only names credentials anyway.
        src["gateway"]["credentials"] = {"anthropic": "env:T_ANTHROPIC", "github": "env:T_GH"}
        src["state_dir"] = "state"
        self.src = src

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def compile(self, cfg: dict[str, Any] | None = None) -> tuple[dict[str, Any], Any]:
        p = self.dir / "src.json"
        p.write_text(json.dumps(cfg if cfg is not None else self.src))
        return tether.compile_policy(p)

    def errors(self, mutate: Callable[[dict[str, Any]], None]) -> str:
        cfg = json.loads(json.dumps(self.src))
        mutate(cfg)
        _, lints = self.compile(cfg)
        return "\n".join(lints.errors)

    def test_repo_policy_compiles_clean(self) -> None:
        out, lints = tether.compile_policy(HERE / "spike/msb/poc-policy.json")
        self.assertEqual(lints.errors, [])
        args = out["msb_args"]
        self.assertEqual(args[:4], ["create", "node:22-bookworm", "--name", "tether-cage"])
        joined = " ".join(args)
        self.assertIn("--net-default deny --net-rule allow@host:tcp:18001 --tmpfs /run/tether", joined)
        self.assertNotIn("--secret", joined)
        self.assertEqual(out["caps"], [(a, t) for a in ("claude", "codex") for t in ("agent", "git", "npm")])
        self.assertEqual(out["machine_env"]["CAGE_AGENTS"], "claude codex")
        # keychain: names become cmd: specs; nothing was resolved
        cred = out["tether"]["routes"]["anthropic"]["credential"]
        self.assertEqual(cred, "cmd:security find-generic-password -s tether-anthropic -w")
        self.assertEqual(out["settings"], {"claude": {"skipWebFetchPreflight": True}})

    def test_spend_reserve_knob(self) -> None:
        out, lints = self.compile()
        self.assertIs(out["tether"]["spend"]["reserve"], True)
        self.assertFalse([w for w in lints.warnings if "reserve" in w])
        cfg = json.loads(json.dumps(self.src))
        cfg["gateway"].setdefault("spend", {})["reserve"] = False
        out, lints = self.compile(cfg)
        self.assertEqual(lints.errors, [])
        self.assertIs(out["tether"]["spend"]["reserve"], False)
        self.assertIn("gateway.spend.reserve: off; calls in flight can all overshoot the cap", lints.warnings)
        p = self.dir / "tether.json"
        p.write_text(json.dumps(out["tether"]))
        self.assertFalse(tether.Policy(p).reserve)

    def test_nono_profile_shape(self) -> None:
        out, lints = self.compile()
        self.assertEqual(lints.errors, [])
        prof = out["nono"]["claude"]
        self.assertEqual(prof["network"], {"connect_port": [18001]})
        self.assertNotIn("/run/tether", json.dumps(prof["filesystem"]))
        self.assertIn("/home/claude/.npm", prof["filesystem"]["allow"])
        self.assertEqual(prof["filesystem"]["read"], ["/usr", "/etc", "/opt/nono", "/opt/tether"])
        cmds = prof["command_policies"]["commands"]
        git = cmds["git"]["sandbox"]
        self.assertEqual(git["argv_prepend"][:4], ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"])
        self.assertIn("include.path=/run/tether/claude/git", git["argv_prepend"])
        self.assertEqual(cmds["git-remote-http"]["from"]["session"], "deny")
        self.assertIn("--userconfig", cmds["npm"]["sandbox"]["argv_prepend"])
        creds = prof["command_policies"]["credentials"]
        self.assertEqual(creds["npm-cap"], {"type": "raw-file", "path": "/run/tether/claude/npm"})

    def test_npm_scripts_edge(self) -> None:
        # Off by default: no sh edge, so npm's `sh -c` hits nono's exec gate.
        for a in ("claude", "codex"):
            cfg = json.loads(json.dumps(self.src))
            cfg["agents"][a]["tools"]["npm"].pop("scripts", None)
            out, lints = self.compile(cfg)
            self.assertEqual(lints.errors, [])
            cmds = out["nono"][a]["command_policies"]["commands"]
            self.assertNotIn("sh", cmds)
            self.assertNotIn("can_use", cmds["npm"])
        cfg = json.loads(json.dumps(self.src))
        cfg["agents"]["claude"]["tools"]["npm"]["scripts"] = True
        out, lints = self.compile(cfg)
        self.assertEqual(lints.errors, [])
        cmds = out["nono"]["claude"]["command_policies"]["commands"]
        self.assertEqual(cmds["npm"]["can_use"], ["sh"])
        self.assertEqual(cmds["sh"]["executable"], "/bin/sh")
        self.assertEqual(cmds["sh"]["from"]["session"], "deny")
        box = cmds["sh"]["from"]["npm"]["sandbox"]
        # npm's files, but no cap, no network, and no npm_config_* (its proxy vars hold the cap)
        self.assertEqual(box["fs_write"], cmds["npm"]["sandbox"]["fs_write"])
        self.assertNotIn("use_credentials", box)
        self.assertNotIn("network", box)
        env = box["environment"]["allow_vars"]
        self.assertFalse([v for v in env if v.startswith("npm_config") or v in ("*", "npm_*")])
        self.assertIn("npm_package_*", env)

    def test_npm_scripts_lints(self) -> None:
        def on_git(c: dict[str, Any]) -> None:
            c["agents"]["claude"]["tools"]["git"]["scripts"] = True

        def not_bool(c: dict[str, Any]) -> None:
            c["agents"]["claude"]["tools"]["npm"]["scripts"] = "yes"

        self.assertIn("only npm runs lifecycle scripts", self.errors(on_git))
        self.assertIn("scripts: true or false", self.errors(not_bool))

    def test_compiled_config_loads_and_mints_by_grant(self) -> None:
        os.environ["T_ANTHROPIC"] = REAL_KEY
        os.environ["T_GH"] = GH_TOKEN
        out, lints = self.compile()
        self.assertEqual(lints.errors, [])
        files = tether.write_compiled(out, self.dir / "out")
        self.assertIn(self.dir / "out/nono/codex.json", files)
        cfg = self.dir / "out/tether.json"
        pol = tether.Policy(cfg)
        self.assertEqual(list(pol.sandboxes), ["tether-cage"])
        self.assertEqual(pol.sandboxes["tether-cage"].listen, "127.0.0.1:18001")
        self.assertEqual(pol.cap_session, 2.0)
        self.assertNotIn(REAL_KEY, cfg.read_text())

        def cli(*a: str) -> str:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                tether.main(["-c", str(cfg), *a])
            return buf.getvalue()

        git = cli("cap", "mint", "--session", "cage", "--grant", "claude/git", "--env", "-")
        self.assertIn("extraHeader = Authorization: Basic", git)
        npm = cli("cap", "mint", "--session", "cage", "--grant", "codex/npm", "--env", "-")
        self.assertIn("https-proxy=http://agent:tth_", npm)
        env = cli("cap", "mint", "--session", "cage", "--grant", "codex", "--env", "-")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertIn("HTTPS_PROXY", env)
        recs = {(r["agent"], r["tool"]): r for r in json.loads(cli("cap", "list", "--json"))}
        self.assertEqual(recs[("claude", "git")]["routes"], ["github"])
        self.assertEqual(recs[("claude", "git")]["egress"], [])
        self.assertEqual(recs[("codex", None)]["egress"], ["pypi.org"])
        for bad in (["--grant", "nobody"], ["--grant", "claude/pip"], ["--grant", "claude", "--route", "github"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cli("cap", "mint", "--session", "cage", *bad)

    def test_subset_checks(self) -> None:
        def agent_egress(c: dict[str, Any]) -> None:
            c["agents"]["claude"]["egress"].append("evil.example")

        self.assertIn("agents.claude: egress evil.example is wider than the ceiling", self.errors(agent_egress))

        def tool_route(c: dict[str, Any]) -> None:
            c["ceiling"]["routes"] = ["anthropic"]

        e = self.errors(tool_route)
        self.assertIn("agents.claude.tools.git: route github is not in the ceiling", e)
        self.assertIn("ceiling.routes", self.errors(lambda c: c["ceiling"]["routes"].append("openai")))

        def tool_fs(c: dict[str, Any]) -> None:
            c["agents"]["codex"]["tools"]["npm"]["fs"]["write"].append("/srv")

        self.assertIn("agents.codex.tools.npm.fs.write: /srv is outside codex's grants", self.errors(tool_fs))

        def tool_home(c: dict[str, Any]) -> None:
            c["agents"]["codex"]["tools"]["git"]["fs"]["write"].append("~/.ssh")

        self.assertIn("/home/codex/.ssh is outside codex's grants", self.errors(tool_home))

        def spend(c: dict[str, Any]) -> None:
            c["agents"]["claude"]["spend_usd"] = 5
            c["gateway"]["spend"]["session_usd_per_day"] = 3

        e = self.errors(spend)
        self.assertIn("above the ceiling's $2.0", e)
        self.assertIn("agents.claude: spend_usd $5 is above the session cap", e)

    def test_wildcard_and_route_lints(self) -> None:
        for pat, why in (
            ("*.s3.amazonaws.com", "amazonaws.com"),
            ("*.amazonaws.com", "amazonaws.com"),
            ("*.core.windows.net", "blob.core.windows.net"),
            ("*.ngrok-free.app", "ngrok-free.app"),
            ("*.com", "public suffix"),
            ("*", "literal host"),
            ("registry.*.org", "literal host"),
            ("pypi.or?", "literal host"),
        ):
            self.assertIn(why, tether.egress_lint(pat) or "", pat)
        for ok in ("registry.npmjs.org", "*.pythonhosted.org"):
            self.assertIsNone(tether.egress_lint(ok), ok)

        def wide(c: dict[str, Any]) -> None:
            c["ceiling"]["egress"].append("*.s3.amazonaws.com")
            c["agents"]["claude"]["egress"].append("*.s3.amazonaws.com")

        e = self.errors(wide)
        self.assertIn("ceiling.egress: *.s3.amazonaws.com: wildcard over amazonaws.com", e)
        self.assertIn("agents.claude: egress *.s3.amazonaws.com", e)

        def route_host(c: dict[str, Any]) -> None:
            c["ceiling"]["egress"].append("*.anthropic.com")

        self.assertIn("ceiling: egress *.anthropic.com covers api.anthropic.com", self.errors(route_host))
        self.assertIn("covers github.com", self.errors(lambda c: c["ceiling"]["egress"].append("github.com")))

        def owner_glob(c: dict[str, Any]) -> None:
            c["gateway"]["routes"]["github"]["paths"] = ["/*/info/refs", "/metcalfc/**"]

        e = self.errors(owner_glob)
        self.assertIn("path /*/info/refs: the first segment must be literal", e)
        self.assertIn("path /metcalfc/**: '**'", e)
        self.assertIn("list its paths", self.errors(lambda c: c["gateway"]["routes"]["github"].pop("paths")))

        def no_cap(c: dict[str, Any]) -> None:
            del c["gateway"]["spend"]["session_usd_per_day"]
            del c["ceiling"]["spend"]

        self.assertIn("route anthropic is metered and granted, but there is no session spend cap", self.errors(no_cap))

    def test_shape_and_fs_lints(self) -> None:
        def deny(c: dict[str, Any]) -> None:
            c["agents"]["claude"]["tools"]["npm"]["fs"]["deny"] = ["/work/.git"]

        self.assertIn("Landlock has no deny", self.errors(deny))
        self.assertIn("too wide", self.errors(lambda c: c["agents"]["claude"]["fs"]["write"].append("~")))
        self.assertIn("~/.config", self.errors(lambda c: c["agents"]["claude"]["fs"]["write"].append("~/.config/gh")))
        self.assertIn("unknown key 'egres'", self.errors(lambda c: c["agents"]["codex"].update(egres=["x"])))
        self.assertIn("only 'laptop'", self.errors(lambda c: c.update(placement={"cloud": "gcp"})))
        self.assertIn("deny_private", self.errors(lambda c: c["gateway"]["egress"].update(deny_private=False)))
        self.assertIn("knows how to sandbox", self.errors(lambda c: c["agents"]["codex"]["tools"].update(pip={})))
        no_github = self.errors(lambda c: c["agents"]["codex"]["tools"]["git"].pop("routes"))
        self.assertIn("a git cap needs the github route", no_github)
        self.assertIn("schema", self.errors(lambda c: c.update(schema="rl/poc-2")))
        # a policy with errors writes nothing
        cfg = json.loads(json.dumps(self.src))
        cfg["agents"]["claude"]["egress"].append("evil.example")
        p = self.dir / "bad.json"
        p.write_text(json.dumps(cfg))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tether.main(["compile", str(p), "-o", str(self.dir / "never")])
        self.assertFalse((self.dir / "never").exists())
