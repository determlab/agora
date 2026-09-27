"""Our own chat server (M1, issue #61): the part of Zulip's REST API we use.

A stand-in for Zulip that speaks the same wire format under ``/api/v1/``, so
``bot/zulip.py``, ``bot/zulip_client.py``, ``bot/setup_streams.py`` and
``hooks/agora_hook.py`` work against it with nothing changed but
``ZULIP_SITE``. Field names, the HTTP Basic ``email:api_key`` auth and the
``{"result":"error","msg","code"}`` error shape are Zulip's, because those
tools read them.

Usage:
  python chat/server.py serve --port 8095 [--db PATH] [--seed-ids N]
  python chat/server.py bootstrap --json [--from-env bot/.env] [--db PATH]
  python chat/server.py add-human --email E --name N --json [--db PATH]

``bootstrap`` creates one admin *bot* and prints its credentials; with
``--from-env`` it also creates each ``ZULIP_<ROLE>_EMAIL`` / ``_API_KEY`` pair
in that file as a bot with that very email and key, so switching a tool over
is one line (``ZULIP_SITE``). ``add-human`` is for the founder to run himself:
no agent creates his account. No password anywhere.

The event queue (D12) lives in SQLite, like everything else: a queue and its
events survive a restart and expire only after 7 days without a poll, where
Zulip's expire after minutes of silence (where the first attempt broke).

Stdlib only (D2's shape), one SQLite file in WAL mode, bound to 127.0.0.1
only (D4). Not here: the browser page, history import, search.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import secrets
import sqlite3
import string
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chat.sqlite3")
REALM = "chat.localhost"
ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER = 100, 200, 400  # Zulip's role numbers
NEWEST = 1 << 62
MAX_CONTENT = 10_000  # Zulip's own limit, in characters
POLL_SECONDS = 50.0  # an idle long poll answers with a heartbeat after this
QUEUE_TTL = 7 * 24 * 3600  # a queue nobody polled for this long is gone
# `@**all**` and its aliases notify everyone in the stream: for the hook's
# wake rule that is a mention like any other.
WILDCARDS = ("@**all**", "@**everyone**", "@**channel**", "@**stream**")

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
CREATE TABLE IF NOT EXISTS queues (
    queue_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, event_types TEXT,
    narrow TEXT NOT NULL, created INTEGER NOT NULL, last_poll INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_queue ON events (queue_id, id);
"""


class ApiError(Exception):
    """Becomes Zulip's error body. ``msg`` should say what to do about it."""

    def __init__(self, msg: str, code: str = "BAD_REQUEST", status: int = 400, **extra):
        super().__init__(msg)
        self.msg, self.code, self.status, self.extra = msg, code, status, extra


def _no_stream(name) -> ApiError:
    # Same answer for "absent" and "private and you are not in it": Zulip does
    # not reveal a private stream's existence to a non-subscriber, nor do we.
    return ApiError(f"unknown stream '{name}', or it is invite_only and you are not "
                    f"subscribed: create it, or be subscribed to it, with "
                    f"POST /users/me/subscriptions", "STREAM_DOES_NOT_EXIST")


def _new_key() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))


class Store:
    """All state. One connection behind a lock: SQLite serialises writes
    anyway, and this keeps ThreadingHTTPServer's threads off each other.

    Every write goes through ``tx()``, one ``BEGIN IMMEDIATE … COMMIT``: a
    message, its recipients and the events it puts in each queue land
    together or not at all. ``changed`` wakes the long polls after a commit."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            if self.db.in_transaction:  # nested: the outer one commits
                yield
                return
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
            self.changed.notify_all()

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.tx():
            return self.db.execute(sql, args).lastrowid

    def seed_ids(self, floor: int) -> None:
        """Message and event ids continue above ``floor`` (Zulip's last id):
        the hook's ``last_message_id`` and ``zulip.py read --since`` carry
        Zulip-era ids, and a counter from 1 would sit below them unseen.
        Only ever raises the counter."""
        with self.tx():
            for table in ("messages", "events"):
                row = self.one("SELECT seq FROM sqlite_sequence WHERE name=?", (table,))
                if row is None:
                    self.x("INSERT INTO sqlite_sequence (name, seq) VALUES (?,?)",
                           (table, floor))
                elif row[0] < floor:
                    self.x("UPDATE sqlite_sequence SET seq=? WHERE name=?", (floor, table))

    def max_message_id(self) -> int:
        row = self.one("SELECT seq FROM sqlite_sequence WHERE name='messages'")
        return int(row[0]) if row else -1

    # -- accounts and streams: bootstrap, add-human and the setup endpoints

    def create_user(self, email: str, full_name: str, is_bot: bool = False,
                    role: int = ROLE_MEMBER, api_key: str | None = None) -> dict:
        uid = self.x("INSERT INTO users (email, full_name, is_bot, api_key, role, date_joined)"
                     " VALUES (?,?,?,?,?,?)",
                     (email, full_name, int(is_bot), api_key or _new_key(), role,
                      int(time.time())))
        return self.user(uid)

    def create_stream(self, name: str, invite_only: bool = True) -> int:
        return self.x("INSERT INTO streams (name, invite_only) VALUES (?,?)",
                      (name, int(invite_only)))

    def subscribe(self, user_id: int, stream_id: int) -> bool:
        with self.tx():
            return self.db.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?)",
                                   (user_id, stream_id)).rowcount > 0

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

    def stream_by_ref(self, ref):
        ref = str(ref).lstrip("#")
        return self.one("SELECT * FROM streams WHERE name=? OR CAST(stream_id AS TEXT)=?",
                        (ref, ref))

    def visible_stream(self, me: dict, ref) -> sqlite3.Row:
        """A stream by name or id that ``me`` may read and post to."""
        row = self.stream_by_ref(ref)
        if row is None or (row["invite_only"] and row["stream_id"] not in self.subscribed(me["id"])):
            raise _no_stream(str(ref).lstrip("#"))
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


def stream_json(r) -> dict:
    return {"stream_id": r["stream_id"], "name": r["name"],
            "invite_only": bool(r["invite_only"]), "description": r["description"],
            "rendered_description": r["description"], "is_web_public": False,
            "history_public_to_subscribers": True}


def _mention_marks(u: dict) -> list[str]:
    return [f"@**{u['full_name']}**", f"@**{u['full_name']}|{u['id']}**", *WILDCARDS]


def _mentions(content: str, u: dict) -> bool:
    return any(mark in content for mark in _mention_marks(u))


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


def _check_content(content) -> None:
    if not content or not content.strip():
        raise ApiError("Message must not be empty: send a non-empty 'content'")
    if len(content) > MAX_CONTENT:
        raise ApiError(f"Message too long: {len(content)} characters, the limit is "
                       f"{MAX_CONTENT}; split it into several messages")


def _terms(narrow: list) -> list[tuple]:
    """(operator, operand, negated) for each narrow term, either shape."""
    out = []
    for term in narrow:
        if isinstance(term, (list, tuple)) and len(term) >= 2:
            out.append((term[0], term[1], False))
        elif isinstance(term, dict):
            out.append((term.get("operator"), term.get("operand"), bool(term.get("negated"))))
        else:
            raise ApiError(f"Invalid narrow term {term!r}", "BAD_NARROW")
    return out


NARROW_OPS = "channel, stream, topic, sender, is:mentioned, is:private, is:dm"


def _known_op(op, operand) -> bool:
    return op in ("channel", "stream", "topic", "subject", "sender") or (
        op == "is" and operand in ("mentioned", "private", "dm"))


class Chat:
    """The endpoints. Each returns the success body (without ``result``)."""

    def __init__(self, store: Store, poll_seconds: float = POLL_SECONDS):
        self.s = store
        self.poll_seconds = poll_seconds

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
        m["flags"] = self.flags(row, me)
        return m

    @staticmethod
    def flags(row, me: dict) -> list[str]:
        flags = []
        if row["sender_id"] == me["id"]:
            flags.append("read")
        if _mentions(row["content"], me):
            flags.append("mentioned")
        return flags

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

    # -- the event queue (D12)

    def _narrow_matches(self, narrow: list, row, user: dict) -> bool:
        for op, operand, neg in _terms(narrow):
            if op in ("channel", "stream"):
                ok = row["type"] == "stream" and self.s.one(
                    "SELECT 1 FROM streams WHERE stream_id=? AND (name=? OR "
                    "CAST(stream_id AS TEXT)=?)",
                    (row["stream_id"], str(operand).lstrip("#"), str(operand))) is not None
            elif op in ("topic", "subject"):
                ok = row["subject"].lower() == str(operand).lower()
            elif op == "sender":
                sender = self.s.user(row["sender_id"])
                ok = str(operand).lower() in (sender["email"].lower(), str(sender["id"]))
            elif op == "is" and operand == "mentioned":
                ok = _mentions(row["content"], user)
            elif op == "is" and operand in ("private", "dm"):
                ok = row["type"] == "private"
            else:
                ok = False
            if ok == neg:
                return False
        return True

    def _publish(self, etype: str, row, build) -> None:
        """Write one event into every live queue that may see message ``row``
        and asked for ``etype``. Called inside the writer's transaction, so
        the change and its events commit together. Visibility is membership
        now: a stream's subscribers, a direct message's recipients — never a
        non-subscriber of an invite_only stream."""
        if row["type"] == "stream":
            who = "SELECT user_id FROM subscriptions WHERE stream_id=?"
            arg = row["stream_id"]
        else:
            who = "SELECT user_id FROM recipients WHERE message_id=?"
            arg = row["id"]
        users: dict[int, dict] = {}
        for q in self.s.q(f"SELECT * FROM queues WHERE user_id IN ({who}) ORDER BY created",
                          (arg,)):
            types = json.loads(q["event_types"]) if q["event_types"] else None
            if types is not None and etype not in types:
                continue
            user = users.get(q["user_id"]) or users.setdefault(q["user_id"],
                                                               self.s.user(q["user_id"]))
            if etype == "message" and not self._narrow_matches(json.loads(q["narrow"]),
                                                               row, user):
                continue
            self.s.x("INSERT INTO events (queue_id, body) VALUES (?,?)",
                     (q["queue_id"], json.dumps(build(user))))

    def _expire(self) -> None:
        with self.s.tx():
            gone = int(time.time()) - QUEUE_TTL
            self.s.x("DELETE FROM events WHERE queue_id IN "
                     "(SELECT queue_id FROM queues WHERE last_poll < ?)", (gone,))
            self.s.x("DELETE FROM queues WHERE last_poll < ?", (gone,))

    def _queue(self, me: dict, qid) -> sqlite3.Row:
        row = self.s.one("SELECT * FROM queues WHERE queue_id=? AND user_id=?",
                         (str(qid), me["id"]))
        if row is None or row["last_poll"] < time.time() - QUEUE_TTL:
            raise ApiError(f"Bad event queue ID: {qid}. Register a new queue with "
                           f"POST /register", "BAD_EVENT_QUEUE_ID", queue_id=qid)
        return row

    # POST /register
    def register(self, me: dict, p: dict) -> dict:
        types = _json_list(p["event_types"]) if p.get("event_types") else None
        narrow = _json_list(p.get("narrow") or "[]")
        for op, operand, _ in _terms(narrow):
            if not _known_op(op, operand):
                raise ApiError(f"Invalid narrow operator '{op}:{operand}': this server "
                               f"knows {NARROW_OPS}", "BAD_NARROW")
        self._expire()
        qid = f"{int(time.time())}:{secrets.token_hex(8)}"
        now = int(time.time())
        self.s.x("INSERT INTO queues VALUES (?,?,?,?,?,?)",
                 (qid, me["id"], json.dumps(types) if types is not None else None,
                  json.dumps(narrow), now, now))
        return {"queue_id": qid, "last_event_id": -1,
                "max_message_id": self.s.max_message_id(),
                "zulip_version": "agora-chat", "zulip_feature_level": 0,
                "event_queue_longpoll_timeout_seconds": int(self.poll_seconds),
                "user_id": me["id"], "email": me["email"], "full_name": me["full_name"]}

    # GET /events
    def events(self, me: dict, p: dict) -> dict:
        qid = p.get("queue_id")
        if not qid:
            raise ApiError("Missing 'queue_id' argument", "REQUEST_VARIABLE_MISSING")
        last = _int(p, "last_event_id", -1)
        self._queue(me, qid)
        with self.s.tx():  # the client has these: acknowledged, gone
            self.s.x("DELETE FROM events WHERE queue_id=? AND id<=?", (qid, last))
            self.s.x("UPDATE queues SET last_poll=? WHERE queue_id=?", (int(time.time()), qid))
        deadline = time.monotonic() + self.poll_seconds
        with self.s.lock:  # checked and waited on under one lock: no missed wake-up
            while True:
                self._queue(me, qid)  # DELETE /events while we waited
                rows = self.s.q("SELECT id, body FROM events WHERE queue_id=? AND id>? "
                                "ORDER BY id", (qid, last))
                left = deadline - time.monotonic()
                if rows or _bool(p.get("dont_block"), False) or left <= 0:
                    break
                self.s.changed.wait(left)
        events = [{**json.loads(r["body"]), "id": r["id"]} for r in rows]
        if not events and not _bool(p.get("dont_block"), False):
            # Same id the client sent: a heartbeat never moves a cursor past
            # an event it has not been given.
            events = [{"type": "heartbeat", "id": last}]
        return {"events": events, "queue_id": qid}

    # DELETE /events
    def delete_queue(self, me: dict, p: dict) -> dict:
        qid = p.get("queue_id")
        self._queue(me, qid)
        with self.s.tx():
            self.s.x("DELETE FROM events WHERE queue_id=?", (qid,))
            self.s.x("DELETE FROM queues WHERE queue_id=?", (qid,))
        return {}

    # -- messages

    # POST /messages
    def send(self, me: dict, p: dict) -> dict:
        kind = p.get("type", "stream")
        content = p.get("content")
        _check_content(content)
        now = int(time.time())
        to = p.get("to")
        if to is None:
            raise ApiError("Missing 'to' argument: a stream name or id, or a JSON list "
                           "of emails for a direct message", "REQUEST_VARIABLE_MISSING")
        with self.s.tx():
            if kind in ("stream", "channel"):
                refs = _json_list(to)
                if len(refs) != 1:
                    raise ApiError("'to' must name exactly one stream")
                stream = self.s.visible_stream(me, refs[0])
                topic = p.get("topic", p.get("subject"))
                if topic is None:
                    raise ApiError("Missing topic: send 'topic'", "REQUEST_VARIABLE_MISSING")
                mid = self.s.x("INSERT INTO messages (sender_id, type, stream_id, subject,"
                               " content, timestamp) VALUES (?,?,?,?,?,?)",
                               (me["id"], "stream", stream["stream_id"], topic, content, now))
            elif kind in ("private", "direct"):
                users = [self.s.user_by_ref(r) for r in _json_list(to)]
                if not users:
                    raise ApiError("'to' names no recipients")
                mid = self.s.x("INSERT INTO messages (sender_id, type, subject, content,"
                               " timestamp) VALUES (?,?,'',?,?)",
                               (me["id"], "private", content, now))
                for uid in {me["id"], *(u["id"] for u in users)}:
                    self.s.x("INSERT INTO recipients VALUES (?,?)", (mid, uid))
            else:
                raise ApiError(f"Invalid message type '{kind}': use 'stream' or 'private'")
            row = self.s.one("SELECT * FROM messages WHERE id=?", (mid,))

            def build(user):
                # `flags` on the event itself: that is where the hook's
                # _wakes() looks for "mentioned".
                return {"type": "message", "message": self.message_json(row, user),
                        "flags": self.flags(row, user)}
            self._publish("message", row, build)
        return {"id": mid}

    # GET /messages
    def fetch(self, me: dict, p: dict) -> dict:
        narrow = _json_list(p.get("narrow", "[]"))
        subs = self.s.subscribed(me["id"])
        where, args, channel = [], [], None
        for op, operand, neg in _terms(narrow):
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
                marks = _mention_marks(me)
                clause = "(" + " OR ".join(["instr(m.content, ?) > 0"] * len(marks)) + ")"
                a = marks
            elif op == "is" and operand in ("private", "dm"):
                clause, a = "m.type='private'", []
            else:
                raise ApiError(f"Invalid narrow operator '{op}:{operand}': M1 knows "
                               f"{NARROW_OPS}", "BAD_NARROW")
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
        # anchor=newest there is no after side, and Zulip returns num_before.
        before_op, after_op = "<", ">=" if include else ">"
        before_n, after_n = num_before, num_after + (1 if include else 0)
        if anchor == NEWEST:
            before_op, after_n = ("<=" if include else "<"), 0
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
        if content is not None:
            if not content.strip():
                raise ApiError("Message must not be empty: to delete it, use DELETE")
            _check_content(content)
        now = int(time.time())
        with self.s.tx():
            self.s.x("UPDATE messages SET content=coalesce(?, content), subject=coalesce(?, "
                     "subject), last_edit_timestamp=? WHERE id=?", (content, topic, now, mid))
            new = self.s.one("SELECT * FROM messages WHERE id=?", (mid,))

            def build(user):
                ev = {"type": "update_message", "user_id": me["id"], "message_id": mid,
                      "message_ids": [mid], "flags": self.flags(new, user),
                      "edit_timestamp": now, "rendering_only": False,
                      "propagate_mode": "change_one"}
                if row["type"] == "stream":
                    ev["stream_id"] = row["stream_id"]
                if content is not None:
                    ev.update(orig_content=row["content"], content=new["content"],
                              rendered_content=new["content"])
                if topic is not None:
                    ev.update(orig_subject=row["subject"], subject=new["subject"])
                return ev
            self._publish("update_message", new, build)
        return {"detached_uploads": []}

    # POST /messages/{id}/reactions
    def react(self, me: dict, mid: int, p: dict) -> dict:
        row = self._visible_message(me, mid)
        name = p.get("emoji_name")
        if not name:
            raise ApiError("Missing 'emoji_name' argument", "REQUEST_VARIABLE_MISSING")
        code, rtype = p.get("emoji_code") or name, p.get("reaction_type") or "unicode_emoji"
        with self.s.tx():
            try:
                self.s.x("INSERT INTO reactions VALUES (?,?,?,?,?)",
                         (mid, me["id"], name, code, rtype))
            except sqlite3.IntegrityError:
                raise ApiError("Reaction already exists.", "REACTION_ALREADY_EXISTS")
            self._publish("reaction", row, lambda user: {
                "type": "reaction", "op": "add", "message_id": mid, "user_id": me["id"],
                "user": {"user_id": me["id"], "email": me["email"],
                         "full_name": me["full_name"]},
                "emoji_name": name, "emoji_code": code, "reaction_type": rtype})
        return {}

    # -- streams, subscriptions, bots: what bot/setup_streams.py calls

    @staticmethod
    def _admin(me: dict, what: str) -> None:
        if me["role"] > ROLE_ADMIN:
            raise ApiError(f"Must be an organization administrator to {what}: use the "
                           f"credentials from add-human or bootstrap", "UNAUTHORIZED")

    def subscriptions(self, me: dict) -> dict:
        return {"subscriptions": [
            stream_json(r) for r in self.s.q(
                "SELECT s.* FROM streams s JOIN subscriptions x ON x.stream_id=s.stream_id "
                "WHERE x.user_id=? ORDER BY s.name", (me["id"],))]}

    # GET /streams
    def streams(self, me: dict, p: dict) -> dict:
        if _bool(p.get("include_all_active"), False):
            self._admin(me, "list every stream (include_all_active)")
            rows = self.s.q("SELECT * FROM streams ORDER BY name")
        else:
            rows = self.s.q("SELECT * FROM streams WHERE invite_only=0 OR stream_id IN "
                            "(SELECT stream_id FROM subscriptions WHERE user_id=?) "
                            "ORDER BY name", (me["id"],))
        return {"streams": [stream_json(r) for r in rows]}

    def _principals(self, me: dict, p: dict) -> list[dict]:
        if not p.get("principals"):
            return [me]
        users = [self.s.user_by_ref(r) for r in _json_list(p["principals"])]
        if any(u["id"] != me["id"] for u in users):
            self._admin(me, "subscribe or unsubscribe other users")
        return users

    @staticmethod
    def _names(p: dict) -> list[str]:
        subs = _json_list(p.get("subscriptions") or "[]")
        names = [s.get("name") if isinstance(s, dict) else s for s in subs]
        if not names or not all(isinstance(n, str) and n.strip() for n in names):
            raise ApiError("'subscriptions' must be a JSON list of stream names or "
                           "{\"name\": ...} objects", "BAD_REQUEST")
        return [n.strip().lstrip("#") for n in names]

    # POST /users/me/subscriptions
    def subscribe(self, me: dict, p: dict) -> dict:
        names, users = self._names(p), self._principals(me, p)
        invite_only = _bool(p.get("invite_only"), False)
        done: dict[str, list] = {}
        already: dict[str, list] = {}
        with self.s.tx():
            for name in names:
                row = self.s.stream_by_ref(name)
                if row is None:
                    self._admin(me, f"create stream #{name}")
                    if len(name) > 60:
                        raise ApiError(f"Stream name too long: '{name}' (60 characters max)")
                    row = self.s.one("SELECT * FROM streams WHERE stream_id=?",
                                     (self.s.create_stream(name, invite_only),))
                elif row["invite_only"] and me["role"] > ROLE_ADMIN \
                        and row["stream_id"] not in self.s.subscribed(me["id"]):
                    raise _no_stream(name)
                for u in users:
                    into = done if self.s.subscribe(u["id"], row["stream_id"]) else already
                    into.setdefault(u["email"], []).append(row["name"])
        return {"subscribed": done, "already_subscribed": already, "unauthorized": []}

    # DELETE /users/me/subscriptions
    def unsubscribe(self, me: dict, p: dict) -> dict:
        names, users = self._names(p), self._principals(me, p)
        removed, not_removed = [], []
        with self.s.tx():
            for name in names:
                row = self.s.stream_by_ref(name)
                if row is None:
                    raise _no_stream(name)
                n = 0
                for u in users:
                    n += self.s.db.execute("DELETE FROM subscriptions WHERE user_id=? AND "
                                           "stream_id=?", (u["id"], row["stream_id"])).rowcount
                (removed if n else not_removed).append(row["name"])
        return {"removed": removed, "not_removed": not_removed}

    def _stream_by_id(self, me: dict, sid: int) -> sqlite3.Row:
        if me["role"] <= ROLE_ADMIN:
            row = self.s.one("SELECT * FROM streams WHERE stream_id=?", (sid,))
            if row is None:
                raise _no_stream(sid)
            return row
        return self.s.visible_stream(me, sid)

    # PATCH /streams/{id}
    def update_stream(self, me: dict, sid: int, p: dict) -> dict:
        self._admin(me, "change a stream")
        self._stream_by_id(me, sid)
        with self.s.tx():
            if p.get("is_private") is not None:
                self.s.x("UPDATE streams SET invite_only=? WHERE stream_id=?",
                         (int(_bool(p["is_private"], False)), sid))
            if p.get("description") is not None:
                self.s.x("UPDATE streams SET description=? WHERE stream_id=?",
                         (p["description"], sid))
            if p.get("new_name"):
                self.s.x("UPDATE streams SET name=? WHERE stream_id=?", (p["new_name"], sid))
        return {}

    # POST /bots
    def create_bot(self, me: dict, p: dict) -> dict:
        self._admin(me, "create a bot")
        full_name, short = (p.get("full_name") or "").strip(), (p.get("short_name") or "").strip()
        if not full_name or not re.fullmatch(r"[a-z0-9_.-]+", short or "-"):
            raise ApiError("Send 'full_name' and a 'short_name' of lowercase letters, "
                           "digits, '.', '_' or '-'", "BAD_REQUEST")
        # <short_name>-bot@<realm>: the hook's _wakes() tells a bot from a
        # person by exactly this suffix.
        email = f"{short}-bot@{REALM}"
        if self.s.one("SELECT 1 FROM users WHERE email=?", (email,)):
            raise ApiError(f"Username already in use: {email}", "BAD_REQUEST")
        bot = self.s.create_user(email, full_name, is_bot=True)
        return {"user_id": bot["id"], "api_key": bot["api_key"], "email": bot["email"],
                "avatar_url": None, "default_sending_stream": None,
                "default_events_register_stream": None, "default_all_public_streams": False}

    def route(self, method: str, path: str, me: dict, p: dict) -> dict:
        m = re.fullmatch(r"messages/(\d+)(/reactions)?", path)
        st = re.fullmatch(r"streams/(\d+)(/members)?", path)
        if path == "messages" and method == "POST":
            return self.send(me, p)
        if path == "messages" and method == "GET":
            return self.fetch(me, p)
        if m and not m.group(2) and method == "PATCH":
            return self.edit(me, int(m.group(1)), p)
        if m and m.group(2) and method == "POST":
            return self.react(me, int(m.group(1)), p)
        if path == "register" and method == "POST":
            return self.register(me, p)
        if path == "events" and method == "GET":
            return self.events(me, p)
        if path == "events" and method == "DELETE":
            return self.delete_queue(me, p)
        if path == "users/me" and method == "GET":
            return user_json(me)
        if path == "users" and method == "GET":
            return {"members": [user_json(dict(r)) for r in self.s.q("SELECT * FROM users "
                                                                        "ORDER BY id")]}
        if path == "users/me/subscriptions":
            if method == "GET":
                return self.subscriptions(me)
            if method == "POST":
                return self.subscribe(me, p)
            if method == "DELETE":
                return self.unsubscribe(me, p)
        if path == "get_stream_id" and method == "GET":
            return {"stream_id": self.s.visible_stream(me, p.get("stream", ""))["stream_id"]}
        if path == "streams" and method == "GET":
            return self.streams(me, p)
        if st and st.group(2) and method == "GET":
            sid = self._stream_by_id(me, int(st.group(1)))["stream_id"]
            return {"subscribers": [r[0] for r in self.s.q(
                "SELECT user_id FROM subscriptions WHERE stream_id=? ORDER BY user_id", (sid,))]}
        if st and not st.group(2) and method == "PATCH":
            return self.update_stream(me, int(st.group(1)), p)
        if path == "bots" and method == "POST":
            return self.create_bot(me, p)
        raise ApiError(f"Endpoint not found: {method} /api/v1/{path} is not served by this "
                       f"chat server (see chat/README.md for what it serves)",
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
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _body_length(self) -> int:
        raw = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw)
        except ValueError:
            length = -1
        if length < 0:
            # The rest of the stream cannot be framed: answer, then hang up.
            self.close_connection = True
            raise ApiError(f"Malformed Content-Length header {raw!r}: send the body's "
                           f"length in bytes", "BAD_REQUEST")
        return length

    def _handle(self, method: str) -> None:
        try:
            url = urllib.parse.urlsplit(self.path)
            params = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
            length = self._body_length()
            if length:
                body = self.rfile.read(length).decode("utf-8", "replace")
                params.update(urllib.parse.parse_qsl(body, keep_blank_values=True))
            if not url.path.startswith("/api/v1/"):
                raise ApiError(f"Endpoint not found: {url.path} (the API is under /api/v1/)",
                               "BAD_REQUEST", 404)
            me = self.chat.s.authenticate(self.headers.get("Authorization"))
            out = self.chat.route(method, url.path[len("/api/v1/"):].strip("/"), me, params)
            self._reply(200, {"result": "success", "msg": "", **out})
        except ApiError as e:
            self._reply(e.status, {"result": "error", "msg": e.msg, "code": e.code, **e.extra})
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


def make_server(db: str, port: int = 8095, poll_seconds: float = POLL_SECONDS,
                seed_ids: int | None = None) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1 only; ``port=0`` picks a free one."""
    store = Store(db)
    if seed_ids is not None:
        store.seed_ids(seed_ids)
    handler = type("ChatHandler", (Handler,), {"chat": Chat(store, poll_seconds)})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    return server


def bootstrap(store: Store) -> dict:
    """The admin bot, created once; a second run hands back the same one."""
    email = f"admin-bot@{REALM}"
    row = store.one("SELECT * FROM users WHERE email=?", (email,))
    user = dict(row) if row else store.create_user(email, "Admin", is_bot=True, role=ROLE_ADMIN)
    return {"email": user["email"], "api_key": user["api_key"]}


def _read_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
    return out


def bots_from_env(store: Store, path: str) -> list[dict]:
    """Each ``ZULIP_<ROLE>_EMAIL`` / ``ZULIP_<ROLE>_API_KEY`` pair in ``path``
    as a bot with that email and key. ``ADMIN`` is skipped: in bot/.env it is
    the founder's own account, which only ``add-human`` makes. A bot that
    exists already gets the file's key; a human is never touched."""
    env, out = _read_env(path), []
    for key in sorted(env):
        m = re.fullmatch(r"ZULIP_([A-Z0-9_]+)_EMAIL", key)
        if not m or m.group(1) == "ADMIN" or not env.get(f"ZULIP_{m.group(1)}_API_KEY"):
            continue
        role, email, api_key = m.group(1), env[key], env[f"ZULIP_{m.group(1)}_API_KEY"]
        # setup_streams.py finds bots by these full names: CTO, CMO, COO, Watchdog.
        name = role if len(role) <= 3 else role.replace("_", " ").title()
        row = store.one("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,))
        if row is None:
            store.create_user(email, name, is_bot=True, api_key=api_key)
            action = "created"
        elif row["is_bot"]:
            store.x("UPDATE users SET api_key=? WHERE id=?", (api_key, row["id"]))
            action = "already exists"
        else:
            action = "skipped: a human has this email"
        out.append({"role": role, "email": email, "action": action})
    return out


def add_human(store: Store, email: str, name: str) -> dict:
    """An admin human: setup_streams.py runs as him and --check counts him as
    the founder in #feature. Refuses an email that is taken."""
    if store.one("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)):
        raise ApiError(f"{email} already has an account: nothing was created. Its key "
                       f"is in the database you pointed --db at", "BAD_REQUEST")
    return store.create_user(email, name, is_bot=False, role=ROLE_ADMIN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the API on 127.0.0.1")
    serve.add_argument("--seed-ids", type=int, metavar="N",
                       help="message and event ids continue above N (Zulip's last id)")
    serve.add_argument("--poll-seconds", type=float, default=POLL_SECONDS,
                       help="how long an idle GET /events waits before a heartbeat")
    boot = sub.add_parser("bootstrap", help="create the admin bot, print its credentials")
    boot.add_argument("--from-env", metavar="FILE",
                      help="also create each ZULIP_<ROLE>_EMAIL/_API_KEY pair as a bot")
    human = sub.add_parser("add-human", help="create an admin human (the founder runs this)")
    human.add_argument("--email", required=True)
    human.add_argument("--name", required=True)
    for p in (boot, human):
        p.add_argument("--json", action="store_true", help="print JSON (the agent path)")
    for p in (serve, boot, human):
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--port", type=int, default=8095)
    args = parser.parse_args(argv)
    site = f"http://{HOST}:{args.port}"

    if args.cmd == "bootstrap":
        store = Store(args.db)
        creds = {**bootstrap(store), "site": site}
        if args.from_env:
            creds["bots"] = bots_from_env(store, args.from_env)
        if args.json:
            print(json.dumps(creds))
        else:
            print(f"ZULIP_SITE={creds['site']}\nZULIP_BOT_EMAIL={creds['email']}\n"
                  f"ZULIP_BOT_API_KEY={creds['api_key']}")
            for b in creds.get("bots", []):
                print(f"# {b['role']}: {b['email']} {b['action']}")
        return 0
    if args.cmd == "add-human":
        try:
            user = add_human(Store(args.db), args.email, args.name)
        except ApiError as e:
            print(f"add-human: {e.msg}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"email": user["email"], "api_key": user["api_key"],
                              "user_id": user["id"], "site": site}))
        else:
            print(f"ZULIP_SITE={site}\nZULIP_ADMIN_EMAIL={user['email']}\n"
                  f"ZULIP_ADMIN_API_KEY={user['api_key']}")
        return 0
    server = make_server(args.db, args.port, args.poll_seconds, args.seed_ids)
    print(f"serving http://{HOST}:{server.server_address[1]}/api/v1/ db={args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
