"""One version, everywhere it is shown, and one honest answer about discovery.

Both halves of this file exist because of what the same process does to a
meeting. Agora is developed inside a meeting held in Agora, so a change can land
under people mid-conversation and "which build am I looking at" has to have one
answer rather than three. It had three: `agora/__init__.py` said 0.1.0,
`SERVER_INFO` said 0.1.0 and the HTTP `Server:` header said Agora/0.2 — D9.

The second half is D3 wearing a container: inside one, `~/.claude/sessions` does
not exist, so the roster is empty and every session reads as offline. That must
not render as "nobody is running".
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

import agora
from agora.discovery import availability
from agora.mcp import SERVER_INFO

ROOT = Path(__file__).resolve().parent.parent


def test_the_version_is_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", agora.__version__), \
        "the image tag follows this string; it has to be a plain version"


def test_mcp_initialize_reports_the_package_version(server):
    """The first thing an agent learns about the server it just connected to."""
    result = server.rpc("initialize", {"protocolVersion": "2025-06-18",
                                       "clientInfo": {"name": "t", "version": "1"}})
    assert result["serverInfo"]["version"] == agora.__version__
    assert SERVER_INFO["version"] == agora.__version__


def test_the_state_the_page_renders_carries_the_version(server):
    """The chair's copy. Criterion: answerable without leaving the browser."""
    status, state = server.get("/api/state")
    assert status == 200
    assert state["version"] == agora.__version__


def test_the_http_server_header_agrees_with_the_package(server):
    """The third of the three numbers that used to disagree."""
    _, _, headers = server.raw("/api/state")
    assert headers["Server"].startswith(f"Agora/{agora.__version__}")


def test_the_page_has_somewhere_to_put_the_version():
    """`$("ver")` on a missing id throws and kills the rest of the state
    handler, which in a browser looks like the roster simply not updating."""
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="ver"' in page
    assert "S.version" in page


def test_the_dockerfile_does_not_hardcode_a_version():
    """The image tag has to follow `agora/__version__` rather than be typed
    beside it — a second literal is how the three numbers happened."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG AGORA_VERSION" in dockerfile
    body = "\n".join(l for l in dockerfile.splitlines() if not l.startswith("#"))
    assert agora.__version__ not in body, \
        "the Dockerfile writes the version out again; pass it as a build arg"


def test_the_container_installs_nothing():
    """Zero runtime dependencies (D2). A pip stage here would be the place that
    quietly stops being true, and no import-reading test would catch it."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    body = "\n".join(l for l in dockerfile.splitlines() if not l.startswith("#"))
    for forbidden in ("pip install", "apt-get install", "requirements"):
        assert forbidden not in body, f"the image installs something: {forbidden}"


# ---- discovery says why it is empty ----------------------------------------

def test_a_readable_empty_registry_is_available_and_says_nothing(tmp_path):
    """An empty directory is a true and different fact from an unreadable one:
    it really does mean nobody is running, so nothing is warned about."""
    present = tmp_path / "sessions"
    present.mkdir(exist_ok=True)
    seen = availability(present)
    assert seen["available"] is True
    assert seen["reason"] == "" and seen["note"] == ""


def test_a_missing_registry_says_so_rather_than_returning_nothing(tmp_path):
    """The container case. `claude_sessions` returning [] is correct and
    useless on its own: an empty roster reads as "nobody is running"."""
    gone = tmp_path / "not-mounted"
    seen = availability(gone)
    assert seen["available"] is False
    assert str(gone) in seen["path"]
    assert seen["reason"], "an unavailable registry must say why"


def test_the_state_the_page_renders_says_the_registry_is_missing(server, monkeypatch):
    from agora import discovery
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS",
                        Path("/definitely/not/mounted/sessions"))
    _, state = server.get("/api/state")
    assert state["discovery"]["available"] is False
    assert state["roster"] == []
    assert state["discovery"]["reason"]


def test_a_registry_whose_pids_resolve_to_nothing_says_so(tmp_path):
    """The container case that mounting the directory does NOT fix: the files
    are readable and every pid in them belongs to the host, so the roster is
    empty while `available` is true. Silence here would be a false all-clear."""
    present = tmp_path / "sessions"
    present.mkdir(exist_ok=True)
    (present / "s.json").write_text(json.dumps({
        "pid": 999_999, "sessionId": "s1", "name": "ghost-1",
        "cwd": "/x", "updatedAt": time.time() * 1000}), encoding="utf-8")
    seen = availability(present)
    assert seen["available"] is True     # the directory is readable
    assert seen["files"] == 1 and seen["live"] == 0
    assert seen["note"], ("a readable registry that yields nobody must say so; "
                          "an empty roster otherwise reads as 'nobody is running'")


def test_the_page_renders_the_reason_instead_of_an_empty_roster():
    """A banner nobody paints is the same defect as no banner (D3)."""
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "S.discovery" in page, "the page must read the availability flag"
    paint = re.search(r"function paintRoster\(\)\{(.*?)\n\}", page, re.S)
    assert paint, "paintRoster is gone"
    body = paint.group(1)
    assert "d.reason" in body and "d.note" in body, \
        "the roster must show both failures: unreadable, and readable but empty"
    assert "!reachable.length && !trouble" in body, \
        "'nobody can be called' must not be shown when the real answer is " \
        "'the registry told me nothing'"


# ---- the URL a client dials is not always the one the server binds ----------

def _advertised(argv: list[str], root: Path) -> str:
    """Run `main`'s URL calculation without leaving a server running.

    The socket layer is stubbed rather than bound: what is under test is which
    URL is handed to `Agora` (and so to the invite text), not the listen.
    """
    import agora.server as srv

    class _FakeServer:
        daemon_threads = True

        def __init__(self, addr, handler):
            self.addr = addr

        def serve_forever(self):
            raise KeyboardInterrupt   # main's normal shutdown path

        def server_close(self):
            pass

    seen: dict[str, str] = {}
    real = srv.Agora

    def _record(app_root, url):
        seen["url"] = url
        return real(app_root, url)

    old = srv.ThreadingHTTPServer, srv.Agora
    srv.ThreadingHTTPServer, srv.Agora = _FakeServer, _record
    try:
        srv.main([*argv, "--no-open", "--root", str(root)])
    finally:
        srv.ThreadingHTTPServer, srv.Agora = old
    return seen["url"]


@pytest.mark.parametrize("argv,expected", [
    (["--host", "0.0.0.0", "--port", "8765"], "http://127.0.0.1:8765"),
    (["--host", "0.0.0.0", "--public-url", "http://127.0.0.1:8766/"],
     "http://127.0.0.1:8766"),
    (["--host", "127.0.0.1", "--port", "9000"], "http://127.0.0.1:9000"),
])
def test_the_advertised_url_is_one_a_client_can_dial(argv, expected, tmp_path,
                                                     monkeypatch):
    """A container binds 0.0.0.0 and is published on the host loopback, often on
    another port. The advertised URL is copied by hand into `claude mcp add`, so
    `http://0.0.0.0:8765/mcp` is a registration that reaches nothing."""
    monkeypatch.delenv("AGORA_PUBLIC_URL", raising=False)  # a real one would win
    assert _advertised(argv, tmp_path) == expected


def test_the_public_url_can_come_from_the_environment(tmp_path, monkeypatch):
    """The container sets it as an env var; there is no command line to edit."""
    monkeypatch.setenv("AGORA_PUBLIC_URL", "http://127.0.0.1:8766")
    assert _advertised(["--host", "0.0.0.0"], tmp_path) == "http://127.0.0.1:8766"
