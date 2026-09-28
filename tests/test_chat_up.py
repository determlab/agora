"""``chat/server.py up``, ``add-human --write-env`` and ``autostart`` (issue #72).

``up`` runs the way an agent runs it — a process, ``--json``, a temp DB and a
temp env file — never against bot/.env, never with a real address, never
into the real Startup folder.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_chat_events import Running
from test_chat_messages import ROOT, SERVER, Api, chat
from test_chat_setup import HUMAN, cli_env

ZULIP_CLI = ROOT / "bot" / "zulip.py"
SETUP = ROOT / "bot" / "setup_streams.py"
ROLES = ("CTO", "CMO", "COO", "WATCHDOG", "POOL")
STREAMS = ["cto", "cmo", "coo", "status", "feature", "pool"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env_file(path, site: str | None = None, extra: str = "") -> dict:
    keys = {r: f"{r.lower()}-key-from-zulip" for r in ROLES}
    path.write_text((f"ZULIP_SITE={site}\n" if site else "")
                    + "ZULIP_ADMIN_EMAIL=someone@example.invalid\nZULIP_ADMIN_API_KEY=a\n"
                    + "".join(f"ZULIP_{r}_EMAIL={r.lower()}-bot@zulip.localhost\n"
                              f"ZULIP_{r}_API_KEY={keys[r]}\n" for r in ROLES) + extra,
                    encoding="utf-8")
    return keys


def _server(*args, timeout=60):
    return subprocess.run([sys.executable, str(SERVER), *map(str, args)], capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout, env=cli_env("x"))


class FakeZulip:
    """Answers GET /api/v1/messages with one message, id ``newest``."""

    def __init__(self, newest: int):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"result": "success", "messages": [{"id": newest}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.site = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _state(db: str) -> dict:
    store = chat.Store(db)
    try:
        return {"users": [tuple(r) for r in store.q("SELECT * FROM users ORDER BY id")],
                "streams": [tuple(r) for r in store.q("SELECT * FROM streams ORDER BY 1")],
                "subs": [tuple(r) for r in store.q("SELECT * FROM subscriptions ORDER BY 1, 2")],
                "floor": store.max_message_id()}
    finally:
        store.db.close()


def test_up_no_serve_twice_same_bots_streams_and_floor_and_never_a_human(tmp_path):
    db, env = str(tmp_path / "chat.sqlite3"), tmp_path / "bot.env"
    zulip = FakeZulip(newest=600)
    # A founder pair already in the file is his, not a bot's: never made by `up`.
    _env_file(env, zulip.site, "ZULIP_FOUNDER_EMAIL=founder@example.invalid\n"
                               "ZULIP_FOUNDER_API_KEY=f\n")
    try:
        first = _server("up", "--no-serve", "--json", "--db", db, "--env", env)
    finally:
        zulip.stop()
    # Zulip is gone now: the stored floor stands.
    second = _server("up", "--no-serve", "--json", "--db", db, "--env", env)
    runs = []
    for done in (first, second):
        assert done.returncode == 0, done.stderr
        [line] = done.stdout.splitlines()
        runs.append(json.loads(line))
    assert set(runs[0]) >= {"url", "db", "bots", "streams", "humans", "pid"}
    assert "answered" in first.stderr and "not reachable" in second.stderr
    for out in runs:
        assert sorted(b["email"] for b in out["bots"]) == sorted(
            f"{r.lower()}-bot@zulip.localhost" for r in ROLES)
        assert out["streams"] == STREAMS and out["humans"] == [] and out["id_floor"] == 600
    assert {b["action"] for b in runs[1]["bots"]} == {"already exists"}
    assert _state(db)["floor"] == 600

    # Nothing changed on the second run; a third says the same.
    before = _state(db)
    assert _server("up", "--no-serve", "--json", "--db", db, "--env", env).returncode == 0
    assert _state(db) == before
    assert all(u[3] == 1 for u in before["users"])  # is_bot: `up` never makes a human
    assert "add-human" in second.stderr and "--write-env" in second.stderr
    assert "ZULIP_FOUNDER_" in second.stderr  # and why --write-env would refuse


def test_up_names_the_missing_env_file_and_the_variable(tmp_path, capsys):
    missing = tmp_path / "nope.env"
    assert chat.main(["up", "--no-serve", "--db", str(tmp_path / "c.sqlite3"),
                      "--env", str(missing)]) == 2
    err = capsys.readouterr().err
    assert str(missing) in err and "ZULIP_COO_EMAIL" in err and "--env" in err


def test_up_on_a_busy_port_says_how_to_find_the_holder(tmp_path, capsys):
    env = tmp_path / "bot.env"
    _env_file(env)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        assert chat.main(["up", "--no-update", "--port", str(port), "--db",
                          str(tmp_path / "c.sqlite3"), "--env", str(env)]) == 1
    err = capsys.readouterr().err
    assert f"netstat -ano | findstr :{port}" in err and "--port" in err


def test_up_serves_add_human_joins_every_stream_posts_to_feature_and_check_passes(tmp_path):
    """The DoD's browser run, minus the browser: the page's own call, a POST
    with the human's Basic auth, then the COO reads it with bot/zulip.py."""
    db, env, port = str(tmp_path / "chat.sqlite3"), tmp_path / "bot.env", _free_port()
    keys = _env_file(env)
    proc = subprocess.Popen([sys.executable, str(SERVER), "up", "--json", "--no-update",
                             "--port", str(port), "--db", db, "--env", str(env)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", env=cli_env("x"))
    try:
        summary = json.loads(proc.stdout.readline())
        url = f"http://127.0.0.1:{port}"
        assert summary["url"] == url and isinstance(summary["pid"], int)
        assert summary["humans"] == [] and summary["streams"] == STREAMS

        made = _server("add-human", "--email", HUMAN, "--name", "Test Human", "--write-env",
                       "--json", "--db", db, "--env", env, "--port", port)
        assert made.returncode == 0, made.stderr
        human = json.loads(made.stdout)
        assert sorted(human["streams"]) == sorted(STREAMS) and human["env_written"] == str(env)
        written = chat._read_env(str(env))
        assert written["ZULIP_FOUNDER_EMAIL"] == HUMAN
        assert written["ZULIP_FOUNDER_API_KEY"] == human["api_key"]

        page = Api(url, written["ZULIP_FOUNDER_EMAIL"], written["ZULIP_FOUNDER_API_KEY"])
        status, sent = page("POST", "messages", type="stream", to="feature",
                            topic="#72 up", content="hello from the founder's page")
        assert status == 200, sent
        tools = cli_env(url, ZULIP_COO_EMAIL="coo-bot@zulip.localhost",
                        ZULIP_COO_API_KEY=keys["COO"], ZULIP_ADMIN_EMAIL=HUMAN,
                        ZULIP_ADMIN_API_KEY=human["api_key"])
        read = subprocess.run([sys.executable, str(ZULIP_CLI), "--as", "COO", "read",
                               "--stream", "feature", "--topic", "#72 up"], env=tools,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert read.returncode == 0, read.stderr
        assert "hello from the founder's page" in read.stdout and HUMAN in read.stdout

        check = subprocess.run([sys.executable, str(SETUP), "--check"], env=tools,
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert check.returncode == 0, check.stdout + check.stderr
        assert "FAIL" not in check.stdout and "MISSING" not in check.stdout
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def test_add_human_write_env_appends_once_then_refuses_and_changes_nothing(tmp_path):
    db, env = str(tmp_path / "chat.sqlite3"), tmp_path / "bot.env"
    env.write_bytes(b"ZULIP_COO_EMAIL=coo-bot@zulip.localhost\nZULIP_COO_API_KEY=k")  # no \n
    first = _server("add-human", "--email", HUMAN, "--name", "Test Human", "--write-env",
                    "--json", "--db", db, "--env", env)
    assert first.returncode == 0, first.stderr
    key = json.loads(first.stdout)["api_key"]
    assert env.read_text(encoding="utf-8") == (
        "ZULIP_COO_EMAIL=coo-bot@zulip.localhost\nZULIP_COO_API_KEY=k\n"
        f"ZULIP_FOUNDER_EMAIL={HUMAN}\nZULIP_FOUNDER_API_KEY={key}\n")

    before = env.read_bytes()
    again = _server("add-human", "--email", "test-human-2@example.invalid", "--name", "Two",
                    "--write-env", "--json", "--db", db, "--env", env)
    assert again.returncode != 0 and again.stdout == ""
    assert "ZULIP_FOUNDER_EMAIL" in again.stderr and "not overwritten" in again.stderr
    assert env.read_bytes() == before
    assert [r[0] for r in _state(db)["users"]] == [1]  # the refusal made no account


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """Autostart as on Windows, and never the real Startup folder: the env
    variable too points at a temp dir, in case a flag is ever dropped."""
    monkeypatch.setattr(chat, "_is_windows", lambda: True)
    monkeypatch.setenv("AGORA_STARTUP_DIR", str(tmp_path / "not-this-one"))
    startup = tmp_path / "Startup"
    startup.mkdir()
    return startup


def _auto(capsys, *args) -> tuple[int, dict]:
    code = chat.main(["autostart", *args, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_autostart_install_status_remove_touch_exactly_one_file(windows, tmp_path, capsys):
    run = Running(str(tmp_path / "chat.sqlite3"))
    try:
        port = run.base.rsplit(":", 1)[1]
        code, out = _auto(capsys, "install", "--startup-dir", str(windows), "--port", port)
        assert code == 0 and out["installed"]
        [made] = os.listdir(windows)
        assert out["file"] == str(windows / made) and made.endswith(".cmd")
        script = (windows / made).read_text(encoding="utf-8")
        assert f" up --port {port}" in script and "server.log" in script and "/min" in script
        assert not (tmp_path / "not-this-one").exists()

        # issue #87: without --dashboard-cmd, the server autostart starts at
        # login has no sync thread and the dashboard panel stays empty.
        # `startup_script()` must pass the same command `chat/run.cmd` does
        # (`chat.default_dashboard_cmd()`), escaped for the .cmd file the same
        # way run.cmd's own static text is: pull the quoted value back out and
        # prove shlex.split (what `Dashboard.sync` actually runs it through)
        # parses it into the interpreter path plus the dashboard.py args,
        # rather than just asserting substrings are present somewhere.
        assert "--dashboard-cmd" in script
        m = re.search(r'--dashboard-cmd "((?:[^"\\]|\\.)*)"', script)
        assert m, script
        value = m.group(1).replace('\\"', '"')
        assert value == chat.default_dashboard_cmd()
        assert shlex.split(value) == [
            sys.executable.replace("\\", "/"),
            "C:/PlayGround/ops/tools/dashboard.py", "--json", "--no-tokens",
        ]

        code, out = _auto(capsys, "status", "--startup-dir", str(windows), "--port", port)
        assert code == 0 and out["installed"] is True and out["answering"] is True
    finally:
        run.stop()
    code, out = _auto(capsys, "status", "--startup-dir", str(windows), "--port", port)
    assert out["installed"] is True and out["answering"] is False

    code, out = _auto(capsys, "remove", "--startup-dir", str(windows))
    assert code == 0 and out["removed"] is True and os.listdir(windows) == []
    code, out = _auto(capsys, "remove", "--startup-dir", str(windows))
    assert out["removed"] is False  # nothing was there: not reported as removed
    code, out = _auto(capsys, "status", "--startup-dir", str(windows))
    assert out["installed"] is False


def test_autostart_elsewhere_than_windows_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(chat, "_is_windows", lambda: False)
    startup = tmp_path / "Startup"
    startup.mkdir()
    assert chat.main(["autostart", "install", "--startup-dir", str(startup)]) == 2
    assert "Windows only" in capsys.readouterr().err and os.listdir(startup) == []


# -- the updater: `git pull --ff-only` on main, restart on a new server.py

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd, *args):
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "init.defaultBranch=main", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done


@needs_git
def test_updater_fast_forwards_main_and_a_failed_pull_is_not_success(tmp_path):
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "server.py").write_text("x = 1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "one")
    _git(tmp_path, "clone", str(origin), str(clone))
    (origin / "server.py").write_text("x = 2\n")
    _git(origin, "commit", "-am", "two")

    up = chat.Updater(lambda: None, root=str(clone), source=str(clone / "server.py"))
    ok, out = up.pull()
    assert ok, out
    assert (clone / "server.py").read_text() == "x = 2\n" and up.changed()

    _git(clone, "checkout", "-b", "feature")
    ok, out = up.pull()
    assert not ok and "not main" in out
    _git(clone, "checkout", "main")
    _git(clone, "commit", "--allow-empty", "-m", "local only")  # diverged: no fast-forward
    (origin / "server.py").write_text("x = 3\n")
    _git(origin, "commit", "-am", "three")
    ok, out = up.pull()
    assert not ok and (clone / "server.py").read_text() == "x = 2\n"


def test_updater_restarts_on_a_new_server_py_but_not_on_one_that_does_not_compile(tmp_path):
    source = tmp_path / "server.py"
    source.write_text("x = 1\n")
    restarted = threading.Event()
    up = chat.Updater(restarted.set, every=3600, root=str(tmp_path), source=str(source))
    up.pull = lambda: (False, "skipped in the test")
    source.write_text("def broken(:\n")
    assert not up.changed() and not up.changed()  # logged once, kept running
    threading.Thread(target=up.run, kwargs={"check_every": 0.05}, daemon=True).start()
    time.sleep(0.3)
    assert not restarted.is_set()
    source.write_text("x = 2\n")
    assert restarted.wait(5)
    up.stop.set()
