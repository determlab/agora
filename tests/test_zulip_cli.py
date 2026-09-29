"""bot/zulip.py send|read, with Zulip's HTTP layer stubbed and no network.

Two of these run the script as a subprocess, the way a session calls it
through Bash: a script that works when imported and fails when run is the
defect that cost ops #1 and #2 a round each.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
SCRIPT = BOT_DIR / "zulip.py"
BOT = "coo-bot@zulip.localhost"


def _load():
    sys.path.insert(0, str(BOT_DIR))
    try:
        spec = importlib.util.spec_from_file_location("zulip_cli_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(BOT_DIR))


def msg(i: int, content: str = "hi", **extra) -> dict:
    return {"id": i, "type": "stream", "display_recipient": "coo",
            "subject": "smoke", "content": content, "timestamp": 1790000000 + i,
            "sender_full_name": "Founder", "sender_email": "founder@x", **extra}


class FakeZulip:
    """Stands in for ZulipClient._request. Records every call."""

    def __init__(self, subscribed=("coo",), exists=("coo", "cto"), messages=(), unread=None):
        self.subscribed, self.exists = list(subscribed), list(exists)
        self.messages = list(messages)
        self.unread = list(unread) if unread is not None else []
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, client, method, path, params=None):
        from zulip_client import ZulipError
        self.calls.append((method, path, params or {}))
        if path == "users/me/subscriptions":
            return {"result": "success",
                    "subscriptions": [{"name": n} for n in self.subscribed]}
        if path == "get_stream_id":
            if params["stream"] in self.exists:
                return {"result": "success", "stream_id": 1}
            raise ZulipError("Invalid channel name")
        if path == "messages" and method == "POST":
            return {"result": "success", "id": 99}
        if path == "messages" and method == "GET":
            anchor = params["anchor"]
            if anchor == "newest":
                found = self.messages
            else:  # Zulip may include the anchor; the CLI must drop it
                found = [m for m in self.messages if m["id"] >= int(anchor)]
            return {"result": "success", "messages": list(reversed(found))}
        if path == "unread" and method == "GET":
            return {"result": "success", "streams": self.unread}
        if path == "mark_topic_as_read" and method == "POST":
            return {"result": "success"}
        if path == "mark_stream_as_read" and method == "POST":
            return {"result": "success"}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def cli(monkeypatch, tmp_path):
    mod = _load()
    monkeypatch.setattr(mod, "DOTENV", str(tmp_path / "missing.env"))
    monkeypatch.setenv("ZULIP_BOT_EMAIL", BOT)
    monkeypatch.setenv("ZULIP_BOT_API_KEY", "test-key")
    return mod


def _stub(monkeypatch, cli, fake: FakeZulip) -> FakeZulip:
    monkeypatch.setattr(cli.ZulipClient, "_request",
                        lambda self, m, p, params=None: fake(self, m, p, params))
    return fake


def test_send_posts_and_prints_the_id(cli, monkeypatch, capsys):
    fake = _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["send", "--stream", "coo", "--topic", "#141 record shape",
                     "--text", "hello"]) == 0
    assert "id=99" in capsys.readouterr().out
    method, path, params = fake.calls[-1]
    assert (method, path) == ("POST", "messages")
    assert params == {"type": "stream", "to": "coo", "content": "hello",
                      "topic": "#141 record shape"}


def test_send_to_unsubscribed_stream_is_refused_and_sends_nothing(
        cli, monkeypatch, capsys):
    fake = _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["send", "--stream", "cto", "--topic", "t", "--text", "x"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "not subscribed to #cto" in out.err
    assert not [c for c in fake.calls if c[:2] == ("POST", "messages")]


def test_send_to_missing_stream_says_so(cli, monkeypatch, capsys):
    _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["send", "--stream", "nosuch", "--topic", "t", "--text", "x"]) == 2
    assert "#nosuch does not exist" in capsys.readouterr().err


def test_read_prints_oldest_first_with_sender_timestamp_and_id(
        cli, monkeypatch, capsys):
    _stub(monkeypatch, cli, FakeZulip(messages=[msg(5, "first"), msg(9, "second")]))
    assert cli.main(["read", "--stream", "coo", "--topic", "smoke"]) == 0
    out = capsys.readouterr().out
    assert out.index("[5]") < out.index("[9]")
    assert "Founder <founder@x>" in out and "UTC" in out and "second" in out


def test_read_since_is_strictly_after_the_id(cli, monkeypatch, capsys):
    fake = _stub(monkeypatch, cli, FakeZulip(
        messages=[msg(5, "old"), msg(7, "boundary"), msg(9, "new")]))
    assert cli.main(["read", "--mentions", "--since", "7"]) == 0
    out = capsys.readouterr().out
    assert "[9]" in out
    assert "[7]" not in out and "[5]" not in out
    params = fake.calls[-1][2]
    assert params["narrow"] == [{"operator": "is", "operand": "mentioned"}]
    assert params["anchor"] == 7 and params["include_anchor"] == "false"


def test_as_pool_read_json_prints_one_object_per_message_after_since(
        cli, monkeypatch, capsys):
    """The pool daemon's read (issue #80): JSON Lines, strictly after --since."""
    monkeypatch.setenv("ZULIP_POOL_EMAIL", "pool-bot@x")
    monkeypatch.setenv("ZULIP_POOL_API_KEY", "k")
    pool = {"display_recipient": "pool", "subject": "pool control"}
    fake = _stub(monkeypatch, cli, FakeZulip(subscribed=("pool",), exists=("pool",), messages=[
        msg(5, "old", **pool), msg(7, "boundary", **pool), msg(9, "pause shal", **pool),
        msg(11, "status ✓", **pool)]))
    assert cli.main(["--as", "POOL", "read", "--stream", "pool", "--since", "7", "--json"]) == 0
    lines = capsys.readouterr().out.splitlines()
    rows = [json.loads(line) for line in lines]
    assert [r["id"] for r in rows] == [9, 11]
    assert rows[0] == {"id": 9, "timestamp": 1790000009, "sender_full_name": "Founder",
                       "sender_email": "founder@x", "stream": "pool",
                       "topic": "pool control", "content": "pause shal"}
    assert rows[1]["content"] == "status ✓"
    params = fake.calls[-1][2]
    assert params["narrow"] == [{"operator": "channel", "operand": "pool"}]
    assert params["anchor"] == 7 and params["include_anchor"] == "false"


def test_read_json_with_nothing_new_prints_nothing_on_stdout(cli, monkeypatch, capsys):
    _stub(monkeypatch, cli, FakeZulip(messages=[msg(5)]))
    assert cli.main(["read", "--stream", "coo", "--since", "5", "--json"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "(no messages)" in out.err


def test_read_unsubscribed_stream_is_refused(cli, monkeypatch, capsys):
    _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["read", "--stream", "cto"]) == 2
    assert "not subscribed to #cto" in capsys.readouterr().err


def test_as_name_picks_that_bots_credentials(cli, monkeypatch):
    monkeypatch.setenv("ZULIP_COO_EMAIL", "coo@x")
    monkeypatch.setenv("ZULIP_COO_API_KEY", "k")
    assert cli.load_client("COO").email == "coo@x"


@pytest.mark.parametrize("role", ["CTO", "COO"])
def test_as_cto_or_coo_sends_to_feature(cli, monkeypatch, capsys, role):
    """#feature (issue #56) needs nothing special in the CLI: each bot's own
    subscriptions decide, so both of its members can post there."""
    monkeypatch.setenv(f"ZULIP_{role}_EMAIL", f"{role.lower()}-bot@x")
    monkeypatch.setenv(f"ZULIP_{role}_API_KEY", "k")
    fake = _stub(monkeypatch, cli, FakeZulip(
        subscribed=(role.lower(), "feature"), exists=("coo", "cto", "feature")))
    assert cli.main(["--as", role, "send", "--stream", "feature",
                     "--topic", "ops#44 routes", "--text", "step one"]) == 0
    assert "sent #feature › ops#44 routes id=99" in capsys.readouterr().out
    assert fake.calls[-1][2] == {"type": "stream", "to": "feature",
                                 "content": "step one", "topic": "ops#44 routes"}


def test_cmo_is_refused_in_feature(cli, monkeypatch, capsys):
    monkeypatch.setenv("ZULIP_CMO_EMAIL", "cmo-bot@x")
    monkeypatch.setenv("ZULIP_CMO_API_KEY", "k")
    fake = _stub(monkeypatch, cli, FakeZulip(subscribed=("cmo",), exists=()))
    assert cli.main(["--as", "CMO", "send", "--stream", "feature",
                     "--topic", "t", "--text", "x"]) == 2
    assert "Nothing was sent" in capsys.readouterr().err
    assert not [c for c in fake.calls if c[:2] == ("POST", "messages")]


# -- issue #168: unread and mark-read


def test_unread_prints_streams_and_topics(cli, monkeypatch, capsys):
    fake = _stub(monkeypatch, cli, FakeZulip(unread=[
        {"stream_id": 1, "name": "coo", "unread": 3,
         "topics": [{"name": "t1", "unread": 2}, {"name": "t2", "unread": 1}]}]))
    assert cli.main(["unread"]) == 0
    out = capsys.readouterr().out
    assert "#coo: 3 unread" in out and "t1: 2" in out and "t2: 1" in out
    assert fake.calls[-1][:2] == ("GET", "unread")


def test_unread_json_prints_the_raw_streams_list(cli, monkeypatch, capsys):
    streams = [{"stream_id": 1, "name": "coo", "unread": 1,
               "topics": [{"name": "t1", "unread": 1}]}]
    _stub(monkeypatch, cli, FakeZulip(unread=streams))
    assert cli.main(["unread", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"streams": streams}


def test_unread_with_nothing_says_so_on_stderr(cli, monkeypatch, capsys):
    _stub(monkeypatch, cli, FakeZulip(unread=[]))
    assert cli.main(["unread"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "no unread" in out.err


def test_mark_read_calls_mark_topic_as_read_with_the_streams_id(cli, monkeypatch, capsys):
    fake = _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["mark-read", "--stream", "coo", "--topic", "smoke"]) == 0
    assert "marked read: #coo › smoke" in capsys.readouterr().out
    method, path, params = fake.calls[-1]
    assert (method, path) == ("POST", "mark_topic_as_read")
    assert params == {"stream_id": 1, "topic_name": "smoke"}


def test_mark_read_json_prints_one_object(cli, monkeypatch, capsys):
    _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["mark-read", "--stream", "coo", "--topic", "smoke", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "stream": "coo", "topic": "smoke"}


def test_mark_read_of_a_stream_that_does_not_exist_is_refused(cli, monkeypatch, capsys):
    _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["mark-read", "--stream", "nosuch", "--topic", "t"]) == 1
    assert "refused by Zulip" in capsys.readouterr().err


def test_mark_read_without_a_topic_marks_the_whole_stream(cli, monkeypatch, capsys):
    # No --topic calls mark_stream_as_read, never mark_topic_as_read.
    fake = _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["mark-read", "--stream", "coo"]) == 0
    assert "marked read: #coo (all topics)" in capsys.readouterr().out
    method, path, params = fake.calls[-1]
    assert (method, path) == ("POST", "mark_stream_as_read")
    assert params == {"stream_id": 1}


def test_mark_read_without_a_topic_json_prints_a_null_topic(cli, monkeypatch, capsys):
    _stub(monkeypatch, cli, FakeZulip())
    assert cli.main(["mark-read", "--stream", "coo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "stream": "coo", "topic": None}


def _run(args, env_extra, tmp_path, prelude=""):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZULIP_")}
    env.update(env_extra)
    if prelude:
        runner = tmp_path / "run.py"
        runner.write_text(
            f"import runpy, sys\nsys.path.insert(0, {str(BOT_DIR)!r})\n{prelude}\n"
            f"sys.argv = [{str(SCRIPT)!r}, *{args!r}]\n"
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n",
            encoding="utf-8")
        cmd = [sys.executable, str(runner)]
    else:
        cmd = [sys.executable, str(SCRIPT), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=tmp_path, timeout=60)


def test_run_as_a_script_refuses_an_unsubscribed_stream(tmp_path):
    prelude = (
        "import zulip_client\n"
        "def fake(self, method, path, params=None):\n"
        "    if path == 'users/me/subscriptions':\n"
        "        return {'result': 'success', 'subscriptions': [{'name': 'coo'}]}\n"
        "    if path == 'get_stream_id':\n"
        "        return {'result': 'success', 'stream_id': 2}\n"
        "    raise AssertionError('sent anyway: ' + path)\n"
        "zulip_client.ZulipClient._request = fake\n")
    done = _run(["send", "--stream", "cto", "--topic", "t", "--text", "x"],
                {"ZULIP_BOT_EMAIL": BOT, "ZULIP_BOT_API_KEY": "k"}, tmp_path, prelude)
    assert done.returncode == 2, done.stderr
    assert done.stdout == ""
    assert "not subscribed to #cto" in done.stderr


def test_run_as_a_script_without_credentials_names_what_is_missing(tmp_path):
    """The plain script, no wrapper. A bot name nobody has, so a developer's
    real bot/.env can never turn this into a network call."""
    done = _run(["--as", "nobody test", "read", "--mentions"], {}, tmp_path)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "ZULIP_NOBODY_TEST_EMAIL" in done.stderr
