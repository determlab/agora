"""Agora's SessionStart hook — auto-connect, self-naming, and the wake.

Two roles, one script, selected by argv:

``register``  runs synchronously at session start. It tells Agora the session
              exists — name, cwd, pid, session id — and prints
              ``additionalContext`` so the agent knows, without being told by a
              human, who it is and how to behave in a meeting.

``wait``      runs as an **async** hook with ``asyncRewake``. It parks in a long
              poll against Zulip's event queue (``register`` once, then
              ``GET /api/v1/events``). When someone @-mentions or DMs this
              session's bot, or anyone else writes in its own role stream (issue
              #50), it prints the message and **exits 2**, which wakes the
              session with that text. That is the whole mechanism by which a
              person reaches into a running agent: there is no other supported
              one. Agora was never the wake — Claude Code is; Zulip only
              supplies the URL (issue #41).

              The same ``wait`` is registered on ``Stop`` too, so a session is
              reachable again after every turn, not once (issue #47). A lock
              keeps it to one poller per session, and the queue state is saved
              so a message sent mid-answer wakes the session on the next run.

The session's own name comes from Claude Code's registry rather than from
anybody typing it. A hook payload carries ``session_id``; the registry maps that
to the display name, so a session states its name by looking it up.

Never fails loudly. A hook that breaks a session start is worse than a hook that
does nothing, so every path exits 0 on error except the deliberate exit 2.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import sys
import tempfile
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
# One lock and one saved queue per session, so the poller that runs after each
# turn neither doubles up nor loses what arrived in between.
STATE_DIR = Path(tempfile.gettempdir())
# The holder touches its lock on every poll. One untouched this long belongs to
# a poller that died without cleaning up, whatever its pid now points at: a
# reused pid must not leave a session silently unreachable.
LOCK_STALE_SECONDS = 5 * (POLL_SECONDS + 15)


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
    "your bot, sends it a direct message, or anyone but you writes in "
    "your own stream, this session is woken mid-turn with that message, its "
    "stream and its topic.\n\n"
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
        except (OSError, ValueError, http.client.HTTPException):
            return None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError,
            http.client.HTTPException):
        return None


def _wakes(event: dict, bot_email: str, name: str = "") -> bool:
    """The whole wake rule: a mention of this bot, a direct message to it, or
    any message in its own role stream (``#coo`` for session ``COO``) —
    never its own post (issue #50).

    In its own stream any other sender wakes it, bots included: the founder
    should not have to tag the role he is already talking to, and the hourly
    watchdog posts to ``#coo`` as its own ``Watchdog`` bot precisely to wake
    the COO. Only the session's own bot is excluded, so it cannot wake itself.

    Zulip ANDs the terms of a register ``narrow``, so "mentioned OR private OR
    own stream" cannot be one narrow. The queue takes every message and this
    picks.
    """
    if event.get("type") != "message":
        return False
    msg = event.get("message")
    if not isinstance(msg, dict) or msg.get("sender_email") == bot_email:
        return False
    if "mentioned" in (event.get("flags") or []) or msg.get("type") == "private":
        return True
    return (bool(name) and msg.get("type") == "stream"
            and str(msg.get("display_recipient") or "").lower() == name.lower())


def _rewake_text(name: str, msg: dict) -> str:
    sender = msg.get("sender_full_name") or msg.get("sender_email") or "someone"
    if msg.get("type") == "private":
        where = f"Direct message to @{name} from {sender}"
        reply = "Reply to them in the same direct message."
    else:
        stream = msg.get("display_recipient") or "?"
        topic = msg.get("subject") or ""
        if "mentioned" in (msg.get("flags") or []):
            where = f"@{name} in #{stream} › {topic} — from {sender}"
        else:  # written in this role's own stream, no tag needed
            where = f"#{stream} › {topic} — from {sender}"
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
    """Park until this session's bot is mentioned or DMed, or anyone else writes
    in its own role stream. Exit 2 to wake it.

    Registered on ``SessionStart`` and again on ``Stop`` (issue #47), so it runs
    after every turn. The lock keeps that to one poller per session; the saved
    queue state means a message sent while the agent was answering is still
    delivered by the next run.
    """
    data = _payload()
    # This half is about to park for hours, so it can afford to wait for the
    # registry entry to appear. Giving up here is what left a restarted session
    # permanently uncallable.
    session_id = str(data.get("session_id") or "")
    ident = _identity(session_id, patience=60.0)
    name = ident["name"]
    if not name:
        return 0
    creds = _zulip_creds(name)
    if creds is None:
        return 0  # no bot for this session: nobody can wake it
    return wait_for(creds, name, session_id or name)


def wait_for(creds: tuple[str, str, str], name: str, session: str,
             seconds: float | None = None) -> int:
    """The poller itself. Prints the rewake text and returns 2 on a hit, 0 at
    the deadline or when another poller already serves this session.

    ``bot/zulip.py wait`` calls this too, so a person can run the same code by
    hand.
    """
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", session)[:100] or "unknown"
    lock = STATE_DIR / f"agora-wait-{key}.lock"
    state_path = STATE_DIR / f"agora-wait-{key}.json"
    if not _take_lock(lock):
        return 0  # a live poller already parks for this session
    try:
        state = _load_state(state_path)
        deadline = time.time() + (WAIT_SECONDS if seconds is None else seconds)
        while time.time() < deadline:
            if not _hold_lock(lock):
                return 0  # another poller took over; it serves this session now
            try:
                text = _poll_once(creds, name, state, state_path)
            except Exception:
                # A malformed answer is a misbehaving server. It must not end
                # the wait for the rest of the session: back off, keep waiting.
                text = None
                time.sleep(2.0)
            if text:
                print(text)
                return 2  # asyncRewake: exit 2 wakes the session with the text
        return 0
    finally:
        _drop_lock(lock)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows terminates the process. Ask instead.
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot tell: treat as alive, the mtime rule decides
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _take_lock(lock: Path) -> bool:
    """Create the lock with this pid in it. A lock whose pid is dead is stale
    and is replaced; one just created by a racing poller (no pid written yet)
    is respected."""
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock.read_text(encoding="utf-8").strip() or "0")
                age = time.time() - lock.stat().st_mtime
            except (OSError, ValueError):
                return False
            if age < LOCK_STALE_SECONDS and (_pid_alive(pid) or pid == 0):
                return False
            try:
                lock.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        # Two pollers replacing the same stale lock can both get here; only
        # the one whose pid is in the file holds it.
        return _hold_lock(lock)
    return False


def _hold_lock(lock: Path) -> bool:
    """True while the lock still names this process, touching it so it stays
    fresh. False once another poller has replaced it."""
    try:
        if lock.read_text(encoding="utf-8").strip() != str(os.getpid()):
            return False
        os.utime(lock)
        return True
    except OSError:
        return False


def _drop_lock(lock: Path) -> None:
    try:
        if lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink()
    except (OSError, ValueError):
        pass


def _load_state(path: Path) -> dict:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("last_message_id") is None:
            raise ValueError("no message floor: start over")
        return {"queue_id": saved.get("queue_id"),
                "last_event_id": int(saved.get("last_event_id", -1)),
                "last_message_id": int(saved["last_message_id"]),
                "backfill": bool(saved.get("backfill"))}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"queue_id": None, "last_event_id": -1,
                "last_message_id": None, "backfill": False}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def _backfill(creds: tuple[str, str, str], after: int) -> list | None:
    """Every message after id ``after`` that the live path would have woken
    on, oldest first, or None if Zulip could not answer. Two narrows, because
    narrow terms are ANDed; each is paged to the end so a long gap loses
    nothing."""
    found: dict = {}
    for narrow in ([{"operator": "is", "operand": "mentioned"}],
                   [{"operator": "is", "operand": "dm"}]):
        anchor = after
        for _page in range(50):
            got = _zulip(creds, "GET", "messages", {
                "narrow": json.dumps(narrow), "anchor": anchor,
                "include_anchor": "false", "num_before": 0, "num_after": 100,
                "apply_markdown": "false"}, 30.0)
            if not got or got.get("result") != "success":
                return None
            msgs = [m for m in got.get("messages") or [] if isinstance(m, dict)]
            for msg in msgs:
                i = int(msg.get("id", -1))
                anchor = max(anchor, i)
                # The same test as the live queue's: the mention flag or a DM.
                if i > after and ("mentioned" in (msg.get("flags") or [])
                                  or msg.get("type") == "private"):
                    found[i] = msg
            if got.get("found_newest", True) or not msgs:
                break
    return [found[i] for i in sorted(found)]


def _poll_once(creds: tuple[str, str, str], name: str, state: dict,
               state_path: Path) -> str | None:
    """One step: register if needed, backfill if a queue was lost, else one
    long poll. Returns the rewake text on a hit. State is saved before a hit
    is returned, so the next run never delivers the same message twice."""
    bot_email = creds[1]
    if state["queue_id"] is None:
        reg = _zulip(creds, "POST", "register", {
            "event_types": json.dumps(["message"]),
            "apply_markdown": "false"}, 15.0)
        if not reg or reg.get("result") != "success":
            time.sleep(2.0)  # Zulip down or refusing; keep waiting
            return None
        queue_id, last_event_id = str(reg["queue_id"]), int(reg["last_event_id"])
        state["queue_id"], state["last_event_id"] = queue_id, last_event_id
        if state["last_message_id"] is None:
            # First run for this session: start from now, not from history.
            state["last_message_id"] = int(reg.get("max_message_id", -1))
        else:
            # A queue was lost between runs: what arrived in the gap is only
            # in the message history.
            state["backfill"] = True
        _save_state(state_path, state)
    candidates: list = []
    if state["backfill"]:
        msgs = _backfill(creds, state["last_message_id"])
        if msgs is None:
            time.sleep(2.0)
            return None
        state["backfill"] = False
        candidates = [m for m in msgs if m.get("sender_email") != bot_email]
        seen = [int(m["id"]) for m in msgs]
    else:
        got = _zulip(creds, "GET", "events", {
            "queue_id": state["queue_id"], "last_event_id": state["last_event_id"]},
            POLL_SECONDS + 15)
        if got and got.get("code") == "BAD_EVENT_QUEUE_ID":
            # The queue expired while nobody polled. The normal path after a
            # long gap, not an error: take a new one and backfill.
            state["queue_id"] = None
            time.sleep(2.0)
            return None
        if not got or got.get("result") != "success":
            time.sleep(2.0)  # Zulip down; back off a little and keep waiting
            return None
        events = [e for e in got.get("events") or [] if isinstance(e, dict)]
        # Resume past every event, heartbeats included, or the next poll
        # fetches them again. A bad id is skipped, not fatal.
        seen = []
        for event in events:
            try:
                state["last_event_id"] = max(state["last_event_id"], int(event["id"]))
            except (KeyError, TypeError, ValueError):
                pass
            msg = event.get("message")
            if event.get("type") == "message" and isinstance(msg, dict):
                try:
                    seen.append(int(msg["id"]))
                except (KeyError, TypeError, ValueError):
                    pass
        # The event's flags ride on a copy of its message, so the rewake text
        # can tell a mention from a message in the session's own stream.
        candidates = [{**e["message"], "flags": e.get("flags") or []}
                      for e in events if _wakes(e, bot_email, name)]
    # A message already delivered — by the queue or by a backfill — is never
    # delivered again.
    floor = state["last_message_id"]
    hits = [m for m in candidates if int(m.get("id", -1)) > floor]
    if seen:
        state["last_message_id"] = max([floor, *seen])
    _save_state(state_path, state)
    if not hits:
        return None
    return "\n\n---\n\n".join(_rewake_text(name, m) for m in hits)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "register"
    try:
        return do_wait() if mode == "wait" else do_register()
    except Exception:
        return 0  # never break a session start


if __name__ == "__main__":
    raise SystemExit(main())
