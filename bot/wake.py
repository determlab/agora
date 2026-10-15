"""Wake: a founder @mention of a role bot opens a fresh role session (ops#293).

The founder writes ``@**CTO** <question>`` in Zulip (or DMs the CTO bot) and a
CTO session opens on its own, answers in that stream and topic, and closes
when idle. Same for COO, PM, CMO, CPSO, and PA (``@**PA**`` or any founder
message in ``#pa``; it works in ``PA_DIR``, not the ops repo). No session
sits waiting for him.

How it works:
  - One long-poll per role bot (``ZULIP_<ROLE>_EMAIL`` / ``_API_KEY``), each
    on its own thread, all feeding one loop. The queue id and
    ``last_event_id`` are kept in ``bot/.wake/state.json``; a restart resumes
    the saved queue, or re-registers and backfills by message id, so nothing
    is acted on twice and nothing is skipped (``resume.py``).
  - Only a message whose ``sender_id`` is ``FOUNDER_ZULIP_ID`` (bot/.env, never
    inferred) does anything. Every other sender is skipped and logged.
  - A role session the founder opened by hand (Claude Code's registry names it
    after the role, its pid is alive) is left alone: its Stop-hook wait
    delivers the message (hooks/agora_hook.py).
  - Otherwise the role gets one live process (``AGENT["command"]``) that reads
    the founder's messages on stdin as a stream: the first opens it, with the
    topic's last 20 messages (so the previous session's close line is the
    handoff), and later ones — a new mention, or any founder message in the
    session's topic — are written to the same process. Never ``--resume``
    (ops#293 4b): a new session starts fresh.
  - Idle close: 15 minutes after the founder's last message, once the turn has
    ended, or at 4 hours of age, the session is asked for one line in the
    topic — what was agreed and where it is written — and its stdin closed.
    A close that has not finished 2 minutes later is stopped.
  - Only a process this wake.py started and still holds is ever written to or
    stopped. After a restart the old sessions are not ours: their stdin is
    gone, so they end on their own, and the next mention opens a fresh one.

Security (type B — this starts processes from chat input): only the founder's
user id triggers; the message text goes to the process on **stdin**, never
onto a command line; the command is a fixed list (no shell) whose only
placeholder is the role's model; no new permissions and no skip-permissions
flag — the session runs under the working folder's own settings.

Usage:
  python bot/wake.py                    # run: long-poll forever (up.cmd starts it)
  python bot/wake.py --dry-run [--json] # what it would do with the waiting messages; spawns nothing
  python bot/wake.py --json             # status: which role sessions are live, last event ids

Stdlib only (D2).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
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
# ``claude``. The only placeholder is {model}; the message is never on the
# command line. "stdin": "stream-json" writes each message as one JSON user
# line and keeps stdin open between them; "text" writes one prompt and closes
# it (a CLI that cannot stream: follow-ups then wait for a fresh session).
AGENT: dict[str, Any] = {
    "command": ["claude", "-p", "--model", "{model}", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose"],
    "stdin": "stream-json",
    "cwd": os.environ.get("WAKE_OPS_DIR", r"C:\PlayGround\ops"),
    # CLAUDE.md, session rules (ops#289): COO and CTO on Opus, the rest on Sonnet.
    "models": {"COO": "opus", "CTO": "opus", "PM": "sonnet", "CMO": "sonnet",
               "CPSO": "sonnet", "PA": "sonnet"},
}
ROLES = tuple(AGENT["models"])
PA_STREAM = "pa"  # any founder message there calls the PA, no tag needed (ops#293 7)

IDLE_SECONDS = 15 * 60
MAX_AGE_SECONDS = 4 * 60 * 60
CLOSE_GRACE_SECONDS = 2 * 60
HISTORY_MESSAGES = 20
TICK_SECONDS = 30.0
BACKFILL_LIMIT = 1000


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


PA_DIR_MISSING = ("PA_DIR missing in bot/.env — add the founder's PA folder (the one "
                  "holding the PA's CLAUDE.md), e.g. PA_DIR=C:\\PlayGround\\pa")


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
    if not bots:
        raise ConfigError("no role bot credentials in bot/.env — add ZULIP_<ROLE>_EMAIL and "
                          "ZULIP_<ROLE>_API_KEY for at least one of " + ", ".join(ROLES) + ".")
    cwd = {role: AGENT["cwd"] for role in ROLES}
    cwd["PA"] = get("PA_DIR") or None
    return {"founder_id": int(raw), "site": site, "bots": bots, "missing": missing,
            "cwd": cwd}


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
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# -- the agent process ---------------------------------------------------------

def build_command(role: str) -> list[str]:
    """The argv for a role. Only its model is substituted; a role not in the
    table is refused (KeyError)."""
    model = AGENT["models"][role]
    return [part.format(model=model) for part in AGENT["command"]]


def encode_message(text: str) -> bytes:
    """One user turn on the agent's stdin."""
    if AGENT["stdin"] == "stream-json":
        line = {"type": "user", "message": {"role": "user",
                                            "content": [{"type": "text", "text": text}]}}
        return (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    return text.encode("utf-8")


class Spawner:
    """Starts the agent and keeps its stdin to write later messages to. Only
    a process started here and still held is alive, written to or stopped:
    a pid from an earlier run could belong to anything by now."""

    def __init__(self, log_dir: Path = STATE_DIR):
        self.log_dir = log_dir
        self.procs: dict[int, subprocess.Popen] = {}
        self.sent: dict[int, int] = {}
        self.done: dict[int, int] = {}

    def start(self, argv: list[str], cwd: str, role: str) -> int:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # Windows: CreateProcess finds only .exe; npm installs claude.cmd.
        exe = shutil.which(argv[0])
        if exe is None:
            raise FileNotFoundError(f"{argv[0]} is not on PATH — install it, or set "
                                    "AGENT['command'] in bot/wake.py to its full path")
        proc = subprocess.Popen([exe, *argv[1:]], cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.procs[proc.pid] = proc
        self.sent[proc.pid] = self.done[proc.pid] = 0
        threading.Thread(target=self._drain, args=(proc, role), daemon=True,
                         name=f"wake-log-{role}").start()
        return proc.pid

    def _drain(self, proc: subprocess.Popen, role: str) -> None:
        """Copy its output to bot/.wake/<ROLE>.log and count finished turns
        (a stream-json ``result`` line), so idle close never cuts a turn."""
        with open(self.log_dir / f"{role}.log", "ab") as log:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                try:
                    if json.loads(line).get("type") == "result":
                        self.done[proc.pid] += 1
                except (ValueError, AttributeError):
                    pass

    def send(self, pid: int, text: str) -> bool:
        proc = self.procs.get(pid)
        if proc is None or proc.poll() is not None or proc.stdin.closed:
            return False
        try:
            proc.stdin.write(encode_message(text))
            proc.stdin.flush()
        except OSError:
            return False
        self.sent[pid] += 1
        if AGENT["stdin"] != "stream-json":
            self.end_input(pid)
        return True

    def end_input(self, pid: int) -> None:
        proc = self.procs.get(pid)
        if proc is not None and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def alive(self, pid: int | None) -> bool:
        proc = self.procs.get(pid) if pid else None
        return proc is not None and proc.poll() is None

    def busy(self, pid: int | None) -> bool:
        return self.alive(pid) and self.sent.get(pid, 0) > self.done.get(pid, 0)

    def stop(self, pid: int) -> None:
        proc = self.procs.get(pid)
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass


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


def _send_hint(role: str, stream: str, topic: str) -> str:
    return (f'python "{ZULIP_CLI}" --as {role} send --stream {json.dumps(stream)} '
            f'--topic {json.dumps(topic)} --text "..."')


def _who(role: str) -> str:
    if role == "PA":
        return "You are the founder's PA (this folder's CLAUDE.md says how you work)."
    return f"You are the {role} (read agents/{role.lower()}.md)."


def message_prompt(role: str, msg: dict[str, Any]) -> str:
    stream, topic = _where(msg, role)
    sender = msg.get("sender_full_name") or msg.get("sender_email") or "the founder"
    origin = ("a direct message" if msg.get("type") != "stream"
              else f"#{stream} › {topic}")
    return (f"Message from {sender} in {origin}:\n\n{msg.get('content', '')}\n\n"
            f"Answer in #{stream} › {topic} with:\n  {_send_hint(role, stream, topic)}\n")


def history_block(history: list[dict[str, Any]]) -> str:
    if not history:
        return ""
    lines = [f"[{m.get('id')}] {m.get('sender_full_name') or m.get('sender_email')}: "
             f"{m.get('content', '')}" for m in history]
    return ("The topic's last messages, oldest first (a previous session's close line, "
            "if any, says what was agreed and where):\n" + "\n".join(lines) + "\n\n")


def first_prompt(role: str, msgs: list[dict[str, Any]],
                 history: list[dict[str, Any]]) -> str:
    return (f"{_who(role)} The founder called you from Zulip; this session was opened "
            "for that and closes when he goes quiet. It is a fresh session: what came "
            "before is only what is below.\n\n" + history_block(history)
            + "\n---\n".join(message_prompt(role, m) for m in msgs))


def close_prompt(role: str, stream: str, topic: str) -> str:
    return (f"Idle close: this {role} session ends now. Post exactly one line in "
            f"#{stream} › {topic} — what was agreed, and where it is written (file, issue "
            f"or commit) — with:\n  {_send_hint(role, stream, topic)}\nThen stop.")


# -- the decisions -----------------------------------------------------------

class Waker:
    """Every decision, with Zulip, the clock and processes injected so the
    tests drive it with fakes. Owns the state; ``save`` after each change."""

    def __init__(self, founder_id: int, state_path: Path, spawner: Any,
                 clock: Callable[[], float] = time.time, dry_run: bool = False,
                 emit: Callable[[dict[str, Any]], None] = lambda d: None,
                 cwd: dict[str, str | None] | None = None,
                 history: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None):
        self.founder_id = founder_id
        self.state_path = state_path
        self.state = load_state(state_path)
        self.spawner = spawner
        self.clock = clock
        self.dry_run = dry_run
        self.emit = emit
        self.cwd = cwd if cwd is not None else {r: AGENT["cwd"] for r in ROLES}
        self.history = history or (lambda role, msg: [])

    def role(self, role: str) -> dict[str, Any]:
        return self.state["roles"].setdefault(role, {})

    def save(self) -> None:
        if not self.dry_run:
            save_state(self.state_path, self.state)

    def _decide(self, role: str, action: str, reason: str, msg: dict[str, Any] | None,
                **extra: Any) -> dict[str, Any]:
        d = {"role": role, "action": action, "reason": reason, **extra}
        if msg is not None:
            d["message_id"] = msg.get("id")
            if msg.get("type") == "stream":
                d["stream"], d["topic"] = msg.get("display_recipient"), msg.get("subject")
        self.emit(d)
        return d

    def _live(self, sess: dict[str, Any] | None) -> bool:
        return bool(sess) and (self.spawner.alive(sess.get("pid")) or bool(sess.get("dry")))

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
        d = self._route(role, msg, flags)
        self.save()
        return d

    def _route(self, role: str, msg: dict[str, Any], flags: list[str]) -> dict[str, Any]:
        if msg.get("sender_id") != self.founder_id:
            who = msg.get("sender_email") or msg.get("sender_id")
            return self._decide(role, "skip", f"sender {who} is not the founder", msg)
        stream = str(msg.get("display_recipient") or "").lower()
        called = ("mentioned" in flags or msg.get("type") == "private"
                  or (role == "PA" and msg.get("type") == "stream" and stream == PA_STREAM))
        sess = self.role(role).get("session")
        live = self._live(sess)
        in_topic = live and _where(msg, role) == (sess["stream"], sess["topic"])
        if not (called or in_topic):
            return self._decide(role, "skip", f"not a mention of or a DM to {role}", msg)
        if not self.cwd.get(role):
            return self._decide(role, "skip", PA_DIR_MISSING if role == "PA"
                                else f"no working folder for {role}", msg)
        now = self.clock()
        if live and sess.get("closing_at"):
            sess.setdefault("pending", []).append(msg)
            return self._decide(role, "queue", "the session is closing; a fresh one opens "
                                "with this message", msg)
        if live:
            if not self.dry_run and not self.spawner.send(sess["pid"], message_prompt(role, msg)):
                return self._spawn(role, [msg])  # its stdin is gone: a fresh one
            sess["last_founder_at"] = now
            if called:
                sess["stream"], sess["topic"] = _where(msg, role)
            return self._decide(role, "deliver", f"to the open {role} session"
                                + ("" if called else " (founder, same topic)"), msg,
                                session_id=sess["id"])
        hand = registry_live(role, _hook()._pid_alive)
        if hand:
            return self._decide(role, "skip", f"live {role} session (pid {hand}); "
                                "its hook delivers the message", msg)
        return self._spawn(role, [msg])

    def _spawn(self, role: str, msgs: list[dict[str, Any]]) -> dict[str, Any]:
        now = self.clock()
        stream, topic = _where(msgs[-1], role)
        sess = {"id": str(uuid.uuid4()), "stream": stream, "topic": topic,
                "started_at": now, "last_founder_at": now, "pid": None}
        self.role(role)["session"] = sess
        history = [m for m in self.history(role, msgs[0])
                   if m.get("id") not in {x.get("id") for x in msgs}][-HISTORY_MESSAGES:]
        argv = build_command(role)
        d = self._decide(role, "spawn", f"founder called {role}; no live session", msgs[-1],
                         session_id=sess["id"], model=AGENT["models"][role],
                         cwd=self.cwd[role], command=argv, history=len(history))
        if self.dry_run:
            sess["dry"] = True  # as if it were running: later messages are delivered
            return d
        self.save()  # the session is on disk before its process exists
        try:
            sess["pid"] = self.spawner.start(argv, self.cwd[role], role)
        except OSError as exc:
            self.role(role)["session"] = None
            return self._decide(role, "error", f"could not start {argv[0]}: {exc}",
                                msgs[-1])
        self.spawner.send(sess["pid"], first_prompt(role, msgs, history))
        return d

    def _close(self, role: str, sess: dict[str, Any], why: str) -> dict[str, Any]:
        self.spawner.send(sess["pid"], close_prompt(role, sess["stream"], sess["topic"]))
        self.spawner.end_input(sess["pid"])
        sess["closing_at"] = self.clock()
        return self._decide(role, "close", why, None, session_id=sess["id"])

    def tick(self) -> list[dict[str, Any]]:
        """Close idle or old sessions, stop a close that hangs, forget ended ones."""
        out = []
        now = self.clock()
        for role, rs in list(self.state["roles"].items()):
            sess = rs.get("session")
            if not sess or sess.get("dry"):
                continue
            alive = self.spawner.alive(sess.get("pid"))
            if sess.get("closing_at") or not alive:
                if alive and now - sess["closing_at"] < CLOSE_GRACE_SECONDS:
                    continue
                if alive:
                    self.spawner.stop(sess["pid"])
                    out.append(self._decide(role, "stopped", "the close turn ran past "
                                            f"{CLOSE_GRACE_SECONDS // 60} min", None))
                elif sess.get("closing_at"):
                    out.append(self._decide(role, "closed", "close turn done", None))
                else:
                    out.append(self._decide(role, "ended", "the process is gone (it ended, "
                                            "or an earlier wake.py started it)", None))
                pending = sess.get("pending") or []
                rs["session"] = None
                if pending:
                    out.append(self._spawn(role, pending))
                self.save()
                continue
            age, idle = now - sess["started_at"], now - sess["last_founder_at"]
            if age >= MAX_AGE_SECONDS:
                out.append(self._close(role, sess, "4 h of age"))
            elif idle >= IDLE_SECONDS and not self.spawner.busy(sess["pid"]):
                out.append(self._close(role, sess, "15 min without the founder"))
            else:
                continue
            self.save()
        return out

    def status(self) -> dict[str, Any]:
        roles = {}
        for role in ROLES:
            rs = self.state["roles"].get(role, {})
            sess = rs.get("session")
            ours = bool(sess) and self.spawner.alive(sess.get("pid"))
            hand = None if ours else registry_live(role, _hook()._pid_alive)
            roles[role] = {
                "live": ours or bool(hand),
                "opened_by": "wake" if ours else ("hand" if hand else None),
                "session_id": sess.get("id") if ours else None,
                "topic": f"#{sess['stream']} › {sess['topic']}" if ours else None,
                "last_event_id": rs.get("last_event_id"),
                "last_message_id": rs.get("last_message_id"),
            }
        return {"ok": True, "roles": roles}


def _queue_gone(exc: ZulipError) -> bool:
    return "BAD_EVENT_QUEUE_ID" in str(exc) or "Bad event queue" in str(exc)


def backfill(client: ZulipClient, after: int | None, newest: int = 20,
             role: str | None = None) -> list[tuple[dict[str, Any], list[str]]]:
    """Messages after ``after`` that mention this bot or are DMs to it (and,
    for the PA, everything in #pa), in order, with the flags a queue event
    would carry. What a lost queue missed. ``after=None``: the newest
    ``newest`` of them instead (a first dry run). A founder follow-up in an
    open session's topic without a mention is not in it: after an outage
    that session is not ours any more anyway."""
    found: dict[int, tuple[dict[str, Any], list[str]]] = {}
    if after is None:
        window = {"anchor": "newest", "num_before": newest, "num_after": 0}
    else:
        window = {"anchor": after, "include_anchor": "false",
                  "num_before": 0, "num_after": BACKFILL_LIMIT}
    narrows = [([{"operator": "is", "operand": "mentioned"}], "mentioned"),
               ([{"operator": "is", "operand": "dm"}], None)]
    if role == "PA":
        narrows.append(([{"operator": "channel", "operand": PA_STREAM}], None))
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


def open_queue(client: ZulipClient, rs: dict[str, Any], role: str | None = None
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
    return backfill(client, int(rs["last_message_id"]), role=role), []


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


def history_fetcher(clients: dict[str, ZulipClient], founder_id: int
                    ) -> Callable[[str, dict[str, Any]], list[dict[str, Any]]]:
    """The topic's last messages before ``msg`` (or the DM thread's), read as
    that role's bot — the handoff a fresh session starts from (ops#293 4b)."""
    def fetch(role: str, msg: dict[str, Any]) -> list[dict[str, Any]]:
        if msg.get("type") == "stream":
            narrow = [{"operator": "channel", "operand": msg.get("display_recipient")},
                      {"operator": "topic", "operand": msg.get("subject")}]
        else:
            narrow = [{"operator": "dm", "operand": [founder_id]}]
        try:
            payload = clients[role]._request("GET", "messages", {
                "narrow": narrow, "anchor": msg["id"], "include_anchor": "false",
                "num_before": HISTORY_MESSAGES, "num_after": 0, "apply_markdown": "false"})
        except (ZulipError, OSError, ValueError, KeyError):
            return []  # a fresh session without context beats none at all
        return sorted(payload.get("messages", []), key=lambda m: m["id"])
    return fetch


def run(cfg: dict[str, Any], waker: Waker) -> int:
    hook = _hook()
    lock = STATE_DIR / "wake.lock"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not hook._take_lock(lock):
        print("wake: another wake.py already runs (bot/.wake/wake.lock); nothing to do.")
        return 0
    inbox: queue.Queue = queue.Queue()
    clients = {r: ZulipClient(cfg["site"], e, k) for r, (e, k) in cfg["bots"].items()}
    waker.history = history_fetcher(clients, cfg["founder_id"])

    def start(role: str) -> None:
        rs = waker.role(role)
        pairs, events = open_queue(clients[role], rs, role)
        for msg, flags in pairs:
            waker.on_message(role, msg, flags)
        process_batch(waker, role, events)
        threading.Thread(target=_poller, daemon=True, name=f"wake-{role}",
                         args=(role, clients[role], rs["queue_id"], rs["last_event_id"],
                               inbox)).start()
        print(f"wake: {role} listening (queue {rs['queue_id']}, event {rs['last_event_id']})")

    def start_until_up(role: str) -> None:
        # Zulip down (it boots beside us in up.cmd, or restarted): keep trying.
        while True:
            try:
                return start(role)
            except (ZulipError, OSError, ValueError) as exc:
                print(f"wake: {role} cannot open its queue ({exc}); retrying in 30 s")
                hook._hold_lock(lock)
                waker.tick()  # the 15 min and 4 h limits hold through an outage
                time.sleep(30)

    try:
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
    clients = {r: ZulipClient(cfg["site"], e, k, timeout=30.0)
               for r, (e, k) in cfg["bots"].items()}
    waker.history = history_fetcher(clients, cfg["founder_id"])
    for role, client in clients.items():
        rs = waker.role(role)
        after = rs.get("last_message_id")
        pairs = backfill(client, None if after is None else int(after), role=role)
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
                  dry_run=args.dry_run, emit=emit, cwd=cfg["cwd"])
    try:
        if args.dry_run:
            return dry_run(cfg, waker)
        if args.json:
            print(json.dumps({**waker.status(), "missing_bots": cfg["missing"]},
                             ensure_ascii=False))
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
