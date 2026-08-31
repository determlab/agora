"""Agora's SessionStart hook — auto-connect, self-naming, and the summons.

Two roles, one script, selected by argv:

``register``  runs synchronously at session start. It tells Agora the session
              exists — name, cwd, pid, session id — and prints
              ``additionalContext`` so the agent knows, without being told by a
              human, who it is and how to behave in a meeting.

``wait``      runs as an **async** hook with ``asyncRewake``. It parks in a long
              poll against ``/api/summons``. When the chair clicks *Call to
              room*, it prints the invitation and **exits 2**, which wakes the
              session with that text. That is the whole mechanism by which a web
              page reaches into a running agent: there is no other supported one.

The session's own name comes from Claude Code's registry rather than from
anybody typing it. A hook payload carries ``session_id``; the registry maps that
to the display name, so a session states its name by looking it up.

Never fails loudly. A hook that breaks a session start is worse than a hook that
does nothing, so every path exits 0 on error except the deliberate exit 2.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows stdout defaults to the ANSI codepage, which mangles anything outside
# it — and this script's whole output is text a session will read.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - already unicode
        pass

SERVER = os.environ.get("AGORA_URL", "http://127.0.0.1:8765").rstrip("/")
SESSIONS = Path.home() / ".claude" / "sessions"
POLL_SECONDS = 300.0


def _payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _lookup(session_id: str) -> dict | None:
    """One pass over the registry for this session id."""
    if not SESSIONS.exists():
        return None
    for path in SESSIONS.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if session_id and rec.get("sessionId") != session_id:
            continue
        return {"name": rec.get("name") or "", "cwd": rec.get("cwd") or os.getcwd(),
                "pid": rec.get("pid") or os.getppid(), "session_id": session_id}
    return None


def _identity(session_id: str, *, patience: float = 0.0) -> dict:
    """Name, cwd and pid for this session, from Claude Code's own registry.

    `SessionStart` can fire before the session has written its own registry
    entry. Without patience the hook found no name, returned quietly, and that
    session was never reachable — for the rest of its life, with nothing in any
    log to say why. Waiting a few seconds costs nothing: the sync half runs
    once, and the async half is about to park for hours.
    """
    deadline = time.time() + patience
    while True:
        found = _lookup(session_id)
        if found and found["name"]:
            return found
        if time.time() >= deadline:
            return found or {"name": "", "cwd": os.getcwd(),
                             "pid": os.getppid(), "session_id": session_id}
        time.sleep(0.5)


def _post(path: str, body: dict, timeout: float = 5.0) -> dict | None:
    req = urllib.request.Request(
        f"{SERVER}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _get(path: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(f"{SERVER}{path}", timeout=timeout) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


HOW_TO_SIT = (
    "You are connected to Agora, a meeting room the founder chairs in a browser. "
    "Your name there is {name!r} — use it exactly, and do not ask anyone to "
    "rename you.\n\n"
    "You will be CALLED into a meeting when the chair wants you; do not join a "
    "room uninvited. When you are called you will receive a message naming the "
    "room.\n\n"
    "Once called: `room_join` (room id, your name, provider \"claude-code\", "
    "your role) -> `room_history` to read what was said before you arrived -> "
    "then loop on `room_wait` with the last seq you saw, replying with "
    "`room_post`.\n\n"
    "**You join MUTED.** That is deliberate — five agents talking at once is not "
    "a meeting. Read, follow the conversation, and wait for the chair to unmute "
    "you. A muted `room_post` is refused and that is not an error; keep waiting "
    "on `room_wait`.\n\n"
    "Do not leave after one turn. Keep calling `room_wait` until the room closes "
    "or the chair tells you to go."
)


def do_register() -> int:
    data = _payload()
    # The sync half must not delay a session start, so it waits only briefly.
    ident = _identity(str(data.get("session_id") or ""), patience=8.0)
    if not ident["name"]:
        return 0  # not a registered session; say nothing rather than guess
    ok = _post("/api/register", {**ident, "provider": "claude-code"})
    if ok is None:
        return 0  # Agora is not running. Silence is correct.
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": HOW_TO_SIT.format(name=ident["name"]),
    }}))
    return 0


def do_wait() -> int:
    """Park until the chair calls this session. Exit 2 to wake it."""
    data = _payload()
    # This half is about to park for hours, so it can afford to wait for the
    # registry entry to appear. Giving up here is what left a restarted session
    # permanently uncallable.
    ident = _identity(str(data.get("session_id") or ""), patience=60.0)
    name = ident["name"]
    if not name:
        return 0
    deadline = time.time() + 60 * 60 * 11  # under a 12h hook timeout
    while time.time() < deadline:
        got = _get(f"/api/summons?session={name}&timeout={int(POLL_SECONDS)}",
                   POLL_SECONDS + 15)
        if got and got.get("summoned"):
            print(
                f"The chair has called you into the Agora meeting "
                f"{got.get('title')!r} (room id `{got.get('room')}`).\n\n"
                f"Agenda: {got.get('agenda') or '(none set)'}\n\n"
                f"Join now: `room_join` with room=\"{got.get('room')}\" and "
                f"name=\"{name}\". Then `room_history` to read what was said "
                f"before you arrived, then loop on `room_wait` from seq "
                f"{got.get('seq', 0)}.\n\n"
                f"You will be muted on arrival. Follow the conversation and wait "
                f"for the chair to unmute you — do not treat a refused "
                f"`room_post` as an error. Stay in the loop until the room "
                f"closes.")
            return 2  # asyncRewake: exit 2 wakes the session with the text above
        if got is None:
            # Agora down or nothing pending; back off a little and keep waiting.
            time.sleep(2.0)
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "register"
    try:
        return do_wait() if mode == "wait" else do_register()
    except Exception:
        return 0  # never break a session start


if __name__ == "__main__":
    raise SystemExit(main())
