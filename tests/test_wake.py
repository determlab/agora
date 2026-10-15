"""bot/wake.py (ops#293): a founder @mention opens a fresh role session.

Zulip, the clock and the agent process are all fakes here: no network, no
``claude``. One test starts a real child process to prove a message reaches
it on stdin, as one stream-json line, and never on its command line.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
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
    """Records starts and every stdin write; a test ends processes by hand."""

    def __init__(self):
        self.started: list[tuple[list[str], str, str]] = []
        self.sent: list[tuple[int, str]] = []
        self.ended_input: list[int] = []
        self.running: set[int] = set()
        self.busy_pids: set[int] = set()
        self.stopped: list[int] = []
        self._pid = 1000

    def start(self, argv, cwd, role):
        self._pid += 1
        self.started.append((argv, cwd, role))
        self.running.add(self._pid)
        return self._pid

    def send(self, pid, text):
        if pid not in self.running or pid in self.ended_input:
            return False
        self.sent.append((pid, text))
        return True

    def end_input(self, pid):
        self.ended_input.append(pid)

    def alive(self, pid):
        return pid in self.running

    def busy(self, pid):
        return pid in self.busy_pids

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


CWD = {"COO": "/ops", "CTO": "/ops", "PM": "/ops", "CMO": "/ops", "CPSO": "/ops",
       "PA": "/pa"}


def make(wake, tmp_path, clock=None, spawner=None, dry_run=False, cwd=None, history=None):
    emitted = []
    w = wake.Waker(FOUNDER, tmp_path / "state.json", spawner or FakeSpawner(),
                   clock=clock or Clock(), dry_run=dry_run, emit=emitted.append,
                   cwd=cwd or CWD, history=history)
    w.emitted = emitted
    return w


# -- who triggers, and the one live process ----------------------------------

def test_founder_mention_spawns_once_with_text_on_stdin_only(wake, tmp_path):
    w = make(wake, tmp_path)
    d = w.on_message("CTO", msg(10, TRICKY), ["mentioned"])
    assert d["action"] == "spawn"
    (argv, cwd, role), = w.spawner.started
    assert (cwd, role) == ("/ops", "CTO")
    assert argv[:4] == ["claude", "-p", "--model", "opus"]
    assert "--input-format" in argv and "stream-json" in argv
    assert not any(TRICKY in a or "rm -rf" in a for a in argv)
    assert not any(a in ("--resume", "--continue", "--dangerously-skip-permissions")
                   for a in argv)
    [(_, prompt)] = w.spawner.sent
    assert TRICKY in prompt and "#cto › 141 record shape" in prompt


def test_second_message_while_live_goes_to_the_same_process(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CTO", msg(10), ["mentioned"])
    d = w.on_message("CTO", msg(11, "and the index?"), ["mentioned"])
    assert d["action"] == "deliver"
    assert len(w.spawner.started) == 1
    pid, text = w.spawner.sent[-1]
    assert pid == w.role("CTO")["session"]["pid"] and "and the index?" in text


def test_founder_follow_up_in_the_topic_needs_no_mention_and_keeps_it_open(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    clock.t += 10 * 60
    d = w.on_message("CTO", msg(11, "one more thing"), [])
    assert d["action"] == "deliver" and "same topic" in d["reason"]
    clock.t += 10 * 60  # 20 min after the spawn, 10 after the follow-up
    assert w.tick() == []
    assert w.on_message("CTO", msg(12, stream="cto", topic="other"), [])["action"] == "skip"


@pytest.mark.parametrize("sender", [99, 41])
def test_bot_or_non_founder_never_spawns(wake, tmp_path, sender):
    w = make(wake, tmp_path)
    d = w.on_message("CTO", msg(10, sender=sender), ["mentioned"])
    assert d["action"] == "skip" and "not the founder" in d["reason"]
    assert w.on_message("CTO", msg(11, sender=sender, stream=None), [])["action"] == "skip"
    assert w.spawner.started == []


def test_non_founder_in_a_live_topic_is_not_delivered(wake, tmp_path):
    w = make(wake, tmp_path)
    w.on_message("CTO", msg(10), ["mentioned"])
    assert w.on_message("CTO", msg(11, sender=41), [])["action"] == "skip"
    assert len(w.spawner.sent) == 1


def test_sender_id_is_compared_not_the_name(wake, tmp_path):
    w = make(wake, tmp_path)
    impostor = msg(10, sender=77, sender_full_name="Founder", sender_email="u8@x")
    assert w.on_message("CTO", impostor, ["mentioned"])["action"] == "skip"
    assert w.spawner.started == []


def test_founder_without_mention_and_no_session_is_skipped(wake, tmp_path):
    w = make(wake, tmp_path)
    assert w.on_message("CTO", msg(10), [])["action"] == "skip"
    assert w.spawner.started == []


def test_founder_dm_spawns_and_is_answered_in_the_role_stream(wake, tmp_path):
    w = make(wake, tmp_path)
    d = w.on_message("PM", msg(10, stream=None), [])
    assert d["action"] == "spawn" and d["model"] == "sonnet"
    assert "#pm › direct message" in w.spawner.sent[0][1]


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


# -- a fresh session starts from the topic, never --resume (ops#293 4b) --------

def test_fresh_session_gets_the_topics_last_20_messages(wake, tmp_path):
    asked = []

    def history(role, m):
        asked.append((role, m["id"]))
        return [msg(i, f"older {i}") for i in range(1, 26)] + [m]

    w = make(wake, tmp_path, history=history)
    d = w.on_message("CTO", msg(30, "pick up where we left"), ["mentioned"])
    assert asked == [("CTO", 30)] and d["history"] == 20
    prompt = w.spawner.sent[0][1]
    assert "older 6" in prompt and "older 25" in prompt and "older 5:" not in prompt
    assert prompt.index("older 25") < prompt.index("pick up where we left")
    assert prompt.count("pick up where we left") == 1


# -- idle close, fake clock ----------------------------------------------------

def test_idle_close_at_15_minutes_then_a_fresh_session_with_the_close_line(wake, tmp_path):
    clock = Clock()
    close_line = msg(20, "Agreed: shape v2, written in rfc-007.", sender=99)
    w = make(wake, tmp_path, clock=clock, history=lambda r, m: [close_line])
    w.on_message("CTO", msg(10), ["mentioned"])
    pid = w.role("CTO")["session"]["pid"]
    clock.t += 14 * 60 + 59
    assert w.tick() == []
    clock.t += 1
    [d] = w.tick()
    assert d["action"] == "close" and "15 min" in d["reason"]
    assert w.spawner.sent[-1][0] == pid and "Idle close" in w.spawner.sent[-1][1]
    assert "#cto › 141 record shape" in w.spawner.sent[-1][1]
    assert "--as CTO send" in w.spawner.sent[-1][1]
    assert w.spawner.ended_input == [pid]
    w.spawner.finish_all()
    assert w.tick()[0]["action"] == "closed"
    d = w.on_message("CTO", msg(21, "new question"), ["mentioned"])
    assert d["action"] == "spawn" and len(w.spawner.started) == 2
    assert "rfc-007" in w.spawner.sent[-1][1]


def test_idle_close_waits_for_a_running_turn(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    w.spawner.busy_pids.add(w.role("CTO")["session"]["pid"])
    clock.t += 20 * 60
    assert w.tick() == []
    w.spawner.busy_pids.clear()
    assert w.tick()[0]["action"] == "close"


def test_close_at_4_hours_even_while_talking(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    pid = w.role("CTO")["session"]["pid"]
    w.spawner.busy_pids.add(pid)
    for i in range(1, 24):  # a message every 10 minutes: never idle
        clock.t += 10 * 60
        assert w.on_message("CTO", msg(10 + i), ["mentioned"])["action"] == "deliver"
        assert w.tick() == []
    clock.t += 10 * 60
    [d] = w.tick()
    assert d["action"] == "close" and "4 h" in d["reason"]
    assert w.spawner.ended_input == [pid] and w.spawner.stopped == []


def test_a_close_that_hangs_is_stopped_after_2_minutes(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    pid = w.role("CTO")["session"]["pid"]
    clock.t += 15 * 60
    w.tick()
    clock.t += 119
    assert w.tick() == []
    clock.t += 1
    [d] = w.tick()
    assert d["action"] == "stopped" and w.spawner.stopped == [pid]
    assert w.role("CTO")["session"] is None


def test_message_during_close_opens_a_fresh_session_after_it(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    clock.t += 15 * 60
    w.tick()
    assert w.on_message("CTO", msg(11, "wait!"), ["mentioned"])["action"] == "queue"
    w.spawner.finish_all()
    out = w.tick()
    assert [d["action"] for d in out] == ["closed", "spawn"]
    assert "wait!" in w.spawner.sent[-1][1] and len(w.spawner.started) == 2


# -- restart: a pid from an earlier run is never trusted ------------------------

def test_after_restart_an_old_session_is_never_written_to_or_stopped(wake, tmp_path):
    clock = Clock()
    w = make(wake, tmp_path, clock=clock)
    w.on_message("CTO", msg(10), ["mentioned"])
    old_pid = w.role("CTO")["session"]["pid"]

    fresh = FakeSpawner()
    fresh.running.add(old_pid)  # the pid was reused by an unrelated process...
    fresh.alive = lambda pid: False  # ...which this wake.py did not start
    w2 = make(wake, tmp_path, clock=clock, spawner=fresh)
    clock.t += 5 * 60 * 60
    [d] = w2.tick()
    assert d["action"] == "ended"
    assert fresh.stopped == [] and fresh.sent == []
    assert w2.on_message("CTO", msg(11), ["mentioned"])["action"] == "spawn"


def test_a_spawn_that_fails_says_so_and_leaves_no_session(wake, tmp_path):
    class Broken(FakeSpawner):
        def start(self, argv, cwd, role):
            raise FileNotFoundError("claude is not on PATH")
    w = make(wake, tmp_path, spawner=Broken())
    d = w.on_message("CTO", msg(10), ["mentioned"])
    assert d["action"] == "error" and "not on PATH" in d["reason"]
    assert w.role("CTO")["session"] is None


def test_the_real_spawner_never_trusts_a_pid_it_did_not_start(wake, tmp_path):
    import os
    sp = wake.Spawner(log_dir=tmp_path)
    assert sp.alive(os.getpid()) is False
    assert sp.send(os.getpid(), "x") is False
    sp.stop(os.getpid())  # a no-op, or this test would not finish


# -- resume of the Zulip queue after a restart ----------------------------------

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
        narrow = params["narrow"]
        op = narrow[0]
        if op["operator"] == "is":
            rows = [m for m in self.history
                    if (op["operand"] == "dm") == (m.get("type") == "private")]
        else:  # channel (+ topic): the history of one topic
            rows = [m for m in self.history if m.get("display_recipient") == op["operand"]
                    and all(m.get("subject") == t["operand"] for t in narrow[1:])]
        if params["anchor"] == "newest":
            rows = rows[-params["num_before"]:]
        elif params.get("num_after"):
            rows = [m for m in rows if m["id"] > params["anchor"]]
        else:
            rows = [m for m in rows if m["id"] < params["anchor"]][-params["num_before"]:]
        return {"result": "success", "messages": rows}


def test_first_start_acts_on_nothing_old(wake, tmp_path):
    w = make(wake, tmp_path)
    client = FakeClient(history=[msg(5)], max_message_id=5)
    pairs, events = wake.open_queue(client, w.role("CTO"))
    assert pairs == [] and events == []
    assert w.role("CTO")["last_message_id"] == 5


def test_restart_with_expired_queue_neither_respawns_nor_skips(wake, tmp_path):
    w = make(wake, tmp_path)
    w.role("CTO").update(queue_id="q-old", last_event_id=3, last_message_id=9)
    w.on_message("CTO", msg(10), ["mentioned"])
    assert len(w.spawner.started) == 1

    w2 = make(wake, tmp_path)  # same state file, a new process: nothing of ours runs
    assert w2.role("CTO")["last_message_id"] == 10
    client = FakeClient(history=[msg(10), msg(11, "while you were down")])
    pairs, _ = wake.open_queue(client, w2.role("CTO"))
    assert client.registered == 1 and [m["id"] for m, _ in pairs] == [11]
    pairs = [(msg(10), ["mentioned"])] + pairs  # and a replay of the handled one
    actions = [w2.on_message("CTO", m, f)["action"] for m, f in pairs]
    assert actions == ["skip", "spawn"]
    assert "while you were down" in w2.spawner.sent[-1][1]


def test_restart_with_live_queue_resumes_from_saved_event_id(wake, tmp_path):
    w = make(wake, tmp_path)
    w.role("CTO").update(queue_id="q1", last_event_id=3, last_message_id=10)
    w.save()
    w2 = make(wake, tmp_path)
    client = FakeClient(queue_alive=True, events=[
        {"id": 3, "type": "message", "message": msg(10), "flags": ["mentioned"]},
        {"id": 4, "type": "message", "message": msg(11), "flags": ["mentioned"]},
        {"id": 5, "type": "heartbeat"}])
    _, events = wake.open_queue(client, w2.role("CTO"))
    assert client.registered == 0 and [e["id"] for e in events] == [4, 5]
    wake.process_batch(w2, "CTO", events)
    assert [d["action"] for d in w2.emitted] == ["spawn"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["roles"]["CTO"]["last_event_id"] == 5
    assert saved["roles"]["CTO"]["last_message_id"] == 11


def test_history_fetcher_reads_the_topic_before_the_message(wake):
    rows = [msg(i, f"m{i}") for i in range(1, 30)] + [msg(40, topic="elsewhere")]
    fetch = wake.history_fetcher({"CTO": FakeClient(history=rows)}, FOUNDER)
    got = fetch("CTO", msg(25))
    assert [m["id"] for m in got] == list(range(5, 25))


# -- PA (ops#293 7) -------------------------------------------------------------

def test_pa_is_called_by_any_founder_message_in_pa_and_works_in_pa_dir(wake, tmp_path):
    w = make(wake, tmp_path)
    d = w.on_message("PA", msg(10, "book the dentist", stream="pa", topic="errands"), [])
    assert d["action"] == "spawn" and d["model"] == "sonnet" and d["cwd"] == "/pa"
    assert w.spawner.started[0][1] == "/pa"
    assert "PA" in w.spawner.sent[0][1] and "CLAUDE.md" in w.spawner.sent[0][1]


def test_pa_without_pa_dir_skips_with_the_fix(wake, tmp_path):
    w = make(wake, tmp_path, cwd={**CWD, "PA": None})
    d = w.on_message("PA", msg(10, stream="pa", topic="errands"), [])
    assert d["action"] == "skip" and "PA_DIR missing in bot/.env" in d["reason"]
    assert w.spawner.started == []


def test_load_config_reads_pa_dir(wake, tmp_path, monkeypatch):
    for k in ("FOUNDER_ZULIP_ID", "PA_DIR"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("FOUNDER_ZULIP_ID=8\nZULIP_PA_EMAIL=pa-bot@x\nZULIP_PA_API_KEY=k\n"
                   "PA_DIR=C:\\PlayGround\\pa\n")
    cfg = wake.load_config(env)
    assert cfg["cwd"]["PA"] == "C:\\PlayGround\\pa" and "PA" in cfg["bots"]
    env.write_text("FOUNDER_ZULIP_ID=8\nZULIP_PA_EMAIL=pa-bot@x\nZULIP_PA_API_KEY=k\n")
    assert wake.load_config(env)["cwd"]["PA"] is None


# -- config, command, dry run, the real process ----------------------------------

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


def test_command_takes_only_the_model(wake):
    assert wake.build_command("CPSO")[:4] == ["claude", "-p", "--model", "sonnet"]
    with pytest.raises(KeyError):
        wake.build_command("INTERN")


def test_dry_run_json_prints_one_line_per_message_and_spawns_nothing(wake, tmp_path,
                                                                    monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("FOUNDER_ZULIP_ID=8\nZULIP_CTO_EMAIL=cto-bot@x\nZULIP_CTO_API_KEY=k\n")
    for role in wake.ROLES:
        monkeypatch.delenv(f"ZULIP_{role}_EMAIL", raising=False)
    monkeypatch.delenv("FOUNDER_ZULIP_ID", raising=False)
    monkeypatch.setattr(wake, "STATE_DIR", tmp_path / ".wake")
    monkeypatch.setattr(wake.load_config, "__defaults__", (env,))
    client = FakeClient(history=[msg(10), msg(11, sender=99), msg(12, "and?")])
    monkeypatch.setattr(wake.ZulipClient, "_request",
                        lambda self, m, p, params=None: client._request(m, p, params))
    monkeypatch.setattr(wake.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert wake.main(["--dry-run", "--json"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(d["role"], d["action"]) for d in lines] == [
        ("CTO", "spawn"), ("CTO", "skip"), ("CTO", "deliver")]
    assert not (tmp_path / ".wake" / "state.json").exists()


def test_real_spawner_streams_messages_on_stdin(wake, tmp_path, monkeypatch):
    got, args = tmp_path / "stdin.txt", tmp_path / "argv.json"
    script = ("import sys,json,pathlib\n"
              "pathlib.Path(sys.argv[2]).write_text(json.dumps(sys.argv),encoding='utf-8')\n"
              "lines=[]\n"
              "for line in sys.stdin:\n"
              "    lines.append(line)\n"
              "    print(json.dumps({'type':'result'}),flush=True)\n"
              "pathlib.Path(sys.argv[1]).write_text(''.join(lines),encoding='utf-8')\n")
    sp = wake.Spawner(log_dir=tmp_path / "logs")
    pid = sp.start([sys.executable, "-c", script, str(got), str(args)], str(tmp_path), "CTO")
    assert sp.send(pid, TRICKY) and sp.send(pid, "second")
    deadline = time.time() + 30
    while sp.busy(pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not sp.busy(pid) and sp.alive(pid)  # both turns done, still listening
    sp.end_input(pid)
    sp.procs[pid].wait(timeout=30)
    assert not sp.alive(pid)
    lines = [json.loads(x) for x in got.read_text(encoding="utf-8").splitlines()]
    assert [x["message"]["content"][0]["text"] for x in lines] == [TRICKY, "second"]
    assert all(x["type"] == "user" for x in lines)
    assert not any("rm -rf" in a for a in json.loads(args.read_text(encoding="utf-8")))


def test_status_json_names_live_sessions_and_event_ids(wake, tmp_path, monkeypatch):
    class Hook:
        @staticmethod
        def _pid_alive(pid):
            return False
    monkeypatch.setattr(wake, "_hook", lambda: Hook)
    w = make(wake, tmp_path)
    w.role("CTO").update(last_event_id=7)
    w.on_message("CTO", msg(10), ["mentioned"])
    st = w.status()
    assert st["roles"]["CTO"]["live"] is True and st["roles"]["CTO"]["opened_by"] == "wake"
    assert st["roles"]["CTO"]["last_event_id"] == 7
    assert st["roles"]["CTO"]["topic"] == "#cto › 141 record shape"
    assert st["roles"]["PM"]["live"] is False and "PA" in st["roles"]
