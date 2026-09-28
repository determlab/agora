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
      out.plain = parseQuote("hi ```quote\\nx\\n```");
    """)
    assert got["given"] == {"sender": "CTO", "quote": "hi", "rest": "ok"}
    assert got["zulip"] == {"sender": "CTO", "quote": "שלום\nשני", "rest": ""}
    assert got["round"] == {"sender": "COO", "quote": "a\nb", "rest": "reply"}
    assert got["plain"] is None


def test_a_quote_does_not_wake_the_person_quoted(world):
    run, human = world["run"], world["human"]
    # What quoteBlock() writes: the silent @_** form, which is not a mention.
    assert '"@_**" + name + "** said:\\n```quote\\n"' in _function("quoteBlock")
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


def test_dialogs_are_in_the_page_and_there_is_no_stream_delete_or_unread_count():
    text = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"\b(confirm|prompt|alert)\(", _markup().script)
    assert "showModal()" in text and "<dialog" in text
    # Both need server work first (#74): no control that reaches nothing.
    assert 'api("DELETE", "streams' not in text and "messages/flags" not in text
    assert "unread" not in text.lower()


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
      x.parent = this;
      this.childNodes.push(x);
    }
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
     kids: n.childNodes.map(dump)}
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
      out.roles = [["CTO", true], ["coo", true], ["CMO", true], ["Watchdog", true],
                   ["Deploy bot", true], ["Hemi", false], ["cto", false], ["", false]]
        .map(([n, b]) => roleOf(n, b));
      out.again = roleOf("CTO", true) === roleOf(" CTO ", true);
      out.initials = ["hemi", "שרה", "  CTO", ""].map(initial);
      const a = avatar("Watchdog", true, true);
      out.av = [a.tagName, a.className, a.textContent];
    """, prelude=FAKE_DOM)
    assert got["roles"] == ["cto", "coo", "cmo", "watchdog", "bot", "human", "cto", "human"]
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
                 "Basic", "auth", "Enter", "Shift", "COO", "CTO", "CMO", "Watchdog", "all"}
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
    assert 'updatePick(); });' in script


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


# -- issue #82: the dashboard panel, side by side at 1280px, the sync pill


def test_page_registers_for_dashboard_events():
    # Registered up front, not added later: a queue that only ever asked for
    # "message" never gets woken by a sync (see chat/server.py's Dashboard).
    connect = _function("connect")
    assert re.search(r'event_types:\s*\["message",\s*"dashboard"\]', connect)
    assert "await loadDashboard()" in connect
    poll = _function("poll")
    assert 'ev.type === "dashboard"' in poll and "await loadDashboard()" in poll


def test_page_has_the_panel_and_the_switch():
    m = _markup()
    ids = {a.get("id") for _, a in m.elements}
    assert {"chat", "dash", "view-chat", "view-dash"} <= ids
    css = _css()
    assert "@media (min-width: 1280px)" in css
    assert "grid-template-columns: 1fr 1fr" in css
    # Each side scrolls on its own: the frame itself does not.
    assert "overflow: auto" in _rule("#dash", css)
    assert "overflow: hidden" in _rule("#app", css)


DASH_FIXTURE = {
    "live": [{"name": "CTO", "doing": "reviewing #82"}, {"name": "Hemi", "doing": "in the pool"}],
    "stuck": [
        {"who": "COO", "what": "PR #90 merge", "since": "2h", "waits_on": "founder"},
        {"who": "CMO", "what": "README review", "since": "1d", "waits_on": "CI"},
    ],
    "who_works": [
        {"name": "CTO", "on": "issue #82", "waits_on": "founder"},
        {"name": "Watchdog", "on": "issue #81", "waits_on": None},
    ],
    "roles": [{"name": "CTO", "role": "engineering"}, {"name": "COO", "role": "ops"}],
    "queue": [{"title": "issue #90", "priority": 2}, {"title": "issue #82", "priority": 1}],
    "prs": {"feature": [{"title": "PR #93"}], "fix": [{"title": "PR #92"}]},
    "week": ["3 issues closed", "2 PRs merged"],
    "products": [{"name": "agora", "status": "in development"}],
}

DASH_NAMES = ("dashRow", "dashList", "renderLive", "renderStuck", "renderWaitingForYou",
             "renderRoles", "renderQueue", "renderPRs", "renderWeek", "renderProducts",
             "pillText", "renderDashboardPanel")


def test_page_renders_every_card_from_a_fixture_document():
    prelude = FAKE_DOM + """
      const S = {dashboard: null};
    """
    got = _js(DASH_NAMES, f"""
      S.dashboard = {{doc: {json.dumps(DASH_FIXTURE)}, last_sync: 1700000000,
                      stale: false, last_error: null}};
      renderDashboardPanel();
      const rows = (id) => $(id).querySelector(".dash-list").children.map((li) => li.textContent);
      out.live = rows("card-live");
      out.waiting = rows("card-waiting");
      out.roles = rows("card-roles");
      out.queue = rows("card-queue");
      out.week = rows("card-week");
      out.products = rows("card-products");
      out.stuck = rows("card-stuck");
      out.prsHidden = $("card-prs-section").hidden;
      out.prs = $("card-prs").querySelector(".dash-prs").children
        .map((c) => [c.tagName, c.textContent]);
      // The prs section is the one that hides itself when the field is absent.
      S.dashboard = {{doc: {{...{json.dumps(DASH_FIXTURE)}, prs: undefined}}, last_sync: 1700000000,
                      stale: false, last_error: null}};
      renderDashboardPanel();
      out.prsHiddenWhenAbsent = $("card-prs-section").hidden;
    """, prelude=prelude)
    assert got["live"] == ["CTO · reviewing #82", "Hemi · in the pool"]
    # Only the founder's own rows, drawn from both stuck and who_works.
    assert got["waiting"] == ["COO · PR #90 merge", "CTO · issue #82"]
    assert got["roles"] == ["CTO · engineering", "COO · ops"]
    # Sorted by priority, not by the order the document listed them in.
    assert got["queue"] == ["issue #82 · עדיפות 1", "issue #90 · עדיפות 2"]
    assert got["week"] == ["3 issues closed", "2 PRs merged"]
    assert got["products"] == ["agora · in development"]
    # The plain "stuck" card is everyone, not filtered to the founder.
    assert got["stuck"] == ["COO · PR #90 merge · 2h", "CMO · README review · 1d"]
    assert got["prsHidden"] is False
    assert got["prs"] == [["DT", "feature"], ["DD", "PR #93"], ["DT", "fix"], ["DD", "PR #92"]]
    assert got["prsHiddenWhenAbsent"] is True


def test_pill_states():
    now = 1700000000
    fresh = {"last_sync": now - 180, "stale": False, "last_error": None}
    stale = {"last_sync": now - 8000, "stale": True, "last_error": None}
    error = {"last_sync": now - 180, "stale": False, "last_error": "gh: not logged in"}
    no_cmd = {"last_sync": None, "stale": True,
             "last_error": "no dashboard command configured: start the server with --dashboard-cmd"}
    got = _js(("pillText",), f"""
      const now = {now};
      out.fresh = pillText({json.dumps(fresh)}, now);
      out.stale = pillText({json.dumps(stale)}, now);
      out.error = pillText({json.dumps(error)}, now);
      out.noCmd = pillText({json.dumps(no_cmd)}, now);
    """)
    assert got["fresh"] == "מתעדכן אוטומטית · עודכן לפני 3 דק׳ · מקור: GitHub"
    assert got["stale"] == "מתעדכן אוטומטית · עודכן לפני 133 דק׳ · מקור: GitHub · לא עדכני"
    assert got["error"] == "gh: not logged in"
    assert got["noCmd"] == "no dashboard command configured: start the server with --dashboard-cmd"
    # Fresh, stale, error and no-command are four different texts.
    assert len({got["fresh"], got["stale"], got["error"], got["noCmd"]}) == 4


def test_sync_now_calls_post_sync_and_reloads_the_dashboard_even_on_failure():
    # loadDashboard() is real (it is what makes the pill's minute count move
    # after the POST); only api() is stubbed, so this runs the real handler.
    prelude = FAKE_DOM + API_STUB + """
      const S = {dashboard: null};
    """
    got = _js(("loadDashboard", "renderDashboardPanel", "dashRow", "dashList", "renderLive",
              "renderStuck", "renderWaitingForYou", "renderRoles", "renderQueue", "renderPRs",
              "renderWeek", "renderProducts", "pillText"), """
      const handler = async () => {
        try { await api("POST", "dashboard/sync"); } catch (e) { /* still reload */ }
        await loadDashboard();
      };
      await handler();
      out.ok = calls.splice(0).map((c) => [c[0], c[1]]);
      FAIL = true;
      await handler();
      out.failed = calls.splice(0).map((c) => [c[0], c[1]]);
    """, prelude=prelude)
    assert got["ok"] == [["POST", "dashboard/sync"], ["GET", "dashboard"]]
    assert got["failed"] == [["POST", "dashboard/sync"], ["GET", "dashboard"]]
    # The real handler, wired exactly this way: POST then loadDashboard() in `finally`.
    script = _markup().script
    handler = script[script.index('$("sync-now").addEventListener'):]
    handler = handler[:handler.index("});") + 3]
    assert 'api("POST", "dashboard/sync")' in handler
    assert handler.index("finally") > 0 and "await loadDashboard()" in handler[handler.index("finally"):]
