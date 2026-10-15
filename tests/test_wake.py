"""bot/wake.py (ops#293): a founder @mention opens a fresh role session.

Zulip, the clock and the agent process are all fakes here: no network, no
``claude``. One test starts a real child process to prove the prompt reaches
it on stdin and never on its command line.
"""
from __future__ import annotations

import importlib.util
import json
import sys
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
              "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read(),encoding='utf-8');"
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
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.spawner.finish_all()
    clock.t += 10 * 60
    d = w.on_message("CTO", msg(11, "hm, thinking"), [])
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


def test_pa_mention_elsewhere_spawns_and_others_in_pa_never_do(wake, tmp_path):
    w = make(wake, tmp_path)
    assert w.on_message("PA", msg(10, stream="coo"), ["mentioned"])["action"] == "spawn"
    w2 = make(wake, tmp_path / "b")
    assert w2.on_message("PA", msg(11, stream="pa", sender=99), [])["action"] == "skip"
    assert w2.on_message("PA", msg(12, stream="coo"), [])["action"] == "skip"
    assert w2.spawner.started == []
    # the home-stream rule is the PA's only: a CTO bot in #pa still needs a mention
    assert w2.on_message("CTO", msg(13, stream="pa"), [])["action"] == "skip"


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
