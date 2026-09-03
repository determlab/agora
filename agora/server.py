"""Agora — a meeting room for a human and any number of agents.

One process serves three things:

* ``/mcp``      JSON-RPC over HTTP. Any MCP client joins here — Claude Code,
                Codex, Cursor, Gemini. This is what makes it provider-neutral.
* ``/api/*``    REST + SSE for the browser: the chair's seat, the roster, admin.
* ``/``         the web chat itself.

Bound to 127.0.0.1 by default and deliberately unauthenticated: it is a
single-user local tool, and the loopback bind *is* the boundary. Do not expose
it on a LAN without putting something in front of it — anyone who can reach the
port can speak as the chair.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .discovery import (GHOST_AFTER, availability, claude_sessions,
                        invite_text, roster)
from .mcp import ANY_ROOM, McpHandler
from .room import (HUMAN, LOBBY, MESSAGE, NOTE, ONLINE_WINDOW, SUMMARY, Hub,
                   Muted, NotSeated, RoomClosed, mention_note)

STATIC = Path(__file__).resolve().parent.parent / "static"
CHAIR = "chair"

#: How recently a lobby participant must have polled to count as parked.
#: `room_wait` refreshes `last_seen` on every call, and its ceiling is 25s, so a
#: session that is genuinely waiting touches this several times over. Shared with
#: `Participant.online` rather than declared twice, because two numbers that mean
#: the same thing drift.
#:
#: Membership alone is NOT liveness. A session that joined the lobby and then
#: crashed, was killed, or lost its connection stays in `participants` forever,
#: and reporting that as reachable rebuilds the exact false-green this whole
#: mechanism exists to remove — only with a longer fuse.
LOBBY_FRESH = ONLINE_WINDOW

#: A hook re-registers on every summons poll. Anything older than one poll plus
#: slack is a hook that has exited, and reporting it as reachable is a false
#: green that the chair discovers only by clicking three times.
REGISTRATION_TTL = 400.0


def _parked(lobby) -> set[str]:
    """Who is actually waiting in the lobby right now, not who ever joined it."""
    if lobby is None:
        return set()
    now = time.time()
    return {name for name, p in lobby.participants.items()
            if p.last_seen and (now - p.last_seen) < LOBBY_FRESH}


def _liveness(entry: dict[str, Any] | None, *, registered: bool = False) -> str:
    """Whether a session could hear a call right now.

    **A post is not a wake.** The Call button writes into the Lobby, and a Lobby
    message is only seen by a session already looping on `room_wait`. A session
    that is `idle` — parked on its human, not inside a tool call — never sees it,
    so offering Call for one is the same false green this app has shipped on
    three other surfaces. `busy` is the only registry state a queued call
    actually lands in, at the end of that turn.

    Anything the registry does not vouch for is `offline`, and that covers stale
    on its own: `claude_sessions` drops an entry whose heartbeat is past
    `STALE_AFTER`, so a stale file arrives here as no entry at all. A row can
    outlive its entry — a seat held over MCP is still a row — and a seat is not
    a session. An unrecognised status is read as `idle` for the same reason: the
    honest default is the one that promises less.

    `registered` is the one thing that outranks a missing entry, and only when
    there is no entry at all: a hook parked on `/api/summons` is a wake path
    the registry knows nothing about, and in a container it is the only one
    there is. It gets its own value rather than being folded into `busy` or
    `idle`, because those are statements about what the session is doing and
    this measures something else — that something is listening. Freshness is
    already applied by `Summons.registered`, so a hook that stopped polling
    arrives here as `False` and the row falls back to `offline` (D7).
    """
    if entry is None:
        return "hooked" if registered else "offline"
    return "busy" if entry.get("status") == "busy" else "idle"


class Summons:
    """The chair calling a session into a room.

    A web page cannot reach into a running agent, and Claude Code's session
    pipe is undocumented and Claude-only. What *is* available is a hook: a
    session's ``SessionStart`` hook parks here in a long-poll, and exiting with
    code 2 wakes its session with whatever text this returns. So the chair's
    "call to room" button writes here, and the hook turns it into a message the
    agent actually receives.

    Deliberately in memory: a summons is a live invitation. One that survived a
    restart of this server would call a session into a meeting that ended.
    """

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._registered: dict[str, dict[str, Any]] = {}
        #: name -> (when it was handed over, which room). Delivering a call is
        #: not the same as the agent arriving, and the chair needs to see the
        #: difference: clicking Call three times because nothing visibly
        #: happened is what a missing "delivered, waiting" state looks like.
        # (when, room, the registration it had at delivery)
        self._delivered: dict[str, tuple[float, str, dict[str, Any]]] = {}
        self._cond = threading.Condition()

    def register(self, name: str, info: dict[str, Any]) -> None:
        with self._cond:
            info["registered_at"] = time.time()
            self._registered[name] = info

    def registered(self) -> dict[str, dict[str, Any]]:
        """Only sessions whose hook is still parked.

        A registration used to live forever. The async hook exits as soon as it
        delivers one summons, so after the first Call the entry stayed and the
        button kept reporting that it had woken something that was no longer
        listening — the same false green, arriving later. The hook re-registers
        on every poll, so anything older than a poll interval plus slack is not
        parked any more.
        """
        cutoff = time.time() - REGISTRATION_TTL
        with self._cond:
            return {k: v for k, v in self._registered.items()
                    if v.get("registered_at", 0) > cutoff}

    def outstanding(self, max_age: float) -> dict[str, dict[str, Any]]:
        """Sessions woken by a call that have not turned up yet.

        A hook deregisters the instant it takes a summons, so a session Agora
        knows *only* by its registration would drop off the roster in the one
        window the chair is watching it — between "woken" and "joined" — while
        the Call button's own toast promises a row saying "joining…". Bounded,
        because a call nobody ever answered stops being evidence of anything.
        """
        cutoff = time.time() - max_age
        with self._cond:
            return {name: {**was, "name": name, "registered_at": at}
                    for name, (at, _room, was) in self._delivered.items()
                    if at > cutoff}

    def forget(self, name: str) -> None:
        """Drop a registration. Called when a summons is delivered, because the
        hook exits at that moment and nothing is parked any more."""
        with self._cond:
            self._registered.pop(name, None)

    def drop_calls_for_room(self, room_id: str) -> list[str]:
        """Cancel pending calls into a room that no longer exists.

        A call outlived its room: the agent was pulled toward nothing and could
        not tell a deleted room from a wrong id.
        """
        with self._cond:
            gone = [n for n, p in self._pending.items()
                    if p.get("room") == room_id]
            for n in gone:
                self._pending.pop(n, None)
            return gone

    def call(self, name: str, payload: dict[str, Any]) -> None:
        with self._cond:
            # Stamped so the roster can show a queued call's age. A call that
            # has waited three hours is telling the chair something a call that
            # has waited two minutes is not, and an invitation that ages
            # invisibly is the same failure as a green PR nobody merges.
            payload = {**payload, "queued_at": time.time()}
            self._pending[name] = payload
            self._cond.notify_all()

    def pending(self, name: str) -> dict[str, Any] | None:
        with self._cond:
            return self._pending.get(name)

    def delivered(self, name: str) -> tuple[float, str, dict[str, Any]] | None:
        with self._cond:
            return self._delivered.get(name)

    def arrived(self, name: str) -> None:
        with self._cond:
            self._delivered.pop(name, None)

    def wait(self, name: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                pending = self._pending.pop(name, None)
                if pending is not None:
                    # The caller is about to exit and wake its session, so it is
                    # no longer parked. Saying otherwise is a lie the Call button
                    # would repeat.
                    was = self._registered.pop(name, None)
                    # Keep what the registration knew. `outstanding` rebuilds the
                    # row from here, and a row that degrades to "project unknown"
                    # the instant Call lands is a worse answer than the one it
                    # had a second earlier — from the same evidence.
                    self._delivered[name] = (time.time(),
                                             str(pending.get("room", "")),
                                             was or {})
                    return pending
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=min(remaining, 5.0))


class Agora:
    """Everything the request handler needs, in one place."""

    def __init__(self, root: Path, public_url: str) -> None:
        self.hub = Hub(root / "rooms")
        self.summons = Summons()
        self.mcp = McpHandler(self.hub, self.summons)
        self.public_url = public_url
        self.chair_name = CHAIR


class Handler(BaseHTTPRequestHandler):
    # Read from `agora.__version__` (D9). It used to say 0.2 while the package
    # and `serverInfo` both said 0.1.0, which is how "which version am I
    # looking at" had three answers.
    server_version = f"Agora/{__version__}"
    # BaseHTTPRequestHandler defaults to HTTP/1.0, which closes the connection
    # after every response. Long polls and SSE both want a connection that
    # survives, and every response here sets Content-Length, which 1.1 requires.
    protocol_version = "HTTP/1.1"
    app: Agora  # injected on the server instance

    # ---- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        """Log failures only. A line per request is noise for a local tool.

        `args` is not always (method, code, size): `log_error` calls through
        here with a single formatted string, so the status has to be looked for
        rather than indexed.
        """
        status = str(args[1]) if len(args) > 1 else ""
        if not status.startswith(("2", "3")):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _room(self, room_id: str):
        room = self.app.hub.resolve(room_id)
        if room is None:
            self._json({"error": f"no room {room_id}"}, 404)
        return room

    # ---- GET ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/mcp":
            # Streamable HTTP allows a GET stream for server->client messages.
            # This server is tools-only and never initiates, so decline cleanly
            # rather than holding a socket that will never carry anything.
            return self._json({"error": "this server is POST-only"}, 405)

        if path == "/api/stream":
            return self._state_stream()

        if path == "/api/state":
            return self._json(self._state())

        if path == "/api/summons":
            # The async half of a session's SessionStart hook parks here.
            who = (query.get("session") or [""])[0]
            secs = min(float((query.get("timeout") or ["300"])[0]), 900.0)
            # Polling IS the registration. The registry is in memory, so a
            # restart of this server would otherwise leave every hooked session
            # invisible until its own next session start — which for an idle
            # session may be tomorrow. A hook that is parked here is reachable
            # by definition, so say so on every poll and the state heals itself.
            if who:
                self.app.summons.register(who, {"name": who, "via": "hook"})
            payload = self.app.summons.wait(who, secs)
            if payload is None:
                # 204 must carry no body — sending one desyncs a keep-alive
                # connection under HTTP/1.1.
                return self._send(204, b"", "application/json")
            return self._json({"summoned": True, **payload})

        if path.startswith("/api/rooms/"):
            rest = path[len("/api/rooms/"):]
            room_id, _, tail = rest.partition("/")
            room = self._room(room_id)
            if room is None:
                return
            if tail == "":
                return self._json(room.snapshot())
            if tail == "stream":
                return self._stream(room)
            if tail == "invite":
                who = (query.get("session") or [""])[0]
                return self._json(invite_text(room, self.app.public_url, who))
            if tail == "summary":
                return self._json({"text": room.local_summary()})
            if tail == "export":
                md = _export(room)
                return self._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8",
                                  {"Content-Disposition":
                                   f'attachment; filename="{room.id}.md"'})
        self._json({"error": "not found"}, 404)

    def _state(self) -> dict[str, Any]:
        """Everything the left-hand panes render: rooms, and who can be reached."""
        reg = self.app.summons.registered()
        lobby = self.app.hub.get(LOBBY)
        waiting = _parked(lobby)
        # A parked hook and an unanswered call are both the summons registry
        # knowing a session that discovery cannot see. `reg` wins the merge: a
        # session that has re-registered since being called is parked again.
        rows = roster(self.app.hub,
                      {**self.app.summons.outstanding(GHOST_AFTER), **reg})
        # `roster` has just read the registry and that read is cached for a
        # second, so this is a dict build rather than a second scan of disk.
        live = {s["name"]: s for s in claude_sessions()}
        now = time.time()
        for row in rows:
            queued = self.app.summons.pending(row["name"])
            handed = self.app.summons.delivered(row["name"])
            if handed and handed[1] in row["rooms"]:
                self.app.summons.arrived(row["name"])   # it turned up
                handed = None
            row["hooked"] = row["name"] in reg
            row["in_lobby"] = row["name"] in waiting
            # Same class of fact as the two above, and the one that decides
            # whether Call is worth offering at all — see `_liveness`.
            row["liveness"] = _liveness(live.get(row["name"]),
                                        registered=row["hooked"])
            row["pending"] = queued is not None
            row["queued_age"] = int(now - queued["queued_at"]) if queued else 0
            # Woken, told which room, not there yet. Usually it is mid-turn.
            row["awaiting"] = bool(handed)
            row["awaiting_age"] = int(now - handed[0]) if handed else 0
            # How the summons stands, which is not the same question as whether
            # anything is listening — `liveness` answers that one:
            #   now     — hooked or actively waiting; a call wakes it in ~1s
            #   queued  — a call is already sitting in the Lobby for it
            #   later   — nothing is parked; whether the call is ever picked up
            #             depends on `liveness`, and for `idle` it is not
            row["reach"] = ("awaiting" if handed
                            else "now" if (row["hooked"] or row["in_lobby"])
                            else "queued" if queued else "later")
        # Reachable first. A session that parks under a role name ("CMO") sits
        # beside its registry row ("ops-b0"); the callable one sorts up. A busy
        # session sorts above an idle one within the same reach, because the
        # call it queues does eventually land and an idle one's never does.
        order = {"now": 0, "awaiting": 1, "queued": 2, "later": 3}
        rows.sort(key=lambda r: (order[r["reach"]],
                                 0 if r["liveness"] == "busy" else 1,
                                 r["name"].lower()))
        return {
            "rooms": [r for r in self.app.hub.listing() if r["id"] != LOBBY],
            "lobby": {"waiting": sorted(waiting)},
            "roster": rows,
            "chair": self.app.chair_name,
            "url": self.app.public_url,
            # The chair must be able to answer "which version is this" without
            # leaving the browser — behaviour changing under someone mid-meeting
            # is the reason this issue exists.
            "version": __version__,
            # Not the roster's contents, but whether the roster could be built
            # at all. An unreadable registry and an empty one render identically
            # otherwise, and "nobody is running" is the wrong reading (D3).
            "discovery": availability(),
        }

    def _state_stream(self) -> None:
        """Push state when it actually changes. Replaces a Refresh button.

        The roster's source is the filesystem (Claude Code's session registry)
        and the summons registry, neither of which can notify, so this polls
        them server-side and sends only on a real change. The browser holds one
        connection instead of asking every few seconds and mostly getting the
        same answer back.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        last = None
        try:
            while True:
                state = self._state()
                # `queued_age` ticks every second and would defeat the diff, so
                # compare everything else and let the client age it locally.
                fingerprint = json.dumps(
                    {**state, "roster": [{k: v for k, v in r.items()
                                          if k != "queued_age"}
                                         for r in state["roster"]]},
                    sort_keys=True)
                if fingerprint != last:
                    last = fingerprint
                    self.wfile.write(b"event: state\ndata: "
                                     + json.dumps(state).encode("utf-8") + b"\n\n")
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(2.0)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return

    def _static(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(target.suffix,
                                                        "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _stream(self, room) -> None:
        """Server-sent events: every new event in this room, as it happens."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # No Content-Length and no chunked framing on this one, so under
        # HTTP/1.1 it has to be read-until-close. Say so explicitly, or the
        # client waits for a body length that never comes.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        seq = 0
        try:
            snap = room.snapshot()
            self.wfile.write(b"event: snapshot\ndata: "
                             + json.dumps(snap).encode("utf-8") + b"\n\n")
            self.wfile.flush()
            seq = snap["seq"]
            while True:
                events = room.wait_for(seq, 20.0)
                if events:
                    seq = events[-1].seq
                    payload = {"events": [e.as_dict() for e in events],
                               "participants": [p.as_dict()
                                                for p in room.participants.values()],
                               "seq": seq, "closed": room.closed}
                    self.wfile.write(b"event: append\ndata: "
                                     + json.dumps(payload).encode("utf-8") + b"\n\n")
                else:
                    # Comment frame: keeps proxies and the browser from timing out.
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return

    # ---- POST --------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()

        if path == "/mcp":
            response = self.app.mcp.handle(body)
            if response is None:
                return self._send(202, b"", "text/plain")
            return self._json(response)

        if path == "/api/register":
            # A session's SessionStart hook announcing itself. This is how a
            # session states its own name — nobody has to rename anything.
            name = str(body.get("name") or "").strip()
            if not name:
                return self._json({"error": "name required"}, 400)
            self.app.summons.register(name, {
                "name": name,
                "session_id": str(body.get("session_id") or ""),
                "cwd": str(body.get("cwd") or ""),
                "pid": int(body.get("pid") or 0),
                "provider": str(body.get("provider") or "claude-code"),
            })
            return self._json({"ok": True, "url": self.app.public_url,
                               "rooms": self.app.hub.listing()})

        if path == "/api/rooms":
            title = str(body.get("title") or "").strip() or "Untitled meeting"
            room = self.app.hub.create(title, str(body.get("agenda") or ""))
            # The chair's own name, sent by the browser. Hardcoding "chair" made
            # every transcript say "chair" no matter who was running the meeting.
            chair = str(body.get("name") or "").strip() or self.app.chair_name
            room.join(chair, role=HUMAN, provider="human")
            return self._json(room.snapshot())

        if path.startswith("/api/rooms/"):
            rest = path[len("/api/rooms/"):]
            room_id, _, action = rest.partition("/")
            room = self._room(room_id)
            if room is None:
                return
            return self._room_action(room, action, body)

        self._json({"error": "not found"}, 404)

    def _room_action(self, room, action: str, body: dict[str, Any]) -> None:
        name = str(body.get("name") or self.app.chair_name)
        text = str(body.get("text") or "")

        if action == "post":
            if not text.strip():
                return self._json({"error": "nothing to say"}, 400)
            try:
                ev = room.post(name, text, kind=MESSAGE, role=HUMAN, provider="human")
            except (Muted, NotSeated, RoomClosed) as exc:
                return self._json({"error": str(exc)}, 409)
            # A mention is a post, and a post is not a wake. Until the doorbell
            # exists the composer must say which mentioned seats actually heard
            # it — the same rule the Call button follows.
            report = room.mention_report(ev.mentions)
            return self._json({"seq": ev.seq, "mentions": report,
                               "note": mention_note(report)})

        if action == "note":
            if not text.strip():
                return self._json({"error": "empty note"}, 400)
            ev = room.post(name, text, kind=NOTE, role=HUMAN)
            return self._json({"seq": ev.seq})

        if action == "summary":
            ev = room.post(name, text or room.local_summary(),
                           kind=SUMMARY, role=HUMAN)
            return self._json({"seq": ev.seq})

        # ---- admin ---------------------------------------------------------
        if action == "admin":
            what = str(body.get("action") or "")
            target = str(body.get("target") or "")
            try:
                return self._admin(room, what, target, name, text)
            except RoomClosed as exc:
                # Several admin actions announce themselves in the room, and a
                # closed room refuses messages. Reaching the handler's own
                # exception was a 500 and a traceback in the log.
                return self._json({"error": str(exc)}, 409)

        self._json({"error": f"unknown action {action!r}"}, 404)

    def _admin(self, room, what: str, target: str, name: str,
               text: str) -> None:
        """One chair action. Split out so the caller can turn a closed-room
        refusal into a 409 rather than a traceback."""
        if what == "mute":
            return self._json({"ok": room.set_muted(target, True)})
        if what == "unmute":
            return self._json({"ok": room.set_muted(target, False)})
        if what == "kick":
            room.leave(target)
            return self._json({"ok": True})
        if what == "close":
            room.set_meta(closed=True)
            room.post("agora", "meeting closed by the chair",
                      kind="system", role=HUMAN)
            return self._json({"ok": True})
        if what == "reopen":
            room.set_meta(closed=False)
            return self._json({"ok": True})
        if what == "archive":
            # Closes as well: an archived meeting that still accepts posts
            # is a meeting, not an archive.
            room.set_meta(archived=True, closed=True)
            return self._json({"ok": True})
        if what == "unarchive":
            room.set_meta(archived=False)
            return self._json({"ok": True})
        if what == "delete":
            # Archive hides; delete removes. Kept separate and deliberately
            # harder to reach, because the transcript is the record.
            if room.id == LOBBY:
                return self._json({"error": "the Lobby is not deletable"}, 400)
            # A call must not outlive the room it points at: the agent would
            # be pulled toward nothing, unable to tell a deleted room from a
            # wrong id.
            cancelled = self.app.summons.drop_calls_for_room(room.id)
            self.app.hub.delete(room.id)
            return self._json({"ok": True, "deleted": room.id,
                               "cancelled_calls": cancelled})
        if what == "agenda":
            room.set_meta(agenda=text)
            room.post("agora", f"agenda set: {text}", kind="system", role=HUMAN)
            return self._json({"ok": True})
        if what == "title":
            room.set_meta(title=text)
            return self._json({"ok": True})
        if what == "call":
            if self.app.hub.get(room.id) is None:
                return self._json({"error": "that meeting no longer exists"}, 404)
            if room.closed:
                return self._json(
                    {"error": "this meeting is closed — reopen it before "
                              "calling anyone in"}, 409)
            # Read this BEFORE calling. A parked hook wakes, takes the summons
            # and deregisters itself within microseconds, so asking afterwards
            # reports "not hooked" for a call that in fact landed.
            hooked = target in self.app.summons.registered()
            # The same fact the roster row showed before the click, read the
            # same way. A button that says "idle, not callable" and a response
            # that says "woken" is how this app taught the chair to distrust it.
            live = _liveness(next((s for s in claude_sessions()
                                   if s["name"] == target), None),
                             registered=hooked)
            # Two ways to reach a session, tried together because each covers a
            # case the other does not: the hook reaches an idle session, the
            # Lobby reaches one that never restarted.
            payload = {"room": room.id, "title": room.title,
                       "agenda": room.agenda, "seq": room.snapshot()["seq"],
                       "name": target, "url": self.app.public_url}
            self.app.summons.call(target, payload)
            # The Lobby. `room_wait` on it exists in every session already
            # connected, which a newly added tool never would.
            lobby = self.app.hub.get(LOBBY)
            reached_lobby = False
            if lobby is not None:
                # Parked means polling, not merely present — see LOBBY_FRESH.
                reached_lobby = target in _parked(lobby)
                lobby.post(
                    name,
                    f"@{target} — the chair calls you into "
                    f"{room.title!r} (room id `{room.id}`). "
                    f"room_join with room=\"{room.id}\" and name=\"{target}\", "
                    f"then room_history, then loop on room_wait. When idle, "
                    f"rest on room_wait with room=\"{ANY_ROOM}\" and "
                    f"cursors={{\"{room.id}\": {payload['seq']}}} — one call "
                    f"covers every room you are in plus the lobby. You arrive "
                    f"muted; wait for the chair to unmute you.",
                    kind=MESSAGE, role=HUMAN, provider="human")
            room.post("agora", f"{target} called to the room by the chair",
                      kind="system", role=HUMAN)
            # A parked hook and a parked `room_wait` are the two measured wake
            # paths, and both work whatever the registry says the session is
            # doing. Without one of them the call is only a Lobby post, so what
            # happens next is `live`'s answer, not this button's.
            woke = hooked or reached_lobby
            if woke:
                note = ""
            elif live == "busy":
                note = (f"{target} is mid-turn, so nothing woke it just now. "
                        f"The call is queued in the Lobby and arrives when it "
                        f"finishes that turn. Nothing is lost; it is just not "
                        f"instant.")
            elif live == "idle":
                note = (f"{target} is idle — waiting on its human, not in a "
                        f"tool loop — and a call is a post, so there is nothing "
                        f"there to read it. The call is parked in the Lobby, "
                        f"but no session picks it up until that one takes a "
                        f"turn. To reach it now, type in that session's "
                        f"terminal.")
            else:
                note = (f"There is no live session called {target} — no "
                        f"registry entry, or its heartbeat has gone stale. The "
                        f"call is parked in the Lobby in case it comes back, "
                        f"but nothing was reached.")
            return self._json({
                "ok": True, "hooked": hooked, "in_lobby": reached_lobby,
                "liveness": live, "woke": woke, "note": note,
            })
        if what == "prune":
            gone = room.prune()
            return self._json({"ok": True, "dropped": gone})
        if what == "ask_summary":
            room.post(name,
                      f"@{target or 'everyone'} please post a summary of this "
                      f"meeting: call room_history, then room_summarize.",
                      kind=MESSAGE, role=HUMAN)
            return self._json({"ok": True})
        return self._json({"error": f"unknown admin action {what!r}"}, 400)


def _export(room) -> str:
    snap = room.snapshot()
    out = [f"# {snap['title']}", ""]
    if snap["agenda"]:
        out += [f"**Agenda:** {snap['agenda']}", ""]
    out += [f"*{time.strftime('%Y-%m-%d %H:%M', time.localtime(snap['created']))}"
            f" · {len(snap['participants'])} participants*", ""]
    notes = [e for e in snap["events"] if e["kind"] == NOTE]
    summaries = [e for e in snap["events"] if e["kind"] == SUMMARY]
    for s in summaries:
        out += ["## Summary", "", s["text"], ""]
    if notes:
        out += ["## Notes", ""] + [f"- {n['text']} — *{n['author']}*" for n in notes]
        out += [""]
    out += ["## Transcript", ""]
    for e in snap["events"]:
        if e["kind"] not in (MESSAGE, "system"):
            continue
        stamp = time.strftime("%H:%M", time.localtime(e["ts"]))
        if e["kind"] == "system":
            out.append(f"*{stamp} — {e['text']}*")
        else:
            out.append(f"**{e['author']}** ({stamp})  \n{e['text']}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agora", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="where rooms/ lives")
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    # What a *client* should dial, which is not always what the server binds.
    # In a container the bind is 0.0.0.0 (the container's interface) while the
    # reachable address is the host loopback and possibly a different published
    # port; `claude mcp add http://0.0.0.0:8765/mcp` registers a server nothing
    # can reach, and the invite text is the one place that URL is copied by hand.
    parser.add_argument("--public-url", default=os.environ.get("AGORA_PUBLIC_URL", ""),
                        help="the URL clients dial, if it differs from the bind "
                             "(a published container port); env AGORA_PUBLIC_URL")
    args = parser.parse_args(argv)

    bound = f"http://{args.host}:{args.port}"
    # A wildcard bind is not an address. Falling back to loopback keeps the
    # printed and advertised URL dialable when nobody passed --public-url.
    url = args.public_url.rstrip("/") or (
        f"http://127.0.0.1:{args.port}" if args.host in ("0.0.0.0", "::", "")
        else bound)
    app = Agora(args.root, url)

    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.daemon_threads = True

    print(f"Agora {__version__} on {url}" + (f"  (bound {bound})" if url != bound else ""))
    print(f"  rooms   {app.hub.root}")
    print(f"  agents  claude mcp add --transport http agora {url}/mcp")
    # Said once, loudly, at the only moment a human is definitely reading: an
    # empty roster is otherwise indistinguishable from "nobody is running", and
    # in a container that is the default state rather than an edge case. It is
    # also on `/api/state`, because the chair is in a browser, not in this log.
    seen = availability()
    if seen["reason"] or seen["note"]:
        print(f"  WARNING {seen['reason'] or seen['note']}")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
