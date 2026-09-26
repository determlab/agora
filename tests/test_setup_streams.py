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


def test_check_fails_on_missing_stream():
    z = FakeZulip()
    setup_streams.run_setup(z)
    del z.streams["status"]
    assert not setup_streams.run_check(z)
