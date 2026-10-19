"""The company board (agora#174): the pinned goal, the stuck row's "wake"
button and the "updated X min ago" line.

The pin and the wake are server endpoints, over a real socket, because the
issue's agent path is "readable and settable through the server API (JSON),
not only the page": the page is one client of them, an agent is another.
The red "updated" line is the page's own helper, run in node like the rest
of the board's helpers (tests/test_chat_page.py).
"""
from __future__ import annotations

import json
import re
import threading

import pytest

from test_chat_messages import Api, chat
from test_chat_page import FAKE_DOM, _js

FOUNDER_EMAIL = "founder@chat.localhost"  # a temp-DB human, never his real account


def _serve(db: str):
    server = chat.make_server(db, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(server) -> None:
    server.RequestHandlerClass.chat.dashboard.stop.set()
    server.shutdown()
    server.server_close()


def _api(server, user: dict) -> Api:
    return Api(f"http://127.0.0.1:{server.server_address[1]}", user["email"], user["api_key"])


@pytest.fixture
def board(tmp_path):
    """A server with the founder (a human), the PM and CTO role bots, and
    each role's home room as setup_streams.py makes it (#PM upper case)."""
    db = str(tmp_path / "chat.sqlite3")
    server = _serve(db)
    store = server.RequestHandlerClass.chat.s
    founder = store.create_user(FOUNDER_EMAIL, "Founder")
    pm = store.create_user("pm-bot@chat.localhost", "PM", is_bot=True)
    cto = store.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    rooms = {}
    for name, bot in (("PM", pm), ("cto", cto), ("coo", None)):
        sid = store.create_stream(name)
        store.subscribe(founder["id"], sid)
        if bot:
            store.subscribe(bot["id"], sid)
        rooms[name] = sid
    state = {"server": server, "db": db, "store": store, "founder": founder,
             "pm": pm, "rooms": rooms}
    yield state
    _stop(state["server"])


def _messages(store) -> list[dict]:
    return [dict(r) for r in store.q("SELECT * FROM messages ORDER BY id")]


# -- DoD: the pin survives a server restart


def test_the_pin_survives_a_server_restart(board):
    api = _api(board["server"], board["founder"])
    status, body = api("POST", "dashboard/pin", goal="arena")
    assert status == 200 and body["result"] == "success", body
    _stop(board["server"])
    board["server"] = _serve(board["db"])  # same database file, new process state
    api = _api(board["server"], board["founder"])
    status, got = api("GET", "dashboard/pin")
    assert status == 200 and got["goal"] == "arena", got
    # the board's own read carries it too, so the page needs one fetch, not two
    status, dash = api("GET", "dashboard")
    assert status == 200 and dash["pin"]["goal"] == "arena", dash


# -- DoD (agent path): the pin is readable and settable through the JSON API


def test_an_agent_reads_and_sets_the_pin_through_the_json_api(board):
    bot = _api(board["server"], board["pm"])  # an agent, with its bot key
    status, got = bot("GET", "dashboard/pin")
    assert status == 200 and got["goal"] is None and got["at"] is None, got
    status, got = bot("POST", "dashboard/pin", goal="pypi")
    assert status == 200 and got["goal"] == "pypi" and isinstance(got["at"], int), got
    assert got["by"] == "PM"
    assert bot("GET", "dashboard/pin")[1]["goal"] == "pypi"
    # an empty goal clears it: "nothing pinned" is a state, not an error
    status, got = bot("POST", "dashboard/pin", goal="")
    assert status == 200 and got["goal"] is None, got
    # no `goal` at all is a mistake, and the error says what to send
    status, err = bot("POST", "dashboard/pin")
    assert status == 400 and "goal" in err["msg"], err


# -- DoD: "wake" posts one tag to the owner's home room


def test_wake_posts_exactly_one_tag_to_the_owners_home_room(board):
    api = _api(board["server"], board["founder"])
    status, body = api("POST", "dashboard/wake", owner="PM", ref="agora#12")
    assert status == 200 and body["result"] == "success", body
    assert body["posted"] is True and body["room"] == "PM", body
    msgs = _messages(board["store"])
    assert len(msgs) == 1, msgs
    [m] = msgs
    assert m["stream_id"] == board["rooms"]["PM"] and m["sender_id"] == board["founder"]["id"]
    assert m["id"] == body["id"]
    assert re.findall(r"@\*\*[^*]+\*\*", m["content"]) == ["@**PM**"], m["content"]
    assert "agora#12" in m["content"]
    # a second tap (a phone double-tap) does not tag twice, and says so
    status, again = api("POST", "dashboard/wake", owner="PM", ref="agora#12")
    assert status == 200 and again["posted"] is False and again["id"] == m["id"], again
    assert len(_messages(board["store"])) == 1


def test_wake_that_would_reach_nobody_is_refused_and_posts_nothing(board):
    api = _api(board["server"], board["founder"])
    # the founder owns his own rows: there is no room to tag him in
    status, err = api("POST", "dashboard/wake", owner="חמי", ref="bricks#63")
    assert status == 400, err
    # #coo exists but has no COO bot in it: a tag there wakes nobody (D3)
    status, err = api("POST", "dashboard/wake", owner="COO", ref="ops#1")
    assert status == 409 and "COO" in err["msg"], err
    assert _messages(board["store"]) == []


# -- Add (CMO check): "updated X min ago" at the top of the board, red after 10 min


def test_the_updated_line_turns_red_after_ten_minutes():
    got = _js(("el", "minsAgo", "agoText", "updatedLine", "renderUpdated"), """
      const last = 1700000000;
      const at = (s) => renderUpdated({last_sync: last}, (last + s) * 1000);
      at(600);
      out.ten = [$("dash-updated").textContent, $("dash-updated").classList.contains("late")];
      at(601);
      out.late = [$("dash-updated").textContent, $("dash-updated").classList.contains("late")];
      at(60);
      out.back = $("dash-updated").classList.contains("late");
      renderUpdated({last_sync: null}, (last + 1) * 1000);
      out.never = [$("dash-updated").textContent, $("dash-updated").classList.contains("late")];
    """, prelude=FAKE_DOM)
    assert got["ten"] == ["עודכן לפני 10 דק׳", False]
    assert got["late"] == ["עודכן לפני 10 דק׳", True]
    assert got["back"] is False
    # never synced is not fresh: red, in words
    assert got["never"][1] is True and got["never"][0] == "עוד לא עודכן"
