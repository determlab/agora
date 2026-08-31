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
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .discovery import invite_text, roster
from .mcp import McpHandler
from .room import HUMAN, MESSAGE, NOTE, SUMMARY, Hub, Muted, RoomClosed

STATIC = Path(__file__).resolve().parent.parent / "static"
CHAIR = "chair"


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
        self._cond = threading.Condition()

    def register(self, name: str, info: dict[str, Any]) -> None:
        with self._cond:
            info["registered_at"] = time.time()
            self._registered[name] = info

    def registered(self) -> dict[str, dict[str, Any]]:
        with self._cond:
            return dict(self._registered)

    def call(self, name: str, payload: dict[str, Any]) -> None:
        with self._cond:
            self._pending[name] = payload
            self._cond.notify_all()

    def wait(self, name: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                pending = self._pending.pop(name, None)
                if pending is not None:
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
    server_version = "Agora/0.1"
    app: Agora  # injected on the server instance

    # ---- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        # One line per request is noise for a local tool; keep errors only.
        if not str(args[1] if len(args) > 1 else "").startswith(("2", "3")):
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

        if path == "/api/state":
            reg = self.app.summons.registered()
            rows = roster(self.app.hub)
            for row in rows:
                # "connected" means the session's hook checked in with this
                # server, so a call button on that row will actually reach it.
                row["connected"] = row["name"] in reg
            return self._json({
                "rooms": self.app.hub.listing(),
                "roster": rows,
                "chair": self.app.chair_name,
                "url": self.app.public_url,
            })

        if path == "/api/summons":
            # The async half of a session's SessionStart hook parks here.
            who = (query.get("session") or [""])[0]
            secs = min(float((query.get("timeout") or ["300"])[0]), 900.0)
            payload = self.app.summons.wait(who, secs)
            if payload is None:
                return self._json({"summoned": False}, 204)
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
        self.send_header("Connection", "keep-alive")
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
            room.join(self.app.chair_name, role=HUMAN, provider="human")
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
            try:
                ev = room.post(name, text, kind=MESSAGE, role=HUMAN, provider="human")
            except (Muted, RoomClosed) as exc:
                return self._json({"error": str(exc)}, 409)
            return self._json({"seq": ev.seq})

        if action == "note":
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
            if what == "agenda":
                room.set_meta(agenda=text)
                room.post("agora", f"agenda set: {text}", kind="system", role=HUMAN)
                return self._json({"ok": True})
            if what == "title":
                room.set_meta(title=text)
                return self._json({"ok": True})
            if what == "call":
                # Summon a session into this room. Reaches it through its
                # SessionStart hook's long-poll, which wakes the agent.
                if target not in self.app.summons.registered():
                    return self._json(
                        {"error": f"{target} has no Agora hook running. Install "
                                  f"the hook and restart that session — see "
                                  f"README, 'Auto-connect'."}, 409)
                self.app.summons.call(target, {
                    "room": room.id, "title": room.title, "agenda": room.agenda,
                    "seq": room.snapshot()["seq"], "name": target,
                    "url": self.app.public_url,
                })
                room.post("agora", f"{target} called to the room by the chair",
                          kind="system", role=HUMAN)
                return self._json({"ok": True})
            if what == "prune":
                gone = room.prune()
                return self._json({"ok": True, "dropped": gone})
            if what == "ask_summary":
                room.post(self.app.chair_name,
                          f"@{target or 'everyone'} please post a summary of this "
                          f"meeting: call room_history, then room_summarize.",
                          kind=MESSAGE, role=HUMAN)
                return self._json({"ok": True})
            return self._json({"error": f"unknown admin action {what!r}"}, 400)

        self._json({"error": f"unknown action {action!r}"}, 404)


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
    args = parser.parse_args(argv)

    url = f"http://{args.host}:{args.port}"
    app = Agora(args.root, url)

    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.daemon_threads = True

    print(f"Agora on {url}")
    print(f"  rooms   {app.hub.root}")
    print(f"  agents  claude mcp add --transport http agora {url}/mcp")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
