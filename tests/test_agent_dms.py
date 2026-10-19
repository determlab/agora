"""Issue #175: direct messages between role agents.

A role bot DMs another role with ``send --dm <ROLE>``; wake.py opens (or
wakes) that role's session with the DM as input; the founder sees every
bot-to-bot DM in the read-only room "סוכנים"; a DM that is only an
acknowledgement wakes nobody.

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
from test_chat_messages import ROOT, chat
from test_chat_setup import HUMAN

BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
AGENTS_ROOM = "סוכנים"


def _load(name: str):
    sys.path.insert(0, str(BOT_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"{name}_dms", BOT_DIR / f"{name}.py")
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
    yield r
    r.stop()


@pytest.fixture
def cli(run, monkeypatch, tmp_path):
    mod = _load("zulip")
    monkeypatch.setattr(mod, "DOTENV", str(tmp_path / "missing.env"))
    for key in list(os.environ):
        if key.startswith("ZULIP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("ZULIP_SITE", run.base)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for role, bot in run.bots.items():
        monkeypatch.setenv(f"ZULIP_{role}_EMAIL", bot["email"])
        monkeypatch.setenv(f"ZULIP_{role}_API_KEY", bot["api_key"])
    return mod


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


def _waker(run, tmp_path, monkeypatch):
    wake = _load("wake")
    monkeypatch.setattr(wake, "registry_live", lambda role, alive: None)
    sp = _Spawner()
    w = wake.Waker(run.founder["id"], tmp_path / "state.json", sp, bot_wakes=True,
                   bot_ids={b["id"]: role for role, b in run.bots.items()})
    return wake, w, sp


def _dm(cli, capsys, src, to, text):
    assert cli.main(["--as", src, "send", "--dm", to, "--text", text, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def _as_seen_by(run, role, mid):
    status, body = run.api(run.bots[role])("GET", f"messages/{mid}")
    assert status == 200, body
    return body["message"]


# -- DoD: send --dm PM from COO stores one private message, wake opens PM ------

def test_send_dm_pm_from_coo_stores_one_private_message_and_wake_opens_a_pm_session(
        run, cli, capsys, tmp_path, monkeypatch):
    out = _dm(cli, capsys, "COO", "PM", "can you order #170 before #171?")
    assert out["ok"] is True and out["dm"] == "PM" and isinstance(out["id"], int)
    rows = run.store.q("SELECT * FROM messages WHERE type='private'")
    assert len(rows) == 1 and rows[0]["id"] == out["id"]
    assert rows[0]["sender_id"] == run.bots["COO"]["id"]
    to = {r[0] for r in run.store.q("SELECT user_id FROM recipients WHERE message_id=?",
                                    (out["id"],))}
    assert to == {run.bots["COO"]["id"], run.bots["PM"]["id"]}

    wake, w, sp = _waker(run, tmp_path, monkeypatch)
    m = _as_seen_by(run, "PM", out["id"])
    d = w.on_message("PM", m, [])
    assert d["action"] == "spawn" and d["origin"] == "bot:COO", d
    (role, prompt), = sp.started
    assert role == "PM" and "can you order #170 before #171?" in prompt
    assert prompt.splitlines()[0].startswith("From the COO bot")  # never the founder's word
    assert "--as PM send --dm COO" in prompt  # the answer goes back as a DM
    assert "decisions.md" in prompt  # a decision goes to the founder, not a DM
    # The sender's own queue sees its DM too: it never wakes the sender.
    assert w.on_message("COO", _as_seen_by(run, "COO", out["id"]), [])["action"] == "skip"
    assert len(sp.started) == 1


def test_a_second_dm_resumes_or_queues_on_the_open_session_without_a_room_cap(
        run, cli, capsys, tmp_path, monkeypatch):
    """No per-pair or per-room cap on DMs (the founder: no 5-message limit)."""
    wake, w, sp = _waker(run, tmp_path, monkeypatch)
    actions = []
    for i in range(wake.BOT_TOPIC_TURNS_PER_HOUR + 2):
        out = _dm(cli, capsys, "COO", "PM", f"point {i}")
        actions.append(w.on_message("PM", _as_seen_by(run, "PM", out["id"]), [])["action"])
    assert actions[0] == "spawn" and set(actions[1:]) <= {"queue", "resume"}, actions


# -- DoD: the DM shows in the "סוכנים" room view -------------------------------

def test_the_dm_shows_in_the_agents_room_view(run, cli, capsys):
    out = _dm(cli, capsys, "COO", "PM", "the order of #170 and #171")
    founder = run.api(run.founder)
    subs = founder("GET", "users/me/subscriptions")[1]["subscriptions"]
    assert AGENTS_ROOM in [s["name"] for s in subs]
    status, got = founder("GET", "messages", narrow=[{"operator": "channel",
                                                      "operand": AGENTS_ROOM}],
                          anchor="newest", num_before=100, num_after=0)
    assert status == 200, got
    (m,), = [got["messages"]]
    assert m["sender_full_name"] == "COO" and "PM" in m["content"]
    assert "the order of #170 and #171" in m["content"]
    # Only people read it: no bot is a member, so no bot is woken by it.
    members = {r[0] for r in run.store.q(
        "SELECT user_id FROM subscriptions WHERE stream_id=?",
        (run.store.stream_by_ref(AGENTS_ROOM)["stream_id"],))}
    assert members == {run.founder["id"]}
    assert out["id"] != m["id"]


def test_a_founder_dm_to_a_bot_is_not_shown_in_the_agents_room(run, cli, capsys):
    run.api(run.founder)("POST", "messages", type="private",
                         to=json.dumps([run.bots["PM"]["email"]]), content="private to PM")
    room = run.store.stream_by_ref(AGENTS_ROOM)
    if room is not None:
        assert run.store.one("SELECT count(*) FROM messages WHERE stream_id=?",
                             (room["stream_id"],))[0] == 0


def test_the_agents_room_is_read_only(run, cli, capsys):
    _dm(cli, capsys, "COO", "PM", "hello")
    for who in (run.bots["COO"], run.founder):
        status, body = run.api(who)("POST", "messages", type="stream", to=AGENTS_ROOM,
                                    content="may I?")
        assert status == 400 and body["code"] == "AGENTS_ROOM_READ_ONLY", body
        assert "--dm" in body["msg"]  # the error names the fix
    status, body = run.api(run.founder)("POST", "users/me/subscriptions",
                                        subscriptions=[{"name": AGENTS_ROOM}],
                                        principals=[run.bots["PM"]["email"]])
    assert status == 400 and body["code"] == "AGENTS_ROOM_READ_ONLY", body


def test_the_page_shows_the_agents_room_read_only():
    page = (ROOT / "chat" / "page.html").read_text(encoding="utf-8")
    assert f'AGENTS_ROOM = "{AGENTS_ROOM}"' in page
    assert 'id="readonly-note"' in page
    from test_chat_page import API_STUB, FAKE_DOM, _js
    prelude = FAKE_DOM + API_STUB + f"""
      const S = {{open: null, shown: new Set(), msgs: [], members: new Map(), streams: []}};
      const isTouch = () => true;
      function refreshCount() {{}} function markRoomRead() {{}}
      function renderStreams() {{}} function seedTyping() {{}}
      function addMessages() {{}}
      const ROOM_TOPIC = "chat"; const AGENTS_ROOM = "{AGENTS_ROOM}";
    """
    got = _js(("membersLabel", "openRoom", "firstNewId", "scrollToNew", "setReadOnly"), f"""
      await openRoom(5, "{AGENTS_ROOM}");
      out.agents = [$("write").hidden, $("readonly-note").hidden, $("invite-btn").disabled];
      await openRoom(4, "feature");
      out.feature = [$("write").hidden, $("readonly-note").hidden, $("invite-btn").disabled];
    """, prelude=prelude)
    assert got["agents"] == [True, False, True]  # no write box, the note, no invite
    assert got["feature"] == [False, True, False]


# -- DoD: a "קיבלתי" DM wakes nobody --------------------------------------------

@pytest.mark.parametrize("text", ["קיבלתי", "קיבלתי.", "Got it!", "  thanks ", "👍", "תודה רבה"])
def test_an_acknowledgement_dm_wakes_nobody(run, cli, capsys, tmp_path, monkeypatch, text):
    wake, w, sp = _waker(run, tmp_path, monkeypatch)
    out = _dm(cli, capsys, "PM", "COO", text)
    d = w.on_message("COO", _as_seen_by(run, "COO", out["id"]), [])
    assert d["action"] == "skip" and "acknowledgement" in d["reason"], d
    assert sp.started == []


def test_a_dm_that_only_starts_with_an_acknowledgement_still_wakes(
        run, cli, capsys, tmp_path, monkeypatch):
    wake, w, sp = _waker(run, tmp_path, monkeypatch)
    out = _dm(cli, capsys, "PM", "COO", "קיבלתי. ומה עם #171?")
    assert w.on_message("COO", _as_seen_by(run, "COO", out["id"]), [])["action"] == "spawn"


@pytest.mark.parametrize("text", ["אוקיי", "קיבלתי", "thanks"])
def test_a_founder_acknowledgement_dm_still_spawns(run, cli, capsys, tmp_path, monkeypatch, text):
    """The ack filter is for bot DMs only: the founder's "אוקיי" is a reply."""
    wake, w, sp = _waker(run, tmp_path, monkeypatch)
    status, out = run.api(run.founder)("POST", "messages", type="private",
                                       to=json.dumps([run.bots["COO"]["email"]]), content=text)
    assert status == 200, out
    d = w.on_message("COO", _as_seen_by(run, "COO", out["id"]), [])
    assert d["action"] == "spawn", d
    assert [r for r, _ in sp.started] == ["COO"]


def test_the_acknowledgement_list_is_short_and_fixed():
    wake = _load("wake")
    assert "קיבלתי" in wake.ACK_WORDS and "got it" in wake.ACK_WORDS
    assert len(wake.ACK_WORDS) <= 20
    assert not wake.is_ack("כן")  # an answer, not an acknowledgement
    assert not wake.is_ack("got it, but why #171?")


# -- the agent path: --json, and every error names the fix ---------------------

@pytest.mark.parametrize("who", ["cto", "CTO", "Pm"])
def test_dm_takes_any_company_role_in_any_case(run, cli, capsys, who):
    out = _dm(cli, capsys, "COO", who, "hi")
    assert out == {"ok": True, "id": out["id"], "dm": who.upper()}


@pytest.mark.parametrize("who,needle", [
    ("CFO", "COO, CTO, PM, CMO, CPSO"),     # not a role: names the ones there are
    ("PA", "not part of the company"),     # the PA is the founder's alone
    ("COO", "yourself"),                   # a DM to yourself
])
def test_dm_refusals_name_the_fix_and_send_nothing(run, cli, capsys, who, needle):
    assert cli.main(["--as", "COO", "send", "--dm", who, "--text", "hi", "--json"]) == 2
    out = capsys.readouterr()
    body = json.loads(out.out)
    assert body["ok"] is False and needle in body["error"], body
    assert "Nothing was sent" in out.err
    assert run.store.one("SELECT count(*) FROM messages")[0] == 0


def test_dm_to_a_role_whose_bot_email_is_unknown_names_the_env_line(
        run, cli, capsys, monkeypatch):
    monkeypatch.delenv("ZULIP_CMO_EMAIL", raising=False)
    assert cli.main(["--as", "COO", "send", "--dm", "CMO", "--text", "hi", "--json"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert "ZULIP_CMO_EMAIL" in body["error"] and "bot/.env" in body["error"]
    assert run.store.one("SELECT count(*) FROM messages")[0] == 0
