"""chat/server.py's Agents tab (agora#204): GET /api/v1/agents.

Sources, all faked here in tmp folders: bot/.wake/state.json and wake.lock
(AGORA_WAKE_DIR), the chat database, the pool-view file (AGORA_POOL_VIEW) and
the daily log (OPS_POOL_ROOT).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

import pytest

from test_chat_messages import Api, chat


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_role_state_closed_resting_working():
    assert chat.role_state(None) == "closed"
    assert chat.role_state({}) == "closed"
    assert chat.role_state({"session": None}) == "closed"
    assert chat.role_state({"session": {"id": "x", "pending": []}}) == "resting"
    assert chat.role_state({"session": {"id": "x"}}) == "resting"
    assert chat.role_state({"session": {"id": "x", "pending": [{"id": 1}]}}) == "working"


def test_tokens_view_warns_when_the_token_data_is_older_than_two_hours():
    fresh = {"doc": {"tokens": {"agents": {}}, "plan": {"stale": False},
                     "brief": {"tokens": {"age_h": 1.5}}}, "stale": False}
    assert chat.tokens_view(fresh)["stale"] is False
    old = {"doc": {"tokens": {"agents": {}}, "brief": {"tokens": {"age_h": 2.5}}}, "stale": False}
    got = chat.tokens_view(old)
    assert got["stale"] is True and got["reason"] == "old" and got["age_h"] == 2.5
    assert chat.tokens_view({"doc": old["doc"], "stale": True})["reason"] == "old"
    sync_old = {"doc": {"tokens": {}, "brief": {"tokens": {"age_h": 0}}}, "stale": True}
    assert chat.tokens_view(sync_old)["reason"] == "sync-old"
    for nothing in ({"doc": None, "stale": True}, {"doc": {}, "stale": False}, {}):
        assert chat.tokens_view(nothing)["reason"] == "no-data"


@pytest.fixture
def agents(tmp_path, monkeypatch):
    wake = tmp_path / "wake"
    wake.mkdir()
    pool = tmp_path / "pool"
    pool.mkdir()
    monkeypatch.setenv("AGORA_WAKE_DIR", str(wake))
    monkeypatch.setenv("OPS_POOL_ROOT", str(pool))
    monkeypatch.setenv("AGORA_POOL_VIEW", str(tmp_path / "pool-view.json"))
    server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store = server.RequestHandlerClass.chat.s
    bot = store.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    api = Api(f"http://127.0.0.1:{server.server_address[1]}", bot["email"], bot["api_key"])
    yield api, wake, pool, tmp_path, store
    server.shutdown()
    server.server_close()


def test_agents_roles_daemon_and_the_company_roles_only(agents):
    api, wake, pool, tmp_path, store = agents
    now = time.time()
    (wake / "state.json").write_text(json.dumps({
        "roles": {"COO": {"session": {"id": "a", "pending": [{"id": 5}]}},
                  "CTO": {"session": {"id": "b", "pending": []}},
                  "PM": {"session": None}},
        "duty": {"COO": {"wakes": [now - 60, now - 3 * 86400]}}}), encoding="utf-8")
    (wake / "wake.lock").write_text("1", encoding="utf-8")
    coo = store.create_user("coo-bot@chat.localhost", "COO", is_bot=True)
    stream = store.one("SELECT stream_id FROM streams LIMIT 1")
    if stream is None:
        sid = store.x("INSERT INTO streams (name, invite_only) VALUES ('coo', 0)")
    else:
        sid = stream["stream_id"]
    for i in range(2):
        store.x("INSERT INTO messages (sender_id, type, stream_id, subject, content, timestamp) "
                "VALUES (?, 'stream', ?, 'chat', 'hi', ?)", (coo["id"], sid, int(now) - 10 + i))
    status, body = api("GET", "agents")
    assert status == 200, body
    by ={r["name"]: r for r in body["roles"]}
    assert "PA" not in by and set(by) == {"COO", "CTO", "PM", "CMO", "CPSO"}
    assert (by["COO"]["state"], by["CTO"]["state"], by["PM"]["state"], by["CMO"]["state"]) == (
        "working", "resting", "closed", "closed")        # a role with no entry at all is closed
    assert by["COO"]["model"] == "opus" and by["PM"]["model"] == "sonnet"
    assert by["COO"]["messages_today"] == 2 and by["COO"]["last_at"] and by["COO"]["last_room"]
    assert by["CTO"]["last_at"] is None and by["CTO"]["messages_today"] == 0
    assert by["COO"]["self_wakes_today"] == 1
    assert body["daemon"]["alive"] is True and body["daemon"]["state_at"] and body["daemon"]["lock_at"]
    assert body["generated_at"].endswith("Z")
    assert set(body) >= {"roles", "coders", "daemon", "tokens", "generated_at"}
    assert body["tokens"]["reason"] == "no-data"


def test_agents_daemon_is_not_alive_with_an_old_lock_or_none(agents):
    api, wake, *_ = agents
    status, body = api("GET", "agents")
    assert status == 200 and body["daemon"] == {"alive": False, "lock_at": None, "state_at": None}
    assert any("state.json" in e for e in body["errors"])
    lock = wake / "wake.lock"
    lock.write_text("1", encoding="utf-8")
    old = time.time() - chat.AGENTS_ALIVE_S - 60
    os.utime(lock, (old, old))
    assert api("GET", "agents")[1]["daemon"]["alive"] is False


def test_agents_coders_running_and_todays_claims(agents):
    api, wake, pool, tmp_path, store = agents
    now = time.time()
    chip = {"id": "agora#204", "title": "Agents", "url": "https://github.com/determlab/agora/issues/204",
            "note": "", "since": _iso(now - 600), "where": "cloud"}
    other = {**chip, "id": "ops#9", "url": "https://github.com/determlab/ops/issues/9", "where": "local",
             "model": "opus"}
    view = {"schema": "pool-view/1", "generated_at": _iso(now), "summary": {"running": 2, "queued": 0},
            "alerts": [],
            "stages": [{"key": k, "items": [chip, other] if k == "working" else []}
                       for k in ("queue", "working", "ci", "review", "needs_human", "merged")],
            "zones": [{"key": k, "parts": []} for k in ("local", "runner", "cloud")]}
    (tmp_path / "pool-view.json").write_text(json.dumps(view), encoding="utf-8")
    day = _iso(now)[:10]
    lines = [{"time": f"{day}T01:00:00Z", "actor": "cloud", "action": "claim", "target": "agora#204",
              "result": "ok"},
             {"time": f"{day}T02:00:00Z", "actor": "claim.py", "action": "claim", "target": "ops#9",
              "result": "won"},
             {"time": f"{day}T03:00:00Z", "actor": "claim.py", "action": "claim", "target": "ops#9",
              "result": "won"},
             {"time": f"{day}T04:00:00Z", "actor": "claim.py", "action": "claim", "target": "ops#10",
              "result": "lost"},
             {"time": "2020-01-01T03:00:00Z", "actor": "claim.py", "action": "claim", "target": "old#1",
              "result": "won"}]
    (pool / f"log-{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    status, body = api("GET", "agents")
    assert status == 200
    running = body["coders"]["running"]
    assert [(c["id"], c["where"], c["model"]) for c in running] == [
        ("agora#204", "cloud", None), ("ops#9", "local", "opus")]   # no model recorded: null, the page shows ?
    assert body["coders"]["today"] == {"local": 1, "cloud": 1}
