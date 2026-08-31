"""Who is running right now.

Claude Code writes one JSON file per live session under ``~/.claude/sessions``.
It carries the session's display name, working directory, live busy/idle status,
and the named pipe it listens on. That file is the registry — there is nothing
to install and nothing to poll a process for.

Stale files outlive their process, so every entry is checked against the pid
before it is reported online.

Other providers are not discoverable this way and are not meant to be: a Codex,
Cursor or Gemini client appears the moment it joins a room over MCP. Discovery
here is a convenience for the one provider that publishes a registry, not the
mechanism the product depends on.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

CLAUDE_SESSIONS = Path.home() / ".claude" / "sessions"

# A session file older than this with no update is treated as stale even if the
# pid still resolves — pids are reused.
STALE_AFTER = 60 * 60 * 12

# A room participant that is not a live session and has not been heard from in
# this long is a ghost — a renamed or restarted session's abandoned seat.
GHOST_AFTER = 15 * 60


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # No signal 0 on Windows. OpenProcess via ctypes is the cheap check.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def claude_sessions(directory: Path | None = None) -> list[dict[str, Any]]:
    """Every live Claude Code session on this machine, newest first."""
    directory = directory or CLAUDE_SESSIONS
    if not directory.exists():
        return []
    now = time.time()
    out: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = int(rec.get("pid") or 0)
        updated = float(rec.get("updatedAt") or 0) / 1000.0
        if not _pid_alive(pid):
            continue
        if updated and (now - updated) > STALE_AFTER:
            continue
        out.append({
            "provider": "claude-code",
            "name": rec.get("name") or f"pid-{pid}",
            "session_id": rec.get("sessionId", ""),
            "pid": pid,
            "cwd": rec.get("cwd", ""),
            "project": Path(rec.get("cwd", "")).name or "",
            "status": rec.get("status", "unknown"),
            "kind": rec.get("kind", ""),
            "version": rec.get("version", ""),
            "updated": updated,
            "socket": rec.get("messagingSocketPath", ""),
        })
    out.sort(key=lambda s: -s["updated"])
    return out


def roster(hub) -> list[dict[str, Any]]:
    """Discovered sessions, annotated with which rooms they are already in.

    The web UI needs both halves in one place: who exists, and who is already
    seated. A session in no room is what the invite button is for.
    """
    sessions = claude_sessions()
    seated: dict[str, list[str]] = {}
    for room in hub.rooms.values():
        for name in room.participants:
            seated.setdefault(name, []).append(room.id)
    for s in sessions:
        s["rooms"] = seated.get(s["name"], [])
    # A participant that is not a live Claude Code session still deserves a row
    # — a Codex or Cursor client is real — but only while it is actually there.
    # A renamed or restarted session leaves its old seat behind, and a roster
    # that keeps showing `ops-92` after `ops-92` is gone is worse than one that
    # misses a row: the chair invites a name that no longer exists.
    known = {s["name"] for s in sessions}
    now = time.time()
    extra: dict[str, dict[str, Any]] = {}
    for room in hub.rooms.values():
        for name, p in room.participants.items():
            if name in known or name in extra or p.role == "human":
                continue
            if not p.last_seen or (now - p.last_seen) > GHOST_AFTER:
                continue  # a ghost: seated once, not seen since
            extra[name] = {
                "provider": p.provider or "mcp",
                "name": name,
                "session_id": p.session_id,
                "pid": 0,
                "cwd": p.cwd,
                "project": Path(p.cwd).name if p.cwd else "",
                "status": "idle",
                "kind": p.role,
                "version": "",
                "updated": p.last_seen,
                "socket": "",
                "rooms": seated.get(name, []),
            }
    return sessions + list(extra.values())


def invite_text(room: "Any", server_url: str, session_name: str = "") -> dict[str, str]:
    """What to hand a session so it can join and stay.

    Returns both halves, because the two providers need different things: a
    one-time CLI registration of the MCP server, and a prompt that makes the
    agent actually sit in the room instead of checking it once.
    """
    who = session_name or "your session name"
    # --scope user, so every session on this machine gets it from one registration
    # rather than per-project. MCP servers connect at startup, so a session that
    # was already running must be restarted once before it can join — after that,
    # every future meeting works with no restart.
    register = (f'claude mcp add --scope user --transport http agora '
                f'{server_url.rstrip("/")}/mcp')
    prompt = (
        f'Join the Agora meeting "{room.title}" (room id `{room.id}`) and stay in it.\n'
        f'Call `room_join` with room="{room.id}" and name="{who}". '
        f'Then loop: call `room_wait` with room="{room.id}" and the last seq you saw; '
        f'when it returns messages, read them and reply with `room_post` if you have '
        f'something to add. Keep calling `room_wait` until the room closes or the '
        f'founder tells you to leave. Do not leave after one turn — a meeting is a '
        f'conversation, not a question.'
    )
    return {"register": register, "prompt": prompt}
