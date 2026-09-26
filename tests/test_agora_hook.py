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
import time
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

    def __init__(self, events_script: list, register_script: list = (),
                 history: list = (), dead_queues: tuple = (),
                 max_message_id: int = -1, idle: float = 0.0) -> None:
        self.script = list(events_script)
        self.register_script = list(register_script)
        self.history = list(history)          # what GET /messages can backfill
        self.dead_queues = set(dead_queues)   # answer BAD_EVENT_QUEUE_ID
        self.max_message_id = max_message_id
        self.idle = idle                      # how long an empty poll parks
        self.page_size = 100                  # GET /messages page size
        self.channel_error = None             # (status, body) for a channel narrow
        # One step per channel-narrow request, then normal answers: "ok",
        # "drop" (hang up with no answer), or a (status, body) to send.
        self.channel_script: list = []
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
                                   "last_event_id": -1,
                                   "max_message_id": fake.max_message_id})

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                fake.calls.append(("GET", url.path, q))
                if url.path == "/api/v1/messages":
                    term = json.loads(q["narrow"])[0]
                    op, narrow = term["operator"], term["operand"]
                    if op in ("channel", "stream") and fake.channel_script:
                        step = fake.channel_script.pop(0)
                        if step == "drop":
                            self.close_connection = True
                            return
                        if step != "ok":
                            return self._answer(*step)
                    elif op in ("channel", "stream") and fake.channel_error:
                        return self._answer(*fake.channel_error)
                    after = int(q["anchor"])
                    found = [m for m in fake.history if m["id"] > after and (
                        (narrow == "mentioned" and m.get("mentioned"))
                        or (narrow == "dm" and m.get("type") == "private")
                        or (op in ("channel", "stream") and m.get("type") == "stream"
                            and m.get("display_recipient") == narrow))]
                    page = found[:fake.page_size]
                    for m in page:
                        m.setdefault("flags", ["mentioned"] if m.get("mentioned") else [])
                    return self._answer(200, {"result": "success", "messages": page,
                                              "found_newest": len(found) <= fake.page_size})
                if q.get("queue_id") in fake.dead_queues:
                    return self._answer(400, {"result": "error",
                                              "code": "BAD_EVENT_QUEUE_ID"})
                if not fake.script:
                    time.sleep(fake.idle)
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
        "id": 1000 + i, "type": "stream", "display_recipient": "coo",
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
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
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
    polls = [c[2] for c in fake.calls if c[1] == "/api/v1/events"]
    assert [p["last_event_id"] for p in polls] == ["-1", "0"]


def test_direct_message_wakes_but_own_and_unmentioned_posts_do_not(
        hook, monkeypatch, capsys):
    own = mention(1)
    own["message"]["sender_email"] = BOT
    chatter = {**mention(2), "flags": []}  # untagged, and not in #coo
    chatter["message"] = {**chatter["message"], "display_recipient": "cto"}
    dm = {"type": "message", "id": 3, "flags": [], "message": {
        "id": 1003, "type": "private", "content": "are you there?",
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
    polls = [c[2] for c in fake.calls if c[1] == "/api/v1/events"]
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
    polls = [c[2] for c in fake.calls if c[1] == "/api/v1/events"]
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
           "HOME": str(home), "USERPROFILE": str(home), "TEMP": str(tmp_path),
           "TMP": str(tmp_path),
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
                              capture_output=True, text=True, encoding="utf-8", env=env,
                              timeout=60)
        assert done.returncode == 0, done.stderr
        assert done.stdout == ""
        assert done.stderr == ""


# -- issue #47: one poller per session, and nothing lost between turns ------
#
# These run `wait` as a subprocess, the way Claude Code runs it on SessionStart
# and on Stop: a JSON payload on stdin, the registry found through the home
# directory, state and lock in %TEMP%.

def _session_env(tmp_path: Path, site: str, name: str = "COO") -> dict:
    import os
    home = tmp_path / "home"
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "1.json").write_text(json.dumps(
        {"sessionId": "s47", "name": name, "pid": 1, "cwd": str(tmp_path)}),
        encoding="utf-8")
    temp = tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    return {"PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "HOME": str(home), "USERPROFILE": str(home),
            "TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp),
            "ZULIP_SITE": site, "ZULIP_COO_EMAIL": BOT, "ZULIP_COO_API_KEY": "k"}


def _wait_proc(tmp_path: Path, env: dict, seconds: float = 8.0,
               pages: int = 50) -> subprocess.Popen:
    """The hook's `wait`, as a process, with the 11h deadline shortened."""
    runner = tmp_path / "run_wait.py"
    runner.write_text(
        "import runpy, sys\n"
        f"sys.argv = [{str(HOOK)!r}, 'wait']\n"
        f"g = runpy.run_path({str(HOOK)!r}, run_name='hook')\n"
        f"g['main'].__globals__['WAIT_SECONDS'] = {seconds}\n"
        f"g['main'].__globals__['BACKFILL_PAGES'] = {pages}\n"
        "raise SystemExit(g['main']())\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(runner)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env)
    proc.stdin.write(json.dumps({"session_id": "s47"}))
    proc.stdin.close()
    return proc


def _finish(proc: subprocess.Popen) -> tuple[int, str, str]:
    out = proc.stdout.read()
    err = proc.stderr.read()
    return proc.wait(timeout=60), out, err


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "temp" / "agora-wait-s47.json").read_text("utf-8"))


def dm(i: int, content: str, sender: str = "founder@zulip.localhost") -> dict:
    return {"type": "message", "id": 100 + i, "flags": [], "message": {
        "id": i, "type": "private", "content": content,
        "sender_email": sender, "sender_full_name": "Founder"}}


def test_two_messages_a_turn_apart_both_wake_and_the_queue_is_resumed(tmp_path):
    """The next run resumes the saved queue from the saved event id; it does
    not register a new one, so what arrived in between is in it."""
    fake = FakeZulip([ok(dm(11, "message one"))], max_message_id=10)
    try:
        env = _session_env(tmp_path, fake.url)
        code, out, err = _finish(_wait_proc(tmp_path, env))
        assert (code, err) == (2, "")
        assert "message one" in out
        assert _state(tmp_path)["queue_id"] == "q1"
        assert _state(tmp_path)["last_message_id"] == 11

        fake.script = [ok(dm(12, "message two, sent while answering"))]
        code, out, err = _finish(_wait_proc(tmp_path, env))
        assert (code, err) == (2, "")
        assert "message two, sent while answering" in out
        assert "message one" not in out
    finally:
        fake.close()
    registers = [c for c in fake.calls if c[1] == "/api/v1/register"]
    polls = [c[2] for c in fake.calls if c[1] == "/api/v1/events"]
    assert len(registers) == 1
    assert (polls[-1]["queue_id"], polls[-1]["last_event_id"]) == ("q1", "111")


def test_a_lost_queue_is_backfilled_from_history(tmp_path):
    """The saved queue expired while the agent was answering. The message
    sent in that gap is only in the history, and it wakes the session just
    like a live one; the bot's own post and older messages do not."""
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "agora-wait-s47.json").write_text(json.dumps(
        {"queue_id": "q-old", "last_event_id": 5, "last_message_id": 20,
         "backfill": False}), encoding="utf-8")
    history = [
        {"id": 19, "type": "stream", "mentioned": True, "content": "too early",
         "sender_email": "founder@zulip.localhost", "display_recipient": "coo",
         "subject": "t"},
        {"id": 21, "type": "stream", "mentioned": True,
         "content": "sent in the gap", "display_recipient": "coo",
         "subject": "#47 reach", "sender_email": "founder@zulip.localhost",
         "sender_full_name": "Founder"},
        {"id": 22, "type": "private", "content": "my own reply",
         "sender_email": BOT, "sender_full_name": "COO"},
    ]
    fake = FakeZulip([], history=history, dead_queues=("q-old",))
    try:
        code, out, err = _finish(_wait_proc(tmp_path, _session_env(tmp_path, fake.url)))
    finally:
        fake.close()
    assert (code, err) == (2, "")
    assert "@COO in #coo › #47 reach — from Founder" in out
    assert "sent in the gap" in out
    assert "too early" not in out
    assert "my own reply" not in out
    assert _state(tmp_path)["last_message_id"] == 22


def test_a_message_already_backfilled_does_not_wake_twice(tmp_path):
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "agora-wait-s47.json").write_text(json.dumps(
        {"queue_id": "q1", "last_event_id": 5, "last_message_id": 30,
         "backfill": False}), encoding="utf-8")
    # The queue still carries message 30, already delivered by a backfill.
    fake = FakeZulip([ok(dm(30, "delivered before")), ok(dm(31, "brand new"))])
    try:
        code, out, _ = _finish(_wait_proc(tmp_path, _session_env(tmp_path, fake.url)))
    finally:
        fake.close()
    assert code == 2
    assert "brand new" in out and "delivered before" not in out


def test_second_wait_exits_0_at_once_while_the_first_is_parked(tmp_path):
    """Stop fires after every turn. Without the lock each turn would stack a
    second poller, and one mention would wake the session twice."""
    fake = FakeZulip([], idle=0.5)
    lock = tmp_path / "temp" / "agora-wait-s47.lock"
    try:
        env = _session_env(tmp_path, fake.url)
        first = _wait_proc(tmp_path, env, seconds=6)
        for _ in range(100):
            if lock.exists() and lock.read_text("utf-8").strip():
                break
            time.sleep(0.1)
        assert lock.read_text("utf-8").strip() == str(first.pid)
        started = time.time()
        code, out, err = _finish(_wait_proc(tmp_path, env, seconds=6))
        assert (code, out, err) == (0, "", "")
        assert time.time() - started < 5
        assert first.poll() is None  # the first is still parked
        code, out, _ = _finish(first)
        assert (code, out) == (0, "")
    finally:
        fake.close()
    assert len([c for c in fake.calls if c[1] == "/api/v1/register"]) == 1
    assert not lock.exists()  # released on the way out


def test_a_stale_lock_from_a_dead_poller_is_replaced(tmp_path):
    temp = tmp_path / "temp"
    temp.mkdir()
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True).stdout.strip()
    (temp / "agora-wait-s47.lock").write_text(dead, encoding="utf-8")
    fake = FakeZulip([ok(dm(1, "still reachable"))])
    try:
        code, out, _ = _finish(_wait_proc(tmp_path, _session_env(tmp_path, fake.url)))
    finally:
        fake.close()
    assert code == 2 and "still reachable" in out


def test_bot_zulip_wait_runs_the_hooks_own_code(tmp_path):
    """`python bot/zulip.py wait` is the same code path, not a copy."""
    fake = FakeZulip([ok(dm(5, "by hand"))])
    try:
        env = _session_env(tmp_path, fake.url)
        done = subprocess.run(
            [sys.executable, str(HOOK.parent.parent / "bot" / "zulip.py"),
             "--as", "COO", "wait", "--session", "s47", "--seconds", "8"],
            capture_output=True, text=True, encoding="utf-8", env=env,
            timeout=60)
    finally:
        fake.close()
    assert done.returncode == 2, done.stderr
    assert "Direct message to @COO from Founder" in done.stdout
    assert "by hand" in done.stdout
    assert _state(tmp_path)["last_message_id"] == 5


def test_backfill_pages_through_a_long_gap(tmp_path):
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "agora-wait-s47.json").write_text(json.dumps(
        {"queue_id": "q-old", "last_event_id": 5, "last_message_id": 0,
         "backfill": False}), encoding="utf-8")
    history = [{"id": i, "type": "stream", "mentioned": True, "content": f"m{i}",
                "display_recipient": "coo", "subject": "t",
                "sender_email": "founder@zulip.localhost"} for i in range(1, 8)]
    fake = FakeZulip([], history=history, dead_queues=("q-old",))
    fake.page_size = 3
    try:
        code, out, _ = _finish(_wait_proc(tmp_path, _session_env(tmp_path, fake.url)))
    finally:
        fake.close()
    assert code == 2
    assert all(f"m{i}\n" in out for i in range(1, 8))
    assert _state(tmp_path)["last_message_id"] == 7


def test_a_lock_left_by_a_reused_pid_goes_stale(tmp_path):
    """A live pid in an old lock (the dead poller's pid, reused by some other
    process) must not keep the session unreachable forever."""
    import os
    temp = tmp_path / "temp"
    temp.mkdir()
    lock = temp / "agora-wait-s47.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")  # alive, but not a poller
    old = time.time() - 3600
    os.utime(lock, (old, old))
    fake = FakeZulip([ok(dm(1, "reachable again"))])
    try:
        code, out, _ = _finish(_wait_proc(tmp_path, _session_env(tmp_path, fake.url)))
    finally:
        fake.close()
    assert code == 2 and "reachable again" in out


def test_a_poller_whose_lock_was_taken_over_stops(hook, monkeypatch, tmp_path):
    """Two pollers that replaced the same stale lock: the one no longer named
    in it stops at its next poll, so the session is never woken twice."""
    fake = _serve(monkeypatch, [])
    lock = tmp_path / "agora-wait-s1.lock"
    calls = {"n": 0}
    real = hook._poll_once

    def poll(*a, **k):
        calls["n"] += 1
        lock.write_text("999999", encoding="utf-8")  # someone else took it
        return real(*a, **k)

    monkeypatch.setattr(hook, "_poll_once", poll)
    try:
        assert hook.do_wait() == 0
    finally:
        fake.close()
    assert calls["n"] == 1


# -- issue #50: any message in a role's own stream wakes it, but its own ------
#
# Stream name = session name lowercased. Any sender but the session's own bot
# wakes it there, bots included: the hourly watchdog posts to #coo as its own
# `Watchdog` bot to wake the COO.

WATCHDOG = "watchdog-bot@zulip.localhost"


def said(i: int, content: str, stream: str = "coo",
         email: str = "founder@zulip.localhost", who: str = "Founder",
         flags: tuple = ()) -> dict:
    return {"type": "message", "id": 100 + i, "flags": list(flags), "message": {
        "id": i, "type": "stream", "display_recipient": stream,
        "subject": "#50 own stream", "content": content,
        "sender_email": email, "sender_full_name": who}}


def _run(tmp_path: Path, script: list, seconds: float = 8.0):
    fake = FakeZulip(script, idle=0.3)
    try:
        return _finish(_wait_proc(tmp_path, _session_env(tmp_path, fake.url),
                                  seconds=seconds))
    finally:
        fake.close()


def test_a_watchdog_bot_post_in_the_own_stream_wakes_it(tmp_path):
    code, out, err = _run(tmp_path, [ok(said(
        1, "hourly: 2 issues ready", email=WATCHDOG, who="Watchdog"))])
    assert (code, err) == (2, "")
    assert "#coo › #50 own stream — from Watchdog" in out
    assert "hourly: 2 issues ready" in out


def test_the_founder_without_a_tag_in_the_own_stream_wakes_it(tmp_path):
    code, out, err = _run(tmp_path, [ok(said(1, "no tag needed"))])
    assert (code, err) == (2, "")
    assert "#coo › #50 own stream — from Founder" in out
    assert "@COO" not in out  # not a mention, and the text does not claim one
    assert "no tag needed" in out
    assert "Reply in #coo, topic '#50 own stream'." in out


def test_its_own_bot_post_in_the_own_stream_wakes_nobody(tmp_path):
    code, out, err = _run(tmp_path, [ok(said(
        1, "my own status line", email=BOT, who="COO"))], seconds=3)
    assert (code, out, err) == (0, "", "")


def test_the_same_message_in_another_roles_stream_does_not_wake(tmp_path):
    code, out, err = _run(tmp_path, [ok(
        said(1, "for the CTO", stream="cto"),
        said(2, "the CTO's watchdog line", stream="cto", email=WATCHDOG,
             who="Watchdog"))], seconds=3)
    assert (code, out, err) == (0, "", "")


def test_mentions_and_dms_still_wake_from_any_stream(tmp_path):
    code, out, err = _run(tmp_path, [ok(
        said(1, "tagged from #cto", stream="cto", flags=("mentioned",)),
        dm(2, "a direct line"))])
    assert (code, err) == (2, "")
    assert "@COO in #cto › #50 own stream — from Founder" in out
    assert "tagged from #cto" in out
    assert "Direct message to @COO from Founder" in out and "a direct line" in out


# -- issue #52: the backfill also reads the role's own stream -----------------
#
# The saved queue has expired (the fake answers BAD_EVENT_QUEUE_ID for it), so
# what arrived while no poller ran is only in the message history.

def _backfill_run(tmp_path: Path, history: list, seconds: float = 8.0,
                  channel_error: tuple | None = None):
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "agora-wait-s47.json").write_text(json.dumps(
        {"queue_id": "q-old", "last_event_id": 5, "last_message_id": 40,
         "backfill": False}), encoding="utf-8")
    fake = FakeZulip([], history=history, dead_queues=("q-old",), idle=0.3)
    fake.channel_error = channel_error
    try:
        return (*_finish(_wait_proc(tmp_path, _session_env(tmp_path, fake.url),
                                    seconds=seconds)), fake)
    finally:
        fake.close()


def posted(i: int, content: str, stream: str = "coo", mentioned: bool = False,
           email: str = "founder@zulip.localhost", who: str = "Founder") -> dict:
    return {"id": i, "type": "stream", "mentioned": mentioned, "content": content,
            "display_recipient": stream, "subject": "#52 backfill",
            "sender_email": email, "sender_full_name": who}


def test_an_untagged_own_stream_message_is_backfilled_and_wakes(tmp_path):
    code, out, err, fake = _backfill_run(tmp_path, [
        posted(39, "before the floor"),
        posted(41, "no tag, sent while nobody polled")])
    assert (code, err) == (2, "")
    assert "#coo › #52 backfill — from Founder" in out
    assert "no tag, sent while nobody polled" in out
    assert "@COO" not in out
    assert "before the floor" not in out
    assert _state(tmp_path)["last_message_id"] == 41
    narrows = [json.loads(c[2]["narrow"]) for c in fake.calls
               if c[1] == "/api/v1/messages"]
    assert [{"operator": "channel", "operand": "coo"}] in narrows


def test_its_own_bot_post_in_the_own_stream_is_not_backfilled(tmp_path):
    code, out, err, _ = _backfill_run(tmp_path, [
        posted(41, "my own status line", email=BOT, who="COO")], seconds=3)
    assert (code, out, err) == (0, "", "")
    assert _state(tmp_path)["last_message_id"] == 41  # seen, never delivered


def test_a_mention_in_the_own_stream_is_backfilled_once(tmp_path):
    code, out, err, _ = _backfill_run(tmp_path, [
        posted(41, "tagged and in #coo", mentioned=True)])
    assert (code, err) == (2, "")
    assert out.count("tagged and in #coo") == 1
    assert out.count("@COO in #coo › #52 backfill — from Founder") == 1


def test_an_untagged_message_in_another_roles_stream_is_not_backfilled(tmp_path):
    code, out, err, fake = _backfill_run(tmp_path, [
        posted(41, "for the CTO only", stream="cto")], seconds=3)
    assert (code, out, err) == (0, "", "")
    narrows = [json.loads(c[2]["narrow"]) for c in fake.calls
               if c[1] == "/api/v1/messages"]
    assert [{"operator": "channel", "operand": "cto"}] not in narrows



def test_an_own_stream_narrow_error_does_not_stop_dms_waking(tmp_path):
    """A bot with no own stream, or not subscribed to it, gets an error on
    that narrow. It must be skipped, not retried forever: a failed backfill
    stays pending and the live queue is never polled again."""
    code, out, err, fake = _backfill_run(tmp_path, [
        {"id": 41, "type": "private", "content": "a DM in the gap",
         "sender_email": "founder@zulip.localhost", "sender_full_name": "Founder"}],
        channel_error=(400, {"result": "error", "code": "BAD_REQUEST",
                             "msg": "Invalid channel name"}))
    assert (code, err) == (2, "")
    assert "Direct message to @COO from Founder" in out
    assert "a DM in the gap" in out
    assert _state(tmp_path)["last_message_id"] == 41
    assert _state(tmp_path)["backfill"] is False
    narrows = [json.loads(c[2]["narrow"]) for c in fake.calls
               if c[1] == "/api/v1/messages"]
    assert [{"operator": "channel", "operand": "coo"}] in narrows


def test_a_gap_longer_than_the_page_limit_is_read_in_steps(tmp_path):
    """A narrow that runs out of pages caps the floor at what it read; the
    next step goes on from there, so nothing past the cap is skipped."""
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "agora-wait-s47.json").write_text(json.dumps(
        {"queue_id": "q-old", "last_event_id": 5, "last_message_id": 0,
         "backfill": False}), encoding="utf-8")
    history = [{"id": i, "type": "stream", "mentioned": i % 2 == 0,
                "content": f"m{i}", "display_recipient": "coo", "subject": "t",
                "sender_email": "founder@zulip.localhost"} for i in range(1, 8)]
    fake = FakeZulip([], history=history, dead_queues=("q-old",), idle=0.3)
    fake.page_size = 2
    outs = []
    try:
        env = _session_env(tmp_path, fake.url)
        for _ in range(6):
            code, out, err = _finish(_wait_proc(tmp_path, env, seconds=3, pages=1))
            assert err == ""
            outs.append(out)
            if _state(tmp_path)["last_message_id"] == 7:
                break
    finally:
        fake.close()
    every = "".join(outs)
    assert [every.count(f"m{i}\n") for i in range(1, 8)] == [1] * 7
    # One page each: mentions 2 and 4, the stream's 1 and 2. Capped at 2.
    assert "m2\n" in outs[0] and "m3\n" not in outs[0]
    assert "m4\n" not in outs[0]
    assert _state(tmp_path)["last_message_id"] == 7



def _steps(tmp_path: Path, history: list, channel_script: list,
           page_size: int = 100, runs: int = 4) -> tuple[list, FakeZulip]:
    """`wait` run again and again after a lost queue, until the backfill is
    no longer pending. Returns every run's output."""
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "agora-wait-s47.json").write_text(json.dumps(
        {"queue_id": "q-old", "last_event_id": 5, "last_message_id": 40,
         "backfill": False}), encoding="utf-8")
    fake = FakeZulip([], history=history, dead_queues=("q-old",), idle=0.3)
    fake.page_size = page_size
    fake.channel_script = list(channel_script)
    outs = []
    try:
        env = _session_env(tmp_path, fake.url)
        for _ in range(runs):
            code, out, err = _finish(_wait_proc(tmp_path, env, seconds=4))
            assert err == ""
            outs.append(out)
            if not _state(tmp_path)["backfill"] and code == 0:
                break
    finally:
        fake.close()
    return outs, fake


def test_no_answer_on_the_own_stream_narrow_retries_instead_of_skipping(tmp_path):
    """Zulip not answering is not the same as Zulip saying no: the untagged
    #coo message must not be skipped because one request timed out."""
    outs, fake = _steps(tmp_path, [posted(41, "untagged, in the gap")], ["drop"])
    assert "".join(outs).count("untagged, in the gap") == 1
    assert "#coo › #52 backfill — from Founder" in "".join(outs)
    channel = [c for c in fake.calls if c[1] == "/api/v1/messages"
               and json.loads(c[2]["narrow"])[0]["operator"] == "channel"]
    assert len(channel) >= 2  # the dropped one, then the retry
    assert _state(tmp_path)["last_message_id"] == 41


def test_an_own_stream_error_after_one_page_caps_the_floor(tmp_path):
    """Page 1 of #coo read, page 2 an error: the floor stops at what was
    read, so the rest of #coo is delivered later, not jumped over."""
    history = [posted(41, "coo one"), posted(42, "coo two"), posted(43, "coo three"),
               {"id": 44, "type": "private", "content": "a DM after them",
                "sender_email": "founder@zulip.localhost",
                "sender_full_name": "Founder"}]
    error = (400, {"result": "error", "code": "BAD_REQUEST", "msg": "try later"})
    outs, _ = _steps(tmp_path, history, ["ok", error], page_size=1)
    assert "coo one" in outs[0]
    assert "coo two" not in outs[0] and "a DM after them" not in outs[0]
    every = "".join(outs)
    assert [every.count(t) for t in
            ("coo one", "coo two", "coo three", "a DM after them")] == [1, 1, 1, 1]
    assert _state(tmp_path)["last_message_id"] == 44
