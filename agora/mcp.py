"""MCP over streamable HTTP — the door every provider comes through.

Deliberately not the MCP SDK. The server implements the four methods a tool
provider actually needs (`initialize`, `tools/list`, `tools/call`, `ping`) as
plain JSON-RPC, so Agora has zero third-party dependencies and cannot be broken
by an SDK major. That matters here more than usual: this process is the room,
and a room that will not start is worse than one missing a feature.

Any MCP client works — Claude Code, Codex, Cursor, Gemini. That is the whole
reason the transport is MCP rather than Claude Code's private session pipe: the
pipe is undocumented, versioned with the binary, and Claude-only.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .room import AGENT, MESSAGE, NOTE, SUMMARY, Hub, Muted, RoomClosed

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "agora", "version": "0.1.0"}

# Long-poll ceiling.
#
# Was 45s. A session parked at exactly that value got
# `The socket connection was closed unexpectedly.` while 20s returned cleanly,
# so something between the two — a client read timeout, most likely — cuts the
# connection below the value this tool advertised.
#
# The advertised maximum is the thing to fix, not the guidance. A ceiling nobody
# can actually reach is a trap: it works in a live room, where a dropped poll is
# invisible because you retry into an ongoing conversation, and it fails while
# *parked*, where a dropped poll is a missed Call — the chair clicks, the button
# reports success, nothing arrives. That is the same false-green this mechanism
# exists to remove, one layer down.
#
# 25s is comfortably inside the observed-good range and still wakes in about a
# second when something actually happens, because the wait returns on notify.
MAX_WAIT = 25.0


def _text(payload: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload,
                                                               ensure_ascii=False,
                                                               indent=2)
    return {"content": [{"type": "text", "text": body}]}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "room_list",
        "description": "List the meeting rooms on this server, with their agenda "
                       "and how many people are in each. Call this first when you "
                       "are told to join a meeting but not given a room id.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "room_join",
        "description": "Join a meeting room. Announces you to everyone. Safe to "
                       "call again — rejoining just refreshes your presence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "Room id or exact title"},
                "name": {"type": "string", "description": "How you appear in the "
                                                          "room, e.g. 'shal-38'"},
                "provider": {"type": "string", "description": "claude-code, codex, "
                                                              "cursor, gemini, …"},
                "role": {"type": "string", "description": "Your seat, e.g. 'CTO'"},
            },
            "required": ["room", "name"],
        },
    },
    {
        "name": "room_post",
        "description": "Say something in the room. Everyone sees it, including the "
                       "human chairing the meeting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string"},
                "name": {"type": "string", "description": "Your name in the room"},
                "text": {"type": "string"},
            },
            "required": ["room", "name", "text"],
        },
    },
    {
        "name": "room_wait",
        "description": "Block until somebody speaks, then return what was said. "
                       "This is how you stay in a meeting: pass the highest seq you "
                       "have seen, and call it again after each reply. Returns an "
                       "empty list on timeout — that is normal, call it again. "
                       "Also how you wait in the lobby: room_wait on room=\"lobby\" "
                       "returns the moment the chair calls you into a meeting. "
                       "Leave timeout unset unless you have a reason.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string"},
                "name": {"type": "string"},
                "since": {"type": "integer",
                          "description": "Highest seq you already have. 0 for "
                                         "everything since you joined."},
                "timeout": {"type": "number",
                            "description": f"Seconds to wait, max {MAX_WAIT}"},
            },
            "required": ["room", "name"],
        },
    },
    {
        "name": "room_history",
        "description": "The full transcript so far. Call this once after joining so "
                       "you know what was said before you arrived.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string"},
                "limit": {"type": "integer", "description": "Last N events only"},
            },
            "required": ["room"],
        },
    },
    {
        "name": "room_note",
        "description": "Add a note to the room's notes panel. Notes sit beside the "
                       "conversation rather than in it — use them for a decision, an "
                       "action item, or something the chair should not lose.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string"},
                "name": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["room", "name", "text"],
        },
    },
    {
        "name": "room_summarize",
        "description": "Post a summary of the meeting. Write it yourself from the "
                       "transcript — call room_history first. Summaries are marked "
                       "and pinned separately from ordinary messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string"},
                "name": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["room", "name", "text"],
        },
    },
    {
        "name": "agora_standby",
        "description": "Wait to be called into a meeting. Blocks until the chair "
                       "calls you, then returns the room to join. Call this when "
                       "you have nothing else to do and want to be reachable — it "
                       "is how the chair's Call button reaches a session that has "
                       "no Agora hook installed. Returns empty on timeout; call "
                       "it again.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Your session name, e.g. 'shal-38'"},
                "provider": {"type": "string"},
                "timeout": {"type": "number",
                            "description": f"Seconds to wait, max {MAX_WAIT}"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "room_leave",
        "description": "Leave the room. Only do this when the meeting is over or "
                       "you are told to.",
        "inputSchema": {
            "type": "object",
            "properties": {"room": {"type": "string"}, "name": {"type": "string"}},
            "required": ["room", "name"],
        },
    },
]


class McpHandler:
    def __init__(self, hub: Hub, summons: Any = None) -> None:
        self.hub = hub
        # The summons registry, shared with the web side. An agent that parks in
        # `agora_standby` becomes callable from the browser without needing the
        # SessionStart hook installed — the hook is the better path because it
        # works when the agent is idle, but this one needs nothing but MCP.
        self.summons = summons
        self._tools: dict[str, Callable[[dict], dict]] = {
            "agora_standby": self._standby,
            "room_list": self._room_list,
            "room_join": self._room_join,
            "room_post": self._room_post,
            "room_wait": self._room_wait,
            "room_history": self._room_history,
            "room_note": self._room_note,
            "room_summarize": self._room_summarize,
            "room_leave": self._room_leave,
        }

    # ---- JSON-RPC ----------------------------------------------------------

    def handle(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC request in, one response out. None for a notification."""
        method = payload.get("method", "")
        rpc_id = payload.get("id")
        params = payload.get("params") or {}

        if method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions":
                        "Agora is a meeting room shared by a human chair and any "
                        "number of agents. Join with room_join, read the room with "
                        "room_history, then loop on room_wait and reply with "
                        "room_post. Stay until the room closes.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name", "")
                fn = self._tools.get(name)
                if fn is None:
                    return self._error(rpc_id, -32601, f"unknown tool {name!r}")
                result = fn(params.get("arguments") or {})
            else:
                return self._error(rpc_id, -32601, f"unknown method {method!r}")
        except Exception as exc:  # a tool fault must not kill the room
            return self._error(rpc_id, -32603, f"{type(exc).__name__}: {exc}")

        if rpc_id is None:
            return None
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    @staticmethod
    def _error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": code, "message": message}}

    # ---- tools -------------------------------------------------------------

    def _resolve(self, args: dict) -> Any:
        room = self.hub.resolve(str(args.get("room", "")))
        if room is None:
            raise LookupError(
                f"no room {args.get('room')!r}. Call room_list to see what exists.")
        return room

    def _standby(self, args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return _err("name is required — it is how the chair calls you")
        if self.summons is None:
            return _err("this server has no summons registry")
        self.summons.register(name, {"name": name, "via": "mcp",
                                     "provider": str(args.get("provider") or "")})
        timeout = min(float(args.get("timeout") or MAX_WAIT), MAX_WAIT)
        called = self.summons.wait(name, timeout)
        if called is None:
            return _text({"called": False,
                          "note": "Nobody called you in that window. Call "
                                  "agora_standby again to stay reachable."})
        return _text({
            "called": True,
            "room": called.get("room"),
            "title": called.get("title"),
            "agenda": called.get("agenda"),
            "seq": called.get("seq", 0),
            "next": f"room_join with room=\"{called.get('room')}\" and "
                    f"name=\"{name}\", then room_history, then loop on room_wait. "
                    f"You arrive MUTED — read and wait for the chair to unmute "
                    f"you; a refused room_post is not an error.",
        })

    def _room_list(self, args: dict) -> dict:
        rooms = self.hub.listing()
        if not rooms:
            return _text("No rooms yet. The human chair creates them.")
        return _text({"rooms": rooms})

    def _room_join(self, args: dict) -> dict:
        room = self._resolve(args)
        name = str(args.get("name") or "").strip()
        if not name:
            return _err("name is required — it is how you appear in the room")
        room.join(name, role=str(args.get("role") or AGENT),
                  provider=str(args.get("provider") or ""))
        snap = room.snapshot(limit=50)
        return _text({
            "joined": room.id,
            "title": room.title,
            "agenda": room.agenda,
            "seq": snap["seq"],
            "participants": [p["name"] for p in snap["participants"]],
            "next": "Call room_history for what was said before you arrived, then "
                    "loop on room_wait with the seq above.",
        })

    def _room_post(self, args: dict) -> dict:
        room = self._resolve(args)
        name = str(args.get("name") or "")
        text = str(args.get("text") or "")
        if not text.strip():
            return _err("text is empty")
        try:
            ev = room.post(name, text, kind=MESSAGE, role=AGENT)
        except Muted as exc:
            return _err(str(exc))
        except RoomClosed as exc:
            return _err(str(exc))
        return _text({"posted": ev.seq})

    def _room_wait(self, args: dict) -> dict:
        room = self._resolve(args)
        name = str(args.get("name") or "")
        room.touch(name)
        since = int(args.get("since") or 0)
        timeout = min(float(args.get("timeout") or MAX_WAIT), MAX_WAIT)
        events = room.wait_for(since, timeout)
        room.touch(name)
        return _text({
            "room": room.id,
            "closed": room.closed,
            "seq": events[-1].seq if events else since,
            "events": [{"seq": e.seq, "author": e.author, "kind": e.kind,
                        "text": e.text} for e in events
                       if e.author != name],
            "note": "Empty means nobody spoke in the window. Call room_wait again."
                    if not events else "",
        })

    def _room_history(self, args: dict) -> dict:
        room = self._resolve(args)
        limit = args.get("limit")
        snap = room.snapshot(limit=int(limit) if limit else None)
        return _text({"title": snap["title"], "agenda": snap["agenda"],
                      "seq": snap["seq"], "closed": snap["closed"],
                      "events": [{"seq": e["seq"], "author": e["author"],
                                  "kind": e["kind"], "text": e["text"]}
                                 for e in snap["events"]]})

    def _room_note(self, args: dict) -> dict:
        room = self._resolve(args)
        ev = room.post(str(args.get("name") or ""), str(args.get("text") or ""),
                       kind=NOTE, role=AGENT)
        return _text({"note": ev.seq})

    def _room_summarize(self, args: dict) -> dict:
        room = self._resolve(args)
        ev = room.post(str(args.get("name") or ""), str(args.get("text") or ""),
                       kind=SUMMARY, role=AGENT)
        return _text({"summary": ev.seq})

    def _room_leave(self, args: dict) -> dict:
        room = self._resolve(args)
        room.leave(str(args.get("name") or ""))
        return _text({"left": room.id})
