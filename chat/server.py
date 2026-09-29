"""Our own chat server (M1, issue #61): the part of Zulip's REST API we use.

A stand-in for Zulip that speaks the same wire format under ``/api/v1/``, so
``bot/zulip.py``, ``bot/zulip_client.py``, ``bot/setup_streams.py`` and
``hooks/agora_hook.py`` work against it with nothing changed but
``ZULIP_SITE``. Field names, the HTTP Basic ``email:api_key`` auth and the
``{"result":"error","msg","code"}`` error shape are Zulip's, because those
tools read them.

Usage:
  python chat/server.py up [--json] [--no-serve] [--no-update] [--env bot/.env] [--port 8095]
                           [--dashboard-cmd "COMMAND"] [--dashboard-every 300]
  python chat/server.py add-human --email E --name N [--write-env] [--json] [--db PATH]
  python chat/server.py autostart install|remove|status [--json] [--startup-dir DIR]
  python chat/server.py serve --port 8095 [--db PATH] [--seed-ids N] [--dashboard-cmd "COMMAND"]
  python chat/server.py bootstrap --json [--from-env bot/.env] [--db PATH]

``up`` (issue #72) is the one command: it seeds ids above Zulip's, makes
the bots in bot/.env, the streams bot/setup_streams.py makes and their
members, and serves. Run twice, the second run changes nothing. It never
makes a human: without one it prints the ``add-human --write-env`` command.

``bootstrap`` creates one admin *bot* and prints its credentials; with
``--from-env`` it also creates each ``ZULIP_<ROLE>_EMAIL`` / ``_API_KEY`` pair
in that file as a bot with that very email and key, so switching a tool over
is one line (``ZULIP_SITE``). It also makes the Pool bot and a private
``#pool`` holding it, the COO bot and the founder (issue #80), and prints the
Pool bot's ``ZULIP_POOL_*`` pair. ``add-human`` is for the founder to run himself:
no agent creates his account. No password anywhere.

The event queue (D12) lives in SQLite, like everything else: a queue and its
events survive a restart and expire only after 7 days without a poll, where
Zulip's expire after minutes of silence (where the first attempt broke).

``GET /`` serves ``chat/page.html`` (M2, issue #65), a static file and no new
way in: the page asks for the human's email and API key and sends them as the
same HTTP Basic auth the bots use.

Stdlib only (D2's shape), one SQLite file in WAL mode, bound to 127.0.0.1
only (D4). Not here: history import, search.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shlex
import socket
import sqlite3
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(HERE, "data", "chat.sqlite3")
LOG = os.path.join(HERE, "data", "server.log")
BOT_ENV = os.path.join(ROOT, "bot", ".env")
PAGE = os.path.join(HERE, "page.html")
FOUNDER_KEYS = ("ZULIP_FOUNDER_EMAIL", "ZULIP_FOUNDER_API_KEY")
STARTUP_FILE = "agora-chat.cmd"
UPDATE_EVERY = 600.0  # seconds between `git pull --ff-only` runs under `up`
REALM = "chat.localhost"
ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER = 100, 200, 400  # Zulip's role numbers
NEWEST = 1 << 62
MAX_CONTENT = 10_000  # Zulip's own limit, in characters
POLL_SECONDS = 50.0  # an idle long poll answers with a heartbeat after this
QUEUE_TTL = 7 * 24 * 3600  # a queue nobody polled for this long is gone
# `@**all**` and its aliases notify everyone in the stream: for the hook's
# wake rule that is a mention like any other.
WILDCARDS = ("@**all**", "@**everyone**", "@**channel**", "@**stream**")
DASHBOARD_EVERY = 300.0  # seconds between --dashboard-cmd runs (RFC-002 §1)
DASHBOARD_TIMEOUT = 300.0  # a run past this stores nothing (dashboard.py --json --no-tokens takes ~113 s here)
DASHBOARD_STALE = 7200  # a document older than this is `stale`
DASHBOARD_KEEP = 24 * 3600  # rows older than this go on the next good sync
NO_DASHBOARD = "no dashboard command configured: start the server with --dashboard-cmd"


def default_dashboard_cmd() -> str:
    """The dashboard command `chat/run.cmd` passes `--dashboard-cmd` by hand;
    `startup_script()` below needs the same one so the server autostart
    launches at login also syncs (issue #87) — this is the one place that
    builds it. Forward-slashed and quoted around the interpreter path:
    `Dashboard.sync` runs it through `shlex.split`, which is POSIX-mode and
    treats `\\` as an escape character, and the path may contain spaces
    (e.g. `Program Files`)."""
    dash_py = sys.executable.replace("\\", "/")
    return f'"{dash_py}" C:/PlayGround/ops/tools/dashboard.py --json --no-tokens'


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
    is_bot INTEGER NOT NULL, api_key TEXT NOT NULL, role INTEGER NOT NULL,
    date_joined INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS streams (
    stream_id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL,
    invite_only INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '',
    is_archived INTEGER NOT NULL DEFAULT 0);
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
CREATE TABLE IF NOT EXISTS dashboard (ts INTEGER PRIMARY KEY, doc TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS archived_topics (
    stream_id INTEGER NOT NULL, topic TEXT COLLATE NOCASE NOT NULL,
    archived_at INTEGER NOT NULL, PRIMARY KEY (stream_id, topic));
CREATE TABLE IF NOT EXISTS read_messages (
    user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, message_id));
CREATE TABLE IF NOT EXISTS read_floor (
    user_id INTEGER NOT NULL, stream_id INTEGER NOT NULL, floor_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, stream_id));
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
        self.migrate()
        self.db.executescript(SCHEMA)

    def migrate(self) -> None:
        """Bring a database made by an older server up to SCHEMA without
        touching its rows. ``CREATE TABLE IF NOT EXISTS`` adds a new table;
        a new column on an existing table needs its own ``ALTER TABLE``.

        A database that already had a ``users`` table before ``read_floor``
        existed predates per-user read state entirely: every message in
        every stream is "old", so each user already subscribed to a stream
        gets that stream's floor at the current newest message id, rather
        than an unread pile of history nobody ever marked read. A brand-new
        database has no ``users`` table yet here (SCHEMA has not run), so it
        never gets a floor — nothing in it is old.

        A database written by the per-user-only ``read_floor`` (one row per
        user, issue #109) has its floor moved to every stream the user is
        already subscribed to, at the same value — read state earned before
        streams had their own floor survives becoming per-stream."""
        with self.lock:
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(streams)")}
            if cols and "is_archived" not in cols:
                self.db.execute("ALTER TABLE streams ADD COLUMN is_archived "
                                "INTEGER NOT NULL DEFAULT 0")
            had_users = bool(self.db.execute("PRAGMA table_info(users)").fetchall())
            had_subscriptions = bool(
                self.db.execute("PRAGMA table_info(subscriptions)").fetchall())
            floor_cols = {r[1] for r in self.db.execute("PRAGMA table_info(read_floor)")}
            if had_users and not floor_cols and had_subscriptions:
                # One transaction: a crash between CREATE and INSERT must not
                # leave an empty read_floor that no later start backfills.
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute(
                    "CREATE TABLE read_floor (user_id INTEGER NOT NULL, "
                    "stream_id INTEGER NOT NULL, floor_id INTEGER NOT NULL, "
                    "PRIMARY KEY (user_id, stream_id))")
                floor = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
                self.db.execute(
                    "INSERT INTO read_floor (user_id, stream_id, floor_id) "
                    "SELECT user_id, stream_id, ? FROM subscriptions", (floor,))
                self.db.execute("COMMIT")
            elif floor_cols and "stream_id" not in floor_cols:
                # One transaction: a crash mid-rename must not leave
                # read_floor_old behind or an empty read_floor that a later
                # start mistakes for "nothing to backfill".
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("ALTER TABLE read_floor RENAME TO read_floor_old")
                self.db.execute(
                    "CREATE TABLE read_floor (user_id INTEGER NOT NULL, "
                    "stream_id INTEGER NOT NULL, floor_id INTEGER NOT NULL, "
                    "PRIMARY KEY (user_id, stream_id))")
                self.db.execute(
                    "INSERT INTO read_floor (user_id, stream_id, floor_id) "
                    "SELECT o.user_id, s.stream_id, o.floor_id FROM read_floor_old o "
                    "JOIN subscriptions s ON s.user_id=o.user_id")
                self.db.execute("DROP TABLE read_floor_old")
                self.db.execute("COMMIT")

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
        """Joining a stream must not make its earlier history unread: a new
        subscription gets a floor at that stream's newest message id right
        away, so only a message sent after the join counts. ``OR REPLACE``
        also covers a user who left and rejoined: an unsubscribe leaves the
        old floor row behind, and rejoining should reset it to now, not the
        stale value from the earlier membership."""
        with self.tx():
            new = self.db.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?)",
                                  (user_id, stream_id)).rowcount > 0
            if new:
                self._set_floor_to_newest(user_id, stream_id)
            return new

    def _set_floor_to_newest(self, user_id: int, stream_id: int) -> None:
        floor = self.one("SELECT COALESCE(MAX(id), 0) FROM messages WHERE stream_id=?",
                         (stream_id,))[0]
        self.db.execute("INSERT OR REPLACE INTO read_floor (user_id, stream_id, floor_id) "
                        "VALUES (?,?,?)", (user_id, stream_id, floor))

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

    def read_floor(self, uid: int, stream_id: int) -> int:
        """Every message in ``stream_id`` at or below this id is read for
        ``uid`` (the migration above, and a join in ``subscribe()``),
        whatever ``read_messages`` says or does not say about it."""
        row = self.one("SELECT floor_id FROM read_floor WHERE user_id=? AND stream_id=?",
                       (uid, stream_id))
        return row[0] if row else 0

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
            "history_public_to_subscribers": True, "is_archived": bool(r["is_archived"])}


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


class Dashboard:
    """The sync thread (issue #81, RFC-002 §1): runs ``--dashboard-cmd``
    every ``every`` seconds and on ``POST /dashboard/sync``, and keeps each
    good document in the ``dashboard`` table.

    Only exit 0 stores anything. A failure or a timeout leaves the previous
    document where it was and puts the command's own stderr in
    ``last_error``, so a reader can tell a fresh document from an old one
    that nothing replaced (D3). The subprocess runs outside the store lock:
    a two-minute ``gh`` call must not stall every request."""

    def __init__(self, store: Store, cmd: str | None = None, every: float = DASHBOARD_EVERY,
                 clock=time.time):
        self.s, self.cmd, self.every, self.clock = store, cmd, every, clock
        self.timeout = DASHBOARD_TIMEOUT
        self.last_error: str | None = None
        self.running = threading.Lock()  # one run at a time, thread or POST
        self.stop = threading.Event()

    def start(self) -> None:
        if self.cmd:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                ok, why = self.sync()
                if not ok:
                    log(f"dashboard: sync failed: {why}")
            except Exception as e:  # the thread outlives one bad run
                log(f"dashboard: sync crashed: {e!r}")
            if self.stop.wait(self.every):
                return

    def sync(self) -> tuple[bool, str | int]:
        """(True, ts) when a document was stored; (False, why) otherwise."""
        if not self.cmd:
            return False, NO_DASHBOARD
        with self.running:
            try:
                done = subprocess.run(shlex.split(self.cmd), capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=self.timeout)
            except subprocess.TimeoutExpired as e:
                err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) \
                    else (e.stderr or "")
                self.last_error = (f"{err.rstrip()}\n" if err.strip() else "") + \
                    f"timed out after {self.timeout:g}s: {self.cmd}"
                self.last_error = self.last_error[-500:]
                return False, self.last_error.splitlines()[-1]
            except (OSError, ValueError) as e:
                self.last_error = f"could not run {self.cmd!r}: {e}"[-500:]
                return False, self.last_error
            if done.returncode != 0:
                err = (done.stderr or "").strip()
                self.last_error = (err or f"exited {done.returncode} with nothing on "
                                          f"stderr: {self.cmd}")[-500:]
                return False, self.last_error.splitlines()[-1]
            if not done.stdout:
                err = (done.stderr or "").strip()
                self.last_error = (f"{err}\n" if err else "") + \
                    f"exited 0 but printed nothing on stdout: {self.cmd}"
                self.last_error = self.last_error[-500:]
                return False, self.last_error.splitlines()[-1]
            ts = int(self.clock())
            with self.s.tx():
                self.s.x("INSERT OR REPLACE INTO dashboard (ts, doc, error) VALUES (?,?,NULL)",
                         (ts, done.stdout))
                self.s.x("DELETE FROM dashboard WHERE ts < ?", (ts - DASHBOARD_KEEP,))
                # Only a queue that named `dashboard` gets it, stream membership
                # aside: a hook registered for ["message"] is never woken by it.
                for q in self.s.q("SELECT queue_id, event_types FROM queues"):
                    if q["event_types"] and "dashboard" in json.loads(q["event_types"]):
                        self.s.x("INSERT INTO events (queue_id, body) VALUES (?,?)",
                                 (q["queue_id"], json.dumps({"type": "dashboard",
                                                             "last_sync": ts})))
            self.last_error = None
            return True, ts

    def state(self) -> dict:
        if not self.cmd:
            return {"doc": None, "last_sync": None, "stale": True, "last_error": NO_DASHBOARD}
        row = self.s.one("SELECT ts, doc FROM dashboard ORDER BY ts DESC LIMIT 1")
        doc = None
        last_error = self.last_error
        if row is not None:
            try:
                doc = json.loads(row["doc"])
            except (ValueError, TypeError):
                # exit 0 with text that is not JSON: shown as it came. A stored
                # `NULL` (or any other non-str/bytes) hits TypeError, not
                # ValueError — a bad row must never raise out of state().
                doc = row["doc"]
                if doc is None:
                    last_error = last_error or "the stored dashboard document is empty"
        last = row["ts"] if row is not None else None
        return {"doc": doc, "last_sync": last,
                "stale": last is None or self.clock() - last > DASHBOARD_STALE,
                "last_error": last_error}


class Chat:
    """The endpoints. Each returns the success body (without ``result``)."""

    def __init__(self, store: Store, poll_seconds: float = POLL_SECONDS,
                 dashboard: Dashboard | None = None):
        self.s = store
        self.poll_seconds = poll_seconds
        self.dashboard = dashboard or Dashboard(store)

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

    def flags(self, row, me: dict) -> list[str]:
        flags = []
        at_floor = row["type"] == "stream" \
            and row["id"] <= self.s.read_floor(me["id"], row["stream_id"])
        if row["sender_id"] == me["id"] or at_floor \
                or self.s.one("SELECT 1 FROM read_messages WHERE user_id=? AND message_id=?",
                              (me["id"], row["id"])):
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
                if stream["is_archived"]:
                    raise ApiError(f"Stream #{stream['name']} is archived: restore it first "
                                   f"with PATCH /streams/{stream['stream_id']} "
                                   f"is_archived=false", "STREAM_ARCHIVED")
                # A new message brings an archived topic back into the list.
                self.s.x("DELETE FROM archived_topics WHERE stream_id=? AND topic=?",
                         (stream["stream_id"], topic))
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

    # GET /users/me/subscriptions: an archived stream only with include_archived=true
    def subscriptions(self, me: dict, p: dict | None = None) -> dict:
        archived = "" if _bool((p or {}).get("include_archived"), False) \
            else "AND s.is_archived=0 "
        return {"subscriptions": [
            stream_json(r) for r in self.s.q(
                "SELECT s.* FROM streams s JOIN subscriptions x ON x.stream_id=s.stream_id "
                f"WHERE x.user_id=? {archived}ORDER BY s.name", (me["id"],))]}

    # GET /streams: an archived stream only with exclude_archived=false (Zulip's name)
    def streams(self, me: dict, p: dict) -> dict:
        archived = " AND is_archived=0" if _bool(p.get("exclude_archived"), True) else ""
        if _bool(p.get("include_all_active"), False):
            self._admin(me, "list every stream (include_all_active)")
            rows = self.s.q(f"SELECT * FROM streams WHERE 1{archived} ORDER BY name")
        else:
            rows = self.s.q("SELECT * FROM streams WHERE (invite_only=0 OR stream_id IN "
                            "(SELECT stream_id FROM subscriptions WHERE user_id=?))"
                            f"{archived} ORDER BY name", (me["id"],))
        return {"streams": [stream_json(r) for r in rows]}

    # -- archive: a stream or a topic leaves the lists; its messages stay

    # DELETE /streams/{id} is Zulip's "archive a channel"; PATCH is_archived=false
    # restores it. Nothing is deleted either way.
    def archive_stream(self, me: dict, sid: int, archived: bool = True) -> dict:
        self._admin(me, ("archive" if archived else "restore") + " a stream")
        self._stream_by_id(me, sid)
        self.s.x("UPDATE streams SET is_archived=? WHERE stream_id=?", (int(archived), sid))
        return {}

    @staticmethod
    def _topic_arg(p: dict) -> str:
        topic = p.get("topic", p.get("subject"))
        if topic is None or not str(topic).strip():
            raise ApiError("Missing 'topic' argument: the topic name to archive or restore",
                           "REQUEST_VARIABLE_MISSING")
        return str(topic)

    # GET /users/me/{stream_id}/topics: Zulip's topic list, newest first
    def topics(self, me: dict, sid: int, p: dict) -> dict:
        stream = self.s.visible_stream(me, sid)
        rows = self.s.q(
            "SELECT m.subject AS name, max(m.id) AS max_id, "
            "EXISTS (SELECT 1 FROM archived_topics a WHERE a.stream_id=m.stream_id "
            "AND a.topic=m.subject) AS archived "
            "FROM messages m WHERE m.type='stream' AND m.stream_id=? "
            "GROUP BY m.subject COLLATE NOCASE ORDER BY max_id DESC", (stream["stream_id"],))
        include = _bool(p.get("include_archived"), False)
        return {"topics": [{"name": r["name"], "max_id": r["max_id"],
                            "is_archived": bool(r["archived"])}
                           for r in rows if include or not r["archived"]]}

    # GET /unread: per-stream, per-topic unread counts (issue #168). Zulip
    # carries this shape (streams, each with its topics) inside `register`'s
    # `unread_msgs`; this server hands it back on its own so a client can
    # refresh counts without re-registering a queue. A message this user sent
    # is never unread for them, so it is excluded here rather than tracked as
    # read. Messages at or below the caller's read_floor *for that stream*
    # (the migration and ``subscribe()`` above) are old history — from
    # before per-user read state existed, or from before this user joined
    # that stream — read for everyone by definition. An archived stream or
    # an archived topic never contributes: it is hidden from the lists, so a
    # bubble for it could never clear.
    def unread(self, me: dict, p: dict | None = None) -> dict:
        streams: dict[int, dict] = {}
        for row in self.s.q(
                "SELECT s.stream_id AS stream_id, s.name AS name, m.subject AS topic, "
                "COUNT(*) AS n FROM messages m JOIN streams s ON s.stream_id=m.stream_id "
                "LEFT JOIN read_floor f ON f.user_id=? AND f.stream_id=m.stream_id "
                "WHERE m.type='stream' AND m.sender_id != ? AND s.is_archived=0 "
                "AND s.stream_id IN (SELECT stream_id FROM subscriptions WHERE user_id=?) "
                "AND m.id > COALESCE(f.floor_id, 0) AND NOT EXISTS "
                "(SELECT 1 FROM read_messages r WHERE r.user_id=? AND r.message_id=m.id) "
                "AND NOT EXISTS (SELECT 1 FROM archived_topics a WHERE a.stream_id=m.stream_id "
                "AND a.topic=m.subject) "
                "GROUP BY s.stream_id, m.subject COLLATE NOCASE",
                (me["id"], me["id"], me["id"], me["id"])):
            entry = streams.setdefault(row["stream_id"], {
                "stream_id": row["stream_id"], "name": row["name"], "unread": 0, "topics": []})
            entry["topics"].append({"name": row["topic"], "unread": row["n"]})
            entry["unread"] += row["n"]
        return {"streams": list(streams.values())}

    # POST /mark_topic_as_read: Zulip's own endpoint and argument names.
    # Every message in the topic not sent by `me` becomes read for `me`.
    def mark_topic_as_read(self, me: dict, p: dict) -> dict:
        sid = _int(p, "stream_id")
        topic = p.get("topic_name")
        if not topic or not str(topic).strip():
            raise ApiError("Missing 'topic_name' argument: the topic to mark read",
                           "REQUEST_VARIABLE_MISSING")
        self.s.visible_stream(me, sid)
        with self.s.tx():
            for row in self.s.q(
                    "SELECT id FROM messages WHERE type='stream' AND stream_id=? "
                    "AND subject=? COLLATE NOCASE AND sender_id != ?", (sid, topic, me["id"])):
                self.s.x("INSERT OR IGNORE INTO read_messages (user_id, message_id) "
                         "VALUES (?,?)", (me["id"], row["id"]))
        return {}

    # POST /mark_stream_as_read: Zulip's own endpoint and
    # argument name. Every message in the stream not sent by `me`, in any
    # topic, becomes read for `me` — the "mark all as read" next to a stream.
    def mark_stream_as_read(self, me: dict, p: dict) -> dict:
        sid = _int(p, "stream_id")
        self.s.visible_stream(me, sid)
        with self.s.tx():
            for row in self.s.q(
                    "SELECT id FROM messages WHERE type='stream' AND stream_id=? "
                    "AND sender_id != ?", (sid, me["id"])):
                self.s.x("INSERT OR IGNORE INTO read_messages (user_id, message_id) "
                         "VALUES (?,?)", (me["id"], row["id"]))
        return {}

    # GET|POST|DELETE /streams/{id}/archived_topics: list, archive, restore
    def archived_topics(self, me: dict, sid: int, method: str, p: dict) -> dict:
        stream = self.s.visible_stream(me, sid)
        sid = stream["stream_id"]
        if method == "POST":
            topic = self._topic_arg(p)
            if not self.s.one("SELECT 1 FROM messages WHERE type='stream' AND stream_id=? "
                              "AND subject=? COLLATE NOCASE", (sid, topic)):
                raise ApiError(f"No topic '{topic}' in #{stream['name']}: list them with "
                               f"GET /users/me/{sid}/topics", "BAD_REQUEST")
            self.s.x("INSERT OR IGNORE INTO archived_topics VALUES (?,?,?)",
                     (sid, topic, int(time.time())))
        elif method == "DELETE":
            self.s.x("DELETE FROM archived_topics WHERE stream_id=? AND topic=?",
                     (sid, self._topic_arg(p)))
        return {"stream_id": sid, "topics": [r[0] for r in self.s.q(
            "SELECT topic FROM archived_topics WHERE stream_id=? "
            "ORDER BY archived_at DESC, topic", (sid,))]}

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
            if p.get("is_archived") is not None:
                self.archive_stream(me, sid, _bool(p["is_archived"], False))
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
        at = re.fullmatch(r"streams/(\d+)/archived_topics", path)
        tp = re.fullmatch(r"users/me/(\d+)/topics", path)
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
                return self.subscriptions(me, p)
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
        if st and not st.group(2) and method == "DELETE":
            return self.archive_stream(me, int(st.group(1)))
        if at and method in ("GET", "POST", "DELETE"):
            return self.archived_topics(me, int(at.group(1)), method, p)
        if tp and method == "GET":
            return self.topics(me, int(tp.group(1)), p)
        if path == "unread" and method == "GET":
            return self.unread(me, p)
        if path == "mark_topic_as_read" and method == "POST":
            return self.mark_topic_as_read(me, p)
        if path == "mark_stream_as_read" and method == "POST":
            return self.mark_stream_as_read(me, p)
        if path == "bots" and method == "POST":
            return self.create_bot(me, p)
        if path == "dashboard" and method == "GET":
            return self.dashboard.state()
        if path == "dashboard/sync" and method == "POST":
            ok, out = self.dashboard.sync()
            if ok:
                return {"last_sync": out}
            if out == NO_DASHBOARD:
                raise ApiError(out, "DASHBOARD_NOT_CONFIGURED", last_error=out)
            raise ApiError(f"dashboard sync failed, nothing stored: {out}",
                           "DASHBOARD_SYNC_FAILED", 502, last_error=self.dashboard.last_error)
        raise ApiError(f"Endpoint not found: {method} /api/v1/{path} is not served by this "
                       f"chat server (see chat/README.md for what it serves)",
                       "BAD_REQUEST", 404)


def load_page(path: str = PAGE) -> tuple[bytes, str]:
    """The page and its Content-Security-Policy.

    ``default-src 'self'`` alone would block the page's own inline script
    and style, and ``'unsafe-inline'`` would let an injected one run too, so
    each inline block is allowed by its sha256 and nothing else is. Newlines
    are normalised first because a browser hashes the text after turning
    CRLF into LF, and a Windows checkout may have CRLF on disk."""
    with open(path, "rb") as f:
        html = f.read().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

    def hashes(tag: str) -> str:
        found = re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.S)
        return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(
            block.encode("utf-8")).digest()).decode("ascii") + "'" for block in found) or "'none'"
    csp = (f"default-src 'self'; script-src {hashes('script')}; style-src {hashes('style')}; "
           f"base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return html.encode("utf-8"), csp


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgoraChat/0.1"
    chat: Chat  # set by make_server

    def log_message(self, *a):  # quiet: tests and agents read stdout
        pass

    def _reply(self, status: int, body: dict) -> None:
        self._send(status, json.dumps(body).encode(), "application/json")

    def _send(self, status: int, data: bytes, content_type: str, **headers) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="zulip"')
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _local(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _check_origin(self) -> None:
        """A browser names the page that sent a request in ``Origin``: one
        from any other site is refused, so a page elsewhere cannot drive this
        API through the founder's browser. No ``Origin`` at all is how the
        bots, the hook and curl call, and stays allowed."""
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {f"http://{h}" for h in self._local()}:
            raise ApiError(f"Forbidden: a request from origin {origin!r}. This server "
                           f"answers only its own page, http://127.0.0.1:"
                           f"{self.server.server_address[1]}/", "FORBIDDEN", 403)

    def _serve_page(self) -> None:
        # The Host check keeps a DNS-rebound name (evil.example resolving to
        # 127.0.0.1) from loading the page as its own origin.
        host = (self.headers.get("Host") or "").lower()
        if host not in self._local():
            raise ApiError(f"Forbidden: Host {host!r}. Open http://127.0.0.1:"
                           f"{self.server.server_address[1]}/ instead", "FORBIDDEN", 403)
        data, csp = load_page()
        self._send(200, data, "text/html; charset=utf-8", Content_Security_Policy=csp,
                   X_Content_Type_Options="nosniff", Cache_Control="no-store",
                   Referrer_Policy="no-referrer")

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
            self._check_origin()
            if url.path == "/" and method == "GET":
                self._serve_page()  # static, no auth: it holds no data
                return
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
                seed_ids: int | None = None, dashboard_cmd: str | None = None,
                dashboard_every: float = DASHBOARD_EVERY) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1 only; ``port=0`` picks a free one. With
    ``dashboard_cmd`` its sync thread starts too (first run at once)."""
    store = Store(db)
    if seed_ids is not None:
        store.seed_ids(seed_ids)
    dashboard = Dashboard(store, dashboard_cmd, dashboard_every)
    handler = type("ChatHandler", (Handler,), {"chat": Chat(store, poll_seconds, dashboard)})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    dashboard.start()
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
        # ZULIP_FOUNDER_* is what `add-human --write-env` writes: his, too.
        if not m or m.group(1) in ("ADMIN", "FOUNDER") \
                or not env.get(f"ZULIP_{m.group(1)}_API_KEY"):
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
    """An admin human, subscribed to every stream there is (issue #72):
    setup_streams.py runs as him and --check counts him as the founder in
    #feature. Refuses an email that is taken."""
    if store.one("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)):
        raise ApiError(f"{email} already has an account: nothing was created. Its key "
                       f"is in the database you pointed --db at", "BAD_REQUEST")
    with store.tx():
        user = store.create_user(email, name, is_bot=False, role=ROLE_ADMIN)
        subscribe_everywhere(store, user["id"])
    return user


def subscribe_everywhere(store: Store, uid: int) -> list[str]:
    with store.tx():
        for row in store.q("SELECT stream_id FROM streams"):
            store.subscribe(uid, row[0])
    return [r[0] for r in store.q("SELECT s.name FROM streams s JOIN subscriptions x ON "
                                  "x.stream_id=s.stream_id WHERE x.user_id=? ORDER BY s.name",
                                  (uid,))]


def append_founder(path: str, email: str, api_key: str) -> None:
    """ZULIP_FOUNDER_EMAIL / _API_KEY at the end of ``path``. The caller has
    checked neither is there: an existing pair is never overwritten."""
    tail = ""
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        tail = "" if not data or data.endswith(b"\n") else "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{tail}{FOUNDER_KEYS[0]}={email}\n{FOUNDER_KEYS[1]}={api_key}\n")


# -- up (issue #72): one command from an empty database to a served chat


def stream_plan():
    """bot/setup_streams.py itself, loaded by path, so its STREAMS, ROLE_BOTS
    and WATCHDOG_* stay the one list of streams and who belongs where, and
    ``setup_streams.py --check`` agrees with what ``up`` built."""
    bot_dir = os.path.join(ROOT, "bot")
    sys.path.insert(0, bot_dir)  # its `from zulip_client import ...`
    try:
        spec = importlib.util.spec_from_file_location(
            "setup_streams", os.path.join(bot_dir, "setup_streams.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(bot_dir)
    return mod


def ensure_streams(store: Store) -> tuple[list[str], list[str]]:
    """The streams setup_streams.py makes, private, and each bot in exactly
    the ones it belongs in (never removed from others: that is --check's to
    report). Returns (stream names, bots setup_streams.py wants that are
    missing)."""
    plan = stream_plan()
    wanted = [(full, plan.bot_streams(full, short)) for full, short in plan.ROLE_BOTS]
    wanted.append((plan.WATCHDOG_BOT[0], plan.WATCHDOG_STREAMS))
    wanted.append((plan.POOL_BOT[0], {plan.POOL_STREAM}))
    wanted.append((plan.PM_BOT[0], {plan.PM_STREAM}))
    missing = []
    with store.tx():
        for name in plan.STREAMS:
            row = store.stream_by_ref(name)
            if row is None:
                store.create_stream(name, invite_only=True)
            elif not row["invite_only"]:
                store.x("UPDATE streams SET invite_only=1 WHERE stream_id=?", (row["stream_id"],))
        bots = {r["full_name"]: r["id"] for r in store.q("SELECT * FROM users WHERE is_bot=1")}
        for full, names in wanted:
            if full not in bots:
                missing.append(full)
                continue
            for name in names:
                store.subscribe(bots[full], store.stream_by_ref(name)["stream_id"])
    return list(plan.STREAMS), missing


def bootstrap_pool(store: Store) -> dict:
    """The Pool bot and a private #pool (issue #80, ops#85's pool control),
    with the Pool bot, the COO bot and every human in it. A Pool bot that
    ``--from-env`` made from ZULIP_POOL_* is the one used, never a second.
    ``members`` is read back from the subscriptions table, and ``missing``
    names who is not there yet, so nobody is reported in #pool who is not."""
    plan = stream_plan()
    full, short = plan.POOL_BOT
    with store.tx():
        row = store.one("SELECT * FROM users WHERE is_bot=1 AND full_name=?", (full,))
        bot = dict(row) if row else store.create_user(f"{short}-bot@{REALM}", full, is_bot=True)
        stream = store.stream_by_ref(plan.POOL_STREAM)
        if stream is None:
            sid = store.create_stream(plan.POOL_STREAM, invite_only=True)
        else:
            sid = stream["stream_id"]
            store.x("UPDATE streams SET invite_only=1 WHERE stream_id=?", (sid,))
        who = [bot["id"]] + [r["id"] for r in store.q(
            "SELECT id FROM users WHERE is_bot=0 OR (is_bot=1 AND full_name IN (%s))"
            % ",".join("?" * len(plan.POOL_ROLE_BOTS)), tuple(sorted(plan.POOL_ROLE_BOTS)))]
        for uid in who:
            store.subscribe(uid, sid)
    members = store.q("SELECT u.* FROM users u JOIN subscriptions x ON x.user_id=u.id "
                      "WHERE x.stream_id=? ORDER BY u.id", (sid,))
    missing = [f for f in sorted(plan.POOL_ROLE_BOTS)
               if not any(m["is_bot"] and m["full_name"] == f for m in members)]
    if not any(not m["is_bot"] for m in members):
        missing.append("founder")
    return {"email": bot["email"], "api_key": bot["api_key"], "stream": plan.POOL_STREAM,
            "members": [m["email"] for m in members], "missing": missing}


def zulip_max_id(env: dict, own_url: str, timeout: float = 3.0) -> tuple[int | None, str]:
    """Zulip's newest message id, asked once, as each account in bot/.env
    until one answers. (None, why) when Zulip is not reachable: the stored
    floor then stands."""
    site = (env.get("ZULIP_SITE") or "").rstrip("/")
    if not site:
        return None, "no ZULIP_SITE in the env file"
    if site.lower() in (own_url, own_url.replace("127.0.0.1", "localhost")):
        return None, f"ZULIP_SITE is this server ({site})"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    query = urllib.parse.urlencode({"anchor": "newest", "num_before": 1, "num_after": 0,
                                    "narrow": "[]"})
    best, why = None, "no account in the env file"
    for key in sorted(env, key=lambda k: k != "ZULIP_ADMIN_EMAIL"):
        m = re.fullmatch(r"ZULIP_([A-Z0-9_]+)_EMAIL", key)
        api_key = m and env.get(f"ZULIP_{m.group(1)}_API_KEY")
        if not api_key:
            continue
        req = urllib.request.Request(f"{site}/api/v1/messages?{query}")
        req.add_header("Authorization", "Basic " + base64.b64encode(
            f"{env[key]}:{api_key}".encode()).decode())
        try:
            with opener.open(req, timeout=timeout) as resp:
                ids = [x["id"] for x in json.loads(resp.read()).get("messages", [])]
        except urllib.error.HTTPError as e:
            why = f"Zulip at {site} refused {env[key]}: HTTP {e.code}"
            continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            return None, f"Zulip at {site} not reachable: {e}"
        best = max([best or 0, *ids])
    return best, why if best is None else f"Zulip at {site} answered"


def port_busy(port: int) -> bool:
    """Something already accepts on the port. Asked before binding because
    on Windows SO_REUSEADDR (which http.server sets) can bind on top of it."""
    try:
        socket.create_connection((HOST, port), timeout=1).close()
        return True
    except OSError:
        return False


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Updater:
    """Under ``up``: keeps the clone on ``main`` current with ``git pull
    --ff-only`` every ``every`` seconds, and asks for a restart when
    chat/server.py changed (a pull or a hand edit) and still compiles. The
    page needs neither: load_page() reads page.html per request. A failed
    pull is logged as failed and the server keeps running."""

    def __init__(self, restart, every: float = UPDATE_EVERY, root: str = ROOT,
                 source: str = os.path.abspath(__file__)):
        self.restart, self.every, self.root, self.source = restart, every, root, source
        self.digest = self._digest()
        self.bad = None
        self.stop = threading.Event()

    def _digest(self) -> str:
        with open(self.source, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def _git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", self.root, *args], capture_output=True, text=True,
                              timeout=120)

    def pull(self) -> tuple[bool, str]:
        try:
            branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
            if branch.returncode != 0:
                return False, f"not a git clone: {branch.stderr.strip()}"
            if branch.stdout.strip() != "main":
                return False, f"on branch {branch.stdout.strip()!r}, not main: not pulled"
            done = self._git("pull", "--ff-only")
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"git failed: {e}"
        out = (done.stdout + done.stderr).strip()
        return done.returncode == 0, out or f"git pull exited {done.returncode}"

    def changed(self) -> bool:
        """server.py differs from the running one and compiles. A version
        that does not compile is logged once and the running one stays."""
        digest = self._digest()
        if digest == self.digest or digest == self.bad:
            return False
        try:
            with open(self.source, encoding="utf-8") as f:
                compile(f.read(), self.source, "exec")
        except (SyntaxError, ValueError) as e:
            self.bad = digest
            log(f"update: chat/server.py changed but does not compile ({e}): "
                f"keeping the running version")
            return False
        return True

    def run(self, check_every: float = 5.0) -> None:
        next_pull = time.monotonic()
        while not self.stop.wait(0 if next_pull <= time.monotonic() else check_every):
            if time.monotonic() >= next_pull:
                next_pull = time.monotonic() + self.every
                ok, out = self.pull()
                log(f"update: git pull {'ok' if ok else 'FAILED'}: {out}")
            if self.changed():
                log("update: chat/server.py changed: restarting")
                self.restart()
                return


def up_setup(store: Store, env_path: str, own_url: str) -> dict:
    env = _read_env(env_path)
    floor, why = zulip_max_id(env, own_url)
    if floor is not None:
        store.seed_ids(floor)
    stored = store.max_message_id()
    log(f"ids: {why}; " + (f"its newest id is {floor}; " if floor is not None else "")
        + (f"ids continue above {stored}" if stored >= 0 else "no floor stored: ids start at 1"))
    bots = bots_from_env(store, env_path)
    if not bots:
        log(f"bots: no ZULIP_<ROLE>_EMAIL / ZULIP_<ROLE>_API_KEY pair in {env_path}: "
            f"no bot was made")
    streams, missing = ensure_streams(store)
    for full in missing:
        log(f"bots: {full} is missing, so no one speaks for it: add "
            f"ZULIP_{full.upper()}_EMAIL and ZULIP_{full.upper()}_API_KEY to {env_path}")
    humans = []
    for row in store.q("SELECT * FROM users WHERE is_bot=0 ORDER BY id"):
        subscribe_everywhere(store, row["id"])
        humans.append(row["email"])
    return {"bots": bots, "streams": streams, "humans": humans,
            "id_floor": store.max_message_id(), "founder_in_env": any(
                k in env for k in FOUNDER_KEYS)}


def cmd_up(args, argv: list[str]) -> int:
    url = f"http://{HOST}:{args.port}"
    if not os.path.exists(args.env):
        print(f"up: {args.env} not found. It holds each bot's ZULIP_<ROLE>_EMAIL and "
              f"ZULIP_<ROLE>_API_KEY (e.g. ZULIP_COO_EMAIL, ZULIP_COO_API_KEY): create it "
              f"(see bot/README.md) or point --env at it", file=sys.stderr)
        return 2
    store = Store(args.db)
    try:
        done = up_setup(store, args.env, url)
    finally:
        store.db.close()
    if not done["humans"]:
        cmd = f'python "{os.path.abspath(__file__)}" add-human --email <your email> ' \
              f'--name "<your name>" --write-env'
        cmd += "" if args.db == DEFAULT_DB else f' --db "{args.db}"'
        cmd += "" if args.env == BOT_ENV else f' --env "{args.env}"'
        log(f"no human account yet, so nobody can log in to the page. The founder runs, "
            f"once:\n    {cmd}")
        if done["founder_in_env"]:
            log(f"{args.env} already has ZULIP_FOUNDER_*, for an account this database "
                f"does not have: remove those two lines first")
    server = None
    if not args.no_serve:
        if port_busy(args.port):
            return _port_error(args.port, "something already answers there")
        try:
            server = make_server(args.db, args.port, dashboard_cmd=args.dashboard_cmd,
                                 dashboard_every=args.dashboard_every)
        except OSError as e:
            return _port_error(args.port, str(e))
        url = f"http://{HOST}:{server.server_address[1]}"
    summary = {"url": url, "db": args.db, "bots": done["bots"], "streams": done["streams"],
               "humans": done["humans"], "pid": os.getpid(), "id_floor": done["id_floor"]}
    if args.json:
        print(json.dumps(summary), flush=True)
    else:
        for b in done["bots"]:
            print(f"bot {b['role']}: {b['email']} {b['action']}")
        print(f"streams: {', '.join('#' + s for s in done['streams'])}")
        print(f"humans in every stream: {', '.join(done['humans']) or 'none yet'}")
        print(f"ids continue above {done['id_floor']}")
    if server is None:
        return 0
    if not args.json:
        print(f"serving {url}/ (open it in the browser) and /api/v1/ db={args.db}", flush=True)
    restart = threading.Event()
    updater = None
    if not args.no_update:
        def again():
            restart.set()
            server.shutdown()
        updater = Updater(again, args.update_every)
        threading.Thread(target=updater.run, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if updater:
            updater.stop.set()
        server.server_close()
    if restart.is_set():
        subprocess.Popen([sys.executable, os.path.abspath(__file__), *argv])
    return 0


def _port_error(port: int, why: str) -> int:
    print(f"up: port {port} is busy ({why}). See who holds it with "
          f"`netstat -ano | findstr :{port}` (Windows; `lsof -i :{port}` elsewhere) and "
          f"stop it, or pick another port with --port N", file=sys.stderr)
    return 1


# -- autostart (issue #72): one .cmd in the user's Startup folder


def _is_windows() -> bool:
    return sys.platform == "win32"


def startup_dir(arg: str | None) -> str:
    """--startup-dir, else AGORA_STARTUP_DIR, else the real Startup folder.
    Tests always pass one of the first two."""
    if arg or os.environ.get("AGORA_STARTUP_DIR"):
        return arg or os.environ["AGORA_STARTUP_DIR"]
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def startup_script(args) -> str:
    extra = f" --port {args.port}"
    extra += "" if args.db == DEFAULT_DB else f' --db "{args.db}"'
    extra += "" if args.env == BOT_ENV else f' --env "{args.env}"'
    # Without --dashboard-cmd the server autostart launches at login has no
    # sync thread and the dashboard panel stays empty until someone runs
    # run.cmd by hand (issue #87) — so pass the same command run.cmd does.
    # `\"` here is how Windows' argv parsing (not cmd.exe's) represents a
    # literal quote inside this already double-quoted argument: the command's
    # own value quotes the interpreter path (default_dashboard_cmd), and that
    # inner quote has to survive being embedded in the outer quoted argument
    # the same way run.cmd's static text spells it out by hand.
    escaped_dashboard_cmd = default_dashboard_cmd().replace('"', '\\"')
    extra += f' --dashboard-cmd "{escaped_dashboard_cmd}"'
    return ("@echo off\r\n"
            "REM Agora chat server at login (issue #72). Remove with:\r\n"
            f'REM   python "{os.path.abspath(__file__)}" autostart remove\r\n'
            f'if not exist "{os.path.dirname(LOG)}" mkdir "{os.path.dirname(LOG)}"\r\n'
            f'start "agora-chat" /min cmd /c ""{sys.executable}" "{os.path.abspath(__file__)}"'
            f' up{extra} >> "{LOG}" 2>&1"\r\n')


def answers(port: int) -> bool:
    """This chat server, not just anything, answers on the port."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{HOST}:{port}/api/v1/users/me", timeout=2) as resp:
            server = resp.headers.get("Server", "")
    except urllib.error.HTTPError as e:
        server = e.headers.get("Server", "")
    except (urllib.error.URLError, OSError):
        return False
    return server.startswith(Handler.server_version)


def cmd_autostart(args) -> int:
    if not _is_windows():
        print("autostart: Windows only (it writes a .cmd into the Startup folder). "
              "Elsewhere, start `python chat/server.py up` from your own login script",
              file=sys.stderr)
        return 2
    folder = startup_dir(args.startup_dir)
    path = os.path.join(folder, STARTUP_FILE)
    if args.action == "install":
        if not os.path.isdir(folder):
            print(f"autostart: no Startup folder at {folder}: nothing was installed",
                  file=sys.stderr)
            return 1
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(startup_script(args))
        out = {"installed": True, "file": path}
        text = (f"installed {path}: `up` starts minimised at your next login, logging to "
                f"{LOG}. It is not running now because of this; start it with "
                f"`python chat/server.py up`")
    elif args.action == "remove":
        existed = os.path.exists(path)
        if existed:
            os.remove(path)
        out = {"removed": existed, "file": path}
        text = f"removed {path}" if existed else f"not installed ({path} absent): nothing removed"
    else:
        out = {"installed": os.path.exists(path), "file": path,
               "url": f"http://{HOST}:{args.port}", "answering": answers(args.port)}
        text = (f"autostart: {'installed' if out['installed'] else 'NOT installed'} ({path})\n"
                f"server at {out['url']}: {'answering' if out['answering'] else 'NOT answering'}")
    print(json.dumps(out) if args.json else text)
    return 0


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
    human.add_argument("--write-env", action="store_true",
                       help="append ZULIP_FOUNDER_EMAIL/_API_KEY to --env; never overwrites")
    up = sub.add_parser("up", help="set everything up and serve (idempotent)")
    up.add_argument("--no-serve", action="store_true", help="set up, then exit")
    up.add_argument("--no-update", action="store_true",
                    help="no `git pull --ff-only` of main, no restart on a new server.py")
    up.add_argument("--update-every", type=float, default=UPDATE_EVERY, metavar="SECONDS")
    auto = sub.add_parser("autostart", help="start `up` at Windows login (Startup folder)")
    auto.add_argument("action", choices=("install", "remove", "status"))
    auto.add_argument("--startup-dir", metavar="DIR",
                      help="default: AGORA_STARTUP_DIR, else the user's Startup folder")
    for p in (boot, human, up, auto):
        p.add_argument("--json", action="store_true", help="print JSON (the agent path)")
    for p in (human, up, auto):
        p.add_argument("--env", default=BOT_ENV, metavar="FILE",
                       help="the bots' ZULIP_<ROLE>_EMAIL/_API_KEY file (default bot/.env)")
    for p in (serve, up):
        p.add_argument("--dashboard-cmd", metavar="COMMAND",
                       help="run this every --dashboard-every seconds; its stdout (exit 0) "
                            "is what GET /api/v1/dashboard returns")
        p.add_argument("--dashboard-every", type=float, default=DASHBOARD_EVERY,
                       metavar="SECONDS")
    for p in (serve, boot, human, up, auto):
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--port", type=int, default=8095)
    args = parser.parse_args(argv)
    site = f"http://{HOST}:{args.port}"

    if args.cmd == "bootstrap":
        store = Store(args.db)
        creds = {**bootstrap(store), "site": site}
        if args.from_env:
            creds["bots"] = bots_from_env(store, args.from_env)
        # After --from-env, so its COO bot joins #pool and its ZULIP_POOL_*
        # pair, if any, is the Pool bot.
        creds["pool"] = pool = bootstrap_pool(store)
        if args.json:
            print(json.dumps(creds))
        else:
            print(f"ZULIP_SITE={creds['site']}\nZULIP_BOT_EMAIL={creds['email']}\n"
                  f"ZULIP_BOT_API_KEY={creds['api_key']}\n"
                  f"ZULIP_POOL_EMAIL={pool['email']}\nZULIP_POOL_API_KEY={pool['api_key']}")
            for b in creds.get("bots", []):
                print(f"# {b['role']}: {b['email']} {b['action']}")
            print(f"# #{pool['stream']}: {', '.join(pool['members'])}")
            for who in pool["missing"]:
                print(f"# #{pool['stream']}: no {who} yet, so not in it: "
                      + ("add-human subscribes him" if who == "founder" else
                         f"bootstrap --from-env with ZULIP_{who.upper()}_* adds it"))
        return 0
    if args.cmd == "add-human":
        if args.write_env and os.path.exists(args.env):
            present = [k for k in FOUNDER_KEYS if k in _read_env(args.env)]
            if present:
                # Checked before the account exists: a refusal makes nothing.
                print(f"add-human: {args.env} already has {' and '.join(present)}: not "
                      f"overwritten, and no account was made. Remove those lines first "
                      f"if they belong to an account that is gone", file=sys.stderr)
                return 1
        store = Store(args.db)
        try:
            user = add_human(store, args.email, args.name)
        except ApiError as e:
            print(f"add-human: {e.msg}", file=sys.stderr)
            return 1
        streams = subscribe_everywhere(store, user["id"])
        written = None
        if args.write_env:
            try:
                append_founder(args.env, user["email"], user["api_key"])
                written = args.env
            except OSError as e:
                print(f"add-human: the account exists, but {args.env} could not be "
                      f"written ({e}): add its two lines yourself", file=sys.stderr)
        if args.json:
            print(json.dumps({"email": user["email"], "api_key": user["api_key"],
                              "user_id": user["id"], "site": site, "streams": streams,
                              "env_written": written}))
        else:
            print(f"ZULIP_SITE={site}\nZULIP_ADMIN_EMAIL={user['email']}\n"
                  f"ZULIP_ADMIN_API_KEY={user['api_key']}")
            print(f"# in every stream: {', '.join('#' + s for s in streams)}" if streams else
                  "# in no stream yet: `python chat/server.py up` subscribes you to each")
            if written:
                print(f"# wrote {FOUNDER_KEYS[0]} / {FOUNDER_KEYS[1]} to {written}")
        return 0 if written or not args.write_env else 1
    if args.cmd == "up":
        return cmd_up(args, sys.argv[1:] if argv is None else argv)
    if args.cmd == "autostart":
        return cmd_autostart(args)
    server = make_server(args.db, args.port, args.poll_seconds, args.seed_ids,
                         args.dashboard_cmd, args.dashboard_every)
    print(f"serving http://{HOST}:{server.server_address[1]}/ (page) and /api/v1/ "
          f"db={args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
