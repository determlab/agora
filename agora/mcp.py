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

from .discovery import canonical_name
from .room import (AGENT, LOBBY, MESSAGE, NOTE, SUMMARY, Hub, Muted,
                   NotSeated, RoomClosed, mention_note)

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

#: `room_wait` on every room at once. An argument value, not a new tool: a client
#: fetches `tools/list` once at connect, so a tool added later is invisible to
#: every session already running — which is always the sessions that needed it.
#: An unrecognised argument *value* reaches them untouched.
ANY_ROOM = "*"


def _text(payload: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload,
                                                               ensure_ascii=False,
                                                               indent=2)
    return {"content": [{"type": "text", "text": body}]}


def _cursors(value: Any) -> dict[str, int]:
    """The room→seq map a `*` wait resumes from, tolerant of what a model sends.

    Anything that is not a map — missing, 0, a bare seq copied from a
    single-room wait — means "start from now" rather than an error, because the
    alternative is replaying every room's whole transcript at an agent.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for room_id, seq in value.items():
        try:
            out[str(room_id)] = int(seq)
        except (TypeError, ValueError):
            continue
    return out


def _event_out(ev: Any, name: str, room_id: str = "") -> dict[str, Any]:
    """One event as an agent sees it, marked when it is addressed to that agent.

    "Somebody said something" and "somebody asked me" cannot be told apart from
    the text alone, and an agent that cannot tell them apart either answers
    everything or answers nothing. The flag is only present when it is true, so
    a session whose cached schema predates it reads the reply exactly as before.
    """
    out: dict[str, Any] = {"seq": ev.seq, "author": ev.author,
                           "kind": ev.kind, "text": ev.text}
    if room_id:
        out = {"room": room_id, **out}
    if ev.mentions:
        out["mentions"] = list(ev.mentions)
        if name and name in ev.mentions:
            out["mentions_you"] = True
    return out


#: Said once in a reply that carries a mention, rather than per event.
MENTION_HINT = ("An event marked `mentions_you` names you — answer those first, "
                "with room_post.")

#: Rides on every reply that writes something. The tool descriptions say the same
#: thing, but a description is fetched once at connect (D1), so a session already
#: parked holds the schema it started with and will never read the warning there.
#: The reply is the only channel that reaches it.
#:
#: Room 23c152bd: a `room_wait` returned through seq 23, `"CMO joined — muted"`
#: landed at 24, `room_post` returned 25, and 25 became the next cursor. Seq 24
#: was never delivered and the session reported the CMO woken. No number a write
#: can offer fixes that — the room's tip at post time *is* the post's own seq, so
#: `tip` would hand back the same 25. The honest answer is that this is not a
#: position to read from, said where the caller is looking.
CURSOR_NOTE = ("unchanged — the seq above is where your message landed, not "
               "where you are reading from. Only room_wait moves your read "
               "position. Anything said between your last room_wait and this "
               "write sits below this number and you have not seen it.")


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
                "session_id": {"type": "string",
                               "description": "ALWAYS send this. Your Claude Code "
                                              "session id. It is what ties you to "
                                              "the chair's roster; without it a "
                                              "name you chose can fork your "
                                              "identity and the Call button will "
                                              "reach a seat you are not in."},
            },
            "required": ["room", "name"],
        },
    },
    {
        "name": "room_post",
        "description": "Say something in the room. Everyone sees it, including the "
                       "human chairing the meeting. Write @name, exactly as that "
                       "participant appears in the room, to address someone: the "
                       "reply tells you which of them were listening, because a "
                       "post does not wake a session that is not reading. The "
                       "seq it returns is your message's position in the room, "
                       "NOT your read cursor: only room_wait advances that, and "
                       "reusing this number skips whatever was said between "
                       "your last room_wait and this post.",
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
                       "USE room=\"*\" AS YOUR RESTING STATE: it waits on every "
                       "room you are in and the lobby at once, so one call keeps "
                       "you reachable in every meeting and hears the chair calling "
                       "you into a new one. An event marked `mentions_you` names "
                       "you — answer those before anything else in the batch. "
                       "With \"*\" the reply carries a "
                       "`cursors` map instead of a single seq — pass it straight "
                       "back as `cursors` next call and you never re-read anything. "
                       "Whenever you have nothing else to do, call this again with "
                       "room=\"*\"; an agent that stops calling it goes dark. "
                       "Leave timeout unset unless you have a reason.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "room": {"type": "string",
                         "description": "Room id or exact title, or \"*\" for "
                                        "every room you are in plus the lobby"},
                "name": {"type": "string"},
                "since": {"type": "integer",
                          "description": "Highest seq you already have. 0 for "
                                         "everything since you joined. Ignored "
                                         "when room is \"*\" — seqs are per room, "
                                         "so use `cursors` there."},
                "cursors": {"type": "object",
                            "description": "room=\"*\" only: the `cursors` map "
                                           "from your last reply, echoed back. "
                                           "Omit it on the first call and the "
                                           "wait starts from now, which skips "
                                           "anything said between joining a room "
                                           "and this call — to keep that gap, "
                                           "seed the map with each room's `seq` "
                                           "from `room_history` instead."},
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
                       "action item, or something the chair should not lose. The "
                       "seq it returns is the note's position, NOT your read "
                       "cursor: only room_wait advances that.",
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
                       "and pinned separately from ordinary messages. The seq it "
                       "returns is the summary's position, NOT your read cursor: "
                       "only room_wait advances that.",
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
        # The summons registry, shared with the web side. Used to report
        # reachability; the SessionStart hook is what actually parks on it.
        # `agora_standby` used to live here too and was removed: a tool added to
        # a running server is invisible to every session already connected, so
        # it could never have worked for the sessions that needed it. The Lobby
        # does the same job with `room_join`/`room_wait`, which everyone has.
        self.summons = summons
        self._tools: dict[str, Callable[[dict], dict]] = {
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
                        "room_post. Stay until the room closes, and whenever you "
                        "have nothing else to do rest on room_wait with "
                        "room=\"*\" — that covers every room you are in plus the "
                        "lobby, and is how the chair reaches you.",
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
                try:
                    result = fn(params.get("arguments") or {})
                except LookupError as exc:
                    # A caller mistake, not a server fault. Returning it as tool
                    # content lets the agent read it and correct itself; a
                    # JSON-RPC error is opaque to the model and reads as a crash.
                    result = _err(str(exc))
            else:
                return self._error(rpc_id, -32601, f"unknown method {method!r}")
        except Exception as exc:  # a genuine fault must not kill the room
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

    def _room_list(self, args: dict) -> dict:
        # The Lobby is not a meeting. Listing it alongside real rooms invites an
        # agent told to "join the meeting" to sit down in the waiting room.
        rooms = [r for r in self.hub.listing() if r["id"] != LOBBY]
        if not rooms:
            return _text("No rooms yet — the chair creates them. If you have "
                         "nothing else to do, room_join the \"lobby\" and "
                         "room_wait with room=\"*\"; the chair calls you from it.")
        return _text({"rooms": rooms,
                      "lobby": "room_join \"lobby\", then rest on room_wait with "
                               "room=\"*\" — one call covers the lobby and every "
                               "meeting you are in, and that is how the chair "
                               "reaches you."})

    def _room_join(self, args: dict) -> dict:
        room = self._resolve(args)
        name = str(args.get("name") or "").strip()
        if not name:
            return _err("name is required — it is how you appear in the room")
        # If a session id is supplied and it is a live Claude Code session, the
        # registry's name wins over whatever the caller passed. The chair's
        # roster is built from that registry, so a self-chosen name forks the
        # identity: two seats, one of which the Call button can never reach.
        session_id = str(args.get("session_id") or "")
        canonical = canonical_name(session_id)
        role = str(args.get("role") or AGENT)
        if canonical and canonical != name:
            role = name if role == AGENT else role  # keep the chosen name as a label
            name = canonical
        room.join(name, role=role,
                  provider=str(args.get("provider") or ""),
                  session_id=session_id)
        snap = room.snapshot(limit=50)
        return _text({
            "joined": room.id,
            "title": room.title,
            "agenda": room.agenda,
            "seq": snap["seq"],
            "participants": [p["name"] for p in snap["participants"]],
            # Read on every join, so it names the wildcard resting state but
            # stays one sentence longer than the old line, not a paragraph.
            "next": f"Call room_history for what was said before you arrived, "
                    f"then loop on room_wait. When idle, rest on room_wait "
                    f"with room=\"{ANY_ROOM}\" and "
                    f"cursors={{\"{room.id}\": {snap['seq']}}} — one call "
                    f"covers every room you are in plus the lobby.",
        })

    def _room_post(self, args: dict) -> dict:
        room = self._resolve(args)
        name = str(args.get("name") or "")
        text = str(args.get("text") or "")
        if not text.strip():
            return _err("text is empty")
        try:
            ev = room.post(name, text, kind=MESSAGE, role=AGENT)
        except (Muted, NotSeated, RoomClosed) as exc:
            return _err(str(exc))
        # A mention is a post, and a post is not a wake: say which of the seats
        # you named were actually listening rather than implying all of them.
        report = room.mention_report(ev.mentions)
        out: dict[str, Any] = {"posted": ev.seq}
        if report:
            out["mentions"] = report
            note = mention_note(report)
            if note:
                out["note"] = note
        # Last, so it never pushes the mention report down: who heard you is the
        # more urgent half of this reply.
        out["cursor"] = CURSOR_NOTE
        return _text(out)

    def _room_wait(self, args: dict) -> dict:
        name = str(args.get("name") or "")
        timeout = min(float(args.get("timeout") or MAX_WAIT), MAX_WAIT)
        if str(args.get("room") or "").strip() == ANY_ROOM:
            return self._room_wait_any(args, name, timeout)
        room = self._resolve(args)
        room.touch(name)
        since = int(args.get("since") or 0)
        events = room.wait_for(since, timeout)
        room.touch(name)
        out = [_event_out(e, name) for e in events if e.author != name]
        return _text({
            "room": room.id,
            "closed": room.closed,
            "seq": events[-1].seq if events else since,
            "events": out,
            "note": "Empty means nobody spoke in the window. Call room_wait again."
                    if not events else
                    MENTION_HINT if any(e.get("mentions_you") for e in out) else "",
        })

    def _room_wait_any(self, args: dict, name: str, timeout: float) -> dict:
        """`room_wait` with room="*" — one parked call for every meeting at once.

        The cursor is a map, because sequence numbers are per room and a single
        integer cannot address several of them. It is read from `cursors`, and
        from `since` only when that is a map too: a session whose cached schema
        predates this still sends `cursors` through untouched, whereas `since`
        was already declared an integer there.
        """
        cursors = args.get("cursors")
        if not isinstance(cursors, dict):
            cursors = args.get("since")
        events, cursors = self.hub.wait_any(name, _cursors(cursors), timeout)
        out = [_event_out(e, name, rid) for rid, e in events]
        return _text({
            "room": ANY_ROOM,
            "watching": sorted(cursors),
            "cursors": cursors,
            "events": out,
            "next": "Pass `cursors` back as `cursors` and call room_wait with "
                    "room=\"*\" again — that is your resting state, and it is "
                    "what keeps you reachable in every room at once.",
            "note": "Empty means nobody spoke in any of your rooms in the "
                    "window. Call room_wait again." if not events else
                    MENTION_HINT if any(e.get("mentions_you") for e in out) else "",
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
        return _text({"note": ev.seq, "cursor": CURSOR_NOTE})

    def _room_summarize(self, args: dict) -> dict:
        room = self._resolve(args)
        ev = room.post(str(args.get("name") or ""), str(args.get("text") or ""),
                       kind=SUMMARY, role=AGENT)
        return _text({"summary": ev.seq, "cursor": CURSOR_NOTE})

    def _room_leave(self, args: dict) -> dict:
        room = self._resolve(args)
        room.leave(str(args.get("name") or ""))
        return _text({"left": room.id})
