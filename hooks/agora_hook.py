"""Agora's SessionStart hook — auto-connect, self-naming, and the wake.

Two roles, one script, selected by argv:

``register``  runs synchronously at session start. It tells Agora the session
              exists — name, cwd, pid, session id — and prints
              ``additionalContext`` so the agent knows, without being told by a
              human, who it is and how to behave in a meeting.

``wait``      runs as an **async** hook with ``asyncRewake``. It parks in a long
              poll against Zulip's event queue (``register`` once, then
              ``GET /api/v1/events``). When someone @-mentions or DMs this
              session's bot, it prints the message and **exits 2**, which wakes
              the session with that text. That is the whole mechanism by which a
              person reaches into a running agent: there is no other supported
              one. Agora was never the wake — Claude Code is; Zulip only
              supplies the URL (issue #41).

The session's own name comes from Claude Code's registry rather than from
anybody typing it. A hook payload carries ``session_id``; the registry maps that
to the display name, so a session states its name by looking it up.

Never fails loudly. A hook that breaks a session start is worse than a hook that
does nothing, so every path exits 0 on error except the deliberate exit 2.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
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
# Zulip answers an idle long poll with a heartbeat event about once a minute,
# so this is only a ceiling for a server that stops answering altogether.
POLL_SECONDS = 90.0
WAIT_SECONDS = 60 * 60 * 11  # under a 12h hook timeout
# Bot credentials live beside the bot code, never in this file: gitignored.
DOTENV = Path(__file__).resolve().parent.parent / "bot" / ".env"


def _payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _lookup(session_id: str) -> dict | None:
    """One pass over the registry for this session id.

    An empty `session_id` must find nothing. It used to fall through the filter
    and return whichever file glob yielded first — another session's name, given
    to this one, matching no room and no registry entry. Guessing an identity is
    worse than having none.
    """
    if not session_id or not SESSIONS.exists():
        return None
    for path in SESSIONS.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("sessionId") != session_id:
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


HOW_TO_SIT = (
    "You are reachable in Zulip, the team chat the founder uses. Your session "
    "name is {name!r} — use it exactly, and do not ask anyone to rename you.\n\n"
    "Each role has its own stream (#coo, #cto, #cmo); inside a stream the topic "
    "is the issue number, e.g. `#141 record shape`. When someone @-mentions "
    "your bot or sends it a direct message, this session is woken mid-turn "
    "with that message, its stream and its topic.\n\n"
    "Post only when you are asked, or when something needs a person. Answer "
    "in the same stream and topic you were asked in."
)


def _dotenv(path: Path) -> dict:
    """``KEY=value`` lines from the gitignored bot/.env. Missing file: empty."""
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _zulip_creds(name: str) -> tuple[str, str, str] | None:
    """(site, email, api_key) for this session's bot, or None.

    Session ``COO`` uses ``ZULIP_COO_EMAIL`` / ``ZULIP_COO_API_KEY``, from the
    environment first, then bot/.env. A session with no bot of its own has
    nobody to be woken by, and says nothing.
    """
    key = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    if not key:
        return None
    env = _dotenv(DOTENV)

    def get(k: str, default: str = "") -> str:
        return os.environ.get(k) or env.get(k) or default

    email, api_key = get(f"ZULIP_{key}_EMAIL"), get(f"ZULIP_{key}_API_KEY")
    if not email or not api_key:
        return None
    return get("ZULIP_SITE", "http://zulip.localhost:8090").rstrip("/"), email, api_key


def _zulip(creds: tuple[str, str, str], method: str, path: str, params: dict,
           timeout: float) -> dict | None:
    """One Zulip REST call. Returns the decoded body — an error body such as
    ``BAD_EVENT_QUEUE_ID`` included — or None when Zulip cannot be reached.

    Its own few lines of urllib rather than ``bot/zulip_client``: this hook
    runs inside every session on the machine and depends on nothing it cannot
    see in this file.
    """
    site, email, api_key = creds
    url = f"{site}/api/v1/{path}"
    query = urllib.parse.urlencode(params)
    data = None
    if method == "GET":
        url = f"{url}?{query}"
    else:
        data = query.encode("utf-8")
    token = base64.b64encode(f"{email}:{api_key}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {token}",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8") or "{}")
        except (OSError, ValueError):
            return None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _wakes(event: dict, bot_email: str) -> bool:
    """A mention of this bot, or a direct message to it — never its own post.

    Zulip ANDs the terms of a register ``narrow``, so "mentioned OR private"
    cannot be one narrow. The queue takes every message and this picks.
    """
    if event.get("type") != "message":
        return False
    msg = event.get("message") or {}
    if msg.get("sender_email") == bot_email:
        return False
    return "mentioned" in (event.get("flags") or []) or msg.get("type") == "private"


def _rewake_text(name: str, msg: dict) -> str:
    sender = msg.get("sender_full_name") or msg.get("sender_email") or "someone"
    if msg.get("type") == "private":
        where = f"Direct message to @{name} from {sender}"
        reply = "Reply to them in the same direct message."
    else:
        stream = msg.get("display_recipient")
        topic = msg.get("subject") or ""
        where = f"@{name} in #{stream} › {topic} — from {sender}"
        reply = f"Reply in #{stream}, topic {topic!r}."
    return f"{where}\n\n{msg.get('content', '')}\n\n{reply}"


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
    """Park until this session's bot is mentioned or DMed. Exit 2 to wake it."""
    data = _payload()
    # This half is about to park for hours, so it can afford to wait for the
    # registry entry to appear. Giving up here is what left a restarted session
    # permanently uncallable.
    ident = _identity(str(data.get("session_id") or ""), patience=60.0)
    name = ident["name"]
    if not name:
        return 0
    creds = _zulip_creds(name)
    if creds is None:
        return 0  # no bot for this session: nobody can wake it
    bot_email = creds[1]
    deadline = time.time() + WAIT_SECONDS
    queue_id, last_event_id = None, -1
    while time.time() < deadline:
        if queue_id is None:
            reg = _zulip(creds, "POST", "register", {
                "event_types": json.dumps(["message"]),
                "apply_markdown": "false"}, 15.0)
            if not reg or reg.get("result") != "success":
                time.sleep(2.0)  # Zulip down or refusing; keep waiting
                continue
            queue_id, last_event_id = reg["queue_id"], int(reg["last_event_id"])
        got = _zulip(creds, "GET", "events", {
            "queue_id": queue_id, "last_event_id": last_event_id},
            POLL_SECONDS + 15)
        if got and got.get("code") == "BAD_EVENT_QUEUE_ID":
            # The queue expired while nobody polled. The normal path after a
            # long gap, not an error: take a new one.
            queue_id = None
            continue
        if not got or got.get("result") != "success":
            time.sleep(2.0)  # Zulip down; back off a little and keep waiting
            continue
        events = got.get("events") or []
        # Resume past every event, heartbeats included, or the next poll
        # fetches them again.
        for event in events:
            last_event_id = max(last_event_id, int(event.get("id", -1)))
        hits = [e["message"] for e in events if _wakes(e, bot_email)]
        if hits:
            print("\n\n---\n\n".join(_rewake_text(name, m) for m in hits))
            return 2  # asyncRewake: exit 2 wakes the session with the text above
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "register"
    try:
        return do_wait() if mode == "wait" else do_register()
    except Exception:
        return 0  # never break a session start


if __name__ == "__main__":
    raise SystemExit(main())
