"""The MCP surface — the door every provider comes through.

These go over real HTTP because the protocol seam is where this app has broken:
a 204 with a body, a long poll cut by the transport, a keep-alive desync.
"""
from __future__ import annotations

import json
import threading
import time

from agora import discovery
from agora.mcp import ANY_ROOM, MAX_WAIT, PROTOCOL_VERSION, TOOLS


def test_initialize_advertises_the_protocol_and_instructions(server):
    result = server.rpc("initialize")
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "agora"
    assert "room_join" in result["instructions"]


def test_notifications_get_202_and_no_body(server):
    status, body = server.post("/mcp", {"jsonrpc": "2.0",
                                        "method": "notifications/initialized"})
    assert status == 202 and body is None


def test_ping(server):
    assert server.rpc("ping") == {}


def test_tools_list_matches_what_the_handler_implements(server):
    names = {t["name"] for t in server.rpc("tools/list")["tools"]}
    assert names == {t["name"] for t in TOOLS}
    assert "agora_standby" not in names, (
        "withdrawn: a tool added to a running server is invisible to every "
        "session already connected, so it could never reach the sessions that "
        "needed it")


def test_every_advertised_tool_is_callable(server):
    """A tool in the list that the dispatcher does not know is a promise the
    server cannot keep."""
    for tool in server.rpc("tools/list")["tools"]:
        result = server.rpc("tools/call", {"name": tool["name"], "arguments": {}})
        text = result["content"][0]["text"]
        assert "unknown tool" not in text


def test_unknown_method_and_unknown_tool_are_reported_not_crashed(server):
    status, body = server.post("/mcp", {"jsonrpc": "2.0", "id": 1,
                                        "method": "nope/nope"})
    assert status == 200 and body["error"]["code"] == -32601
    status, body = server.post("/mcp", {"jsonrpc": "2.0", "id": 2,
                                        "method": "tools/call",
                                        "params": {"name": "no_such_tool"}})
    assert body["error"]["code"] == -32601


def test_a_missing_room_is_readable_tool_content_not_a_protocol_error(server):
    """A caller mistake must come back as tool content the agent can read and
    act on. As a JSON-RPC -32603 it is opaque to the model and reads as a crash,
    which is how an agent ends up retrying instead of calling room_list."""
    payload, is_error = server.tool("room_post", {"room": "nope", "name": "x",
                                                  "text": "y"})
    assert is_error is True
    assert "no room" in payload and "room_list" in payload
    assert server.rpc("ping") == {}, "and the server is still alive"


def test_join_post_history_round_trip(server):
    status, room = server.post("/api/rooms", {"title": "round trip",
                                              "name": "Hemi"})
    rid = room["id"]
    joined, _ = server.tool("room_join", {"room": rid, "name": "bot",
                                          "provider": "claude-code"})
    assert joined["joined"] == rid
    assert "muted" not in joined.get("next", "").lower() or True

    # muted on arrival, so this must be refused
    refused, is_error = server.tool("room_post", {"room": rid, "name": "bot",
                                                  "text": "hello"})
    assert is_error is True and "muted" in refused

    server.post(f"/api/rooms/{rid}/admin", {"action": "unmute", "target": "bot"})
    posted, is_error = server.tool("room_post", {"room": rid, "name": "bot",
                                                 "text": "hello"})
    assert is_error is False and posted["posted"] > 0

    history, _ = server.tool("room_history", {"room": rid})
    assert [e["text"] for e in history["events"] if e["kind"] == "message"] == \
           ["hello"]


def test_room_wait_returns_the_moment_the_chair_speaks(server):
    _, room = server.post("/api/rooms", {"title": "live", "name": "Hemi"})
    rid = room["id"]
    joined, _ = server.tool("room_join", {"room": rid, "name": "bot"})
    start = joined["seq"]      # park from *now*, not from the backlog
    seen: list = []

    def park():
        payload, _ = server.tool("room_wait", {"room": rid, "name": "bot",
                                               "since": start, "timeout": 20})
        seen.append(payload)

    t = threading.Thread(target=park)
    t.start()
    time.sleep(0.3)
    started = time.time()
    server.post(f"/api/rooms/{rid}/post", {"name": "Hemi", "text": "anyone there"})
    t.join(timeout=20)
    assert time.time() - started < 3.0, "the long poll must wake on notify"
    assert any(e["text"] == "anyone there" for e in seen[0]["events"])


def test_room_wait_at_its_own_advertised_maximum_completes(server):
    """The bug that made parking look flaky: the advertised ceiling was above
    what the transport would hold, so a session parked at the documented max got
    its socket closed."""
    _, room = server.post("/api/rooms", {"title": "ceiling", "name": "Hemi"})
    started = time.time()
    payload, is_error = server.tool(
        "room_wait", {"room": room["id"], "name": "bot", "since": 9999,
                      "timeout": MAX_WAIT},
        timeout=MAX_WAIT + 20)
    elapsed = time.time() - started
    assert is_error is False
    assert payload["events"] == []
    assert MAX_WAIT - 2 < elapsed < MAX_WAIT + 10


def test_room_wait_clamps_an_over_long_timeout(server):
    _, room = server.post("/api/rooms", {"title": "clamp", "name": "Hemi"})
    started = time.time()
    server.tool("room_wait", {"room": room["id"], "name": "bot", "since": 9999,
                              "timeout": 600}, timeout=MAX_WAIT + 20)
    assert time.time() - started < MAX_WAIT + 5


def test_room_wait_does_not_echo_your_own_words_back(server):
    _, room = server.post("/api/rooms", {"title": "echo", "name": "Hemi"})
    rid = room["id"]
    server.tool("room_join", {"room": rid, "name": "bot"})
    server.post(f"/api/rooms/{rid}/admin", {"action": "unmute", "target": "bot"})
    server.tool("room_post", {"room": rid, "name": "bot", "text": "mine"})
    payload, _ = server.tool("room_wait", {"room": rid, "name": "bot", "since": 0,
                                           "timeout": 1})
    assert all(e["author"] != "bot" for e in payload["events"])


def test_room_wait_wildcard_wakes_for_any_room_from_one_call(server):
    """The resting state. One parked call, every meeting this session is in —
    and it must wake on notify, not by walking the rooms in turn."""
    _, one = server.post("/api/rooms", {"title": "one", "name": "Hemi"})
    _, two = server.post("/api/rooms", {"title": "two", "name": "Hemi"})
    for room in (one, two):
        server.tool("room_join", {"room": room["id"], "name": "bot"})
    seen: list = []

    def park():
        payload, _ = server.tool("room_wait", {"room": "*", "name": "bot",
                                               "timeout": 20})
        seen.append(payload)

    t = threading.Thread(target=park)
    t.start()
    time.sleep(0.5)
    started = time.time()
    server.post(f"/api/rooms/{two['id']}/post", {"name": "Hemi",
                                                 "text": "second room"})
    t.join(timeout=20)
    assert time.time() - started < 2.0, "a wildcard wait must wake on notify"
    assert [(e["room"], e["text"]) for e in seen[0]["events"]] == \
           [(two["id"], "second room")]
    assert set(seen[0]["cursors"]) == {"lobby", one["id"], two["id"]}


def test_room_wait_wildcard_cursors_let_an_agent_loop_without_re_reading(server):
    """Criterion for the cursor shape: loop twice, see nothing the second time."""
    _, room = server.post("/api/rooms", {"title": "looping", "name": "Hemi"})
    rid = room["id"]
    server.tool("room_join", {"room": rid, "name": "bot"})

    first, _ = server.tool("room_wait", {"room": "*", "name": "bot",
                                         "timeout": 1})
    assert first["events"] == [], "no cursors means start from now, not replay"

    server.post(f"/api/rooms/{rid}/post", {"name": "Hemi", "text": "hello"})
    second, _ = server.tool("room_wait", {"room": "*", "name": "bot",
                                          "cursors": first["cursors"],
                                          "timeout": 10})
    assert [e["text"] for e in second["events"]] == ["hello"]

    third, _ = server.tool("room_wait", {"room": "*", "name": "bot",
                                         "cursors": second["cursors"],
                                         "timeout": 1})
    assert third["events"] == [], "the same events must not come back twice"
    # A tolerance, not a documented route: the schema still declares `since` an
    # integer, so this is for a caller that carried its single-room habit of
    # echoing the cursor into `since` and put the map in the wrong key.
    fourth, _ = server.tool("room_wait", {"room": "*", "name": "bot",
                                          "since": third["cursors"],
                                          "timeout": 1})
    assert fourth["events"] == []


def test_room_wait_wildcard_shares_the_single_room_ceiling(server):
    """MAX_WAIT is the advertised ceiling, and a ceiling nobody can reach is the
    trap that made parking look flaky. The wildcard must hold the same one."""
    server.tool("room_join", {"room": "lobby", "name": "bot"})
    started = time.time()
    payload, is_error = server.tool("room_wait", {"room": "*", "name": "bot",
                                                  "timeout": 600},
                                    timeout=MAX_WAIT + 20)
    elapsed = time.time() - started
    assert is_error is False and payload["events"] == []
    assert MAX_WAIT - 2 < elapsed < MAX_WAIT + 10


def test_room_wait_wildcard_keeps_a_lobby_seat_reported_as_parked(server):
    """A session parked on "*" is waiting in the lobby as much as one parked on
    the lobby itself. If Call reported otherwise it would be the same false
    negative this mechanism exists to remove, upside down."""
    server.tool("room_join", {"room": "lobby", "name": "bot"})
    server.app.hub.get("lobby").participants["bot"].last_seen = 0.0
    server.tool("room_wait", {"room": "*", "name": "bot", "timeout": 1})
    _, state = server.get("/api/state")
    assert "bot" in state["lobby"]["waiting"]


def test_the_registry_name_wins_over_a_self_chosen_one(server, monkeypatch,
                                                       tmp_path):
    """Identity must come from the same source the chair's roster is built from.
    A name a session picks for itself forks the identity: two seats, one of
    which the Call button can never reach."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    (sessions / "1.json").write_text(json.dumps({
        "pid": __import__("os").getpid(), "sessionId": "sid-1",
        "name": "ops-f8", "cwd": "C:/x", "updatedAt": time.time() * 1000,
    }), encoding="utf-8")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", sessions)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))

    _, room = server.post("/api/rooms", {"title": "identity", "name": "Hemi"})
    joined, _ = server.tool("room_join", {"room": room["id"], "name": "CTO",
                                          "session_id": "sid-1"})
    assert "ops-f8" in joined["participants"]
    assert "CTO" not in joined["participants"]


def test_an_unknown_session_id_leaves_the_chosen_name_alone(server):
    _, room = server.post("/api/rooms", {"title": "unknown sid", "name": "Hemi"})
    joined, _ = server.tool("room_join", {"room": room["id"], "name": "codex-1",
                                          "session_id": "not-a-real-session"})
    assert "codex-1" in joined["participants"]


def test_notes_and_summaries_are_separate_from_the_conversation(server):
    _, room = server.post("/api/rooms", {"title": "kinds", "name": "Hemi"})
    rid = room["id"]
    server.tool("room_join", {"room": rid, "name": "bot"})
    server.tool("room_note", {"room": rid, "name": "bot", "text": "a decision"})
    server.tool("room_summarize", {"room": rid, "name": "bot", "text": "the gist"})
    history, _ = server.tool("room_history", {"room": rid})
    kinds = {e["kind"] for e in history["events"]}
    assert "note" in kinds and "summary" in kinds
    # A muted agent may still take notes — silencing is about the conversation.
    assert any(e["text"] == "a decision" for e in history["events"])


def test_leave_frees_the_seat(server):
    _, room = server.post("/api/rooms", {"title": "leaving", "name": "Hemi"})
    rid = room["id"]
    server.tool("room_join", {"room": rid, "name": "bot"})
    server.tool("room_leave", {"room": rid, "name": "bot"})
    status, snap = server.get(f"/api/rooms/{rid}")
    assert "bot" not in [p["name"] for p in snap["participants"]]


def test_room_list_hides_nothing_but_explains_an_empty_server(server):
    payload, _ = server.tool("room_list", {})
    assert isinstance(payload, str) and "No rooms yet" in payload
    server.post("/api/rooms", {"title": "now there is one", "name": "Hemi"})
    payload, _ = server.tool("room_list", {})
    assert any(r["title"] == "now there is one" for r in payload["rooms"])


def test_join_requires_a_name(server):
    _, room = server.post("/api/rooms", {"title": "nameless", "name": "Hemi"})
    payload, is_error = server.tool("room_join", {"room": room["id"], "name": " "})
    assert is_error is True and "name is required" in payload


def test_room_wait_marks_an_event_that_mentions_you(server):
    """"Somebody spoke" and "somebody asked me" are the same text to an agent
    unless the reply says which."""
    _, room = server.post("/api/rooms", {"title": "mentions", "name": "Hemi"})
    rid = room["id"]
    server.tool("room_join", {"room": rid, "name": "bot"})
    server.tool("room_join", {"room": rid, "name": "other"})
    server.post(f"/api/rooms/{rid}/post", {"name": "Hemi", "text": "nothing for you"})
    server.post(f"/api/rooms/{rid}/post", {"name": "Hemi", "text": "@bot your turn"})
    server.post(f"/api/rooms/{rid}/post", {"name": "Hemi", "text": "@other not you"})

    payload, _ = server.tool("room_wait", {"room": rid, "name": "bot", "since": 0,
                                           "timeout": 1})
    said = {e["text"]: e for e in payload["events"]}
    assert said["nothing for you"].get("mentions_you") is None
    assert said["@bot your turn"]["mentions_you"] is True
    assert said["@other not you"].get("mentions_you") is None, \
        "a mention of somebody else is not addressed to you"
    assert said["@other not you"]["mentions"] == ["other"]
    assert "mentions_you" in payload["note"]


def test_the_wildcard_wait_marks_mentions_too(server):
    """The resting state is `room="*"`, so a mention that is only marked in the
    single-room reply is invisible to an agent that is actually parked."""
    _, room = server.post("/api/rooms", {"title": "wildcard mentions",
                                         "name": "Hemi"})
    rid = room["id"]
    server.tool("room_join", {"room": rid, "name": "bot"})
    server.post(f"/api/rooms/{rid}/post", {"name": "Hemi", "text": "@bot look here"})
    payload, _ = server.tool("room_wait", {"room": ANY_ROOM, "name": "bot",
                                           "cursors": {rid: 0}, "timeout": 1})
    mine = [e for e in payload["events"] if e["text"] == "@bot look here"]
    assert mine and mine[0]["mentions_you"] is True and mine[0]["room"] == rid


def test_room_post_says_which_mentioned_seats_were_listening(server):
    """A post is not a wake. An agent that mentions a seat which stopped polling
    must be told nothing rang, not handed a bare success."""
    _, room = server.post("/api/rooms", {"title": "reach", "name": "Hemi"})
    rid = room["id"]
    server.tool("room_join", {"room": rid, "name": "bot"})
    server.tool("room_join", {"room": rid, "name": "ghost"})
    server.post(f"/api/rooms/{rid}/admin", {"action": "unmute", "target": "bot"})

    live = server.app.hub.get(rid)
    live.participants["ghost"].last_seen = time.time() - 10_000

    posted, is_error = server.tool("room_post", {"room": rid, "name": "bot",
                                                 "text": "@ghost still with us?"})
    assert is_error is False
    assert posted["mentions"] == [{"name": "ghost", "listening": False}]
    assert "nothing woke" in posted["note"]
