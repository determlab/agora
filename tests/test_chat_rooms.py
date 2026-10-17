"""Rooms (ops#322): a room is a stream whose messages all go to one topic.

The CTO's design: keep the Zulip API, one fixed topic ``chat``; an invite
only subscribes and never wakes; only a person invites (no bot, and never
the PA by a bot); a room name is safe text, checked by the server at create
and rename; old topics show as one room and the database is not rewritten.
The server runs on a temp DB; the founder here is a test human, never his
account. ``bot/zulip.py`` and ``bot/wake.py`` run against that server.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from test_chat_events import Running
from test_chat_messages import chat
from test_chat_setup import HUMAN

BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
HOSTILE = ["a`touch /tmp/pwn`", "a$(touch /tmp/pwn)", 'a" && touch /tmp/pwn; "', "a\\b",
           "a;b", "a|b", "a\nb", "x" * 61]
EDGES = [" lead", "trail "]  # trimmed at create, refused as typed at rename
SAFE = ["coo", "All hands", "כללי", "roadmap-2026_v1.2", "x" * 60]


def _load(name: str):
    sys.path.insert(0, str(BOT_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"{name}_rooms", BOT_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(BOT_DIR))


@pytest.fixture
def run(tmp_path):
    r = Running(str(tmp_path / "chat.sqlite3"))
    s = r.store
    r.founder = chat.add_human(s, HUMAN, "Test Human")
    r.bots = {name: s.create_user(f"{name.lower()}-bot@chat.localhost", name, is_bot=True)
              for name in ("COO", "CTO", "PM", "PA")}
    r.admin_bot = s.user(s.one("SELECT id FROM users WHERE email=?",
                               (chat.bootstrap(s)["email"],))[0])
    yield r
    r.stop()


def _create(run, name, *members):
    return run.api(run.founder)("POST", "users/me/subscriptions",
                                subscriptions=[{"name": name}], invite_only="true",
                                principals=[run.founder["email"], *members])


# -- room names ---------------------------------------------------------------

@pytest.mark.parametrize("name", HOSTILE)
def test_a_hostile_room_name_is_refused_at_create_and_nothing_is_made(run, name):
    status, body = _create(run, name)
    assert status == 400 and body["code"] == "BAD_ROOM_NAME", body
    assert "letters, digits, spaces" in body["msg"] or "too long" in body["msg"] \
        or "needs a name" in body["msg"]
    assert run.store.one("SELECT count(*) FROM streams")[0] == 0


@pytest.mark.parametrize("name", HOSTILE + EDGES)
def test_a_hostile_room_name_is_refused_at_rename_and_the_name_stays(run, name):
    assert _create(run, "design")[0] == 200
    sid = run.store.stream_by_ref("design")["stream_id"]
    status, body = run.api(run.founder)("PATCH", f"streams/{sid}", new_name=name)
    assert status == 400 and body["code"] == "BAD_ROOM_NAME", body
    assert run.store.stream_by_ref(sid)["name"] == "design"


@pytest.mark.parametrize("name", SAFE)
def test_a_safe_room_name_is_accepted_hebrew_included(run, name):
    status, body = _create(run, name)
    assert status == 200, body
    assert run.store.stream_by_ref(name) is not None


def test_server_and_wake_agree_on_the_room_name_rule():
    wake = _load("wake")
    for name in HOSTILE + EDGES + SAFE:
        assert (chat.room_name_error(name) is None) is wake.safe_room(name), name


# -- one room, one topic, old history kept --------------------------------------

def test_a_message_without_a_topic_goes_to_the_rooms_one_topic(run):
    _create(run, "design")
    status, body = run.api(run.founder)("POST", "messages", type="stream", to="design",
                                        content="hello room")
    assert status == 200, body
    assert run.store.one("SELECT subject FROM messages WHERE id=?", (body["id"],))[0] == "chat"


def test_old_topics_read_as_one_room_in_time_order_and_the_rows_are_unchanged(run):
    _create(run, "coo", run.bots["COO"]["email"])
    founder = run.api(run.founder)
    ids = [founder("POST", "messages", type="stream", to="coo", topic=t, content=c)[1]["id"]
           for t, c in (("#141 record shape", "old one"), ("כללי", "old two"),
                        ("chat", "new one"))]
    before = [tuple(r) for r in run.store.q("SELECT id, subject, content FROM messages")]
    got = founder("GET", "messages", narrow=json.dumps([{"operator": "channel",
                                                         "operand": "coo"}]),
                  anchor="newest", num_before=100, num_after=0)[1]
    assert [m["id"] for m in got["messages"]] == ids
    assert [m["content"] for m in got["messages"]] == ["old one", "old two", "new one"]
    after = [tuple(r) for r in run.store.q("SELECT id, subject, content FROM messages")]
    assert after == before  # display only: nothing rewritten


# -- who may invite -------------------------------------------------------------

def test_a_role_bot_cannot_invite_anyone(run):
    _create(run, "design", run.bots["CTO"]["email"])
    cto = run.api(run.bots["CTO"])
    status, body = cto("POST", "users/me/subscriptions", subscriptions=[{"name": "design"}],
                       principals=[run.bots["COO"]["email"]])
    assert status == 400 and body["code"] == "INVITE_NOT_ALLOWED", body
    assert "Invite button" in body["msg"] and "ops#321" not in body["msg"]
    members = run.api(run.founder)("GET", f"streams/{run.store.stream_by_ref('design')['stream_id']}/members")[1]
    assert run.bots["COO"]["id"] not in members["subscribers"]


def test_a_role_bot_cannot_join_a_public_room_by_itself(run):
    # CTO review @a7c77e5: a self-join would change the room's bot count
    _create(run, "design")
    sid = run.store.stream_by_ref("design")["stream_id"]
    run.store.x("UPDATE streams SET invite_only=0 WHERE stream_id=?", (sid,))
    status, body = run.api(run.bots["CTO"])("POST", "users/me/subscriptions",
                                            subscriptions=[{"name": "design"}])
    assert status == 400 and body["code"] == "INVITE_NOT_ALLOWED", body
    assert "Invite button" in body["msg"]
    members = run.api(run.founder)("GET", f"streams/{sid}/members")[1]
    assert run.bots["CTO"]["id"] not in members["subscribers"]
    # a bot already in the room may subscribe again: nothing changes
    _create(run, "plan", run.bots["COO"]["email"])
    status, body = run.api(run.bots["COO"])("POST", "users/me/subscriptions",
                                            subscriptions=[{"name": "plan"}])
    assert status == 200 and body["already_subscribed"] == {run.bots["COO"]["email"]: ["plan"]}


def test_no_bot_invites_the_pa_not_even_the_admin_bot(run):
    _create(run, "design")
    status, body = run.api(run.admin_bot)("POST", "users/me/subscriptions",
                                          subscriptions=[{"name": "design"}],
                                          principals=[run.bots["PA"]["email"]])
    assert status == 400 and body["code"] == "INVITE_NOT_ALLOWED"
    status, body = run.api(run.bots["CTO"])("DELETE", "users/me/subscriptions",
                                            subscriptions=["design"],
                                            principals=[run.founder["email"]])
    assert status == 400 and body["code"] == "INVITE_NOT_ALLOWED"


def test_the_founder_invites_the_pa_and_only_an_existing_account(run):
    _create(run, "family")
    founder = run.api(run.founder)
    status, body = founder("POST", "users/me/subscriptions", subscriptions=[{"name": "family"}],
                           principals=[run.bots["PA"]["email"]])
    assert status == 200 and body["subscribed"] == {run.bots["PA"]["email"]: ["family"]}
    status, body = founder("POST", "users/me/subscriptions", subscriptions=[{"name": "family"}],
                           principals=["nobody@example.invalid"])
    assert status == 400 and "no such email" in body["msg"]
    assert run.store.one("SELECT count(*) FROM users WHERE email='nobody@example.invalid'")[0] == 0


# -- subscription events: an invite tells, never wakes ---------------------------

def _drain(api, qid):
    return [e for e in api("GET", "events", queue_id=qid, last_event_id=-1,
                           dont_block="true")[1]["events"] if e["type"] != "heartbeat"]


def test_invite_and_removal_reach_the_queues_that_asked_for_subscription_events(run):
    _create(run, "design", run.bots["COO"]["email"])
    founder, cto, coo = (run.api(u) for u in (run.founder, run.bots["CTO"], run.bots["COO"]))
    q_cto = cto("POST", "register", event_types=json.dumps(["message", "subscription"]))[1]
    q_coo = coo("POST", "register", event_types=json.dumps(["message", "subscription"]))[1]
    q_old = cto("POST", "register", event_types=json.dumps(["message"]))[1]  # the hook's shape
    founder("POST", "users/me/subscriptions", subscriptions=[{"name": "design"}],
            principals=[run.bots["CTO"]["email"]])
    sid = run.store.stream_by_ref("design")["stream_id"]
    [add] = _drain(cto, q_cto["queue_id"])
    assert add["op"] == "add" and add["subscriptions"][0]["name"] == "design"
    [peer] = _drain(coo, q_coo["queue_id"])
    assert (peer["op"], peer["stream_ids"], peer["user_ids"]) == \
        ("peer_add", [sid], [run.bots["CTO"]["id"]])
    assert _drain(cto, q_old["queue_id"]) == []  # an invite is no message: nothing wakes
    founder("DELETE", "users/me/subscriptions", subscriptions=["design"],
            principals=[run.bots["CTO"]["email"]])
    events = _drain(cto, q_cto["queue_id"])
    assert events[-1]["op"] == "remove" and events[-1]["subscriptions"] == \
        [{"name": "design", "stream_id": sid}]


# -- bot/zulip.py: the agent path -----------------------------------------------

@pytest.fixture
def cli(run, monkeypatch, tmp_path):
    mod = _load("zulip")
    monkeypatch.setattr(mod, "DOTENV", str(tmp_path / "missing.env"))
    for key in list(os.environ):
        if key.startswith("ZULIP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("ZULIP_SITE", run.base)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("ZULIP_CTO_EMAIL", run.bots["CTO"]["email"])
    monkeypatch.setenv("ZULIP_CTO_API_KEY", run.bots["CTO"]["api_key"])
    return mod


def test_cli_send_room_posts_to_the_rooms_one_topic(run, cli, capsys):
    _create(run, "design", run.bots["CTO"]["email"])
    assert cli.main(["--as", "CTO", "send", "--room", "design", "--text", "from the CTO"]) == 0
    assert "sent #design id=" in capsys.readouterr().out
    row = run.store.one("SELECT subject, content FROM messages ORDER BY id DESC LIMIT 1")
    assert tuple(row) == ("chat", "from the CTO")


def test_cli_read_room_json_reads_every_old_topic(run, cli, capsys):
    _create(run, "design", run.bots["CTO"]["email"])
    founder = run.api(run.founder)
    for topic, text in (("old", "first"), ("chat", "second")):
        founder("POST", "messages", type="stream", to="design", topic=topic, content=text)
    assert cli.main(["--as", "CTO", "read", "--room", "design", "--json"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [m["content"] for m in lines] == ["first", "second"]


def test_cli_wait_room_exits_2_with_the_next_message_from_someone_else(run, cli, capsys,
                                                                        monkeypatch):
    _create(run, "design", run.bots["CTO"]["email"])
    founder = run.api(run.founder)
    sleeps = []

    def sleep(_):  # the founder writes while the CLI waits
        sleeps.append(1)
        if len(sleeps) == 1:
            run.api(run.bots["CTO"])("POST", "messages", type="stream", to="design",
                                     content="my own line, not a wake")
            founder("POST", "messages", type="stream", to="design", content="over to you")
    client = cli.load_client("CTO")
    assert cli.cmd_wait_room(client, "design", 30, as_json=True, sleep=sleep) == 2
    line = json.loads(capsys.readouterr().out)
    assert line["content"] == "over to you" and line["stream"] == "design"
    assert run.store.one("SELECT count(*) FROM queues WHERE user_id=?",
                         (run.bots["CTO"]["id"],))[0] == 0  # its own queue, deleted


def test_cli_wait_room_times_out_with_0(run, cli):
    _create(run, "design", run.bots["CTO"]["email"])
    t = iter(range(100))
    client = cli.load_client("CTO")
    assert cli.cmd_wait_room(client, "design", 3, clock=lambda: next(t),
                             sleep=lambda _: None) == 0


@pytest.mark.parametrize("who,code", [("CTO", "INVITE_OFF"), ("pa", "INVITE_OFF"),
                                      ("founder@example.com", "NOT_A_ROLE")])
def test_cli_invite_is_refused_while_321_is_off_and_never_takes_a_person(run, cli, capsys,
                                                                       who, code):
    _create(run, "design", run.bots["CTO"]["email"])
    before = run.store.q("SELECT * FROM subscriptions")
    assert cli.main(["--as", "CTO", "invite", "--room", "design", "--who", who, "--json"]) == 2
    out = capsys.readouterr()
    body = json.loads(out.out)
    assert body["ok"] is False and body["code"] == code
    if code == "INVITE_OFF":
        assert "Invite button" in body["error"] and "ops#321" not in body["error"]
    else:
        assert "never a person" in body["error"]
    assert "Nothing was changed" in out.err
    assert [tuple(r) for r in run.store.q("SELECT * FROM subscriptions")] == \
        [tuple(r) for r in before]


# -- the whole path: the page's message, the server's members, wake's decision -----

class _Spawner:
    def __init__(self):
        self.started = []

    def start(self, argv, prompt, role, cwd=None):
        self.started.append((role, prompt))
        return 1000 + len(self.started)

    def created(self, pid):
        return pid

    def alive(self, pid, created=None):
        return True

    def conversation_exists(self, sid):
        return True


def test_wake_against_the_server_one_bot_room_wakes_multi_bot_room_needs_the_tag(
        run, tmp_path, monkeypatch):
    wake = _load("wake")
    monkeypatch.setattr(wake, "registry_live", lambda role, alive: None)
    _create(run, "pa", run.bots["PA"]["email"])
    _create(run, "All hands", *(b["email"] for b in run.bots.values()))
    clients = {r: wake.ZulipClient(run.base, b["email"], b["api_key"])
               for r, b in run.bots.items()}
    sp = _Spawner()
    w = wake.Waker(run.founder["id"], tmp_path / "state.json", sp,
                   members=lambda role, room: wake.room_members(clients[role], room),
                   history=lambda role, room, n: wake.room_history(clients[role], room, n))
    founder = run.api(run.founder)

    def say(room, text):
        mid = founder("POST", "messages", type="stream", to=room, content=text)[1]["id"]
        return founder("GET", f"messages/{mid}")[1]["message"]

    # #pa: the founder and the PA only — no tag needed
    assert w.on_message("PA", say("pa", "remind me at 5"), [])["action"] == "spawn"
    # All hands: four role bots — only the tagged one wakes
    m = say("All hands", "@**CTO** is the design done?")
    got = {r: w.on_message(r, m, ["mentioned"])["action"] for r in ("COO", "CTO", "PM")}
    assert got == {"COO": "skip", "CTO": "spawn", "PM": "skip"}
    prompt = dict(sp.started)["CTO"]
    assert '--room "All hands"' in prompt and "--topic" not in prompt
