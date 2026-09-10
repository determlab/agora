"""Issue #26 — the heartbeat page's red rules, and the honesty rule around
them: a field the server never measured (`not_reported` / `no_data`) must
never be read as red. These are pure unit tests against synthetic data —
`agora/heartbeat.py`'s own docstring explains why `timers`/`loop` are not real
yet and `queue_depth`/`last_loop_pr` are real only for this repo's own working
tree. No live network or `gh` call is made anywhere in this file.
"""
from __future__ import annotations

import time
from pathlib import Path

from agora import heartbeat
from agora.room import AGENT, Hub


# ---- listening: real, from Room.touch (what room_wait actually refreshes) --

def test_listening_is_not_applicable_when_seated_nowhere(tmp_path):
    hub = Hub(tmp_path / "rooms")
    assert heartbeat.listening("nobody", [], hub) is None


def test_listening_true_right_after_room_wait(tmp_path):
    hub = Hub(tmp_path / "rooms")
    room = hub.create("standup")
    room.join("SHAL", role=AGENT)
    room.touch("SHAL")  # what mcp.py's room_wait calls on every poll
    assert heartbeat.listening("SHAL", [room.id], hub) is True


def test_listening_false_when_stale_past_ten_minutes(tmp_path):
    hub = Hub(tmp_path / "rooms")
    room = hub.create("standup")
    room.join("SHAL", role=AGENT)
    room.participants["SHAL"].last_seen = (
        time.time() - heartbeat.LISTENING_STALE_AFTER - 1)
    assert heartbeat.listening("SHAL", [room.id], hub) is False


def test_listening_false_when_seated_but_never_touched(tmp_path):
    """`join` sets last_seen too, so this covers a row whose rooms list came
    from somewhere else entirely — the honest fallback path in `listening`."""
    hub = Hub(tmp_path / "rooms")
    room = hub.create("standup")
    assert heartbeat.listening("ghost", [room.id], hub) is False


# ---- timers / loop: not_reported until something actually sends them ------

def test_timers_not_reported_with_no_registration():
    assert heartbeat.timers_field(None) == {"status": "not_reported", "items": []}


def test_timers_not_reported_when_registration_never_sent_them():
    assert heartbeat.timers_field({"name": "SHAL"}) == \
        {"status": "not_reported", "items": []}


def test_timers_reported_once_a_registration_sends_them():
    reg = {"name": "SHAL", "timers": [{"name": "loop", "cron": "*/5 * * * *",
                                       "next": time.time() + 300}]}
    field = heartbeat.timers_field(reg)
    assert field["status"] == "reported"
    assert field["items"][0]["name"] == "loop"


def test_loop_not_reported_by_default():
    assert heartbeat.loop_field(None) == {"status": "not_reported"}
    assert heartbeat.loop_field({"name": "SHAL"}) == {"status": "not_reported"}


def test_loop_reported_once_sent():
    field = heartbeat.loop_field({"loop": {"last_run": 1.0, "outcome": "ok"}})
    assert field["status"] == "reported"
    assert field["outcome"] == "ok"


# ---- the three red rules ---------------------------------------------------

def _base_row(**overrides):
    row = {
        "listening": True,
        "timers": {"status": "not_reported", "items": []},
        "loop": {"status": "not_reported"},
        "queue_depth": {"status": "no_data", "value": None},
    }
    row.update(overrides)
    return row


def test_not_red_when_nothing_is_reported():
    """The core honesty check: a row where every optional field is
    not_reported/no_data must never be red — that would be inventing a state
    nobody measured (D3)."""
    red, reasons = heartbeat.evaluate_red(_base_row())
    assert red is False and reasons == []


def test_red_when_not_listening_in_a_room():
    red, reasons = heartbeat.evaluate_red(_base_row(listening=False))
    assert red is True
    assert "listening" in reasons[0]


def test_not_red_when_not_in_any_room():
    """`listening is None` means the rule does not apply — not red."""
    red, reasons = heartbeat.evaluate_red(_base_row(listening=None))
    assert red is False and reasons == []


def test_red_when_a_reported_timer_has_no_next_fire():
    row = _base_row(timers={"status": "reported",
                            "items": [{"name": "watchdog", "cron": "* * * * *",
                                      "next": None}]})
    red, reasons = heartbeat.evaluate_red(row)
    assert red is True
    assert "watchdog" in reasons[0] and "not armed" in reasons[0]


def test_not_red_when_all_reported_timers_are_armed():
    row = _base_row(timers={"status": "reported",
                            "items": [{"name": "watchdog", "cron": "* * * * *",
                                      "next": time.time() + 60}]})
    red, reasons = heartbeat.evaluate_red(row)
    assert red is False and reasons == []


def test_red_when_loop_is_stale_past_twice_its_period_with_a_queue():
    row = _base_row(
        loop={"status": "reported", "period_seconds": 900,
             "last_run": time.time() - 2000},
        queue_depth={"status": "reported", "value": 3})
    red, reasons = heartbeat.evaluate_red(row)
    assert red is True
    assert "loop" in reasons[0]


def test_not_red_when_loop_stale_but_queue_is_empty():
    """A dead loop with nothing queued is not urgent — the issue's rule is
    conjunctive: stale AND a non-empty queue."""
    row = _base_row(
        loop={"status": "reported", "period_seconds": 900,
             "last_run": time.time() - 2000},
        queue_depth={"status": "reported", "value": 0})
    red, reasons = heartbeat.evaluate_red(row)
    assert red is False and reasons == []


def test_not_red_when_loop_reported_but_queue_depth_is_no_data():
    """This is the case that actually matters today: every other repo reports
    a loop it never will (nothing sends `loop` yet) with no_data queue depth,
    since this server cannot see that repo's GitHub state either. The rule
    must not fire on the no_data half."""
    row = _base_row(
        loop={"status": "reported", "period_seconds": 900,
             "last_run": time.time() - 2000},
        queue_depth={"status": "no_data", "value": None})
    red, reasons = heartbeat.evaluate_red(row)
    assert red is False and reasons == []


def test_not_red_when_loop_has_no_declared_period():
    """No `period_seconds` means "2x its period" cannot be computed without
    guessing at a cron string this repo has no parser for (D2). The rule is
    genuinely not evaluated, not silently passed as green-by-default — this
    test documents that gap rather than hiding it."""
    row = _base_row(
        loop={"status": "reported", "last_run": time.time() - 999999},
        queue_depth={"status": "reported", "value": 5})
    red, reasons = heartbeat.evaluate_red(row)
    assert red is False and reasons == []


# ---- repo_snapshot: no real gh/network call anywhere here ------------------

def test_repo_snapshot_is_no_data_without_gh(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: None)
    snap = heartbeat.repo_snapshot(tmp_path, fresh=True)
    assert snap["queue_depth"]["status"] == "no_data"
    assert snap["last_loop_pr"]["status"] == "no_data"


def test_repo_snapshot_is_no_data_without_a_git_repo(tmp_path, monkeypatch):
    """`gh` present but this is not a git working tree at all — still no_data,
    never a crash, never a guess."""
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/gh")
    snap = heartbeat.repo_snapshot(tmp_path, fresh=True)
    assert snap["queue_depth"]["status"] == "no_data"


def test_repo_snapshot_parses_gh_output_through_an_injected_runner(tmp_path, monkeypatch):
    """Proves the parsing path works, without ever shelling out for real."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/gh")

    class FakeResult:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout

    def fake_runner(args, **kwargs):
        if "issue" in args:
            return FakeResult('[{"number": 1}, {"number": 2}]')
        return FakeResult('[{"number": 9, "title": "x", "url": "u", '
                          '"createdAt": "t", "mergedAt": null, "state": "OPEN"}]')

    snap = heartbeat.repo_snapshot(tmp_path, runner=fake_runner, fresh=True)
    assert snap["queue_depth"] == {"status": "reported", "value": 2}
    assert snap["last_loop_pr"]["status"] == "reported"
    assert snap["last_loop_pr"]["number"] == 9


def test_repo_snapshot_is_no_data_when_gh_times_out(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(heartbeat.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_runner(args, **kwargs):
        import subprocess
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    snap = heartbeat.repo_snapshot(tmp_path, runner=fake_runner, fresh=True)
    assert snap["queue_depth"]["status"] == "no_data"
    assert snap["last_loop_pr"]["status"] == "no_data"


# ---- build_row: end to end on synthetic input, still no hub-less crash -----

def test_build_row_marks_this_repo_by_cwd(tmp_path):
    hub = Hub(tmp_path / "rooms")
    session_row = {"name": "AGORA", "provider": "claude-code", "source": "registry",
                   "rooms": [], "liveness": "idle", "cwd": str(tmp_path)}
    repo_data = {"queue_depth": {"status": "reported", "value": 1},
                "last_loop_pr": {"status": "reported", "number": 5}}
    row = heartbeat.build_row(session_row, hub, {}, tmp_path, repo_data)
    assert row["this_repo"] is True
    assert row["queue_depth"]["value"] == 1


def test_build_row_no_data_for_a_different_repo(tmp_path):
    hub = Hub(tmp_path / "rooms")
    other = tmp_path / "elsewhere"
    other.mkdir()
    session_row = {"name": "SHAL", "provider": "claude-code", "source": "registry",
                   "rooms": [], "liveness": "idle", "cwd": str(other)}
    repo_data = {"queue_depth": {"status": "reported", "value": 1},
                "last_loop_pr": {"status": "reported", "number": 5}}
    row = heartbeat.build_row(session_row, hub, {}, tmp_path, repo_data)
    assert row["this_repo"] is False
    assert row["queue_depth"] == {"status": "no_data", "value": None}
    assert row["last_loop_pr"] == {"status": "no_data"}
