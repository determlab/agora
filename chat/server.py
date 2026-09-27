"""Our own chat server, M1 part 1: the message half of Zulip's REST API.

A stand-in for Zulip that speaks the same wire format under ``/api/v1/``, so
``bot/zulip.py``, ``bot/zulip_client.py`` and ``hooks/agora_hook.py`` work
against it with nothing changed but ``ZULIP_SITE`` (issue #61). Field names,
the HTTP Basic ``email:api_key`` auth and the ``{"result":"error","msg",
"code"}`` error shape are Zulip's, because those tools read them.

Usage:
  python chat/server.py serve --port 8095 [--db PATH]
  python chat/server.py bootstrap --json [--db PATH] [--port 8095]

``bootstrap`` creates one admin *bot* and prints its credentials. It never
creates a human and never sets a password.

Stdlib only (D2's shape), one SQLite file in WAL mode, bound to 127.0.0.1
only (D4). Not here yet: the event queue (``/register``, ``/events``), stream
and bot setup endpoints, the browser page, history import.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import sqlite3
import string
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chat.sqlite3")
REALM = "chat.localhost"
ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER = 100, 200, 400  # Zulip's role numbers
NEWEST = 1 << 62

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
    is_bot INTEGER NOT NULL, api_key TEXT NOT NULL, role INTEGER NOT NULL,
    date_joined INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS streams (
    stream_id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL,
    invite_only INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER NOT NULL, stream_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, stream_id));
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL,
    type TEXT NOT NULL, stream_id INTEGER, subject TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL, timestamp INTEGER NOT NULL, last_edit_timestamp INTEGER);
CREATE INDEX IF NOT EXISTS messages_stream ON messages (stream_id, id);
CREATE TABLE IF NOT EXISTS recipients (
    message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id));
CREATE TABLE IF NOT EXISTS reactions (
    message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, emoji_name TEXT NOT NULL,
    emoji_code TEXT NOT NULL, reaction_type TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji_code, reaction_type));
"""


class ApiError(Exception):
    """Becomes Zulip's error body. ``msg`` should say what to do about it."""

    def __init__(self, msg: str, code: str = "BAD_REQUEST", status: int = 400):
        super().__init__(msg)
        self.msg, self.code, self.status = msg, code, status


def _no_stream(name) -> ApiError:
    # Same answer for "absent" and "private and you are not in it": Zulip does
    # not reveal a private stream's existence to a non-subscriber, nor do we.
    return ApiError(f"unknown stream '{name}', or it is invite_only and you are not "
                    f"subscribed: create it, or be subscribed to it, with "
                    f"POST /users/me/subscriptions", "STREAM_DOES_NOT_EXIST")


class Store:
    """All state. One connection behind a lock: SQLite serialises writes
    anyway, and this keeps ThreadingHTTPServer's threads off each other."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    # -- seeding: used by bootstrap and tests; the HTTP setup endpoints are M1 issue 3

    def create_user(self, email: str, full_name: str, is_bot: bool = False,
                    role: int = ROLE_MEMBER) -> dict:
        key = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        uid = self.x("INSERT INTO users (email, full_name, is_bot, api_key, role, date_joined)"
                     " VALUES (?,?,?,?,?,?)",
                     (email, full_name, int(is_bot), key, role, int(time.time())))
        return self.user(uid)

    def create_stream(self, name: str, invite_only: bool = True) -> int:
        return self.x("INSERT INTO streams (name, invite_only) VALUES (?,?)",
                      (name, int(invite_only)))

    def subscribe(self, user_id: int, stream_id: int) -> None:
        self.x("INSERT OR IGNORE INTO subscriptions VALUES (?,?)", (user_id, stream_id))

    def user(self, uid: int) -> dict:
        return dict(self.one("SELECT * FROM users WHERE id=?", (uid,)))

    # -- lookups

    def authenticate(self, header: str | None) -> dict:
        if not header or not header.startswith("Basic "):
            raise ApiError("Missing 'Authorization' header: send HTTP Basic email:api_key",
                           "UNAUTHORIZED", 401)
        try:
            email, _, key = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            raise ApiError("Malformed Authorization header", "UNAUTHORIZED", 401)
        row = self.one("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,))
        if row is None or not secrets.compare_digest(row["api_key"], key):
            raise ApiError(f"Invalid API key for '{email}': use the key bootstrap or "
                           f"the bot's zuliprc gave you", "INVALID_API_KEY", 401)
        return dict(row)

    def subscribed(self, uid: int) -> set[int]:
        return {r[0] for r in self.q("SELECT stream_id FROM subscriptions WHERE user_id=?",
                                     (uid,))}

    def visible_stream(self, me: dict, ref) -> sqlite3.Row:
        """A stream by name or id that ``me`` may read and post to."""
        ref = str(ref).lstrip("#")
        row = self.one("SELECT * FROM streams WHERE name=? OR CAST(stream_id AS TEXT)=?",
                       (ref, ref))
        if row is None or (row["invite_only"] and row["stream_id"] not in self.subscribed(me["id"])):
            raise _no_stream(ref)
        return row

    def user_by_ref(self, ref) -> dict:
        row = self.one("SELECT * FROM users WHERE email=? COLLATE NOCASE "
                       "OR CAST(id AS TEXT)=?", (str(ref), str(ref)))
        if row is None:
            raise ApiError(f"Invalid user '{ref}': no such email or user id", "BAD_REQUEST")
        return dict(row)


def user_json(u: dict) -> dict:
    return {"user_id": u["id"], "email": u["email"], "delivery_email": u["email"],
            "full_name": u["full_name"], "is_bot": bool(u["is_bot"]),
            "bot_type": 1 if u["is_bot"] else None, "role": u["role"],
            "is_owner": u["role"] == ROLE_OWNER, "is_admin": u["role"] <= ROLE_ADMIN,
            "is_guest": u["role"] >= 600, "is_active": True, "avatar_url": None,
            "timezone": "", "date_joined": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(u["date_joined"]))}


def _mentions(content: str, u: dict) -> bool:
    name = u["full_name"]
    return f"@**{name}**" in content or f"@**{name}|{u['id']}**" in content


def _bool(v, default: bool) -> bool:
    if v is None:
        return default
    return str(v).lower() in ("true", "1")


def _int(params: dict, key: str, default: int | None = None) -> int:
    v = params.get(key)
    if v is None:
        if default is None:
            raise ApiError(f"Missing '{key}' argument", "REQUEST_VARIABLE_MISSING")
        return default
    try:
        return int(v)
    except ValueError:
        raise ApiError(f"'{key}' is not an integer: {v!r}", "BAD_REQUEST")


def _json_list(v) -> list:
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
    except (TypeError, ValueError):
        # Zulip also takes "a@x,b@x" or a bare name for `to`.
        return [s.strip() for s in str(v).split(",") if s.strip()]
    return out if isinstance(out, list) else [out]


class Chat:
    """The endpoints. Each returns the success body (without ``result``)."""

    def __init__(self, store: Store):
        self.s = store

    def message_json(self, row, me: dict) -> dict:
        s = self.s
        sender = s.user(row["sender_id"])
        m = {"id": row["id"], "sender_id": sender["id"], "sender_email": sender["email"],
             "sender_full_name": sender["full_name"], "type": row["type"],
             "subject": row["subject"], "content": row["content"],
             "content_type": "text/x-markdown", "timestamp": row["timestamp"],
             "client": "chat", "is_me_message": False, "avatar_url": None,
             "sender_realm_str": REALM, "topic_links": [], "submessages": []}
        if row["last_edit_timestamp"]:
            m["last_edit_timestamp"] = row["last_edit_timestamp"]
        if row["type"] == "stream":
            m["stream_id"] = row["stream_id"]
            m["display_recipient"] = s.one("SELECT name FROM streams WHERE stream_id=?",
                                           (row["stream_id"],))[0]
        else:
            ids = [r[0] for r in s.q("SELECT user_id FROM recipients WHERE message_id=? "
                                     "ORDER BY user_id", (row["id"],))]
            m["display_recipient"] = [{"id": u["id"], "email": u["email"],
                                       "full_name": u["full_name"], "is_mirror_dummy": False}
                                      for u in map(s.user, ids)]
        m["reactions"] = [
            {"emoji_name": r["emoji_name"], "emoji_code": r["emoji_code"],
             "reaction_type": r["reaction_type"], "user_id": r["user_id"],
             "user": {"id": r["user_id"], "email": r["email"],
                      "full_name": r["full_name"], "is_mirror_dummy": False}}
            for r in s.q("SELECT r.*, u.email, u.full_name FROM reactions r JOIN users u "
                         "ON u.id=r.user_id WHERE message_id=? ORDER BY r.rowid", (row["id"],))]
        flags = []
        if row["sender_id"] == me["id"]:
            flags.append("read")
        if _mentions(row["content"], me):
            flags.append("mentioned")
        m["flags"] = flags
        return m

    def _visible_message(self, me: dict, mid: int):
        row = self.s.one("SELECT * FROM messages WHERE id=?", (mid,))
        if row is not None:
            if row["type"] == "stream":
                try:
                    self.s.visible_stream(me, row["stream_id"])
                    return row
                except ApiError:
                    pass
            elif row["sender_id"] == me["id"] or self.s.one(
                    "SELECT 1 FROM recipients WHERE message_id=? AND user_id=?", (mid, me["id"])):
                return row
        raise ApiError(f"Invalid message(s): no message {mid} that you can see",
                       "BAD_REQUEST")

    # POST /messages
    def send(self, me: dict, p: dict) -> dict:
        kind = p.get("type", "stream")
        content = p.get("content")
        if not content or not content.strip():
            raise ApiError("Message must not be empty: send a non-empty 'content'")
        now = int(time.time())
        to = p.get("to")
        if to is None:
            raise ApiError("Missing 'to' argument: a stream name or id, or a JSON list "
                           "of emails for a direct message", "REQUEST_VARIABLE_MISSING")
        if kind in ("stream", "channel"):
            refs = _json_list(to)
            if len(refs) != 1:
                raise ApiError("'to' must name exactly one stream")
            stream = self.s.visible_stream(me, refs[0])
            topic = p.get("topic", p.get("subject"))
            if topic is None:
                raise ApiError("Missing topic: send 'topic'", "REQUEST_VARIABLE_MISSING")
            mid = self.s.x("INSERT INTO messages (sender_id, type, stream_id, subject, content,"
                           " timestamp) VALUES (?,?,?,?,?,?)",
                           (me["id"], "stream", stream["stream_id"], topic, content, now))
        elif kind in ("private", "direct"):
            users = [self.s.user_by_ref(r) for r in _json_list(to)]
            if not users:
                raise ApiError("'to' names no recipients")
            with self.s.lock:
                mid = self.s.x("INSERT INTO messages (sender_id, type, subject, content,"
                               " timestamp) VALUES (?,?,'',?,?)",
                               (me["id"], "private", content, now))
                for uid in {me["id"], *(u["id"] for u in users)}:
                    self.s.x("INSERT INTO recipients VALUES (?,?)", (mid, uid))
        else:
            raise ApiError(f"Invalid message type '{kind}': use 'stream' or 'private'")
        return {"id": mid}

    # GET /messages
    def fetch(self, me: dict, p: dict) -> dict:
        narrow = _json_list(p.get("narrow", "[]"))
        subs = self.s.subscribed(me["id"])
        where, args, channel = [], [], None
        for term in narrow:
            if isinstance(term, (list, tuple)) and len(term) >= 2:
                op, operand, neg = term[0], term[1], False
            elif isinstance(term, dict):
                op, operand, neg = term.get("operator"), term.get("operand"), term.get("negated")
            else:
                raise ApiError(f"Invalid narrow term {term!r}", "BAD_NARROW")
            if op in ("channel", "stream"):
                try:
                    channel = self.s.visible_stream(me, operand)
                except ApiError as e:
                    raise ApiError(e.msg, "BAD_NARROW")
                clause, a = "m.stream_id=?", [channel["stream_id"]]
            elif op in ("topic", "subject"):
                clause, a = "m.subject=? COLLATE NOCASE", [operand]
            elif op == "sender":
                clause, a = "m.sender_id=?", [self.s.user_by_ref(operand)["id"]]
            elif op == "is" and operand == "mentioned":
                clause = "(instr(m.content, ?) > 0 OR instr(m.content, ?) > 0)"
                a = [f"@**{me['full_name']}**", f"@**{me['full_name']}|{me['id']}**"]
            elif op == "is" and operand in ("private", "dm"):
                clause, a = "m.type='private'", []
            else:
                raise ApiError(f"Invalid narrow operator '{op}:{operand}': M1 knows channel, "
                               f"stream, topic, sender, is:mentioned, is:private", "BAD_NARROW")
            where.append(f"NOT ({clause})" if neg else clause)
            args += a
        # What `me` can read: its streams (plus a public one it narrowed to,
        # as Zulip allows) and the direct messages it is part of.
        readable = set(subs) | ({channel["stream_id"]} if channel else set())
        marks = ",".join("?" * len(readable)) or "NULL"
        where.append(f"((m.type='stream' AND m.stream_id IN ({marks})) OR (m.type='private' "
                     f"AND EXISTS (SELECT 1 FROM recipients r WHERE r.message_id=m.id "
                     f"AND r.user_id=?)))")
        args += [*readable, me["id"]]
        cond = " AND ".join(where)

        anchor_raw = p.get("anchor", "newest")
        num_before, num_after = _int(p, "num_before", 0), _int(p, "num_after", 0)
        include = _bool(p.get("include_anchor"), True)
        if anchor_raw in ("newest", "first_unread"):
            anchor = NEWEST
        elif anchor_raw == "oldest":
            anchor = 0
        else:
            anchor = _int(p, "anchor")
        sql = f"SELECT m.* FROM messages m WHERE {cond} AND m.id {{}} ? ORDER BY m.id {{}} LIMIT ?"
        # The anchor message counts on the "after" side, as in Zulip; with
        # anchor=newest there is no after side, so it rides on "before".
        before_op, after_op = "<", ">=" if include else ">"
        before_n, after_n = num_before, num_after + (1 if include else 0)
        if anchor == NEWEST:
            before_op, before_n, after_n = ("<=" if include else "<"), num_before + include, 0
        before = self.s.q(sql.format(before_op, "DESC"), [*args, anchor, before_n])
        after = self.s.q(sql.format(after_op, "ASC"), [*args, anchor, after_n]) if after_n else []
        rows = list(reversed(before)) + list(after)
        found_newest = anchor == NEWEST or len(after) < after_n
        return {"messages": [self.message_json(r, me) for r in rows],
                "anchor": anchor if anchor != NEWEST else (rows[-1]["id"] if rows else 0),
                "found_anchor": any(r["id"] == anchor for r in rows),
                "found_newest": found_newest, "found_oldest": anchor == 0 or len(before) < before_n,
                "history_limited": False}

    # PATCH /messages/{id}
    def edit(self, me: dict, mid: int, p: dict) -> dict:
        row = self._visible_message(me, mid)
        content, topic = p.get("content"), p.get("topic", p.get("subject"))
        if content is None and topic is None:
            raise ApiError("Nothing to change: send 'content' and/or 'topic'")
        if content is not None and row["sender_id"] != me["id"]:
            raise ApiError("You don't have permission to edit this message: only its "
                           "sender can edit its content", "BAD_REQUEST")
        if topic is not None:
            if row["type"] != "stream":
                raise ApiError("A direct message has no topic to edit")
            if row["sender_id"] != me["id"] and me["role"] > ROLE_ADMIN:
                raise ApiError("You don't have permission to edit this message's topic: "
                               "only its sender or an admin can")
        if content is not None and not content.strip():
            raise ApiError("Message must not be empty: to delete it, use DELETE")
        self.s.x("UPDATE messages SET content=coalesce(?, content), subject=coalesce(?, subject),"
                 " last_edit_timestamp=? WHERE id=?", (content, topic, int(time.time()), mid))
        return {"detached_uploads": []}

    # POST /messages/{id}/reactions
    def react(self, me: dict, mid: int, p: dict) -> dict:
        self._visible_message(me, mid)
        name = p.get("emoji_name")
        if not name:
            raise ApiError("Missing 'emoji_name' argument", "REQUEST_VARIABLE_MISSING")
        code, rtype = p.get("emoji_code") or name, p.get("reaction_type") or "unicode_emoji"
        try:
            self.s.x("INSERT INTO reactions VALUES (?,?,?,?,?)", (mid, me["id"], name, code, rtype))
        except sqlite3.IntegrityError:
            raise ApiError("Reaction already exists.", "REACTION_ALREADY_EXISTS")
        return {}

    def subscriptions(self, me: dict) -> dict:
        return {"subscriptions": [
            {"stream_id": r["stream_id"], "name": r["name"], "invite_only": bool(r["invite_only"]),
             "description": r["description"]}
            for r in self.s.q("SELECT s.* FROM streams s JOIN subscriptions x ON "
                              "x.stream_id=s.stream_id WHERE x.user_id=? ORDER BY s.name",
                              (me["id"],))]}

    def route(self, method: str, path: str, me: dict, p: dict) -> dict:
        m = re.fullmatch(r"messages/(\d+)(/reactions)?", path)
        if path == "messages" and method == "POST":
            return self.send(me, p)
        if path == "messages" and method == "GET":
            return self.fetch(me, p)
        if m and not m.group(2) and method == "PATCH":
            return self.edit(me, int(m.group(1)), p)
        if m and m.group(2) and method == "POST":
            return self.react(me, int(m.group(1)), p)
        if path == "users/me" and method == "GET":
            return user_json(me)
        if path == "users" and method == "GET":
            return {"members": [user_json(dict(r)) for r in self.s.q("SELECT * FROM users "
                                                                        "ORDER BY id")]}
        # Read-only lookups bot/zulip.py makes before every send and read.
        if path == "users/me/subscriptions" and method == "GET":
            return self.subscriptions(me)
        if path == "get_stream_id" and method == "GET":
            return {"stream_id": self.s.visible_stream(me, p.get("stream", ""))["stream_id"]}
        raise ApiError(f"Endpoint not found: {method} /api/v1/{path} is not served by this "
                       f"chat server (M1 serves messages, reactions and users)",
                       "BAD_REQUEST", 404)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgoraChat/0.1"
    chat: Chat  # set by make_server

    def log_message(self, *a):  # quiet: tests and agents read stdout
        pass

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="zulip"')
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        params = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length).decode("utf-8", "replace")
            params.update(urllib.parse.parse_qsl(body, keep_blank_values=True))
        try:
            if not url.path.startswith("/api/v1/"):
                raise ApiError(f"Endpoint not found: {url.path} (the API is under /api/v1/)",
                               "BAD_REQUEST", 404)
            me = self.chat.s.authenticate(self.headers.get("Authorization"))
            out = self.chat.route(method, url.path[len("/api/v1/"):].strip("/"), me, params)
            self._reply(200, {"result": "success", "msg": "", **out})
        except ApiError as e:
            self._reply(e.status, {"result": "error", "msg": e.msg, "code": e.code})
        except Exception as e:  # a crash still answers in Zulip's shape, never a dropped socket
            self._reply(500, {"result": "error", "msg": f"server error: {e!r}",
                              "code": "INTERNAL_SERVER_ERROR"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


def make_server(db: str, port: int = 8095) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1 only; ``port=0`` picks a free one."""
    handler = type("ChatHandler", (Handler,), {"chat": Chat(Store(db))})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    return server


def bootstrap(store: Store) -> dict:
    """The admin bot, created once; a second run hands back the same one."""
    email = f"admin-bot@{REALM}"
    row = store.one("SELECT * FROM users WHERE email=?", (email,))
    user = dict(row) if row else store.create_user(email, "Admin", is_bot=True, role=ROLE_ADMIN)
    return {"email": user["email"], "api_key": user["api_key"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the API on 127.0.0.1")
    boot = sub.add_parser("bootstrap", help="create the admin bot, print its credentials")
    boot.add_argument("--json", action="store_true", help="print JSON (the agent path)")
    for p in (serve, boot):
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--port", type=int, default=8095)
    args = parser.parse_args(argv)

    if args.cmd == "bootstrap":
        creds = {**bootstrap(Store(args.db)), "site": f"http://{HOST}:{args.port}"}
        if args.json:
            print(json.dumps(creds))
        else:
            print(f"ZULIP_SITE={creds['site']}\nZULIP_BOT_EMAIL={creds['email']}\n"
                  f"ZULIP_BOT_API_KEY={creds['api_key']}")
        return 0
    server = make_server(args.db, args.port)
    print(f"serving http://{HOST}:{server.server_address[1]}/api/v1/ db={args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
