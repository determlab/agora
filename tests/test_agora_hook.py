"""The wake half of hooks/agora_hook.py, against a fake Zulip on a real socket.

The hook's whole contract is at the HTTP seam — register, long-poll events,
re-register on BAD_EVENT_QUEUE_ID, exit 2 on a hit — so it is tested through
urllib against a live server rather than by patching its HTTP function.
The hook is also run as a subprocess once, the way Claude Code runs it.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "agora_hook.py"
BOT = "coo-bot@zulip.localhost"


def _load():
    spec = importlib.util.spec_from_file_location("agora_hook_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeZulip:
    """Serves a scripted sequence of /events answers and records every call."""

    def __init__(self, events_script: list, register_script: list = ()) -> None:
        self.script = list(events_script)
        self.register_script = list(register_script)
        self.calls: list[tuple[str, str, dict]] = []
        self.queues = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                fake.calls.append(("POST", self.path, form))
                if fake.register_script:
                    return self._answer(*fake.register_script.pop(0))
                fake.queues += 1
                self._answer(200, {"result": "success",
                                   "queue_id": f"q{fake.queues}",
                                   "last_event_id": -1})

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                fake.calls.append(("GET", url.path, q))
                status, body = fake.script.pop(0) if fake.script else (
                    200, {"result": "success", "events": []})
                if status == "truncated":
                    # Promise more bytes than are sent, then hang up: what a
                    # Zulip restart mid-poll looks like to urllib.
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.write(b'{"result": "succ')
                    self.close_connection = True
                    return
                self._answer(status, body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(*events) -> tuple[int, dict]:
    return 200, {"result": "success", "events": list(events)}


def heartbeat(i: int) -> dict:
    return {"type": "heartbeat", "id": i}


def mention(i: int, content: str = "please look at the record shape") -> dict:
    return {"type": "message", "id": i, "flags": ["mentioned"], "message": {
        "type": "stream", "display_recipient": "coo",
        "subject": "#141 record shape", "content": content,
        "sender_email": "founder@zulip.localhost", "sender_full_name": "Founder"}}


@pytest.fixture
def hook(monkeypatch, tmp_path):
    mod = _load()
    monkeypatch.setattr(mod, "_identity", lambda sid, patience=0.0: {
        "name": "COO", "cwd": ".", "pid": 1, "session_id": sid})
    monkeypatch.setattr(mod, "_payload", lambda: {"session_id": "s1"})
    monkeypatch.setattr(mod, "DOTENV", tmp_path / "missing.env")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setenv("ZULIP_COO_EMAIL", BOT)
    monkeypatch.setenv("ZULIP_COO_API_KEY", "test-key")
    monkeypatch.setattr(mod, "WAIT_SECONDS", 5)
    return mod


def _serve(monkeypatch, script, register_script=()):
    fake = FakeZulip(script, register_script)
    monkeypatch.setenv("ZULIP_SITE", fake.url)
    return fake


def test_mention_prints_message_stream_topic_and_exits_2(hook, monkeypatch, capsys):
    fake = _serve(monkeypatch, [ok(heartbeat(0)), ok(mention(1))])
    try:
        assert hook.do_wait() == 2
    finally:
        fake.close()
    out = capsys.readouterr().out
    assert "@COO in #coo › #141 record shape" in out
    assert "please look at the record shape" in out
    # registered for messages only, raw text rather than rendered HTML
    method, path, form = fake.calls[0]
    assert (method, path) == ("POST", "/api/v1/register")
    assert json.loads(form["event_types"]) == ["message"]
    assert form["apply_markdown"] == "false"
    # last_event_id advanced past the heartbeat before the next poll
    polls = [c[2] for c in fake.calls if c[0] == "GET"]
    assert [p["last_event_id"] for p in polls] == ["-1", "0"]


def test_direct_message_wakes_but_own_and_unmentioned_posts_do_not(
        hook, monkeypatch, capsys):
    own = mention(1)
    own["message"]["sender_email"] = BOT
    chatter = {**mention(2), "flags": []}
    dm = {"type": "message", "id": 3, "flags": [], "message": {
        "type": "private", "content": "are you there?",
        "sender_email": "founder@zulip.localhost", "sender_full_name": "Founder"}}
    fake = _serve(monkeypatch, [ok(own, chatter), ok(dm)])
    try:
        assert hook.do_wait() == 2
    finally:
        fake.close()
    out = capsys.readouterr().out
    assert "Direct message to @COO from Founder" in out
    assert "are you there?" in out
    assert "please look" not in out


def test_bad_event_queue_id_registers_again_and_continues(hook, monkeypatch):
    gone = (400, {"result": "error", "code": "BAD_EVENT_QUEUE_ID",
                  "msg": "Bad event queue ID: q1"})
    fake = _serve(monkeypatch, [gone, ok(mention(7))])
    try:
        assert hook.do_wait() == 2
    finally:
        fake.close()
    registers = [c for c in fake.calls if c[0] == "POST"]
    polls = [c[2] for c in fake.calls if c[0] == "GET"]
    assert len(registers) == 2
    assert [p["queue_id"] for p in polls] == ["q1", "q2"]


def test_malformed_answers_do_not_end_the_wait(hook, monkeypatch, capsys):
    """A server that misbehaves is a server to wait out, not a reason to stop
    listening for the rest of the session."""
    fake = _serve(
        monkeypatch,
        [("truncated", None),
         ok({"type": "heartbeat", "id": "x"},  # bad id: skipped
            {"type": "message", "id": 3, "flags": ["mentioned"]}),  # no message
         ok(mention(4))],
        register_script=[(200, {"result": "success"}),  # no queue_id
                         (200, "not an object"),
                         (500, {"result": "error", "msg": "down"})])
    try:
        assert hook.do_wait() == 2
    finally:
        fake.close()
    assert "please look at the record shape" in capsys.readouterr().out
    polls = [c[2] for c in fake.calls if c[0] == "GET"]
    assert [p["last_event_id"] for p in polls] == ["-1", "-1", "3"]


def test_deadline_exits_0_and_prints_nothing(hook, monkeypatch, capsys):
    clock = iter(range(0, 1000, 1))
    monkeypatch.setattr(hook.time, "time", lambda: float(next(clock)))
    fake = _serve(monkeypatch, [])  # heartbeat-free empty polls forever
    try:
        assert hook.do_wait() == 0
    finally:
        fake.close()
    assert capsys.readouterr().out == ""


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_unreachable_zulip_raises_nothing_and_prints_nothing(hook, monkeypatch, capsys):
    monkeypatch.setenv("ZULIP_SITE", f"http://127.0.0.1:{_dead_port()}")
    clock = iter(range(0, 1000, 1))
    monkeypatch.setattr(hook.time, "time", lambda: float(next(clock)))
    assert hook.do_wait() == 0
    assert capsys.readouterr().out == ""


def test_no_bot_for_this_session_exits_0_without_calling_zulip(hook, monkeypatch):
    monkeypatch.delenv("ZULIP_COO_EMAIL")
    monkeypatch.delenv("ZULIP_COO_API_KEY")
    monkeypatch.setattr(hook, "_zulip", lambda *a, **k: pytest.fail("called Zulip"))
    assert hook.do_wait() == 0


def test_credentials_come_from_bot_dotenv(hook, monkeypatch, tmp_path):
    monkeypatch.delenv("ZULIP_COO_EMAIL")
    monkeypatch.delenv("ZULIP_COO_API_KEY")
    monkeypatch.delenv("ZULIP_SITE", raising=False)
    env = tmp_path / ".env"
    env.write_text("# bot keys\nZULIP_COO_EMAIL=coo-bot@x\nZULIP_COO_API_KEY=k\n",
                   encoding="utf-8")
    monkeypatch.setattr(hook, "DOTENV", env)
    assert hook._zulip_creds("COO") == ("http://zulip.localhost:8090", "coo-bot@x", "k")
    assert hook._zulip_creds("agora coder") is None


def test_run_as_a_script_with_zulip_down_exits_0_silently(tmp_path):
    """The way Claude Code runs it: a process, a JSON payload on stdin."""
    home = tmp_path / "home"
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "1.json").write_text(json.dumps(
        {"sessionId": "s1", "name": "COO", "pid": 1, "cwd": str(tmp_path)}),
        encoding="utf-8")
    env = {"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
           "HOME": str(home), "USERPROFILE": str(home),
           "ZULIP_SITE": f"http://127.0.0.1:{_dead_port()}",
           "ZULIP_COO_EMAIL": BOT, "ZULIP_COO_API_KEY": "k",
           "AGORA_URL": f"http://127.0.0.1:{_dead_port()}"}
    # A tiny wrapper shortens the 11h deadline without editing the hook.
    runner = tmp_path / "run.py"
    runner.write_text(
        "import runpy, sys\n"
        f"sys.argv = [{str(HOOK)!r}, 'wait']\n"
        f"g = runpy.run_path({str(HOOK)!r}, run_name='hook')\n"
        "g['main'].__globals__['WAIT_SECONDS'] = 3\n"
        "raise SystemExit(g['main']())\n", encoding="utf-8")
    for mode_args in ([str(HOOK), "register"], [str(runner)]):
        done = subprocess.run([sys.executable, *mode_args],
                              input=json.dumps({"session_id": "s1"}),
                              capture_output=True, text=True, env=env, timeout=60)
        assert done.returncode == 0, done.stderr
        assert done.stdout == ""
        assert done.stderr == ""
