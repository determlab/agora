"""chat/server.py's Numbers tab (issue #154): GET/POST /api/v1/numbers.

The limits file is read from the data folder, a change is written as one
request file next to it, and nothing here starts a process: another program
applies the request. tests/fixtures/limits.json is the shape (the real
limits file has no other documented form), run through a real socket.
"""
from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request

import pytest

from test_chat_messages import Api, ROOT, chat

FIXTURE = ROOT / "tests" / "fixtures" / "limits.json"


@pytest.fixture
def numbers(tmp_path, monkeypatch):
    """(api, data_dir, post_json): a server whose data folder holds a copy of
    the fixture limits file. Any subprocess started by the server fails the
    test that started it."""
    def boom(*a, **k):
        raise AssertionError("the Numbers tab started a process")
    monkeypatch.setattr(chat.subprocess, "run", boom)
    monkeypatch.setattr(chat.subprocess, "Popen", boom)
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(FIXTURE, data / "limits.json")
    server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0, data_dir=str(data))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bot = server.RequestHandlerClass.chat.s.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    api = Api(base, bot["email"], bot["api_key"])

    def post_json(payload):
        req = urllib.request.Request(base + "/api/v1/numbers", data=json.dumps(payload).encode(),
                                     method="POST", headers={
                                         "Authorization": api.auth,
                                         "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield api, data, post_json
    server.shutdown()
    server.server_close()


def test_numbers_get_lists_every_number_by_group(numbers):
    api, _, _ = numbers
    status, body = api("GET", "numbers")
    assert status == 200 and [g["name"] for g in body["groups"]] == ["Intake", "Build"]
    first = body["groups"][1]["numbers"][0]
    assert first == {"key": "max_parallel_runs", "name": "How many runs go at the same time",
                     "value": 3, "unit": "runs", "min": 1, "max": 8}


def test_numbers_valid_change_writes_the_request_file_with_only_known_keys(numbers):
    _, data, post_json = numbers
    status, body = post_json({"max_parallel_runs": 5, "claim_ttl_hours": 1.5})
    assert status == 200 and body["written"] is True
    written = json.loads((data / "limits-request.json").read_text(encoding="utf-8"))
    assert body["path"] == str(data / "limits-request.json")
    assert written["changes"] == {"max_parallel_runs": 5, "claim_ttl_hours": 1.5}
    assert set(written) == {"requested_at", "changes"}


def test_numbers_form_values_from_the_tab_are_read_as_numbers(numbers):
    api, data, _ = numbers
    status, body = api("POST", "numbers", max_parallel_runs="6")
    assert status == 200 and body["written"] is True
    assert json.loads((data / "limits-request.json").read_text())["changes"] == {"max_parallel_runs": 6}


def test_numbers_unknown_key_is_refused_and_nothing_is_written(numbers):
    _, data, post_json = numbers
    status, body = post_json({"max_parallel_runs": 5, "root_password": 1})
    assert status == 400 and body["result"] == "error" and "root_password" in body["msg"]
    assert not (data / "limits-request.json").exists()


def test_numbers_out_of_range_is_refused_with_the_range(numbers):
    _, data, post_json = numbers
    status, body = post_json({"max_parallel_runs": 12})
    assert status == 400 and "max_parallel_runs" in body["msg"]
    assert "1" in body["msg"] and "8" in body["msg"] and "runs" in body["msg"]
    status, body = post_json({"max_parallel_runs": 0})
    assert status == 400 and "max_parallel_runs" in body["msg"]
    assert not (data / "limits-request.json").exists()


def test_numbers_not_a_number_and_a_fraction_for_a_whole_number_are_refused(numbers):
    _, data, post_json = numbers
    for bad in ("many", True, None, 2.5, float("inf")):
        status, body = post_json({"max_parallel_runs": bad})
        assert status == 400 and "max_parallel_runs" in body["msg"], bad
    assert not (data / "limits-request.json").exists()


def test_numbers_an_empty_request_is_refused(numbers):
    _, data, post_json = numbers
    status, body = post_json({})
    assert status == 400 and not (data / "limits-request.json").exists()


def test_numbers_does_not_say_written_when_the_file_could_not_be_written(numbers):
    _, data, post_json = numbers
    (data / "limits-request.json").mkdir()  # a folder where the file goes: the write fails
    status, body = post_json({"max_parallel_runs": 5})
    assert status >= 400 and body["result"] == "error" and "written" not in body


def test_numbers_without_a_data_folder_or_limits_file_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("AGORA_DATA_DIR", raising=False)
    server = chat.make_server(str(tmp_path / "chat.sqlite3"), port=0)
    try:
        c = server.RequestHandlerClass.chat
        bot = c.s.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        api = Api(f"http://127.0.0.1:{server.server_address[1]}", bot["email"], bot["api_key"])
        status, body = api("GET", "numbers")
        assert status == 400 and "data" in body["msg"].lower()
        status, body = api("POST", "numbers", max_parallel_runs="5")
        assert status == 400 and "written" not in body
    finally:
        server.shutdown()
        server.server_close()


def test_numbers_tab_is_in_the_page_and_calls_the_endpoint():
    text = (ROOT / "chat" / "page.html").read_text(encoding="utf-8")
    assert 'id="dash-tab-numbers"' in text and 'api("GET", "numbers")' in text
    assert 'api("POST", "numbers"' in text
