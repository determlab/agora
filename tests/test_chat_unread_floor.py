"""issue #110: the read floor is per user *and* stream, so history from
before a user joined a stream — or from before the user existed at all — is
not unread. Builds on issue #168's ``read_floor`` (issue #109), which was
per user only and so could not cover a later join.
"""
from __future__ import annotations

import sqlite3
import threading

from test_chat_messages import Api, chat, env  # noqa: F401  (fixture)


def test_a_new_user_does_not_see_pre_existing_history_as_unread(env):
    """DoD: create a user after a stream has 5 messages; the unread count
    for that user is 0, and the next message makes it 1."""
    cto, store = env["cto"], env["store"]
    for i in range(5):
        status, _ = cto("POST", "messages", type="stream", to="feature",
                        topic="t", content=str(i))
        assert status == 200
    newbie = store.create_user("newbie@chat.localhost", "Newbie", is_bot=True)
    assert store.subscribe(newbie["id"], env["feature"]) is True
    newbie_api = Api(env["base"], newbie["email"], newbie["api_key"])

    assert newbie_api("GET", "unread")[1]["streams"] == []

    cto("POST", "messages", type="stream", to="feature", topic="t", content="new")
    status, unread = newbie_api("GET", "unread")
    assert status == 200
    [feature] = unread["streams"]
    assert feature["unread"] == 1


def test_joining_a_stream_does_not_count_its_history_as_unread(env):
    """DoD: an existing user joins a stream that has 5 messages; unread for
    that stream is 0, and the next message makes it 1. Joining again
    (already subscribed) does not move the floor."""
    cto, coo, store = env["cto"], env["coo"], env["store"]
    for i in range(5):
        status, _ = cto("POST", "messages", type="stream", to="feature",
                        topic="t", content=str(i))
        assert status == 200

    # COO exists from the `env` fixture but is not subscribed to "feature" yet.
    assert store.subscribe(env["coo_user"]["id"], env["feature"]) is True
    assert coo("GET", "unread")[1]["streams"] == []

    cto("POST", "messages", type="stream", to="feature", topic="t", content="new")
    status, unread = coo("GET", "unread")
    assert status == 200
    [feature] = unread["streams"]
    assert feature["unread"] == 1

    floor_before = store.read_floor(env["coo_user"]["id"], env["feature"])
    assert store.subscribe(env["coo_user"]["id"], env["feature"]) is False
    assert store.read_floor(env["coo_user"]["id"], env["feature"]) == floor_before
    assert coo("GET", "unread")[1]["streams"][0]["unread"] == 1


def test_migration_keeps_todays_per_user_floor_for_every_stream_the_user_was_already_in(
        tmp_path):
    """DoD: the migration keeps today's per-user floor for every stream the
    user was already in. This is a database written by the per-user-only
    ``read_floor`` (issue #109's shape, one row per user): moving to a
    per-stream floor must not turn that already-read history unread again."""
    path = str(tmp_path / "old_floor.sqlite3")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
            is_bot INTEGER NOT NULL, api_key TEXT NOT NULL, role INTEGER NOT NULL,
            date_joined INTEGER NOT NULL);
        CREATE TABLE streams (
            stream_id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL,
            invite_only INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '',
            is_archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE subscriptions (
            user_id INTEGER NOT NULL, stream_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, stream_id));
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL,
            type TEXT NOT NULL, stream_id INTEGER, subject TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL, timestamp INTEGER NOT NULL, last_edit_timestamp INTEGER);
        CREATE TABLE read_floor (user_id INTEGER PRIMARY KEY, floor_id INTEGER NOT NULL);
        INSERT INTO users VALUES (1, 'cto-bot@chat.localhost', 'CTO', 1, 'k1', 400, 1);
        INSERT INTO streams VALUES (7, 'alpha', 1, '', 0);
        INSERT INTO streams VALUES (8, 'beta', 1, '', 0);
        INSERT INTO subscriptions VALUES (1, 7);
        INSERT INTO subscriptions VALUES (1, 8);
        INSERT INTO messages (sender_id, type, stream_id, subject, content, timestamp)
            VALUES (1, 'stream', 7, 't', 'already read under the old floor', 1);
        INSERT INTO read_floor VALUES (1, 42);
    """)
    db.commit()
    db.close()

    store = chat.Store(path)
    assert store.read_floor(1, 7) == 42 and store.read_floor(1, 8) == 42
    # The per-user row is gone, moved into one row per subscribed stream —
    # not left behind alongside the new per-stream rows.
    assert {(r["user_id"], r["stream_id"], r["floor_id"])
            for r in store.q("SELECT * FROM read_floor")} == {(1, 7, 42), (1, 8, 42)}
    store.db.close()
    chat.Store(path).db.close()  # a second start does not re-migrate or crash

    server = chat.make_server(path, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    cto = Api(base, "cto-bot@chat.localhost", "k1")
    try:
        # Nothing pre-existing is unread: the migrated floor (42) is above
        # every message id in this tiny fixture database.
        assert cto("GET", "unread")[1]["streams"] == []
    finally:
        server.shutdown()
        server.server_close()
