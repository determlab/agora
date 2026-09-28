"""chat/server.py's dashboard (issue #81, RFC-002 §1): the sync thread, the
``dashboard`` table, GET /api/v1/dashboard, POST /api/v1/dashboard/sync and
the ``dashboard`` event, over a real socket.

The fake commands are Python one-liners run through the same
``shlex.split`` the server uses: one prints a fixed JSON document and counts
its calls in a file, the other exits 1 with ``gh: not logged in`` on stderr.
"""
from __future__ import annotations

import json
import sys
import threading
import time

import pytest

from test_chat_messages import Api, chat

DOC = {"repos": [{"name": "agora", "open_prs": 2}], "generated": "fixed"}
PY = '"' + sys.executable.replace("\\", "/") + '"'  # shlex keeps it whole


def _ok_cmd(calls) -> str:
    counter = str(calls).replace("\\", "/")
    return (f"{PY} -c 'import json; open(\"{counter}\", \"a\").write(\"x\\n\"); "
            f"print(json.dumps({json.dumps(DOC)}))'")


FAIL_CMD = f"{PY} -c 'import sys; sys.stderr.write(\"gh: not logged in\\n\"); sys.exit(1)'"


def _calls(path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def make(tmp_path):
    """make(cmd, every=3600) -> (api, server): a server with the sync thread
    running, after its first (immediate) run has finished, and a bot's Api."""
    servers = []

    def _make(cmd, every=3600.0):
        server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0, dashboard_cmd=cmd,
                                  dashboard_every=every)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        dash = server.RequestHandlerClass.chat.dashboard
        if cmd:
            deadline = time.monotonic() + 30
            while dash.last_error is None and dash.state()["last_sync"] is None:
                assert time.monotonic() < deadline, "the first sync never finished"
                time.sleep(0.05)
            with dash.running:  # the first run has released it
                pass
        store = server.RequestHandlerClass.chat.s
        bot = store.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        return Api(base, bot["email"], bot["api_key"]), server
    yield _make
    for server in servers:
        server.RequestHandlerClass.chat.dashboard.stop.set()
        server.shutdown()
        server.server_close()


def _dash(server):
    return server.RequestHandlerClass.chat.dashboard


def test_sync_stores_the_document_and_get_returns_it_with_last_sync(make, tmp_path):
    api, server = make(_ok_cmd(tmp_path / "calls"))
    status, body = api("POST", "dashboard/sync")
    assert status == 200 and body["result"] == "success", body
    ts = body["last_sync"]
    assert isinstance(ts, int)
    status, got = api("GET", "dashboard")
    assert status == 200 and got["result"] == "success", got
    assert got["doc"] == DOC
    assert got["last_sync"] == ts and got["stale"] is False and got["last_error"] is None
    row = _dash(server).s.one("SELECT doc, error FROM dashboard WHERE ts=?", (ts,))
    assert json.loads(row["doc"]) == DOC and row["error"] is None


def test_a_failing_command_keeps_the_previous_document_and_sets_last_error(make, tmp_path):
    api, server = make(_ok_cmd(tmp_path / "calls"))
    _, first = api("GET", "dashboard")
    assert first["doc"] == DOC
    _dash(server).cmd = FAIL_CMD
    status, body = api("POST", "dashboard/sync")
    assert status == 502 and body["result"] == "error", body
    assert "gh: not logged in" in body["msg"] and "gh: not logged in" in body["last_error"]
    _, got = api("GET", "dashboard")
    assert got["doc"] == DOC and got["last_sync"] == first["last_sync"]
    assert "gh: not logged in" in got["last_error"]
    assert _dash(server).s.one("SELECT count(*) FROM dashboard")[0] == 1


def test_stale_after_two_hours(make, tmp_path):
    api, server = make(_ok_cmd(tmp_path / "calls"))
    clock = Clock()
    _dash(server).clock = clock
    _, body = api("POST", "dashboard/sync")
    clock.now = body["last_sync"] + 7200
    assert api("GET", "dashboard")[1]["stale"] is False
    clock.now = body["last_sync"] + 7201
    assert api("GET", "dashboard")[1]["stale"] is True


def test_no_command_configured_names_the_flag(make):
    api, _ = make(None)
    status, got = api("GET", "dashboard")
    assert status == 200 and got["last_sync"] is None and got["stale"] is True
    assert got["last_error"] == ("no dashboard command configured: start the server with "
                                 "--dashboard-cmd")
    status, body = api("POST", "dashboard/sync")
    assert status == 400 and body["result"] == "error" and "--dashboard-cmd" in body["msg"]


def test_rows_older_than_24h_are_deleted(make, tmp_path):
    api, server = make(_ok_cmd(tmp_path / "calls"))
    dash, clock = _dash(server), Clock()
    dash.clock = clock
    now = int(clock.now) + 100  # past the first run's row
    clock.now = now
    old, recent = now - 24 * 3600 - 1, now - 24 * 3600 + 60
    for ts in (old, recent):
        dash.s.x("INSERT INTO dashboard VALUES (?, '{}', NULL)", (ts,))
    _, body = api("POST", "dashboard/sync")
    assert body["last_sync"] == now
    tss = [r[0] for r in dash.s.q("SELECT ts FROM dashboard ORDER BY ts")]
    assert old not in tss and recent in tss and now in tss


def test_dashboard_event_reaches_only_queues_that_asked_for_it(make, tmp_path):
    api, server = make(_ok_cmd(tmp_path / "calls"))
    # A second bot in no stream at all: membership does not matter.
    store = _dash(server).s
    coo = store.create_user("coo-bot@chat.localhost", "COO", is_bot=True)
    other = Api(api.base, coo["email"], coo["api_key"])
    _, wants = other("POST", "register", event_types=["message", "dashboard"])
    _, plain = api("POST", "register", event_types=["message"])
    _, body = api("POST", "dashboard/sync")
    _, got = other("GET", "events", queue_id=wants["queue_id"], last_event_id=-1,
                   dont_block="true")
    assert [{k: e[k] for k in ("type", "last_sync")} for e in got["events"]] == [
        {"type": "dashboard", "last_sync": body["last_sync"]}]
    _, none = api("GET", "events", queue_id=plain["queue_id"], last_event_id=-1,
                  dont_block="true")
    assert none["events"] == []


def test_a_failed_sync_sends_no_event(make, tmp_path):
    api, server = make(_ok_cmd(tmp_path / "calls"))
    _, q = api("POST", "register", event_types=["dashboard"])
    _dash(server).cmd = FAIL_CMD
    assert api("POST", "dashboard/sync")[0] == 502
    _, got = api("GET", "events", queue_id=q["queue_id"], last_event_id=-1, dont_block="true")
    assert got["events"] == []


def test_post_sync_forces_a_run_now(make, tmp_path):
    calls = tmp_path / "calls"
    api, _ = make(_ok_cmd(calls))
    before = _calls(calls)
    assert before == 1  # the thread's first run, at start
    assert api("POST", "dashboard/sync")[0] == 200
    assert _calls(calls) == before + 1


def test_the_thread_runs_every_period(make, tmp_path):
    calls = tmp_path / "calls"
    make(_ok_cmd(calls), every=0.2)
    deadline = time.monotonic() + 30
    while _calls(calls) < 3:
        assert time.monotonic() < deadline, _calls(calls)
        time.sleep(0.05)


def test_the_store_lock_is_free_while_the_command_runs(make, tmp_path):
    api, server = make(None)
    dash = _dash(server)
    dash.cmd = f"{PY} -c 'import time; time.sleep(2); print(1)'"
    t = threading.Thread(target=dash.sync)
    t.start()
    time.sleep(0.5)
    assert dash.s.lock.acquire(timeout=0.5)
    dash.s.lock.release()
    assert api("GET", "users/me")[0] == 200
    t.join(timeout=30)


def test_a_timeout_stores_nothing_and_says_so(make, tmp_path):
    api, server = make(None)
    dash = _dash(server)
    dash.cmd, dash.timeout = f"{PY} -c 'import time; time.sleep(5)'", 0.5
    status, body = api("POST", "dashboard/sync")
    assert status == 502 and "timed out" in body["msg"], body
    assert dash.s.one("SELECT count(*) FROM dashboard")[0] == 0
    assert "timed out" in api("GET", "dashboard")[1]["last_error"]


def test_agent_reads_last_sync_with_a_bot_key(make, tmp_path):
    """The agent path: HTTP Basic with a bot key, as bot/zulip.py sends it."""
    api, server = make(_ok_cmd(tmp_path / "calls"))
    status, got = api("GET", "dashboard")
    assert status == 200 and isinstance(got["last_sync"], int) and got["stale"] is False
    _dash(server).cmd = FAIL_CMD
    status, body = api("POST", "dashboard/sync")
    assert status == 502 and body["result"] != "success"
    _, got = api("GET", "dashboard")
    assert got["last_error"].splitlines()[-1] == "gh: not logged in"


def test_no_key_is_refused(make):
    api, _ = make(None)
    bad = Api(api.base, "cto-bot@chat.localhost", "wrong")
    assert bad("GET", "dashboard")[0] == 401
    assert bad("POST", "dashboard/sync")[0] == 401


def test_cli_takes_the_flags():
    serve = ["serve", "--dashboard-cmd", "x --json", "--dashboard-every", "60"]
    parsed = {}

    def fake(db, port, poll, seed, cmd, every):
        parsed.update(cmd=cmd, every=every)
        raise SystemExit(0)
    real, chat.make_server = chat.make_server, fake
    try:
        with pytest.raises(SystemExit):
            chat.main(serve)
    finally:
        chat.make_server = real
    assert parsed == {"cmd": "x --json", "every": 60.0}
