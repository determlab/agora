"""Archive (hide) a stream or a topic, and restore it. Nothing is deleted.

A stream archives with Zulip's own ``DELETE /streams/{id}`` and comes back
with ``PATCH /streams/{id} is_archived=false``; a topic (Zulip has no topic
archive) with ``POST`` / ``DELETE /streams/{id}/archived_topics``. The lists
leave archived rooms out by default; the messages stay readable; a new message
to an archived topic brings it back.
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from test_chat_messages import Api, chat
from test_chat_page import API_STUB, FAKE_DOM, _js, page_api, world  # noqa: F401  (fixture)
from test_zulip_cli import _load as _load_cli


@pytest.fixture
def arc(tmp_path):
    server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store = server.RequestHandlerClass.chat.s
    base = f"http://127.0.0.1:{server.server_address[1]}"
    admin = store.create_user("admin@chat.localhost", "Admin", role=chat.ROLE_ADMIN)
    cto = store.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    sid = store.create_stream("feature", invite_only=True)
    for u in (admin, cto):
        store.subscribe(u["id"], sid)
    yield {"store": store, "base": base, "sid": sid, "admin_user": admin,
           "admin": Api(base, admin["email"], admin["api_key"]),
           "cto": Api(base, cto["email"], cto["api_key"])}
    server.shutdown()
    server.server_close()


def _send(api, topic, text="hi", to="feature"):
    status, body = api("POST", "messages", type="stream", to=to, topic=topic, content=text)
    return status, body


def _names(api, path="users/me/subscriptions", key="subscriptions", **params):
    status, body = api("GET", path, **params)
    assert status == 200, body
    return {s["name"]: s.get("is_archived") for s in body[key]}


def _topics(api, sid, **params):
    status, body = api("GET", f"users/me/{sid}/topics", **params)
    assert status == 200, body
    return {t["name"]: t["is_archived"] for t in body["topics"]}


def _count(store, sid):
    return store.one("SELECT count(*) FROM messages WHERE stream_id=?", (sid,))[0]


# -- streams


def test_archive_and_unarchive_a_stream_keeps_its_messages(arc):
    admin, sid, store = arc["admin"], arc["sid"], arc["store"]
    assert _send(admin, "t1", "one")[0] == 200
    assert _names(admin) == {"feature": False}

    status, body = admin("DELETE", f"streams/{sid}")
    assert status == 200, body
    # Left out of every list by default, shown on request with the flag set.
    assert _names(admin) == {}
    assert _names(admin, "streams", "streams") == {}
    assert _names(admin, include_archived="true") == {"feature": True}
    assert _names(admin, "streams", "streams", exclude_archived="false") == {"feature": True}
    # The history stays: in the database, and readable through the API.
    assert _count(store, sid) == 1
    status, body = admin("GET", "messages", anchor="newest", num_before=10, num_after=0,
                         narrow=[{"operator": "channel", "operand": "feature"}])
    assert status == 200 and [m["content"] for m in body["messages"]] == ["one"]
    # Posting to it is refused, and the error says how to restore it.
    status, body = _send(arc["cto"], "t1")
    assert status == 400 and body["code"] == "STREAM_ARCHIVED"
    assert f"PATCH /streams/{sid} is_archived=false" in body["msg"]

    status, body = admin("PATCH", f"streams/{sid}", is_archived="false")
    assert status == 200, body
    assert _names(admin) == {"feature": False}
    assert _send(arc["cto"], "t1", "two")[0] == 200
    assert _count(store, sid) == 2


def test_patch_is_archived_true_archives_too(arc):
    status, body = arc["admin"]("PATCH", f"streams/{arc['sid']}", is_archived="true")
    assert status == 200, body
    assert _names(arc["admin"]) == {}


def test_a_member_cannot_archive_or_restore_a_stream(arc):
    status, body = arc["cto"]("DELETE", f"streams/{arc['sid']}")
    assert status == 400 and "administrator" in body["msg"]
    assert _names(arc["cto"]) == {"feature": False}


# -- topics


def test_archive_and_unarchive_a_topic_keeps_its_messages(arc):
    admin, cto, sid, store = arc["admin"], arc["cto"], arc["sid"], arc["store"]
    for topic in ("old", "live"):
        assert _send(admin, topic)[0] == 200
    assert _topics(admin, sid) == {"old": False, "live": False}

    # Any member of the stream may archive a topic.
    status, body = cto("POST", f"streams/{sid}/archived_topics", topic="old")
    assert status == 200 and body["topics"] == ["old"], body
    assert _topics(admin, sid) == {"live": False}
    assert _topics(admin, sid, include_archived="true") == {"old": True, "live": False}
    status, body = admin("GET", f"streams/{sid}/archived_topics")
    assert body["topics"] == ["old"]
    assert _count(store, sid) == 2
    status, body = admin("GET", "messages", anchor="newest", num_before=10, num_after=0,
                         narrow=[{"operator": "channel", "operand": "feature"},
                                 {"operator": "topic", "operand": "old"}])
    assert status == 200 and len(body["messages"]) == 1

    status, body = cto("DELETE", f"streams/{sid}/archived_topics", topic="OLD")
    assert status == 200 and body["topics"] == [], body
    assert _topics(admin, sid) == {"old": False, "live": False}


def test_a_new_message_unarchives_its_topic(arc):
    admin, sid = arc["admin"], arc["sid"]
    assert _send(admin, "old", "first")[0] == 200
    assert admin("POST", f"streams/{sid}/archived_topics", topic="old")[0] == 200
    assert _topics(admin, sid) == {}
    assert _send(arc["cto"], "Old", "back")[0] == 200  # topics match without case
    # One topic, listed again, under its newest spelling (as Zulip does).
    assert _topics(admin, sid) == {"Old": False}
    assert admin("GET", f"streams/{sid}/archived_topics")[1]["topics"] == []
    assert _count(arc["store"], sid) == 2


def test_archiving_an_unknown_topic_or_no_topic_is_refused(arc):
    admin, sid = arc["admin"], arc["sid"]
    status, body = admin("POST", f"streams/{sid}/archived_topics", topic="nosuch")
    assert status == 400 and f"GET /users/me/{sid}/topics" in body["msg"]
    status, body = admin("POST", f"streams/{sid}/archived_topics")
    assert status == 400 and body["code"] == "REQUEST_VARIABLE_MISSING"


def test_a_non_member_cannot_touch_a_private_streams_topics(arc):
    store = arc["store"]
    out = store.create_user("cmo-bot@chat.localhost", "CMO", is_bot=True)
    cmo = Api(arc["base"], out["email"], out["api_key"])
    assert _send(arc["admin"], "t")[0] == 200
    status, body = cmo("POST", f"streams/{arc['sid']}/archived_topics", topic="t")
    assert status == 400 and body["code"] == "STREAM_DOES_NOT_EXIST"


# -- the migration


def test_an_old_database_gains_the_archive_columns_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE streams (
            stream_id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL,
            invite_only INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '');
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL,
            type TEXT NOT NULL, stream_id INTEGER, subject TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL, timestamp INTEGER NOT NULL, last_edit_timestamp INTEGER);
        INSERT INTO streams VALUES (7, 'coo', 1, 'the COO');
        INSERT INTO messages (sender_id, type, stream_id, subject, content, timestamp)
            VALUES (1, 'stream', 7, 'hello', 'kept', 1);
    """)
    db.commit()
    db.close()
    store = chat.Store(path)
    row = store.one("SELECT * FROM streams WHERE stream_id=7")
    assert (row["name"], row["description"], row["is_archived"]) == ("coo", "the COO", 0)
    assert store.one("SELECT content FROM messages")[0] == "kept"
    assert store.q("SELECT * FROM archived_topics") == []
    store.db.close()
    chat.Store(path).db.close()  # a second start does not migrate twice


# -- the agent path: bot/zulip.py archive|unarchive --json


@pytest.fixture
def cli(arc, monkeypatch, tmp_path):
    mod = _load_cli()
    monkeypatch.setattr(mod, "DOTENV", str(tmp_path / "missing.env"))
    monkeypatch.setenv("ZULIP_SITE", arc["base"])
    monkeypatch.setenv("ZULIP_BOT_EMAIL", arc["admin_user"]["email"])
    monkeypatch.setenv("ZULIP_BOT_API_KEY", arc["admin_user"]["api_key"])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    return mod


def _json_out(capsys):
    return json.loads(capsys.readouterr().out)


def test_cli_archives_and_restores_a_topic_and_a_stream(arc, cli, capsys):
    admin, sid = arc["admin"], arc["sid"]
    assert _send(admin, "old")[0] == 200

    assert cli.main(["archive", "--stream", "feature", "--topic", "old", "--json"]) == 0
    assert _json_out(capsys) == {"ok": True, "action": "archive", "stream": "feature",
                                 "stream_id": sid, "topic": "old"}
    assert _topics(admin, sid) == {}
    assert cli.main(["unarchive", "--stream", "feature", "--topic", "old"]) == 0
    assert capsys.readouterr().out.strip() == "restored #feature › old"
    assert _topics(admin, sid) == {"old": False}

    assert cli.main(["archive", "--stream", "#feature", "--json"]) == 0
    assert _json_out(capsys)["topic"] is None
    assert _names(admin) == {}
    assert cli.main(["unarchive", "--stream", "feature", "--json"]) == 0
    assert _json_out(capsys)["action"] == "unarchive"
    assert _names(admin) == {"feature": False}


def test_cli_refusal_is_json_and_a_nonzero_exit(arc, cli, capsys):
    assert cli.main(["archive", "--stream", "feature", "--topic", "nosuch", "--json"]) == 1
    out = capsys.readouterr()
    body = json.loads(out.out)
    assert body["ok"] is False and "nosuch" in body["error"]
    assert "refused" in out.err


# -- the page


def test_the_pages_archive_calls_reach_the_server_as_it_sends_them(world):
    run, human = world["run"], world["human"]
    store = run.store
    sid = store.stream_by_ref("feature")["stream_id"]
    assert page_api(run, human, "POST", "messages", type="stream", to="feature",
                    topic="old", content="x")[0] == 200
    got = _js(("archiveTopic", "restoreTopic", "setStreamArchived"), f"""
      await archiveTopic({sid}, "old");
      await restoreTopic({sid}, "old");
      await setStreamArchived({sid}, true);
      await setStreamArchived({sid}, false);
      out.calls = calls;
    """, prelude=API_STUB)
    assert got["calls"] == [
        ["POST", f"streams/{sid}/archived_topics", {"topic": "old"}],
        ["DELETE", f"streams/{sid}/archived_topics", {"topic": "old"}],
        ["PATCH", f"streams/{sid}", {"is_archived": "true"}],
        ["PATCH", f"streams/{sid}", {"is_archived": "false"}]]
    # Replayed against the real server, each one does what it says.
    expect = [{"old": True}, {"old": False}]
    for (method, path, params), want in zip(got["calls"][:2], expect):
        status, body = page_api(run, human, method, path, **params)
        assert status == 200, body
        status, body = page_api(run, human, "GET", f"users/me/{sid}/topics",
                                include_archived="true")
        assert {t["name"]: t["is_archived"] for t in body["topics"]} == want
    for (method, path, params), want in zip(got["calls"][2:], (True, False)):
        status, body = page_api(run, human, method, path, **params)
        assert status == 200, body
        status, body = page_api(run, human, "GET", "users/me/subscriptions",
                                include_archived="true")
        assert {s["name"]: s["is_archived"] for s in body["subscriptions"]}["feature"] is want


def test_the_sidebar_archives_a_topic_and_the_archive_list_restores():
    prelude = FAKE_DOM + API_STUB + """
      const CARET = "c", GEAR = "g", SVG = "http://www.w3.org/2000/svg";
      const opened = [];
      let loads = 0;
      function openTopic(...a) { opened.push(a); }
      function openMembers() {}
      async function loadStreams() { loads += 1; }
      const S = {streams: [{stream_id: 1, name: "feature", invite_only: true}],
                 topics: new Map([["feature", new Map([["live", 9]])]]),
                 archivedStreams: [{stream_id: 2, name: "old-room", invite_only: false}],
                 archivedTopics: new Map([["feature", ["done"]]]),
                 collapsed: new Set(), open: null, adding: null};
    """
    got = _js(("svgIcon", "renderStreams", "newTopicItem", "renderArchive", "archiveAction",
               "archiveTopic", "restoreTopic", "setStreamArchived"), """
      renderStreams();
      renderArchive();
      const arc = $("streams").querySelectorAll("button.topic-archive");
      out.buttons = arc.map((b) => b.textContent);
      await arc[0].on.click();
      const rows = $("archived").querySelectorAll("li.archived-row");
      out.rows = rows.map((r) => r.querySelector(".archived-name").textContent);
      out.label = $("archive-btn").textContent;
      await rows[0].querySelector("button.icon").on.click();
      await rows[1].querySelector("button.icon").on.click();
      rows[1].querySelector("button.archived-name").on.click();
      out.calls = calls; out.opened = opened; out.loads = loads; out.toasts = toasts;
    """, prelude=prelude)
    assert got["buttons"] == ["ארכב"]
    assert got["rows"] == ["#old-room", "#feature › done"]
    assert got["label"] == "ארכיון · 2"
    assert got["calls"] == [["POST", "streams/1/archived_topics", {"topic": "live"}],
                            ["PATCH", "streams/2", {"is_archived": "false"}],
                            ["DELETE", "streams/1/archived_topics", {"topic": "done"}]]
    # Each action reads the lists back from the server; an archived topic still opens.
    assert got["loads"] == 3 and got["opened"] == [[1, "feature", "done"]]
    assert all(not bad for _, bad in got["toasts"])
