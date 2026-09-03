"""Reading Claude Code's session registry, and turning it into a roster."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

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


def test_the_cache_makes_repeat_reads_cheap(tmp_path, monkeypatch):
    _session(tmp_path, "one")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    assert len(claude_sessions()) == 1
    _session(tmp_path, "two", sid="s2")
    assert len(claude_sessions()) == 1, "within the TTL the cache should hold"
    assert len(claude_sessions(fresh=True)) == 2


def _count_scans(monkeypatch):
    """Every registry scan, counted. `glob` is the one call a scan cannot skip.

    Counting the scans is the only honest way to prove a cache was read: a
    timing assertion passes on a slow machine that did all the work anyway.
    """
    scans: list[str] = []
    real = Path.glob

    def counting(self, pattern, *a, **kw):
        scans.append(str(self))
        return real(self, pattern, *a, **kw)

    monkeypatch.setattr(Path, "glob", counting)
    return scans


def test_the_registry_path_and_none_hit_the_same_cache(tmp_path, monkeypatch):
    """`claude_sessions(CLAUDE_SESSIONS)` and `claude_sessions()` are the same
    read, so they must cost the same. They did not: the read gate tested
    `directory is None` and the write gate tested the resolved path, so the
    explicit form wrote a cache it could never read. It cost a full uncached
    scan per state build — and, worse, made the correct call at the call site
    in `availability` look like a mistake worth "simplifying" away."""
    _session(tmp_path, "one")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    scans = _count_scans(monkeypatch)

    assert len(claude_sessions(tmp_path)) == 1        # scans once, fills cache
    assert len(scans) == 1
    for _ in range(4):
        claude_sessions(tmp_path)
        claude_sessions()
        claude_sessions(None)
    assert len(scans) == 1, (
        "within the TTL every form of the registry read must come from the "
        f"cache; the disk was scanned {len(scans)} times")


def test_fresh_scans_disk_in_both_forms(tmp_path, monkeypatch):
    _session(tmp_path, "one")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    claude_sessions()                                  # warm it
    scans = _count_scans(monkeypatch)
    claude_sessions(fresh=True)
    claude_sessions(tmp_path, fresh=True)
    assert len(scans) == 2, "fresh=True must never answer from the cache"


def test_another_directory_neither_reads_nor_writes_the_cache(
        tmp_path, monkeypatch):
    """The tests pass a fixture directory. If that shared the module cache,
    one test's sessions would leak into the next one's roster."""
    registry, other = tmp_path / "registry", tmp_path / "other"
    _session(registry, "real")
    _session(other, "fixture")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", registry)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    scans = _count_scans(monkeypatch)

    assert [s["name"] for s in claude_sessions(other)] == ["fixture"]
    assert [s["name"] for s in claude_sessions(other)] == ["fixture"]
    assert len(scans) == 2, "a foreign directory must be read from disk"
    assert discovery._cache == (0.0, []), (
        "a foreign directory must not write the cache the registry reads")
    assert [s["name"] for s in claude_sessions()] == ["real"]


def test_availability_counts_files_separately_from_the_cached_read(
        tmp_path, monkeypatch):
    """`files` and `live` are two measurements, and the gap between them is
    what tells "readable but nothing resolves" from "genuinely nobody" (D3).
    The cache may make `live` cheap; it may not make `files` a copy of it."""
    _session(tmp_path, "alive")
    _session(tmp_path, "ghost", sid="s2", pid=999_999)
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    seen = discovery.availability()
    assert seen["available"] is True
    assert (seen["files"], seen["live"]) == (2, 1)
    assert seen["note"] == "" and seen["reason"] == ""
    # Same answer on the second call, now served from the warm cache.
    assert discovery.availability()["files"] == 2


def test_availability_still_names_both_readings_when_nothing_resolves(
        tmp_path, monkeypatch):
    _session(tmp_path, "ghost", pid=999_999)
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    seen = discovery.availability()
    assert seen["available"] is True and seen["files"] == 1 and seen["live"] == 0
    assert "readable but none names a process" in seen["note"]


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


def test_a_registration_earns_a_row_of_its_own_when_nothing_else_has_one(
        tmp_path, monkeypatch):
    """The container: no registry to read, and the hook is the only evidence
    that the session exists at all. The row says so in `source`, and it says
    "unknown" for what the registry would have told us."""
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path / "empty")
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    hub = Hub(tmp_path / "rooms")
    [row] = roster(hub, {"shal-7": {"name": "shal-7", "session_id": "sid-7",
                                    "cwd": "C:/PlayGround/agora",
                                    "registered_at": time.time()}})
    assert row["name"] == "shal-7" and row["source"] == "registration"
    assert row["status"] == "unknown" and row["pid"] == 0
    assert row["project"] == "agora" and row["rooms"] == []


def test_a_registration_under_a_second_name_does_not_fork_the_session(
        tmp_path, monkeypatch):
    """Identity comes from the registry (D6). A session that registered as
    "CMO" and resolves as "ops-b0" is one agent, and two rows would offer the
    chair two seats with only one of them reachable."""
    _session(tmp_path, "ops-b0", sid="sid-1")
    monkeypatch.setattr(discovery, "CLAUDE_SESSIONS", tmp_path)
    monkeypatch.setattr(discovery, "_cache", (0.0, []))
    hub = Hub(tmp_path / "rooms")
    rows = roster(hub, {"CMO": {"name": "CMO", "session_id": "sid-1",
                                "registered_at": time.time()}})
    assert [r["name"] for r in rows] == ["ops-b0"]
    assert rows[0]["source"] == "registry"


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
