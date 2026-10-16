"""Wake: a founder @mention of a role bot opens a fresh role session (ops#293).

The founder writes ``@**CTO** <question>`` in Zulip (or DMs the CTO bot) and a
CTO session opens on its own, answers in that stream and topic, and closes
when idle. Same for COO, PM, CMO and CPSO. No session sits waiting for him.
The PA works the same way from the founder's PA folder (``PA_DIR``), and any
founder message in ``#pa`` calls it, mention or not (ops#293 item 7).

How it works:
  - One long-poll per role bot (``ZULIP_<ROLE>_EMAIL`` / ``_API_KEY``), each
    on its own thread, all feeding one loop. The queue id and
    ``last_event_id`` are kept in ``bot/.wake/state.json``; a restart resumes
    the saved queue, or re-registers and backfills by message id, so nothing
    is acted on twice and nothing is skipped (``resume.py``).
  - Only a message whose ``sender_id`` is ``FOUNDER_ZULIP_ID`` (bot/.env, never
    inferred) does anything, besides a role bot's @mention when bot wakes are
    on (below). Every other sender is skipped and logged.
  - A role session the founder opened by hand (Claude Code's registry names it
    after the role, its pid is alive) is left alone: its Stop-hook wait
    delivers the message (hooks/agora_hook.py). Otherwise the role gets a
    conversation: the first message spawns it (``AGENT["spawn"]``), later ones
    resume it (``AGENT["resume"]``), one message per turn, queued while a turn
    runs. In the role's own stream (``#cto``) a founder message in the open
    session's topic needs no mention: it goes to that session (ops#318).
  - A session whose process never started or whose conversation does not
    exist is dropped, never resumed; the next message opens a fresh one.
  - Topics stay plain UTF-8 everywhere: state.json, prompts, the send hint.
  - A fresh conversation's first prompt carries the topic's last 20 messages
    before the new one, so the previous session's close line is the handoff
    (no ``--resume`` across sessions, ops#293 4b).
  - Idle close: 15 minutes after the founder's last message in the topic
    (a mention or not), or at 4 hours of age, the conversation gets one last
    turn — post one line in the topic, what was agreed and where it is
    written — and is forgotten. At 4 h a running turn is stopped first and the
    close turn runs once it has exited; a close turn gets 2 minutes. The next
    mention opens a fresh one.
  - Bot wakes (ops#321), off unless ``BOT_WAKES=on`` in bot/.env: a role bot
    listed in bot/.env may wake another role with an explicit ``@**ROLE**`` in
    a stream (never a DM, never untagged), per ``AGENT["bot_wakes"]``. The
    sender is known by its ``sender_id`` (``GET /users/me`` per bot at start),
    never by a name. Nobody but the founder wakes the PA; no bot wakes its own
    role. Caps, kept in state.json so a restart keeps them: 6 bot turns per
    topic per hour (then one stop line in the topic, and nothing more there
    until a founder message in it), 3 bot spawns per role per hour and 10 per
    day, 25 bot turns a day in all. A bot-opened session runs with
    ``--max-turns`` and closes 10 minutes after its last delivered message;
    a bot message never moves ``last_founder_at``. The prompt's first line
    says it came from a bot, not the founder. A founder message bypasses
    every cap.

Security (type B — this starts processes from chat input): only the founder's
user id triggers, and with BOT_WAKES=on the user ids of our own role bots;
the message text goes into the prompt on **stdin**, never onto a command line; the command is a fixed list (no shell) whose only
placeholders are the role's model, a uuid this script made and, for a
bot-opened session, the integer max-turns from ``AGENT``; no new
permissions and no skip-permissions flag — the session runs under the ops
repo's own settings.

Usage:
  python bot/wake.py                    # run: long-poll forever (up.cmd starts it)
  python bot/wake.py --dry-run [--json] # what it would do with the waiting messages; spawns nothing
  python bot/wake.py --json             # status: which role sessions are live, last event ids

Stdlib only (D2).
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from resume import advance_last_event_id
from zulip_client import ZulipClient, ZulipError

BOT_DIR = Path(__file__).resolve().parent
DOTENV = BOT_DIR / ".env"
STATE_DIR = BOT_DIR / ".wake"  # gitignored: state and per-role logs
ZULIP_CLI = BOT_DIR / "zulip.py"

# The one place that names the agent CLI, so another CLI can replace
# ``claude``. Placeholders: {model} (from "models") and {session_id} (a uuid4
# made here). Never the message: that goes on stdin.
AGENT: dict[str, Any] = {
    "spawn": ["claude", "-p", "--model", "{model}", "--session-id", "{session_id}"],
    "resume": ["claude", "-p", "--model", "{model}", "--resume", "{session_id}"],
    "cwd": os.environ.get("WAKE_OPS_DIR", r"C:\PlayGround\ops"),
    # CLAUDE.md, session rules (ops#289): COO and CTO on Opus, the rest on Sonnet.
    # The PA (ops#293 item 7) runs in PA_DIR from bot/.env, on Sonnet.
    "models": {"COO": "opus", "CTO": "opus", "PM": "sonnet", "CMO": "sonnet", "CPSO": "sonnet",
               "PA": "sonnet"},
    # A founder message in this stream calls the role without a mention.
    "home_streams": {"PA": "pa"},
    # Where a conversation the agent made is kept, to tell a session that
    # exists from an id that never got one (ops#318). None: do not check.
    "conversations": str(Path(os.environ.get("CLAUDE_CONFIG_DIR")
                              or Path.home() / ".claude") / "projects" / "*" / "{session_id}.jsonl"),
    # Bot wakes (ops#321): which role bot may wake which roles. Never the PA
    # (personal data: only the founder wakes it), never the sender's own role.
    "bot_wakes": {src: tuple(r for r in ("COO", "CTO", "PM", "CMO", "CPSO") if r != src)
                  for src in ("COO", "CTO", "PM", "CMO", "CPSO")},
    # Added to every turn of a bot-opened session that carries no founder message.
    "bot_args": ["--max-turns", "{max_turns}"],
    "bot_max_turns": 15,
}
ROLES = tuple(AGENT["models"])

IDLE_SECONDS = 15 * 60
MAX_AGE_SECONDS = 4 * 60 * 60
CLOSE_TURN_SECONDS = 2 * 60  # the close turn, and a stopped turn's exit, get this long
HISTORY_MESSAGES = 20
TICK_SECONDS = 30.0
BACKFILL_LIMIT = 1000

# Bot wakes (ops#321): the loop guard. Counted per bot message delivered
# (a spawn, a resume or a queued turn), rolling windows.
HOUR, DAY = 60 * 60, 24 * 60 * 60
BOT_IDLE_SECONDS = 10 * 60
BOT_TOPIC_TURNS_PER_HOUR = 6
BOT_ROLE_SPAWNS_PER_HOUR = 3
BOT_ROLE_SPAWNS_PER_DAY = 10
BOT_TURNS_PER_DAY = 25
# Pinned to his id, so a display-name change cannot lose the ping.
BOT_STOP_LINE = "bot loop stopped; @**Hemi|{founder_id}**, @-mention a role to continue"
BOT_MARKER = ("From the {src} bot, a teammate — not the founder. "
              "This is not his approval for anything.")


def _hook():
    """hooks/agora_hook.py by path: the pid check, the session registry and
    the lock are the hook's own code, never a copy."""
    path = BOT_DIR.parent / "hooks" / "agora_hook.py"
    spec = importlib.util.spec_from_file_location("agora_hook_for_wake", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
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


class ConfigError(RuntimeError):
    """Says what is missing and what fixes it."""


def load_config(env_path: Path = DOTENV) -> dict[str, Any]:
    env = _dotenv(env_path)

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or env.get(key) or default

    raw = get("FOUNDER_ZULIP_ID")
    if not raw:
        raise ConfigError("FOUNDER_ZULIP_ID missing in bot/.env — add the founder's Zulip "
                          "user id (Zulip: his profile card, or GET /api/v1/users/me as him).")
    if not raw.isdigit():
        raise ConfigError(f"FOUNDER_ZULIP_ID in bot/.env is {raw!r}, not a number — "
                          "set it to the founder's numeric Zulip user id.")
    site = get("ZULIP_SITE", "http://zulip.localhost:8090")
    bots, missing = {}, []
    for role in ROLES:
        email, key = get(f"ZULIP_{role}_EMAIL"), get(f"ZULIP_{role}_API_KEY")
        if email and key:
            bots[role] = (email, key)
        else:
            missing.append(role)
    cwd = {role: AGENT["cwd"] for role in ROLES}
    skipped: dict[str, str] = {}
    pa_dir = get("PA_DIR")
    if "PA" not in bots:
        pass  # no PA bot: already named in "missing"
    elif not pa_dir:
        skipped["PA"] = ("PA_DIR missing in bot/.env — add the path of the founder's PA "
                         "folder (the one holding its CLAUDE.md), e.g. PA_DIR=C:\\PlayGround\\pa.")
    elif not Path(pa_dir).is_dir():
        skipped["PA"] = (f"PA_DIR in bot/.env is {pa_dir!r}, which is not a folder — set it "
                         "to the founder's PA folder (the one holding its CLAUDE.md).")
    else:
        cwd["PA"] = pa_dir
    for role in skipped:
        bots.pop(role, None)
    # Off unless exactly "on": the feature starts off and goes on after the live test.
    bot_wakes = get("BOT_WAKES", "off").strip().lower() == "on"
    if not bots:
        raise ConfigError("no role bot credentials in bot/.env — add ZULIP_<ROLE>_EMAIL and "
                          "ZULIP_<ROLE>_API_KEY for at least one of " + ", ".join(ROLES) + ".")
    return {"founder_id": int(raw), "site": site, "bots": bots, "missing": missing,
            "skipped": skipped, "cwd": cwd, "bot_wakes": bot_wakes}


# -- state -------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("roles"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"roles": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Plain UTF-8, never \\u escapes: a session that reads a topic from here
    # must copy it as written (ops#318, a Hebrew topic posted as "\\u05db...").
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# -- the agent process ---------------------------------------------------------

def build_command(kind: str, role: str, session_id: str, bot: bool = False) -> list[str]:
    """The argv for a spawn or a resume. Only the role's model, our own uuid
    and, for a bot turn, the integer max-turns from AGENT are substituted; a
    model not in the table is refused."""
    model = AGENT["models"][role]
    uuid.UUID(session_id)  # raises on anything that is not one of ours
    parts = AGENT[kind] + (AGENT["bot_args"] if bot else [])
    max_turns = int(AGENT["bot_max_turns"])
    return [part.format(model=model, session_id=session_id, max_turns=max_turns)
            for part in parts]


def _proc_start_time(pid: int) -> int | None:
    """When the process with this pid was created, in the OS's own units, or
    None if it does not exist or cannot be read. Stored with the pid, it tells
    our process from another one that got the same pid later."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    try:  # Linux: field 22 of /proc/<pid>/stat, after the "(comm)" that may hold spaces
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(stat.rpartition(")")[2].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class SpawnError(RuntimeError):
    """The agent process could not be started. The message says what failed
    and how to fix it; the wake loop logs it and goes on."""


def resolve_program(argv: list[str]) -> list[str]:
    """``argv`` with ``argv[0]`` as a full path. On Windows ``claude`` is an
    npm ``claude.cmd`` shim, and CreateProcess finds a .cmd only by its full
    path. Only argv[0] changes: the message text is never in argv (a .cmd runs
    through cmd.exe, which parses its arguments), and there is no shell=True."""
    found = shutil.which(argv[0])
    if found is None:
        raise SpawnError(f"cannot start {argv[0]!r}: it is not on PATH. Install it "
                         f"(npm install -g @anthropic-ai/claude-code) or add its "
                         f"folder to PATH, then restart wake.py.")
    return [found, *argv[1:]]


class Spawner:
    """Starts the agent with the prompt on stdin. ``alive`` and ``stop`` take
    a pid and its create time so they still work after a restart, when no
    handle is left: a pid whose create time does not match is someone else's,
    counts as dead and is never killed."""

    def __init__(self, log_dir: Path = STATE_DIR):
        self.log_dir = log_dir
        self.procs: dict[int, subprocess.Popen] = {}

    def start(self, argv: list[str], prompt: str, role: str, cwd: str | None = None) -> int:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        argv = resolve_program(argv)
        with open(self.log_dir / f"{role}.log", "ab") as log:
            try:
                proc = subprocess.Popen(argv, cwd=cwd or AGENT["cwd"], stdin=subprocess.PIPE,
                                        stdout=log, stderr=subprocess.STDOUT)
            except OSError as exc:
                raise SpawnError(f"cannot start {argv[0]!r} for {role}: {exc}. Check that "
                                 f"it runs by hand and that the cwd exists.") from exc
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass  # it died at once; alive() says so and the log says why
        self.procs[proc.pid] = proc
        return proc.pid

    def created(self, pid: int) -> int | None:
        return _proc_start_time(pid)

    def _ours(self, pid: int, created: int | None) -> bool:
        """No handle (a restart): the pid is ours only if its create time
        still matches the one stored at spawn."""
        return created is not None and _proc_start_time(pid) == created

    def alive(self, pid: int | None, created: int | None = None) -> bool:
        if not pid:
            return False
        proc = self.procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        return self._ours(pid, created) and _hook()._pid_alive(pid)

    def conversation_exists(self, session_id: str) -> bool:
        """Whether the agent ever made this conversation. A spawn that never
        ran leaves none, and a ``--resume`` of it fails (ops#318). Unknown
        (no pattern, or no conversation folder at all): True."""
        pattern = AGENT.get("conversations")
        if not pattern:
            return True
        pattern = pattern.format(session_id=str(uuid.UUID(session_id)))
        root = Path(pattern.split("*")[0])
        if not root.is_dir():
            return True
        return bool(glob.glob(pattern))

    def stop(self, pid: int, created: int | None = None) -> None:
        proc = self.procs.get(pid)
        try:
            if proc is not None:
                proc.terminate()
            elif self._ours(pid, created):
                import signal
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _any_pid_alive(pid: int) -> bool:
    return _hook()._pid_alive(pid)


def registry_live(role: str, alive: Callable[[int], bool]) -> int | None:
    """Pid of a session the founder opened by hand under this role's name, if
    one is alive. Its own hook delivers the message; we do nothing."""
    sessions = _hook().SESSIONS
    if not sessions.exists():
        return None
    for path in sessions.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(rec.get("name") or "").upper() == role and alive(int(rec.get("pid") or 0)):
            return int(rec["pid"])
    return None


# -- prompts -----------------------------------------------------------------

def _where(msg: dict[str, Any], role: str) -> tuple[str, str]:
    if msg.get("type") == "stream":
        return str(msg.get("display_recipient") or ""), str(msg.get("subject") or "")
    # zulip.py sends to streams only: a DM is answered in the role's own stream.
    return role.lower(), "direct message"


def own_stream(role: str) -> str:
    """The role's own stream (``#cto`` for the CTO). There the founder's
    untagged follow-up in an open session's topic goes to that session."""
    return AGENT["home_streams"].get(role, role.lower())


def _quote(text: str) -> str:
    """A double-quoted argument with the text as plain UTF-8: never a \\u
    escape, never cut (ops#318). Only quotes and backslashes are escaped."""
    return json.dumps(text, ensure_ascii=False)


def _send_hint(role: str, stream: str, topic: str) -> str:
    return (f'python "{ZULIP_CLI}" --as {role} send --stream {_quote(stream)} '
            f'--topic {_quote(topic)} --text "..."')


def _who(msg: dict[str, Any]) -> str:
    return str(msg.get("sender_full_name") or msg.get("sender_email") or msg.get("sender_id"))


def history_block(stream: str, topic: str, history: list[dict[str, Any]]) -> str:
    """The topic's last messages before the new one: the handoff (ops#293 4b)."""
    if not history:
        return ""
    lines = "\n".join(f"- {_who(m)}: {m.get('content', '')}" for m in history)
    return (f"The last {len(history)} messages in #{stream} › {topic} before this one, "
            f"oldest first. A previous session's close line among them is your handoff: "
            f"read the file or issue it names.\n\n{lines}\n\n")


def message_prompt(role: str, msg: dict[str, Any], first: bool,
                   history: str = "", bot: str | None = None) -> str:
    """``bot``: the role whose bot sent ``msg``, or None for the founder. A
    bot's message starts with the marker line (ops#321)."""
    stream, topic = _where(msg, role)
    sender = msg.get("sender_full_name") or msg.get("sender_email") or "the founder"
    origin = ("a direct message" if msg.get("type") != "stream"
              else f"#{stream} › {topic}")
    who = ("You are the founder's PA (the CLAUDE.md in this folder says how you work)"
           if role == "PA" else f"You are the {role} (read agents/{role.lower()}.md)")
    caller = (f"The {bot} bot called you from Zulip; this session was opened for that "
              f"and closes 10 minutes after its last message. A bot wake never stands "
              f"in for a founder decision" if bot else
              "The founder called you from Zulip; this session was opened for "
              "that and closes when he goes quiet")
    head = f"{who}. {caller}.\n\n{history}" if first else ""
    marker = BOT_MARKER.format(src=bot) + "\n\n" if bot else ""
    return (f"{marker}{head}Message from {sender} in {origin}:\n\n{msg.get('content', '')}\n\n"
            f"Answer in #{stream} › {topic} with:\n  {_send_hint(role, stream, topic)}\n"
            f"Copy the stream and topic exactly as written here.\n")


def close_prompt(role: str, stream: str, topic: str) -> str:
    return (f"Idle close: this {role} session ends now. Post exactly one line in "
            f"#{stream} › {topic} — what was agreed, and where it is written (file, issue "
            f"or commit) — with:\n  {_send_hint(role, stream, topic)}\nThen stop.")


def mentions(role: str, content: str) -> bool:
    """An explicit ``@**ROLE**`` (or ``@**ROLE|id**``) in the text. A silent
    ``@_**ROLE**`` or a wildcard does not count."""
    return re.search(rf"(?<!_)@\*\*{re.escape(role)}(\|\d+)?\*\*", content,
                     re.IGNORECASE) is not None


# -- the decisions -----------------------------------------------------------

History = Callable[[str, str, str, int], list[dict[str, Any]]]


class Waker:
    """Every decision, with Zulip, the clock and processes injected so the
    tests drive it with fakes. Owns the state; ``save`` after each change.
    ``history(role, stream, topic, n)`` returns the topic's newest ``n``
    messages, oldest first (None: no handoff, as in a dry run)."""

    def __init__(self, founder_id: int, state_path: Path, spawner: Any,
                 clock: Callable[[], float] = time.time, dry_run: bool = False,
                 emit: Callable[[dict[str, Any]], None] = lambda d: None,
                 history: History | None = None, cwd: dict[str, str] | None = None,
                 bot_wakes: bool = False, bot_ids: dict[int, str] | None = None,
                 post: Callable[[str, str, str, str], None] | None = None):
        self.founder_id = founder_id
        self.bot_wakes = bot_wakes
        # {sender_id: role} of our own role bots, read from Zulip (ops#321).
        self.bot_ids: dict[int, str] = dict(bot_ids or {})
        # post(role, stream, topic, text) as that role's bot: the stop line.
        self.post = post
        self.state_path = state_path
        self.state = load_state(state_path)
        self.spawner = spawner
        self.clock = clock
        self.dry_run = dry_run
        self.emit = emit
        self.history = history
        self.cwd = cwd or {}

    def role(self, role: str) -> dict[str, Any]:
        return self.state["roles"].setdefault(role, {})

    def save(self) -> None:
        if not self.dry_run:
            save_state(self.state_path, self.state)

    def _alive(self, sess: dict[str, Any]) -> bool:
        return self.spawner.alive(sess.get("pid"), sess.get("pid_created"))

    def _decide(self, role: str, action: str, reason: str, msg: dict[str, Any] | None,
                **extra: Any) -> dict[str, Any]:
        d = {"role": role, "action": action, "reason": reason, **extra}
        if msg is not None:
            src = self._bot_of(msg)
            if src:
                d.setdefault("origin", f"bot:{src}")
            d["message_id"] = msg.get("id")
            if msg.get("type") == "stream":
                d["stream"], d["topic"] = msg.get("display_recipient"), msg.get("subject")
        self.emit(d)
        return d

    def on_message(self, role: str, msg: dict[str, Any], flags: list[str]) -> dict[str, Any]:
        """One message from this role bot's queue. Every message gets exactly
        one decision; ``last_message_id`` makes a resumed queue or a backfill
        idempotent."""
        rs = self.role(role)
        mid = int(msg.get("id") or 0)
        if mid and mid <= int(rs.get("last_message_id") or 0):
            return self._decide(role, "skip", "already handled (resume)", msg)
        if mid:
            rs["last_message_id"] = mid
        try:
            d = self._route(role, msg, flags)
        except SpawnError as exc:
            sess = rs.get("session")
            if sess and not sess.get("pid"):
                rs["session"] = None  # a fresh session that never started
            print(f"wake: {role}: {exc}; message {mid} skipped", file=sys.stderr)
            d = self._decide(role, "error", f"spawn failed: {exc}", msg)
        self.save()
        return d

    def _stale(self, sess: dict[str, Any]) -> bool:
        """A session whose process never started, or whose conversation does
        not exist, cannot be resumed: drop it (ops#318). Asked only when no
        turn runs, since a running spawn may not have written it yet."""
        if sess.get("dry"):
            return False
        if not sess.get("pid"):
            return True
        exists = getattr(self.spawner, "conversation_exists", None)
        return exists is not None and not exists(sess["id"])

    def _drop_stale(self, role: str) -> list[dict[str, Any]]:
        """Forget the role's session if it is stale; messages queued on it
        open a fresh one."""
        sess = self.role(role).get("session")
        if not sess or self._alive(sess) or not self._stale(sess):
            return []
        return self._finish(role, f"session {sess['id']} never started or has no "
                            "conversation; dropped, not resumed")

    def _bot_of(self, msg: dict[str, Any]) -> str | None:
        """The role whose bot sent ``msg``, by ``sender_id`` only; None for the
        founder or anyone else."""
        sender = msg.get("sender_id")
        if not self.bot_wakes or sender == self.founder_id or not isinstance(sender, int):
            return None
        return self.bot_ids.get(sender)

    def _route(self, role: str, msg: dict[str, Any], flags: list[str]) -> dict[str, Any]:
        if msg.get("sender_id") == self.founder_id:
            return self._route_founder(role, msg, flags)
        src = self._bot_of(msg)
        if src is not None:
            return self._route_bot(role, src, msg, flags)
        who = msg.get("sender_email") or msg.get("sender_id")
        off = "" if self.bot_wakes else \
            "; bot wakes are off (BOT_WAKES=on in bot/.env turns them on)"
        return self._decide(role, "skip", f"sender {who} is not the founder{off}", msg)

    # -- bot wakes (ops#321) --

    def _bots(self) -> dict[str, Any]:
        b = self.state.setdefault("bots", {})
        b.setdefault("log", [])
        b.setdefault("topic_reset", {})
        b.setdefault("topic_stopped", {})
        return b

    @staticmethod
    def _topic_key(msg: dict[str, Any]) -> str:
        # Zulip topics are case-insensitive: Handoff and handoff are one topic.
        return f"{msg.get('display_recipient')}\n{str(msg.get('subject')).casefold()}"

    def _founder_resets(self, msg: dict[str, Any]) -> None:
        """A founder message in a topic resets its bot-turn count and lifts
        its stop."""
        if msg.get("type") != "stream" or "bots" not in self.state:
            return
        b, key = self._bots(), self._topic_key(msg)
        b["topic_reset"][key] = self.clock()
        b["topic_stopped"].pop(key, None)

    def _bot_cap(self, role: str, src: str, msg: dict[str, Any],
                 spawn: bool) -> dict[str, Any] | None:
        """The skip decision if a cap holds, else None and the turn is counted."""
        now, b, key = self.clock(), self._bots(), self._topic_key(msg)
        b["log"] = log = [e for e in b["log"] if now - e["at"] < DAY]
        # A reset older than the hour window changes no count.
        b["topic_reset"] = {k: t for k, t in b["topic_reset"].items() if now - t < HOUR}
        since = max(now - HOUR, b["topic_reset"].get(key, 0.0))
        in_topic = sum(1 for e in log if e["topic"] == key and e["at"] >= since)
        if key in b["topic_stopped"] or in_topic >= BOT_TOPIC_TURNS_PER_HOUR:
            posted = False
            if key not in b["topic_stopped"]:
                b["topic_stopped"][key] = now
                posted = self._post_stop(role, msg)
            return self._decide(role, "skip", f"bot loop stopped in this topic "
                                f"({BOT_TOPIC_TURNS_PER_HOUR} bot turns in an hour); a "
                                f"founder message here resets it", msg, stop_line=posted)
        if len(log) >= BOT_TURNS_PER_DAY:
            return self._decide(role, "skip", f"{BOT_TURNS_PER_DAY} bot turns today, "
                                "the daily cap; founder wakes still work", msg)
        if spawn:
            spawns = [e["at"] for e in log if e["role"] == role and e["spawn"]]
            if sum(1 for t in spawns if now - t < HOUR) >= BOT_ROLE_SPAWNS_PER_HOUR:
                return self._decide(role, "skip", f"{BOT_ROLE_SPAWNS_PER_HOUR} bot spawns of "
                                    f"{role} in the last hour, the cap", msg)
            if len(spawns) >= BOT_ROLE_SPAWNS_PER_DAY:
                return self._decide(role, "skip", f"{BOT_ROLE_SPAWNS_PER_DAY} bot spawns of "
                                    f"{role} today, the cap", msg)
        log.append({"at": now, "role": role, "from": src, "topic": key, "spawn": spawn})
        return None

    def _post_stop(self, role: str, msg: dict[str, Any]) -> bool:
        if self.post is None or self.dry_run:
            return False
        stream, topic = _where(msg, role)
        try:
            self.post(role, stream, topic, BOT_STOP_LINE.format(founder_id=self.founder_id))
            return True
        except (ZulipError, OSError, ValueError) as exc:
            print(f"wake: {role}: cannot post the stop line ({exc})", file=sys.stderr)
            return False

    def _route_bot(self, role: str, src: str, msg: dict[str, Any],
                   flags: list[str]) -> dict[str, Any]:
        """A role bot's message: only an explicit @**ROLE** in a stream, by
        the matrix, under the caps. It never moves ``last_founder_at``."""
        if role == "PA":
            return self._decide(role, "skip", "only the founder wakes the PA", msg)
        if src == role:
            return self._decide(role, "skip", f"the {role} bot never wakes its own role", msg)
        if role not in AGENT["bot_wakes"].get(src, ()):
            return self._decide(role, "skip", f"the {src} bot may not wake {role} "
                                "(AGENT['bot_wakes'])", msg)
        if msg.get("type") != "stream":
            return self._decide(role, "skip", "a bot wakes a role only by @-mention in a "
                                "stream, never by DM", msg)
        if "mentioned" not in flags or not mentions(role, str(msg.get("content") or "")):
            return self._decide(role, "skip", f"no @**{role}** in a bot's message", msg)
        self._drop_stale(role)
        hand = registry_live(role, _any_pid_alive)
        if hand:
            return self._decide(role, "skip", f"live {role} session (pid {hand}); "
                                "its hook delivers the message", msg)
        sess = self.role(role).get("session")
        # Queued behind a running turn counts as no spawn; if that session then
        # closes, _finish spawns it outside the per-role spawn cap. The topic
        # and daily caps still bound it.
        capped = self._bot_cap(role, src, msg, spawn=not sess or bool(sess.get("closing")))
        if capped:
            return capped
        return self._deliver(role, msg, founder=False)

    # -- the founder --

    def _route_founder(self, role: str, msg: dict[str, Any],
                       flags: list[str]) -> dict[str, Any]:
        self._founder_resets(msg)
        self._drop_stale(role)
        rs = self.role(role)
        sess = rs.get("session")
        now = self.clock()
        home = AGENT["home_streams"].get(role)
        in_home = bool(home) and msg.get("type") == "stream" and \
            msg.get("display_recipient") == home
        if "mentioned" not in flags and msg.get("type") != "private" and not in_home:
            in_topic = bool(sess) and not sess.get("closing") and \
                _where(msg, role) == (sess.get("stream"), sess.get("topic"))
            if not in_topic:
                return self._decide(role, "skip", f"not a mention of or a DM to {role}", msg)
            if msg.get("display_recipient") != own_stream(role):
                # The founder is still in the conversation's topic: not idle.
                sess["last_founder_at"] = now
                return self._decide(role, "skip", f"not a mention of {role}; extends the "
                                    "open session's idle timer, not delivered", msg)
            # In the role's own stream, an untagged follow-up in the open
            # session's topic goes to that session (ops#318); it opens none.
        hand = registry_live(role, _any_pid_alive)
        if hand:
            return self._decide(role, "skip", f"live {role} session (pid {hand}); "
                                "its hook delivers the message", msg)
        return self._deliver(role, msg, founder=True)

    def _deliver(self, role: str, msg: dict[str, Any], founder: bool) -> dict[str, Any]:
        """Spawn, resume or queue: the founder's message, or a bot's that
        passed the gate. Only the founder's moves ``last_founder_at``."""
        sess = self.role(role).get("session")
        if sess and sess.get("closing"):
            if not self._alive(sess):
                # Nothing is running for it: forget it and start fresh now,
                # never a close turn on a session someone is talking to.
                self._finish(role, "a message came to a closing session; closed")
                return self._spawn(role, [msg])
            sess.setdefault("pending", []).append(msg)
            return self._decide(role, "queue", "the session is closing; a fresh one "
                                "opens with this message", msg)
        if sess:
            if founder:
                sess["last_founder_at"] = self.clock()
            if self._alive(sess) or sess.get("dry"):
                sess.setdefault("pending", []).append(msg)
                return self._decide(role, "queue", f"{role} session is answering "
                                    f"(pid {sess.get('pid') or 'dry run'}); delivered when the turn ends", msg)
            self._run(role, "resume", message_prompt(role, msg, first=False,
                                                     bot=self._bot_of(msg)),
                      bot=self._bot_turn(sess, [msg]))
            stream, topic = _where(msg, role)
            sess["stream"], sess["topic"] = stream, topic
            return self._decide(role, "resume", f"open {role} session, turn ended",
                                msg, session_id=sess["id"])
        return self._spawn(role, [msg])

    def _handoff(self, role: str, msgs: list[dict[str, Any]]) -> tuple[str, int | str]:
        """The topic's last HISTORY_MESSAGES messages before ``msgs``, as a
        prompt block, and how many (or why none)."""
        if self.history is None:
            return "", 0
        stream, topic = _where(msgs[0], role)
        new = {m.get("id") for m in msgs}
        first = min(int(m.get("id") or 0) for m in msgs) or None
        try:
            rows = self.history(role, stream, topic, HISTORY_MESSAGES + len(msgs))
        except (ZulipError, OSError, ValueError) as exc:
            return f"(The topic's history could not be read: {exc}.)\n\n", f"error: {exc}"
        rows = [m for m in rows if m.get("id") not in new
                and (first is None or int(m.get("id") or 0) < first)]
        rows = rows[-HISTORY_MESSAGES:]
        return history_block(stream, topic, rows), len(rows)

    def _bot_turn(self, sess: dict[str, Any], msgs: list[dict[str, Any]]) -> bool:
        """A bot-opened session's turn that carries no founder message runs
        with max-turns; the founder's turns keep today's limits."""
        return bool(sess.get("origin")) and all(self._bot_of(m) for m in msgs)

    def _spawn(self, role: str, msgs: list[dict[str, Any]]) -> dict[str, Any]:
        now = self.clock()
        stream, topic = _where(msgs[-1], role)
        sid = str(uuid.uuid4())
        bots = [self._bot_of(m) for m in msgs]
        sess = {"id": sid, "stream": stream, "topic": topic, "started_at": now,
                "last_founder_at": now, "pid": None}
        if all(bots):  # opened by a bot: no founder time, its own idle rule
            sess.update(origin=f"bot:{bots[0]}", last_founder_at=None)
        self.role(role)["session"] = sess
        block, count = self._handoff(role, msgs)
        prompt = "\n---\n".join(message_prompt(role, m, first=(i == 0),
                                               history=block if i == 0 else "", bot=bots[i])
                                for i, m in enumerate(msgs))
        bot = self._bot_turn(sess, msgs)
        self._run(role, "spawn", prompt, bot=bot)
        why = f"the {bots[0]} bot called {role}" if bot else f"founder called {role}"
        return self._decide(role, "spawn", f"{why}; no live session",
                            msgs[-1], session_id=sid, model=AGENT["models"][role],
                            cwd=self.cwd.get(role, AGENT["cwd"]), history=count,
                            command=build_command("spawn", role, sid, bot=bot))

    def _run(self, role: str, kind: str, prompt: str, bot: bool = False,
             delivers: bool = True) -> None:
        sess = self.role(role)["session"]
        argv = build_command(kind, role, sess["id"], bot=bot)
        if delivers:
            sess["last_delivered_at"] = self.clock()
        if self.dry_run:
            sess["dry"] = True  # as if its turn were running: later messages queue
            return
        self.save()  # the session is on disk before its process exists: no double spawn
        try:
            sess["pid"] = self.spawner.start(argv, prompt, role, self.cwd.get(role))
        except SpawnError:
            sess["pid"] = sess["pid_created"] = None
            raise
        sess["pid_created"] = self.spawner.created(sess["pid"])

    def _close_turn(self, role: str, sess: dict[str, Any], now: float) -> None:
        sess["close_turn_at"] = now
        self._run(role, "resume", close_prompt(role, sess["stream"], sess["topic"]),
                  bot=bool(sess.get("origin")), delivers=False)

    def _finish(self, role: str, reason: str) -> list[dict[str, Any]]:
        """Forget the session; a message that came in meanwhile opens a fresh one."""
        rs = self.role(role)
        pending = (rs.get("session") or {}).get("pending") or []
        rs["session"] = None
        out = [self._decide(role, "closed", reason, None)]
        if pending:
            out.append(self._spawn(role, pending))
        self.save()
        return out

    def _tick_closing(self, role: str, sess: dict[str, Any], running: bool,
                      now: float) -> list[dict[str, Any]]:
        if sess.get("close_turn_at") is None:
            # A turn stopped at 4 h: its process may still hold the session id,
            # so the close turn waits until it has exited.
            if not running:
                self._close_turn(role, sess, now)
                self.save()
                return []
            if now - sess.get("stopped_at", now) < CLOSE_TURN_SECONDS:
                return []
            return self._finish(role, "the stopped turn did not exit in 2 min; "
                                "closed without a close line")
        if not running:
            return self._finish(role, "close turn done")
        if now - sess["close_turn_at"] < CLOSE_TURN_SECONDS:
            return []
        self.spawner.stop(sess["pid"], sess.get("pid_created"))
        return self._finish(role, "the close turn ran past 2 min; stopped")

    def tick(self) -> list[dict[str, Any]]:
        """Deliver queued messages when a turn ends; close idle or old sessions."""
        out = []
        now = self.clock()
        for role, rs in list(self.state["roles"].items()):
            try:
                out += self._tick_role(role, rs, now)
            except SpawnError as exc:
                print(f"wake: {role}: {exc}; session dropped", file=sys.stderr)
                rs["session"] = None
                out.append(self._decide(role, "error", f"spawn failed: {exc}", None))
                self.save()
        return out

    @staticmethod
    def _idle(sess: dict[str, Any], now: float) -> str | None:
        """Why the session is idle, or None. The founder's session: 15 min
        after his last message. A bot-opened one (ops#321): 10 min after its
        last delivered message, and 15 after his if he wrote in it."""
        founder_at = sess.get("last_founder_at")
        if founder_at is not None and now - founder_at < IDLE_SECONDS:
            return None
        if sess.get("origin"):
            last = sess.get("last_delivered_at") or sess["started_at"]
            if now - last >= BOT_IDLE_SECONDS:
                return "10 min since the last message of a bot-opened session"
            return None
        return "15 min without the founder"

    def _tick_role(self, role: str, rs: dict[str, Any], now: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        sess = rs.get("session")
        if not sess or sess.get("dry"):
            return out
        running = self._alive(sess)
        if not running and self._stale(sess):
            return self._drop_stale(role)
        if sess.get("closing"):
            out += self._tick_closing(role, sess, running, now)
            return out
        age = now - sess["started_at"]
        idle = None if running else self._idle(sess, now)
        if age >= MAX_AGE_SECONDS or idle:
            sess.update(closing=True, pending=sess.get("pending") or [])
            if running:  # 4 h: stop the turn; the close turn runs once it has exited
                self.spawner.stop(sess["pid"], sess.get("pid_created"))
                sess["stopped_at"] = now
            else:
                self._close_turn(role, sess, now)
            why = "4 h of age" if age >= MAX_AGE_SECONDS else idle
            out.append(self._decide(role, "close", why, None, session_id=sess["id"]))
            self.save()
            return out
        pending = sess.get("pending") or []
        if pending and not running:
            sess["pending"] = []
            prompt = "\n---\n".join(message_prompt(role, m, first=False, bot=self._bot_of(m))
                                    for m in pending)
            stream, topic = _where(pending[-1], role)
            sess["stream"], sess["topic"] = stream, topic
            self._run(role, "resume", prompt, bot=self._bot_turn(sess, pending))
            out.append(self._decide(role, "resume", "queued messages after the turn",
                                    pending[-1], session_id=sess["id"]))
            self.save()
        return out

    def status(self) -> dict[str, Any]:
        roles = {}
        for role in ROLES:
            rs = self.state["roles"].get(role, {})
            sess = rs.get("session")
            hand = registry_live(role, _any_pid_alive)
            roles[role] = {
                "live": bool(hand) or bool(sess and self._alive(sess)),
                "opened_by": "hand" if hand else ((sess.get("origin") or "wake") if sess else None),
                "session_id": sess.get("id") if sess else None,
                "topic": f"#{sess['stream']} › {sess['topic']}" if sess else None,
                "last_event_id": rs.get("last_event_id"),
                "last_message_id": rs.get("last_message_id"),
            }
        return {"ok": True, "roles": roles}


# -- Zulip: queues, resume, backfill ----------------------------------------

def _queue_gone(exc: ZulipError) -> bool:
    return "BAD_EVENT_QUEUE_ID" in str(exc) or "Bad event queue" in str(exc)


def backfill(client: ZulipClient, after: int | None, newest: int = 20,
             home: str | None = None) -> list[tuple[dict[str, Any], list[str]]]:
    """Messages after ``after`` that mention this bot or are DMs to it (or
    are in its ``home`` stream), in order, with the flags a queue event would
    carry. What a lost queue missed. ``after=None``: the newest ``newest`` of
    them instead (a first dry run)."""
    found: dict[int, tuple[dict[str, Any], list[str]]] = {}
    if after is None:
        window = {"anchor": "newest", "num_before": newest, "num_after": 0}
    else:
        window = {"anchor": after, "include_anchor": "false",
                  "num_before": 0, "num_after": BACKFILL_LIMIT}
    narrows = [([{"operator": "is", "operand": "mentioned"}], "mentioned"),
               ([{"operator": "is", "operand": "dm"}], None)]
    if home:
        narrows.append(([{"operator": "stream", "operand": home}], None))
    for narrow, flag in narrows:
        payload = client._request("GET", "messages", {
            "narrow": narrow, "apply_markdown": "false", **window})
        for m in payload.get("messages", []):
            if after is None or m["id"] > after:
                flags = list(m.get("flags") or [])
                if flag and flag not in flags:
                    flags.append(flag)
                prev = found.get(m["id"])
                found[m["id"]] = (m, sorted(set(flags) | set(prev[1] if prev else [])))
    pairs = [found[k] for k in sorted(found)]
    return pairs[-newest:] if after is None else pairs


def topic_history(client: ZulipClient, stream: str, topic: str, n: int
                  ) -> list[dict[str, Any]]:
    """The newest ``n`` messages in a stream's topic, oldest first."""
    payload = client._request("GET", "messages", {
        "narrow": [{"operator": "stream", "operand": stream},
                   {"operator": "topic", "operand": topic}],
        "anchor": "newest", "num_before": n, "num_after": 0, "apply_markdown": "false"})
    return sorted(payload.get("messages", []), key=lambda m: m["id"])


def open_queue(client: ZulipClient, rs: dict[str, Any], home: str | None = None
               ) -> tuple[list[tuple[dict[str, Any], list[str]]], list[dict[str, Any]]]:
    """Resume the saved queue, or register a new one. Returns (backfilled
    messages, first events). A saved queue that expired is replaced and the
    gap read from message history after ``last_message_id``; a first start
    begins at the newest message, so old mentions spawn nothing."""
    if rs.get("queue_id"):
        try:
            events = client.get_events(rs["queue_id"], rs["last_event_id"], dont_block=True)
            return [], events
        except ZulipError as exc:
            if not _queue_gone(exc):
                raise
    reg = client.register(event_types=["message"])
    rs["queue_id"], rs["last_event_id"] = reg["queue_id"], reg["last_event_id"]
    if rs.get("last_message_id") is None:
        rs["last_message_id"] = reg.get("max_message_id", 0)
        return [], []
    return backfill(client, int(rs["last_message_id"]), home=home), []


def _message_pairs(events: list[dict[str, Any]]):
    for e in events:
        if e.get("type") == "message" and isinstance(e.get("message"), dict):
            yield e["message"], list(e.get("flags") or [])


def process_batch(waker: Waker, role: str, events: list[dict[str, Any]]) -> None:
    rs = waker.role(role)
    for msg, flags in _message_pairs(events):
        waker.on_message(role, msg, flags)
    rs["last_event_id"] = advance_last_event_id(events, int(rs.get("last_event_id", -1)))
    waker.save()


def _poller(role: str, client: ZulipClient, queue_id: str, last: int,
            out: queue.Queue) -> None:
    while True:
        try:
            events = client.get_events(queue_id, last)
        except Exception as exc:  # noqa: BLE001 - handed to the main loop
            out.put((role, queue_id, exc))
            return
        last = advance_last_event_id(events, last)
        if events:
            out.put((role, queue_id, events))


def open_until_up(start: Callable[[str], None], role: str, wait: Callable[[], None],
                  sleep: Callable[[float], None] = time.sleep) -> None:
    """``start(role)`` until Zulip answers. ``wait`` runs on every retry (the
    tick and the lock), so idle close and the 4 h cap still hold while Zulip
    is down."""
    while True:
        try:
            return start(role)
        except (ZulipError, OSError, ValueError) as exc:
            print(f"wake: {role} cannot open its queue ({exc}); retrying in 30 s")
            wait()
            sleep(30)


def resolve_bot_ids(clients: dict[str, Any], waker: Waker, wait: Callable[[], None],
                    sleep: Callable[[float], None] = time.sleep) -> None:
    """Every role bot's user id, before any queue opens: a backfill read
    before the sender's id is known would route a bot's message as "not the
    founder" and move past it. Who is a bot is read from Zulip, never from
    text (ops#321). Retries like the queue open while Zulip is down."""
    if not waker.bot_wakes:
        return

    def resolve(role: str) -> None:
        waker.bot_ids[int(clients[role].own_user()["user_id"])] = role

    for role in clients:
        open_until_up(resolve, role, wait, sleep)


def announce_skipped(cfg: dict[str, Any], waker: Waker) -> None:
    for role, reason in cfg.get("skipped", {}).items():
        waker._decide(role, "skip", reason, None)


def run(cfg: dict[str, Any], waker: Waker) -> int:
    hook = _hook()
    lock = STATE_DIR / "wake.lock"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not hook._take_lock(lock):
        print("wake: another wake.py already runs (bot/.wake/wake.lock); nothing to do.")
        return 0
    inbox: queue.Queue = queue.Queue()
    clients = {r: ZulipClient(cfg["site"], e, k) for r, (e, k) in cfg["bots"].items()}

    def start(role: str) -> None:
        rs = waker.role(role)
        pairs, events = open_queue(clients[role], rs, own_stream(role))
        for msg, flags in pairs:
            waker.on_message(role, msg, flags)
        process_batch(waker, role, events)
        threading.Thread(target=_poller, daemon=True, name=f"wake-{role}",
                         args=(role, clients[role], rs["queue_id"], rs["last_event_id"],
                               inbox)).start()
        print(f"wake: {role} listening (queue {rs['queue_id']}, event {rs['last_event_id']})")

    def wait() -> None:
        hook._hold_lock(lock)
        waker.tick()

    def start_until_up(role: str) -> None:
        # Zulip down (it boots beside us in up.cmd, or restarted): keep trying.
        open_until_up(start, role, wait)

    waker.history = lambda role, stream, topic, n: topic_history(clients[role], stream, topic, n)
    waker.post = lambda role, stream, topic, text: clients[role].send_message(stream, text, topic)
    announce_skipped(cfg, waker)
    try:
        resolve_bot_ids(clients, waker, wait)  # every id before the first backfill
        for role in clients:
            start_until_up(role)
        for role in cfg["missing"]:
            print(f"wake: {role} has no bot credentials in bot/.env; not listening for it")
        while True:
            hook._hold_lock(lock)
            try:
                role, qid, item = inbox.get(timeout=TICK_SECONDS)
            except queue.Empty:
                role = None
            if role is not None:
                if isinstance(item, Exception):
                    print(f"wake: {role} poll failed ({item}); reopening in 5 s")
                    time.sleep(5)
                    start_until_up(role)  # the saved queue if it lives, else a new one + backfill
                elif qid == waker.role(role).get("queue_id"):
                    process_batch(waker, role, item)
            waker.tick()
    except KeyboardInterrupt:
        return 0
    finally:
        hook._drop_lock(lock)


def dry_run(cfg: dict[str, Any], waker: Waker) -> int:
    """What the waiting messages would do, from history after each role's
    ``last_message_id`` (the last 20 mentions and DMs on a first run).
    Registers nothing, saves nothing, spawns nothing."""
    announce_skipped(cfg, waker)
    clients = {r: ZulipClient(cfg["site"], e, k, timeout=30.0) for r, (e, k) in cfg["bots"].items()}
    if waker.bot_wakes:
        waker.bot_ids.update({int(c.own_user()["user_id"]): r for r, c in clients.items()})
    for role, client in clients.items():
        rs = waker.role(role)
        after = rs.get("last_message_id")
        pairs = backfill(client, None if after is None else int(after),
                         home=own_stream(role))
        for msg, flags in pairs:
            waker.on_message(role, msg, flags)
    waker.tick()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wake.py", description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print what it would do with the waiting messages; spawns nothing")
    parser.add_argument("--json", action="store_true",
                        help="JSON lines; alone, print status (live sessions, last event ids) "
                             "and exit")
    args = parser.parse_args(argv)

    def emit(d: dict[str, Any]) -> None:
        if args.json:
            print(json.dumps(d, ensure_ascii=False), flush=True)
        else:
            what = f" #{d['stream']} › {d['topic']}" if d.get("stream") else ""
            print(f"wake: {d['role']} {d['action']}{what}: {d['reason']}", flush=True)

    try:
        cfg = load_config()
    except ConfigError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    waker = Waker(cfg["founder_id"], STATE_DIR / "state.json", Spawner(),
                  dry_run=args.dry_run, emit=emit, cwd=cfg["cwd"],
                  bot_wakes=cfg["bot_wakes"])
    try:
        if args.dry_run:
            return dry_run(cfg, waker)
        if args.json:
            print(json.dumps({**waker.status(), "missing_bots": cfg["missing"],
                              "skipped": cfg["skipped"]}, ensure_ascii=False))
            return 0
        return run(cfg, waker)
    except ZulipError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
