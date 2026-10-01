"""chat/server.py, the messages part of Zulip's API, over a real socket.

Most tests run the server in a thread and speak HTTP to it. The last one runs
it the way an agent does — ``serve`` and ``bootstrap --json`` as processes —
and drives the unchanged ``bot/zulip.py send|read`` against it: compatibility
with the tools we already have is the whole point of this server (issue #61).
"""
from __future__ import annotations

import ast
import base64
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "chat" / "server.py"
ZULIP_CLI = ROOT / "bot" / "zulip.py"
# Never through a proxy, never towards the real Zulip.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _load():
    spec = importlib.util.spec_from_file_location("chat_server_under_test", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chat = _load()


class Api:
    def __init__(self, base: str, email: str, key: str):
        self.base = base
        self.auth = "Basic " + base64.b64encode(f"{email}:{key}".encode()).decode()

    def __call__(self, method: str, path: str, **params):
        enc = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (list, dict)) else v
                                      for k, v in params.items()})
        url, body = f"{self.base}/api/v1/{path}", None
        if method == "GET":
            url += "?" + enc
        else:
            body = enc.encode()
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self.auth)
        try:
            with OPENER.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


@pytest.fixture
def env(tmp_path):
    server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store = server.RequestHandlerClass.chat.s
    base = f"http://127.0.0.1:{server.server_address[1]}"
    cto = store.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    coo = store.create_user("coo-bot@chat.localhost", "COO", is_bot=True)
    feature = store.create_stream("feature", invite_only=True)
    store.subscribe(cto["id"], feature)
    yield {"store": store, "cto": Api(base, cto["email"], cto["api_key"]),
           "coo": Api(base, coo["email"], coo["api_key"]), "coo_user": coo,
           "base": base, "feature": feature}
    server.shutdown()
    server.server_close()


def test_send_then_read_back_by_anchor_and_narrow(env):
    cto = env["cto"]
    ids = []
    for topic, text in [("#61 m1", "one"), ("other", "two"), ("#61 m1", "three")]:
        status, body = cto("POST", "messages", type="stream", to="feature",
                           topic=topic, content=text)
        assert status == 200 and body["result"] == "success", body
        ids.append(body["id"])
    assert ids == sorted(ids)

    narrow = [{"operator": "channel", "operand": "feature"},
              {"operator": "topic", "operand": "#61 m1"}]
    _, got = cto("GET", "messages", anchor="newest", num_before=100, num_after=0, narrow=narrow)
    assert [m["content"] for m in got["messages"]] == ["one", "three"]
    m = got["messages"][0]
    assert m["sender_email"] == "cto-bot@chat.localhost" and m["sender_full_name"] == "CTO"
    assert (m["display_recipient"], m["subject"], m["type"]) == ("feature", "#61 m1", "stream")
    assert m["stream_id"] == env["feature"] and isinstance(m["timestamp"], int)

    # Strictly after an id, the way bot/zulip.py read --since asks.
    _, got = cto("GET", "messages", anchor=ids[0], include_anchor="false",
                 num_before=0, num_after=1000, narrow=[["stream", "feature"]])
    assert [m["id"] for m in got["messages"]] == ids[1:] and got["found_newest"]
    _, got = cto("GET", "messages", anchor="oldest", num_before=0, num_after=1)
    assert [m["id"] for m in got["messages"]] == ids[:2] and not got["found_newest"]
    _, got = cto("GET", "messages", anchor=ids[1], num_before=1, num_after=0)
    assert [m["id"] for m in got["messages"]] == ids[:2] and got["found_anchor"]
    _, got = cto("GET", "messages", anchor="newest", num_before=10, num_after=0,
                 narrow=[{"operator": "sender", "operand": "cto-bot@chat.localhost"}])
    assert len(got["messages"]) == 3


def test_edit_content_and_topic(env):
    cto = env["cto"]
    _, sent = cto("POST", "messages", type="stream", to="feature", topic="t", content="draft")
    status, body = cto("PATCH", f"messages/{sent['id']}", content="final", topic="t2")
    assert status == 200 and body["result"] == "success", body
    _, got = cto("GET", "messages", anchor=sent["id"], num_before=0, num_after=0)
    m = got["messages"][0]
    assert (m["content"], m["subject"]) == ("final", "t2") and "last_edit_timestamp" in m


def test_react(env):
    cto = env["cto"]
    _, sent = cto("POST", "messages", type="stream", to="feature", topic="t", content="ok?")
    status, body = cto("POST", f"messages/{sent['id']}/reactions", emoji_name="thumbs_up",
                       emoji_code="1f44d")
    assert status == 200 and body["result"] == "success", body
    _, got = cto("GET", "messages", anchor=sent["id"], num_before=0, num_after=0)
    [r] = got["messages"][0]["reactions"]
    assert (r["emoji_name"], r["emoji_code"], r["user"]["email"]) == \
        ("thumbs_up", "1f44d", "cto-bot@chat.localhost")
    status, body = cto("POST", f"messages/{sent['id']}/reactions", emoji_name="thumbs_up",
                       emoji_code="1f44d")
    assert status == 400 and body["code"] == "REACTION_ALREADY_EXISTS"


def test_non_subscriber_refused_on_invite_only_stream(env):
    cto, coo = env["cto"], env["coo"]
    _, sent = cto("POST", "messages", type="stream", to="feature", topic="t", content="secret")
    status, body = coo("POST", "messages", type="stream", to="feature", topic="t",
                       content="let me in")
    assert status == 400 and body["result"] == "error"
    assert body["code"] == "STREAM_DOES_NOT_EXIST" and "subscri" in body["msg"]
    status, body = coo("GET", "messages", anchor="newest", num_before=10, num_after=0,
                       narrow=[{"operator": "channel", "operand": "feature"}])
    assert status == 400 and body["result"] == "error" and body["code"] == "BAD_NARROW"
    # Not by the back door either: no narrow, an edit, a reaction.
    _, body = coo("GET", "messages", anchor="newest", num_before=10, num_after=0)
    assert body["messages"] == []
    assert coo("PATCH", f"messages/{sent['id']}", topic="x")[1]["result"] == "error"
    assert coo("POST", f"messages/{sent['id']}/reactions",
               emoji_name="eyes")[1]["result"] == "error"
    # Subscribed, the same bot gets in.
    env["store"].subscribe(env["coo_user"]["id"], env["feature"])
    assert coo("POST", "messages", type="stream", to="feature", topic="t",
               content="in")[0] == 200


def test_bad_key_refused_with_401(env):
    bad = Api(env["base"], "cto-bot@chat.localhost", "not-the-key")
    status, body = bad("GET", "users/me")
    assert status == 401
    assert body["result"] == "error" and body["code"] == "INVALID_API_KEY" and body["msg"]
    req = urllib.request.Request(env["base"] + "/api/v1/users/me")
    with pytest.raises(urllib.error.HTTPError) as exc:
        OPENER.open(req, timeout=10)
    assert exc.value.code == 401


def test_mentioned_flag_for_the_reader(env):
    cto, coo = env["cto"], env["coo"]
    env["store"].subscribe(env["coo_user"]["id"], env["feature"])
    _, sent = cto("POST", "messages", type="stream", to="feature", topic="t",
                  content="@**COO** please look")
    cto("POST", "messages", type="stream", to="feature", topic="t", content="no mention")
    _, got = coo("GET", "messages", anchor="newest", num_before=10, num_after=0,
                 narrow=[{"operator": "is", "operand": "mentioned"}])
    assert [m["id"] for m in got["messages"]] == [sent["id"]]
    assert "mentioned" in got["messages"][0]["flags"]
    _, got = cto("GET", "messages", anchor=sent["id"], num_before=0, num_after=0)
    assert "mentioned" not in got["messages"][0]["flags"]  # per reader


def test_private_messages_and_users(env):
    cto, coo = env["cto"], env["coo"]
    status, sent = cto("POST", "messages", type="private",
                       to=["coo-bot@chat.localhost"], content="just us")
    assert status == 200, sent
    _, got = coo("GET", "messages", anchor="newest", num_before=10, num_after=0,
                 narrow=[{"operator": "is", "operand": "private"}])
    [m] = got["messages"]
    assert m["type"] == "private" and {u["email"] for u in m["display_recipient"]} == \
        {"cto-bot@chat.localhost", "coo-bot@chat.localhost"}
    _, me = coo("GET", "users/me")
    assert (me["email"], me["full_name"], me["is_bot"]) == ("coo-bot@chat.localhost", "COO", True)
    _, users = coo("GET", "users")
    assert {u["email"] for u in users["members"]} == {"cto-bot@chat.localhost",
                                                      "coo-bot@chat.localhost"}
    assert all("api_key" not in u for u in users["members"])


def test_anchor_newest_returns_num_before_rows(env):
    cto = env["cto"]
    ids = [cto("POST", "messages", type="stream", to="feature", topic="t",
               content=str(i))[1]["id"] for i in range(4)]
    _, got = cto("GET", "messages", anchor="newest", num_before=2, num_after=0)
    assert [m["id"] for m in got["messages"]] == ids[-2:]


def test_all_is_a_mention_for_every_reader(env):
    cto, coo = env["cto"], env["coo"]
    env["store"].subscribe(env["coo_user"]["id"], env["feature"])
    _, sent = cto("POST", "messages", type="stream", to="feature", topic="t",
                  content="@**all** standup")
    _, got = coo("GET", "messages", anchor="newest", num_before=10, num_after=0,
                 narrow=[{"operator": "is", "operand": "mentioned"}])
    assert [m["id"] for m in got["messages"]] == [sent["id"]]
    assert "mentioned" in got["messages"][0]["flags"]


def test_all_hands_wakes_every_subscribed_bot_without_tagging(env):
    """ops#242: no @all needed in 'All hands' — a human post there mentions
    every subscribed bot, a bot's post does not, and other streams are
    unchanged."""
    store, cto, coo = env["store"], env["cto"], env["coo"]
    all_hands = store.create_stream("All hands")
    cto_user = store.user_by_ref("cto-bot@chat.localhost")
    store.subscribe(cto_user["id"], all_hands)
    store.subscribe(env["coo_user"]["id"], all_hands)
    founder = store.create_user("founder@chat.localhost", "Founder")
    store.subscribe(founder["id"], all_hands)
    founder_api = Api(env["base"], founder["email"], founder["api_key"])

    _, sent = founder_api("POST", "messages", type="stream", to="All hands",
                          topic="standup", content="morning, no tags needed")
    for who in (cto, coo):
        _, got = who("GET", "messages", anchor="newest", num_before=10, num_after=0,
                     narrow=[{"operator": "is", "operand": "mentioned"}])
        assert [m["id"] for m in got["messages"]] == [sent["id"]]
        assert "mentioned" in got["messages"][0]["flags"]

    # A bot's own post in 'All hands' does not wake the other bots.
    _, bot_sent = cto("POST", "messages", type="stream", to="All hands",
                      topic="standup", content="status update")
    _, got = coo("GET", "messages", anchor="newest", num_before=10, num_after=0,
                 narrow=[{"operator": "is", "operand": "mentioned"}])
    assert bot_sent["id"] not in [m["id"] for m in got["messages"]]

    # Other streams keep the plain mention rule: no free ride from 'All hands'.
    store.subscribe(founder["id"], env["feature"])
    _, other_sent = founder_api("POST", "messages", type="stream", to="feature",
                                topic="t", content="no tag here either")
    _, got = cto("GET", "messages", anchor="newest", num_before=10, num_after=0,
                 narrow=[{"operator": "is", "operand": "mentioned"}])
    assert other_sent["id"] not in [m["id"] for m in got["messages"]]


def test_message_over_10000_characters_is_refused(env):
    cto = env["cto"]
    status, body = cto("POST", "messages", type="stream", to="feature", topic="t",
                       content="x" * 10_001)
    assert status == 400 and body["result"] == "error"
    assert body["msg"].startswith("Message too long")
    assert cto("POST", "messages", type="stream", to="feature", topic="t",
               content="x" * 10_000)[0] == 200
    _, sent = cto("POST", "messages", type="stream", to="feature", topic="t", content="ok")
    status, body = cto("PATCH", f"messages/{sent['id']}", content="y" * 10_001)
    assert status == 400 and body["msg"].startswith("Message too long")


# -- issue #168: unread messages


def test_168_own_messages_are_never_unread(env):
    cto = env["cto"]
    cto("POST", "messages", type="stream", to="feature", topic="t", content="one")
    cto("POST", "messages", type="stream", to="feature", topic="t", content="two")
    status, unread = cto("GET", "unread")
    assert status == 200 and unread["streams"] == []


def test_168_unread_counts_per_stream_and_topic(env):
    cto, coo = env["cto"], env["coo"]
    env["store"].subscribe(env["coo_user"]["id"], env["feature"])
    cto("POST", "messages", type="stream", to="feature", topic="t1", content="a")
    cto("POST", "messages", type="stream", to="feature", topic="t1", content="b")
    cto("POST", "messages", type="stream", to="feature", topic="t2", content="c")
    status, unread = coo("GET", "unread")
    assert status == 200
    [feature] = unread["streams"]
    assert feature["name"] == "feature" and feature["unread"] == 3
    assert {t["name"]: t["unread"] for t in feature["topics"]} == {"t1": 2, "t2": 1}
    # CTO sent every message: nothing is unread for CTO.
    assert cto("GET", "unread")[1]["streams"] == []


def test_168_mark_topic_as_read_clears_only_that_topic_and_only_others_messages(env):
    cto, coo = env["cto"], env["coo"]
    env["store"].subscribe(env["coo_user"]["id"], env["feature"])
    cto("POST", "messages", type="stream", to="feature", topic="t1", content="a")
    coo("POST", "messages", type="stream", to="feature", topic="t1", content="mine")
    cto("POST", "messages", type="stream", to="feature", topic="t2", content="b")
    status, body = coo("POST", "mark_topic_as_read", stream_id=env["feature"], topic_name="t1")
    assert status == 200 and body == {"result": "success", "msg": ""}
    status, unread = coo("GET", "unread")
    [feature] = unread["streams"]
    assert feature["unread"] == 1 and [t["name"] for t in feature["topics"]] == ["t2"]
    # Idempotent: marking an already-read topic again changes nothing.
    status, body = coo("POST", "mark_topic_as_read", stream_id=env["feature"], topic_name="t1")
    assert status == 200
    assert coo("GET", "unread")[1]["streams"][0]["unread"] == 1


def test_168_mark_topic_as_read_is_case_insensitive_on_the_topic_name(env):
    cto, coo = env["cto"], env["coo"]
    env["store"].subscribe(env["coo_user"]["id"], env["feature"])
    cto("POST", "messages", type="stream", to="feature", topic="Roadmap", content="a")
    status, body = coo("POST", "mark_topic_as_read", stream_id=env["feature"], topic_name="roadmap")
    assert status == 200, body
    assert coo("GET", "unread")[1]["streams"] == []


def test_168_mark_topic_as_read_needs_a_topic_and_a_visible_stream(env):
    coo = env["coo"]
    status, body = coo("POST", "mark_topic_as_read", stream_id=env["feature"])
    assert status == 400 and body["code"] == "REQUEST_VARIABLE_MISSING"
    status, body = coo("POST", "mark_topic_as_read", stream_id=999999, topic_name="t")
    assert status == 400 and body["code"] == "STREAM_DOES_NOT_EXIST"


def test_168_the_read_flag_follows_mark_topic_as_read(env):
    cto, coo = env["cto"], env["coo"]
    env["store"].subscribe(env["coo_user"]["id"], env["feature"])
    _, sent = cto("POST", "messages", type="stream", to="feature", topic="t", content="hi")
    _, got = coo("GET", "messages", anchor=sent["id"], num_before=0, num_after=0)
    assert "read" not in got["messages"][0]["flags"]
    coo("POST", "mark_topic_as_read", stream_id=env["feature"], topic_name="t")
    _, got = coo("GET", "messages", anchor=sent["id"], num_before=0, num_after=0)
    assert "read" in got["messages"][0]["flags"]
    # The sender's own message is always "read", mark-read or not.
    _, got = cto("GET", "messages", anchor=sent["id"], num_before=0, num_after=0)
    assert "read" in got["messages"][0]["flags"]


def test_malformed_content_length_answers_in_zulips_shape(env):
    port = int(env["base"].rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(b"POST /api/v1/messages HTTP/1.1\r\nHost: x\r\n"
                  b"Content-Length: banana\r\n\r\n")
        data = b""
        while b"\r\n\r\n" not in data or not data.rstrip().endswith(b"}"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400")
    got = json.loads(body)
    assert got["result"] == "error" and "Content-Length" in got["msg"] and got["code"]


def test_a_write_is_one_transaction_with_its_events(env, monkeypatch):
    """A failure after the message row is written leaves no message behind:
    the message, a DM's recipients and the queue events commit together."""
    cto, store = env["cto"], env["store"]
    before = store.one("SELECT count(*) FROM messages")[0]

    def boom(*a, **k):
        raise RuntimeError("crash between message and events")
    monkeypatch.setattr(chat.Chat, "_publish", boom)
    status, body = cto("POST", "messages", type="private",
                       to=["coo-bot@chat.localhost"], content="lost?")
    assert status == 500 and body["result"] == "error"
    assert store.one("SELECT count(*) FROM messages")[0] == before
    assert store.one("SELECT count(*) FROM recipients")[0] == 0
    assert not store.db.in_transaction


def test_seed_ids_continue_above_zulips(tmp_path):
    store = chat.Store(str(tmp_path / "c.sqlite3"))
    store.seed_ids(600)
    store.seed_ids(10)  # never lowers
    cto = store.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    sid = store.create_stream("cto")
    store.subscribe(cto["id"], sid)
    out = chat.Chat(store).send(cto, {"type": "stream", "to": "cto", "topic": "t",
                                      "content": "first"})
    assert out["id"] == 601 and store.max_message_id() == 601
    store.db.close()


def test_every_import_is_standard_library():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names and not names - set(sys.stdlib_module_names), names


def test_binds_loopback_only():
    assert chat.HOST == "127.0.0.1"
    files = [p for p in (ROOT / "chat").rglob("*") if p.is_file() and "data" not in p.parts
             and p.suffix in (".py", ".md", ".cmd")]
    assert ROOT / "chat" / "server.py" in files
    for path in files:
        assert "0.0.0.0" not in path.read_text(encoding="utf-8"), path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_bot_zulip_cli_send_and_read_against_it(tmp_path):
    """The agent path, end to end, with bot/zulip.py exactly as it is."""
    db, port = str(tmp_path / "chat.sqlite3"), _free_port()
    proc = subprocess.Popen([sys.executable, str(SERVER), "serve", "--port", str(port),
                             "--db", db], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline, "server never answered"
                time.sleep(0.1)

        out = subprocess.run([sys.executable, str(SERVER), "bootstrap", "--json", "--db", db,
                              "--port", str(port)], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        creds = json.loads(out.stdout)
        assert creds["site"] == f"http://127.0.0.1:{port}"
        # Seeding streams is M1 issue 3's endpoint; here it goes straight to the store.
        store = chat.Store(db)
        admin = store.one("SELECT id FROM users WHERE email=?", (creds["email"],))["id"]
        store.subscribe(admin, store.create_stream("coo", invite_only=True))
        store.db.close()

        # Only the test server and the temp credentials: no inherited ZULIP_*,
        # and every variable bot/zulip.py reads is set, so bot/.env fills nothing.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ZULIP_")}
        env.update(ZULIP_SITE=creds["site"], ZULIP_BOT_EMAIL=creds["email"],
                   ZULIP_BOT_API_KEY=creds["api_key"], NO_PROXY="127.0.0.1,localhost",
                   PYTHONIOENCODING="utf-8")

        def cli(*args):
            return subprocess.run([sys.executable, str(ZULIP_CLI), *args], env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  timeout=60)

        sent = cli("send", "--stream", "coo", "--topic", "#61 smoke",
                   "--text", "hello from our own chat")
        assert sent.returncode == 0, sent.stderr
        assert "sent #coo" in sent.stdout
        read = cli("read", "--stream", "coo", "--topic", "#61 smoke")
        assert read.returncode == 0, read.stderr
        assert "hello from our own chat" in read.stdout
        assert creds["email"] in read.stdout
        refused = cli("send", "--stream", "nowhere", "--topic", "t", "--text", "x")
        assert refused.returncode == 2 and "Nothing was sent" in refused.stderr
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
