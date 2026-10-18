"""Issue #159: hooks/agora_hook.py finds its multi-bot rooms against the real
chat server, not a Zulip-shaped fake.

``_multi_bot_rooms()`` reads ``users/me/subscriptions?include_subscribers=true``;
chat/server.py ignored that flag, so no room had a ``subscribers`` list and
the #146 rule never fired. The founder here is a test human, never his account.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from test_chat_events import Running
from test_chat_messages import chat
from test_chat_setup import HUMAN

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "agora_hook.py"
ROOM = "פגישה דייזין פול"


def _load():
    spec = importlib.util.spec_from_file_location("agora_hook_real_server", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def run(tmp_path, monkeypatch):
    for var in ("no_proxy", "NO_PROXY"):  # the hook's urllib must not proxy localhost
        monkeypatch.setenv(var, "*")
    r = Running(str(tmp_path / "chat.sqlite3"))
    s = r.store
    r.founder = chat.add_human(s, HUMAN, "Test Human")
    r.bots = {name: s.create_user(f"{name.lower()}-bot@chat.localhost", name, is_bot=True)
              for name in ("COO", "CTO")}
    yield r
    r.stop()


def _creds(run, name):
    bot = run.bots[name]
    return run.base, bot["email"], bot["api_key"]


def test_subscriptions_list_their_members_only_when_asked(run):
    s = run.store
    room = s.create_stream(ROOM, invite_only=True)
    for u in (run.founder, *run.bots.values()):
        s.subscribe(u["id"], room)
    api = run.api(run.bots["COO"])
    status, body = api("GET", "users/me/subscriptions", include_subscribers="true")
    assert status == 200, body
    (sub,) = body["subscriptions"]
    assert sub["subscribers"] == sorted(
        u["id"] for u in (run.founder, *run.bots.values()))
    status, body = api("GET", "users/me/subscriptions")
    assert status == 200 and "subscribers" not in body["subscriptions"][0]


def test_multi_bot_rooms_finds_a_hebrew_room_with_two_role_bots(run):
    s = run.store
    room = s.create_stream(ROOM, invite_only=True)
    own = s.create_stream("coo", invite_only=True)
    for u in (run.founder, *run.bots.values()):
        s.subscribe(u["id"], room)
    s.subscribe(run.founder["id"], own)
    s.subscribe(run.bots["COO"]["id"], own)
    hook = _load()
    assert hook._multi_bot_rooms(_creds(run, "COO")) == {ROOM.casefold()}
    assert hook._multi_bot_rooms(_creds(run, "CTO")) == {ROOM.casefold()}
    # and the untagged founder post there wakes the COO (the #146 rule)
    event = {"type": "message", "flags": [], "message": {
        "type": "stream", "display_recipient": ROOM, "sender_email": HUMAN,
        "content": "בלי תיוג"}}
    assert hook._wakes(event, run.bots["COO"]["email"], "COO",
                       hook._multi_bot_rooms(_creds(run, "COO")))
