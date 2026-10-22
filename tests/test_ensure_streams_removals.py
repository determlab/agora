"""Issue #198: a restart (``up_setup`` -> ``ensure_streams``) must not re-add a
role bot that a person removed from a room, and every add the server makes is
recorded with no actor (``actor_id`` NULL is the server)."""
from __future__ import annotations

import threading

from test_chat_messages import Api, chat

ROLES = ("COO", "CTO", "CMO", "PM", "Pool", "Watchdog", "PA")


def _setup(tmp_path):
    server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store = server.RequestHandlerClass.chat.s
    base = f"http://127.0.0.1:{server.server_address[1]}"
    bots = {n: store.create_user(f"{n.lower()}-bot@chat.localhost", n, is_bot=True)
            for n in ROLES}
    human = store.create_user("founder@example.invalid", "Founder", is_bot=False,
                              role=chat.ROLE_ADMIN)
    return server, store, base, bots, human


def _members(store, room):
    sid = store.stream_by_ref(room)["stream_id"]
    return {r["full_name"] for r in store.q(
        "SELECT u.full_name FROM users u JOIN subscriptions x ON x.user_id=u.id "
        "WHERE x.stream_id=?", (sid,))}


def test_removed_bot_is_not_re_added_by_a_restart(tmp_path):
    server, store, base, bots, human = _setup(tmp_path)
    try:
        chat.ensure_streams(store)
        assert "PM" in _members(store, "coo")
        api = Api(base, human["email"], human["api_key"])
        code, body = api("DELETE", "users/me/subscriptions", subscriptions=["coo"],
                         principals=[bots["PM"]["email"]])
        assert code == 200 and body["removed"] == ["coo"], body
        chat.ensure_streams(store)  # the restart
        assert "PM" not in _members(store, "coo")
        assert "PM" in _members(store, "PM")  # rooms it was not removed from stay
    finally:
        server.shutdown()


def test_new_room_still_gets_its_default_bots(tmp_path):
    server, store, base, bots, human = _setup(tmp_path)
    try:
        chat.ensure_streams(store)
        sid = store.stream_by_ref("cto")["stream_id"]
        store.x("DELETE FROM subscriptions WHERE stream_id=?", (sid,))
        store.x("DELETE FROM streams WHERE stream_id=?", (sid,))
        chat.ensure_streams(store)  # "cto" is created again by setup
        assert "CTO" in _members(store, "cto")
    finally:
        server.shutdown()


def test_every_server_add_is_logged_with_no_actor(tmp_path):
    server, store, base, bots, human = _setup(tmp_path)
    try:
        chat.ensure_streams(store)
        chat.bootstrap_pool(store)
        rows = store.q("SELECT op, actor_id FROM membership_log")
        assert rows and all(r["op"] == "add" and r["actor_id"] is None for r in rows)
    finally:
        server.shutdown()
