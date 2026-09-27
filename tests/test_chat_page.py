"""chat/page.html and ``GET /`` (issue #65, M2): the browser page.

The server runs in a thread on a temp DB, over a real socket. "Through the
page endpoint" means what the page's own ``fetch`` sends: the API under
``/api/v1/`` with the page's ``Origin`` and the human's HTTP Basic auth. The
human is made in the temp DB only, never the founder's account. The read-back
runs the unchanged ``bot/zulip.py read`` as a process; the wake runs the
hook's poll path, imported and never edited.

What the page does inside a browser is checked where it can be without one:
its markup (``dir="auto"``, the mention buttons, the ids its script reaches
for) and its script's text (no ``innerHTML``, the 401 path).
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import json
import re
import subprocess
import sys
import urllib.parse
from html.parser import HTMLParser

import pytest

from test_chat_events import Running
from test_chat_messages import ROOT, chat
from test_chat_setup import HUMAN, cli_env

PAGE = ROOT / "chat" / "page.html"
HOOK = ROOT / "hooks" / "agora_hook.py"
ZULIP_CLI = ROOT / "bot" / "zulip.py"
BOTS = ("CTO", "COO", "CMO", "Watchdog")


def _request(run, method: str, path: str, params: dict | None = None,
             user: dict | None = None, **headers):
    """One request with exactly these headers (``Host`` included, if given)."""
    port = run.server.server_address[1]
    body = urllib.parse.urlencode(params or {}) if params else None
    if body is not None and method == "GET":
        path, body = f"{path}?{body}", None
    hdrs = {k.replace("_", "-"): v for k, v in headers.items()}
    if user is not None:
        hdrs["Authorization"] = "Basic " + base64.b64encode(
            f"{user['email']}:{user['api_key']}".encode("utf-8")).decode("ascii")
    if body is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def page_api(run, user, method, path, **params):
    """The API as the page calls it: its own Origin, the human's Basic auth."""
    status, _, raw = _request(run, method, f"/api/v1/{path}", params, user,
                              Origin=f"http://127.0.0.1:{run.server.server_address[1]}")
    return status, json.loads(raw)


@pytest.fixture
def world(tmp_path):
    run = Running(str(tmp_path / "chat.sqlite3"))
    store = run.store
    human = chat.add_human(store, HUMAN, "Test Human")
    bots = {name: store.create_user(f"{name.lower()}-bot@chat.localhost", name, is_bot=True)
            for name in BOTS}
    feature = store.create_stream("feature", invite_only=True)
    for u in (human, bots["CTO"], bots["COO"]):
        store.subscribe(u["id"], feature)
    yield {"run": run, "human": human, "bots": bots, "tmp": tmp_path}
    run.stop()


def _hook(world, monkeypatch):
    spec = importlib.util.spec_from_file_location("agora_hook_page", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(hook, "STATE_DIR", world["tmp"])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    return hook


class Markup(HTMLParser):
    """Every element's attributes, and the inline script's text."""

    def __init__(self):
        super().__init__()
        self.elements: list[tuple[str, dict]] = []
        self.script = ""
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag):
        self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.script += data


def _markup(text: str | None = None) -> Markup:
    m = Markup()
    m.feed(text if text is not None else PAGE.read_text(encoding="utf-8"))
    return m


# -- GET /


def test_the_page_is_served_without_auth_as_html_with_dir_auto(world):
    status, headers, raw = _request(world["run"], "GET", "/")
    assert status == 200, raw
    assert headers["Content-Type"].startswith("text/html")
    m = _markup(raw.decode("utf-8"))
    bodies = [a for t, a in m.elements if "body" in (a.get("class") or "").split()]
    assert bodies and all(a.get("dir") == "auto" for a in bodies), bodies
    [compose] = [a for t, a in m.elements if t == "textarea" and a.get("id") == "compose"]
    assert compose.get("dir") == "auto"


def test_the_csp_allows_the_pages_own_inline_script_by_hash_and_nothing_else(world):
    _, headers, raw = _request(world["run"], "GET", "/")
    csp = headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'self';"), csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    # The hash a browser computes: over the script text it parsed, as UTF-8.
    script = _markup(raw.decode("utf-8")).script
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_the_csp_hash_survives_a_crlf_checkout(tmp_path):
    crlf = tmp_path / "page.html"
    crlf.write_bytes(PAGE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    served, csp = chat.load_page(str(crlf))
    assert b"\r" not in served
    script = _markup(served.decode("utf-8")).script
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    assert f"'sha256-{digest}'" in csp


def test_a_foreign_origin_gets_403_on_the_page_and_the_api(world):
    run, human = world["run"], world["human"]
    status, _, raw = _request(run, "GET", "/", Origin="http://evil.example")
    assert status == 403, raw
    assert json.loads(raw)["code"] == "FORBIDDEN"
    # Even with a valid key: the foreign page is refused and nothing is stored.
    status, _, raw = _request(run, "POST", "/api/v1/messages",
                              {"type": "stream", "to": "feature", "topic": "t",
                               "content": "from evil"}, human, Origin="http://evil.example")
    assert status == 403, raw
    assert run.store.one("SELECT 1 FROM messages WHERE content='from evil'") is None


def test_a_foreign_host_gets_403_and_the_two_local_names_are_served(world):
    run = world["run"]
    port = run.server.server_address[1]
    status, _, _ = _request(run, "GET", "/", Host=f"evil.example:{port}")
    assert status == 403
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        status, _, _ = _request(run, "GET", "/", Host=host)
        assert status == 200, host


def test_the_api_without_origin_still_works_as_the_bots_call_it(world):
    status, _, raw = _request(world["run"], "GET", "/api/v1/users/me",
                              user=world["bots"]["CTO"])
    assert status == 200 and json.loads(raw)["email"] == "cto-bot@chat.localhost"


# -- the key screen


def test_no_key_and_a_wrong_key_get_zulips_401_and_the_page_shows_the_key_screen(world):
    run = world["run"]
    origin = f"http://127.0.0.1:{run.server.server_address[1]}"
    status, _, raw = _request(run, "GET", "/api/v1/users/me", Origin=origin)
    body = json.loads(raw)
    assert status == 401 and body["result"] == "error" and body["msg"] and body["code"]
    wrong = {"email": HUMAN, "api_key": "not-the-key"}
    status, _, raw = _request(run, "GET", "/api/v1/users/me", user=wrong, Origin=origin)
    body = json.loads(raw)
    assert status == 401 and body["code"] == "INVALID_API_KEY" and HUMAN in body["msg"]
    # The page: on any 401 it shows the key screen with the server's msg.
    script = _markup().script
    assert re.search(r"resp\.status === 401\) \{\s*showKeys\(data\.msg", script), \
        "the page must show the key screen with the 401's msg"
    assert "add-human" in PAGE.read_text(encoding="utf-8")
    assert 'id="forget"' in PAGE.read_text(encoding="utf-8")
    assert "localStorage.removeItem" in script


# -- writing through the page


def test_a_hebrew_message_reads_back_unchanged_through_bot_zulip_read(world):
    run, human = world["run"], world["human"]
    text = "שלום, זה מבחן — עברית מימין לשמאל (#65)"
    status, body = page_api(run, human, "POST", "messages", type="stream",
                            to="feature", topic="#65 page", content=text)
    assert status == 200 and body["id"], body
    env = cli_env(run.base, ZULIP_ADMIN_EMAIL=human["email"],
                  ZULIP_ADMIN_API_KEY=human["api_key"])
    read = subprocess.run([sys.executable, str(ZULIP_CLI), "--as", "ADMIN", "read",
                           "--stream", "feature", "--topic", "#65 page"], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert read.returncode == 0, read.stderr
    assert text in read.stdout.splitlines(), read.stdout


def test_a_mention_of_the_cto_from_the_page_wakes_the_cto_through_the_hook(world, monkeypatch):
    run, human, cto = world["run"], world["human"], world["bots"]["CTO"]
    hook = _hook(world, monkeypatch)
    creds = (run.base, cto["email"], cto["api_key"])
    state = {"queue_id": None, "last_event_id": -1, "last_message_id": None,
             "backfill": False}
    path = world["tmp"] / "hook-cto.json"
    assert hook._poll_once(creds, "CTO", state, path) is None  # registers; idle
    status, body = page_api(run, human, "POST", "messages", type="stream",
                            to="feature", topic="ops#65", content="@**CTO** שלום")
    assert status == 200, body
    text = hook._poll_once(creds, "CTO", state, path)
    assert text and "@CTO in #feature" in text and "@**CTO** שלום" in text


def test_markup_in_a_message_is_stored_and_read_back_as_text(world):
    run, human = world["run"], world["human"]
    evil = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    status, body = page_api(run, human, "POST", "messages", type="stream",
                            to="feature", topic="xss", content=evil)
    assert status == 200, body
    status, got = page_api(run, human, "GET", "messages", anchor=body["id"],
                           num_before=0, num_after=0)
    assert status == 200 and got["messages"][0]["content"] == evil


def test_the_page_never_renders_markup_from_a_string():
    script = _markup().script
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink
    assert ".textContent = m.content" in script


def test_the_page_loads_nothing_from_elsewhere():
    text = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", text, re.I)
    assert 'src="http' not in text and 'href="http' not in text


def test_the_page_has_no_inline_handlers_or_styles_the_csp_would_block():
    for tag, attrs in _markup().elements:
        assert "style" not in attrs, tag
        assert not [a for a in attrs if a.startswith("on")], (tag, attrs)


# -- the mention buttons


def test_each_mention_button_inserts_exactly_what_wakes_that_bot(world, monkeypatch):
    names = [a["data-mention"] for _, a in _markup().elements if "data-mention" in a]
    assert names == ["COO", "CTO", "CMO", "Watchdog", "all"], names
    run, human, bots = world["run"], world["human"], world["bots"]
    # A stream that is nobody's own and not #feature: only a mention wakes here.
    sid = run.store.create_stream("mentions", invite_only=True)
    for u in (human, *bots.values()):
        run.store.subscribe(u["id"], sid)
    hook = _hook(world, monkeypatch)
    states = {}
    for name, bot in bots.items():
        states[name] = {"queue_id": None, "last_event_id": -1, "last_message_id": None,
                        "backfill": False}
        assert hook._poll_once((run.base, bot["email"], bot["api_key"]), name,
                               states[name], world["tmp"] / f"m-{name}.json") is None
    # What the button inserts, as the page's script builds it.
    assert '"@**" + btn.dataset.mention + "** "' in _markup().script
    for name in [*names, None]:
        content = f"@**{name}** ping-{name}" if name else "no mention here"
        status, body = page_api(run, human, "POST", "messages", type="stream",
                                to="mentions", topic="t", content=content)
        assert status == 200, body
    for name, bot in bots.items():
        text = hook._poll_once((run.base, bot["email"], bot["api_key"]), name,
                               states[name], world["tmp"] / f"m-{name}.json") or ""
        assert f"ping-{name}" in text and "ping-all" in text, (name, text)
        assert "no mention here" not in text
        for other in BOTS:
            if other != name:
                assert f"ping-{other}" not in text, (name, other)


# -- the seam a browser fails silently on


def test_every_id_the_script_reaches_for_is_in_the_markup():
    m = _markup()
    ids = {a["id"] for _, a in m.elements if "id" in a}
    used = set(re.findall(r'\$\("([\w-]+)"\)', m.script))
    assert used and not used - ids, used - ids


def test_every_api_call_the_page_makes_is_a_route_the_server_serves(world):
    calls = set(re.findall(r'api\("(GET|POST|DELETE)", "([\w/]+)"', _markup().script))
    assert {("POST", "register"), ("GET", "events"), ("POST", "messages")} <= calls
    for method, path in calls:
        status, body = page_api(world["run"], world["human"], method, path,
                                **({"dont_block": "true"} if path == "events" else {}))
        assert status != 404, (method, path, body)
