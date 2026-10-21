"""chat/pool_view.py and GET /api/v1/dashboard/pool (issue #196).

The fixture tests/fixtures/pool-view.json is hand-written with made-up repos."""
from __future__ import annotations

import copy
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_chat_messages import OPENER, Api, chat

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "pool-view.json"

_spec = importlib.util.spec_from_file_location("pool_view_under_test", ROOT / "chat" / "pool_view.py")
pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pv)

GEN = datetime(2026, 10, 21, 6, 35, 2, tzinfo=timezone.utc)


def _obj():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _write(tmp_path, obj):
    p = tmp_path / "view.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_fixture_loads_without_errors():
    got = pv.load(FIXTURE, GEN)
    assert got["errors"] == []
    assert got["view"]["schema"] == "pool-view/1"
    assert got["stale"] is False and got["age_s"] == 0


def test_check_names_missing_id_and_url():
    o = _obj()
    del o["stages"][1]["items"][0]["id"]
    del o["stages"][2]["items"][0]["url"]
    errs = pv.check(o)
    assert any(e.startswith("stages[1].items[0].id") for e in errs)
    assert any(e.startswith("stages[2].items[0].url") for e in errs)


def test_check_names_wrong_zone_key_and_stage_order():
    o = _obj()
    o["zones"][1]["key"] = "ci"
    o["stages"][0], o["stages"][1] = o["stages"][1], o["stages"][0]
    errs = pv.check(o)
    assert any(e.startswith("zones[1].key") for e in errs)
    assert any(e.startswith("stages[0].key") for e in errs)


def test_errors_unset_env_missing_file_bad_json_and_schema(tmp_path):
    for path in (None, "", tmp_path / "nope.json"):
        got = pv.load(path, GEN)
        assert got["view"] is None and len(got["errors"]) == 1
    assert "AGORA_POOL_VIEW" in pv.load(None, GEN)["errors"][0]
    assert str(tmp_path / "nope.json") in pv.load(tmp_path / "nope.json", GEN)["errors"][0]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    got = pv.load(bad, GEN)
    assert got["view"] is None and len(got["errors"]) == 1
    o = _obj()
    o["schema"] = "pool-view/2"
    got = pv.load(_write(tmp_path, o), GEN)
    assert got["view"] is None and len(got["errors"]) == 1
    assert "pool-view/2" in got["errors"][0] and "reads pool-view/1" in got["errors"][0]


def test_stale_after_fifteen_minutes():
    t = pv.parse_time(_obj()["generated_at"])
    fresh = pv.load(FIXTURE, t + timedelta(minutes=14))
    old = pv.load(FIXTURE, t + timedelta(minutes=16))
    assert fresh["stale"] is False and fresh["age_s"] == 14 * 60
    assert old["stale"] is True and old["age_s"] == 16 * 60


def test_extra_fields_are_ignored(tmp_path):
    o = copy.deepcopy(_obj())
    o["producer"] = "demo"
    o["stages"][0]["items"][0]["colour"] = "blue"
    o["zones"][0]["parts"][0]["extra"] = 1
    got = pv.load(_write(tmp_path, o), GEN)
    assert got["errors"] == []
    assert got["view"]["producer"] == "demo"


@pytest.fixture
def pool_endpoint(tmp_path):
    server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bot = server.RequestHandlerClass.chat.s.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield Api(base, bot["email"], bot["api_key"]), base
    server.shutdown()
    server.server_close()


def test_pool_endpoint_answers_with_key(pool_endpoint, monkeypatch):
    api, _ = pool_endpoint
    monkeypatch.setenv("AGORA_POOL_VIEW", str(FIXTURE))
    status, body = api("GET", "dashboard/pool")
    assert status == 200
    assert body["errors"] == [] and body["view"]["schema"] == "pool-view/1"
    assert body["stale"] is True and isinstance(body["age_s"], int)  # the fixture is old
    monkeypatch.delenv("AGORA_POOL_VIEW")
    status, body = api("GET", "dashboard/pool")
    assert status == 200 and body["view"] is None and len(body["errors"]) == 1


def test_pool_endpoint_without_key_is_401(pool_endpoint):
    _, base = pool_endpoint
    with pytest.raises(urllib.error.HTTPError) as e:
        OPENER.open(urllib.request.Request(base + "/api/v1/dashboard/pool"), timeout=10)
    assert e.value.code == 401
