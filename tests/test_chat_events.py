"""chat/server.py's durable event queue (D12): POST /register, GET /events,
DELETE /events, over a real socket, with the long poll shortened.

The property the first attempt at our chat lost: a queue must hold its events
while nobody polls it, across a server restart, and hand back exactly what
was missed from a stored ``last_event_id``.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

from test_chat_messages import ROOT, Api, chat

HOOK = ROOT / "hooks" / "agora_hook.py"


class Running:
    """One server on a temp DB; ``restart()`` stops it and starts another on
    the same file, the way a crash and a relaunch would."""

    def __init__(self, db: str, poll: float = 0.5, seed: int | None = None):
        self.db, self.poll, self.seed = db, poll, seed
        self.start()

    def start(self):
        self.server = chat.make_server(self.db, port=0, poll_seconds=self.poll,
                                       seed_ids=self.seed)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.store = self.server.RequestHandlerClass.chat.s
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        with self.store.lock:
            self.store.db.close()

    def restart(self):
        self.stop()
        self.start()

    def api(self, user: dict) -> Api:
        return Api(self.base, user["email"], user["api_key"])


@pytest.fixture
def run(tmp_path):
    r = Running(str(tmp_path / "chat.sqlite3"))
    s = r.store
    r.cto = s.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    r.coo = s.create_user("coo-bot@chat.localhost", "COO", is_bot=True)
    r.feature = s.create_stream("feature", invite_only=True)
    s.subscribe(r.cto["id"], r.feature)
    yield r
    r.stop()


def _register(api, **params):
    status, reg = api("POST", "register", event_types=["message", "update_message",
                                                       "reaction"], **params)
    assert status == 200 and reg["result"] == "success", reg
    assert reg["last_event_id"] == -1 and reg["queue_id"]
    return reg["queue_id"]


def _post(api, text, to="feature", topic="t"):
    status, body = api("POST", "messages", type="stream", to=to, topic=topic, content=text)
    assert status == 200, body
    return body["id"]


def _poll(api, qid, last=-1, block=False):
    status, body = api("GET", "events", queue_id=qid, last_event_id=last,
                       dont_block="false" if block else "true")
    assert status == 200 and body["result"] == "success", body
    return body["events"]


def test_long_poll_returns_within_a_second_of_a_new_message(run):
    run.server.RequestHandlerClass.chat.poll_seconds = 5.0
    cto = run.api(run.cto)
    qid = _register(cto)
    got = {}

    def poll():
        got["events"] = _poll(cto, qid, block=True)
        got["at"] = time.monotonic()
    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.5)
    sent_at = time.monotonic()
    mid = _post(cto, "wake up")
    t.join(timeout=10)
    assert got["at"] - sent_at < 1.0
    [ev] = got["events"]
    assert ev["type"] == "message" and ev["message"]["id"] == mid


def test_an_idle_poll_ends_with_a_heartbeat_that_moves_no_cursor(run):
    cto = run.api(run.cto)
    qid = _register(cto)
    start = time.monotonic()
    events = _poll(cto, qid, block=True)
    assert 0.4 <= time.monotonic() - start < 5
    assert events == [{"type": "heartbeat", "id": -1}]
    # The next message after a heartbeat is still delivered.
    mid = _post(cto, "after the heartbeat")
    [ev] = _poll(cto, qid, last=events[0]["id"])
    assert ev["message"]["id"] == mid


def test_resume_from_a_stored_last_event_id_gets_exactly_the_missed_events(run):
    cto = run.api(run.cto)
    qid = _register(cto)
    ids = [_post(cto, f"m{i}") for i in range(4)]
    events = _poll(cto, qid)
    assert [e["message"]["id"] for e in events] == ids
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    stored = events[1]["id"]
    again = _poll(cto, qid, last=stored)
    assert [e["message"]["id"] for e in again] == ids[2:]
    # Asking again with the same id returns the same events.
    assert _poll(cto, qid, last=stored) == again
    assert _poll(cto, qid, last=again[-1]["id"]) == []


def test_a_queue_survives_a_server_stop_and_start_with_no_event_lost(run):
    qid = _register(run.api(run.cto))
    first = _post(run.api(run.cto), "before the restart")
    run.restart()
    cto = run.api(run.cto)
    second = _post(cto, "after the restart")
    events = _poll(cto, qid)
    assert [e["message"]["id"] for e in events] == [first, second]
    assert [e["message"]["content"] for e in events] == ["before the restart",
                                                         "after the restart"]


def test_unknown_or_deleted_or_expired_queue_gives_bad_event_queue_id(run):
    cto, coo = run.api(run.cto), run.api(run.coo)
    status, body = cto("GET", "events", queue_id="no-such-queue", last_event_id=-1)
    assert status == 400 and body["code"] == "BAD_EVENT_QUEUE_ID" and body["msg"]
    qid = _register(cto)
    assert coo("GET", "events", queue_id=qid, last_event_id=-1)[1]["code"] == \
        "BAD_EVENT_QUEUE_ID"  # someone else's queue
    assert cto("DELETE", "events", queue_id=qid)[0] == 200
    assert cto("GET", "events", queue_id=qid, last_event_id=-1)[1]["code"] == \
        "BAD_EVENT_QUEUE_ID"
    # Seven days unpolled is gone; six days is not.
    old, fresh = _register(cto), _register(cto)
    now = int(time.time())
    run.store.x("UPDATE queues SET last_poll=? WHERE queue_id=?", (now - 8 * 86400, old))
    run.store.x("UPDATE queues SET last_poll=? WHERE queue_id=?", (now - 6 * 86400, fresh))
    assert cto("GET", "events", queue_id=old, last_event_id=-1)[1]["code"] == \
        "BAD_EVENT_QUEUE_ID"
    assert cto("GET", "events", queue_id=fresh, last_event_id=-1,
               dont_block="true")[0] == 200


def test_no_event_leaks_from_a_private_stream_to_a_non_subscriber(run):
    cto, coo = run.api(run.cto), run.api(run.coo)
    spy = _register(coo)
    narrowed = _register(coo, narrow=[["stream", "feature"]])
    mid = _post(cto, "founder only")
    cto("PATCH", f"messages/{mid}", content="edited secret")
    cto("POST", f"messages/{mid}/reactions", emoji_name="eyes")
    assert _poll(coo, spy) == [] and _poll(coo, narrowed) == []


def test_flags_ride_on_the_event_and_all_is_a_mention(run):
    cto, coo = run.api(run.cto), run.api(run.coo)
    run.store.subscribe(run.coo["id"], run.feature)
    qid = _register(cto)
    _post(coo, "@**CTO** please design this")
    _post(coo, "@**all** standup")
    _post(coo, "no tag")
    events = _poll(cto, qid)
    assert [e["flags"] for e in events] == [["mentioned"], ["mentioned"], []]
    ev = events[0]
    assert ev["type"] == "message" and ev["message"]["sender_email"] == "coo-bot@chat.localhost"
    assert ev["message"]["display_recipient"] == "feature"


def test_edits_and_reactions_are_events_too(run):
    cto = run.api(run.cto)
    qid = _register(cto)
    only_messages = cto("POST", "register", event_types=["message"])[1]["queue_id"]
    mid = _post(cto, "draft")
    cto("PATCH", f"messages/{mid}", content="final")
    cto("POST", f"messages/{mid}/reactions", emoji_name="thumbs_up", emoji_code="1f44d")
    events = _poll(cto, qid)
    assert [e["type"] for e in events] == ["message", "update_message", "reaction"]
    edit, react = events[1], events[2]
    assert (edit["message_id"], edit["orig_content"], edit["content"]) == (mid, "draft", "final")
    assert (react["op"], react["message_id"], react["emoji_name"], react["user"]["email"]) == \
        ("add", mid, "thumbs_up", "cto-bot@chat.localhost")
    assert [e["type"] for e in _poll(cto, only_messages)] == ["message"]


def test_reaction_remove_is_an_event_too(run):
    """ops#249 batch 1: DELETE reactions publishes op=remove, the same way
    POST already publishes op=add — a live page can toggle its own chip
    without waiting for a reload."""
    cto = run.api(run.cto)
    mid = _post(cto, "ok?")
    cto("POST", f"messages/{mid}/reactions", emoji_name="thumbs_up", emoji_code="1f44d")
    qid = _register(cto)
    cto("DELETE", f"messages/{mid}/reactions", emoji_name="thumbs_up", emoji_code="1f44d")
    [ev] = _poll(cto, qid)
    assert (ev["type"], ev["op"], ev["message_id"], ev["emoji_name"]) == \
        ("reaction", "remove", mid, "thumbs_up")


def test_typing_events_reach_every_subscriber_but_not_a_stranger(run):
    """ops#249 batch 1: a 'typing' event rides the same queue, filtered the
    same way as every other event type — only a subscriber of the stream
    sees it, and only a queue that asked for it."""
    cto, coo = run.api(run.cto), run.api(run.coo)
    run.store.subscribe(run.coo["id"], run.feature)
    qid = coo("POST", "register", event_types=["typing"])[1]["queue_id"]
    only_messages = coo("POST", "register", event_types=["message"])[1]["queue_id"]
    status, body = cto("POST", "typing", type="stream", to="feature", topic="t",
                       op="start", status="working")
    assert status == 200, body
    [ev] = _poll(coo, qid)
    assert (ev["type"], ev["op"], ev["status"], ev["topic"]) == \
        ("typing", "start", "working", "t")
    assert ev["sender"]["email"] == "cto-bot@chat.localhost"
    assert _poll(coo, only_messages) == []  # did not ask for "typing"
    cto("POST", "typing", type="stream", to="feature", topic="t", op="stop")
    [stop_ev] = _poll(coo, qid, last=ev["id"])
    assert stop_ev["op"] == "stop"
    # A stranger (not subscribed to #feature) never sees it, same as a message.
    pm_bot = run.store.create_user("pm-bot@chat.localhost", "PM", is_bot=True)
    pm = run.api(pm_bot)
    stray_qid = pm("POST", "register", event_types=["typing"])[1]["queue_id"]
    cto("POST", "typing", type="stream", to="feature", topic="t", op="start")
    assert _poll(pm, stray_qid) == []


def test_register_narrow_filters_messages(run):
    cto = run.api(run.cto)
    run.store.subscribe(run.cto["id"], run.store.create_stream("cto"))
    qid = _register(cto, narrow=[["stream", "cto"]])
    _post(cto, "elsewhere", to="feature")
    mid = _post(cto, "here", to="cto")
    assert [e["message"]["id"] for e in _poll(cto, qid)] == [mid]
    status, body = cto("POST", "register", narrow=[["search", "x"]])
    assert status == 400 and body["code"] == "BAD_NARROW"


def test_direct_messages_reach_the_recipients_queues(run):
    cto, coo = run.api(run.cto), run.api(run.coo)
    qid = _register(coo)
    status, sent = cto("POST", "messages", type="private", to=["coo-bot@chat.localhost"],
                       content="just us")
    [ev] = _poll(coo, qid)
    assert ev["message"]["id"] == sent["id"] and ev["message"]["type"] == "private"


def _load_hook(monkeypatch, tmp_path: Path):
    spec = importlib.util.spec_from_file_location("agora_hook_under_chat", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mod, "DOTENV", tmp_path / "missing.env")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    return mod


def test_zulip_era_hook_state_receives_the_first_new_message(tmp_path, monkeypatch):
    """Ids continue from Zulip's: a hook whose saved floor is Zulip's message
    600 and whose saved queue only Zulip knew must still wake on the first
    message this server takes (#61 item 1). BAD_EVENT_QUEUE_ID, a new queue,
    a backfill from 600: the hook's own path, unedited."""
    r = Running(str(tmp_path / "chat.sqlite3"), seed=600)
    try:
        s = r.store
        coo = s.create_user("coo-bot@chat.localhost", "COO", is_bot=True)
        human = s.create_user("test-human@example.invalid", "Test Human", role=chat.ROLE_ADMIN)
        sid = s.create_stream("coo")
        s.subscribe(coo["id"], sid)
        s.subscribe(human["id"], sid)
        hook = _load_hook(monkeypatch, tmp_path)
        (tmp_path / "agora-wait-floor.json").write_text(json.dumps(
            {"queue_id": "1700000000:zulip-era", "last_event_id": 41,
             "last_message_id": 600, "backfill": False}), encoding="utf-8")
        mid = _post(r.api(human), "first message on our own server", to="coo")
        assert mid == 601
        code = hook.wait_for((r.base, coo["email"], coo["api_key"]), "COO", "floor", 15)
        assert code == 2
        state = json.loads((tmp_path / "agora-wait-floor.json").read_text("utf-8"))
        assert state["last_message_id"] == 601 and state["queue_id"] != "1700000000:zulip-era"
    finally:
        r.stop()
