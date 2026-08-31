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
import threading
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


#: The registry is read on every state push and on every join. Each read stats
#: and parses a handful of files and probes their pids, and the state stream
#: does it every two seconds per open browser tab. A one-second cache makes that
#: cost independent of how many things are watching, and one second is far below
#: any rate a human notices.
_CACHE_TTL = 1.0
_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
_cache_lock = threading.Lock()


def claude_sessions(directory: Path | None = None,
                    *, fresh: bool = False) -> list[dict[str, Any]]:
    """Every live Claude Code session on this machine, newest first."""
    if directory is None and not fresh:
        with _cache_lock:
            at, cached = _cache
            if (time.time() - at) < _CACHE_TTL:
                return cached
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
    if directory == CLAUDE_SESSIONS:
        with _cache_lock:
            globals()["_cache"] = (time.time(), out)
    return out


def _in_container() -> bool:
    """Best-effort: is this process inside a container?

    Only used to choose the wording of a warning, never to change behaviour —
    a wrong guess costs a slightly off sentence, not a wrong roster.
    """
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        # Last, and least reliable: cgroup v2 hosts often carry no "docker"
        # string at all, so a miss here is not evidence of a host.
        return "docker" in Path("/proc/1/cgroup").read_text()
    except OSError:
        return False


def availability(directory: Path | None = None) -> dict[str, Any]:
    """Whether the roster could be built, and what to say when it could not.

    An empty roster has three causes that render identically, and only one of
    them is "nobody is running":

    1. The registry directory is not there at all. Normal inside a container,
       where ``~/.claude`` belongs to a user who does not exist.
    2. It is there and readable, but every entry names a pid this process cannot
       see. Also normal inside a container: the session files were written by
       Windows processes and the pid check runs in the container's own pid
       namespace, where those numbers mean nothing or mean something else. On
       Docker Desktop there is no ``--pid=host`` that fixes this — the "host"
       is the Linux VM, not Windows. **Mounting the directory is necessary and
       not sufficient**; discovery does not work in a container here.
    3. Every session really has exited, leaving stale files behind — the case
       the pid check exists for.

    2 and 3 are the same measurement, so this reports the measurement (files
    found, files that resolved) and names both readings rather than picking one.
    Reporting any of the three as "nobody is running" is the defect class this
    repo refuses (D3).

    ``available`` answers only "can the directory be read". ``note`` carries the
    second warning, so the page can show both without conflating them.
    """
    directory = directory or CLAUDE_SESSIONS
    if not directory.exists():
        return {
            "available": False, "path": str(directory), "files": 0, "live": 0,
            "note": "",
            "reason": ("No Claude Code session registry at this path, so the "
                       "roster cannot be built and nothing can be called — "
                       "which is not the same as nobody running. In a "
                       "container, mount the host's ~/.claude/sessions "
                       "read-only; see CONTRIBUTING.md."),
        }
    files = len(list(directory.glob("*.json")))
    # `None`, not `directory`, in the default case: claude_sessions reads its
    # cache only when passed None (it writes it whenever the resolved path is
    # CLAUDE_SESSIONS — the two gates disagree). Passing the resolved path here
    # is correct and costs a full uncached scan of the registry on every state
    # build, which _state_stream does every 2s per open tab.
    live = len(claude_sessions(None if directory == CLAUDE_SESSIONS else directory))
    note = ""
    if files and not live:
        note = (f"{files} session files are readable but none names a process "
                f"this server can see, so the roster is empty.")
        note += (" Agora is running in a container: the pids in those files "
                 "belong to the host, and they cannot be checked from in here. "
                 "Discovery does not work in a container — see CONTRIBUTING.md."
                 if _in_container() else
                 " Either every session has exited and left its file behind, "
                 "or those pids belong to another machine.")
    return {"available": True, "path": str(directory), "files": files,
            "live": live, "note": note, "reason": ""}


def canonical_name(session_id: str) -> str:
    """The registry's name for a session id, or "" if it is not a live session.

    Identity has to come from the same source the chair's roster is built from.
    A name a session picks for itself is a second source of truth by
    construction: it forks the moment the two disagree, and then the roster
    offers a Call that reaches a seat nobody is sitting in.
    """
    if not session_id:
        return ""
    for s in claude_sessions():
        if s["session_id"] == session_id:
            return s["name"]
    return ""


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
        # "*" is ANY_ROOM in agora/mcp.py, which is the source of truth for the
        # value. Spelled literally here rather than imported: mcp.py imports this
        # module (canonical_name), so importing back would be a circular import.
        f'Then loop: call `room_wait` with room="*" and '
        f'cursors={{"{room.id}": <the seq room_join returned>}} — one parked '
        f'call covers this meeting, every other room you are in, and the lobby, '
        f'and it is how the chair reaches you. Pass the `cursors` map from each '
        f'reply straight back into the next call. When it returns messages, read '
        f'them and reply with `room_post` if you have something to add. Keep '
        f'calling `room_wait` until the room closes or the founder tells you to '
        f'leave, and rest there again whenever you have nothing else to do. Do '
        f'not leave after one turn — a meeting is a conversation, not a question.'
    )
    return {"register": register, "prompt": prompt}
