"""bot/wake.py (ops#293): a founder @mention opens a fresh role session.

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


def make(wake, tmp_path, clock=None, spawner=None, dry_run=False):
    emitted = []
    w = wake.Waker(FOUNDER, tmp_path / "state.json", spawner or FakeSpawner(),
                   clock=clock or Clock(), dry_run=dry_run, emit=emitted.append)
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
    assert TRICKY in prompt and "#cto › 141 record shape" in prompt
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


def test_founder_without_mention_in_a_stream_is_skipped(wake, tmp_path):
    w = make(wake, tmp_path)
    assert w.on_message("CTO", msg(10), [])["action"] == "skip"
    assert w.spawner.started == []


def test_founder_dm_spawns_and_is_answered_in_the_role_stream(wake, tmp_path):
    w = make(wake, tmp_path)
    d = w.on_message("PM", msg(10, stream=None), [])
    assert d["action"] == "spawn" and d["model"] == "sonnet"
    prompt = w.spawner.started[0][1]
    assert "#pm › direct message" in prompt


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
    assert "Idle close" in prompt and "#cto › 141 record shape" in prompt
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

    def __init__(self, history=(), queue_alive=False, events=(), max_message_id=0):
        self.history = list(history)
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
        assert (method, path) == ("GET", "messages")
        operand = params["narrow"][0]["operand"]
        rows = [m for m in self.history
                if (operand == "dm") == (m.get("type") == "private")]
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
    monkeypatch.setattr(wake.load_config, "__defaults__", (env,))
    client = FakeClient(history=[msg(10), msg(11, sender=99)])
    monkeypatch.setattr(wake.ZulipClient, "_request",
                        lambda self, m, p, params=None: client._request(m, p, params))
    monkeypatch.setattr(wake.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert wake.main(["--dry-run", "--json"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(d["role"], d["action"]) for d in lines] == [("CTO", "spawn"), ("CTO", "skip")]
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
    assert st["roles"]["CTO"]["topic"] == "#cto › 141 record shape"
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
    def fetch(role, stream, topic, n):
        if calls is not None:
            calls.append((role, stream, topic, n))
        return rows[-n:]
    return fetch


def test_fresh_session_gets_the_topic_last_20_messages_before_the_new_one(wake, tmp_path):
    rows = [msg(i, f"old {i}", sender=FOUNDER if i % 2 else 99) for i in range(1, 30)]
    rows.append(msg(30, "CTO closing: agreed X, written in ops#300", sender=99))
    rows.append(msg(31, "next question"))
    calls = []
    w = make(wake, tmp_path)
    w.history = _history(rows, calls)
    d = w.on_message("CTO", rows[-1], ["mentioned"])
    assert d["action"] == "spawn" and d["history"] == 20
    assert calls == [("CTO", "cto", "141 record shape", 21)]
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


def test_topic_history_asks_zulip_for_that_topic(wake):
    seen = {}

    class C:
        def _request(self, method, path, params=None):
            seen.update(params)
            return {"messages": [{"id": 3}, {"id": 1}]}
    assert [m["id"] for m in wake.topic_history(C(), "cto", "t", 21)] == [1, 3]
    assert seen["narrow"] == [{"operator": "stream", "operand": "cto"},
                              {"operator": "topic", "operand": "t"}]
    assert seen["num_before"] == 21 and seen["anchor"] == "newest"


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


def test_founder_follow_up_in_the_topic_extends_the_idle_timer(wake, tmp_path):
    # outside the role's own stream (#coo for the CTO), an untagged follow-up
    # only keeps the session open; in #cto it is delivered (next test)
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10, stream="coo"), ["mentioned"])
    w.spawner.finish_all()
    clock.t += 10 * 60
    d = w.on_message("CTO", msg(11, "hm, thinking", stream="coo"), [])
    assert d["action"] == "skip" and "extends" in d["reason"]
    clock.t += 10 * 60
    assert w.tick() == []  # 20 min since the mention, 10 since the follow-up
    clock.t += 5 * 60
    assert w.tick()[0]["action"] == "close"
    assert len(w.spawner.started) == 2  # the spawn and the close turn; nothing delivered
    # another topic, or another sender, does not count
    w2 = make(wake, tmp_path / "b", clock=clock)
    w2.on_message("CTO", msg(20), ["mentioned"])
    t0 = w2.role("CTO")["session"]["last_founder_at"]
    clock.t += 60
    w2.on_message("CTO", msg(21, topic="other"), [])
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


def test_pa_mention_elsewhere_skips_and_others_in_pa_never_do(wake, tmp_path):
    w = make(wake, tmp_path)
    d = w.on_message("PA", msg(10, "@**PA** hi", stream="coo"), ["mentioned"])
    assert d["action"] == "skip" and d["reason"] == "PA wakes only in #pa or a founder DM (ops#327)"
    assert w.spawner.started == []
    w2 = make(wake, tmp_path / "b")
    assert w2.on_message("PA", msg(11, stream="pa", sender=99), [])["action"] == "skip"
    assert w2.on_message("PA", msg(12, stream="coo"), [])["action"] == "skip"
    assert w2.spawner.started == []
    # the home-stream rule is the PA's only: a CTO bot in #pa still needs a mention
    assert w2.on_message("CTO", msg(13, stream="pa"), [])["action"] == "skip"


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
    assert (sess["stream"], sess["topic"]) == ("pa", "errands") and not sess.get("pending")


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


def test_pa_backfill_reads_its_home_stream(wake):
    client = FakeClient()
    narrows = []
    orig = client._request

    def spy(method, path, params=None):
        narrows.append(params["narrow"][0])
        if params["narrow"][0]["operator"] == "stream":
            return {"messages": [msg(5, stream="pa", topic="errands")]}
        return orig(method, path, params)
    client._request = spy
    pairs = wake.backfill(client, 1, home="pa")
    assert {"operator": "stream", "operand": "pa"} in narrows
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


def test_untagged_never_opens_a_session_nor_reaches_another_topic(wake, tmp_path):
    w = make(wake, tmp_path)
    assert w.on_message("CTO", msg(10), [])["action"] == "skip"  # no session: needs a mention
    assert w.spawner.started == []
    w.on_message("CTO", msg(11), ["mentioned"])
    w.spawner.finish_all()
    assert w.on_message("CTO", msg(12, topic="other"), [])["action"] == "skip"
    assert w.on_message("CTO", msg(13, sender=99), [])["action"] == "skip"
    assert w.on_message("PM", msg(14, stream="pm"), [])["action"] == "skip"  # PM has no session
    assert len(w.spawner.started) == 1


def test_backfill_reads_the_role_own_stream(wake, tmp_path, monkeypatch):
    # a restart must not miss an untagged follow-up in #cto
    assert wake.own_stream("CTO") == "cto" and wake.own_stream("PA") == "pa"
    homes = []
    monkeypatch.setattr(wake, "backfill", lambda client, after, home=None: homes.append(home) or [])
    cfg = {"site": "http://x", "bots": {"CTO": ("c@x", "k"), "PA": ("p@x", "k")}, "skipped": {}}
    wake.dry_run(cfg, make(wake, tmp_path, dry_run=True))
    assert homes == ["cto", "pa"]


HEBREW = "כללי"


def test_hebrew_topic_stays_plain_utf8_in_state_and_prompt(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CMO", msg(10, "שלום", stream="cmo", topic=HEBREW), ["mentioned"])
    raw = (tmp_path / "state.json").read_bytes()
    assert HEBREW.encode("utf-8") in raw and b"\\u05db" not in raw
    assert wake.load_state(tmp_path / "state.json")["roles"]["CMO"]["session"]["topic"] == HEBREW
    _, prompt, _ = w.spawner.started[0]
    assert f'--stream "cmo" --topic "{HEBREW}"' in prompt
    assert "\\u05" not in prompt and "\ufffd" not in prompt
    assert f"#cmo › {HEBREW}" in prompt
    close = wake.close_prompt("CMO", "cmo", HEBREW)
    assert f'--topic "{HEBREW}"' in close and "\\u05" not in close
    # the prompt reaches the child as whole UTF-8 characters
    assert prompt.encode("utf-8").decode("utf-8") == prompt


def test_hebrew_topic_reaches_a_real_child_on_stdin_intact(wake, tmp_path, monkeypatch):
    out = tmp_path / "seen.txt"
    script = ("import sys,pathlib; pathlib.Path(sys.argv[1]).write_bytes("
              "sys.stdin.buffer.read())")
    monkeypatch.setattr(wake, "resolve_program", lambda argv: argv)
    sp = wake.Spawner(log_dir=tmp_path)
    prompt = wake.message_prompt("CMO", msg(10, stream="cmo", topic=HEBREW), first=True)
    pid = sp.start([sys.executable, "-c", script, str(out)], prompt, "CMO", str(tmp_path))
    sp.procs[pid].wait(timeout=30)
    assert out.read_bytes().decode("utf-8") == prompt
    assert f'--topic "{HEBREW}"' in out.read_text(encoding="utf-8")


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
    # an untagged follow-up to a stale session is not resumed either
    w2 = make(wake, tmp_path / "b")
    w2.on_message("CMO", msg(20, stream="cmo"), ["mentioned"])
    w2.spawner.finish_all()
    w2.spawner.missing.add(w2.spawner.started[0][0][5])
    assert w2.on_message("CMO", msg(21, stream="cmo"), [])["action"] == "skip"
    assert len(w2.spawner.started) == 1 and w2.role("CMO")["session"] is None


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
    w.post = lambda role, stream, topic, text: (posts if posts is not None else []).append(
        (role, stream, topic, text))
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
    assert posts == [("CPSO", "coo", "handoff", line)]
    assert [d.get("stop_line") for d in ds[6:]] == [True, False, False]
    assert len(w.spawner.started) == 6
    # another topic is not stopped
    d = w.on_message("CTO", bmsg(50, "COO", "CTO", topic="other"), ["mentioned"])
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
    for i in range(30):  # one turn per topic, so neither the topic nor the role cap
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
    by_role = {d["role"]: d for d in lines}
    assert by_role["CTO"]["action"] == "spawn" and by_role["CTO"]["origin"] == "bot:COO"
    assert by_role["COO"]["action"] == "skip" and "own role" in by_role["COO"]["reason"]


def test_run_knows_every_bot_before_the_first_backfill(wake, tmp_path, monkeypatch):
    # COO starts first; its backfill holds the CPSO bot's @**COO**. Every id
    # must be known by then, or the wake is skipped as "not the founder".
    history = [bmsg(10, "CPSO", "COO", "@**COO** handoff ready", stream="coo")]
    done = threading.Event()

    class Client(FakeClient):
        def __init__(self, site, email, key, timeout=None):
            super().__init__(history=history)
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
