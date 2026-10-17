"""bot/wake.py (ops#293): a founder @mention opens a fresh role session.

Rooms (ops#322): ``alone`` is a room with one role bot, where any founder
message wakes it; ``crowd`` has several, where only ``@**ROLE**`` does.

Zulip, the clock and the agent process are all fakes here: no network, no
``claude``. One test starts a real child process to prove the prompt reaches
it on stdin and never on its command line.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import uuid
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
FOUNDER = 8
TRICKY = 'why? "; rm -rf / & echo $(whoami) | del *'


def _load():
    sys.path.insert(0, str(BOT_DIR))
    try:
        spec = importlib.util.spec_from_file_location("wake_under_test", BOT_DIR / "wake.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(BOT_DIR))


@pytest.fixture
def wake(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "registry_live", lambda role, alive: None)
    return mod


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSpawner:
    def __init__(self):
        self.started: list[tuple[list[str], str, str]] = []
        self.cwds: list[str | None] = []
        self.running: set[int] = set()
        self.stopped: list[int] = []
        self.stubborn: set[int] = set()  # stop() does not end these
        self.missing: set[str] = set()  # session ids with no conversation
        self._pid = 1000

    def start(self, argv, prompt, role, cwd=None):
        self._pid += 1
        self.started.append((argv, prompt, role))
        self.cwds.append(cwd)
        self.running.add(self._pid)
        return self._pid

    def created(self, pid):
        return pid * 10

    def alive(self, pid, created=None):
        return pid in self.running

    def stop(self, pid, created=None):
        self.stopped.append(pid)
        if pid not in self.stubborn:
            self.running.discard(pid)

    def conversation_exists(self, session_id):
        return session_id not in self.missing

    def finish_all(self):
        self.running.clear()


def msg(i, content="what about the record shape?", sender=FOUNDER, stream="cto",
        topic="141 record shape", **extra):
    m = {"id": i, "sender_id": sender, "sender_email": f"u{sender}@x",
         "sender_full_name": "Founder" if sender == FOUNDER else "Someone",
         "content": content, **extra}
    if stream is None:
        m["type"] = "private"
    else:
        m.update(type="stream", display_recipient=stream, subject=topic)
    return m


def user(uid, name, bot=True):
    return {"user_id": uid, "full_name": name, "email": f"{name.lower()}@x", "is_bot": bot}


def alone(role, room):
    """A room with the founder and this one role bot: today's #cto or #pa."""
    return [user(FOUNDER, "Founder", bot=False), user(100, role)]


def crowd(role, room):
    """A room with the founder and three role bots, like All hands."""
    return [user(FOUNDER, "Founder", bot=False), user(101, "COO"), user(102, "CTO"),
            user(103, "PM"), user(104, "PA"), user(105, "Watchdog")]


def real(role, room):
    """The rooms setup_streams.py makes: #coo has the COO and the PM, #PM the
    PM, COO and CTO, #feature the CTO and COO (no owner), All hands every
    role. Any other room has this one role bot."""
    rooms = {"coo": ("COO", "PM"), "PM": ("PM", "COO", "CTO"), "feature": ("CTO", "COO"),
             "All hands": ("COO", "CTO", "PM", "PA")}
    if room not in rooms:
        return alone(role, room)
    return [user(FOUNDER, "Founder", bot=False)] + [
        user(200 + i, r) for i, r in enumerate(rooms[room])]


def make(wake, tmp_path, clock=None, spawner=None, dry_run=False, members=alone):
    emitted = []
    w = wake.Waker(FOUNDER, tmp_path / "state.json", spawner or FakeSpawner(),
                   clock=clock or Clock(), dry_run=dry_run, emit=emitted.append,
                   members=members)
    w.emitted = emitted
    return w


# -- who triggers ------------------------------------------------------------

def test_founder_mention_spawns_once_with_text_on_stdin_only(wake, tmp_path):
    w = make(wake, tmp_path)
    d = w.on_message("CTO", msg(10, TRICKY), ["mentioned"])
    assert d["action"] == "spawn"
    (argv, prompt, role), = w.spawner.started
    assert role == "CTO"
    assert argv[:4] == ["claude", "-p", "--model", "opus"]
    assert argv[4] == "--session-id" and uuid.UUID(argv[5])
    assert not any(TRICKY in a or "rm -rf" in a for a in argv)
    assert TRICKY in prompt and "the room #cto" in prompt and '--room "cto"' in prompt
    assert "--dangerously-skip-permissions" not in argv


def test_second_message_while_live_does_not_spawn_again(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CTO", msg(10), ["mentioned"])
    d = w.on_message("CTO", msg(11, "and the index?"), ["mentioned"])
    assert d["action"] == "queue"
    assert len(w.spawner.started) == 1
    w.spawner.finish_all()
    [r] = w.tick()
    assert r["action"] == "resume"
    argv, prompt, _ = w.spawner.started[-1]
    assert argv[4] == "--resume" and argv[5] == w.spawner.started[0][0][5]
    assert "and the index?" in prompt and "and the index?" not in " ".join(argv)


def test_next_message_after_the_turn_resumes_the_same_conversation(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.spawner.finish_all()
    d = w.on_message("CTO", msg(11, "thanks, one more"), ["mentioned"])
    assert d["action"] == "resume"
    assert [a[0][4] for a in w.spawner.started] == ["--session-id", "--resume"]


@pytest.mark.parametrize("sender", [99, 41])
def test_bot_or_non_founder_never_spawns(wake, tmp_path, sender):
    w = make(wake, tmp_path)
    d = w.on_message("CTO", msg(10, sender=sender), ["mentioned"])
    assert d["action"] == "skip" and "not the founder" in d["reason"]
    d = w.on_message("CTO", msg(11, sender=sender, stream=None), [])
    assert d["action"] == "skip"
    assert w.spawner.started == []


def test_sender_id_is_compared_not_the_name(wake, tmp_path):
    w = make(wake, tmp_path)
    impostor = msg(10, sender=77, sender_full_name="Founder", sender_email="u8@x")
    assert w.on_message("CTO", impostor, ["mentioned"])["action"] == "skip"
    assert w.spawner.started == []


def test_founder_without_mention_in_a_multi_bot_room_is_skipped(wake, tmp_path):
    w = make(wake, tmp_path, members=crowd)
    d = w.on_message("CTO", msg(10, stream="All hands"), ["mentioned"])  # @all, or the broadcast flag
    assert d["action"] == "skip" and "only @**CTO** wakes CTO" in d["reason"]


def test_founder_without_mention_in_a_stream_is_skipped(wake, tmp_path):
    # outside the role's own stream: the PM reads #coo, but #coo is not the PM's
    w = make(wake, tmp_path, members=real)
    assert w.on_message("PM", msg(10, stream="coo"), [])["action"] == "skip"
    assert w.spawner.started == []


def test_founder_dm_spawns_and_is_answered_in_the_role_stream(wake, tmp_path):
    w = make(wake, tmp_path)
    d = w.on_message("PM", msg(10, stream=None), [])
    assert d["action"] == "spawn" and d["model"] == "sonnet"
    prompt = w.spawner.started[0][1]
    assert "a direct message" in prompt and '--room "PM"' in prompt


def test_a_session_opened_by_hand_is_left_to_its_hook(wake, tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "registry_live", lambda role, alive: 4242)
    w = make(wake, tmp_path)
    d = w.on_message("COO", msg(10, stream="coo"), ["mentioned"])
    assert d["action"] == "skip" and "4242" in d["reason"]
    assert w.spawner.started == []


def test_registry_live_matches_role_name_and_live_pid(tmp_path, monkeypatch):
    mod = _load()
    (tmp_path / "a.json").write_text(json.dumps({"name": "cto", "pid": 5}))
    (tmp_path / "b.json").write_text(json.dumps({"name": "CMO", "pid": 6}))

    class Hook:
        SESSIONS = tmp_path
    monkeypatch.setattr(mod, "_hook", lambda: Hook)
    assert mod.registry_live("CTO", lambda pid: pid == 5) == 5
    assert mod.registry_live("CTO", lambda pid: False) is None
    assert mod.registry_live("PM", lambda pid: True) is None


# -- idle close, fake clock ----------------------------------------------------

def test_idle_close_at_15_minutes_then_a_fresh_session(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    first_id = w.spawner.started[0][0][5]
    w.spawner.finish_all()
    clock.t += 14 * 60 + 59
    assert w.tick() == []
    clock.t += 1
    [d] = w.tick()
    assert d["action"] == "close" and "15 min" in d["reason"]
    argv, prompt, _ = w.spawner.started[-1]
    assert argv[4:] == ["--resume", first_id]
    assert "Idle close" in prompt and "the room #cto" in prompt and '--room "cto"' in prompt
    assert "--as CTO send" in prompt
    w.spawner.finish_all()
    [d] = w.tick()
    assert d["action"] == "closed"
    d = w.on_message("CTO", msg(11, "new question"), ["mentioned"])
    assert d["action"] == "spawn" and d["session_id"] != first_id


def test_idle_close_waits_for_a_running_turn(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    clock.t += 20 * 60
    assert w.tick() == []  # still answering
    w.spawner.finish_all()
    assert w.tick()[0]["action"] == "close"


def test_close_at_4_hours_even_while_talking(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    for i in range(1, 24):  # a message every 10 minutes: never idle
        w.spawner.finish_all()
        clock.t += 10 * 60
        assert w.on_message("CTO", msg(10 + i), ["mentioned"])["action"] == "resume"
        assert w.tick() == []
    clock.t += 10 * 60  # 4 h since the spawn, a turn still running
    running = w.role("CTO")["session"]["pid"]
    n = len(w.spawner.started)
    [d] = w.tick()
    assert d["action"] == "close" and "4 h" in d["reason"]
    assert w.spawner.stopped == [running]
    assert len(w.spawner.started) == n  # the close turn waits for the stopped one to exit
    assert w.tick() == []
    assert "Idle close" in w.spawner.started[-1][1]


def test_message_during_close_opens_a_fresh_session_after_it(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.spawner.finish_all()
    clock.t += 15 * 60
    w.tick()
    assert w.on_message("CTO", msg(11, "wait!"), ["mentioned"])["action"] == "queue"
    w.spawner.finish_all()
    closed, d = w.tick()
    assert closed["action"] == "closed"
    assert d["action"] == "spawn" and "wait!" in w.spawner.started[-1][1]


# -- resume after restart -----------------------------------------------------

class FakeClient:
    """register / get_events / _request(GET messages) with a scripted history."""

    def __init__(self, history=(), queue_alive=False, events=(), max_message_id=0,
                 members=None):
        self.history = list(history)
        self.members = members if members is not None else alone("CTO", "cto")
        self.queue_alive = queue_alive
        self.events = list(events)
        self.max_message_id = max_message_id
        self.registered = 0

    def register(self, event_types, narrow=None):
        self.registered += 1
        return {"queue_id": f"q{self.registered}", "last_event_id": -1,
                "max_message_id": self.max_message_id}

    def get_events(self, queue_id, last_event_id, dont_block=False):
        from zulip_client import ZulipError
        if not self.queue_alive:
            raise ZulipError("GET events failed: {'code': 'BAD_EVENT_QUEUE_ID'}")
        return [e for e in self.events if e["id"] > last_event_id]

    def _request(self, method, path, params=None):
        if path == "get_stream_id":
            return {"stream_id": 1}
        if path == "streams/1/members":
            return {"subscribers": [u["user_id"] for u in self.members]}
        if path == "users":
            return {"members": self.members + [user(200, "Stranger", bot=False)]}
        assert (method, path) == ("GET", "messages")
        operand = params["narrow"][0]["operand"] if params["narrow"] else None
        rows = [m for m in self.history
                if operand is None or (operand == "dm") == (m.get("type") == "private")]
        if params["anchor"] == "newest":
            rows = rows[-params["num_before"]:]
        else:
            rows = [m for m in rows if m["id"] > params["anchor"]]
        return {"result": "success", "messages": rows}


def test_first_start_acts_on_nothing_old(wake, tmp_path):
    w = make(wake, tmp_path)
    client = FakeClient(history=[msg(5)], max_message_id=5)
    pairs, events = wake.open_queue(client, w.role("CTO"))
    assert pairs == [] and events == []
    assert w.role("CTO")["last_message_id"] == 5


def test_restart_with_expired_queue_neither_respawns_nor_skips(wake, tmp_path):
    spawner = FakeSpawner()
    w = make(wake, tmp_path, spawner=spawner)
    w.role("CTO").update(queue_id="q-old", last_event_id=3, last_message_id=9)
    w.on_message("CTO", msg(10), ["mentioned"])
    assert len(spawner.started) == 1
    spawner.finish_all()  # the whole machine restarted: the turn is gone too

    w2 = make(wake, tmp_path, spawner=spawner)  # same state file
    assert w2.role("CTO")["last_message_id"] == 10
    client = FakeClient(history=[msg(10), msg(11, "while you were down")])
    pairs, _ = wake.open_queue(client, w2.role("CTO"))
    assert client.registered == 1 and [m["id"] for m, _ in pairs] == [11]
    # and a duplicate delivery of the handled message (an old queue's replay) is skipped
    pairs = [(msg(10), ["mentioned"])] + pairs
    actions = [w2.on_message("CTO", m, f)["action"] for m, f in pairs]
    assert actions == ["skip", "resume"]
    assert len(spawner.started) == 2
    assert "while you were down" in spawner.started[-1][1]


def test_restart_with_live_queue_resumes_from_saved_event_id(wake, tmp_path):
    spawner = FakeSpawner()
    w = make(wake, tmp_path, spawner=spawner)
    w.role("CTO").update(queue_id="q1", last_event_id=3, last_message_id=10)
    w.save()
    w2 = make(wake, tmp_path, spawner=spawner)
    client = FakeClient(queue_alive=True, events=[
        {"id": 3, "type": "message", "message": msg(10), "flags": ["mentioned"]},
        {"id": 4, "type": "message", "message": msg(11), "flags": ["mentioned"]},
        {"id": 5, "type": "heartbeat"}])
    _, events = wake.open_queue(client, w2.role("CTO"))
    assert client.registered == 0 and [e["id"] for e in events] == [4, 5]
    wake.process_batch(w2, "CTO", events)
    assert [a for a in (d["action"] for d in w2.emitted)] == ["spawn"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["roles"]["CTO"]["last_event_id"] == 5
    assert saved["roles"]["CTO"]["last_message_id"] == 11


# -- config, command, dry run, the real process -------------------------------

def test_missing_founder_id_says_what_to_add(wake, tmp_path, monkeypatch):
    monkeypatch.delenv("FOUNDER_ZULIP_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text("ZULIP_CTO_EMAIL=cto-bot@x\nZULIP_CTO_API_KEY=k\n")
    with pytest.raises(wake.ConfigError, match="FOUNDER_ZULIP_ID missing in bot/.env"):
        wake.load_config(env)
    env.write_text("FOUNDER_ZULIP_ID=founder@x\nZULIP_CTO_EMAIL=a\nZULIP_CTO_API_KEY=k\n")
    with pytest.raises(wake.ConfigError, match="not a number"):
        wake.load_config(env)
    env.write_text("FOUNDER_ZULIP_ID=8\n")
    for role in wake.ROLES:
        monkeypatch.delenv(f"ZULIP_{role}_EMAIL", raising=False)
    with pytest.raises(wake.ConfigError, match="no role bot credentials"):
        wake.load_config(env)


def test_command_takes_only_our_uuid(wake):
    with pytest.raises(ValueError):
        wake.build_command("spawn", "CTO", "x; rm -rf /")
    with pytest.raises(KeyError):
        wake.build_command("spawn", "INTERN", str(uuid.uuid4()))


def test_dry_run_json_prints_one_line_per_message_and_spawns_nothing(wake, tmp_path,
                                                                    monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("FOUNDER_ZULIP_ID=8\nZULIP_CTO_EMAIL=cto-bot@x\nZULIP_CTO_API_KEY=k\n")
    for role in wake.ROLES:
        monkeypatch.delenv(f"ZULIP_{role}_EMAIL", raising=False)
    monkeypatch.delenv("FOUNDER_ZULIP_ID", raising=False)
    monkeypatch.setattr(wake, "DOTENV", env)
    monkeypatch.setattr(wake, "STATE_DIR", tmp_path / ".wake")
    monkeypatch.setattr(wake, "DUTY_DIR", tmp_path / "duty")
    monkeypatch.setattr(wake.load_config, "__defaults__", (env,))
    client = FakeClient(history=[msg(10), msg(11, sender=99)])
    monkeypatch.setattr(wake.ZulipClient, "_request",
                        lambda self, m, p, params=None: client._request(m, p, params))
    monkeypatch.setattr(wake.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert wake.main(["--dry-run", "--json"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    # Then the duty decision (ops#338): no duty file here, a skip.
    assert [(d["role"], d["action"]) for d in lines] == [("CTO", "spawn"), ("CTO", "skip"),
                                                         ("CTO", "duty-skip")]
    assert not (tmp_path / ".wake" / "state.json").exists()


def test_real_spawner_puts_the_prompt_on_stdin(wake, tmp_path, monkeypatch):
    got, args = tmp_path / "stdin.txt", tmp_path / "argv.json"
    script = ("import sys,json,pathlib;"
              "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read());"
              "pathlib.Path(sys.argv[2]).write_text(json.dumps(sys.argv),encoding='utf-8')")
    monkeypatch.setitem(wake.AGENT, "cwd", str(tmp_path))
    sp = wake.Spawner(log_dir=tmp_path / "logs")
    pid = sp.start([sys.executable, "-c", script, str(got), str(args)], TRICKY, "CTO")
    sp.procs[pid].wait(timeout=30)
    assert not sp.alive(pid)
    assert got.read_text(encoding="utf-8") == TRICKY
    assert not any("rm -rf" in a for a in json.loads(args.read_text(encoding="utf-8")))


def test_status_json_names_live_sessions_and_event_ids(wake, tmp_path):
    w = make(wake, tmp_path)
    w.role("CTO").update(last_event_id=7)
    w.on_message("CTO", msg(10), ["mentioned"])
    st = w.status()
    assert st["roles"]["CTO"]["live"] is True
    assert st["roles"]["CTO"]["last_event_id"] == 7
    assert st["roles"]["CTO"]["room"] == "#cto"
    assert st["roles"]["PM"]["live"] is False


# -- CTO fix round (PR #127) ---------------------------------------------------

def _open_and_go_idle(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.spawner.finish_all()
    clock.t += 15 * 60
    assert w.tick()[0]["action"] == "close"
    return w, clock


def test_a_hung_close_turn_is_stopped_after_2_minutes(wake, tmp_path):
    w, clock = _open_and_go_idle(wake, tmp_path)
    close_pid = w.role("CTO")["session"]["pid"]
    w.on_message("CTO", msg(11, "still there?"), ["mentioned"])
    clock.t += 119
    assert w.tick() == []
    clock.t += 1
    closed, d = w.tick()
    assert closed["action"] == "closed" and "2 min" in closed["reason"]
    assert close_pid in w.spawner.stopped
    assert d["action"] == "spawn" and "still there?" in w.spawner.started[-1][1]


def test_4h_close_turn_runs_only_after_the_stopped_turn_exits(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    sid = w.role("CTO")["session"]["id"]
    w.spawner.stubborn.add(w.role("CTO")["session"]["pid"])
    clock.t += 4 * 60 * 60
    assert w.tick()[0]["action"] == "close"
    for _ in range(3):  # still holding the session: no second process on the same id
        clock.t += 30
        assert w.tick() == []
    assert len(w.spawner.started) == 1
    w.spawner.finish_all()
    clock.t += 10
    assert w.tick() == []
    argv, prompt, _ = w.spawner.started[-1]
    assert argv[4:] == ["--resume", sid] and "Idle close" in prompt


def test_4h_stopped_turn_that_never_exits_is_given_up_without_a_resume(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.spawner.stubborn.add(w.role("CTO")["session"]["pid"])
    clock.t += 4 * 60 * 60
    w.tick()
    clock.t += 2 * 60
    [d] = w.tick()
    assert d["action"] == "closed" and "without a close line" in d["reason"]
    assert len(w.spawner.started) == 1
    assert w.role("CTO")["session"] is None


def test_pid_create_time_is_stored_and_a_reused_pid_is_not_ours(wake, tmp_path, monkeypatch):
    w = make(wake, tmp_path)
    w.on_message("CTO", msg(10), ["mentioned"])
    sess = w.role("CTO")["session"]
    assert sess["pid_created"] == sess["pid"] * 10
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["roles"]["CTO"]["session"]["pid_created"] == sess["pid"] * 10

    killed = []
    monkeypatch.setattr(wake.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(wake, "_hook", lambda: type("H", (), {"_pid_alive": staticmethod(
        lambda pid: True)}))
    monkeypatch.setattr(wake, "_proc_start_time", lambda pid: 555)
    sp = wake.Spawner(log_dir=tmp_path)  # after a restart: no Popen handle
    assert sp.alive(4321, 555) is True
    assert sp.alive(4321, 554) is False  # same pid, another process
    assert sp.alive(4321, None) is False  # no create time stored: never trusted
    sp.stop(4321, 554)
    sp.stop(4321, None)
    assert killed == []
    sp.stop(4321, 555)
    assert killed == [4321]


@pytest.mark.skipif(sys.platform == "darwin", reason="create time: Linux and Windows only")
def test_real_process_create_time_after_restart(wake, tmp_path):
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        created = wake._proc_start_time(child.pid)
        assert created is not None
        sp = wake.Spawner(log_dir=tmp_path)  # a fresh wake.py: no handle
        assert sp.alive(child.pid, created)
        assert not sp.alive(child.pid, created + 1)
        sp.stop(child.pid, created + 1)
        assert child.poll() is None  # the mismatched stop killed nothing
        sp.stop(child.pid, created)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()


def test_a_session_from_state_without_create_time_counts_as_dead(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock, spawner=wake.Spawner(log_dir=tmp_path))
    import os
    w.role("CTO")["session"] = {"id": str(uuid.uuid4()), "stream": "cto", "topic": "t",
                                "started_at": clock.t, "last_founder_at": clock.t,
                                "pid": os.getpid()}  # a live pid, but not provably ours
    assert w.status()["roles"]["CTO"]["live"] is False


def _history(rows, calls=None):
    def fetch(role, room, n):
        if calls is not None:
            calls.append((role, room, n))
        return rows[-n:]
    return fetch


def test_fresh_session_gets_the_room_last_20_messages_before_the_new_one(wake, tmp_path):
    rows = [msg(i, f"old {i}", sender=FOUNDER if i % 2 else 99) for i in range(1, 30)]
    rows.append(msg(30, "CTO closing: agreed X, written in ops#300", sender=99))
    rows.append(msg(31, "next question"))
    calls = []
    w = make(wake, tmp_path)
    w.history = _history(rows, calls)
    d = w.on_message("CTO", rows[-1], ["mentioned"])
    assert d["action"] == "spawn" and d["history"] == 20
    assert calls == [("CTO", "cto", 21)]
    prompt = w.spawner.started[0][1]
    assert "agreed X, written in ops#300" in prompt
    assert "old 11" in prompt and "old 10\n" not in prompt
    assert prompt.index("ops#300") < prompt.index("Message from Founder")
    assert prompt.count("next question") == 1
    # the next message in the same conversation is a resume: no history again
    w.spawner.finish_all()
    w.on_message("CTO", msg(32, "and?"), ["mentioned"])
    assert "ops#300" not in w.spawner.started[-1][1]


def test_unreadable_history_still_spawns_and_says_so(wake, tmp_path):
    def broken(*a):
        raise wake.ZulipError("GET messages: cannot reach Zulip")
    w = make(wake, tmp_path)
    w.history = broken
    d = w.on_message("CTO", msg(10), ["mentioned"])
    assert d["action"] == "spawn" and str(d["history"]).startswith("error")
    assert "could not be read" in w.spawner.started[0][1]


def test_room_history_asks_for_the_whole_room_across_old_topics(wake):
    seen = {}

    class C:
        def _request(self, method, path, params=None):
            seen.update(params)
            return {"messages": [{"id": 3}, {"id": 1}]}
    assert [m["id"] for m in wake.room_history(C(), "cto", 21)] == [1, 3]
    assert seen["narrow"] == [{"operator": "stream", "operand": "cto"}]
    assert seen["num_before"] == 21 and seen["anchor"] == "newest"


def test_room_members_reads_the_servers_subscriber_list(wake):
    client = FakeClient(members=crowd("CTO", "x"))
    got = wake.room_members(client, "All hands")
    assert sorted(u["full_name"] for u in got) == ["COO", "CTO", "Founder", "PA", "PM", "Watchdog"]
    assert wake.role_bots(got) == {"COO", "CTO", "PM", "PA"}  # Watchdog is no role


def test_zulip_down_does_not_stop_the_tick(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.spawner.finish_all()
    attempts = []

    def start(role):
        attempts.append(role)
        if len(attempts) < 4:
            raise wake.ZulipError("cannot reach Zulip")

    def sleep(seconds):
        clock.t += seconds

    ticks = []
    wake.open_until_up(start, "PM", lambda: ticks.append(w.tick()), sleep)
    assert len(attempts) == 4 and len(ticks) == 3
    # 15 min of outage is 30 retries; here 90 s, so prove the tick runs on a retry
    clock.t += 15 * 60
    attempts.clear()
    ticks.clear()
    wake.open_until_up(start, "PM", lambda: ticks.append(w.tick()), sleep)
    assert ticks[0][0]["action"] == "close"


def test_founder_follow_up_in_the_room_extends_the_idle_timer(wake, tmp_path):
    # in a room with several role bots and two open sessions an untagged
    # follow-up only keeps the session open; in a one-bot room (#cto), or with
    # one open session in the room, it is delivered
    clock = Clock()
    w = make(wake, tmp_path, clock=clock, members=crowd)
    w.on_message("PM", msg(9, "@**PM** plan", stream="coo"), ["mentioned"])
    w.on_message("CTO", msg(10, "@**CTO** look", stream="coo"), ["mentioned"])
    w.spawner.finish_all()
    clock.t += 10 * 60
    d = w.on_message("CTO", msg(11, "hm, thinking", stream="coo"), [])
    assert d["action"] == "skip" and "extends" in d["reason"]
    clock.t += 10 * 60
    # 20 min since the mention, 10 since the follow-up
    assert [d for d in w.tick() if d["role"] == "CTO"] == []
    clock.t += 5 * 60
    assert next(d for d in w.tick() if d["role"] == "CTO")["action"] == "close"
    # two spawns and two close turns; nothing delivered to the CTO
    assert [(a[4], role) for a, _, role in w.spawner.started if role == "CTO"] == \
        [("--session-id", "CTO"), ("--resume", "CTO")]
    assert "hm, thinking" not in "".join(p for _, p, _ in w.spawner.started)
    # another room, or another sender, does not count
    w2 = make(wake, tmp_path / "b", clock=clock, members=crowd)
    w2.on_message("CTO", msg(20, "@**CTO**"), [])
    t0 = w2.role("CTO")["session"]["last_founder_at"]
    clock.t += 60
    w2.on_message("CTO", msg(21, stream="other"), [])
    w2.on_message("CTO", msg(22, sender=99), [])
    assert w2.role("CTO")["session"]["last_founder_at"] == t0


# -- the PA (ops#293 item 7) ----------------------------------------------------

def _env(tmp_path, monkeypatch, text):
    for key in ["FOUNDER_ZULIP_ID", "PA_DIR"] + [f"ZULIP_{r}_{k}" for r in
                                                  ("COO", "CTO", "PM", "CMO", "CPSO", "PA")
                                                  for k in ("EMAIL", "API_KEY")]:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(text)
    return env


def test_pa_runs_in_pa_dir_on_sonnet(wake, tmp_path, monkeypatch):
    pa = tmp_path / "pa"
    pa.mkdir()
    env = _env(tmp_path, monkeypatch, f"FOUNDER_ZULIP_ID=8\nZULIP_PA_EMAIL=pa-bot@x\n"
                                      f"ZULIP_PA_API_KEY=k\nPA_DIR={pa}\n")
    cfg = wake.load_config(env)
    assert "PA" in cfg["bots"] and cfg["cwd"]["PA"] == str(pa) and cfg["skipped"] == {}
    assert cfg["cwd"]["CTO"] == wake.AGENT["cwd"]
    w = make(wake, tmp_path)
    w.cwd = cfg["cwd"]
    d = w.on_message("PA", msg(10, "book the dentist", stream="pa", topic="errands"), [])
    assert d["action"] == "spawn" and d["model"] == "sonnet" and d["cwd"] == str(pa)
    argv, prompt, role = w.spawner.started[0]
    assert argv[:4] == ["claude", "-p", "--model", "sonnet"] and role == "PA"
    assert w.spawner.cwds == [str(pa)]
    assert "founder's PA" in prompt and "--as PA send" in prompt and "agents/pa.md" not in prompt


def test_the_pa_never_wakes_in_a_company_room_and_others_in_pa_never_do(wake, tmp_path):
    # the founder rule (2026-10-17): the PA is his personal assistant, it wakes
    # only in #pa or a DM, never in a company room, tagged or not
    w = make(wake, tmp_path, members=crowd)
    d = w.on_message("PA", msg(10, "@**PA** hi", stream="All hands"), ["mentioned"])
    assert d["action"] == "skip" and d["reason"] == "PA wakes only in #pa or a founder DM (ops#327)"
    w1 = make(wake, tmp_path / "a")  # a room with the founder and the PA only
    assert w1.on_message("PA", msg(10, "hi", stream="errands"), [])["action"] == "skip"
    assert w.spawner.started == [] and w1.spawner.started == []
    assert w1.on_message("PA", msg(11, "hi", stream="pa"), [])["action"] == "spawn"
    assert w1.on_message("PA", msg(12, "hi", stream=None), [])["action"] == "queue"
    w2 = make(wake, tmp_path / "b", members=crowd)
    assert w2.on_message("PA", msg(11, stream="pa", sender=99), [])["action"] == "skip"
    assert w2.on_message("PA", msg(12, stream="All hands"), [])["action"] == "skip"
    assert w2.spawner.started == []
    # a role not in the room never wakes there: the CTO and #pa (founder + PA)
    w3 = make(wake, tmp_path / "c", members=lambda role, room: alone("PA", room))
    d = w3.on_message("CTO", msg(13, "@**CTO** here?", stream="pa"), ["mentioned"])
    assert d["action"] == "skip" and "CTO is not a member of #pa" in d["reason"]
    assert w3.spawner.started == []


def test_a_pa_in_a_company_room_does_not_turn_off_untagged_wakes(wake, tmp_path):
    # CTO review @a7c77e5: the PA never wakes outside #pa, so it is not counted
    # as a role bot there; {founder, COO, PA} is a one-bot room for the COO
    def plan(role, room):
        return [user(FOUNDER, "Founder", bot=False), user(101, "COO"), user(104, "PA")]
    w = make(wake, tmp_path, members=plan)
    d = w.on_message("COO", msg(10, "what next?", stream="plan"), [])
    assert d["action"] == "spawn", d
    assert w.on_message("PA", msg(10, "what next?", stream="plan"), [])["action"] == "skip"
    # in #pa the PA still counts: {founder, COO, PA} there needs an @**COO**
    w1 = make(wake, tmp_path / "a", members=plan)
    d = w1.on_message("COO", msg(11, "what next?", stream="pa"), [])
    assert d["action"] == "skip" and "2 role bots (COO, PA)" in d["reason"]


def test_pa_stays_out_of_company_streams_without_a_mention(wake, tmp_path):
    w = make(wake, tmp_path)
    # founder in All hands, tagging another role, nobody, or a wildcard: no PA
    for i, (text, flags) in enumerate([("@**COO** which project?", ["mentioned"]),
                                       ("@**CTO** thoughts", ["mentioned"]),
                                       ("status please", []),
                                       ("@**all** heads up", ["mentioned"])], start=20):
        d = w.on_message("PA", msg(i, text, stream="All hands", topic="כולם"), flags)
        assert d["action"] == "skip", text
    assert w.spawner.started == [] and w.role("PA").get("session") is None
    # even an explicit @**PA** there never wakes it; anything in #pa still does
    d = w.on_message("PA", msg(30, "@**PA** book it", stream="All hands"), ["mentioned"])
    assert d["action"] == "skip" and d["reason"] == "PA wakes only in #pa or a founder DM (ops#327)"
    assert w.spawner.started == []
    w2 = make(wake, tmp_path / "b")
    assert w2.on_message("PA", msg(31, "hi", stream="pa"), [])["action"] == "spawn"
    # a founder DM still wakes the PA (CTO on PR 133)
    w3 = make(wake, tmp_path / "d")
    assert w3.on_message("PA", msg(33, "hi", stream=None), [])["action"] == "spawn"
    # other roles' routing is unchanged
    w3 = make(wake, tmp_path / "c")
    assert w3.on_message("COO", msg(32, "@**COO** x", stream="All hands"),
                         ["mentioned"])["action"] == "spawn"


def test_pa_mention_elsewhere_never_touches_an_open_pa_session(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("PA", msg(50, "hi", stream="pa", topic="errands"), [])
    before = dict(w.role("PA")["session"])
    n = len(w.spawner.started)
    d = w.on_message("PA", msg(51, "@**PA** leave", stream="All hands", topic="כולם"), ["mentioned"])
    assert d["action"] == "skip"
    sess = w.role("PA")["session"]
    assert sess == before and not sess.get("pending") and len(w.spawner.started) == n


def test_pa_unwoken_room_never_becomes_the_close_room(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("PA", msg(40, "hi", stream="pa", topic="errands"), [])
    d = w.on_message("PA", msg(41, "company talk", stream="All hands", topic="כולם"), [])
    assert d["action"] == "skip"
    sess = w.role("PA")["session"]
    assert sess["stream"] == "pa" and not sess.get("pending")


@pytest.mark.parametrize("line,fix", [("", "PA_DIR missing in bot/.env"),
                                      ("PA_DIR=/no/such/folder\n", "not a folder")])
def test_pa_without_pa_dir_is_skipped_with_the_fix(wake, tmp_path, monkeypatch, line, fix):
    env = _env(tmp_path, monkeypatch, "FOUNDER_ZULIP_ID=8\nZULIP_CTO_EMAIL=c@x\n"
                                      "ZULIP_CTO_API_KEY=k\nZULIP_PA_EMAIL=pa@x\n"
                                      "ZULIP_PA_API_KEY=k\n" + line)
    cfg = wake.load_config(env)
    assert "PA" not in cfg["bots"] and "CTO" in cfg["bots"]
    assert fix in cfg["skipped"]["PA"]
    w = make(wake, tmp_path)
    wake.announce_skipped(cfg, w)
    assert w.emitted == [{"role": "PA", "action": "skip", "reason": cfg["skipped"]["PA"]}]


def test_backfill_reads_every_room_the_bot_is_in(wake):
    # a restart must not miss an untagged founder message in a one-bot room
    client = FakeClient()
    narrows = []

    def spy(method, path, params=None):
        narrows.append(params["narrow"])
        if params["narrow"] == []:
            return {"messages": [msg(5, stream="pa", topic="errands")]}
        return {"messages": []}
    client._request = spy
    pairs = wake.backfill(client, 1)
    assert [] in narrows
    assert [m["id"] for m, _ in pairs] == [5]


# -- Windows: claude is an npm .cmd shim; a failed spawn skips one message ----

class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def close(self):
        pass


class _FakeProc:
    pid = 4242

    def __init__(self):
        self.stdin = _Sink()

    def poll(self):
        return None


def test_spawner_resolves_argv0_and_keeps_the_message_on_stdin(wake, tmp_path, monkeypatch):
    shim = r"C:\Users\x\AppData\Roaming\npm\claude.cmd"
    seen = {}
    monkeypatch.setattr(wake.shutil, "which", lambda name: shim if name == "claude" else None)

    def popen(argv, **kw):
        seen["argv"], seen["kw"], seen["proc"] = argv, kw, _FakeProc()
        return seen["proc"]

    monkeypatch.setattr(wake.subprocess, "Popen", popen)
    monkeypatch.setitem(wake.AGENT, "cwd", str(tmp_path))
    nasty = 'run & del * | type "x" %PATH% %%'
    argv = wake.build_command("spawn", "CTO", str(uuid.uuid4()))
    wake.Spawner(log_dir=tmp_path).start(argv, nasty, "CTO")
    assert seen["argv"][0] == shim and seen["argv"][1:] == argv[1:]
    assert not seen["kw"].get("shell")
    for ch in ("&", "|", '"', "%"):
        assert not any(ch in a for a in seen["argv"])
    assert seen["proc"].stdin.data.decode("utf-8") == nasty


def test_spawner_names_the_fix_when_claude_is_not_on_path(wake, tmp_path, monkeypatch):
    monkeypatch.setattr(wake.shutil, "which", lambda name: None)
    monkeypatch.setattr(wake.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(wake.SpawnError, match="not on PATH.*Install"):
        wake.Spawner(log_dir=tmp_path).start(["claude", "-p"], "hi", "CTO")


def test_spawner_wraps_popen_oserror(wake, tmp_path, monkeypatch):
    monkeypatch.setattr(wake.shutil, "which", lambda name: r"C:\x\claude.cmd")

    def boom(*a, **k):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(wake.subprocess, "Popen", boom)
    with pytest.raises(wake.SpawnError, match="cannot start"):
        wake.Spawner(log_dir=tmp_path).start(["claude", "-p"], "hi", "CTO")


class _FailingSpawner(FakeSpawner):
    def __init__(self, error, fail_times=1):
        super().__init__()
        self.error, self.fail_times = error, fail_times

    def start(self, argv, prompt, role, cwd=None):
        if self.fail_times:
            self.fail_times -= 1
            raise self.error("cannot start 'claude': it is not on PATH")
        return super().start(argv, prompt, role, cwd)


def test_failed_spawn_skips_the_message_and_the_next_one_works(wake, tmp_path, capsys):
    w = make(wake, tmp_path, spawner=_FailingSpawner(wake.SpawnError, fail_times=1))
    d = w.on_message("CTO", msg(10), ["mentioned"])
    assert d["action"] == "error" and "not on PATH" in d["reason"]
    assert w.role("CTO")["session"] is None
    assert w.role("CTO")["last_message_id"] == 10
    assert "message 10 skipped" in capsys.readouterr().err
    assert w.tick() == []
    d = w.on_message("CTO", msg(11), ["mentioned"])
    assert d["action"] == "spawn" and len(w.spawner.started) == 1


def test_failed_resume_in_tick_does_not_crash_the_loop(wake, tmp_path):
    sp = _FailingSpawner(wake.SpawnError, fail_times=0)
    w = make(wake, tmp_path, spawner=sp)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.on_message("CTO", msg(11, "and also"), ["mentioned"])  # queued: turn running
    sp.finish_all()
    sp.fail_times = 1
    out = w.tick()
    assert out[-1]["action"] == "error"
    assert w.role("CTO")["session"] is None


# -- ops#318: untagged follow-ups, plain UTF-8 topics, stale sessions ----------

def test_untagged_follow_up_in_own_stream_topic_goes_to_the_open_session(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CTO", msg(10), ["mentioned"])
    sid = w.spawner.started[0][0][5]
    # while the turn runs it queues, then is resumed into the same session
    d = w.on_message("CTO", msg(11, "and the index? " + TRICKY), [])
    assert d["action"] == "queue"
    w.spawner.finish_all()
    [r] = w.tick()
    assert r["action"] == "resume"
    argv, prompt, _ = w.spawner.started[-1]
    assert argv[4:6] == ["--resume", sid]
    assert "and the index?" in prompt and not any("index" in a or "rm -rf" in a for a in argv)
    # after the turn, the next untagged one resumes at once
    w.spawner.finish_all()
    assert w.on_message("CTO", msg(12, "ok"), [])["action"] == "resume"
    assert len(w.spawner.started) == 3


def test_untagged_in_a_multi_bot_room_never_opens_a_session_nor_reaches_another_room(
        wake, tmp_path):
    w = make(wake, tmp_path, members=crowd)
    assert w.on_message("CTO", msg(10, stream="coo"), [])["action"] == "skip"
    assert w.spawner.started == []
    w.on_message("CTO", msg(11, "@**CTO**", stream="coo"), [])
    w.spawner.finish_all()
    assert w.on_message("CTO", msg(12, stream="other"), [])["action"] == "skip"
    assert w.on_message("CTO", msg(13, stream="coo", sender=99), [])["action"] == "skip"
    assert w.on_message("PM", msg(14, stream="coo"), [])["action"] == "skip"  # PM: not tagged
    assert len(w.spawner.started) == 1


def test_untagged_outside_the_own_stream_never_opens_a_session(wake, tmp_path):
    w = make(wake, tmp_path, members=real)
    assert w.on_message("CTO", msg(10, stream="feature"), [])["action"] == "skip"
    assert w.spawner.started == []
    w.on_message("CTO", msg(11, "@**CTO**", stream="feature"), ["mentioned"])
    w.spawner.finish_all()
    # #feature has no owner: the room's one open session (the CTO's) gets it
    assert w.on_message("CTO", msg(12, stream="feature", topic="other"), [])["action"] == "resume"
    assert w.on_message("CTO", msg(13, sender=99), [])["action"] == "skip"
    assert w.on_message("PM", msg(14, stream="feature"), [])["action"] == "skip"
    assert [a[4] for a, _, _ in w.spawner.started] == ["--session-id", "--resume"]


@pytest.mark.parametrize("role,stream", [("PM", "PM"), ("CTO", "cto"), ("CMO", "cmo"),
                                         ("CPSO", "cpso"), ("COO", "coo")])
def test_founder_untagged_in_the_role_own_stream_wakes_it(wake, tmp_path, role, stream):
    # founder 2026-10-17: in #PM the PM answered only when he tagged it
    w = make(wake, tmp_path, members=real)
    assert wake.own_stream(role) == stream
    assert w.on_message(role, msg(10, "hi", stream=stream), [])["action"] == "spawn"


def test_founder_untagged_in_a_shared_room_wakes_only_its_owner(wake, tmp_path):
    # the real shared rooms (setup_streams.py): PM reads #coo; COO and CTO read #PM.
    # One untagged founder line wakes the room's owner and no other bot.
    w = make(wake, tmp_path, members=real)
    assert w.on_message("PM", msg(10, "hi", stream="coo"), [])["action"] == "skip"
    assert w.on_message("COO", msg(11, "hi", stream="PM"), [])["action"] == "skip"
    assert w.on_message("CTO", msg(12, "hi", stream="PM"), [])["action"] == "skip"
    assert w.spawner.started == []
    assert w.on_message("COO", msg(13, "hi", stream="coo"), [])["action"] == "spawn"
    assert len(w.spawner.started) == 1


def test_every_home_stream_is_a_stream_setup_streams_makes(wake):
    sys.path.insert(0, str(BOT_DIR))
    try:
        import setup_streams
    finally:
        sys.path.remove(str(BOT_DIR))
    homes = set(wake.AGENT["home_streams"].values())
    assert homes <= set(setup_streams.STREAMS), sorted(homes - set(setup_streams.STREAMS))


def test_a_dm_is_answered_in_the_role_own_room(wake):
    assert wake.own_stream("CTO") == "cto" and wake.own_stream("PA") == "pa"


HEBREW = "כללי"


def test_hebrew_room_stays_plain_utf8_in_state_and_prompt(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CMO", msg(10, "שלום", stream=HEBREW, topic="old topic"), ["mentioned"])
    raw = (tmp_path / "state.json").read_bytes()
    assert HEBREW.encode("utf-8") in raw and b"\\u05db" not in raw
    assert wake.load_state(tmp_path / "state.json")["roles"]["CMO"]["session"]["stream"] == HEBREW
    _, prompt, _ = w.spawner.started[0]
    assert f'--room "{HEBREW}"' in prompt and "--topic" not in prompt
    assert "\\u05" not in prompt and "\ufffd" not in prompt
    assert f"the room #{HEBREW}" in prompt
    close = wake.close_prompt("CMO", HEBREW)
    assert f'--room "{HEBREW}"' in close and "\\u05" not in close
    # the prompt reaches the child as whole UTF-8 characters
    assert prompt.encode("utf-8").decode("utf-8") == prompt


def test_hebrew_room_reaches_a_real_child_on_stdin_intact(wake, tmp_path, monkeypatch):
    out = tmp_path / "seen.txt"
    script = ("import sys,pathlib; pathlib.Path(sys.argv[1]).write_bytes("
              "sys.stdin.buffer.read())")
    monkeypatch.setattr(wake, "resolve_program", lambda argv: argv)
    sp = wake.Spawner(log_dir=tmp_path)
    prompt = wake.message_prompt("CMO", msg(10, stream=HEBREW), first=True)
    pid = sp.start([sys.executable, "-c", script, str(out)], prompt, "CMO", str(tmp_path))
    sp.procs[pid].wait(timeout=30)
    assert out.read_bytes().decode("utf-8") == prompt
    assert f'--room "{HEBREW}"' in out.read_text(encoding="utf-8")


def test_closing_session_that_never_started_is_dropped_not_resumed(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    sid = str(uuid.uuid4())
    # left by an earlier run: closing, its close turn not run, no process ever
    w.role("CMO")["session"] = {"id": sid, "stream": "cmo", "topic": HEBREW, "closing": True,
                                "started_at": clock.t, "last_founder_at": clock.t,
                                "pid": None, "pending": []}
    [d] = w.tick()
    assert d["action"] == "closed" and "dropped" in d["reason"]
    assert w.spawner.started == [] and w.role("CMO")["session"] is None
    d = w.on_message("CMO", msg(10, stream="cmo", topic=HEBREW), ["mentioned"])
    assert d["action"] == "spawn"
    argv = w.spawner.started[0][0]
    assert argv[4] == "--session-id" and argv[5] != sid
    assert not any(a == "--resume" for a, *_ in w.spawner.started)


def test_session_whose_conversation_does_not_exist_starts_fresh(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CMO", msg(10, stream="cmo"), ["mentioned"])
    sid = w.spawner.started[0][0][5]
    w.spawner.finish_all()
    w.spawner.missing.add(sid)  # claude exited before it made the conversation
    d = w.on_message("CMO", msg(11, "again", stream="cmo"), ["mentioned"])
    assert d["action"] == "spawn"
    argv = w.spawner.started[-1][0]
    assert argv[4] == "--session-id" and argv[5] != sid
    assert [a[0][4] for a in w.spawner.started] == ["--session-id", "--session-id"]
    # an untagged follow-up to a stale session is not resumed either: in a
    # one-bot room it opens a fresh one
    w2 = make(wake, tmp_path / "b")
    w2.on_message("CMO", msg(20, stream="cmo"), [])
    w2.spawner.finish_all()
    stale = w2.spawner.started[0][0][5]
    w2.spawner.missing.add(stale)
    assert w2.on_message("CMO", msg(21, stream="cmo"), [])["action"] == "spawn"
    assert [a[0][4] for a in w2.spawner.started] == ["--session-id", "--session-id"]
    assert w2.role("CMO")["session"]["id"] != stale


def test_founder_message_to_a_closing_session_with_nothing_running_starts_fresh(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    sid = str(uuid.uuid4())
    w.role("CTO")["session"] = {"id": sid, "stream": "cto", "topic": "t", "closing": True,
                                "started_at": clock.t, "last_founder_at": clock.t,
                                "pid": 5, "pid_created": 50, "pending": []}
    d = w.on_message("CTO", msg(10, topic="t"), ["mentioned"])
    assert d["action"] == "spawn"
    assert [a[0][4] for a in w.spawner.started] == ["--session-id"]
    assert w.spawner.started[0][0][5] != sid


def test_real_spawner_checks_the_conversation_file(wake, tmp_path, monkeypatch):
    sid = str(uuid.uuid4())
    monkeypatch.setitem(wake.AGENT, "conversations",
                        str(tmp_path / "projects" / "*" / "{session_id}.jsonl"))
    sp = wake.Spawner(log_dir=tmp_path)
    assert sp.conversation_exists(sid) is True  # no folder at all: cannot tell
    (tmp_path / "projects" / "C--PlayGround-ops").mkdir(parents=True)
    assert sp.conversation_exists(sid) is False
    (tmp_path / "projects" / "C--PlayGround-ops" / f"{sid}.jsonl").write_text("{}")
    assert sp.conversation_exists(sid) is True
    with pytest.raises(ValueError):
        sp.conversation_exists("../../etc/passwd")


# -- ops#322: rooms ----------------------------------------------------------

def test_322_an_invite_never_wakes_a_role(wake, tmp_path):
    w = make(wake, tmp_path)
    invite = {"id": 1, "type": "subscription", "op": "add",
              "subscriptions": [{"name": "roadmap", "stream_id": 9}]}
    wake.process_batch(w, "CTO", [invite])
    assert w.spawner.started == []
    assert [(d["action"], d["reason"]) for d in w.emitted] == \
        [("skip", "invited to #roadmap; an invite never wakes a role")]
    assert w.role("CTO")["last_event_id"] == 1


def test_322_in_a_one_bot_room_an_untagged_founder_message_wakes(wake, tmp_path):
    w = make(wake, tmp_path)  # #cto: the founder and the CTO bot
    d = w.on_message("CTO", msg(10, "no tag at all", stream="cto"), [])
    assert d["action"] == "spawn" and d["room"] == "cto"


def test_322_in_a_multi_bot_room_only_the_tagged_role_wakes(wake, tmp_path):
    w = make(wake, tmp_path, members=crowd)
    m = msg(10, "@**PM** what ships this week?", stream="All hands")
    decisions = {role: w.on_message(role, m, ["mentioned"]) for role in ("COO", "CTO", "PM", "PA")}
    assert {r: d["action"] for r, d in decisions.items()} == \
        {"COO": "skip", "CTO": "skip", "PM": "spawn", "PA": "skip"}
    assert [role for _, _, role in w.spawner.started] == ["PM"]
    # a quote's silent @_**CTO** and an @**all** are no tag of the CTO
    for text in ("@_**CTO** [said](#near/3):\n```quote\nx\n```\nok", "@**all** hello"):
        assert w.on_message("CTO", msg(11, text, stream="All hands"), ["mentioned"])["action"] == "skip"


def test_322_a_mention_of_a_role_not_in_the_room_does_not_wake_it(wake, tmp_path):
    w = make(wake, tmp_path, members=crowd)  # no CMO among them
    d = w.on_message("CMO", msg(10, "@**CMO** a post?", stream="All hands"), ["mentioned"])
    assert d["action"] == "skip" and d["reason"] == "CMO is not a member of #All hands"
    assert w.spawner.started == []


def test_322_unknown_members_only_an_explicit_tag_wakes(wake, tmp_path):
    def broken(role, room):
        raise wake.ZulipError("GET streams/1/members: cannot reach Zulip")
    w = make(wake, tmp_path, members=broken)
    assert w.on_message("CTO", msg(10, "untagged", stream="cto"), ["mentioned"])["action"] == "skip"
    assert w.on_message("CTO", msg(11, "@**CTO** tagged", stream="cto"), [])["action"] == "spawn"


def test_322_removing_a_role_closes_its_session_in_that_room(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CTO", msg(10, stream="design"), [])
    pid = w.role("CTO")["session"]["pid"]
    w.on_message("CTO", msg(11, "queued", stream="design"), [])  # a turn runs: queued
    removed = {"id": 4, "type": "subscription", "op": "remove",
               "subscriptions": [{"name": "design", "stream_id": 9}]}
    wake.process_batch(w, "CTO", [removed])
    assert w.spawner.stopped == [pid]
    assert w.role("CTO")["session"] is None
    assert w.emitted[-1]["action"] == "closed" and "removed from #design" in w.emitted[-1]["reason"]
    assert len(w.spawner.started) == 1  # the queued message from that room is dropped
    # a removal from another room leaves the session alone
    w.on_message("CTO", msg(12, stream="cto"), [])
    w.on_subscription("CTO", {"op": "remove", "subscriptions": [{"name": "design"}]})
    assert w.role("CTO")["session"]["stream"] == "cto"


def test_322_a_session_whose_role_left_the_room_closes_on_the_next_message(wake, tmp_path):
    # an old queue registered before "subscription" events: the membership
    # check on the next founder message in that room closes it
    rooms = {"design": alone("CTO", "design")}
    w = make(wake, tmp_path, members=lambda role, room: rooms[room])
    w.on_message("CTO", msg(10, stream="design"), [])
    w.spawner.finish_all()
    rooms["design"] = [user(FOUNDER, "Founder", bot=False), user(101, "COO")]
    d = w.on_message("CTO", msg(11, "@**CTO** still there?", stream="design"), ["mentioned"])
    assert d["action"] == "skip" and "not a member" in d["reason"]
    assert w.role("CTO")["session"] is None and len(w.spawner.started) == 1


HOSTILE = ["a`touch /tmp/pwn`", "a$(touch /tmp/pwn)", 'a" && touch /tmp/pwn; "', "a\\b",
           "a\nb", " lead", "x" * 61, "", "a;b", "a|b", "a>b"]


@pytest.mark.parametrize("name", HOSTILE)
def test_322_a_hostile_room_name_never_reaches_a_hint(wake, tmp_path, name):
    assert wake.safe_room(name) is False
    with pytest.raises(ValueError):
        wake.message_prompt("CTO", msg(10, stream=name), first=True)
    with pytest.raises(ValueError):
        wake.close_prompt("CTO", name)
    w = make(wake, tmp_path)
    d = w.on_message("CTO", msg(10, "@**CTO** hi", stream=name), ["mentioned"])
    assert d["action"] == "skip" and "not safe text" in d["reason"]
    assert w.spawner.started == []


@pytest.mark.parametrize("name", ["coo", "All hands", "כללי", "roadmap-2026_v1.2", "x" * 60])
def test_322_a_safe_room_name_is_accepted(wake, name):
    assert wake.safe_room(name) is True
    assert f'--room "{name}"' in wake.close_prompt("CTO", name)


def test_322_the_pa_in_a_shared_room_is_told_who_else_reads_it(wake, tmp_path):
    def guest(role, room):  # #pa with a human guest in it
        return [user(FOUNDER, "Founder", bot=False), user(106, "PA"),
                user(300, "Guest", bot=False)]
    w = make(wake, tmp_path, members=guest)
    w.bot_ids = {106: "PA"}
    w.on_message("PA", msg(10, "my schedule?", stream="pa"), [])
    prompt = w.spawner.started[0][1]
    assert "Also in #pa: Guest." in prompt
    assert "others read this room; share personal data only if he asks here." in prompt.lower()
    # alone with the founder in #pa: no such line
    w2 = make(wake, tmp_path / "b")
    w2.bot_ids = {100: "PA"}
    w2.on_message("PA", msg(11, "my schedule?", stream="pa"), [])
    assert "Also in" not in w2.spawner.started[0][1]


def test_322_a_saved_session_with_an_unsafe_room_closes_without_a_hint(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.role("CTO")["session"] = {"id": str(uuid.uuid4()), "stream": "a$(x)", "topic": "t",
                                "started_at": clock.t, "last_founder_at": clock.t,
                                "pid": 5, "pid_created": 50}
    clock.t += 16 * 60
    [d] = w.tick()  # idle: the close turn would need a hint; it is refused, not crashed
    assert d["action"] == "error" and "not safe text" in d["reason"]
    assert w.spawner.started == [] and w.role("CTO")["session"] is None


# -- ops#322: everyone in a room reads everything ------------------------------

def _room(rows):
    """A room's history that grows as the test posts: the fetch reads it live."""
    def fetch(role, room, n):
        return [m for m in rows if m.get("display_recipient") == room][-n:]
    return fetch


def bot_msg(i, name, content, stream="All hands"):
    # the bot ids of crowd(); anyone else gets an id of their own
    m = msg(i, content, sender={"COO": 101, "CTO": 102, "PM": 103}.get(name, 200 + i),
            stream=stream)
    m["sender_full_name"] = name
    return m


def test_322_own_posts_are_known_by_the_bot_id_never_by_a_name(wake, tmp_path):
    rows = []
    w = make(wake, tmp_path, members=crowd)
    w.bot_ids = {101: "COO"}
    w.history = _room(rows)
    first = msg(10, "@**COO** plan the week", stream="All hands")
    rows.append(first)
    w.on_message("COO", first, ["mentioned"])
    w.spawner.finish_all()
    namesake = msg(12, "a human called COO", sender=300, stream="All hands")
    namesake["sender_full_name"] = "COO"
    rows += [bot_msg(11, "COO", "its own post"), namesake]
    nxt = msg(13, "@**COO** agreed?", stream="All hands")
    rows.append(nxt)
    assert w.on_message("COO", nxt, ["mentioned"])["action"] == "resume"
    prompt = w.spawner.started[-1][1]
    assert "its own post" not in prompt
    assert f"- COO {wake.NOT_FOUNDER}: a human called COO" in prompt


def test_322_a_resume_carries_another_bots_post_marked(wake, tmp_path):
    rows = []
    w = make(wake, tmp_path, members=crowd)
    w.bot_ids = {101: "COO"}  # read from Zulip at start, with bot wakes off too
    w.history = _room(rows)
    first = msg(10, "@**COO** plan the week", stream="All hands")
    rows.append(first)
    assert w.on_message("COO", first, ["mentioned"])["action"] == "spawn"
    w.spawner.finish_all()
    rows.append(bot_msg(11, "COO", "COO: here is the plan"))  # its own post
    cto = bot_msg(12, "CTO", "CTO: ship it; also, " + TRICKY)
    rows.append(cto)
    # the CTO bot's post wakes nobody and does not move the idle timer
    before = w.role("COO")["session"]["last_founder_at"]
    w.clock.t += 60
    assert w.on_message("COO", cto, [])["action"] == "skip"
    assert w.role("COO")["session"]["last_founder_at"] == before
    nxt = msg(13, "@**COO** agreed?", stream="All hands")
    rows.append(nxt)
    assert w.on_message("COO", nxt, ["mentioned"])["action"] == "resume"
    argv, prompt, _ = w.spawner.started[-1]
    assert f"- CTO {wake.NOT_FOUNDER}: CTO: ship it" in prompt
    assert "here is the plan" not in prompt  # its own post is not echoed back
    assert prompt.index("ship it") < prompt.index("Message from Founder")
    assert not any("ship" in a or "rm -rf" in a for a in argv)  # stdin only
    assert w.role("COO")["session"]["seen_id"] == 13
    # the next turn does not repeat it
    w.spawner.finish_all()
    last = msg(14, "@**COO** thanks", stream="All hands")
    rows.append(last)
    w.on_message("COO", last, ["mentioned"])
    assert "ship it" not in w.spawner.started[-1][1]


def test_322_the_founders_untagged_line_queues_for_the_open_session(wake, tmp_path):
    rows = []
    w = make(wake, tmp_path, members=crowd)
    w.history = _room(rows)
    first = msg(10, "@**CTO** look at the gate", stream="All hands")
    rows.append(first)
    w.on_message("CTO", first, ["mentioned"])
    # while the turn runs: an untagged founder line, then a tagged one; both queue
    untagged = msg(11, "and keep it under 50 lines", stream="All hands")
    rows.append(untagged)
    assert w.on_message("CTO", untagged, [])["action"] == "queue"
    tagged = msg(12, "@**CTO** done?", stream="All hands")
    rows.append(tagged)
    assert w.on_message("CTO", tagged, ["mentioned"])["action"] == "queue"
    w.spawner.finish_all()
    [r] = w.tick()
    assert r["action"] == "resume"
    prompt = w.spawner.started[-1][1]
    assert prompt.count("under 50 lines") == 1  # a message, not repeated as catch-up
    assert prompt.index("under 50 lines") < prompt.index("done?")
    assert w.role("CTO")["session"]["seen_id"] == 12


def _open(w, role, room, i):
    """Open a session for ``role`` in ``room`` and let its turn end."""
    w.on_message(role, msg(i, f"@**{role}** start", stream=room), ["mentioned"])
    w.spawner.finish_all()


def test_322_untagged_in_a_multi_bot_room_resumes_the_one_open_session(wake, tmp_path):
    # (d) All hands: several role bots, no owner, so the one open session gets it
    w = make(wake, tmp_path, members=crowd)
    _open(w, "COO", "All hands", 10)
    m = msg(11, "and the budget too", stream="All hands")
    got = {r: w.on_message(r, m, [])["action"] for r in ("COO", "CTO", "PM")}
    assert got == {"COO": "resume", "CTO": "skip", "PM": "skip"}
    assert [(a[4], role) for a, _, role in w.spawner.started] == \
        [("--session-id", "COO"), ("--resume", "COO")]
    assert "and the budget too" in w.spawner.started[-1][1]


def test_322_untagged_in_a_multi_bot_room_with_no_open_session_wakes_nobody(wake, tmp_path):
    w = make(wake, tmp_path, members=crowd)
    _open(w, "COO", "cto", 10)  # open, but in another room
    m = msg(11, "anyone?", stream="All hands")
    assert {w.on_message(r, m, [])["action"] for r in ("COO", "CTO", "PM")} == {"skip"}
    assert len(w.spawner.started) == 1  # never a spawn


def test_322_untagged_in_a_multi_bot_room_with_two_open_sessions_wakes_nobody(wake, tmp_path):
    w = make(wake, tmp_path, members=crowd)
    _open(w, "COO", "All hands", 10)
    _open(w, "PM", "All hands", 11)
    w.clock.t += 60
    m = msg(12, "which of you?", stream="All hands")
    for r in ("COO", "CTO", "PM"):
        d = w.on_message(r, m, [])
        assert d["action"] == "skip" and "only @**" in d["reason"]
    assert len(w.spawner.started) == 2
    # the founder is still talking there: both idle timers move
    assert w.role("COO")["session"]["last_founder_at"] == w.clock.t
    assert w.role("PM")["session"]["last_founder_at"] == w.clock.t


def test_322_untagged_in_a_closing_session_room_wakes_nobody(wake, tmp_path):
    w = make(wake, tmp_path, members=crowd)
    _open(w, "COO", "All hands", 10)
    w.role("COO")["session"]["closing"] = True
    assert w.on_message("COO", msg(11, "x", stream="All hands"), [])["action"] == "skip"
    assert len(w.spawner.started) == 1


def _pm_room(role, room):
    """#PM as setup_streams.py makes it: the PM, the COO and the CTO."""
    return [user(FOUNDER, "Founder", bot=False), user(103, "PM"), user(101, "COO"),
            user(102, "CTO")]


def _coo_room(role, room):
    """#coo with the PM and an invited CTO."""
    return [user(FOUNDER, "Founder", bot=False), user(101, "COO"), user(103, "PM"),
            user(102, "CTO")]


def test_322_owner_untagged_in_pm_wakes_the_pm_only(wake, tmp_path):
    # (a) #PM {PM, COO, CTO}, nothing open: the owner spawns, the others skip
    w = make(wake, tmp_path, members=_pm_room)
    m = msg(10, "a new feature request", stream="PM")
    got = {r: w.on_message(r, m, [])["action"] for r in ("PM", "COO", "CTO")}
    assert got == {"PM": "spawn", "COO": "skip", "CTO": "skip"}
    assert [role for _, _, role in w.spawner.started] == ["PM"]


def test_322_owner_untagged_in_coo_wakes_the_coo(wake, tmp_path):
    # (b) #coo {COO, PM}, nothing open: the COO spawns, the PM skips
    w = make(wake, tmp_path, members=real)
    m = msg(10, "what is on today?", stream="coo")
    got = {r: w.on_message(r, m, [])["action"] for r in ("COO", "PM")}
    assert got == {"COO": "spawn", "PM": "skip"}
    assert [role for _, _, role in w.spawner.started] == ["COO"]


def test_322_owner_gets_the_line_even_when_another_role_has_the_open_session(wake, tmp_path):
    # (c) #coo while the CTO has the only open session there: the COO gets the
    # line, the CTO skips and its idle timer moves
    clock = Clock()
    w = make(wake, tmp_path, clock=clock, members=_coo_room)
    _open(w, "CTO", "coo", 10)
    clock.t += 60
    m = msg(11, "and the deadline?", stream="coo")
    got = {r: w.on_message(r, m, []) for r in ("COO", "CTO", "PM")}
    assert {r: d["action"] for r, d in got.items()} == \
        {"COO": "spawn", "CTO": "skip", "PM": "skip"}
    assert "extends" in got["CTO"]["reason"] and "owner, COO" in got["CTO"]["reason"]
    assert w.role("CTO")["session"]["last_founder_at"] == clock.t
    assert [role for _, _, role in w.spawner.started] == ["CTO", "COO"]
    assert "and the deadline?" in w.spawner.started[-1][1]


def test_322_a_tag_in_an_owned_room_wakes_only_that_role(wake, tmp_path):
    # (e) @**CTO** in #PM: only the CTO, not the owner
    w = make(wake, tmp_path, members=_pm_room)
    m = msg(10, "@**CTO** can we store this?", stream="PM")
    got = {r: w.on_message(r, m, ["mentioned"])["action"] for r in ("PM", "COO", "CTO")}
    assert got == {"PM": "skip", "COO": "skip", "CTO": "spawn"}
    assert [role for _, _, role in w.spawner.started] == ["CTO"]


def test_322_the_owner_is_matched_without_case(wake, tmp_path, monkeypatch):
    # without #135's map the PM's own room defaults to "pm"; Zulip names
    # are case-insensitive, so #PM is still the PM's
    monkeypatch.setitem(wake.AGENT, "home_streams", {"PA": "pa"})
    w = make(wake, tmp_path, members=_pm_room)
    m = msg(10, "hi", stream="PM")
    got = {r: w.on_message(r, m, [])["action"] for r in ("PM", "COO", "CTO")}
    assert got == {"PM": "spawn", "COO": "skip", "CTO": "skip"}


def test_322_an_invited_role_in_pa_does_not_stop_the_pas_untagged_wake(wake, tmp_path):
    pa_room = [user(FOUNDER, "Founder", bot=False), user(104, "PA"), user(102, "CTO")]
    w = make(wake, tmp_path, members=lambda role, room: pa_room)
    m = msg(10, "remind me at 5", stream="pa")
    assert w.on_message("PA", m, [])["action"] == "spawn"
    assert w.on_message("CTO", m, [])["action"] == "skip"
    assert [role for _, _, role in w.spawner.started] == ["PA"]


def test_322_the_catch_up_is_capped_and_names_the_read_since_command(wake, tmp_path):
    rows = []
    w = make(wake, tmp_path, members=crowd)
    w.history = _room(rows)
    first = msg(10, "@**PM** status", stream="All hands")
    rows.append(first)
    w.on_message("PM", first, ["mentioned"])
    w.spawner.finish_all()
    rows += [bot_msg(i, "CTO", f"chatter {i}") for i in range(11, 41)]
    nxt = msg(41, "@**PM** and now?", stream="All hands")
    rows.append(nxt)
    w.on_message("PM", nxt, ["mentioned"])
    prompt = w.spawner.started[-1][1]
    assert prompt.count(wake.NOT_FOUNDER) == wake.HISTORY_MESSAGES
    assert "chatter 40" in prompt and "chatter 21\n" in prompt and "chatter 20\n" not in prompt
    assert ('--as PM read --room "All hands" --since 10 --json') in prompt


def test_322_a_session_saved_before_seen_id_resumes_without_a_catch_up(wake, tmp_path):
    rows = [msg(5, "@**CTO** old"), bot_msg(6, "COO", "noise", stream="cto")]
    w = make(wake, tmp_path)
    w.history = _room(rows)
    w.on_message("CTO", rows[0], ["mentioned"])
    w.spawner.finish_all()
    del w.role("CTO")["session"]["seen_id"]
    w.on_message("CTO", msg(7, "next"), [])
    assert "noise" not in w.spawner.started[-1][1]
    assert w.role("CTO")["session"]["seen_id"] == 7


def test_322_the_spawn_handoff_marks_lines_not_from_the_founder(wake, tmp_path):
    rows = [bot_msg(5, "CTO", "CTO: closing line", stream="cto"), msg(6, "hi")]
    w = make(wake, tmp_path)
    w.history = _room(rows)
    w.on_message("CTO", rows[-1], [])
    prompt = w.spawner.started[0][1]
    assert f"- CTO {wake.NOT_FOUNDER}: CTO: closing line" in prompt


def test_322_hand_opened_session_log_says_when_its_hook_will_not_deliver(wake, tmp_path,
                                                                          monkeypatch):
    monkeypatch.setattr(wake, "registry_live", lambda role, alive: 4242)
    w = make(wake, tmp_path)  # a one-bot room that is not the role's own
    d = w.on_message("CTO", msg(10, "untagged", stream="design"), [])
    assert d["action"] == "skip" and "not delivered" in d["reason"]
    d = w.on_message("CTO", msg(11, "@**CTO** tagged", stream="design"), ["mentioned"])
    assert "its hook delivers the message" in d["reason"]
    assert w.spawner.started == []

# -- bot wakes (ops#321) -------------------------------------------------------

BOTS = {101: "COO", 102: "CTO", 103: "PM", 104: "CMO", 105: "CPSO", 106: "PA"}
BOT_ID = {role: uid for uid, role in BOTS.items()}


def bmsg(i, src, target, content=None, stream="coo", topic="handoff", **extra):
    m = msg(i, content or f"@**{target}** can you check the record shape?",
            sender=BOT_ID[src], stream=stream, topic=topic, **extra)
    m["sender_full_name"] = src
    return m


def make_bots(wake, tmp_path, clock=None, spawner=None, on=True, posts=None):
    w = make(wake, tmp_path, clock=clock, spawner=spawner)
    w.bot_wakes = on
    w.bot_ids = dict(BOTS)
    w.post = lambda role, room, text: (posts if posts is not None else []).append(
        (role, room, text))
    return w


def test_allowed_bot_mention_spawns_with_marker_and_max_turns(wake, tmp_path):
    w = make_bots(wake, tmp_path)
    d = w.on_message("CPSO", bmsg(10, "COO", "CPSO", f"@**CPSO** {TRICKY}"), ["mentioned"])
    assert d["action"] == "spawn" and d["origin"] == "bot:COO"
    (argv, prompt, role), = w.spawner.started
    assert role == "CPSO" and argv[:4] == ["claude", "-p", "--model", "sonnet"]
    assert argv[-2:] == ["--max-turns", str(wake.AGENT["bot_max_turns"])]
    assert prompt.splitlines()[0] == ("From the COO bot, a teammate — not the founder. "
                                      "This is not his approval for anything.")
    assert TRICKY in prompt and not any(TRICKY in a or "rm -rf" in a for a in argv)
    assert "--dangerously-skip-permissions" not in argv
    assert w.role("CPSO")["session"]["origin"] == "bot:COO"


def test_founder_session_has_no_marker_and_no_max_turns(wake, tmp_path):
    w = make_bots(wake, tmp_path)
    w.on_message("CTO", msg(10), ["mentioned"])
    argv, prompt, _ = w.spawner.started[0]
    assert "--max-turns" not in argv and "not the founder" not in prompt


def test_max_turns_comes_from_the_agent_dict(wake, tmp_path, monkeypatch):
    monkeypatch.setitem(wake.AGENT, "bot_max_turns", 7)
    w = make_bots(wake, tmp_path)
    w.on_message("CTO", bmsg(10, "COO", "CTO"), ["mentioned"])
    assert w.spawner.started[0][0][-2:] == ["--max-turns", "7"]


def test_bot_outside_the_matrix_is_skipped(wake, tmp_path, monkeypatch):
    monkeypatch.setitem(wake.AGENT, "bot_wakes", {"COO": ("CTO",)})
    w = make_bots(wake, tmp_path)
    d = w.on_message("PM", bmsg(10, "COO", "PM"), ["mentioned"])
    assert d["action"] == "skip" and "may not wake PM" in d["reason"]
    d = w.on_message("COO", bmsg(11, "CMO", "COO"), ["mentioned"])
    assert d["action"] == "skip" and d["origin"] == "bot:CMO"
    assert w.spawner.started == []


def test_unknown_bot_or_impostor_name_is_not_the_founder(wake, tmp_path):
    w = make_bots(wake, tmp_path)
    pool = msg(10, "@**CTO** go", sender=150)  # pool / GitHub / notifier bot
    pool["sender_full_name"] = "COO"
    d = w.on_message("CTO", pool, ["mentioned"])
    assert d["action"] == "skip" and "not the founder" in d["reason"]
    assert w.spawner.started == []


@pytest.mark.parametrize("stream", ["pa", "All hands", "coo", None])
@pytest.mark.parametrize("src", ["COO", "CTO", "PM", "CMO", "CPSO"])
def test_no_bot_wakes_the_pa(wake, tmp_path, src, stream):
    w = make_bots(wake, tmp_path)
    d = w.on_message("PA", bmsg(10, src, "PA", stream=stream, topic="errands"), ["mentioned"])
    assert d["action"] == "skip"
    if stream is not None:
        assert "only the founder wakes the PA" in d["reason"]
    assert w.spawner.started == []


def test_a_bot_never_wakes_its_own_role(wake, tmp_path):
    w = make_bots(wake, tmp_path)
    d = w.on_message("COO", bmsg(10, "COO", "COO"), ["mentioned"])
    assert d["action"] == "skip" and "own role" in d["reason"]
    assert w.spawner.started == []


def test_bot_dm_never_wakes(wake, tmp_path):
    w = make_bots(wake, tmp_path)
    d = w.on_message("CTO", bmsg(10, "COO", "CTO", stream=None), [])
    assert d["action"] == "skip" and "never by DM" in d["reason"]
    assert w.spawner.started == []


def test_untagged_bot_message_in_the_role_own_stream_is_skipped(wake, tmp_path):
    w = make_bots(wake, tmp_path)
    # even with the CTO's session open in that topic (ops#318's untagged path)
    w.on_message("CTO", msg(10, stream="cto", topic="t"), ["mentioned"])
    w.spawner.finish_all()
    d = w.on_message("CTO", bmsg(11, "COO", "CTO", "and the index?", stream="cto",
                                 topic="t"), [])
    assert d["action"] == "skip" and "no @**CTO**" in d["reason"]
    # a "mentioned" flag without an explicit @**CTO** in the text (a group, silent)
    d = w.on_message("CTO", bmsg(12, "COO", "CTO", "@_**CTO** fyi", stream="cto",
                                 topic="t"), ["mentioned"])
    assert d["action"] == "skip"
    # the PA's home stream rule is the founder's only
    d = w.on_message("CTO", bmsg(13, "COO", "CTO", "hi", stream="pa"), [])
    assert d["action"] == "skip"
    assert len(w.spawner.started) == 1


def test_mentions_matches_only_an_explicit_role_mention(wake):
    assert wake.mentions("CTO", "hey @**CTO** look")
    assert wake.mentions("CTO", "hey @**cto|102** look")
    assert not wake.mentions("CTO", "hey @_**CTO** look")
    assert not wake.mentions("CTO", "hey @**CTOs** look")
    assert not wake.mentions("CTO", "hey @**all** look")


def test_bot_wakes_off_skips_everything(wake, tmp_path):
    w = make_bots(wake, tmp_path, on=False)
    d = w.on_message("CPSO", bmsg(10, "COO", "CPSO"), ["mentioned"])
    assert d["action"] == "skip" and "bot wakes are off" in d["reason"]
    assert w.spawner.started == []
    assert w.on_message("CTO", msg(11), ["mentioned"])["action"] == "spawn"


@pytest.mark.parametrize("line,on", [("", False), ("BOT_WAKES=off\n", False),
                                     ("BOT_WAKES=yes\n", False), ("BOT_WAKES=on\n", True),
                                     ("BOT_WAKES=ON\n", True)])
def test_bot_wakes_starts_off_and_only_on_turns_it_on(wake, tmp_path, monkeypatch, line, on):
    monkeypatch.delenv("BOT_WAKES", raising=False)
    env = _env(tmp_path, monkeypatch, "FOUNDER_ZULIP_ID=8\nZULIP_CTO_EMAIL=c@x\n"
                                      "ZULIP_CTO_API_KEY=k\n" + line)
    assert wake.load_config(env)["bot_wakes"] is on


def _ping_pong(w, clock, n, start=10, topic="handoff"):
    """n bot turns in one topic, COO and CPSO tagging each other."""
    out = []
    for i in range(n):
        src, dst = ("COO", "CPSO") if i % 2 == 0 else ("CPSO", "COO")
        w.spawner.finish_all()
        out.append(w.on_message(dst, bmsg(start + i, src, dst, topic=topic), ["mentioned"]))
        clock.t += 60
    return out


def test_topic_cap_posts_the_stop_line_once(wake, tmp_path):
    clock, posts = Clock(), []
    w = make_bots(wake, tmp_path, clock=clock, posts=posts)
    ds = _ping_pong(w, clock, 9)
    assert [d["action"] for d in ds[:6]] == ["spawn", "spawn", "resume", "resume",
                                             "resume", "resume"]
    assert all(d["action"] == "skip" and "bot loop stopped" in d["reason"] for d in ds[6:])
    # posted once, by the bot of the role the stopped message was for
    line = "bot loop stopped; @**Hemi|8**, @-mention a role to continue"
    assert posts == [("CPSO", "coo", line)]
    assert [d.get("stop_line") for d in ds[6:]] == [True, False, False]
    assert len(w.spawner.started) == 6
    # ops#322: the cap is the room's, so another topic in it is stopped too
    d = w.on_message("CTO", bmsg(50, "COO", "CTO", topic="other"), ["mentioned"])
    assert d["action"] == "skip" and "bot loop stopped in this room" in d["reason"]
    # another room is not
    d = w.on_message("CTO", bmsg(51, "COO", "CTO", stream="eng"), ["mentioned"])
    assert d["action"] == "spawn"


def test_founder_message_resets_the_topic_and_bypasses_every_cap(wake, tmp_path):
    clock, posts = Clock(), []
    w = make_bots(wake, tmp_path, clock=clock, posts=posts)
    _ping_pong(w, clock, 7)
    assert len(posts) == 1
    # the founder bypasses the cap in the stopped topic
    w.spawner.finish_all()
    d = w.on_message("CPSO", msg(30, "@**CPSO** go on", stream="coo", topic="handoff"),
                     ["mentioned"])
    assert d["action"] == "resume" and "origin" not in d
    # and his message reset it: six more bot turns, then one more stop line
    ds = _ping_pong(w, clock, 7, start=40)
    assert [d["action"] for d in ds].count("skip") == 1
    assert len(posts) == 2


def test_founder_bypasses_the_daily_and_role_caps(wake, tmp_path):
    clock = Clock()
    w = make_bots(wake, tmp_path, clock=clock)
    w._bots()["log"] = [{"at": clock.t, "role": "CTO", "from": "COO", "topic": f"x\n{i}",
                         "spawn": True} for i in range(30)]
    assert w.on_message("CTO", bmsg(10, "COO", "CTO"), ["mentioned"])["action"] == "skip"
    assert w.on_message("CTO", msg(11), ["mentioned"])["action"] == "spawn"


def test_role_spawn_cap_per_hour_and_per_day(wake, tmp_path):
    clock = Clock()
    w = make_bots(wake, tmp_path, clock=clock)

    def spawn_and_close(i):
        d = w.on_message("CTO", bmsg(i, "COO", "CTO", topic=f"t{i}"), ["mentioned"])
        if d["action"] == "spawn":
            w.role("CTO")["session"] = None  # closed; the next one spawns
        return d

    assert [spawn_and_close(i)["action"] for i in range(10, 14)] == ["spawn"] * 3 + ["skip"]
    assert "in the last hour" in w.emitted[-1]["reason"]
    actions = []
    for i in range(14, 34):  # 20 h more: all inside one rolling day
        clock.t += 61 * 60
        actions.append(spawn_and_close(i)["action"])
    assert actions.count("spawn") == 7  # 10 in the rolling day, then none
    assert "today" in w.emitted[-1]["reason"]


def test_global_daily_cap(wake, tmp_path):
    clock = Clock()
    w = make_bots(wake, tmp_path, clock=clock)
    actions = []
    for i in range(30):  # 3 turns an hour, so neither the room nor the role cap
        src, dst = [("COO", "CTO"), ("CTO", "PM"), ("PM", "CMO"), ("CMO", "CPSO"),
                    ("CPSO", "COO")][i % 5]
        w.spawner.finish_all()
        actions.append(w.on_message(dst, bmsg(10 + i, src, dst, topic=f"t{i}"),
                                    ["mentioned"])["action"])
        clock.t += 20 * 60  # each role spawns once, then resumes: no role cap
    assert "skip" not in actions[:25] and actions[25:] == ["skip"] * 5
    skipped = [d for d in w.emitted if d["action"] == "skip"]
    assert len(skipped) == 5 and all("25 bot turns today" in d["reason"] for d in skipped)


def test_counters_survive_a_reload_of_state_json(wake, tmp_path):
    clock, posts = Clock(), []
    spawner = FakeSpawner()
    w = make_bots(wake, tmp_path, clock=clock, spawner=spawner, posts=posts)
    _ping_pong(w, clock, 7)
    assert len(posts) == 1
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(saved["bots"]["log"]) == 6 and saved["bots"]["topic_stopped"]
    w2 = make_bots(wake, tmp_path, clock=clock, spawner=spawner, posts=posts)  # restart
    d = w2.on_message("CPSO", bmsg(30, "COO", "CPSO"), ["mentioned"])
    assert d["action"] == "skip" and "bot loop stopped" in d["reason"]
    assert len(posts) == 1  # still once
    w3 = make_bots(wake, tmp_path / "fresh", clock=clock, spawner=spawner)
    assert w3.on_message("CPSO", bmsg(31, "COO", "CPSO"), ["mentioned"])["action"] != "skip"


def test_bot_message_does_not_move_last_founder_at(wake, tmp_path):
    clock = Clock()
    w = make_bots(wake, tmp_path, clock=clock)
    # a founder session: a bot's message is delivered but keeps his idle clock
    w.on_message("CTO", msg(10, stream="coo", topic="handoff"), ["mentioned"])
    t0 = w.role("CTO")["session"]["last_founder_at"]
    w.spawner.finish_all()
    clock.t += 10 * 60
    d = w.on_message("CTO", bmsg(11, "COO", "CTO"), ["mentioned"])
    assert d["action"] == "resume"
    assert "--max-turns" not in w.spawner.started[-1][0]  # his session keeps its limits
    assert w.role("CTO")["session"]["last_founder_at"] == t0
    w.spawner.finish_all()
    clock.t += 5 * 60
    [d] = w.tick()
    assert d["action"] == "close" and "15 min without the founder" in d["reason"]
    # a bot-opened session has no founder time at all
    w2 = make_bots(wake, tmp_path / "b", clock=clock)
    w2.on_message("CPSO", bmsg(20, "COO", "CPSO"), ["mentioned"])
    assert w2.role("CPSO")["session"]["last_founder_at"] is None


def test_bot_opened_session_closes_10_min_after_its_last_message(wake, tmp_path):
    clock = Clock()
    w = make_bots(wake, tmp_path, clock=clock)
    w.on_message("CPSO", bmsg(10, "COO", "CPSO"), ["mentioned"])
    w.spawner.finish_all()
    clock.t += 6 * 60
    assert w.on_message("CPSO", bmsg(11, "COO", "CPSO"), ["mentioned"])["action"] == "resume"
    w.spawner.finish_all()
    clock.t += 9 * 60 + 59
    assert w.tick() == []
    clock.t += 1
    [d] = w.tick()
    assert d["action"] == "close" and "10 min" in d["reason"]
    argv, prompt, _ = w.spawner.started[-1]
    assert "Idle close" in prompt and argv[-2:] == ["--max-turns", "15"]


def test_founder_joining_a_bot_session_keeps_it_15_min_without_max_turns(wake, tmp_path):
    clock = Clock()
    w = make_bots(wake, tmp_path, clock=clock)
    w.on_message("CPSO", bmsg(10, "COO", "CPSO"), ["mentioned"])
    w.spawner.finish_all()
    d = w.on_message("CPSO", msg(11, "@**CPSO** my view", stream="coo", topic="handoff"),
                     ["mentioned"])
    assert d["action"] == "resume" and "--max-turns" not in w.spawner.started[-1][0]
    assert "not the founder" not in w.spawner.started[-1][1]
    w.spawner.finish_all()
    clock.t += 12 * 60
    assert w.tick() == []  # 12 min: past the bot 10, inside his 15
    clock.t += 3 * 60
    assert w.tick()[0]["action"] == "close"


def test_ping_pong_resumes_the_open_session_never_stacks_a_spawn(wake, tmp_path):
    clock = Clock()
    w = make_bots(wake, tmp_path, clock=clock)
    w.on_message("COO", msg(10, "@**COO** ask the CPSO", stream="coo", topic="handoff"),
                 ["mentioned"])
    d = w.on_message("CPSO", bmsg(11, "COO", "CPSO"), ["mentioned"])
    assert d["action"] == "spawn"
    # CPSO tags the COO back while the COO's turn still runs: queued, no second COO
    d = w.on_message("COO", bmsg(12, "CPSO", "COO", "@**COO** done"), ["mentioned"])
    assert d["action"] == "queue"
    assert [r for _, _, r in w.spawner.started] == ["COO", "CPSO"]
    w.spawner.finish_all()
    [d] = w.tick()  # the COO's queued turn
    assert d["action"] == "resume" and d["role"] == "COO" and d["origin"] == "bot:CPSO"
    argv, prompt, _ = w.spawner.started[-1]
    assert argv[4] == "--resume" and prompt.startswith("From the CPSO bot")


def test_live_hand_session_gets_bot_message_through_its_hook(wake, tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "registry_live", lambda role, alive: 4242)
    w = make_bots(wake, tmp_path)
    d = w.on_message("CTO", bmsg(10, "COO", "CTO"), ["mentioned"])
    assert d["action"] == "skip" and "4242" in d["reason"]
    assert w.spawner.started == [] and not w._bots()["log"]


def test_bot_prompt_reaches_a_real_child_on_stdin_only(wake, tmp_path, monkeypatch):
    got, args = tmp_path / "stdin.txt", tmp_path / "argv.json"
    script = ("import sys,json,pathlib;"
              "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read());"
              "pathlib.Path(sys.argv[2]).write_text(json.dumps(sys.argv),encoding='utf-8')")
    monkeypatch.setitem(wake.AGENT, "spawn", [sys.executable, "-c", script, str(got),
                                              str(args), "{model}", "{session_id}"])
    monkeypatch.setitem(wake.AGENT, "cwd", str(tmp_path))
    sp = wake.Spawner(log_dir=tmp_path / "logs")
    w = make_bots(wake, tmp_path, spawner=sp)
    w.on_message("CTO", bmsg(10, "COO", "CTO", f"@**CTO** {TRICKY}"), ["mentioned"])
    pid = w.role("CTO")["session"]["pid"]
    sp.procs[pid].wait(timeout=30)
    text = got.read_text(encoding="utf-8")
    assert text.startswith("From the COO bot, a teammate — not the founder.") and TRICKY in text
    argv = json.loads(args.read_text(encoding="utf-8"))
    assert argv[-2:] == ["--max-turns", "15"]
    assert not any("rm -rf" in a or "not the founder" in a for a in argv)


def test_dry_run_reads_bot_ids_from_zulip_when_on(wake, tmp_path, monkeypatch, capsys):
    env = _env(tmp_path, monkeypatch, "FOUNDER_ZULIP_ID=8\nZULIP_CTO_EMAIL=cto-bot@x\n"
                                      "ZULIP_CTO_API_KEY=k\nZULIP_COO_EMAIL=coo-bot@x\n"
                                      "ZULIP_COO_API_KEY=k\nBOT_WAKES=on\n")
    monkeypatch.delenv("BOT_WAKES", raising=False)
    monkeypatch.setattr(wake, "STATE_DIR", tmp_path / ".wake")
    monkeypatch.setattr(wake, "DUTY_DIR", tmp_path / "duty")
    monkeypatch.setattr(wake.load_config, "__defaults__", (env,))
    ids = {wake.ZulipClient._basic_auth_header(e, "k"): uid
           for e, uid in (("cto-bot@x", 102), ("coo-bot@x", 101))}
    client = FakeClient(history=[bmsg(10, "COO", "CTO", stream="cto", topic="t")])

    def request(self, method, path, params=None):
        if path == "users/me":
            return {"result": "success", "user_id": ids[self._auth_header]}
        return client._request(method, path, params)
    monkeypatch.setattr(wake.ZulipClient, "_request", request)
    monkeypatch.setattr(wake.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert wake.main(["--dry-run", "--json"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    by_role = {d["role"]: d for d in lines if not d["action"].startswith("duty-")}
    assert by_role["CTO"]["action"] == "spawn" and by_role["CTO"]["origin"] == "bot:COO"
    assert by_role["COO"]["action"] == "skip" and "own role" in by_role["COO"]["reason"]


def test_run_knows_every_bot_before_the_first_backfill(wake, tmp_path, monkeypatch):
    # COO starts first; its backfill holds the CPSO bot's @**COO**. Every id
    # must be known by then, or the wake is skipped as "not the founder".
    history = [bmsg(10, "CPSO", "COO", "@**COO** handoff ready", stream="coo")]
    done = threading.Event()

    class Client(FakeClient):
        def __init__(self, site, email, key, timeout=None):
            super().__init__(history=history, members=alone("COO", "coo"))
            self.email = email

        def own_user(self):
            return {"user_id": BOT_ID[self.email.split("-")[0].upper()]}

        def get_events(self, queue_id, last_event_id, dont_block=False):
            done.wait(5)
            raise OSError("test over")

    monkeypatch.setattr(wake, "ZulipClient", Client)
    monkeypatch.setattr(wake, "STATE_DIR", tmp_path / ".wake")
    monkeypatch.setattr(wake, "TICK_SECONDS", 0.01)
    w = make_bots(wake, tmp_path)
    w.bot_ids = {}
    w.role("COO")["last_message_id"] = 9

    def stop():
        raise KeyboardInterrupt
    w.tick = stop
    cfg = {"site": "http://z", "missing": [],
           "bots": {r: (f"{r.lower()}-bot@x", "k") for r in ("COO", "CPSO", "CTO")}}
    try:
        assert wake.run(cfg, w) == 0
    finally:
        done.set()
    d = next(e for e in w.emitted if e.get("role") == "COO")
    assert d["action"] == "spawn" and d["origin"] == "bot:CPSO"
    assert w.bot_ids == {101: "COO", 105: "CPSO", 102: "CTO"}


# -- ops#322 x ops#321: the merged rules ----------------------------------------

def _design(role, room):
    """#design: the founder, the COO bot and the PM bot; no CTO."""
    return [user(FOUNDER, "Founder", bot=False), user(101, "COO"), user(103, "PM")]


def test_a_bot_mention_of_a_role_outside_the_room_wakes_nothing(wake, tmp_path):
    w = make_bots(wake, tmp_path)
    w.members = _design
    d = w.on_message("CTO", bmsg(10, "COO", "CTO", stream="design"), ["mentioned"])
    assert d["action"] == "skip" and "CTO is not a member of #design" in d["reason"]
    assert w.spawner.started == []
    assert w._bots()["log"] == []  # refused before any cap counted it
    # the PM is in the room: the same bot's @**PM** there does wake it
    d = w.on_message("PM", bmsg(11, "COO", "PM", stream="design"), ["mentioned"])
    assert d["action"] == "spawn" and d["origin"] == "bot:COO"


def test_two_topics_in_one_room_count_against_one_cap(wake, tmp_path):
    clock = Clock()
    w = make_bots(wake, tmp_path, clock=clock)
    ds = []
    for i in range(7):  # the COO bot varies --topic inside #cto
        w.spawner.finish_all()
        ds.append(w.on_message("CTO", bmsg(10 + i, "COO", "CTO", stream="cto",
                                           topic=f"topic {i % 2}"), ["mentioned"]))
        clock.t += 60
    assert [d["action"] for d in ds[:6]].count("skip") == 0
    assert ds[6]["action"] == "skip" and "bot loop stopped in this room" in ds[6]["reason"]
    # a founder message in either topic resets the room
    w.on_message("CTO", msg(30, "@**CTO** go on", stream="cto", topic="topic 1"), ["mentioned"])
    w.spawner.finish_all()
    d = w.on_message("CTO", bmsg(31, "COO", "CTO", stream="cto", topic="topic 0"),
                     ["mentioned"])
    assert d["action"] in ("resume", "queue")


def test_the_founder_is_routed_by_the_room_rule_only(wake, tmp_path):
    # #coo holds the COO and the PM: an untagged line there goes to the COO,
    # the room's owner, and never to the PM (the old (stream, topic) rule is gone)
    def coo_room(role, room):
        return [user(FOUNDER, "Founder", bot=False), user(101, "COO"), user(103, "PM")]
    clock = Clock()
    w = make(wake, tmp_path, clock=clock, members=coo_room)
    assert w.on_message("COO", msg(10, "@**COO** plan", stream="coo"),
                        ["mentioned"])["action"] == "spawn"
    w.spawner.finish_all()
    clock.t += 60
    d = w.on_message("COO", msg(11, "and one more thing", stream="coo"), [])
    assert d["action"] == "resume"
    assert len(w.spawner.started) == 2 and "one more thing" in w.spawner.started[-1][1]
    assert w.role("COO")["session"]["last_founder_at"] == clock.t
    assert w.on_message("PM", msg(11, "and one more thing", stream="coo"),
                        [])["action"] == "skip"
    w.spawner.finish_all()
    # a mention elsewhere is not "in the topic": the room is the key
    assert w.on_message("COO", msg(12, "@**COO** now?", stream="coo", topic="other"),
                        ["mentioned"])["action"] == "resume"


def test_a_bot_opened_session_gets_the_catch_up_too(wake, tmp_path):
    rows = []
    w = make_bots(wake, tmp_path)
    w.members = crowd
    w.history = _room(rows)
    first = bmsg(10, "COO", "CTO", "@**CTO** check the schema", stream="All hands")
    rows.append(first)
    assert w.on_message("CTO", first, ["mentioned"])["action"] == "spawn"
    assert w.role("CTO")["session"]["seen_id"] == 10
    w.spawner.finish_all()
    rows.append(bmsg(11, "PM", "COO", "PM: the spec is in docs/spec.md", stream="All hands"))
    rows.append(bmsg(12, "CTO", "COO", "CTO: looking", stream="All hands"))  # its own post
    nxt = bmsg(13, "COO", "CTO", "@**CTO** and the index?", stream="All hands")
    rows.append(nxt)
    d = w.on_message("CTO", nxt, ["mentioned"])
    assert d["action"] == "resume" and d["origin"] == "bot:COO"
    prompt = w.spawner.started[-1][1]
    # one marker for each job: BOT_MARKER heads the prompt, NOT_FOUNDER marks lines
    assert prompt.count(wake.BOT_MARKER.format(src="COO")) == 1
    assert f"- PM {wake.NOT_FOUNDER}: PM: the spec is in docs/spec.md" in prompt
    assert "CTO: looking" not in prompt
    assert prompt.index("docs/spec.md") < prompt.index("Message from COO")
    assert w.role("CTO")["session"]["seen_id"] == 13
    # the queued resume of a bot-opened session carries it as well
    rows.append(bmsg(14, "PM", "COO", "PM: index is fine", stream="All hands"))
    q = bmsg(15, "COO", "CTO", "@**CTO** ok?", stream="All hands")
    rows.append(q)
    assert w.on_message("CTO", q, ["mentioned"])["action"] == "queue"
    w.spawner.finish_all()
    w.tick()
    prompt = w.spawner.started[-1][1]
    assert f"- PM {wake.NOT_FOUNDER}: PM: index is fine" in prompt
    assert w.role("CTO")["session"]["seen_id"] == 15


def test_the_pa_is_never_woken_outside_pa_by_anyone(wake, tmp_path):
    # the founder rule (2026-10-17): the PA wakes only in #pa or a founder DM
    w = make_bots(wake, tmp_path)
    w.members = crowd
    for i, m in enumerate([msg(10, "@**PA** hi", stream="All hands"),
                           msg(11, "hi", stream="errands"),
                           bmsg(12, "COO", "PA", stream="All hands"),
                           bmsg(13, "COO", "PA", stream="pa")]):
        assert w.on_message("PA", m, ["mentioned"])["action"] == "skip", i
    assert w.spawner.started == []


def test_first_prompt_tells_a_role_to_use_its_skills_but_not_the_pa(wake):
    for role in ("CMO", "PM", "CTO", "CPSO", "COO"):
        p = wake.message_prompt(role, msg(10, stream=role.lower(), topic="t"), first=True)
        assert wake.SKILL_LINE in p and "invoke the matching skill" in p
        assert p.index(f"You are the {role}") < p.index(wake.SKILL_LINE)
    pa = wake.message_prompt("PA", msg(10, stream="pa", topic="t"), first=True)
    assert "matching skill" not in pa
    resume = wake.message_prompt("CTO", msg(10, stream="cto", topic="t"), first=False)
    assert "matching skill" not in resume
