"""ops#325: the chat on a phone — long messages fold, every action is visible
without hover, the compose row stays above the browser bar and the keyboard,
no zoom on focus, a topic opens at its first unread message, a one-row top
bar, 44px tap targets, and "+" (not "👍+") only on the tapped message.

Checked the way test_chat_page checks the page: its CSS rules, its markup,
and its own functions run in node over the small stand-in DOM.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from test_chat_page import FAKE_DOM, PAGE, Text, _css, _function, _js, _markup, _rule


def _media(query: str) -> str:
    """Every rule inside ``@media (query) { ... }``, all such blocks joined."""
    css, out = _css(), []
    for found in re.finditer(r"@media " + re.escape(query) + r"\s*\{", css):
        depth, i = 1, found.end()
        for j in range(i, len(css)):
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            if depth == 0:
                out.append(css[i:j])
                break
    assert out, query
    return "\n".join(out)


def _const(name: str) -> str:
    found = re.search(rf"^const {name} = [^;]*;", _markup().script, re.MULTILINE)
    assert found, name
    return found.group(0)


class _Tpl(HTMLParser):
    """The message template as JS that builds it in the stand-in DOM."""

    def __init__(self):
        super().__init__()
        self.js, self.stack, self.n = [], ["root"], 0

    def handle_starttag(self, tag, attrs):
        self.n += 1
        v = f"e{self.n}"
        self.js.append(f"const {v} = document.createElement({json.dumps(tag)});")
        for k, val in attrs:
            if k == "class":
                self.js.append(f"{v}.className = {json.dumps(val)};")
            elif k == "hidden":
                self.js.append(f"{v}.hidden = true;")
            elif k in ("dir", "type", "title"):
                self.js.append(f"{v}.{k} = {json.dumps(val)};")
            else:
                self.js.append(f"{v}.setAttribute({json.dumps(k)}, {json.dumps(val or '')});")
        self.js.append(f"{self.stack[-1]}.append({v});")
        self.stack.append(v)

    def handle_endtag(self, tag):
        self.stack.pop()

    def handle_data(self, data):
        if data.strip():
            self.js.append(f"{self.stack[-1]}.append({json.dumps(data.strip())});")


def _template_js() -> str:
    tpl = re.search(r'<template id="msg-tpl">(.*?)</template>',
                    PAGE.read_text(encoding="utf-8"), re.DOTALL).group(1)
    p = _Tpl()
    p.feed(tpl)
    return ("function buildMsg() { const root = document.createElement('div');\n"
            + "\n".join(p.js) + "\nreturn root.children[0]; }\n"
            "$('msg-tpl').content = {firstElementChild: {cloneNode: () => buildMsg()}};\n")


MSG_FUNCS = ("el", "isLong", "textDir", "parseQuote", "inlineParts", "inlineNodes", "fillBody",
             "roleOf", "initial", "renderReactions", "wireReactBtn", "tapMessage", "messageNode")


def _msg_prelude(extra: str = "") -> str:
    return (FAKE_DOM + _const("FOLD_LINES") + "\n" + _const("FOLD_CHARS") + "\n" + _template_js()
            + """
      const matchOne = El.prototype.matches;
      El.prototype.matches = function (sel) { return sel.split(",").some((x) => matchOne.call(this, x.trim())); };
      const S = {me: {user_id: 1}, users: new Map(), unfolded: new Set(), tapped: null, newFrom: null, msgs: []};
      function openReactPicker() {}
      function insertText() {}
      function jumpToQuote() {}
      function msg(id, content) {
        return {id, content, sender_id: 2, sender_full_name: "CTO", timestamp: 1760000000, reactions: []};
      }
    """ + extra)


# -- 1. long messages fold


def test_325_a_40_line_message_shows_6_lines_and_opens_in_place_on_tap():
    got = _js(MSG_FUNCS, """
      const long = Array.from({length: 40}, (_, i) => "line " + i).join("\\n");
      const node = messageNode(msg(5, long), new Date(), true);
      const body = node.querySelector(".body");
      const more = node.querySelector(".bubble").querySelector(".more-btn");
      out.before = [body.classList.contains("folded"), more && more.textContent];
      more.on.click();
      out.after = [body.classList.contains("folded"), !!node.querySelector(".more-btn"), [...S.unfolded]];
      // A re-render (a live message arrives) keeps it open.
      const again = messageNode(msg(5, long), new Date(), true);
      out.again = [again.querySelector(".body").classList.contains("folded"), !!again.querySelector(".more-btn")];
      const short = messageNode(msg(6, "שלום\\nעוד שורה"), new Date(), true);
      out.short = [short.querySelector(".body").classList.contains("folded"), !!short.querySelector(".more-btn")];
      out.chars = [isLong("x".repeat(400)), isLong("x".repeat(401)), isLong("a\\nb\\nc\\nd\\ne\\nf"),
                   isLong("a\\nb\\nc\\nd\\ne\\nf\\ng")];
    """, prelude=_msg_prelude())
    assert got["before"] == [True, "הצג עוד"]
    assert got["after"] == [False, False, [5]]
    assert got["again"] == [False, False]
    assert got["short"] == [False, False]
    assert got["chars"] == [False, True, False, True]
    css = _css()
    folded = _rule(".body.folded", css)
    assert "max-height: calc(6 * 1.45em)" in folded and "overflow: hidden" in folded
    assert "max-height: calc(3 * 1.45em" in _rule(".body.folded pre", css)
    # Opening it in place grows the bubble downward and moves no scroll position.
    fold = _function("messageNode")
    fold = fold[fold.index("isLong("):fold.index("S.tapped === m.id")]
    assert "scrollTop" not in fold and "scrollIntoView" not in fold


def test_325_a_long_message_that_fits_in_6_lines_drops_its_button():
    got = _js(("el", "dayLabel", "renderMessages"), """
      const box = $("messages");
      renderMessages();
      out.folded = box.querySelectorAll(".body.folded").length;
      out.buttons = box.querySelectorAll(".more-btn").length;
    """, prelude=FAKE_DOM + """
      function messageNode(m) {
        const n = document.createElement("div");
        const bubble = document.createElement("div");
        bubble.className = "bubble";
        const body = document.createElement("div");
        body.className = "body folded";
        body.clientHeight = 87; body.scrollHeight = m.id === 1 ? 60 : 300;
        const more = document.createElement("button");
        more.className = "more-btn";
        bubble.append(body, more);
        n.append(bubble);
        return n;
      }
      const S = {newFrom: null, msgs: [{id: 1, timestamp: 1, sender_id: 2}, {id: 2, timestamp: 2, sender_id: 2}]};
    """)
    assert got == {"folded": 1, "buttons": 1}


def test_325_folding_measures_every_body_before_it_unfolds_any():
    # CTO nit on PR 138: a read after a class change forces a reflow per message.
    got = _js(("el", "dayLabel", "renderMessages"), """
      renderMessages();
      out.log = log.join("");
    """, prelude=FAKE_DOM + """
      const log = [];
      function messageNode(m) {
        const n = document.createElement("div");
        const bubble = document.createElement("div");
        bubble.className = "bubble";
        const body = document.createElement("div");
        let cls = "body folded";
        Object.defineProperty(body, "className", {
          get: () => cls, set: (v) => { if (v !== cls) log.push("w"); cls = v; }});
        Object.defineProperty(body, "clientHeight", {get: () => { log.push("r"); return 87; }});
        Object.defineProperty(body, "scrollHeight", {get: () => { log.push("r"); return 60; }});
        bubble.append(body);
        n.append(bubble);
        return n;
      }
      const S = {newFrom: null, msgs: [1, 2, 3].map((id) => ({id, timestamp: id, sender_id: 2}))};
    """)
    assert re.fullmatch(r"r+w{3}", got["log"]), got["log"]


# -- 2. no action waits for a hover; an archive by a stray tap is undone


def test_325_on_a_touch_screen_every_row_and_message_action_is_visible():
    hover_none = _media("(hover: none)")
    shown = _rule(".gear, .mark-read, .topic-archive, .quote-btn", hover_none)
    assert "opacity: 1" in shown
    # The topic name fills the row, so a tap on the row's empty end opens the
    # topic; only the visible "ארכב" archives.
    assert "flex: 1" in _rule(".topic-row .topic")


def test_325_archiving_a_topic_from_its_row_offers_undo_that_restores_it():
    prelude = FAKE_DOM + """
      const CARET = "c", GEAR = "g", CHECK = "k", SVG = "http://www.w3.org/2000/svg";
      function openTopic() {}
      function openMembers() {}
      function markStreamRead() {}
      const actions = [], server = [];
      function archiveAction(done, call, undo) { actions.push({done, call, undo}); }
      function archiveTopic(id, t) { server.push(["archive", id, t]); }
      function restoreTopic(id, t) { server.push(["restore", id, t]); }
      const S = {streams: [{stream_id: 1, name: "feature", invite_only: true}],
                 topics: new Map([["feature", new Map([["a", 5]])]]), unread: new Map(),
                 collapsed: new Set(), open: null, adding: null};
    """
    got = _js(("el", "svgIcon", "renderStreams", "newTopicItem"), """
      renderStreams();
      $("streams").querySelector("button.topic-archive").on.click();
      const [first] = actions;
      first.call();
      first.undo();
      actions[1].call();
      out.done = [first.done, actions[1].done];
      out.server = server;
    """, prelude=prelude)
    assert got["server"] == [["archive", 1, "a"], ["restore", 1, "a"]]
    assert got["done"] == ["#feature › a הועבר לארכיון", "#feature › a שוחזר"]


def test_325_the_archive_toast_has_an_undo_button_that_runs_it():
    real = FAKE_DOM.replace("const toasts = [];\nfunction toast(text, bad) { toasts.push([text, !!bad]); }\n", "")
    got = _js(("el", "toast", "archiveAction"), """
      let undone = 0;
      await archiveAction("#feature › a הועבר לארכיון", async () => {}, () => { undone++; });
      const t = $("toast");
      const b = t.querySelector("button.toast-act");
      out.shown = [t.textContent, t.hidden, timers[0][1]];
      b.on.click();
      out.after = [undone, t.hidden];
      await archiveAction("#feature שוחזר", async () => {});
      out.plain = [!!t.querySelector("button.toast-act"), timers[1][1]];
    """, prelude=real + """
      const timers = [];
      const setTimeout = (f, ms) => { timers.push([f, ms]); return timers.length; };
      const clearTimeout = () => {};
      async function loadStreams() {}
      const S = {toastTimer: 0};
    """)
    assert got["shown"] == ["#feature › a הועבר לארכיוןבטל", False, 7000]
    assert got["after"] == [1, True]
    assert got["plain"] == [False, 3500]


# -- 3. the compose row and the top bar stay inside the visible screen


def test_325_the_app_fits_the_visible_viewport_and_the_safe_areas():
    meta = [a for t, a in _markup().elements if t == "meta" and a.get("name") == "viewport"]
    assert "viewport-fit=cover" in meta[0]["content"]
    app = _rule("#app")
    assert app.index("height: calc(100vh - 2rem)") < app.index("height: calc(100dvh - 2rem)")
    small = _media("(max-width: 700px)")
    phone = _rule("#app", small)
    assert phone.index("height: 100vh") < phone.index("height: 100dvh")
    assert "padding-top: env(safe-area-inset-top" in phone
    assert "top: env(safe-area-inset-top" in _rule("#side", small)
    assert "padding-bottom: calc(.5rem + env(safe-area-inset-bottom" in _rule("#write")


# -- 4. no zoom on focus


def test_325_every_field_is_16px_on_a_touch_screen_so_ios_does_not_zoom():
    coarse = _media("(pointer: coarse)")
    rule = _rule("input, select, textarea, #compose, .appr-note", coarse)
    assert "font-size: 16px" in rule
    # Last in the stylesheet, so it wins over .appr-note's own 13px.
    css = _css()
    assert css.rindex("@media (pointer: coarse)") > css.index(".appr-note {")


# -- 5. a topic opens at its first unread message


def test_325_the_first_new_message_is_the_nth_last_from_someone_else():
    got = _js(("firstNewId",), """
      const l = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((id) => ({id, sender_id: id === 9 ? 1 : 2}));
      out.r = [firstNewId(l, 0, 1), firstNewId(l, 7, 1), firstNewId(l, 1, 1), firstNewId(l, 50, 1),
               firstNewId([], 3, 1), firstNewId([...l].reverse(), 2, 1)];
    """)
    # 7 unread from others, and the reader's own id 9 in between: from id 3.
    assert got["r"] == [None, 3, 10, 1, None, 8]


def test_325_a_topic_with_7_unread_opens_with_the_new_line_at_the_top():
    got = _js(("el", "dayLabel", "renderMessages", "scrollToNew"), """
      const box = $("messages");
      renderMessages();
      const kids = box.children;
      const at = kids.findIndex((k) => k.className === "new-line");
      out.line = [kids[at].textContent, kids[at].dir, kids[at + 1].dataset.id, kids[at + 1].className,
                  kids.slice(at + 1).length];
      // Line 300px below the box's top, box already scrolled 1000px: lift it to the top.
      box.scrollTop = 1000;
      box.getBoundingClientRect = () => ({top: 50});
      kids[at].getBoundingClientRect = () => ({top: 350});
      scrollToNew();
      out.scroll = box.scrollTop;
    """, prelude=FAKE_DOM + """
      function messageNode(m, when, first) {
        const n = document.createElement("div");
        n.className = "msg" + (first ? " first" : "");
        n.dataset.id = String(m.id);
        return n;
      }
      const S = {newFrom: 4, msgs: Array.from({length: 10}, (_, i) => ({id: i + 1, timestamp: 1760000000, sender_id: 2}))};
    """)
    assert got["line"] == ["חדש", "rtl", "4", "msg first", 7]
    assert got["scroll"] == 1300
    open_ = _function("openTopic")
    # The count is read before markTopicRead clears it; the line is placed
    # after the bottom-scroll that addMessages does.
    assert open_.index("const unreadN") < open_.index("markTopicRead(streamId, topic)")
    assert open_.index("S.newFrom = firstNewId(got.messages, unreadN") < open_.index("addMessages(got.messages, true)")
    assert open_.index("addMessages(got.messages, true)") < open_.index("scrollToNew()")
    assert "S.newFrom = null" in _function("closeTopic")
    assert "S.unfolded.clear()" in _function("closeTopic")  # no session-long growth
    assert "var(--accent)" in _rule(".new-line")


# -- 6. a one-row top bar on the phone


def test_325_under_700px_the_top_bar_is_one_row_with_room_for_the_name():
    small = _media("(max-width: 700px)")
    assert "display: none" in _rule("#live-text", small)
    switch = _rule("#view-switch", small)
    assert "position: absolute" in switch and "inset-inline-end" in switch
    assert "padding-inline-end" in _rule("#top", small)
    # The bar height and the switch width are set once and drive every coupled rule.
    assert "--top-h: 3.75rem" in _rule("#app", small) and "--switch-w: 7.5rem" in _rule("#app", small)
    assert "var(--top-h)" in _rule("#top", small) and "var(--switch-w)" in _rule("#top", small)
    assert "var(--top-h) / 2" in switch
    assert "var(--switch-w)" in _rule(".dhead h2", small)
    assert not re.search(r"1\.875rem|8rem|7\.5rem;", _rule("#top", small) + switch + _rule(".dhead h2", small))
    got = _js(("placeTools",), """
      for (const id of ["top", "side-tools", "members-btn", "notify-btn", "theme-btn"]) $(id).id = id;
      placeTools();
      out.small = [$("side-tools").children.map((c) => c.id), $("top").children.length];
      SMALL.matches = false;
      placeTools();
      out.wide = [$("side-tools").children.length, $("top").children.length];
    """, prelude=FAKE_DOM + "const SMALL = {matches: true};")
    assert got["small"] == [["members-btn", "notify-btn", "theme-btn"], 0]
    assert got["wide"] == [0, 3]
    script = _markup().script
    assert 'const SMALL = matchMedia("(max-width: 700px)")' in script
    assert 'SMALL.addEventListener("change", placeTools)' in script
    # The dot alone still says what it means.
    assert '$("live").title = text' in _function("setLive")


# -- 7. 44px tap targets


def test_325_every_sidebar_top_bar_and_message_button_is_44px_on_touch():
    coarse = _media("(pointer: coarse)")
    sel = (".btn, .icon, .topic, .stream-name, .new-topic, .archived-name, .reaction-chip, "
           ".react-picker button,\n  .more-btn")
    assert "min-height: 44px" in _rule(sel, coarse)
    assert "min-width: 44px" in _rule(".icon, .react-btn, .reaction-chip", coarse)
    assert "gap: 8px" in _rule("#top, #me-row, #side-tools, .stream-row, .topic-row, .line, .reactions", coarse)
    # Every button in the top bar, the sidebar head and the message template
    # carries one of those classes (the sidebar rows are built as .stream-name,
    # .topic, .icon and .new-topic by renderStreams).
    ids = {"menu-btn", "members-btn", "notify-btn", "theme-btn", "pair-btn", "forget",
           "archive-btn", "new-stream-btn", "view-chat", "view-dash", "send-btn"}
    found = {}
    for tag, a in _markup().elements:
        if tag == "button" and (a.get("id") in ids or "quote-btn" in (a.get("class") or "")
                                or "react-btn" in (a.get("class") or "")):
            found[a.get("id") or a["class"]] = set(a["class"].split())
    assert ids <= found.keys() and len(found) == len(ids) + 2
    for key, cls in found.items():
        assert cls & {"btn", "icon"}, key
    rows = _function("renderStreams")
    for cls in ('"stream-name"', '"topic"', '"icon gear"', '"icon mark-read"', '"icon topic-archive"'):
        assert f"className = {cls}" in rows, cls


# -- 8. "+" only on the tapped message, no keyboard hint on a phone


def test_325_the_react_button_is_a_plain_plus_shown_only_on_the_tapped_message():
    tpl = re.search(r'<template id="msg-tpl">(.*?)</template>', PAGE.read_text(encoding="utf-8"), re.DOTALL).group(1)
    btn = re.search(r'<button[^>]*react-btn[^>]*>(.*?)</button>', tpl, re.DOTALL)
    assert btn.group(1) == "+" and 'aria-label="הוסף תגובה"' in btn.group(0)
    assert "&#128077;" not in tpl and "👍" not in tpl
    assert "display: none" in _rule(".msg:not(.tapped) .react-btn", _media("(hover: none)"))
    got = _js(MSG_FUNCS, """
      const nodes = [1, 2, 3, 4, 5].map((id) => messageNode(msg(id, "hi " + id), new Date(), true));
      for (const n of nodes) $("messages").append(n);
      const tap = (n, target) => n.querySelector(".bubble").on.click({target: target || n.querySelector(".body")});
      const state = () => nodes.map((n) => n.classList.contains("tapped"));
      tap(nodes[2]);
      out.one = state();
      tap(nodes[4]);
      out.other = state();
      tap(nodes[4]);
      out.again = state();
      // A tap on a link inside the bubble follows the link, nothing else.
      const a = document.createElement("a");
      nodes[1].querySelector(".body").append(a);
      tap(nodes[1], a);
      out.link = state();
      // A re-render keeps the tapped one.
      tap(nodes[0]);
      out.kept = messageNode(msg(1, "hi 1"), new Date(), true).classList.contains("tapped");
      out.thumbs = nodes.map((n) => n.textContent).join("").includes("👍");
    """, prelude=_msg_prelude())
    assert got["one"] == [False, False, True, False, False]
    assert got["other"] == [False, False, False, False, True]
    assert got["again"] == [False] * 5
    assert got["link"] == [False] * 5
    assert got["kept"] is True and got["thumbs"] is False


def test_325_the_keyboard_hint_is_gone_on_touch_and_starts_with_hebrew_on_desktop():
    assert "display: none" in _rule("#hint", _media("(pointer: coarse)"))
    p = Text()
    p.feed(PAGE.read_text(encoding="utf-8"))
    [hint] = [c for c in p.chunks if "Shift+Enter" in c]
    # A leading "@" is a neutral char: in an RTL line it ran off to the far end.
    assert re.match(r"[֐-׿]", hint) and "@" in hint


def test_325_the_hint_and_the_mic_privacy_line_both_sit_under_the_composer():
    # Merge with #131 (ops#323): its #mic-note follows this PR's #hint, both kept.
    page = PAGE.read_text(encoding="utf-8")
    assert page.count('id="hint"') == 1 and page.count('id="mic-note"') == 1
    hint = page.index('id="hint"')
    assert hint < page.index('id="mic-note"') < page.index('id="send-error"')
    assert "הקלדת @" in page[hint : page.index("</p>", hint)]


# -- seen in the browser: RTL sidebar alignment


def test_325_latin_and_hebrew_topic_names_start_at_the_same_right_edge():
    assert "text-align: right" in _rule(".topic")
    assert "text-align: right" in _rule(".stream-label")
    assert 'btn.dir = "auto"' in _function("renderStreams")
