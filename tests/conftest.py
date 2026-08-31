"""Fixtures: an isolated hub, and a real server on a real socket.

The API tests talk to a live `ThreadingHTTPServer` over HTTP rather than calling
handler methods directly. That is deliberate — most of what broke in this app
broke at the protocol seam (HTTP/1.0 closing long polls, a 204 with a body
desyncing keep-alive, an SSE stream with no Content-Length), and none of those
are reachable by calling a method.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agora import discovery
from agora.room import Hub
from agora.server import Agora, Handler, ThreadingHTTPServer


@pytest.fixture
def hub(tmp_path: Path) -> Hub:
    return Hub(tmp_path / "rooms")


@pytest.fixture(autouse=True)
def _no_registry(monkeypatch, tmp_path):
    """Point discovery at an empty directory and clear its cache.

    Without this every test would see whatever sessions happen to be running on
    the developer's machine, and would pass or fail depending on that.
    """
    empty = tmp_path / "sessions"
    empty.mkdir()
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", empty)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    yield
    monkeypatch.setattr(discovery, "_cache", (0.0, []))


class Client:
    """A tiny HTTP client. urllib only — the app has no dependencies and its
    tests should not add any."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def _open(self, req, timeout=10.0):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return exc.code, json.loads(body) if body else None

    def get(self, path: str, timeout: float = 10.0):
        return self._open(urllib.request.Request(self.base + path), timeout)

    def post(self, path: str, payload: dict | None = None, timeout: float = 10.0):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        return self._open(req, timeout)

    def raw(self, path: str, timeout: float = 10.0) -> tuple[int, str, dict]:
        req = urllib.request.Request(self.base + path)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)

    def rpc(self, method: str, params: dict | None = None, rpc_id: int = 1,
            timeout: float = 30.0):
        """One JSON-RPC call to /mcp. Returns the `result` or raises on error."""
        status, body = self.post("/mcp", {"jsonrpc": "2.0", "id": rpc_id,
                                          "method": method,
                                          "params": params or {}}, timeout)
        assert status == 200, body
        if "error" in body:
            raise AssertionError(body["error"])
        return body["result"]

    def tool(self, name: str, args: dict | None = None, timeout: float = 30.0):
        """Call a tool and return its decoded payload plus the isError flag."""
        result = self.rpc("tools/call", {"name": name, "arguments": args or {}},
                          timeout=timeout)
        text = result["content"][0]["text"]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        return payload, bool(result.get("isError"))


@pytest.fixture
def server(tmp_path: Path):
    """A live Agora on an ephemeral port, torn down after the test."""
    app = Agora(tmp_path, "http://127.0.0.1:0")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    app.public_url = f"http://127.0.0.1:{port}"

    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = Client(app.public_url)
    client.app = app  # tests occasionally need to reach in
    try:
        yield client
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
