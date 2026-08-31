"""Reading Claude Code's session registry, and turning it into a roster."""
from __future__ import annotations

import json
import os
import time

from agora import discovery
from agora.discovery import GHOST_AFTER, canonical_name, claude_sessions, roster
from agora.room import AGENT, HUMAN, Hub


def _session(directory, name, sid="sid", pid=None, updated=None, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    rec = {"pid": pid if pid is not None else os.getpid(), "sessionId": sid,
           "name": name, "cwd": f"C:/PlayGround/{name}",
           "updatedAt": (updated if updated is not None else time.time()) * 1000,
           "status": "idle", "kind": "interactive", "version": "2.1.251",
           "messagingSocketPath": r"\\.\pipe\LOCAL\cc-msg-x"}
    rec.update(extra)
    (directory / f"{name}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def test_a_missing_registry_is_not_an_error(tmp_path):
    assert claude_sessions(tmp_path / "does-not-exist") == []


def test_a_live_session_is_reported_with_its_project(tmp_path):
    _session(tmp_path, "shal-38", sid="s1")
    [row] = claude_sessions(tmp_path)
    assert row["name"] == "shal-38"
    assert row["project"] == "shal-38"
    assert row["provider"] == "claude-code"
    assert row["session_id"] == "s1"


def test_a_dead_pid_is_dropped(tmp_path):
    _session(tmp_path, "zombie", pid=999_999)
    assert claude_sessions(tmp_path) == []


def test_an_ancient_entry_is_dropped_even_with_a_live_pid(tmp_path):
    """Pids are reused; a year-old file with a recycled pid is not a session."""
    _session(tmp_path, "ancient", updated=time.time() - 60 * 60 * 24 * 400)
    assert claude_sessions(tmp_path) == []


def test_unreadable_json_is_skipped_not_fatal(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    _session(tmp_path, "fine")
    assert [s["name"] for s in claude_sessions(tmp_path)] == ["fine"]


def test_sessions_come_back_newest_first(tmp_path):
    _session(tmp_path, "older", sid="a", updated=time.time() - 500)
    _session(tmp_path, "newer", sid="b", updated=time.time())
    assert [s["name"] for s in claude_sessions(tmp_path)] == ["newer", "older"]


def test_the_cache_makes_repeat_reads_cheap_but_a_directory_arg_bypasses_it(
        tmp_path, monkeypatch):
    _session(tmp_path, "one")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    assert len(claude_sessions()) == 1
    _session(tmp_path, "two", sid="s2")
    assert len(claude_sessions()) == 1, "within the TTL the cache should hold"
    assert len(claude_sessions(fresh=True)) == 2


def test_canonical_name_maps_a_session_id_to_the_registry_name(
        tmp_path, monkeypatch):
    _session(tmp_path, "ops-f8", sid="sid-1")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    assert canonical_name("sid-1") == "ops-f8"
    assert canonical_name("nope") == ""
    assert canonical_name("") == ""


def test_the_roster_says_which_rooms_a_session_is_in(tmp_path, monkeypatch):
    _session(tmp_path, "shal-38", sid="s1")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    hub = Hub(tmp_path / "rooms")
    room = hub.create("a meeting")
    room.join("shal-38", role=AGENT)
    [row] = [r for r in roster(hub) if r["name"] == "shal-38"]
    assert row["rooms"] == [room.id]


def test_a_non_claude_participant_gets_a_row_while_it_is_fresh(
        tmp_path, monkeypatch):
    """A Codex or Cursor client is real and belongs on the roster — but only
    while it is actually there."""
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path / "empty")
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    hub = Hub(tmp_path / "rooms")
    room = hub.create("mixed")
    room.join("codex-1", role=AGENT, provider="codex")
    assert any(r["name"] == "codex-1" for r in roster(hub))

    room.participants["codex-1"].last_seen = time.time() - GHOST_AFTER - 10
    assert not any(r["name"] == "codex-1" for r in roster(hub)), (
        "a ghost seat must not keep being offered — the chair would call a name "
        "that no longer exists")


def test_the_chair_is_not_listed_as_a_callable_session(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path / "empty")
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    hub = Hub(tmp_path / "rooms")
    hub.create("m").join("Hemi", role=HUMAN)
    assert not any(r["name"] == "Hemi" for r in roster(hub))


def test_invite_text_covers_registration_and_the_sitting_instruction(tmp_path,
                                                                     monkeypatch):
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path / "empty")
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    hub = Hub(tmp_path / "rooms")
    room = hub.create("Ship it")
    body = discovery.invite_text(room, "http://127.0.0.1:8765", "shal-38")
    assert "--scope user" in body["register"]
    assert "/mcp" in body["register"]
    assert "shal-38" in body["prompt"]
    assert "room_wait" in body["prompt"], (
        "the invitation has to make the agent STAY; a meeting is a conversation")
