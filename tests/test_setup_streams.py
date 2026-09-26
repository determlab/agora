"""Tests for bot/setup_streams.py against an in-memory fake of the Zulip admin
API. No live Zulip needed — the fake holds streams, users and subscriptions
the way the server would, so setup and --check are tested as a pair."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import setup_streams  # noqa: E402


class FakeZulip:
    def __init__(self):
        self.streams: dict[str, dict] = {}
        self.subs: dict[int, set[int]] = {}
        self.users: list[dict] = [{"user_id": 1, "full_name": "Admin", "email": "a@x", "is_bot": False}]
        self._next = 100

    def _id(self):
        self._next += 1
        return self._next

    def list_streams(self):
        return [dict(s) for s in self.streams.values()]

    def create_stream(self, name):
        sid = self._id()
        self.streams[name] = {"name": name, "stream_id": sid, "invite_only": True}
        self.subs[sid] = {1}

    def add_public_stream(self, name):
        sid = self._id()
        self.streams[name] = {"name": name, "stream_id": sid, "invite_only": False}
        self.subs[sid] = {1}
        return sid

    def make_private(self, stream_id):
        for s in self.streams.values():
            if s["stream_id"] == stream_id:
                s["invite_only"] = True

    def list_users(self):
        return [dict(u) for u in self.users]

    def create_bot(self, full_name, short_name):
        uid = self._id()
        self.users.append({"user_id": uid, "full_name": full_name, "email": f"{short_name}-bot@x", "is_bot": True})
        return {"user_id": uid}

    def subscribe(self, stream, principals):
        sid = self.streams[stream]["stream_id"]
        for u in self.users:
            if u["email"] in principals:
                self.subs[sid].add(u["user_id"])

    def stream_subscribers(self, stream_id):
        return sorted(self.subs[stream_id])


def test_setup_then_check_passes_and_streams_are_private():
    z = FakeZulip()
    setup_streams.run_setup(z)
    assert setup_streams.run_check(z)
    assert all(s["invite_only"] for s in z.streams.values())


def test_setup_is_idempotent():
    z = FakeZulip()
    setup_streams.run_setup(z)
    before = (z.list_streams(), z.list_users(), {k: set(v) for k, v in z.subs.items()})
    setup_streams.run_setup(z)
    assert (z.list_streams(), z.list_users(), z.subs) == before


def test_check_fails_on_public_stream_and_setup_converts_it(capsys):
    z = FakeZulip()
    z.add_public_stream("coo")
    setup_streams.run_setup(z)  # converts in place, keeps the same id
    assert z.streams["coo"]["invite_only"]
    z.streams["cmo"]["invite_only"] = False
    assert not setup_streams.run_check(z)
    assert "#cmo: PUBLIC" in capsys.readouterr().out


def test_check_fails_on_stray_subscription():
    z = FakeZulip()
    setup_streams.run_setup(z)
    cto = next(u for u in z.users if u["full_name"] == "CTO")
    z.subs[z.streams["cmo"]["stream_id"]].add(cto["user_id"])
    assert not setup_streams.run_check(z)


def test_check_fails_on_bot_in_status():
    z = FakeZulip()
    setup_streams.run_setup(z)
    coo = next(u for u in z.users if u["full_name"] == "COO")
    z.subs[z.streams["status"]["stream_id"]].add(coo["user_id"])
    assert not setup_streams.run_check(z)


def _bot(z, full_name):
    return next(u for u in z.users if u["full_name"] == full_name)


def _bot_streams(z, full_name):
    uid = _bot(z, full_name)["user_id"]
    return {name for name, s in z.streams.items() if uid in z.subs[s["stream_id"]]}


def test_setup_creates_watchdog_on_status_and_coo_only(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    wd = _bot(z, "Watchdog")
    assert wd["is_bot"] and wd["email"] == "watchdog-bot@x"
    assert _bot_streams(z, "Watchdog") == {"status", "coo"}
    # role bots unchanged: each in its own stream only
    assert _bot_streams(z, "COO") == {"coo"}
    assert _bot_streams(z, "CTO") == {"cto"}
    assert _bot_streams(z, "CMO") == {"cmo"}
    capsys.readouterr()
    assert setup_streams.run_check(z)
    out = capsys.readouterr().out
    assert "#status: OK (only Watchdog subscribed)" in out


def test_setup_creates_watchdog_once():
    z = FakeZulip()
    setup_streams.run_setup(z)
    setup_streams.run_setup(z)
    assert sum(1 for u in z.users if u["full_name"] == "Watchdog") == 1


def test_setup_subscribes_existing_watchdog_that_was_missing_a_stream():
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["coo"]["stream_id"]].discard(_bot(z, "Watchdog")["user_id"])
    assert not setup_streams.run_check(z)
    setup_streams.run_setup(z)
    assert _bot_streams(z, "Watchdog") == {"status", "coo"}
    assert setup_streams.run_check(z)


def test_check_fails_on_coo_bot_in_status_even_with_watchdog(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["status"]["stream_id"]].add(_bot(z, "COO")["user_id"])
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "#status: FAIL — bots that must not be subscribed: ['COO']" in capsys.readouterr().out


def test_check_fails_on_unknown_bot_in_status():
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.create_bot("Echo", "echo")
    z.subs[z.streams["status"]["stream_id"]].add(_bot(z, "Echo")["user_id"])
    assert not setup_streams.run_check(z)


def test_check_fails_when_watchdog_in_cto():
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["cto"]["stream_id"]].add(_bot(z, "Watchdog")["user_id"])
    assert not setup_streams.run_check(z)


def test_check_fails_when_watchdog_missing(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    wd = _bot(z, "Watchdog")
    z.users.remove(wd)
    for subs in z.subs.values():
        subs.discard(wd["user_id"])
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    out = capsys.readouterr().out
    assert "bot Watchdog: MISSING" in out
    assert "#status: FAIL — Watchdog is not subscribed" in out


def test_check_fails_when_watchdog_not_in_status():
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["status"]["stream_id"]].discard(_bot(z, "Watchdog")["user_id"])
    assert not setup_streams.run_check(z)


def test_check_fails_on_unknown_bot_in_role_stream():
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.create_bot("Echo", "echo")
    z.subs[z.streams["cmo"]["stream_id"]].add(_bot(z, "Echo")["user_id"])
    assert not setup_streams.run_check(z)


def test_human_members_are_not_checked():
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.users.append({"user_id": 2, "full_name": "Founder", "email": "f@x", "is_bot": False})
    for s in z.streams.values():
        z.subs[s["stream_id"]].add(2)
    assert setup_streams.run_check(z)


def test_check_fails_on_missing_stream():
    z = FakeZulip()
    setup_streams.run_setup(z)
    del z.streams["status"]
    assert not setup_streams.run_check(z)
