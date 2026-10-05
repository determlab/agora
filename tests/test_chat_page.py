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
import shutil
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


def _function(name: str) -> str:
    """One top-level function of the page's script, as written (braces
    counted; the helpers run by ``_js`` keep theirs balanced)."""
    script = _markup().script
    found = re.search(rf"^(async )?function {name}\(", script, re.M)
    assert found, name
    depth, i = 0, script.index("{", script.index(")", found.end()))
    for j in range(i, len(script)):
        depth += {"{": 1, "}": -1}.get(script[j], 0)
        if depth == 0:
            return script[found.start():j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


# The stand-in for the page's api(): it records each call with the params
# encoded exactly as the real api() encodes them (strings as-is, the rest
# JSON), so a test can replay them against the real server.
API_STUB = """
const calls = [];
let FAIL = false;
async function api(method, path, params) {
  const enc = {};
  for (const [k, v] of Object.entries(params || {})) enc[k] = typeof v === "string" ? v : JSON.stringify(v);
  calls.push([method, path, enc]);
  if (FAIL) throw new Error("network down");
  return {};
}
"""


def _js(names: tuple[str, ...], body: str, prelude: str = ""):
    """Run the page's own functions ``names`` in node and return what ``body``
    puts in ``out``. CI guarantees Python only, so no node means a skip."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed: the page's helpers cannot run here")
    src = "\n".join([prelude, *(_function(n) for n in names),
                     "(async () => { const out = {};", body,
                     "console.log(JSON.stringify(out)); })()"
                     ".catch((e) => { console.error(e); process.exit(1); });"])
    run = subprocess.run([node, "-"], input=src, capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


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
    # ops#249 batch 1: a service worker is registered from this page, so its
    # script fetch needs its own allowance — worker-src, not script-src,
    # governs it, and script-src's hash-only allowance would otherwise block it.
    assert "worker-src 'self';" in csp


# -- ops#249 batch 1: the PWA shell (manifest, service worker, icons) and
# the phone-notification wiring that needs them.


def test_the_pwa_shell_is_served_and_linked_from_the_page(world):
    status, headers, raw = _request(world["run"], "GET", "/manifest.json")
    assert status == 200 and headers["Content-Type"] == "application/manifest+json"
    manifest = json.loads(raw)
    assert manifest["name"] and manifest["display"] == "standalone"
    icon_paths = {i["src"] for i in manifest["icons"]}
    assert icon_paths == {"/icon-192.png", "/icon-512.png"}
    for path in icon_paths:
        status, headers, raw = _request(world["run"], "GET", path)
        assert status == 200 and headers["Content-Type"] == "image/png"
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    status, headers, raw = _request(world["run"], "GET", "/sw.js")
    assert status == 200 and "javascript" in headers["Content-Type"]
    assert b"showNotification" not in raw  # the SW relays clicks; the page shows notifications
    assert b"self.addEventListener(\"notificationclick\"" in raw
    text = PAGE.read_text(encoding="utf-8")
    assert '<link rel="manifest" href="/manifest.json">' in text
    assert 'navigator.serviceWorker.register("/sw.js")' in _markup().script


def test_a_foreign_host_cannot_fetch_the_pwa_shell_either(world):
    # Same Host-rebinding protection as the page itself (D4's spirit): a
    # static asset still answers only this server's own two local names or
    # an --allow-host name, never any other Host header.
    run = world["run"]
    port = run.server.server_address[1]
    status, _, _ = _request(run, "GET", "/manifest.json", Host=f"evil.example:{port}")
    assert status == 403
    status, _, _ = _request(run, "GET", "/sw.js", Host=f"127.0.0.1:{port}")
    assert status == 200


def test_notification_wiring_is_stronger_for_a_mention_than_a_plain_message():
    src = _function("notifyMessage")
    assert "if (S.me && m.sender_id === S.me.user_id) return;" in src
    # The split this issue asks for: "mentioned" (the founder tagged) is
    # requireInteraction + renotify + a longer vibration; a plain message is
    # none of those.
    assert 'const strong = (m.flags || []).includes("mentioned");' in src
    assert "requireInteraction: strong" in src and "renotify: strong" in src
    assert "vibrate: strong ? [200, 100, 200, 100, 200] : [80]" in src
    assert "notifyMessage(m, isOpen);" in _function("onMessage")


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
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                 "createContextualFragment", "DOMParser", "srcdoc"):
        assert sink not in script, sink
    # Message text, the quote inside it and its code all end in textContent.
    assert "fillBody(body, m.content)" in script and "fillBody(body, q.rest)" in script
    assert "n.textContent = part.replace(" in _function("fillBody")
    assert "el.append(...inlineNodes(" in _function("fillBody")
    assert "n.textContent = p.text" in _function("inlineNodes")
    assert "qt.textContent = q.quote" in script
    assert '.textContent = q.sender' in script and '.textContent = m.sender_full_name' in script


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
    assert names == ["COO", "CTO", "CMO", "CPSO", "Watchdog", "all"], names
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
    # The negative lookahead drops a call built by string concatenation
    # (e.g. "messages/" + m.id + "/reactions"): the literal before the "+"
    # is only a prefix, and checking it against the server would either
    # report a false 404 or, worse, pass by the accident of path.strip("/")
    # turning "streams/" into "streams" — a coverage claim this static scan
    # cannot actually back up either way (D3's shape, one layer up).
    calls = set(re.findall(r'api\("(GET|POST|DELETE)", "([\w/]+)"(?!\s*\+)', _markup().script))
    assert {("POST", "register"), ("GET", "events"), ("POST", "messages")} <= calls
    for method, path in calls:
        status, body = page_api(world["run"], world["human"], method, path,
                                **({"dont_block": "true"} if path == "events" else {}))
        assert status != 404, (method, path, body)


# -- issue #71: direction, quote, reconnect, the stream dialogs


def test_the_whole_script_parses():
    # A syntax error anywhere leaves a blank page and no test would notice.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed: the page's script cannot be parsed here")
    run = subprocess.run([node, "--check", "-"], input=_markup().script, capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr


def test_the_direction_helper_skips_a_leading_mention():
    script = _markup().script
    # Where it is used: every message body, a quote, and the compose box.
    assert "el.dir = textDir(text)" in script and "qt.dir = textDir(q.quote)" in script
    assert "box.dir = box.value.trim() ? textDir(box.value)" in script
    cases = {"@**CTO** שלום": "rtl", "@**CTO** hello": "ltr", "שלום": "rtl", "hello": "ltr",
             "@**CTO** @**all** שלום": "rtl", "  @**CTO**שלום": "rtl", "12 שלום": "rtl",
             "@**CTO** 12 hello שלום": "ltr", "": "ltr", "@_**CTO** said:": "ltr"}
    out = _js(("textDir",), f"const cases = {json.dumps(list(cases))};"
                            "out.dirs = cases.map(textDir);")
    assert dict(zip(cases, out["dirs"])) == cases


def test_the_quote_helper_splits_sender_quote_and_reply():
    got = _js(("parseQuote", "quoteBlock"), """
      out.given = parseQuote("@_**CTO** said:\\n```quote\\nhi\\n```\\nok");
      out.zulip = parseQuote("@_**CTO|7** [said](#narrow/near/3):\\n```quote\\nשלום\\nשני\\n```\\n");
      out.round = parseQuote(quoteBlock("COO", "a\\nb") + "reply");
      out.withId = parseQuote(quoteBlock("COO", "a\\nb", 42) + "reply");
      out.plain = parseQuote("hi ```quote\\nx\\n```");
    """)
    assert got["given"] == {"sender": "CTO", "id": None, "quote": "hi", "rest": "ok"}
    # "#narrow/near/3" is Zulip's own real link, not this page's "#near/ID"
    # marker (ops#249): it still parses as a quote, just with no jump id.
    assert got["zulip"] == {"sender": "CTO", "id": None, "quote": "שלום\nשני", "rest": ""}
    assert got["round"] == {"sender": "COO", "id": None, "quote": "a\nb", "rest": "reply"}
    assert got["withId"] == {"sender": "COO", "id": 42, "quote": "a\nb", "rest": "reply"}
    assert got["plain"] is None


def test_a_quote_does_not_wake_the_person_quoted(world):
    run, human = world["run"], world["human"]
    # What quoteBlock() writes: the silent @_** form, which is not a mention.
    src = _function("quoteBlock")
    assert '"@_**" + name + "**" + (id ? " [said](#near/" + id + ")" : "") + ":\\n```quote\\n"' in src
    text = "@_**CTO** said:\n```quote\nhi\n```\nok"
    status, body = page_api(run, human, "POST", "messages", type="stream",
                            to="feature", topic="quote", content=text)
    assert status == 200, body
    status, got = page_api(run, world["bots"]["CTO"], "GET", "messages",
                           anchor=body["id"], num_before=0, num_after=0)
    assert got["messages"][0]["content"] == text
    assert "mentioned" not in got["messages"][0]["flags"]


def test_a_reconnect_deletes_the_old_queue_before_registering_again():
    script = _markup().script
    poll = _function("poll")
    assert "await reconnect(gen)" in poll and "await connect(gen)" not in poll
    assert re.search(r"await dropQueue\(\);\s*await connect\(gen\);", _function("reconnect"))
    assert 'api("DELETE", "events", {queue_id: old})' in _function("dropQueue")
    assert 'enc.set(k, typeof v === "string" ? v : JSON.stringify(v))' in script
    stub = API_STUB + """
      const S = {queue: null};
      async function connect(gen) { calls.push(["connect", gen]); S.queue = "q2"; }
    """
    got = _js(("dropQueue", "reconnect"), """
      S.queue = "q1"; await reconnect(1); out.ok = calls.splice(0);
      S.queue = "q2"; FAIL = true; await reconnect(2); out.failed = calls.splice(0);
      S.queue = null; FAIL = false; await reconnect(3); out.none = calls.splice(0);
    """, prelude=stub)
    assert got["ok"] == [["DELETE", "events", {"queue_id": "q1"}], ["connect", 1]]
    # The old queue being gone (or the network) does not stop the reconnect.
    assert got["failed"] == [["DELETE", "events", {"queue_id": "q2"}], ["connect", 2]]
    assert got["none"] == [["connect", 3]]


def test_the_queue_delete_the_page_sends_removes_the_queue(world):
    run, human = world["run"], world["human"]
    status, reg = page_api(run, human, "POST", "register", event_types='["message"]')
    assert status == 200, reg
    status, body = page_api(run, human, "DELETE", "events", queue_id=reg["queue_id"])
    assert status == 200, body
    status, body = page_api(run, human, "GET", "events", queue_id=reg["queue_id"],
                            last_event_id=-1, dont_block="true")
    assert body["code"] == "BAD_EVENT_QUEUE_ID", body


def test_the_stream_dialogs_call_subscriptions_with_principals():
    script = _markup().script
    assert re.search(r'api\("POST", "users/me/subscriptions",\s*\{subscriptions: \[\{name: name\}\], '
                     r'invite_only: isPrivate, principals: emails\}', _function("createStream"))
    assert re.search(r'api\("POST", "users/me/subscriptions",\s*\{subscriptions: \[\{name: stream\}\], '
                     r'principals: \[email\]\}', _function("addMember"))
    assert re.search(r'api\("DELETE", "users/me/subscriptions", \{subscriptions: \[stream\], '
                     r'principals: \[email\]\}', _function("removeMember"))
    assert "createStream(name, " in script and "addMember(S.panel.name, email)" in script
    assert "removeMember(s.name, u.email)" in script
    # The creator is always a member, or the new stream would vanish from the page.
    assert "const emails = [S.me.email, " in script


def test_create_add_remove_and_private_reach_the_server_as_the_page_sends_them(world):
    run, human, bots = world["run"], world["human"], world["bots"]
    cto, coo = bots["CTO"]["email"], bots["COO"]["email"]
    got = _js(("createStream", "addMember", "removeMember", "setPrivate"), f"""
      await createStream("tmp", true, {json.dumps([human["email"], cto])});
      await addMember("tmp", {json.dumps(coo)});
      await removeMember("tmp", {json.dumps(cto)});
      await setPrivate(0, false);
      out.calls = calls;
    """, prelude=API_STUB)
    create, add, remove, private = got["calls"]
    assert create[:2] == ["POST", "users/me/subscriptions"]
    assert json.loads(create[2]["principals"]) == [human["email"], cto]
    assert add[:2] == ["POST", "users/me/subscriptions"]
    assert json.loads(add[2]["principals"]) == [coo]
    assert remove[:2] == ["DELETE", "users/me/subscriptions"]
    assert json.loads(remove[2]["principals"]) == [cto]
    assert private == ["PATCH", "streams/0", {"is_private": "false"}]

    def cto_send():
        return page_api(run, bots["CTO"], "POST", "messages", type="stream", to="tmp",
                        topic="t", content="from the CTO")

    status, body = page_api(run, human, create[0], create[1], **create[2])
    assert status == 200 and set(body["subscribed"]) == {human["email"], cto}, body
    status, subs = page_api(run, human, "GET", "users/me/subscriptions")
    [tmp] = [s for s in subs["subscriptions"] if s["name"] == "tmp"]
    assert tmp["invite_only"] is True
    assert cto_send()[0] == 200
    status, body = page_api(run, human, add[0], add[1], **add[2])
    assert status == 200 and body["subscribed"] == {coo: ["tmp"]}, body
    status, body = page_api(run, human, remove[0], remove[1], **remove[2])
    assert status == 200 and body["removed"] == ["tmp"], body
    status, body = cto_send()
    assert status == 400 and "tmp" in body["msg"], body
    status, members = page_api(run, human, "GET", f"streams/{tmp['stream_id']}/members")
    assert set(members["subscribers"]) == {human["id"], bots["COO"]["id"]}
    status, body = page_api(run, human, "PATCH", f"streams/{tmp['stream_id']}", **private[2])
    assert status == 200, body
    status, subs = page_api(run, human, "GET", "users/me/subscriptions")
    assert [s["invite_only"] for s in subs["subscriptions"] if s["name"] == "tmp"] == [False]


def test_the_private_toggle_never_reports_a_state_it_did_not_read_back():
    script = _markup().script
    assert 'report("members-status", privateReport(S.panel.name, want, now))' in script
    got = _js(("privacy", "privateReport"), """
      out.read = privateReport("x", false, {stream_id: 1, name: "x", invite_only: true});
      out.gone = privateReport("x", true, undefined);
      out.goneOff = privateReport("x", false, undefined);
    """)
    # The read-back wins over what was asked for.
    assert got["read"] == "#x עכשיו פרטי"
    # Not listed any more (you removed yourself): no "is now", only what was accepted.
    assert "עכשיו" not in got["gone"] and "עכשיו" not in got["goneOff"]
    assert got["gone"].startswith("#x הוגדר פרטי (השרת קיבל; הדף לא יכול לקרוא את זה בחזרה")
    assert got["goneOff"].startswith("#x הוגדר ציבורי (")


def test_create_reports_the_privacy_the_server_holds_not_the_checkbox(world):
    run, human, bots = world["run"], world["human"], world["bots"]
    cto = bots["CTO"]["email"]
    script = _markup().script
    assert 'report("create-status", createReport(name, wantPrivate, r, s))' in script
    # The stream already exists as public and the human is not a member.
    status, body = page_api(run, human, "POST", "users/me/subscriptions",
                            subscriptions=json.dumps([{"name": "old"}]), principals=json.dumps([cto]))
    assert status == 200 and list(body["subscribed"]) == [cto], body
    status, r = page_api(run, human, "POST", "users/me/subscriptions",
                         subscriptions=json.dumps([{"name": "old"}]), invite_only="true",
                         principals=json.dumps([human["email"], cto]))
    assert status == 200, r
    status, subs = page_api(run, human, "GET", "users/me/subscriptions")
    [old] = [s for s in subs["subscriptions"] if s["name"] == "old"]
    assert old["invite_only"] is False
    got = _js(("privacy", "createReport"), f"""
      const r = {json.dumps(r)}, s = {json.dumps(old)};
      out.existed = createReport("old", true, r, s);
      out.asked = createReport("old", false, r, s);
      out.fresh = createReport("new", true, {{subscribed: {{"a@x": ["new"]}}, already_subscribed: {{}}}},
                               {{name: "new", invite_only: true}});
      out.unread = createReport("new", true, {{subscribed: {{"a@x": ["new"]}}, already_subscribed: {{}}}});
    """)
    assert got["existed"].startswith("#old (ציבורי): נוספו 1, 1 כבר חברים")
    assert got["existed"].endswith("הוא כבר היה קיים כציבורי, לכן פרטי לא הוחל")
    assert got["asked"] == "#old (ציבורי): נוספו 1, 1 כבר חברים (הערוץ היה קיים)"
    assert got["fresh"] == "#new (פרטי): נוספו 1"
    assert got["unread"] == "#new: נוספו 1; הדף לא הצליח לקרוא בחזרה אם הוא פרטי"


def test_79_rename_form_prefills_the_name_and_refuses_empty_before_the_server():
    text = PAGE.read_text(encoding="utf-8")
    assert 'id="members-rename"' in text and 'for="members-name"' in text
    script = _markup().script
    assert '$("members-name").value = s.name' in _function("openMembers")
    # The rename call, as the page sends it: PATCH with new_name, nothing else.
    assert 'api("PATCH", "streams/" + streamId, {new_name: newName})' in _function("renameStream")
    handler = script[script.index('$("members-rename").addEventListener'):
                     script.index('$("members-private").addEventListener')]
    # Refused client-side, never reaching renameStream(): the server's own
    # rule for new_name (`if p.get("new_name")`) silently ignores an empty
    # string, which would report success for a rename that did not happen.
    assert handler.index('if (!want)') < handler.index('renameStream(')
    assert 'report("members-status", "צריך שם לערוץ.", true);\n    return;' in handler
    # The read-back wins, exactly like the private toggle (D3's shape).
    assert 'report("members-status", renameReport(oldName, want, now))' in handler


def test_79_the_rename_read_back_never_claims_a_name_it_did_not_confirm():
    got = _js(("renameReport",), """
      out.read = renameReport("old", "new", {stream_id: 1, name: "new"});
      out.mismatch = renameReport("old", "new", {stream_id: 1, name: "old"});
      out.gone = renameReport("old", "new", undefined);
    """)
    assert got["read"] == "#old שונה ל-#new"
    # What the server now holds wins over what was typed.
    assert got["mismatch"] == "#old שונה ל-#old"
    assert got["gone"].startswith("#old: השרת קיבל בקשה לשם #new (הדף לא יכול לקרוא את זה בחזרה")


def test_79_rename_reaches_the_server_through_the_pages_own_call_and_the_sidebar_label_follows(world):
    run, human = world["run"], world["human"]
    status, subs = page_api(run, human, "GET", "users/me/subscriptions")
    [feature] = [s for s in subs["subscriptions"] if s["name"] == "feature"]
    # What the page's own renameStream() sends, exactly as it is wired to the
    # gear panel's form (see the previous test).
    got = _js(("renameStream",), f"""
      await renameStream({feature["stream_id"]}, "roadmap");
      out.calls = calls;
    """, prelude=API_STUB)
    [call] = got["calls"]
    assert call == ["PATCH", f"streams/{feature['stream_id']}", {"new_name": "roadmap"}]
    # Replayed against the real server, the way the page's fetch would send it.
    status, body = page_api(run, human, call[0], call[1], **call[2])
    assert status == 200, body
    status, subs = page_api(run, human, "GET", "users/me/subscriptions")
    renamed = {s["stream_id"]: s["name"] for s in subs["subscriptions"]}
    assert renamed[feature["stream_id"]] == "roadmap"
    # The sidebar label is exactly this render function reading exactly this
    # (now server-confirmed) name.
    prelude = FAKE_DOM + f"""
      const CARET = "c", GEAR = "g", SVG = "http://www.w3.org/2000/svg";
      const S = {{streams: [{{stream_id: {feature["stream_id"]}, name: "roadmap", invite_only: true}}],
                 topics: new Map(), collapsed: new Set(), open: null, adding: null}};
    """
    got = _js(("svgIcon", "renderStreams", "newTopicItem"), """
      renderStreams();
      out.label = $("streams").querySelector(".stream-label").textContent;
    """, prelude=prelude)
    assert got["label"] == "#roadmap"


def test_dialogs_are_in_the_page_and_there_is_no_stream_delete_control():
    text = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"\b(confirm|prompt|alert)\(", _markup().script)
    assert "showModal()" in text and "<dialog" in text
    # No control that reaches nothing: archiving a stream is PATCH is_archived
    # (see setStreamArchived), never Zulip's generic bulk-flags endpoint.
    assert 'api("DELETE", "streams' not in text and "messages/flags" not in text


def test_the_theme_follows_the_system_and_a_stored_choice():
    m = _markup()
    text = PAGE.read_text(encoding="utf-8")
    assert "@media (prefers-color-scheme: dark)" in text and ':root[data-theme="dark"]' in text
    assert "localStorage.setItem(THEME, next)" in m.script
    assert "applyTheme(localStorage.getItem(THEME))" in m.script
    # One style block and one script, so the CSP hashes cover all of it.
    assert text.count("<style>") == 1 and text.count("<script>") == 1
    assert "@media (max-width: 700px)" in text


# -- issue #77: the page as the founder's mockup draws it

# A small stand-in for the DOM, enough for the page's own render functions to
# run in node: elements, text nodes, classList, the selectors the page uses
# ("tag", ".cls", "tag.cls" and descendants of those) and a textarea's
# setRangeText. textContent always makes a text node, as a browser's does.
FAKE_DOM = r"""
class Text_ {
  constructor(t) { this.data = String(t); this.parent = null; }
  get textContent() { return this.data; }
}
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.childNodes = []; this.className = "";
    this.dir = ""; this.hidden = false; this.attrs = {}; this.on = {}; this.dataset = {};
    this.style = {}; this.value = ""; this.parent = null; this.type = ""; this.title = "";
    this.selectionStart = 0; this.selectionEnd = 0; this.disabled = false;
  }
  get classList() {
    const el = this;
    const get = () => el.className.split(" ").filter(Boolean);
    return {
      add: (...c) => { el.className = [...new Set([...get(), ...c])].join(" "); },
      remove: (...c) => { el.className = get().filter((x) => !c.includes(x)).join(" "); },
      contains: (c) => get().includes(c),
      toggle: (c, on) => {
        const want = on === undefined ? !get().includes(c) : !!on;
        if (want) el.classList.add(c); else el.classList.remove(c);
        return want;
      },
    };
  }
  get children() { return this.childNodes.filter((c) => c instanceof El); }
  append(...ns) {
    for (const n of ns) {
      const x = typeof n === "string" ? new Text_(n) : n;
      if (x.parent) x.parent.childNodes = x.parent.childNodes.filter((c) => c !== x);
      x.parent = this;
      this.childNodes.push(x);
    }
  }
  remove() {
    if (this.parent) this.parent.childNodes = this.parent.childNodes.filter((c) => c !== this);
    this.parent = null;
  }
  replaceChildren(...ns) { this.childNodes = []; this.append(...ns); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(t) { this.childNodes = []; this.append(new Text_(t)); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); }
  addEventListener(k, f) { this.on[k] = f; }
  focus() { document.activeElement = this; }
  setRangeText(text, start, end, mode) {
    this.value = this.value.slice(0, start) + text + this.value.slice(end);
    if (mode === "end") this.selectionStart = this.selectionEnd = start + text.length;
  }
  matches(sel) {
    const m = /^([a-z]*)((?:\.[\w-]+)*)$/i.exec(sel);
    if (!m) throw new Error("selector " + sel);
    if (m[1] && m[1].toUpperCase() !== this.tagName) return false;
    return m[2].split(".").filter(Boolean).every((c) => this.classList.contains(c));
  }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/), out = [], root = this;
    const up = (el, i) => {
      for (let e = el.parent; e && e !== root; e = e.parent) {
        if (e.matches(parts[i])) return i === 0 || up(e, i - 1);
      }
      return false;
    };
    const walk = (el) => {
      for (const c of el.children) {
        if (c.matches(parts[parts.length - 1]) && (parts.length === 1 || up(c, parts.length - 2))) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    for (let e = this; e instanceof El; e = e.parent) if (e.matches(sel)) return e;
    return null;
  }
}
const MENTION_BTNS = ["COO", "CTO", "CMO", "Watchdog", "all"].map((n) => ({dataset: {mention: n}}));
const document = {
  activeElement: null,
  createElement: (t) => new El(t),
  createElementNS: (ns, t) => { const e = new El(t); e.ns = ns; return e; },
  createTextNode: (t) => new Text_(t),
  querySelectorAll: () => MENTION_BTNS,
};
const els = {};
const $ = (id) => els[id] || (els[id] = new El(id === "compose" ? "textarea" : "div"));
const toasts = [];
function toast(text, bad) { toasts.push([text, !!bad]); }
const dump = (n) => n instanceof El
  ? {tag: n.tagName.toLowerCase(), cls: n.className, dir: n.dir, text: n.textContent,
     href: n.href, kids: n.childNodes.map(dump)}
  : {text: n.data};
"""


def _css() -> str:
    return re.search(r"<style>(.*?)</style>", PAGE.read_text(encoding="utf-8"), re.S).group(1)


def _rule(selector: str, css: str | None = None) -> str:
    """The declarations of the first rule written with exactly ``selector``."""
    found = re.search(r"(?:^|[}\n])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css or _css())
    assert found, selector
    return found.group(1)


def _token(block: str, name: str) -> str:
    return re.search(rf"{name}:\s*(#[0-9a-f]{{6}})", block).group(1)


def _rgb(hex_: str) -> tuple[int, int, int]:
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


class Text(HTMLParser):
    """The page's visible text, outside script, style, code and pre."""

    SKIP = {"script", "style", "code", "pre"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        self._skip += tag in self.SKIP

    def handle_endtag(self, tag):
        self._skip -= tag in self.SKIP

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.chunks.append(data.strip())


def test_77_the_palette_is_teal_on_green_grey_in_light_and_dark():
    css = _css()
    light = _rule(":root", css)
    assert _token(light, "--accent") == "#1f6f78" and _token(light, "--bg") == "#eef1ed"
    assert "#2f6fed" not in css and "#f4f5f7" not in css
    dark = _rule(':root[data-theme="dark"]', css)
    system = _rule(':root:not([data-theme="light"])', css)
    # The system's dark and the chosen dark are one palette, written twice.
    assert dark.split() == system.split()
    for block in (light, dark):
        r, g, b = _rgb(_token(block, "--accent"))
        assert g > r and b > r, block          # teal: green and blue over red
        r, g, b = _rgb(_token(block, "--bg"))
        assert g > r and g >= b, block         # green-grey
    assert _token(dark, "--accent") != _token(light, "--accent")


def test_77_every_sender_gets_a_coloured_square_by_role():
    got = _js(("roleOf", "initial", "avatar"), """
      out.roles = [["CTO", true], ["coo", true], ["CMO", true], ["CPSO", true], ["Watchdog", true],
                   ["Deploy bot", true], ["Hemi", false], ["cto", false], ["", false]]
        .map(([n, b]) => roleOf(n, b));
      out.again = roleOf("CTO", true) === roleOf(" CTO ", true);
      out.initials = ["hemi", "שרה", "  CTO", ""].map(initial);
      const a = avatar("Watchdog", true, true);
      out.av = [a.tagName, a.className, a.textContent];
    """, prelude=FAKE_DOM)
    assert got["roles"] == ["cto", "coo", "cmo", "cpso", "watchdog", "bot", "human", "cto", "human"]
    assert got["again"] is True
    assert got["initials"] == ["H", "ש", "C", "?"]
    assert got["av"] == ["SPAN", "avatar role-watchdog small", "W"]
    css = _css()
    for role in {*got["roles"]}:
        assert re.search(rf"\.role-{role} \{{ background: #[0-9a-f]{{6}}; \}}", css), role
    # A square, not a circle; and each message's avatar comes from roleOf.
    assert "border-radius: 8px" in _rule(".avatar", css)
    node = _function("messageNode")
    assert 'av.classList.add("role-" + roleOf(m.sender_full_name, u ? u.is_bot : false))' in node
    assert "av.textContent = initial(m.sender_full_name)" in node


def test_77_your_own_group_is_mirrored_whole():
    css = _css()
    assert "flex-direction: row-reverse" in _rule(".msg.mine", css)
    assert "flex-direction: row-reverse" in _rule(".msg.mine .head", css)
    assert "flex-direction: row-reverse" in _rule(".msg.mine .line", css)
    assert "align-items: flex-end" in _rule(".msg.mine .stack", css)
    # The tail corner is on the avatar's side: left for others, right for you.
    assert "border-top-left-radius: 4px" in _rule(".msg.first .bubble", css)
    assert "border-top-right-radius: 4px" in _rule(".msg.mine.first .bubble", css)
    # The avatar is the group's first child, so row-reverse moves it too.
    tpl = re.search(r'<template id="msg-tpl">(.*?)</template>', PAGE.read_text(encoding="utf-8"), re.S).group(1)
    assert tpl.index('class="avatar"') < tpl.index('class="stack"') < tpl.index('class="bubble"')


def test_77_the_ui_speaks_hebrew_right_to_left_and_keeps_technical_terms_english():
    m = _markup()
    [html] = [a for t, a in m.elements if t == "html"]
    assert html == {"lang": "he", "dir": "rtl"}
    p = Text()
    p.feed(PAGE.read_text(encoding="utf-8"))
    technical = {"agora", "Email", "email", "API", "key", "api_key", "localStorage", "HTTP",
                 "Basic", "auth", "Enter", "Shift", "COO", "CTO", "CMO", "CPSO", "Watchdog", "all"}
    for chunk in p.chunks:
        latin = set(re.findall(r"[A-Za-z_]+", chunk)) - technical
        assert not latin, (chunk, latin)
    text = " ".join(p.chunks)
    for he in ("שלח", "חברים", "בחרו נושא", "ציטוט", "הזכר:", "ערוצים", "שכח מפתח"):
        assert he in text, he
    # The labels the script writes are Hebrew too.
    for he in ('"+ נושא חדש"', '"פרטי"', '"בוט" : "אדם"', '"נשלח · id "', '"מחובר"'):
        assert he in m.script, he
    for en in ('"Pick a topic"', '"Members"', '"quote"', '"remove"', '"sent, id "', '"Forget key"'):
        assert en not in m.script, en


def test_77_members_is_a_side_panel_from_the_right_with_the_api_it_calls(world):
    m = _markup()
    [panel] = [(t, a) for t, a in m.elements if a.get("id") == "members-panel"]
    assert panel[0] == "aside" and "drawer" in panel[1]["class"].split()
    assert 'id="members-dlg"' not in PAGE.read_text(encoding="utf-8")
    css = _css()
    drawer = _rule(".drawer", css)
    assert "right: 0" in drawer and "translateX(100%)" in drawer and "transition: transform" in drawer
    assert "transform: none" in _rule(".drawer.open", css)
    [(_, box)] = [(t, a) for t, a in m.elements if a.get("id") == "members-api"]
    assert box.get("dir") == "ltr"
    lm = _function("listMembers")
    # The box shows the very path the list is then read from.
    assert 'const path = "streams/" + s.stream_id + "/members"' in lm
    assert '$("members-api").textContent = "GET /api/v1/users\\nGET /api/v1/" + path' in lm
    assert 'await api("GET", path)' in lm and 'api("GET", "users")' in _function("loadUsers")
    assert 'tag.textContent = u.is_bot ? "בוט" : "אדם"' in lm
    assert "li.append(avatar(u.full_name, u.is_bot), name, tag, rm)" in lm
    # And what the box names is what the server answers.
    run, human = world["run"], world["human"]
    status, subs = page_api(run, human, "GET", "users/me/subscriptions")
    [feature] = [s for s in subs["subscriptions"] if s["name"] == "feature"]
    for path in ("users", f"streams/{feature['stream_id']}/members"):
        status, body = page_api(run, human, "GET", path)
        assert status == 200, (path, body)


def test_77_the_members_button_shows_the_count_the_server_read(world):
    got = _js(("membersLabel",), "out.l = [membersLabel(3), membersLabel(0), membersLabel(null), membersLabel(undefined)];")
    assert got["l"] == ["חברים · 3", "חברים · 0", "חברים", "חברים"]
    rc = _function("refreshCount")
    assert '(await api("GET", "streams/" + streamId + "/members")).subscribers.length' in rc
    assert "catch (e) { n = null; }" in rc
    assert "refreshCount(streamId)" in _function("openTopic")
    run, human = world["run"], world["human"]
    status, subs = page_api(run, human, "GET", "users/me/subscriptions")
    [feature] = [s for s in subs["subscriptions"] if s["name"] == "feature"]
    status, body = page_api(run, human, "GET", f"streams/{feature['stream_id']}/members")
    assert status == 200 and len(body["subscribers"]) == 3, body


def test_77_new_topic_lives_under_each_stream_with_caret_private_tag_and_gear():
    assert 'id="newtopic"' not in PAGE.read_text(encoding="utf-8")
    prelude = FAKE_DOM + """
      const CARET = "c", GEAR = "g", SVG = "http://www.w3.org/2000/svg";
      const opened = [], members = [];
      function openTopic(...a) { opened.push(a); }
      function openMembers(s) { members.push(s.name); }
      const S = {streams: [{stream_id: 1, name: "feature", invite_only: true},
                           {stream_id: 2, name: "open", invite_only: false}],
                 topics: new Map([["feature", new Map([["a", 5], ["b", 9]])]]),
                 collapsed: new Set([2]), open: null, adding: null};
    """
    got = _js(("svgIcon", "renderStreams", "newTopicItem"), """
      renderStreams();
      const list = $("streams");
      const rows = list.querySelectorAll(".stream-row");
      out.rows = rows.map((r) => [r.className, r.querySelector(".stream-label").textContent,
                                  r.querySelectorAll(".tag").map((t) => t.textContent),
                                  r.querySelector("svg.caret") !== null,
                                  r.querySelector("button.gear svg") !== null]);
      out.svgns = list.querySelector("svg.caret").ns;
      out.topics = list.querySelectorAll(".topic").map((t) => t.textContent);
      out.adders = list.querySelectorAll("button.new-topic").map((b) => b.textContent);
      list.querySelectorAll("button.gear")[1].on.click();
      list.querySelectorAll("button.new-topic")[0].on.click();
      const input = $("streams").querySelector(".new-topic-form input");
      out.focused = document.activeElement === input;
      input.value = "  ";
      $("streams").querySelector("form.new-topic-form").on.submit({preventDefault() {}});
      out.empty = [opened.length, toasts.splice(0)];
      input.value = " ops#77 ";
      $("streams").querySelector("form.new-topic-form").on.submit({preventDefault() {}});
      out.opened = opened; out.members = members; out.toasts = toasts; out.adding = S.adding;
    """, prelude=prelude)
    assert got["rows"] == [["stream-row", "#feature", ["פרטי"], True, True],
                           ["stream-row folded", "#open", [], True, True]]
    assert got["svgns"] == "http://www.w3.org/2000/svg"
    assert got["topics"] == ["b", "a"]
    assert got["adders"] == ["+ נושא חדש", "+ נושא חדש"]
    assert got["members"] == ["open"] and got["focused"] is True
    assert got["empty"][0] == 0 and got["empty"][1][0][1] is True
    assert got["opened"] == [[1, "feature", "ops#77"]] and got["adding"] is None
    # Nothing exists on the server until the first message: the toast says so.
    [(msg, bad)] = got["toasts"]
    assert not bad and "ops#77" in msg and "עם ההודעה הראשונה" in msg
    css = _css()
    assert "transform: rotate(90deg)" in _rule(".stream-row.folded .caret", css)
    assert "transition: transform" in _rule(".caret", css)
    gear = _rule(".gear", css)
    assert "opacity: 0" in gear and "transition: opacity" in gear
    assert "opacity: 1" in _rule(".stream-row:hover .gear, .stream-row.open .gear, .gear:focus", css)
    assert "⚙" not in PAGE.read_text(encoding="utf-8") and "▸" not in PAGE.read_text(encoding="utf-8")


def test_77_the_sidebar_top_has_the_logo_your_name_and_the_forget_key_button():
    m = _markup()
    ids = [a.get("id") for _, a in m.elements]
    assert ids.index("logo") < ids.index("me-row") < ids.index("who") < ids.index("forget") < ids.index("streams")
    st = _function("start")
    assert '$("who").textContent = S.me.full_name' in st and '$("who").title = S.me.email' in st
    # Forgetting the key still works: it drops the stored credentials.
    assert 'localStorage.removeItem(STORE)' in m.script and '$("forget").addEventListener' in m.script


def test_77_inline_code_and_mentions_render_as_elements_never_as_markup():
    got = _js(("inlineParts",), r"""
      out.parts = inlineParts("see `a<b>` and @**CTO** or @_**COO|7** <img src=x onerror=alert(1)>");
      out.none = [inlineParts("no marks `` @**"), inlineParts("`a\nb`")];
    """)
    assert got["parts"] == [
        {"kind": "text", "text": "see "}, {"kind": "code", "text": "a<b>"},
        {"kind": "text", "text": " and "}, {"kind": "mention", "text": "@CTO"},
        {"kind": "text", "text": " or "}, {"kind": "mention", "text": "@COO"},
        {"kind": "text", "text": " <img src=x onerror=alert(1)>"}]
    assert got["none"] == [[{"kind": "text", "text": "no marks `` @**"}],
                           [{"kind": "text", "text": "`a\nb`"}]]
    dom = _js(("textDir", "inlineParts", "inlineNodes", "fillBody"), r"""
      const el = document.createElement("div");
      fillBody(el, "@**CTO** שלום `x<y>` <b>hi</b>\n```py\nprint(1)\n```\nend");
      out.body = dump(el);
    """, prelude=FAKE_DOM)
    body = dom["body"]
    assert body["dir"] == "rtl"
    kinds = [(k.get("tag"), k.get("cls"), k.get("dir"), k["text"]) for k in body["kids"]]
    assert kinds == [
        ("strong", "mention-name", "ltr", "@CTO"), (None, None, None, " שלום "),
        ("code", "chip", "ltr", "x<y>"), (None, None, None, " <b>hi</b>\n"),
        ("pre", "", "ltr", "print(1)\n"), (None, None, None, "\nend")]
    # Every element's content is one text node: nothing was parsed.
    for k in body["kids"]:
        if "tag" in k:
            assert k["kids"] == [{"text": k["text"]}]
    css = _css()
    ment = _rule(".mention-name", css)
    assert "color: var(--accent)" in ment and "font-weight: 700" in ment
    assert "background: var(--code)" in _rule(".chip", css)


def test_77_a_mentioned_bubble_is_filled_yellow_without_a_side_bar():
    rule = _rule(".msg.mention .bubble")
    assert "background: var(--mention)" in rule and "border-inline-start" not in rule
    assert "3px solid var(--mention-line)" not in _css()


def test_77_typing_at_opens_a_picker_that_inserts_the_mention():
    got = _js(("mentionQuery", "pickNames"), """
      out.q = ["hi @", "hi @ct", "@CTO", "mail a@b", "hi @**CTO** x", "@x y"].map(mentionQuery);
      const names = ["COO", "CTO", "CMO", "Watchdog", "all", "Hemi Paska"];
      out.p = [pickNames(names, ""), pickNames(names, "c"), pickNames(names, "at"), pickNames(names, "zz")];
    """)
    assert got["q"] == ["", "ct", "CTO", None, None, None]
    assert got["p"][0] == ["COO", "CTO", "CMO", "Watchdog", "all", "Hemi Paska"]
    assert got["p"][1] == ["COO", "CTO", "CMO", "Watchdog"]  # starts-with first, then contains
    assert got["p"][2] == ["Watchdog"] and got["p"][3] == []
    prelude = FAKE_DOM + """
      function fitCompose() {}
      const S = {me: {user_id: 1}, pick: null, users: new Map([
        [1, {user_id: 1, full_name: "Me", is_bot: false}],
        [2, {user_id: 2, full_name: "CTO", is_bot: true}],
        [3, {user_id: 3, full_name: "Dana Levi", is_bot: false}]])};
    """
    dom = _js(("roleOf", "initial", "avatar", "mentionQuery", "pickNames", "mentionNames",
               "closePick", "renderPick", "updatePick", "choosePick"), """
      const box = $("compose");
      box.value = "hi @d"; box.selectionStart = box.selectionEnd = 5;
      updatePick();
      out.items = S.pick.items; out.shown = !$("picker").hidden;
      out.opts = $("picker").children.map((li) => [li.className, li.attrs["aria-selected"], li.textContent]);
      out.names = mentionNames();
      choosePick(0);
      out.value = box.value; out.caret = box.selectionStart; out.closed = S.pick === null && $("picker").hidden;
      box.value = "hi there"; box.selectionStart = box.selectionEnd = 8;
      updatePick();
      out.none = S.pick;
    """, prelude=prelude)
    assert dom["items"] == ["Dana Levi", "Watchdog"] and dom["shown"] is True
    assert dom["opts"] == [["active", "true", "DDana Levi"], ["", "false", "WWatchdog"]]
    assert dom["names"] == ["COO", "CTO", "CMO", "Watchdog", "all", "Dana Levi"]
    assert dom["value"] == "hi @**Dana Levi** " and dom["caret"] == len("hi @**Dana Levi** ")
    assert dom["closed"] is True and dom["none"] is None
    # The keys: arrows move, Enter/Tab pick (and do not send), Escape closes.
    script = _markup().script
    keys = script[script.index('$("compose").addEventListener("keydown"'):]
    keys = keys[:keys.index("});") + 3]
    assert keys.index("if (S.pick") < keys.index("send();")
    for k in ('"ArrowDown"', '"ArrowUp"', '"Enter" || e.key === "Tab"', '"Escape"'):
        assert k in keys, k
    assert '"@**" + name + "** "' in _function("choosePick")
    assert '$("compose").addEventListener("input", onComposeInput);' in script
    assert "updatePick();" in _function("onComposeInput")


def test_77_mention_buttons_have_a_label_and_compose_has_a_hint():
    m = _markup()
    ids = [a.get("id") or a.get("data-mention") for _, a in m.elements]
    assert ids.index("mention-label") < ids.index("COO")
    p = Text()
    p.feed(PAGE.read_text(encoding="utf-8"))
    assert "הזכר:" in p.chunks
    [hint] = [c for c in p.chunks if "Shift+Enter" in c]
    assert "@" in hint and "Enter שולח" in hint and ids.index("compose") < ids.index("hint")


def test_77_live_status_is_a_dot_green_when_live_amber_blinking_when_reconnecting():
    m = _markup()
    ids = [a.get("id") or a.get("class") for _, a in m.elements]
    assert ids[ids.index("live") + 1] == "dot"
    css = _css()
    assert "var(--ok)" in _rule("#live.live .dot", css)
    wait = _rule("#live.wait .dot", css)
    assert "var(--wait)" in wait and "animation: blink" in wait and "@keyframes blink" in css
    got = _js(("setLive",), """
      setLive("wait", "reconnecting"); out.a = [$("live").className, $("live-text").textContent];
      setLive("live", "מחובר"); out.b = [$("live").className, $("live").title];
    """, prelude=FAKE_DOM)
    assert got == {"a": ["wait", "reconnecting"], "b": ["live", "מחובר"]}
    poll = _function("poll")
    assert poll.count('setLive("wait", ') == 2 and 'setLive("live", ' in poll
    assert 'setLive("live", ' in _function("connect")


def test_77_day_separator_says_today_yesterday_weekday_or_the_date():
    import datetime
    days = ["יום ראשון", "יום שני", "יום שלישי", "יום רביעי", "יום חמישי", "יום שישי", "שבת"]
    got = _js(("dayLabel",), """
      const now = new Date(2026, 8, 28, 0, 5);
      out.l = [new Date(2026, 8, 28, 0, 1), new Date(2026, 8, 27, 23, 59), new Date(2026, 8, 25, 12),
               new Date(2026, 8, 21, 12), new Date(2026, 0, 1, 9)].map((d) => dayLabel(d, now));
    """)
    fri = days[(datetime.date(2026, 9, 25).weekday() + 1) % 7]
    assert got["l"] == ["היום · 28.9.2026", "אתמול · 27.9.2026", fri + " · 25.9.2026",
                        "21.9.2026", "1.1.2026"]
    assert "sep.textContent = dayLabel(when, now)" in _function("renderMessages")
    css = _css()
    assert "display: flex" in _rule(".day", css)
    line = _rule(".day::before, .day::after", css)
    assert "flex: 1" in line and "border-top: 1px solid var(--line)" in line


def test_77_quote_is_a_button_beside_the_bubble_and_the_quote_box_is_code_grey():
    tpl = re.search(r'<template id="msg-tpl">(.*?)</template>', PAGE.read_text(encoding="utf-8"), re.S).group(1)
    line = tpl[tpl.index('class="line"'):]
    assert line.index('class="bubble"') < line.index('class="icon quote-btn">ציטוט</button>')
    assert '<blockquote class="quote" hidden>' in tpl
    css = _css()
    btn = _rule(".quote-btn", css)
    assert "opacity: 0" in btn and "transition: opacity" in btn
    assert "opacity: 1" in _rule(".msg:hover .quote-btn, .quote-btn:focus", css)
    assert "background: var(--code)" in _rule(".quote", css)
    assert "insertText(quoteBlock(m.sender_full_name" in _function("messageNode")


def test_77_the_app_sits_in_a_rounded_card():
    css = _css()
    assert "padding: 1rem" in _rule("body", css)
    app = _rule("#app", css)
    assert "border-radius: 14px" in app and "border: 1px solid var(--line)" in app
    assert "overflow: hidden" in app and "height: calc(100vh - 2rem)" in app


def test_77_a_toast_follows_each_action_and_says_success_only_when_the_server_did():
    real = FAKE_DOM.replace("const toasts = [];\nfunction toast(text, bad) { toasts.push([text, !!bad]); }\n", "")
    assert "function toast" not in real
    got = _js(("toast",), """
      toast("נשלח · id 3");
      out.ok = [$("toast").textContent, $("toast").className, $("toast").hidden, timers[0][1]];
      toast("לא נשלח: boom", true);
      out.bad = [$("toast").className, timers[1][1]];
      timers[1][0]();
      out.after = $("toast").hidden;
    """, prelude=real + """
      const timers = [];
      const setTimeout = (f, ms) => { timers.push([f, ms]); return timers.length; };
      const clearTimeout = () => {};
      const S = {toastTimer: 0};
    """)
    assert got["ok"] == ["נשלח · id 3", "", False, 3500]
    assert got["bad"] == ["bad", 7000] and got["after"] is True
    prelude = FAKE_DOM + """
      let FAIL = null;
      async function api(method, path, params) {
        if (FAIL) throw new Error(FAIL);
        return {result: "success", id: 42};
      }
      function fitCompose() {}
      function sendTyping() {}
      let lastTypingSent = 0;
      const S = {sending: false, open: {stream_id: 1, topic: "t"}};
    """
    sent = _js(("sendError", "send"), """
      const box = $("compose");
      box.value = "hello"; await send();
      out.ok = [box.value, toasts.splice(0)];
      box.value = "again"; FAIL = "stream does not exist"; await send();
      out.bad = [box.value, toasts.splice(0), $("send-error").textContent];
      S.open = null; FAIL = null; await send();
      out.closed = [box.value, toasts.splice(0)];
    """, prelude=prelude)
    assert sent["ok"] == ["", [["נשלח · id 42", False]]]
    assert sent["bad"] == ["again", [["לא נשלח: stream does not exist", True]], "stream does not exist"]
    assert sent["closed"][0] == "again" and sent["closed"][1][0][1] is True
    # Every panel result goes through report(): the status line and a toast.
    script = _markup().script
    assert re.search(r"function report\(id, text, bad\) \{\s*say\(id, text, bad\);\s*toast\(text, bad\);", script)
    for call in ('report("members-status", subscribeReport(', 'report("members-status", r.removed.length',
                 'report("members-status", privateReport(', 'report("create-status", createReport(',
                 'report("create-status", "לא נוצר: "', 'report("members-status", "לא נוסף: "'):
        assert call in script, call


# -- ops#249 batch 1: one-tap emoji reactions


def test_reaction_chips_group_by_emoji_and_mark_mine():
    got = _js(("renderReactions",), """
      const node = document.createElement("div");
      const box = document.createElement("div");
      box.className = "reactions";
      node.append(box);
      const m = {id: 5, reactions: [
        {emoji_name: "👍", emoji_code: "👍", reaction_type: "unicode_emoji", user_id: 1},
        {emoji_name: "👍", emoji_code: "👍", reaction_type: "unicode_emoji", user_id: 2},
        {emoji_name: "🎉", emoji_code: "🎉", reaction_type: "unicode_emoji", user_id: 2}]};
      renderReactions(node, m);
      out.chips = box.children.map((c) => [c.className, c.textContent]);
    """, prelude=FAKE_DOM + "const S = {me: {user_id: 1}};")
    assert got["chips"] == [["reaction-chip mine", "👍 2"], ["reaction-chip", "🎉 1"]]


def test_toggle_reaction_adds_then_removes_through_the_api():
    prelude = API_STUB + FAKE_DOM + """
      const S = {me: {user_id: 1, full_name: "Me"}};
    """
    got = _js(("toggleReaction", "renderReactions"), """
      const node = document.createElement("div");
      const box = document.createElement("div");
      box.className = "reactions";
      node.append(box);
      $("messages").querySelector = () => node;  // stand in for the real lookup by data-id
      const m = {id: 7, reactions: []};
      await toggleReaction(m, "👍");
      out.afterAdd = [calls[0], [...m.reactions].map((r) => r.emoji_name), box.textContent];
      await toggleReaction(m, "👍");
      out.afterRemove = [calls[1][0], calls[1][1], m.reactions.length, box.textContent];
    """, prelude=prelude)
    out_add = got["afterAdd"]
    assert out_add[0] == ["POST", "messages/7/reactions", {"emoji_name": "👍"}]
    assert out_add[1] == ["👍"] and "👍 1" in out_add[2]
    assert got["afterRemove"][:3] == ["DELETE", "messages/7/reactions", 0]
    assert got["afterRemove"][3] == ""


def test_toggle_reaction_does_not_double_count_a_live_event_that_wins_the_race():
    """The sender is a subscriber of its own stream, so the live "reaction"
    event for a tap can reach onReaction() before this same tap's own POST
    promise resolves (both are separate in-flight requests). An earlier
    version of toggleReaction appended its optimistic entry unconditionally
    after the await, double-counting a reaction the live event had already
    added. Regression test for that race, not just the happy path above."""
    prelude = FAKE_DOM + """
      const S = {me: {user_id: 1, full_name: "Me"}};
      // The POST "resolves" only after delivering the live event first —
      // exactly the ordering that broke the naive implementation.
      async function api(method, path, params) {
        onReaction({type: "reaction", op: "add", message_id: 7, user_id: 1,
                   emoji_name: params.emoji_name, emoji_code: params.emoji_name,
                   reaction_type: "unicode_emoji", user: {full_name: "Me"}});
        return {};
      }
    """
    got = _js(("toggleReaction", "renderReactions", "onReaction"), """
      const node = document.createElement("div");
      const box = document.createElement("div");
      box.className = "reactions";
      node.append(box);
      $("messages").querySelector = () => node;
      const m = {id: 7, reactions: []};
      S.msgs = [m];  // onReaction() looks the message up here, same reference
      await toggleReaction(m, "👍");
      out.reactions = m.reactions.length;
      out.chip = box.textContent;
    """, prelude=prelude)
    assert got["reactions"] == 1
    assert got["chip"] == "👍 1"


def test_a_live_reaction_event_updates_the_open_message_in_place():
    got = _js(("onReaction", "renderReactions"), """
      const node = document.createElement("div");
      const box = document.createElement("div");
      box.className = "reactions";
      node.append(box);
      $("messages").querySelector = () => node;
      S.msgs = [{id: 9, reactions: []}];
      onReaction({type: "reaction", op: "add", message_id: 9, user_id: 2, emoji_name: "🙏",
                 emoji_code: "🙏", reaction_type: "unicode_emoji", user: {full_name: "COO"}});
      out.afterAdd = [S.msgs[0].reactions.length, box.textContent];
      onReaction({type: "reaction", op: "remove", message_id: 9, user_id: 2, emoji_name: "🙏",
                 emoji_code: "🙏", reaction_type: "unicode_emoji", user: {full_name: "COO"}});
      out.afterRemove = [S.msgs[0].reactions.length, box.textContent];
      // A message not in S.msgs (a different, unopened topic) is a no-op,
      // not an error: the event is simply not for anything on screen.
      onReaction({type: "reaction", op: "add", message_id: 404, user_id: 2, emoji_name: "🙏",
                 emoji_code: "🙏", reaction_type: "unicode_emoji", user: {full_name: "COO"}});
      out.noCrash = true;
    """, prelude=FAKE_DOM + "const S = {me: {user_id: 1}};")
    assert got["afterAdd"] == [1, "🙏 1"]
    assert got["afterRemove"] == [0, ""]
    assert got["noCrash"] is True


def test_one_tap_reacts_a_long_press_opens_the_picker():
    prelude = FAKE_DOM + """
      const timers = [];
      let nextId = 1;
      function setTimeout(f, ms) { const id = nextId++; timers.push([id, f, ms]); return id; }
      function clearTimeout(id) { const i = timers.findIndex((t) => t[0] === id); if (i >= 0) timers.splice(i, 1); }
      let pickedWith = null, toggledWith = null;
      function openReactPicker(btn, m) { pickedWith = m; }
      function toggleReaction(m, name) { toggledWith = [m.id, name]; }
    """
    got = _js(("wireReactBtn",), """
      const btn = document.createElement("button");
      const m = {id: 3};
      wireReactBtn(btn, m);
      // A quick tap: pointerdown then pointerup well before the long-press delay.
      btn.on.pointerdown();
      btn.on.pointerup();
      btn.on.click();
      out.tap = [toggledWith, pickedWith];
      toggledWith = null;
      // A long press: pointerdown, the delayed callback fires (simulated —
      // nothing here advances real time), then the eventual click is a no-op.
      btn.on.pointerdown();
      const [, fire] = timers[timers.length - 1];
      fire();
      btn.on.click();
      out.hold = [toggledWith, pickedWith];
    """, prelude=prelude)
    assert got["tap"] == [[3, "👍"], None]
    assert got["hold"] == [None, {"id": 3}]


def test_254_the_reaction_row_sits_under_the_message_text():
    # The row (chips, then the add-reaction button) is in the same message
    # block as the text, directly under it — not beside it in .line, where
    # the add-reaction button used to sit next to the quote button.
    tpl = re.search(r'<template id="msg-tpl">(.*?)</template>',
                    PAGE.read_text(encoding="utf-8"), re.S).group(1)
    assert tpl.index('class="body"') < tpl.index('class="reactions"')
    line = tpl[tpl.index('class="line"'):tpl.index('class="reactions"')]
    assert "react-btn" not in line
    assert "react-btn" in tpl[tpl.index('class="reactions"'):]

    # Same for a message with no reactions yet: rendering an empty reactions
    # list must still leave the always-there add-reaction button in the row
    # after the text, not wipe it out along with the (absent) chips.
    got = _js(("renderReactions",), """
      const stack = document.createElement("div");
      const body = document.createElement("div");
      body.className = "body";
      const box = document.createElement("div");
      box.className = "reactions";
      const btn = document.createElement("button");
      btn.className = "react-btn";
      box.append(btn);
      stack.append(body, box);
      renderReactions(stack, {id: 7, reactions: []});
      out.order = stack.children.indexOf(body) < stack.children.indexOf(box);
      out.btnPresent = box.children.includes(btn);
      out.chips = box.querySelectorAll(".reaction-chip").length;
    """, prelude=FAKE_DOM + "const S = {me: {user_id: 1}};")
    assert got["order"] is True
    assert got["btnPresent"] is True
    assert got["chips"] == 0


# -- ops#249 batch 1: the typing / "working on it" indicator


def test_typing_indicator_shows_only_for_the_open_room_and_clears_on_stop():
    prelude = """
      const els = {};
      const $ = (id) => els[id] || (els[id] = {textContent: ""});
      const S = {open: {stream_id: 1, topic: "t"}, typing: new Map()};
      const TYPING_TTL_MS = 15000;
    """
    got = _js(("typingKey", "onTyping", "renderTyping", "clearTyping"), """
      onTyping({op: "start", status: "typing", stream_id: 1, topic: "t",
               sender: {user_id: 5, full_name: "CTO"}});
      out.oneTyper = $("typing").textContent;
      onTyping({op: "start", status: "working", stream_id: 1, topic: "t",
               sender: {user_id: 6, full_name: "COO"}});
      out.twoTypers = $("typing").textContent;
      // A different room's event never shows here.
      onTyping({op: "start", status: "typing", stream_id: 2, topic: "other",
               sender: {user_id: 7, full_name: "CMO"}});
      out.unaffectedByOtherRoom = $("typing").textContent;
      onTyping({op: "stop", status: "typing", stream_id: 1, topic: "t",
               sender: {user_id: 5, full_name: "CTO"}});
      out.afterOneStops = $("typing").textContent;
      clearTyping(1, "t", 6);
      out.afterClear = $("typing").textContent;
    """, prelude=prelude)
    assert got["oneTyper"] == "CTO מקליד/ה…"
    assert got["twoTypers"] == "CTO מקליד/ה… · COO עובד/ת על זה…"
    assert got["unaffectedByOtherRoom"] == got["twoTypers"]
    assert got["afterOneStops"] == "COO עובד/ת על זה…"
    assert got["afterClear"] == ""


def test_typing_indicator_is_blank_with_no_room_open():
    got = _js(("renderTyping",), """
      renderTyping();
      out.text = $("typing").textContent;
    """, prelude="""
      const els = {};
      const $ = (id) => els[id] || (els[id] = {textContent: ""});
      const S = {open: null, typing: new Map()};
    """)
    assert got["text"] == ""


def test_typing_input_handler_restarts_after_an_idle_stop_and_stops_once():
    """ops#249 batch 1, found in CTO review on the PR: (1) the idle "stop"
    must reset lastTypingSent, or typing again within 8s of the original
    "start" sends no new "start" and the indicator stays wrongly off;
    (2) emptying the box must send "stop" only once, not on every
    subsequent input event."""
    prelude = """
      const els = {compose: {value: ""}};
      const $ = (id) => els[id] || (els[id] = {});
      function fitCompose() {}
      function updatePick() {}
      const sent = [];
      function sendTyping(op) { sent.push(op); }
      let lastTypingSent = 0;
      let typingIdle = 0;
      const timers = [];
      let nextId = 1;
      function setTimeout(f, ms) { const id = nextId++; timers.push([id, f, ms]); return id; }
      function clearTimeout(id) { const i = timers.findIndex((t) => t[0] === id); if (i >= 0) timers.splice(i, 1); }
    """
    got = _js(("onComposeInput",), """
      $("compose").value = "hi";
      onComposeInput();
      out.afterFirstStart = [...sent];
      // The idle timer fires (simulated: nothing here advances real time):
      // sends "stop" and must reset lastTypingSent.
      const [, fire] = timers[timers.length - 1];
      fire();
      out.lastTypingSentAfterIdle = lastTypingSent;
      // Typing again, still well inside the original 8s throttle window:
      // must send a fresh "start" now that the server was told "stop".
      sent.length = 0;
      onComposeInput();
      out.afterResume = [...sent];
      // Emptying the box: "stop" exactly once across repeated input events.
      sent.length = 0;
      $("compose").value = "";
      onComposeInput();
      onComposeInput();
      onComposeInput();
      out.afterEmpty = [...sent];
    """, prelude=prelude)
    assert got["afterFirstStart"] == ["start"]
    assert got["lastTypingSentAfterIdle"] == 0
    assert got["afterResume"] == ["start"]
    assert got["afterEmpty"] == ["stop"]


# -- ops#249 batch 1: "tap the quote to jump to it"


def test_jump_to_quote_scrolls_when_loaded_fetches_when_not_and_toasts_when_missing():
    # `apiImpl` is mutated from the test body; `api` (what jumpToQuote
    # actually calls) stays a single top-level function so jumpToQuote's
    # closure resolves it — a stub redeclared inside the async IIFE below
    # would be invisible to a function defined outside it.
    prelude = FAKE_DOM + """
      El.prototype.scrollIntoView = function () { this.scrolled = true; };
      const S = {open: {stream_id: 1, stream: "feature", topic: "t"}, shown: new Set()};
      function addMessages(list) { for (const m of list) S.shown.add(m.id); }
      const calls = [];
      let apiImpl = async () => ({});
      async function api(method, path) { calls.push([method, path]); return apiImpl(); }
    """
    got = _js(("jumpToQuote",), """
      const found = document.createElement("div");
      found.dataset.id = "5";
      $("messages").querySelector = (sel) => (sel === '[data-id="5"]' ? found : null);
      await jumpToQuote(5);
      out.found = [found.scrolled, found.classList.contains("jump-flash"), calls.length];
    """, prelude=prelude)
    assert got["found"] == [True, True, 0]  # loaded already: never asks the server

    # Not currently loaded: fetched from the server, then found by a second lookup.
    fetched = _js(("jumpToQuote",), """
      const node = document.createElement("div");
      let lookups = 0;
      $("messages").querySelector = () => { lookups += 1; return lookups > 1 ? node : null; };
      apiImpl = async () => ({message: {id: 12, display_recipient: "feature", subject: "t"}});
      await jumpToQuote(12);
      out.scrolled = node.scrolled;
      out.calls = calls;
    """, prelude=prelude)
    assert fetched["calls"] == [["GET", "messages/12"]]
    assert fetched["scrolled"] is True

    missing = _js(("jumpToQuote",), """
      $("messages").querySelector = () => null;
      apiImpl = async () => { throw new Error("gone"); };
      await jumpToQuote(99);
      out.toasted = toasts;
    """, prelude=prelude)
    assert missing["toasted"] == [["ההודעה המקורית לא נמצאה בנושא הזה.", True]]


# -- issue #85: clickable links (URLs and repo#N refs)


def test_85_urls_and_repo_refs_parse_as_link_parts_a_lone_hash_does_not():
    got = _js(("inlineParts",), r"""
      out.url = inlineParts("see https://example.com/x for more");
      out.trailing_dot = inlineParts("see https://example.com/x. next");
      out.trailing_paren = inlineParts("(https://example.com/x)");
      out.ref = inlineParts("fixed in ops#94 today");
      out.other_refs = inlineParts("see agora#80 shal#237 adk-lab#11 pytest-shal#6");
      out.lone_hash = inlineParts("see #123 here");
    """)
    assert got["url"] == [
        {"kind": "text", "text": "see "},
        {"kind": "link", "text": "https://example.com/x", "href": "https://example.com/x"},
        {"kind": "text", "text": " for more"}]
    # The trailing "." is not part of the link: it belongs to the sentence.
    assert got["trailing_dot"] == [
        {"kind": "text", "text": "see "},
        {"kind": "link", "text": "https://example.com/x", "href": "https://example.com/x"},
        {"kind": "text", "text": ". next"}]
    assert got["trailing_paren"] == [
        {"kind": "text", "text": "("},
        {"kind": "link", "text": "https://example.com/x", "href": "https://example.com/x"},
        {"kind": "text", "text": ")"}]
    assert got["ref"] == [
        {"kind": "text", "text": "fixed in "},
        {"kind": "link", "text": "ops#94", "href": "https://github.com/determlab/ops/issues/94"},
        {"kind": "text", "text": " today"}]
    assert got["other_refs"] == [
        {"kind": "text", "text": "see "},
        {"kind": "link", "text": "agora#80", "href": "https://github.com/determlab/agora/issues/80"},
        {"kind": "text", "text": " "},
        {"kind": "link", "text": "shal#237", "href": "https://github.com/determlab/shal/issues/237"},
        {"kind": "text", "text": " "},
        {"kind": "link", "text": "adk-lab#11", "href": "https://github.com/determlab/adk-lab/issues/11"},
        {"kind": "text", "text": " "},
        {"kind": "link", "text": "pytest-shal#6", "href": "https://github.com/determlab/pytest-shal/issues/6"}]
    # No repo name in front of the "#": stays plain text, never a link.
    assert got["lone_hash"] == [{"kind": "text", "text": "see #123 here"}]


def test_85_links_render_as_real_anchor_elements_opening_in_a_new_tab():
    got = _js(("inlineParts", "inlineNodes"), r"""
      out.nodes = inlineNodes("look at ops#94 and https://x.test/y.").map((n) => (
        n.tagName ? {tag: n.tagName, text: n.textContent, href: n.href,
                     target: n.target, rel: n.rel, dir: n.dir}
                  : {text: n.data}));
    """, prelude=FAKE_DOM)
    assert got["nodes"] == [
        {"text": "look at "},
        {"tag": "A", "text": "ops#94", "href": "https://github.com/determlab/ops/issues/94",
         "target": "_blank", "rel": "noopener noreferrer", "dir": "ltr"},
        {"text": " and "},
        {"tag": "A", "text": "https://x.test/y", "href": "https://x.test/y",
         "target": "_blank", "rel": "noopener noreferrer", "dir": "ltr"},
        {"text": "."}]


def test_85_a_hebrew_message_with_a_link_renders_as_dom_nodes_and_keeps_rtl_order():
    dom = _js(("textDir", "inlineParts", "inlineNodes", "fillBody"), r"""
      const el = document.createElement("div");
      fillBody(el, "שלום https://example.com/x ops#94 עולם");
      out.body = dump(el);
    """, prelude=FAKE_DOM)
    body = dom["body"]
    assert body["dir"] == "rtl"
    kinds = [(k.get("tag"), k.get("dir"), k["text"]) for k in body["kids"]]
    assert kinds == [
        (None, None, "שלום "), ("a", "ltr", "https://example.com/x"), (None, None, " "),
        ("a", "ltr", "ops#94"), (None, None, " עולם")]
    # Every link's content is one text node: nothing was parsed as markup.
    for k in body["kids"]:
        if k.get("tag") == "a":
            assert k["kids"] == [{"text": k["text"]}]
    assert 'id="send-status"' not in PAGE.read_text(encoding="utf-8")


# -- issue #82: the dashboard panel. Revised on agora#100 (founder ruling):
# full width with a chat/board switch, chat and the board never shown
# together, at every viewport size — not just side by side at 1280px.


def test_page_registers_for_dashboard_events():
    # Extended, not replaced: the same register call still asks for "message"
    # (issue #82 says "extend an existing tool/call", D1's shape one layer up).
    # ops#249 batch 1 extends it the same way, for "reaction" and "typing".
    script = _markup().script
    assert re.search(r'api\("POST", "register",\s*'
                     r'\{event_types: \["message", "dashboard", "reaction", "typing"\]\}\)', script)


def test_page_has_the_panel_and_the_switch():
    text = PAGE.read_text(encoding="utf-8")
    for needle in ('id="dash"', 'id="view-chat"', 'id="view-dash"'):
        assert needle in text, needle
    # agora#100: chat and the board are never shown together at any width —
    # the side-by-side-at-1280px rule (agora#82) is gone.
    assert "1280px" not in text
    # The switch survives either pane being hidden: it is its own element,
    # not nested inside #side, #main or #dash.
    m = _markup()
    ids = [a.get("id") for _, a in m.elements]
    assert ids.index("view-switch") < ids.index("side")
    assert ids.index("view-switch") < ids.index("main") and ids.index("view-switch") < ids.index("dash")


def test_the_board_view_hides_the_sidebar_and_chat_not_just_chat():
    # agora#100: "when the board is shown, hide the channel list and the
    # chat; the switch brings them back" — setView("dash") must hide #side
    # too, not only #main, so the board gets the full width.
    script = _markup().script
    m = re.search(r"function setView\(view\) \{.*?\n\}", script, re.S)
    assert m, "setView not found"
    body = m.group(0)
    assert re.search(r'\$\("side"\)\.hidden\s*=\s*view\s*!==\s*"chat"', body)
    assert re.search(r'\$\("main"\)\.hidden\s*=\s*view\s*!==\s*"chat"', body)
    assert re.search(r'\$\("dash"\)\.hidden\s*=\s*view\s*!==\s*"dash"', body)


# The real document: `python tools/dashboard.py --out` from ops, 2026-09-28,
# committed unchanged from agora#82 comment 5867412108. Rounds 1-3 tested
# hand-written fields no producer sends; every card test reads this file.
DASHBOARD_FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "dashboard-doc.json").read_text(encoding="utf-8"))

CARDS = ("live", "stuck", "roles", "queue", "products", "waiting", "week", "tokens", "timeline",
         "prs", "progress", "approvals", "kpis")

# The newer document (ops#220): `python tools/dashboard.py --json --no-tokens`
# on 2026-09-29 with timeline, plan, open_prs and tokens.pool_runs.
DASHBOARD_FIXTURE_220 = json.loads(
    (ROOT / "tests" / "fixtures" / "dashboard-doc-220.json").read_text(encoding="utf-8"))

DASH_FUNCS = ("el", "minsAgo", "agoText", "pillText", "renderPill", "joinDetail", "cutList", "link",
              "rowItem", "renderRows", "localMidnight", "hhmm", "renderKpis", "timelineWindow",
              "renderTimeline", "renderLive", "stuckRow", "renderStuck", "renderWaiting", "renderRoles",
              "renderQueue", "renderPrs", "renderWeek", "renderProducts", "fmtTok", "resetText",
              "renderPlan", "renderTokens", "renderProgress", "shortRef", "renderApprovals",
              "clearApprovalsPending")


def _consts(*names: str) -> str:
    """The page's own one-line `const`/`let` of each name, as written."""
    script = _markup().script
    out = []
    for n in names:
        m = re.search(rf"^(?:const|let) {n} = .*;", script, re.M)
        assert m, n
        out.append(m.group(0))
    return "\n".join(out)


def _dash_prelude() -> str:
    return FAKE_DOM + _consts("DASH_LIMIT", "dashOpen", "approvalsNotes", "TOKVIEW", "VIEW", "tokView",
                              "ANSWER_HE", "CHECK_HE", "ROLE_STATE", "TOK_WIN", "approvalsIdx",
                              "approvalsPending") + """
const rerendered = [];
function rerenderDash(cls) { rerendered.push(cls); }
let mounted = null;
function mountCard(id, node) { mounted = {id, node}; }
const timers = [];
function setTimeout(fn, ms) { const t = {fn, ms, cancelled: false}; timers.push(t); return timers.length; }
function clearTimeout(id) { const t = timers[id - 1]; if (t) t.cancelled = true; }
const localStorage = {data: {}, setItem(k, v) { this.data[k] = String(v); }, getItem(k) { return this.data[k] ?? null; }};
"""


def _dash(body: str, extra: tuple[str, ...] = ()):
    return _js(DASH_FUNCS + extra, body, prelude=_dash_prelude())


def _text(dumped) -> str:
    return json.dumps(dumped, ensure_ascii=False)


def _find(dumped, cls: str) -> list:
    """Every node in a dump whose class list holds `cls`."""
    out = []
    if isinstance(dumped, dict):
        if cls in (dumped.get("cls") or "").split():
            out.append(dumped)
        for k in dumped.get("kids") or []:
            out += _find(k, cls)
    return out


def _render_all(doc, now_ms):
    return _dash(f"""
      const doc = {json.dumps(doc)};
      const now = {now_ms};
      out.live = dump(renderLive(doc.live));
      out.stuck = dump(renderStuck(doc.stuck));
      out.roles = dump(renderRoles(doc.roles));
      out.queue = dump(renderQueue(doc.queue));
      out.week = dump(renderWeek(doc.week));
      out.products = dump(renderProducts(doc.products));
      out.waiting = dump(renderWaiting(doc.stuck));
      out.tokens = dump(renderTokens(doc.tokens, doc.plan));
      out.timeline = dump(renderTimeline(doc.timeline, now));
      out.prs = dump(renderPrs(doc.open_prs));
      out.progress = dump(renderProgress(doc.progress));
      out.approvals = dump(renderApprovals(doc.next_moves));
      out.kpis = dump(renderKpis(doc, now));
    """)


@pytest.mark.parametrize("fixture", ["old", "220"])
def test_every_card_renders_cleanly_from_a_real_document(fixture):
    # The round-2 bug class (agora#90): "[object Object]", "undefined", NaN.
    # The 2026-09-28 document has no timeline / plan / open_prs at all: each
    # of those cards says so instead of breaking.
    doc = DASHBOARD_FIXTURE if fixture == "old" else DASHBOARD_FIXTURE_220
    now = "Date.parse('2026-09-29T19:11:53Z')" if fixture == "220" else "Date.parse('2026-09-28T12:00:00Z')"
    got = _render_all(doc, now)
    for name in CARDS:
        text = _text(got[name])
        assert "[object" not in text and "undefined" not in text and "NaN" not in text, (name, text[:300])
        assert got[name]["text"].strip(), name
    if fixture == "old":
        assert "צריך dashboard.py עדכני" in got["timeline"]["text"]
        assert "צריך dashboard.py עדכני" in got["prs"]["text"]
        assert "אין נתון תוכנית" in got["tokens"]["text"]


def test_lists_are_cut_to_five_rows_with_a_show_all_toggle():
    got = _dash("""
      const rows = Array.from({length: 8}, (_, i) => ({label: "r#" + i, detail: "t" + i}));
      out.cut = dump(renderRows(rows, "k"));
      out.cutMore = renderRows(rows, "k").childNodes[1].textContent;
      renderRows(rows, "k").childNodes[1].onclick();
      out.open = [...dashOpen];
      out.rerendered = rerendered;
      out.full = dump(renderRows(rows, "k"));
      out.few = dump(renderRows(rows.slice(0, 3), "k"));
      out.empty = dump(renderRows([], "k", "התור ריק"));
    """)
    ul = got["cut"]["kids"][0]
    assert len(ul["kids"]) == 5 and got["cutMore"] == "הצג הכל (8)"
    assert got["open"] == ["k"] and got["rerendered"] == ["more-k"]
    full_ul, less = got["full"]["kids"]
    assert len(full_ul["kids"]) == 8 and "all" in full_ul["cls"].split() and less["text"] == "הצג פחות"
    assert got["few"]["tag"] == "ul" and len(got["few"]["kids"]) == 3        # no toggle when it all fits
    assert got["empty"]["kids"][0]["text"] == "התור ריק"


def test_a_row_is_one_line_with_the_full_text_on_hover():
    long = "א" * 300
    got = _dash(f"""
      const li = rowItem({{label: "shal#1", url: "https://x/1", detail: "{long}", meta: "צריך אדם", metaCls: "warn"}});
      out.li = dump(li);
      out.title = li.childNodes[1].title;
    """)
    kids = got["li"]["kids"]
    assert kids[0]["tag"] == "a" and kids[0]["href"] == "https://x/1" and "ref" in kids[0]["cls"]
    assert "tx" in kids[1]["cls"].split() and got["title"] == "א" * 300
    assert kids[2]["cls"] == "chip warn"
    css = _css()
    assert "text-overflow: ellipsis" in _rule(".tx, .one", css) and "nowrap" in _rule(".tx, .one", css)


def test_kpi_strip_is_four_coloured_numbers():
    got = _dash("""
      const now = Date.parse("2026-09-29T12:00:00Z");
      const mid = localMidnight(now);
      const doc = {next_moves: [{id: "a"}, {id: "b"}], live: [],
                   timeline: {merges: [{ref: "a#1", at: new Date(mid + 3600e3).toISOString()},
                                       {ref: "a#2", at: new Date(mid - 3600e3).toISOString()}]},
                   plan: {"5h": {percent: 90, expired: false}, stale: false}};
      out.k = dump(renderKpis(doc, now));
      out.none = dump(renderKpis({}, now));
      out.expired = dump(renderKpis({plan: {"5h": {percent: 90, expired: true}}}, now));
    """)
    tiles = [(k["cls"], k["kids"][0]["text"], k["kids"][1]["text"]) for k in got["k"]["kids"]]
    assert tiles == [("kpi warn", "2", "מחכה לך"), ("kpi none", "פנוי", "ה-POOL"),
                     ("kpi ok", "1", "נמזגו היום"), ("kpi bad", "90%", "תוכנית · 5 שעות")]
    assert [k["kids"][0]["text"] for k in got["none"]["kids"]] == ["—", "פנוי", "—", "—"]
    assert got["expired"]["kids"][3]["kids"][0]["text"] == "—"


def test_timeline_has_run_merge_and_decision_lanes_a_now_line_and_newest_first_events():
    got = _dash("""
      const now = new Date(2026, 8, 29, 12, 0).getTime();       // local noon
      const iso = (h, m) => new Date(2026, 8, 29, h, m).toISOString();
      const t = {runs: [{ref: "shal#2", round: 1, start: iso(9, 0), end: iso(9, 40), outcome: "RunEnded", live: false, url: "u2"},
                        {ref: "shal#2", round: 2, start: iso(11, 0), end: null, live: true, url: "u2"},
                        {ref: "ops#5", round: 1, start: iso(10, 0), end: null, live: false, url: "u5"}],
                 merges: [{ref: "agora#9", title: "merged thing", at: iso(10, 30), url: "m9"}],
                 decisions: [{id: "c", answer: "yes", q_he: "לשחרר?", note: "go", at: iso(11, 30)}], notes: {}};
      out.full = dump(renderTimeline(t, now));
      out.noDecisions = dump(renderTimeline({...t, decisions: null, notes: {decisions: "no decisions record yet"}}, now));
      out.empty = dump(renderTimeline({runs: [], merges: [], decisions: [], notes: {}}, now));
      out.missing = dump(renderTimeline(undefined, now));
    """)
    full = got["full"]
    lanes = [k["text"] for k in _find(full, "ln")]
    assert lanes == ["ריצות", "מיזוגים", "החלטות מייסד"]
    runs = _find(full, "run")
    assert [r["cls"] for r in runs] == ["ev run", "ev run live", "ev run unk"]
    assert len(_find(full, "mg")) >= 1 and len(_find(full, "dc")) >= 1
    assert len(_find(full, "now")) == 3 and "עכשיו 12:00" in _text(full)
    times = [k["text"] for k in _find(full, "evlist")[0]["kids"] for k in k["kids"] if k["tag"] == "time"]
    assert times == sorted(times, reverse=True) and times[0] == "11:30"
    assert "המייסד אושר: לשחרר? (go)" in _text(full)
    nd = got["noDecisions"]
    assert [k["text"] for k in _find(nd, "ln")] == ["ריצות", "מיזוגים"]
    assert "החלטות מייסד: אין עדיין מקור" in _text(nd)
    assert "אין אירועים היום עדיין" in _text(got["empty"])
    assert "צריך dashboard.py עדכני" in got["missing"]["text"]


def test_pool_now_shows_the_run_big_or_one_no_run_line():
    got = _dash("""
      out.idle = dump(renderLive([]));
      out.run = dump(renderLive([{ref: "agora#71", url: "u", round: 2, phase: "reviewer", claimed_min: 34,
                                   idle_min: -2, last_action: "Bash: pytest -q", prev_end: "08:10Z"}]));
    """)
    assert "אין ריצה עכשיו" in got["idle"]["text"] and "פנוי" in got["idle"]["text"]
    run = got["run"]
    big = _find(run, "big")[0]
    assert big["tag"] == "a" and big["text"] == "agora#71"
    assert "2 · reviewer" in run["text"] and "34 מאז שהתחיל" in run["text"]
    assert "0 מאז הפעולה האחרונה" in run["text"]            # clock skew clamped (agora#100)
    assert _find(run, "act")[0]["tag"] == "bdi"


def test_roles_have_an_avatar_a_one_line_task_and_a_status_dot():
    got = _dash("""
      out.r = dump(renderRoles([{role: "CTO", state: "waiting", doing: "review", ref: "ops#1", url: "u"},
                                {role: "Pool", state: "busy", doing: "shal#2", ref: "shal#2", url: "u"},
                                {role: "CMO", state: "off", doing: "אין משימה פתוחה", ref: null}]));
    """)
    avs = _find(got["r"], "av")
    assert [(a["cls"], a["text"]) for a in avs] == [("av role-cto", "CTO"), ("av role-pool", "Pool"), ("av role-cmo", "CMO")]
    assert [s["cls"] + ":" + s["text"] for s in _find(got["r"], "st")] == ["st wait:מחכה", "st busy:עובד", "st idle:פנוי"]


def test_queue_and_prs_carry_chips():
    got = _dash("""
      out.q = dump(renderQueue([{priority: "p0", ref: "shal#1", url: "u", title: "t", needs_human: true}]));
      out.p = dump(renderPrs([{ref: "ops#2", url: "u", title: "b", type: "B", verdict: "RECOMMEND", draft: false},
                              {ref: "ops#3", url: "u", title: "a", type: "A", verdict: "CHANGES", draft: true},
                              {ref: "ops#4", url: "u", title: "n", type: null, verdict: null, draft: false}]));
      out.pNull = dump(renderPrs(null));
    """)
    li = got["q"]["kids"][0]["kids"]
    assert (li[0]["cls"], li[0]["text"]) == ("chip p0", "P0") and li[-1]["text"] == "צריך אדם"
    chips = [c["text"] for c in _find(got["p"], "chip")]
    assert chips == ["type B", "type A", "שינויים", "לא נבדק"]
    assert "טיוטה · a" in _text(got["p"])
    assert "GitHub לא נקרא" in got["pNull"]["text"]
    assert "type A: ממתין למיזוג" in got["p"]["text"] and "auto-merge" not in got["p"]["text"]   # the founder merges


def test_prs_say_when_the_list_is_cut():
    # CTO on agora#113: dashboard.py asks GitHub for the first 50 open PRs only.
    got = _dash("""
      const p = [{ref: "ops#2", url: "u", title: "b", type: "B", verdict: "RECOMMEND", draft: false}];
      out.cut = renderPrs(p, 73).textContent;
      out.whole = renderPrs(p, 1).textContent;
      out.noTotal = renderPrs(p).textContent;
    """)
    assert "מוצגים 1 מתוך 73" in got["cut"]
    assert "מתוך" not in got["whole"] and "מתוך" not in got["noTotal"]


def test_overlapping_runs_get_their_own_rows_and_short_bars_no_text():
    # QA on ops#220: four parallel pool runs drew on top of each other, and
    # a 5-minute bar showed one cut letter of its ref.
    got = _dash("""
      const now = new Date(2026, 8, 29, 12, 0).getTime();
      const iso = (h, m) => new Date(2026, 8, 29, h, m).toISOString();
      const t = {runs: [{ref: "shal#1", round: 1, start: iso(4, 0), end: iso(6, 0), live: false, url: "u"},
                        {ref: "shal#2", round: 1, start: iso(4, 30), end: iso(4, 40), live: false, url: "u"},
                        {ref: "shal#3", round: 1, start: iso(8, 0), end: iso(8, 10), live: false, url: "u"}],
                 merges: [], decisions: [], notes: {}};
      const tl = renderTimeline(t, now);
      const runs = [];
      const walk = (n) => { if ((n.className || "").split(" ").includes("run")) runs.push(n); (n.childNodes || []).forEach(walk); };
      walk(tl);
      out.runs = runs.map((r) => ({text: r.textContent, top: r.style.top || "", title: r.title}));
    """)
    runs = got["runs"]
    assert [r["text"] for r in runs] == ["shal#1", "", ""]             # 2 h is wide enough, 10 min is not
    assert runs[0]["top"] != runs[1]["top"]                            # overlapping: two rows
    assert runs[2]["top"] == runs[0]["top"]                            # after shal#1 ended: back to row one
    assert all(r["title"].startswith(r["title"].split(" ")[0]) and "shal#" in r["title"] for r in runs)


def test_tokens_card_today_week_toggle_role_and_pool_bars_and_plan():
    got = _dash("""
      const t = {agents: {COO: {"24h": {weighted: 3e6}, "7d": {weighted: 9e6}}, CTO: {"24h": {weighted: 1e6}, "7d": {weighted: 2e6}}},
                 pool: {"24h": {weighted: 2e6}, "7d": {weighted: 5e6}},
                 pool_runs: {"24h": [{ref: "shal#1", weighted: 1.5e6}, {ref: "shal#2", weighted: 5e5},
                                     {ref: "a#3", weighted: 1e5}, {ref: "a#4", weighted: 1e4}], "7d": []},
                 last_limit: {what: "5-hour limit reached", reset: "2099-01-01T00:00:00Z", active: true}};
      const plan = {"5h": {percent: 62, resets_at: "2099-01-01T00:00:00Z", expired: false},
                    "7d": {percent: 41, resets_at: "2099-01-05T09:00:00Z", expired: false}, stale: true, age_min: 120};
      const card = renderTokens(t, plan, "24h");
      out.today = dump(card);
      const btns = card.querySelectorAll(".tseg button");
      out.pressed = btns.map((b) => [b.textContent, b.attrs["aria-pressed"]]);
      btns[1].onclick();
      out.tokView = tokView; out.stored = localStorage.getItem(TOKVIEW); out.rerendered = rerendered;
      out.week = dump(renderTokens(t, plan, "7d"));
      out.old = dump(renderTokens({agents: {COO: {"5h": {weighted: 1}}}}, null, "24h"));
      out.none = dump(renderTokens(null, null, "24h"));
    """)
    today = got["today"]
    assert got["pressed"] == [["היום", "true"], ["השבוע", "false"]]
    assert got["tokView"] == "7d" and got["stored"] == "7d" and got["rerendered"] == ["tok-7d"]
    assert _find(today, "big")[0]["text"] == "6.0M" and "טוקנים היום" in today["text"]
    role_bars = [b for b in _find(today, "tb") if "pool" not in b["cls"]]
    pool_bars = _find(today, "pool")
    assert [b["kids"][0]["text"] for b in role_bars] == ["COO", "CTO"]
    assert [b["kids"][0]["text"] for b in pool_bars] == ["pool · shal#1", "pool · shal#2", "pool · a#3"]  # top 3
    assert "הצג הכל (4)" in today["text"]
    assert "חלון 5 שעות: 62% מהמגבלה" in today["text"] and "שבועי: 41% מהמגבלה" in today["text"]
    assert "שורת הסטטוס לא רצה מאז" in today["text"]
    bdis = [k["text"] for k in _find(today, "warnline")[-1]["kids"] if k.get("tag") == "bdi"]
    assert bdis == ["5-hour limit reached", "2099-01-01T00:00:00Z"]      # QA on PR 112
    assert "טוקנים השבוע" in got["week"]["text"] and _find(got["week"], "big")[0]["text"] == "16.0M"
    assert "אין עדיין נתון להיום" in got["old"]["text"]
    assert "אין נתון תוכנית" in got["none"]["text"]


def test_week_card_is_one_big_number_in_hebrew():
    got = _dash("out.w = dump(renderWeek({points_closed: 34, points_closed_previous: 27, "
                "percent_of_company: 5.2, median_go_to_close_h: 1.3}));")
    assert _find(got["w"], "big")[0]["text"] == "34"
    assert "נקודות נסגרו (שבוע קודם: 27)" in got["w"]["text"] and "1.3h" in got["w"]["text"]


def test_progress_card_renders_stages_milestone_bars_chain_and_blocker():
    got = _dash("""
      out.full = dump(renderProgress({
        stages: [{he: "1. הוכח", current: false}, {he: "2. בשימוש", current: true}],
        milestones: [{repo: "shal", title: "v0.4.0", closed: 3, total: 4, percent: 75, url: "https://x/m1"}],
        chain: [{ref: "bricks#50", title: "psu", done: true, url: "https://x/50"},
                {ref: "shal#253", title: "pack", done: false, url: "https://x/253"}],
        blocker: {ref: "bricks#50", title: "psu", url: "https://x/50"},
      }));
      out.empty = dump(renderProgress(null));
    """)
    text = _text(got["full"])
    for s in ("1. הוכח", "2. בשימוש", "v0.4.0", "3/4", "75%", "bricks#50", "shal#253", "חוסם", "בוצע", "פתוח"):
        assert s in text, s
    assert _find(got["full"], "prog-chain")
    assert got["empty"]["text"] == "—"


def test_approvals_card_renders_cost_note_field_and_prev_next():
    got = _dash("""
      const moves = [
        {id: "a", q_he: "A?", why_he: "because a", cost_he: "0.1M", link: "https://github.com/determlab/shal/issues/231"},
        {id: "b", q_he: "B?", why_he: "because b", link: "https://x/2", deferred: true},
      ];
      const card = renderApprovals(moves);
      out.first = dump(card);
      card.querySelector(".appr-next").onclick();
      out.second = dump(mounted.node);
      out.one = dump(renderApprovals([moves[0]]));
      out.empty = dump(renderApprovals([]));
    """)
    text = _text(got["first"])
    assert "1 מתוך 2" in text and "A?" in text and "because a" in text and "0.1M" in text
    assert "shal#231" in text and '"cls": "appr-note"' in text
    assert "2 מתוך 2" in _text(got["second"]) and "נדחה קודם" in _text(got["second"])
    assert not _find(got["one"], "appr-nav")          # one card: no dead prev/next buttons
    assert "אין אישורים ממתינים" in _text(got["empty"])


def test_yes_no_later_open_a_ten_second_undo_window_before_sending():
    # The founder's Undo answer on #176: a click never sends right away.
    got = _dash("""
      const moves = [{id: "a", q_he: "A?", why_he: "because a", link: "https://x/1"}];
      const first = renderApprovals(moves);
      first.querySelector(".appr-note").value = "not today";
      [...first.querySelectorAll("button")].find((b) => b.textContent === "לא").onclick();
      out.pending = {id: approvalsPending.id, answer: approvalsPending.answer, note: approvalsPending.note};
      out.timerMs = timers[timers.length - 1].ms;
      out.mountedId = mounted.id;
      out.undo = dump(mounted.node);
    """)
    assert got["pending"] == {"id": "a", "answer": "no", "note": "not today"}
    assert got["timerMs"] == 10000 and got["mountedId"] == "dash-approvals"
    text = _text(got["undo"])
    assert "נדחה" in text and "A?" in text and "בטל" in text


def test_undo_clears_the_pending_decision_and_nothing_is_sent():
    got = _dash("""
      const moves = [{id: "a", q_he: "A?", why_he: "because a", link: "https://x/1"}];
      const first = renderApprovals(moves);
      [...first.querySelectorAll("button")].find((b) => b.textContent === "כן").onclick();
      mounted.node.querySelector("button").onclick();  // the undo bar's one button
      out.pendingAfterUndo = approvalsPending;
      out.cancelled = timers[timers.length - 1].cancelled;
      out.backToNormal = dump(mounted.node);
    """)
    assert got["pendingAfterUndo"] is None and got["cancelled"] is True
    assert "מתוך" in _text(got["backToNormal"])


def test_later_defers_and_a_typed_note_survives_a_re_render():
    got = _dash("""
      const moves = [{id: "a", q_he: "A?"}];
      const first = renderApprovals(moves);
      const note = first.querySelector(".appr-note");
      note.value = "half typed"; note.oninput();
      out.again = renderApprovals(moves).querySelector(".appr-note").value;   // a sync re-render
      [...first.querySelectorAll("button")].find((b) => b.textContent === "אחר כך").onclick();
      out.answer = approvalsPending.answer;
    """)
    assert got["again"] == "half typed" and got["answer"] == "later"


def test_commit_approval_posts_id_answer_and_note_then_reloads():
    got = _js(("commitApproval",), """
      await commitApproval("a", "no", "not today");
      out.calls = calls; out.loaded = loaded; out.toasts = toasts;
    """, prelude=API_STUB + FAKE_DOM + _consts("ANSWER_HE")
       + "\nlet loaded = false;\nasync function loadDashboard() { loaded = true; }\n")
    assert got["calls"] == [["POST", "dashboard/approve", {"id": "a", "answer": "no", "note": "not today"}]]
    assert got["loaded"] is True and got["toasts"] == [["נדחה: נשמר", False]]


def test_commit_approval_toasts_on_failure_but_still_reloads():
    got = _js(("commitApproval",), """
      FAIL = true;
      await commitApproval("a", "yes", "");
      out.toasts = toasts; out.loaded = loaded;
    """, prelude=API_STUB + FAKE_DOM + _consts("ANSWER_HE")
       + "\nlet loaded = false;\nasync function loadDashboard() { loaded = true; }\n")
    assert got["toasts"] and got["toasts"][0][1] is True and got["loaded"] is True


def test_pill_states():
    now_s = 1700000000
    got = _dash(f"""
      const now = {now_s} * 1000;
      out.fresh = pillText({{last_sync: {now_s} - 120, stale: false, last_error: null}}, now);
      out.stale = pillText({{last_sync: {now_s} - 10000, stale: true, last_error: null}}, now);
      out.never = pillText({{last_sync: null, stale: true, last_error: null}}, now);
      out.error = pillText({{last_sync: {now_s} - 120, stale: false, last_error: "gh: not logged in"}}, now);
      renderPill({{last_sync: {now_s} - 120, stale: false, last_error: null}}, now);
      out.freshClass = $("dash-pill").className;
      renderPill({{last_sync: {now_s} - 10000, stale: true, last_error: null}}, now);
      out.staleClass = $("dash-pill").className;
      renderPill({{last_sync: {now_s} - 120, stale: true, last_error: "gh: not logged in"}}, now);
      out.errorText = $("dash-pill-text").textContent;
      out.errorKids = $("dash-pill-text").childNodes.map((n) => n.tagName || "#text");
      out.errorClass = $("dash-pill").className;
    """)
    assert got["fresh"] == "מתעדכן אוטומטית · סנכרון אחרון לפני 2 דק׳ · מקור: GitHub"
    assert got["stale"] == "ישן: סנכרון אחרון לפני 3 שע׳ · מקור: GitHub"
    assert got["never"] == "עוד לא סונכרן · מקור: GitHub"
    assert got["error"] == "שגיאה: gh: not logged in"
    assert "stale" not in got["freshClass"].split() and "stale" in got["staleClass"].split()
    # An error replaces the line outright, red, the English part in its own <bdi>.
    assert got["errorText"] == got["error"] and got["errorKids"] == ["#text", "BDI"]
    assert "bad" in got["errorClass"].split() and "stale" not in got["errorClass"].split()


def test_panel_css_classes_from_the_mockup_are_present():
    # The mockup's own class names (B8UrALfZEZ34F1sPeJEiNz), kept unrenamed.
    css = _css()
    for cls in (".dhead", ".cards", ".card", ".card.wide", ".rows", ".ref", ".chip", ".chip.p0", ".chip.b",
                ".live", ".big", ".bar", ".role", ".st", ".sync", ".go", ".tseg", ".tb", ".tb.pool", ".plan",
                ".tl", ".tlin", ".lane", ".axis", ".ev.run", ".ev.mg", ".ev.dc", ".now", ".future", ".evlist",
                ".mk", ".kpi", ".more"):
        assert re.search(re.escape(cls) + r"[\s,{]", css), cls
    assert "repeat(auto-fill, minmax(min(290px, 100%), 1fr))" in _rule(".cards", css)
    assert "outline" in _rule("button:focus-visible, a:focus-visible, input:focus-visible, "
                              "textarea:focus-visible, select:focus-visible", css)


def test_the_switch_says_board_and_a_reload_opens_the_same_view():
    text = PAGE.read_text(encoding="utf-8")
    assert '<button type="button" id="view-dash" class="btn">לוח</button>' in text
    script = _markup().script
    set_view = re.search(r"function setView\(view\) \{.*?\n\}", script, re.S).group(0)
    assert "localStorage.setItem(VIEW, view)" in set_view
    assert re.search(r'setView\(.*localStorage\.getItem\(VIEW\).*=== "dash" \? "dash" : "chat"\)', script)


def test_every_board_control_is_wired():
    # No dead buttons (ops#220 item 8): each static control on the board has a
    # handler, and the board has its own theme button (#top is hidden there).
    script = _markup().script
    for bid in ("dash-sync", "dash-theme", "view-chat", "view-dash"):
        assert re.search(rf'\$\("{bid}"\)\.addEventListener\("click"', script), bid
    body = _function("renderTokens") + _function("cutList") + _function("renderApprovals")
    assert body.count(".onclick = ") >= 6


def test_the_sync_button_says_it_is_working_and_comes_back():
    script = _markup().script
    handler = script[script.index('$("dash-sync").addEventListener'):]
    handler = handler[:handler.index("});") + 3]
    assert 'b.textContent = "מסנכרן…"' in handler and 'b.textContent = "סנכרן עכשיו"' in handler
    assert "b.disabled = true" in handler and "b.disabled = false" in handler


def test_a_failed_fetch_keeps_the_last_good_document():
    got = _js(("loadDashboard",), """
      S.dashboard = {doc: {week: {points_closed: 3}}, last_sync: 10, stale: false, last_error: null};
      FAIL = true;
      await loadDashboard();
      out.d = S.dashboard;
    """, prelude=API_STUB + "const S = {dashboard: null};\nfunction renderDashboard() {}\n")
    assert got["d"] == {"doc": {"week": {"points_closed": 3}}, "last_sync": 10, "stale": True,
                        "last_error": "network down"}


# -- issue #168: unread bubbles


def test_168_unread_bubbles_render_next_to_streams_and_topics_hidden_when_zero():
    prelude = FAKE_DOM + """
      const CARET = "c", GEAR = "g", CHECK = "k", SVG = "http://www.w3.org/2000/svg";
      const S = {streams: [{stream_id: 1, name: "feature", invite_only: true},
                           {stream_id: 2, name: "quiet", invite_only: false}],
                 topics: new Map([["feature", new Map([["a", 5], ["b", 9]])],
                                  ["quiet", new Map([["c", 1]])]]),
                 unread: new Map([[1, {unread: 3, topics: new Map([["a", 3]])}]]),
                 collapsed: new Set(), open: null, adding: null};
    """
    got = _js(("svgIcon", "renderStreams", "newTopicItem", "el"), """
      renderStreams();
      const rows = $("streams").querySelectorAll(".stream-row");
      out.streamBadges = rows.map((r) => r.querySelectorAll(".badge").map((b) => b.textContent));
      const items = $("streams").querySelectorAll(".topic-row");
      out.topicBadges = items.map((it) => [it.querySelector(".topic").textContent,
                                           it.querySelectorAll(".badge").map((b) => b.textContent)]);
    """, prelude=prelude)
    # #feature has 3 unread, all in topic "a" — #quiet and topic "b" have none
    # and show no badge at all (hidden, not a "0").
    assert got["streamBadges"] == [["3"], []]
    assert got["topicBadges"] == [["b", []], ["a", ["3"]], ["c", []]]


def test_168_every_streams_topic_list_starts_collapsed_the_first_time_it_is_seen():
    # loadStreams() defaults a stream's fold to collapsed the first time it
    # appears (page load, or a brand-new subscription); a reconnect that
    # sees the same stream again must not re-collapse a fold the human chose.
    stub = """
      const calls = [];
      async function api(method, path, params) {
        calls.push([method, path, params]);
        if (path === "users/me/subscriptions") {
          return {subscriptions: [
            {stream_id: 1, name: "feature", invite_only: true, is_archived: false},
            {stream_id: 2, name: "later", invite_only: false, is_archived: false}]};
        }
        return {topics: []};
      }
      function renderStreams() {}
      function renderArchive() {}
      function closeTopic() {}
      const S = {streams: [], archivedStreams: [], topics: new Map(), archivedTopics: new Map(),
                 collapsed: new Set(), seenStreams: new Set(), open: null};
    """
    got = _js(("loadStreams",), """
      await loadStreams();
      out.firstLoad = [...S.collapsed].sort();
      S.collapsed.delete(1);   // the human expands #feature
      await loadStreams();     // a reconnect: the same two streams again
      out.afterReconnect = [...S.collapsed].sort();
    """, prelude=stub)
    assert got["firstLoad"] == [1, 2]
    assert got["afterReconnect"] == [2]


def test_168_mark_topic_read_zeroes_the_bubble_locally_and_tells_the_server():
    prelude = API_STUB + """
      const S = {unread: new Map([[1, {unread: 5, topics: new Map([["a", 2], ["b", 3]])}]])};
      let renders = 0;
      function renderStreams() { renders += 1; }
      function updateTitle() {}
    """
    got = _js(("markTopicRead",), """
      await markTopicRead(1, "A");
      out.calls = calls.splice(0);
      out.entry = {unread: S.unread.get(1).unread, topics: [...S.unread.get(1).topics.entries()]};
      out.renders = renders;
      await markTopicRead(1, "nothing-to-zero");
      out.calls2 = calls.splice(0);
      out.renders2 = renders;
    """, prelude=prelude)
    assert got["calls"] == [["POST", "mark_topic_as_read", {"stream_id": "1", "topic_name": "A"}]]
    # Case-insensitive: "A" clears the "a" entry, "b" is untouched.
    assert got["entry"] == {"unread": 3, "topics": [["b", 3]]}
    assert got["renders"] == 1
    # Nothing local to zero, but the server still gets told (idempotent).
    assert got["calls2"] == [["POST", "mark_topic_as_read",
                              {"stream_id": "1", "topic_name": "nothing-to-zero"}]]
    assert got["renders2"] == 1


def test_168_a_live_message_bumps_the_bubble_unless_its_topic_is_open_or_the_message_is_mine():
    prelude = API_STUB + """
      const S = {me: {user_id: 9}, open: {stream_id: 1, stream: "feature", topic: "open-topic"},
                 topics: new Map([["feature", new Map()]]), archivedTopics: new Map(),
                 unread: new Map(), typing: new Map()};
      let rendered = 0, titled = 0;
      function renderStreams() { rendered += 1; }
      function updateTitle() { titled += 1; }
      function renderArchive() {}
      function addMessages(list, toEnd) {}
      function renderTyping() {}
    """
    got = _js(("onMessage", "markTopicRead", "bumpUnread", "typingKey", "clearTyping",
              "notifyAllowed", "notifyMessage"), """
      const msg = (subject, id, sender) => ({type: "stream", stream_id: 1,
        display_recipient: "feature", subject, id, sender_id: sender});
      onMessage(msg("closed-topic", 1, 5));
      onMessage(msg("closed-topic", 2, 5));
      out.otherReaderUnread = [...S.unread.get(1).topics.entries()];
      onMessage(msg("closed-topic", 3, 9));  // my own message: never unread
      out.afterMine = S.unread.get(1).unread;
      await Promise.resolve();
      calls.length = 0;
      onMessage(msg("open-topic", 4, 5));    // arrives while its own topic is open
      await Promise.resolve();
      out.openTopicUnread = (S.unread.get(1).topics.get("open-topic") || 0);
      out.markReadCalls = calls;
      out.rendered = rendered; out.titled = titled;
    """, prelude=prelude)
    assert got["otherReaderUnread"] == [["closed-topic", 2]]
    assert got["afterMine"] == 2  # unchanged by my own message
    assert got["openTopicUnread"] == 0
    assert got["markReadCalls"] == [["POST", "mark_topic_as_read",
                                     {"stream_id": "1", "topic_name": "open-topic"}]]
    assert got["rendered"] > 0 and got["titled"] > 0


def test_168_the_tab_title_shows_the_total_unread_count():
    got = _js(("updateTitle",), """
      updateTitle();
      out.empty = document.title;
      S.unread.set(1, {unread: 2, topics: new Map()});
      S.unread.set(2, {unread: 1, topics: new Map()});
      updateTitle();
      out.some = document.title;
    """, prelude="const document = {}; const S = {unread: new Map()};")
    assert got["empty"] == "agora · צ'אט"
    assert got["some"] == "(3) Agora"


# -- reload consistency and mark-stream-read


def test_load_unread_replaces_local_state_with_exactly_what_the_server_says():
    # A reload starts with stale local counts (or none at all); loadUnread()
    # must throw them away and take the server's answer as-is, so a reload
    # never shows a count that disagrees with what mark_topic_as_read already
    # persisted server-side.
    prelude = """
      async function api(method, path) {
        if (path === "unread") return {streams: [{stream_id: 1, name: "feature", unread: 2,
          topics: [{name: "a", unread: 2}]}]};
        return {};
      }
      const S = {unread: new Map([[1, {unread: 99, topics: new Map([["stale", 99]])}],
                                  [2, {unread: 5, topics: new Map()}]])};
      function renderStreams() {}
      function updateTitle() {}
    """
    got = _js(("loadUnread",), """
      await loadUnread();
      out.streamIds = [...S.unread.keys()];
      out.feature = {unread: S.unread.get(1).unread, topics: [...S.unread.get(1).topics.entries()]};
    """, prelude=prelude)
    assert got["streamIds"] == [1]
    assert got["feature"] == {"unread": 2, "topics": [["a", 2]]}


def test_mark_read_button_shows_only_with_unread_and_clears_the_bubble_locally():
    prelude = FAKE_DOM + """
      const CARET = "c", GEAR = "g", CHECK = "k", SVG = "http://www.w3.org/2000/svg";
      const S = {streams: [{stream_id: 1, name: "feature", invite_only: true},
                           {stream_id: 2, name: "quiet", invite_only: false}],
                 topics: new Map([["feature", new Map([["a", 5]])], ["quiet", new Map()]]),
                 unread: new Map([[1, {unread: 3, topics: new Map([["a", 3]])}]]),
                 collapsed: new Set(), open: null, adding: null};
      let markedStream = null;
      async function api(method, path, params) {
        markedStream = [method, path, params];
        return {};
      }
      function updateTitle() {}
    """
    got = _js(("svgIcon", "renderStreams", "newTopicItem", "el", "markStreamRead"), """
      renderStreams();
      const rows = $("streams").querySelectorAll(".stream-row");
      out.hasButton = rows.map((r) => !!r.querySelector(".mark-read"));
      await $("streams").querySelectorAll(".mark-read")[0].on.click();
      out.markedStream = markedStream;
      const entry = S.unread.get(1);
      out.entryAfter = {unread: entry.unread, topics: [...entry.topics.entries()]};
    """, prelude=prelude)
    assert got["hasButton"] == [True, False]
    assert got["markedStream"] == ["POST", "mark_stream_as_read", {"stream_id": 1}]
    assert got["entryAfter"] == {"unread": 0, "topics": []}


def test_mark_stream_read_tells_the_server_even_with_nothing_local_to_zero():
    prelude = API_STUB + """
      const S = {unread: new Map()};
      let renders = 0;
      function renderStreams() { renders += 1; }
      function updateTitle() {}
    """
    got = _js(("markStreamRead",), """
      await markStreamRead(7);
      out.calls = calls;
      out.renders = renders;
    """, prelude=prelude)
    assert got["calls"] == [["POST", "mark_stream_as_read", {"stream_id": "7"}]]
    assert got["renders"] == 0  # nothing local to clear: no local entry, no re-render


# -- ops#236: phone pairing (the button, the QR, the ?pair= bootstrap)


def test_the_pair_button_sits_in_me_row_and_opens_a_dialog():
    m = _markup()
    ids = [a.get("id") for _, a in m.elements]
    assert ids.index("who") < ids.index("pair-btn") < ids.index("forget")
    [btn] = [a for t, a in m.elements if a.get("id") == "pair-btn"]
    assert btn.get("title")
    [dlg] = [(t, a) for t, a in m.elements if a.get("id") == "pair-dlg"]
    assert dlg[0] == "dialog"
    assert '$("pair-btn").addEventListener("click"' in m.script
    assert '$("pair-dlg").showModal()' in m.script
    assert '$("pair-close").addEventListener("click", () => $("pair-dlg").close())' in m.script


def test_the_pair_button_asks_the_server_and_draws_the_qr_it_answers_with():
    script = _markup().script
    assert 'await api("POST", "pair")' in script
    assert "renderQr($(\"pair-qr\")" in script
    assert 'say("pair-status", "שגיאה: " + e.message, true)' in script


def test_pair_redeem_bootstrap_uses_fetch_not_api_and_cleans_the_url():
    script = _markup().script
    # No auth yet to send: this goes through a plain fetch, never api().
    rp = _function("redeemPair")
    assert 'fetch("/api/v1/pair/redeem"' in rp
    assert "Authorization" not in rp
    assert 'new URLSearchParams(location.search).get("pair")' in script
    assert "await redeemPair(code)" in script
    assert 'localStorage.setItem(STORE, JSON.stringify(S.creds))' in script
    assert 'history.replaceState(null, "", location.pathname)' in script
    # A real reload would resubmit the one-time code.
    assert "location.reload(" not in script and "location.assign(" not in script


def test_the_qr_renders_as_an_svg_with_a_quiet_zone_and_dark_modules():
    script = _markup().script
    start = script.index("const SVG =")
    end = script.index("const S = {")
    got = _js((), """
      const svg = renderQr($("pair-qr"), "https://mypc.tail1234.ts.net/?pair=" + "x".repeat(32));
      out.tag = svg.tagName;
      out.kids = $("pair-qr").children.length;
      out.rects = svg.children.filter((c) => c.tagName === "RECT").length;
      out.viewBox = svg.attrs.viewBox;
      // Drawing again must clear the old QR, not pile a second one on top.
      renderQr($("pair-qr"), "https://mypc.tail1234.ts.net/?pair=short");
      out.kidsAfterRedraw = $("pair-qr").children.length;
    """, prelude=FAKE_DOM + script[start:end])
    assert got["tag"] == "SVG"
    assert got["kids"] == 1
    assert got["kidsAfterRedraw"] == 1
    assert got["rects"] > 50  # the white background plus plenty of dark modules
    assert got["viewBox"]


def test_pairing_a_phone_is_a_real_round_trip_through_the_server(tmp_path):
    """What the page's two fetches actually drive: POST /pair (Basic auth,
    the allowed Host/Origin) then POST /pair/redeem (no auth at all) gets
    back the same human's email and api_key. The QR drawing and the URL
    cleanup are covered separately above; node is not available here to
    drive a real browser."""
    # A local import: test_chat_allow_host imports `_request` from this very
    # module, so importing it back at module load time would be circular.
    from test_chat_allow_host import NAME, Served

    run = Served(str(tmp_path / "chat.sqlite3"), allow_hosts=[NAME])
    try:
        human = chat.add_human(run.store, HUMAN, "Test Human")
        status, _, raw = _request(run, "POST", "/api/v1/pair", user=human, Host=NAME,
                                  Origin=f"https://{NAME}")
        pair = json.loads(raw)
        assert status == 200, pair
        status, _, raw = _request(run, "POST", "/api/v1/pair/redeem", {"pair": pair["code"]},
                                  Host=NAME)
        body = json.loads(raw)
        assert status == 200, body
        assert body["email"] == human["email"]
        assert body["api_key"] == human["api_key"]
    finally:
        run.stop()

