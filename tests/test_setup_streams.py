"""Tests for bot/setup_streams.py against an in-memory fake of the Zulip admin
API. No live Zulip needed — the fake holds streams, users and subscriptions
the way the server would, so setup and --check are tested as a pair."""

from __future__ import annotations

import json
import os
import sys

import pytest

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

    def own_user(self):
        return {"user_id": 1, "email": "a@x", "full_name": "Admin"}


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
    # role bots: each in its own stream, plus #feature for CTO and COO (#56),
    # plus #pool for the COO (#80), plus #PM for the COO and CTO (#104), plus
    # #all for every role bot (#238)
    assert _bot_streams(z, "COO") == {"coo", "feature", "pool", "PM", "all"}
    assert _bot_streams(z, "CTO") == {"cto", "feature", "PM", "all"}
    assert _bot_streams(z, "CMO") == {"cmo", "all"}
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


def test_human_members_are_not_checked_outside_feature():
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.users.append({"user_id": 2, "full_name": "Founder", "email": "f@x", "is_bot": False})
    for name, s in z.streams.items():
        if name != "feature":
            z.subs[s["stream_id"]].add(2)
    assert setup_streams.run_check(z)


def test_check_fails_on_missing_stream():
    z = FakeZulip()
    setup_streams.run_setup(z)
    del z.streams["status"]
    assert not setup_streams.run_check(z)


# -- issue #56: #feature holds exactly the founder, the CTO bot and the COO bot
#
# The admin account ("Admin", user 1) is the founder. #feature is the one
# stream where human members are checked too.


def _members(z, stream):
    by_id = {u["user_id"]: u["full_name"] for u in z.users}
    return {by_id[i] for i in z.subs[z.streams[stream]["stream_id"]]}


def test_setup_creates_private_feature_with_founder_cto_and_coo_only(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    assert z.streams["feature"]["invite_only"]
    assert _members(z, "feature") == {"Admin", "CTO", "COO"}
    capsys.readouterr()
    assert setup_streams.run_check(z)
    assert "#feature: OK" in capsys.readouterr().out


def test_setup_subscribes_the_founder_to_a_feature_made_by_hand():
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["feature"]["stream_id"]].discard(1)
    assert not setup_streams.run_check(z)
    setup_streams.run_setup(z)
    assert _members(z, "feature") == {"Admin", "CTO", "COO"}
    assert setup_streams.run_check(z)


def test_check_fails_on_public_feature(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.streams["feature"]["invite_only"] = False
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "#feature: PUBLIC" in capsys.readouterr().out


def test_check_fails_when_feature_is_missing():
    z = FakeZulip()
    setup_streams.run_setup(z)
    del z.streams["feature"]
    assert not setup_streams.run_check(z)


@pytest.mark.parametrize("stray", ["CMO", "Watchdog"])
def test_check_fails_on_cmo_or_watchdog_in_feature(capsys, stray):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["feature"]["stream_id"]].add(_bot(z, stray)["user_id"])
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    out = capsys.readouterr().out
    assert f"#feature: FAIL — members that must not be subscribed: ['{stray}']" in out


def test_check_fails_on_another_human_in_feature(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.users.append({"user_id": 2, "full_name": "Guest", "email": "g@x", "is_bot": False})
    z.subs[z.streams["feature"]["stream_id"]].add(2)
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "members that must not be subscribed: ['Guest']" in capsys.readouterr().out


def test_check_fails_when_the_founder_is_not_in_feature(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["feature"]["stream_id"]].discard(1)
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "#feature: FAIL — the founder is not subscribed" in capsys.readouterr().out


@pytest.mark.parametrize("bot", ["CTO", "COO"])
def test_check_fails_when_cto_or_coo_is_not_in_feature(capsys, bot):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["feature"]["stream_id"]].discard(_bot(z, bot)["user_id"])
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "subscriptions: WRONG" in capsys.readouterr().out


def test_feature_does_not_open_other_role_streams():
    """The exception is #feature only: the CTO bot in #coo still fails."""
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["coo"]["stream_id"]].add(_bot(z, "CTO")["user_id"])
    assert not setup_streams.run_check(z)


# -- issue #80: #pool holds the founder, the COO bot and the Pool bot


def test_setup_creates_private_pool_with_founder_coo_and_pool_bot(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    assert z.streams["pool"]["invite_only"]
    assert _members(z, "pool") == {"Admin", "COO", "Pool"}
    assert _bot(z, "Pool")["email"] == "pool-bot@x"
    assert _bot_streams(z, "Pool") == {"pool", "all"}  # issue #238: also #all
    assert _bot_streams(z, "PM") == {"PM", "all"}  # issue #104, #238: #PM and #all
    capsys.readouterr()
    assert setup_streams.run_check(z)
    assert "#pool: OK" in capsys.readouterr().out


def test_setup_creates_the_pool_bot_once():
    z = FakeZulip()
    setup_streams.run_setup(z)
    setup_streams.run_setup(z)
    assert sum(1 for u in z.users if u["full_name"] == "Pool") == 1


def test_check_fails_when_the_pool_bot_is_missing(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    pool = _bot(z, "Pool")
    z.users.remove(pool)
    for subs in z.subs.values():
        subs.discard(pool["user_id"])
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "bot Pool: MISSING" in capsys.readouterr().out


@pytest.mark.parametrize("stray", ["CTO", "CMO", "Watchdog"])
def test_check_fails_on_another_bot_in_pool(capsys, stray):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["pool"]["stream_id"]].add(_bot(z, stray)["user_id"])
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert f"#pool: FAIL — bots that must not be subscribed: ['{stray}']" in capsys.readouterr().out


def test_check_fails_when_the_pool_bot_is_elsewhere_or_the_founder_is_not_in_pool(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["coo"]["stream_id"]].add(_bot(z, "Pool")["user_id"])
    assert not setup_streams.run_check(z)
    z.subs[z.streams["coo"]["stream_id"]].discard(_bot(z, "Pool")["user_id"])
    z.subs[z.streams["pool"]["stream_id"]].discard(1)
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "#pool: FAIL — the founder is not subscribed" in capsys.readouterr().out


# -- ops#238: a private #all stream with the founder and every role bot, plus
# PM and Pool, woken only by @-mention; the Watchdog is not a member.


def test_setup_creates_private_all_with_founder_and_every_bot_but_watchdog(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    assert z.streams["all"]["invite_only"]
    assert _members(z, "all") == {"Admin", "COO", "CTO", "CMO", "PM", "Pool"}
    capsys.readouterr()
    assert setup_streams.run_check(z)
    out = capsys.readouterr().out
    assert "#all: OK (the founder, ['cmo', 'coo', 'cto'], Pool and PM bots only)" in out


def test_setup_is_idempotent_for_all():
    z = FakeZulip()
    setup_streams.run_setup(z)
    before = _members(z, "all")
    setup_streams.run_setup(z)
    assert _members(z, "all") == before


def test_check_fails_when_watchdog_is_in_all(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["all"]["stream_id"]].add(_bot(z, "Watchdog")["user_id"])
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "#all: FAIL — bots that must not be subscribed: ['Watchdog']" in capsys.readouterr().out


@pytest.mark.parametrize("bot", ["COO", "CTO", "CMO", "PM", "Pool"])
def test_check_fails_when_a_role_bot_is_missing_from_all(bot):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["all"]["stream_id"]].discard(_bot(z, bot)["user_id"])
    assert not setup_streams.run_check(z)


def test_check_fails_when_the_founder_is_not_in_all(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["all"]["stream_id"]].discard(1)
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "#all: FAIL — the founder is not subscribed to #all" in capsys.readouterr().out


def test_check_fails_when_all_stream_is_missing():
    z = FakeZulip()
    setup_streams.run_setup(z)
    del z.streams["all"]
    assert not setup_streams.run_check(z)


def test_check_fails_on_public_all(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.streams["all"]["invite_only"] = False
    capsys.readouterr()
    assert not setup_streams.run_check(z)
    assert "#all: PUBLIC" in capsys.readouterr().out


# -- --check --json (ops#238): one JSON object, no [check] lines on stdout


def test_check_json_reports_ok_true_and_includes_the_text_lines(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    capsys.readouterr()
    ok = setup_streams.run_check(z, as_json=True)
    assert ok is True
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert any("#all: OK" in line for line in payload["checks"])


def test_check_json_emits_nothing_but_the_json_object(capsys):
    z = FakeZulip()
    setup_streams.run_setup(z)
    z.subs[z.streams["all"]["stream_id"]].discard(1)
    capsys.readouterr()
    ok = setup_streams.run_check(z, as_json=True)
    assert ok is False
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["ok"] is False
    assert any("#all: FAIL" in line for line in payload["checks"])
