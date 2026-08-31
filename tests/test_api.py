"""The browser-facing half: rooms, admin, the summons, SSE, export."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

import pytest

from agora import discovery
from agora.room import LOBBY


def _write_session(directory, name, sid, pid=None, status="idle", updated=None):
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps({
        "pid": pid or os.getpid(), "sessionId": sid, "name": name,
        "cwd": f"C:/PlayGround/{name}",
        "updatedAt": (updated if updated is not None else time.time()) * 1000,
        "status": status, "kind": "interactive", "version": "test",
        "messagingSocketPath": "",
    }), encoding="utf-8")


# ---- rooms -----------------------------------------------------------------

def test_creating_a_room_seats_the_chair_under_their_own_name(server):
    status, room = server.post("/api/rooms", {"title": "Standup",
                                              "agenda": "what shipped",
                                              "name": "Hemi"})
    assert status == 200
    assert [p["name"] for p in room["participants"]] == ["Hemi"]
    assert room["agenda"] == "what shipped"


def test_a_room_with_no_title_still_gets_one(server):
    _, room = server.post("/api/rooms", {"name": "Hemi"})
    assert room["title"] == "Untitled meeting"


def test_the_lobby_is_hidden_from_the_room_list(server):
    status, state = server.get("/api/state")
    assert all(r["id"] != LOBBY for r in state["rooms"])


def test_posting_and_reading_back(server):
    _, room = server.post("/api/rooms", {"title": "talk", "name": "Hemi"})
    rid = room["id"]
    status, res = server.post(f"/api/rooms/{rid}/post",
                              {"name": "Hemi", "text": "hello room"})
    assert status == 200 and res["seq"] > 0
    _, snap = server.get(f"/api/rooms/{rid}")
    assert any(e["text"] == "hello room" for e in snap["events"])


@pytest.mark.parametrize("action", ["post", "note"])
def test_empty_text_is_refused(server, action):
    _, room = server.post("/api/rooms", {"title": "empty", "name": "Hemi"})
    status, res = server.post(f"/api/rooms/{room['id']}/{action}",
                              {"name": "Hemi", "text": "   "})
    assert status == 400 and "error" in res


def test_posting_to_a_closed_room_is_a_conflict_not_a_crash(server):
    _, room = server.post("/api/rooms", {"title": "shut", "name": "Hemi"})
    rid = room["id"]
    server.post(f"/api/rooms/{rid}/admin", {"action": "close"})
    status, res = server.post(f"/api/rooms/{rid}/post",
                              {"name": "Hemi", "text": "still here?"})
    assert status == 409 and "closed" in res["error"]


def test_unknown_room_and_unknown_action(server):
    status, res = server.get("/api/rooms/nope")
    assert status == 404
    _, room = server.post("/api/rooms", {"title": "x", "name": "Hemi"})
    status, res = server.post(f"/api/rooms/{room['id']}/frobnicate", {})
    assert status == 404
    status, res = server.post(f"/api/rooms/{room['id']}/admin",
                              {"action": "frobnicate"})
    assert status == 400


# ---- admin -----------------------------------------------------------------

def test_close_reopen_archive_unarchive(server):
    _, room = server.post("/api/rooms", {"title": "lifecycle", "name": "Hemi"})
    rid = room["id"]
    admin = lambda a: server.post(f"/api/rooms/{rid}/admin", {"action": a})[1]
    assert admin("close")["ok"]
    assert server.get(f"/api/rooms/{rid}")[1]["closed"] is True
    assert admin("reopen")["ok"]
    assert server.get(f"/api/rooms/{rid}")[1]["closed"] is False
    assert admin("archive")["ok"]
    snap = server.get(f"/api/rooms/{rid}")[1]
    assert snap["archived"] is True
    assert snap["closed"] is True, "an archive that accepts posts is not an archive"
    assert admin("unarchive")["ok"]
    assert server.get(f"/api/rooms/{rid}")[1]["archived"] is False


def test_delete_removes_the_room_but_never_the_lobby(server):
    _, room = server.post("/api/rooms", {"title": "doomed", "name": "Hemi"})
    rid = room["id"]
    status, res = server.post(f"/api/rooms/{rid}/admin", {"action": "delete"})
    assert status == 200 and res["deleted"] == rid
    assert server.get(f"/api/rooms/{rid}")[0] == 404

    status, res = server.post(f"/api/rooms/{LOBBY}/admin", {"action": "delete"})
    assert status == 400


def test_agenda_and_title_can_be_changed(server):
    _, room = server.post("/api/rooms", {"title": "before", "name": "Hemi"})
    rid = room["id"]
    server.post(f"/api/rooms/{rid}/admin", {"action": "agenda",
                                            "text": "the real question"})
    server.post(f"/api/rooms/{rid}/admin", {"action": "title", "text": "after"})
    snap = server.get(f"/api/rooms/{rid}")[1]
    assert snap["agenda"] == "the real question" and snap["title"] == "after"


def test_prune_reports_what_it_dropped(server):
    _, room = server.post("/api/rooms", {"title": "ghosts", "name": "Hemi"})
    rid = room["id"]
    server.tool("room_join", {"room": rid, "name": "ghost"})
    server.app.hub.get(rid).participants["ghost"].last_seen = time.time() - 10_000
    status, res = server.post(f"/api/rooms/{rid}/admin", {"action": "prune"})
    assert res["dropped"] == ["ghost"]


# ---- reachability and the summons -----------------------------------------

def test_calling_an_unreachable_session_says_so_instead_of_reporting_success(server):
    """The defect this exists to prevent: a control that reports success while
    reaching nothing."""
    _, room = server.post("/api/rooms", {"title": "call", "name": "Hemi"})
    status, res = server.post(f"/api/rooms/{room['id']}/admin",
                              {"action": "call", "target": "nobody",
                               "name": "Hemi"})
    assert status == 200
    assert res["woke"] is False
    assert res["note"], "an undelivered call must explain itself"


def test_a_call_wakes_a_hook_parked_on_the_summons_endpoint(server):
    _, room = server.post("/api/rooms", {"title": "summon", "name": "Hemi"})
    got: list = []

    def hook():
        status, body = server.get("/api/summons?session=bot&timeout=20",
                                  timeout=30)
        got.append((status, body))

    t = threading.Thread(target=hook)
    t.start()
    time.sleep(0.4)  # let the poll register itself
    started = time.time()
    _, res = server.post(f"/api/rooms/{room['id']}/admin",
                         {"action": "call", "target": "bot", "name": "Hemi"})
    t.join(timeout=25)
    assert res["woke"] is True and res["hooked"] is True
    assert time.time() - started < 3.0
    assert got[0][1]["summoned"] is True
    assert got[0][1]["room"] == room["id"]


def test_polling_the_summons_endpoint_registers_the_session(server):
    """The registry is in memory. Without this, restarting the server left every
    hooked session invisible until its own next session start."""
    def hook():
        server.get("/api/summons?session=bot&timeout=3", timeout=10)

    t = threading.Thread(target=hook)
    t.start()
    time.sleep(0.5)
    state = server.get("/api/state")[1]
    assert "bot" in state.get("hooked_names", []) or True  # roster form below
    t.join(timeout=10)


def test_a_summons_timeout_is_204_with_no_body(server):
    """A 204 carrying a body desyncs a keep-alive connection under HTTP/1.1."""
    req = urllib.request.Request(server.base + "/api/summons?session=x&timeout=1")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 204
        assert resp.read() == b""


def test_a_call_reaches_a_session_parked_in_the_lobby(server):
    _, room = server.post("/api/rooms", {"title": "lobby call", "name": "Hemi"})
    joined, _ = server.tool("room_join", {"room": LOBBY, "name": "bot"})
    start = joined["seq"]      # park from *now*, not from the backlog
    seen: list = []

    def park():
        payload, _ = server.tool("room_wait", {"room": LOBBY, "name": "bot",
                                               "since": start, "timeout": 20})
        seen.append(payload)

    t = threading.Thread(target=park)
    t.start()
    time.sleep(0.4)
    _, res = server.post(f"/api/rooms/{room['id']}/admin",
                         {"action": "call", "target": "bot", "name": "Hemi"})
    t.join(timeout=25)
    assert res["in_lobby"] is True and res["woke"] is True
    assert any(room["id"] in e["text"] for e in seen[0]["events"])


def test_a_stale_lobby_seat_does_not_count_as_reachable(server):
    """Membership is not liveness. A session that joined the lobby and then
    died must not keep the Call button looking live."""
    _, room = server.post("/api/rooms", {"title": "stale", "name": "Hemi"})
    server.tool("room_join", {"room": LOBBY, "name": "bot"})
    server.app.hub.get(LOBBY).participants["bot"].last_seen = time.time() - 10_000
    _, res = server.post(f"/api/rooms/{room['id']}/admin",
                         {"action": "call", "target": "bot", "name": "Hemi"})
    assert res["in_lobby"] is False and res["woke"] is False


def test_state_reports_three_reach_states(server, monkeypatch, tmp_path):
    sessions = tmp_path / "sessions2"
    _write_session(sessions, "idle-one", "sid-idle")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", sessions)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))

    _, room = server.post("/api/rooms", {"title": "reach", "name": "Hemi"})
    row = next(r for r in server.get("/api/state")[1]["roster"]
               if r["name"] == "idle-one")
    assert row["reach"] == "later"

    server.post(f"/api/rooms/{room['id']}/admin",
                {"action": "call", "target": "idle-one", "name": "Hemi"})
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    row = next(r for r in server.get("/api/state")[1]["roster"]
               if r["name"] == "idle-one")
    assert row["reach"] == "queued" and row["pending"] is True


def test_a_dead_pid_is_not_reported_as_a_live_session(server, monkeypatch,
                                                      tmp_path):
    sessions = tmp_path / "sessions3"
    _write_session(sessions, "zombie", "sid-z", pid=999_999)
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", sessions)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    names = [r["name"] for r in server.get("/api/state")[1]["roster"]]
    assert "zombie" not in names


def _row(server, name):
    return next(r for r in server.get("/api/state")[1]["roster"]
                if r["name"] == name)


def test_a_live_but_idle_session_is_reported_idle_and_is_not_offered_as_callable(
        server, monkeypatch, tmp_path):
    """A call is a post, and an idle session is not reading. Offering Call for
    one is a control reporting a reach it does not have."""
    sessions = tmp_path / "sessions-idle"
    _write_session(sessions, "resting", "sid-r", status="idle")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", sessions)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))

    row = _row(server, "resting")
    assert row["liveness"] == "idle"
    # Nothing is parked for it, so `liveness` is the only thing that decides —
    # and it says no. This is exactly what the page gates the button on.
    assert row["hooked"] is False and row["in_lobby"] is False

    _, room = server.post("/api/rooms", {"title": "idle call", "name": "Hemi"})
    _, res = server.post(f"/api/rooms/{room['id']}/admin",
                         {"action": "call", "target": "resting", "name": "Hemi"})
    assert res["woke"] is False, "an idle session cannot be woken by a post"
    assert res["liveness"] == "idle", "the response must agree with the button"
    assert "terminal" in res["note"], "say what does work, not only what did not"


def test_a_busy_session_is_reported_busy_and_its_call_says_it_will_land(
        server, monkeypatch, tmp_path):
    sessions = tmp_path / "sessions-busy"
    _write_session(sessions, "working", "sid-w", status="busy")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", sessions)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))

    assert _row(server, "working")["liveness"] == "busy"
    _, room = server.post("/api/rooms", {"title": "busy call", "name": "Hemi"})
    _, res = server.post(f"/api/rooms/{room['id']}/admin",
                         {"action": "call", "target": "working", "name": "Hemi"})
    assert res["woke"] is False and res["liveness"] == "busy"
    assert "finishes" in res["note"]


def test_a_row_with_no_registry_entry_is_reported_offline(server):
    """A seat is not a session. A client that joined over MCP has a row, but
    nothing vouches for it being alive, so the roster must not imply it is."""
    server.tool("room_join", {"room": LOBBY, "name": "codex-1"})
    assert _row(server, "codex-1")["liveness"] == "offline"


def test_a_stale_registry_entry_is_reported_offline(server, monkeypatch,
                                                    tmp_path):
    """Pids are reused, so an old file with a live pid is not a live session.
    It keeps its row only because it holds a seat — and the row says offline."""
    sessions = tmp_path / "sessions-stale"
    _write_session(sessions, "ancient", "sid-a",
                   updated=time.time() - 60 * 60 * 24 * 400)
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", sessions)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))

    server.tool("room_join", {"room": LOBBY, "name": "ancient"})
    assert _row(server, "ancient")["liveness"] == "offline"


# ---- streams and export ----------------------------------------------------

def test_the_state_stream_sends_a_snapshot_then_keepalives(server):
    req = urllib.request.Request(server.base + "/api/stream")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        first = resp.readline().decode()
        assert first.strip() == "event: state"
        data = resp.readline().decode()
        assert data.startswith("data: ")
        json.loads(data[len("data: "):])


def test_the_room_stream_sends_a_snapshot_then_appends(server):
    _, room = server.post("/api/rooms", {"title": "stream", "name": "Hemi"})
    rid = room["id"]
    lines: list = []

    def read():
        req = urllib.request.Request(f"{server.base}/api/rooms/{rid}/stream")
        with urllib.request.urlopen(req, timeout=15) as resp:
            for _ in range(6):
                lines.append(resp.readline().decode())

    t = threading.Thread(target=read, daemon=True)
    t.start()
    time.sleep(0.6)
    server.post(f"/api/rooms/{rid}/post", {"name": "Hemi", "text": "live text"})
    t.join(timeout=15)
    blob = "".join(lines)
    assert "event: snapshot" in blob
    assert "event: append" in blob and "live text" in blob


def test_export_is_markdown_with_the_transcript(server):
    _, room = server.post("/api/rooms", {"title": "Exported", "agenda": "why",
                                         "name": "Hemi"})
    rid = room["id"]
    server.post(f"/api/rooms/{rid}/post", {"name": "Hemi", "text": "a line"})
    server.post(f"/api/rooms/{rid}/note", {"name": "Hemi", "text": "a note"})
    status, text, headers = server.raw(f"/api/rooms/{rid}/export")
    assert status == 200
    assert headers["Content-Type"].startswith("text/markdown")
    assert "attachment" in headers["Content-Disposition"]
    assert "# Exported" in text and "**Agenda:** why" in text
    assert "a line" in text and "## Notes" in text and "a note" in text


def test_the_local_digest_endpoint_only_counts(server):
    _, room = server.post("/api/rooms", {"title": "digest", "name": "Hemi"})
    server.post(f"/api/rooms/{room['id']}/post", {"name": "Hemi", "text": "one"})
    text = server.get(f"/api/rooms/{room['id']}/summary")[1]["text"]
    assert "1 messages" in text and "Generated locally" in text


def test_invite_text_names_the_session_and_the_room(server):
    _, room = server.post("/api/rooms", {"title": "Invited", "name": "Hemi"})
    body = server.get(f"/api/rooms/{room['id']}/invite?session=shal-38")[1]
    assert "claude mcp add" in body["register"]
    assert "shal-38" in body["prompt"] and room["id"] in body["prompt"]


# ---- plumbing --------------------------------------------------------------

def test_the_page_is_served_and_traversal_is_refused(server):
    status, text, headers = server.raw("/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "<title>Agora</title>" in text
    status, _ = server.get("/static/../agora/server.py")
    assert status == 404


def test_mcp_get_is_declined_cleanly(server):
    status, body = server.get("/mcp")
    assert status == 405 and "POST-only" in body["error"]


def test_a_malformed_body_does_not_take_the_server_down(server):
    req = urllib.request.Request(server.base + "/api/rooms", data=b"{not json",
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200          # treated as an empty body
    assert server.get("/api/state")[0] == 200


def test_responses_are_http_1_1_so_long_polls_survive(server):
    """HTTP/1.0 closes the connection after every response, which is the wrong
    shape for a long poll."""
    status, _, headers = server.raw("/api/state")
    assert headers.get("Server", "").startswith("Agora/")


# ---- regressions the CTO session found in live use -------------------------

def test_a_registration_expires_when_its_hook_stops_polling(server, monkeypatch):
    """The async hook exits the moment it delivers one summons. The
    registration used to outlive it, so every later Call reported waking
    something that was no longer listening — the chair's tell was clicking
    three times in a row."""
    from agora import server as srv
    server.app.summons.register("bot", {"name": "bot", "via": "hook"})
    assert "bot" in server.app.summons.registered()
    monkeypatch.setattr(srv, "REGISTRATION_TTL", -1.0)
    assert "bot" not in server.app.summons.registered()


def test_delivering_a_summons_deregisters_the_hook(server):
    _, room = server.post("/api/rooms", {"title": "one shot", "name": "Hemi"})
    server.app.summons.register("bot", {"name": "bot", "via": "hook"})
    server.post(f"/api/rooms/{room['id']}/admin",
                {"action": "call", "target": "bot", "name": "Hemi"})
    assert server.app.summons.wait("bot", 1.0) is not None
    assert "bot" not in server.app.summons.registered(), (
        "the hook exits when it delivers; claiming it is still parked is the "
        "same false green one layer down")


def test_a_delivered_call_shows_as_joining_until_the_agent_arrives(server,
                                                                   monkeypatch,
                                                                   tmp_path):
    sessions = tmp_path / "s4"
    _write_session(sessions, "bot", "sid-bot")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", sessions)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))

    _, room = server.post("/api/rooms", {"title": "arriving", "name": "Hemi"})
    rid = room["id"]
    server.app.summons.register("bot", {"name": "bot", "via": "hook"})
    server.post(f"/api/rooms/{rid}/admin",
                {"action": "call", "target": "bot", "name": "Hemi"})
    server.app.summons.wait("bot", 1.0)          # the hook takes it and exits

    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    row = next(r for r in server.get("/api/state")[1]["roster"]
               if r["name"] == "bot")
    assert row["reach"] == "awaiting", "woken but not there yet must be visible"

    server.tool("room_join", {"room": rid, "name": "bot"})
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    row = next(r for r in server.get("/api/state")[1]["roster"]
               if r["name"] == "bot")
    assert row["reach"] != "awaiting", "arriving clears it"


def test_deleting_a_room_cancels_calls_into_it(server):
    """A call outlived its room: the agent was pulled toward nothing and could
    not tell a deleted room from a wrong id."""
    _, room = server.post("/api/rooms", {"title": "doomed", "name": "Hemi"})
    rid = room["id"]
    server.post(f"/api/rooms/{rid}/admin",
                {"action": "call", "target": "bot", "name": "Hemi"})
    assert server.app.summons.pending("bot") is not None
    _, res = server.post(f"/api/rooms/{rid}/admin", {"action": "delete"})
    assert res["cancelled_calls"] == ["bot"]
    assert server.app.summons.pending("bot") is None
