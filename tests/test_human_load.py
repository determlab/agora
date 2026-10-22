"""agora#215: the human-load report. Messages are made up; the work-queue view
is the real pool-view/1 sample."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "chat" / "server.py"
EXAMPLE = ROOT / "chat" / "human-load.example.json"
VIEW = ROOT / "tests" / "fixtures" / "pool-view.json"
NZ = ZoneInfo("Pacific/Auckland")


def _load():
    spec = importlib.util.spec_from_file_location("chat_server_human_load", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chat = _load()


def ts(day: str, hhmm: str = "12:00") -> int:
    return int(datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=NZ).timestamp())


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "chat.sqlite3"
    store = chat.Store(str(db))
    human = store.create_user("h@chat.localhost", "Human", is_bot=False)
    bot = store.create_user("b@chat.localhost", "Bot", is_bot=True)
    rooms = {n: store.create_stream(n) for n in ("general", "noise", "build")}
    cfg = json.loads(EXAMPLE.read_text())
    cfg["human_user_id"] = human["id"]
    cfg["work_queue_view"] = str(VIEW)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))

    def say(who, room, text, when):
        store.x("INSERT INTO messages (sender_id, type, stream_id, subject, content, timestamp)"
                " VALUES (?,?,?,?,?,?)",
                (who["id"], "stream" if room else "private", rooms.get(room), "t", text, when))

    return {"db": str(db), "cfg": str(cfg_path), "cfg_dict": cfg, "say": say, "human": human,
            "bot": bot, "tmp": tmp_path}


def run(env, *extra, config=None):
    return subprocess.run(
        [sys.executable, str(SERVER), "human-load", "--config", config or env["cfg"],
         "--db", env["db"], "--since", "2026-10-01", "--until", "2026-10-31", "--json", *extra],
        capture_output=True, text=True, cwd=ROOT)


def report(env):
    r = run(env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_one_message_per_category_counts_in_its_category(env):
    h, say = env["human"], env["say"]
    for text in ("status?", "ping", "I am stuck here", "go on", "Traceback (most recent call last)",
                 "that is wrong"):
        say(h, "general", text, ts("2026-10-05"))
    say(h, "general", "lovely weather", ts("2026-10-05"))
    doc = report(env)
    assert doc["totals"]["by_category"] == {c: 1 for c in chat.LOAD_CATEGORIES}
    assert doc["totals"]["supervision"] == 6 and doc["totals"]["human_messages"] == 7
    assert doc["totals"]["share"] == round(6 / 7, 4)
    assert doc["totals"]["by_room"] == {"main": 6}


def test_quote_block_and_reply_prefix_are_stripped_before_matching(env):
    h, say = env["human"], env["say"]
    say(h, "general", "@_**Bot** [said](#near/3):\n```quote\nI am stuck, status?\n```\nthanks",
        ts("2026-10-05"))
    say(h, "general", "@_**Bot** ok then", ts("2026-10-05"))
    say(h, "general", "@_**Bot** status please", ts("2026-10-05"))
    doc = report(env)
    assert doc["totals"]["supervision"] == 1
    assert doc["totals"]["human_messages"] == 3


def test_local_midnight_boundary_and_active_day_metric(env):
    h, say = env["human"], env["say"]
    say(h, "general", "status", ts("2026-10-05", "23:59"))
    say(h, "general", "status", ts("2026-10-06", "00:00"))
    say(h, "general", "hello", ts("2026-10-06", "00:01"))
    doc = report(env)
    assert [d["date"] for d in doc["days"]] == ["2026-10-05", "2026-10-06"]
    assert [d["human_messages"] for d in doc["days"]] == [1, 2]
    assert doc["totals"]["active_days"] == 2
    assert doc["totals"]["supervision_per_active_day"] == 1.0
    assert doc["weeks"][0]["week"] == "2026-W41" and doc["weeks"][0]["active_days"] == 2


def test_an_excluded_stream_is_not_counted_at_all(env):
    h, say = env["human"], env["say"]
    say(h, "noise", "status", ts("2026-10-05"))
    say(h, "build", "status", ts("2026-10-05"))
    doc = report(env)
    assert doc["totals"]["human_messages"] == 1
    assert doc["totals"]["by_room"] == {"build room": 1}


def test_agent_side_asks_and_stop_lines_come_from_bot_messages(env):
    b, say = env["bot"], env["say"]
    say(b, "general", "needs your decision", ts("2026-10-05"))
    say(b, "general", "STOP: blocked", ts("2026-10-05"))
    doc = report(env)
    assert doc["agent_side"]["asks_to_human"] == 1
    assert doc["agent_side"]["bot_stop_lines"] == 1


def test_the_agent_side_block_reads_the_real_pool_view(env):
    doc = report(env)["agent_side"]
    assert doc["work_queue"]["generated_at"] == "2026-10-21T06:35:02Z"
    ids = {i["id"]: i for i in doc["items_past_max_age"]["items"]}
    # ci item since 03:00, 3.58h old against a 1h limit; the queue ones are < 4h
    assert "demo/lib#7" in ids and ids["demo/lib#7"]["stage"] == "ci"
    assert "demo/app#11" not in ids
    assert doc["items_past_max_age"]["count"] == len(ids)
    # something is working, so the queue is not idle
    assert doc["queue_idle_hours"] == 0


def test_the_database_opens_read_only(env):
    con = chat.open_read_only(env["db"])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("DELETE FROM messages")
    con.close()


def test_output_validates_against_schema_v1_and_out_file_matches(env):
    env["say"](env["human"], "general", "status", ts("2026-10-05"))
    out = env["tmp"] / "load.json"
    r = run(env, "--out", str(out))
    assert r.returncode == 0
    assert chat.validate_human_load(json.loads(out.read_text())) == []
    assert json.loads(out.read_text()) == json.loads(r.stdout)
    assert chat.validate_human_load({"schema": 1}) != []
    assert "schema" in chat.validate_human_load({"schema": 2})[0]


def test_missing_key_exits_2_naming_the_key_and_the_fix(env):
    cfg = dict(env["cfg_dict"])
    del cfg["timezone"]
    p = env["tmp"] / "bad.json"
    p.write_text(json.dumps(cfg))
    r = run(env, config=str(p))
    assert r.returncode == 2
    assert "timezone" in r.stderr and "add it" in r.stderr


def test_no_config_exits_2_and_says_how_to_pass_one(env):
    r = subprocess.run([sys.executable, str(SERVER), "human-load", "--db", env["db"],
                        "--since", "2026-10-01", "--until", "2026-10-02"],
                       capture_output=True, text=True, cwd=ROOT,
                       env={k: v for k, v in __import__("os").environ.items()
                            if k != "AGORA_CONFIG"})
    assert r.returncode == 2 and "--config" in r.stderr and "AGORA_CONFIG" in r.stderr


def test_ten_thousand_messages_in_under_five_seconds(env):
    h = env["human"]
    base = ts("2026-10-05")
    rows = [(h["id"], "stream", 1, "t", "status" if i % 3 == 0 else "hello", base + i * 5)
            for i in range(10_000)]
    con = sqlite3.connect(env["db"])
    con.executemany("INSERT INTO messages (sender_id, type, stream_id, subject, content, "
                    "timestamp) VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    t0 = time.time()
    r = run(env)
    assert r.returncode == 0
    assert time.time() - t0 < 5
    assert json.loads(r.stdout)["totals"]["human_messages"] == 10_000


def test_agent_path_from_a_clean_checkout_with_the_example_config(tmp_path):
    db = tmp_path / "c.sqlite3"
    chat.Store(str(db)).create_user("h@chat.localhost", "Human", is_bot=False)
    r = subprocess.run([sys.executable, "chat/server.py", "human-load", "--config",
                        "chat/human-load.example.json", "--db", str(db), "--since", "2026-10-01",
                        "--until", "2026-10-21", "--json"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert chat.validate_human_load(json.loads(r.stdout)) == []
