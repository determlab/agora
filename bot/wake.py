"""Wake: a founder @mention of a role bot opens a fresh role session (ops#293).

The founder writes ``@**CTO** <question>`` in Zulip (or DMs the CTO bot) and a
CTO session opens on its own, answers in that stream and topic, and closes
when idle. Same for COO, PM, CMO and CPSO. No session sits waiting for him.

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
    delivers the message (hooks/agora_hook.py). Otherwise the role gets a
    conversation: the first message spawns it (``AGENT["spawn"]``), later ones
    resume it (``AGENT["resume"]``), one message per turn, queued while a turn
    runs.
  - Idle close: 15 minutes after the founder's last message, or at 4 hours of
    age, the conversation gets one last turn — post one line in the topic,
    what was agreed and where it is written — and is forgotten. The next
    mention opens a fresh one.

Security (type B — this starts processes from chat input): only the founder's
user id triggers; the message text goes into the prompt on **stdin**, never
onto a command line; the command is a fixed list (no shell) whose only
placeholders are the role's model and a uuid this script made; no new
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
import importlib.util
import json
import os
import queue
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
    "models": {"COO": "opus", "CTO": "opus", "PM": "sonnet", "CMO": "sonnet", "CPSO": "sonnet"},
}
ROLES = tuple(AGENT["models"])

IDLE_SECONDS = 15 * 60
MAX_AGE_SECONDS = 4 * 60 * 60
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
    return {"founder_id": int(raw), "site": site, "bots": bots, "missing": missing}


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

def build_command(kind: str, role: str, session_id: str) -> list[str]:
    """The argv for a spawn or a resume. Only the role's model and our own
    uuid are substituted; a model not in the table is refused."""
    model = AGENT["models"][role]
    uuid.UUID(session_id)  # raises on anything that is not one of ours
    return [part.format(model=model, session_id=session_id) for part in AGENT[kind]]


class Spawner:
    """Starts the agent with the prompt on stdin. ``alive`` and ``stop`` take
    a pid so they still work after a restart, when no handle is left."""

    def __init__(self, log_dir: Path = STATE_DIR):
        self.log_dir = log_dir
        self.procs: dict[int, subprocess.Popen] = {}

    def start(self, argv: list[str], prompt: str, role: str) -> int:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_dir / f"{role}.log", "ab") as log:
            proc = subprocess.Popen(argv, cwd=AGENT["cwd"], stdin=subprocess.PIPE,
                                    stdout=log, stderr=subprocess.STDOUT)
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass  # it died at once; alive() says so and the log says why
        self.procs[proc.pid] = proc
        return proc.pid

    def alive(self, pid: int | None) -> bool:
        if not pid:
            return False
        proc = self.procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        return _hook()._pid_alive(pid)

    def stop(self, pid: int) -> None:
        proc = self.procs.get(pid)
        try:
            if proc is not None:
                proc.terminate()
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
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


def message_prompt(role: str, msg: dict[str, Any], first: bool) -> str:
    stream, topic = _where(msg, role)
    sender = msg.get("sender_full_name") or msg.get("sender_email") or "the founder"
    origin = ("a direct message" if msg.get("type") != "stream"
              else f"#{stream} › {topic}")
    head = (f"You are the {role} (read agents/{role.lower()}.md). The founder called you "
            f"from Zulip; this session was opened for that and closes when he goes quiet.\n\n"
            if first else "")
    return (f"{head}Message from {sender} in {origin}:\n\n{msg.get('content', '')}\n\n"
            f"Answer in #{stream} › {topic} with:\n  {_send_hint(role, stream, topic)}\n")


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
                 emit: Callable[[dict[str, Any]], None] = lambda d: None):
        self.founder_id = founder_id
        self.state_path = state_path
        self.state = load_state(state_path)
        self.spawner = spawner
        self.clock = clock
        self.dry_run = dry_run
        self.emit = emit

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
        if "mentioned" not in flags and msg.get("type") != "private":
            return self._decide(role, "skip", f"not a mention of or a DM to {role}", msg)
        hand = registry_live(role, self.spawner.alive)
        if hand:
            return self._decide(role, "skip", f"live {role} session (pid {hand}); "
                                "its hook delivers the message", msg)
        rs = self.role(role)
        sess = rs.get("session")
        now = self.clock()
        if sess and sess.get("closing"):
            sess.setdefault("pending", []).append(msg)
            return self._decide(role, "queue", "the session is closing; a fresh one "
                                "opens with this message", msg)
        if sess:
            sess["last_founder_at"] = now
            if self.spawner.alive(sess.get("pid")) or sess.get("dry"):
                sess.setdefault("pending", []).append(msg)
                return self._decide(role, "queue", f"{role} session is answering "
                                    f"(pid {sess.get('pid') or 'dry run'}); delivered when the turn ends", msg)
            self._run(role, "resume", message_prompt(role, msg, first=False))
            stream, topic = _where(msg, role)
            sess["stream"], sess["topic"] = stream, topic
            return self._decide(role, "resume", f"open {role} session, turn ended",
                                msg, session_id=sess["id"])
        return self._spawn(role, [msg])

    def _spawn(self, role: str, msgs: list[dict[str, Any]]) -> dict[str, Any]:
        now = self.clock()
        stream, topic = _where(msgs[-1], role)
        sid = str(uuid.uuid4())
        self.role(role)["session"] = {"id": sid, "stream": stream, "topic": topic,
                                      "started_at": now, "last_founder_at": now, "pid": None}
        prompt = "\n---\n".join(message_prompt(role, m, first=(i == 0))
                                for i, m in enumerate(msgs))
        self._run(role, "spawn", prompt)
        return self._decide(role, "spawn", f"founder called {role}; no live session",
                            msgs[-1], session_id=sid, model=AGENT["models"][role],
                            command=build_command("spawn", role, sid))

    def _run(self, role: str, kind: str, prompt: str) -> None:
        sess = self.role(role)["session"]
        argv = build_command(kind, role, sess["id"])
        if self.dry_run:
            sess["dry"] = True  # as if its turn were running: later messages queue
            return
        self.save()  # the session is on disk before its process exists: no double spawn
        sess["pid"] = self.spawner.start(argv, prompt, role)

    def tick(self) -> list[dict[str, Any]]:
        """Deliver queued messages when a turn ends; close idle or old sessions."""
        out = []
        now = self.clock()
        for role, rs in list(self.state["roles"].items()):
            sess = rs.get("session")
            if not sess or sess.get("dry"):
                continue
            running = self.spawner.alive(sess.get("pid"))
            if sess.get("closing"):
                if running:
                    continue
                pending = sess.get("pending") or []
                rs["session"] = None
                if pending:
                    out.append(self._spawn(role, pending))
                else:
                    out.append(self._decide(role, "closed", "close turn done", None))
                self.save()
                continue
            age, idle = now - sess["started_at"], now - sess["last_founder_at"]
            if age >= MAX_AGE_SECONDS or (idle >= IDLE_SECONDS and not running):
                if running:
                    self.spawner.stop(sess["pid"])
                pending = sess.get("pending") or []
                sess.update(closing=True, pending=pending)
                self._run(role, "resume", close_prompt(role, sess["stream"], sess["topic"]))
                why = "4 h of age" if age >= MAX_AGE_SECONDS else "15 min without the founder"
                out.append(self._decide(role, "close", why, None, session_id=sess["id"]))
                self.save()
                continue
            pending = sess.get("pending") or []
            if pending and not running:
                sess["pending"] = []
                prompt = "\n---\n".join(message_prompt(role, m, first=False) for m in pending)
                stream, topic = _where(pending[-1], role)
                sess["stream"], sess["topic"] = stream, topic
                self._run(role, "resume", prompt)
                out.append(self._decide(role, "resume", "queued messages after the turn",
                                        pending[-1], session_id=sess["id"]))
                self.save()
        return out

    def status(self) -> dict[str, Any]:
        roles = {}
        for role in ROLES:
            rs = self.state["roles"].get(role, {})
            sess = rs.get("session")
            hand = registry_live(role, self.spawner.alive)
            roles[role] = {
                "live": bool(hand) or bool(sess and self.spawner.alive(sess.get("pid"))),
                "opened_by": "hand" if hand else ("wake" if sess else None),
                "session_id": sess.get("id") if sess else None,
                "topic": f"#{sess['stream']} › {sess['topic']}" if sess else None,
                "last_event_id": rs.get("last_event_id"),
                "last_message_id": rs.get("last_message_id"),
            }
        return {"ok": True, "roles": roles}


# -- Zulip: queues, resume, backfill ----------------------------------------

def _queue_gone(exc: ZulipError) -> bool:
    return "BAD_EVENT_QUEUE_ID" in str(exc) or "Bad event queue" in str(exc)


def backfill(client: ZulipClient, after: int | None, newest: int = 20
             ) -> list[tuple[dict[str, Any], list[str]]]:
    """Messages after ``after`` that mention this bot or are DMs to it, in
    order, with the flags a queue event would carry. What a lost queue missed.
    ``after=None``: the newest ``newest`` of them instead (a first dry run)."""
    found: dict[int, tuple[dict[str, Any], list[str]]] = {}
    if after is None:
        window = {"anchor": "newest", "num_before": newest, "num_after": 0}
    else:
        window = {"anchor": after, "include_anchor": "false",
                  "num_before": 0, "num_after": BACKFILL_LIMIT}
    for narrow, flag in (([{"operator": "is", "operand": "mentioned"}], "mentioned"),
                         ([{"operator": "is", "operand": "dm"}], None)):
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


def open_queue(client: ZulipClient, rs: dict[str, Any]
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
    return backfill(client, int(rs["last_message_id"])), []


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
        pairs, events = open_queue(clients[role], rs)
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
    for role, (email, key) in cfg["bots"].items():
        client = ZulipClient(cfg["site"], email, key, timeout=30.0)
        rs = waker.role(role)
        after = rs.get("last_message_id")
        pairs = backfill(client, None if after is None else int(after))
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
                  dry_run=args.dry_run, emit=emit)
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
