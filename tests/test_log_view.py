"""chat/server.py's Log view (issue #155): GET /api/v1/log.

The data folder is the env var OPS_POOL_ROOT and holds log-YYYY-MM-DD.jsonl,
one JSON object per line (time, actor, action, target, result, and when known
run_id, link, error, next_step, credential). The lines below are built in that
documented format; none is a copy of a real pool log line (the session that
wrote this could not read the pool log).
"""
from __future__ import annotations

import json
import threading

import pytest

from test_chat_messages import Api, chat


def _line(time, actor, target, result="ok", **extra):
    return {"time": time, "actor": actor, "action": "claim", "target": target,
            "result": result, **extra}


@pytest.fixture
def logview(tmp_path, monkeypatch):
    """(api, root): a server whose OPS_POOL_ROOT is an empty tmp folder."""
    root = tmp_path / "pool"
    root.mkdir()
    monkeypatch.setenv("OPS_POOL_ROOT", str(root))
    server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bot = server.RequestHandlerClass.chat.s.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    api = Api(f"http://127.0.0.1:{server.server_address[1]}", bot["email"], bot["api_key"])
    yield api, root
    server.shutdown()
    server.server_close()


def _write(root, day, rows, extra_raw=()):
    text = "".join(json.dumps(r) + "\n" for r in rows) + "".join(x + "\n" for x in extra_raw)
    (root / f"log-{day}.jsonl").write_text(text, encoding="utf-8")


def test_log_view_filters_by_actor(logview):
    api, root = logview
    _write(root, "2026-10-21", [
        _line("2026-10-21T08:00:00Z", "coder", "determlab/agora#155"),
        _line("2026-10-21T08:05:00Z", "reviewer", "determlab/agora#155"),
        _line("2026-10-21T08:10:00Z", "coder", "determlab/ops#9")])
    status, body = api("GET", "log", actor="coder")
    assert status == 200
    assert [r["time"] for r in body["lines"]] == ["2026-10-21T08:10:00Z", "2026-10-21T08:00:00Z"]
    assert {r["actor"] for r in body["lines"]} == {"coder"}


def test_log_view_filters_by_target_and_is_newest_first(logview):
    api, root = logview
    _write(root, "2026-10-20", [_line("2026-10-20T23:00:00Z", "coder", "determlab/agora#155")])
    _write(root, "2026-10-21", [
        _line("2026-10-21T08:00:00Z", "coder", "determlab/agora#155"),
        _line("2026-10-21T08:10:00Z", "coder", "determlab/ops#9")])
    status, body = api("GET", "log", target="determlab/agora#155")
    assert status == 200
    assert [r["time"] for r in body["lines"]] == ["2026-10-21T08:00:00Z", "2026-10-20T23:00:00Z"]


def test_log_view_errors_only(logview):
    api, root = logview
    _write(root, "2026-10-21", [
        _line("2026-10-21T08:00:00Z", "coder", "t#1"),
        _line("2026-10-21T08:01:00Z", "coder", "t#2", result="failed", error="boom"),
        _line("2026-10-21T08:02:00Z", "coder", "t#3", error="push refused")])
    status, body = api("GET", "log", errors="1")
    assert status == 200
    assert [r["target"] for r in body["lines"]] == ["t#3", "t#2"]


def test_log_view_missing_folder_or_empty_log_is_an_empty_list(logview, monkeypatch, tmp_path):
    api, root = logview
    status, body = api("GET", "log")  # folder exists, no log file
    assert status == 200 and body["lines"] == [] and body["skipped"] == 0
    monkeypatch.setenv("OPS_POOL_ROOT", str(tmp_path / "nowhere"))
    status, body = api("GET", "log")
    assert status == 200 and body["lines"] == [] and "OPS_POOL_ROOT" in body["hint"]
    monkeypatch.delenv("OPS_POOL_ROOT")
    status, body = api("GET", "log")
    assert status == 200 and body["lines"] == [] and "OPS_POOL_ROOT" in body["hint"]


def test_log_view_malformed_line_is_skipped_and_counted(logview):
    api, root = logview
    _write(root, "2026-10-21", [_line("2026-10-21T08:00:00Z", "coder", "t#1")],
           extra_raw=["{not json", "[1, 2]", ""])
    status, body = api("GET", "log")
    assert status == 200
    assert len(body["lines"]) == 1 and body["skipped"] == 2


def test_log_view_limit_caps_the_lines(logview):
    api, root = logview
    _write(root, "2026-10-21", [_line(f"2026-10-21T08:{m:02d}:00Z", "coder", "t#1")
                                for m in range(10)])
    status, body = api("GET", "log", limit="3")
    assert status == 200 and [r["time"][11:16] for r in body["lines"]] == ["08:09", "08:08", "08:07"]
