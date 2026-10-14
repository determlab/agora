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
        self.running: set[int] = set()
        self.stopped: list[int] = []
        self._pid = 1000

    def start(self, argv, prompt, role):
        self._pid += 1
        self.started.append((argv, prompt, role))
        self.running.add(self._pid)
        return self._pid

    def alive(self, pid):
        return pid in self.running

    def stop(self, pid):
        self.stopped.append(pid)
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
    [d] = w.tick()
    assert d["action"] == "close" and "4 h" in d["reason"]
    assert w.spawner.stopped == [running]


def test_message_during_close_opens_a_fresh_session_after_it(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.spawner.finish_all()
    clock.t += 15 * 60
    w.tick()
    assert w.on_message("CTO", msg(11, "wait!"), ["mentioned"])["action"] == "queue"
    w.spawner.finish_all()
    [d] = w.tick()
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
