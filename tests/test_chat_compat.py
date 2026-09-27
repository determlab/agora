"""The one command that says chat/server.py can replace Zulip for every tool:

    python -m pytest -q tests/test_chat_compat.py

The server runs the way an agent runs it — ``serve`` as a process on a free
port with a temp DB — and every tool runs exactly as it is today: ``bot/
zulip.py send|read|wait`` and ``bot/setup_streams.py`` as processes, an edit
and a reaction through ``bot/zulip_client.py``, and the hook's poll path
imported and called, never edited. Accounts come from ``bootstrap --from-env``
(today's emails and keys) and ``add-human`` (a test human, never the
founder's real account). A failure names the command and what it answered.
"""
from __future__ import annotations

import importlib.util
import json
import secrets
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

from test_chat_messages import ROOT, SERVER
from test_chat_setup import HUMAN, cli_env

ZULIP_CLI = ROOT / "bot" / "zulip.py"
SETUP = ROOT / "bot" / "setup_streams.py"
HOOK = ROOT / "hooks" / "agora_hook.py"
ROLES = ("CTO", "CMO", "COO", "WATCHDOG")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(args, env=None, timeout=60):
    return subprocess.run([sys.executable, *map(str, args)], env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout)


def _ok(done, what: str):
    assert done.returncode == 0, (f"{what} exited {done.returncode}\n"
                                  f"stdout: {done.stdout}\nstderr: {done.stderr}")
    return done


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("chat_compat")
    db, port = str(tmp / "chat.sqlite3"), _free_port()
    # Zulip's ids reached ~600: ours continue above them. A 1 s long poll
    # keeps the wait tests short; production is 50 s.
    proc = subprocess.Popen([sys.executable, str(SERVER), "serve", "--port", str(port),
                             "--db", db, "--seed-ids", "600", "--poll-seconds", "1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline, "chat/server.py serve never answered"
                time.sleep(0.1)

        # bot/.env as it is today: each role's Zulip-era email and key.
        dotenv = tmp / "bot.env"
        keys = {r: secrets.token_hex(16) for r in ROLES}
        dotenv.write_text("".join(f"ZULIP_{r}_EMAIL={r.lower()}-bot@chat.localhost\n"
                                  f"ZULIP_{r}_API_KEY={keys[r]}\n" for r in ROLES),
                          encoding="utf-8")
        boot = _ok(_run([SERVER, "bootstrap", "--json", "--from-env", dotenv, "--db", db,
                         "--port", port]), "server.py bootstrap --from-env")
        admin_bot = json.loads(boot.stdout)
        human = json.loads(_ok(_run([SERVER, "add-human", "--email", HUMAN, "--name",
                                     "Test Human", "--json", "--db", db, "--port", port]),
                               "server.py add-human").stdout)
        creds = {f"ZULIP_{r}_{k}": v for r in ROLES
                 for k, v in (("EMAIL", f"{r.lower()}-bot@chat.localhost"),
                              ("API_KEY", keys[r]))}
        env = cli_env(admin_bot["site"], ZULIP_ADMIN_EMAIL=human["email"],
                      ZULIP_ADMIN_API_KEY=human["api_key"], TEMP=str(tmp), TMP=str(tmp),
                      TMPDIR=str(tmp), **creds)
        # setup_streams.py runs as the human: in Zulip the admin is the founder.
        _ok(_run([SETUP], env), "bot/setup_streams.py (as the test human)")
        yield {"db": db, "env": env, "site": admin_bot["site"], "admin_bot": admin_bot,
               "human": human, "keys": keys, "tmp": tmp, "proc": proc}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def zulip(world, *args, timeout=60):
    return _run([ZULIP_CLI, *args], world["env"], timeout)


def _client(world, role: str | None = None):
    """bot/zulip_client.py as a role's bot, or as the test human."""
    sys.path.insert(0, str(ROOT / "bot"))
    try:
        from zulip_client import ZulipClient
    finally:
        sys.path.pop(0)
    if role is None:
        return ZulipClient(world["site"], HUMAN, world["human"]["api_key"], timeout=30)
    return ZulipClient(world["site"], f"{role.lower()}-bot@chat.localhost", world["keys"][role],
                       timeout=30)


def test_setup_streams_second_run_is_a_no_op_and_check_passes_as_the_human(world):
    again = _ok(_run([SETUP], world["env"]), "bot/setup_streams.py (second run)")
    assert "created" not in again.stdout and "done now" not in again.stdout, again.stdout
    check = _ok(_run([SETUP, "--check"], world["env"]), "bot/setup_streams.py --check")
    assert "FAIL" not in check.stdout and "MISSING" not in check.stdout


def test_zulip_send_and_read_with_the_unchanged_env_pair(world):
    sent = _ok(zulip(world, "--as", "COO", "send", "--stream", "coo", "--topic", "#61 compat",
                     "--text", "hello from our own chat"), "zulip.py --as COO send")
    mid = int(sent.stdout.rsplit("id=", 1)[1])
    assert mid > 600, "message ids must continue above Zulip's"
    read = _ok(zulip(world, "--as", "COO", "read", "--stream", "coo", "--topic", "#61 compat"),
               "zulip.py read")
    assert "hello from our own chat" in read.stdout and "coo-bot@chat.localhost" in read.stdout
    _ok(zulip(world, "--as", "COO", "send", "--stream", "coo", "--topic", "#61 compat",
              "--text", "second"), "zulip.py send")
    since = _ok(zulip(world, "--as", "COO", "read", "--stream", "coo", "--since", str(mid)),
                "zulip.py read --since")
    assert "second" in since.stdout and "hello from our own chat" not in since.stdout


def test_zulip_send_to_a_stream_the_bot_is_not_in_is_refused(world):
    refused = zulip(world, "--as", "CMO", "send", "--stream", "coo", "--topic", "t",
                    "--text", "not mine")
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "refused" in refused.stderr and "Nothing was sent" in refused.stderr


def test_watchdog_posts_to_status(world):
    sent = _ok(zulip(world, "--as", "WATCHDOG", "send", "--stream", "status", "--topic",
                     "status", "--text", "all green"), "zulip.py --as WATCHDOG send #status")
    assert "sent #status" in sent.stdout


def test_an_edit_and_a_reaction_through_zulip_client(world):
    coo = _client(world, "COO")
    mid = coo.send_message("coo", "draft", topic="#61 edit")["id"]
    coo._request("PATCH", f"messages/{mid}", {"content": "final"})
    coo._request("POST", f"messages/{mid}/reactions", {"emoji_name": "thumbs_up",
                                                       "emoji_code": "1f44d"})
    got = coo._request("GET", "messages", {"anchor": mid, "num_before": 0, "num_after": 0})
    [m] = got["messages"]
    assert m["content"] == "final" and m["reactions"][0]["emoji_name"] == "thumbs_up"


def _queues(world, email: str) -> int:
    db = sqlite3.connect(world["db"])
    try:
        return db.execute("SELECT count(*) FROM queues q JOIN users u ON u.id=q.user_id "
                          "WHERE u.email=?", (email,)).fetchone()[0]
    finally:
        db.close()


def test_zulip_wait_wakes_on_a_mention(world):
    cto_email = "cto-bot@chat.localhost"
    before = _queues(world, cto_email)
    proc = subprocess.Popen([sys.executable, str(ZULIP_CLI), "--as", "CTO", "wait",
                             "--session", "compat-cto", "--seconds", "30"],
                            env=world["env"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")
    try:
        deadline = time.monotonic() + 20
        while _queues(world, cto_email) == before:
            assert proc.poll() is None, proc.stderr.read()
            assert time.monotonic() < deadline, "zulip.py wait never registered a queue"
            time.sleep(0.1)
        _ok(zulip(world, "--as", "COO", "send", "--stream", "feature", "--topic", "ops#44",
                  "--text", "@**CTO** the route table?"), "zulip.py --as COO send #feature")
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 2, f"zulip.py wait exited {proc.returncode}: {out}{err}"
    assert "@CTO in #feature" in out and "the route table?" in out


def test_the_hooks_poll_path_wakes_on_a_mention_and_an_own_stream_post(world, monkeypatch):
    spec = importlib.util.spec_from_file_location("agora_hook_compat", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(hook, "STATE_DIR", world["tmp"])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    creds = (world["site"], "coo-bot@chat.localhost", world["keys"]["COO"])
    path = world["tmp"] / "hook-coo.json"
    state = {"queue_id": None, "last_event_id": -1, "last_message_id": None,
             "backfill": False}
    # Registers, then an idle poll: nothing to wake on.
    assert hook._poll_once(creds, "COO", state, path) is None
    assert state["queue_id"] and state["last_message_id"] > 600

    _client(world).send_message("coo", "untagged, in the COO's own stream", topic="#61")
    text = hook._poll_once(creds, "COO", state, path)
    assert text and "untagged, in the COO's own stream" in text and "#coo" in text

    cto = _client(world, "CTO")
    cto.send_message("feature", "@**COO** a process note, please", topic="ops#44")
    text = hook._poll_once(creds, "COO", state, path)
    assert text and "@COO in #feature" in text and "a process note, please" in text
    assert hook._poll_once(creds, "COO", state, path) is None  # delivered once
