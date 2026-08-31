"""The page is one file with no build step, so it cannot be unit-tested the way
the server can. What *is* worth checking mechanically is the seam: every URL the
page calls must be a route the server actually serves, and every element the
script reaches for must exist in the markup. Both of those break silently in a
browser and are invisible until someone clicks the thing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parent.parent / "static" / "index.html")


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_page_exists_and_is_self_contained(html: str):
    assert "<title>Agora</title>" in html
    # No build step and no CDN: the page must not reach off-box for anything.
    assert "http://" not in html.replace("http://127.0.0.1", "")
    assert "https://" not in html


#: Endpoints that never return: asserting anything about them by reading the
#: body would hang the suite. Their behaviour is covered in test_api.
STREAMS = {"stream"}


def test_the_state_endpoint_the_page_polls_is_served(server):
    assert server.get("/api/state")[0] == 200


def test_every_room_endpoint_the_page_calls_is_served(server, html: str):
    """Extracts the `/api/rooms/${cur}/...` tails out of the page and calls each
    one against a real room."""
    _, room = server.post("/api/rooms", {"title": "routes", "name": "Hemi"})
    rid = room["id"]
    tails = set(re.findall(r"/api/rooms/\$\{(?:cur|id|room\.id)\}/(\w+)", html))
    assert tails, "the page should be calling room endpoints"
    assert STREAMS <= tails, "the page should be using the room stream"
    for tail in sorted(tails - STREAMS):
        if tail in {"post", "note", "summary", "admin"}:
            status, _ = server.post(f"/api/rooms/{rid}/{tail}",
                                    {"name": "Hemi", "text": "x",
                                     "action": "prune"})
        elif tail == "export":
            status = server.raw(f"/api/rooms/{rid}/{tail}")[0]  # markdown, not JSON
        else:
            status, _ = server.get(f"/api/rooms/{rid}/{tail}", timeout=5)
        assert status != 404, (
            f"the page calls /{tail}, which the server does not serve")


def _admin_actions(html: str) -> set[str]:
    """Every action name the page can send. Written to survive the ternaries the
    toggles use — `admin(on ? "mute" : "unmute")` is two actions, not zero."""
    found: set[str] = set()
    for match in re.finditer(r'admin\(', html):
        found |= set(re.findall(r'["\'](\w+)["\']',
                                html[match.end():match.end() + 120]))
    found |= set(re.findall(r'action:\s*["\'](\w+)["\']', html))
    found |= set(re.findall(r'\{action:\s*\w+\s*\?\s*["\'](\w+)["\']\s*:\s*["\'](\w+)["\']',
                            html) and [] or [])
    return found


def test_every_admin_action_the_page_sends_is_understood(server, html: str):
    """A button wired to an action the server does not know fails silently in
    the browser — the request 400s and nothing on screen changes."""
    _, room = server.post("/api/rooms", {"title": "actions", "name": "Hemi"})
    rid = room["id"]
    known = {"mute", "unmute", "kick", "close", "reopen", "archive", "unarchive",
             "delete", "agenda", "title", "call", "prune", "ask_summary"}
    actions = _admin_actions(html) & known
    assert {"mute", "unmute", "kick", "close", "archive", "delete"} <= actions, \
        f"expected the chair controls to be wired; found {sorted(actions)}"
    for action in sorted(actions - {"delete"}):   # delete would destroy the fixture
        status, body = server.post(f"/api/rooms/{rid}/admin",
                                   {"action": action, "target": "nobody",
                                    "name": "Hemi", "text": "x"})
        assert "unknown admin action" not in (body or {}).get("error", ""), \
            f"the page sends admin action {action!r}, which the server rejects"


def test_every_element_the_script_reaches_for_exists(html: str):
    """`$("thing")` on a missing id returns null and the next line throws, which
    in a browser silently kills the rest of the handler."""
    ids = set(re.findall(r'\$\("([\w-]+)"\)', html))
    declared = set(re.findall(r'\sid="([\w-]+)"', html))
    # Ids created inside modal markup are addressed with querySelector, not $().
    missing = ids - declared
    assert not missing, f"script reaches for ids that are not in the markup: {missing}"


def test_every_pane_the_layout_hides_has_a_control_that_opens_it(html: str):
    """The narrow layouts drop a pane out of the grid entirely. If nothing on the
    page toggles it back, the pane and everything only reachable through it are
    gone at that width, and the page looks intact while it happens."""
    hidden = set(re.findall(r"\.pane\.(\w+)\s*\{display:none\}", html))
    assert hidden, "the narrow layouts should be hiding panes"
    toggled = set(re.findall(r"togglePane\(['\"](\w+)['\"]\)", html))
    assert hidden <= toggled, \
        f"nothing on the page opens the {sorted(hidden - toggled)} pane"
    for side in sorted(toggled):
        assert f'class="pane {side}"' in html, \
            f"a control toggles a {side!r} pane that the markup does not have"


def test_the_outside_click_dismiss_reads_the_dispatched_path(html: str):
    """Controls inside a pane (toggleArchive, toggleUnreachable) rewrite their
    container's innerHTML during their own onclick, which runs before this
    document listener. By then the clicked node is detached, so anything that
    walks the live tree from e.target sees no ancestors and shuts the pane the
    user is using. composedPath() is snapshotted at dispatch and survives that."""
    handler = re.search(r'document\.addEventListener\("click", e => \{(.*?)\n\}\);',
                        html, re.S)
    assert handler, "the outside-click dismiss handler is gone"
    body = handler.group(1)
    assert "composedPath()" in body, \
        "the dismiss handler must read composedPath(), not the live DOM"
    assert "e.target.closest" not in body, \
        "e.target.closest is detach-prone here: a re-rendered control has no " \
        "ancestors by the time this listener runs"
    assert '.mask' in body, \
        "an open modal should suppress the pane dismiss, as Escape already does"


def test_no_leftover_calls_to_removed_functions(html: str):
    for gone in ["refresh()", "agora_standby", "S.chair,"]:
        assert gone not in html, f"{gone} was removed but the page still calls it"


def test_the_page_handles_both_colour_schemes(html: str):
    assert "prefers-color-scheme:light" in html
    assert "prefers-reduced-motion" in html


def test_user_text_is_escaped_before_it_reaches_the_dom(html: str):
    """Everything in a room is written by someone else — participants, agents,
    room titles. innerHTML with raw text would be an injection."""
    assert 'const esc = s =>' in html
    # Spot-check the places that render other people's words.
    for fragment in ['${esc(e.author)}', '${esc(e.text)}', '${esc(r.title)}',
                     '${esc(s.name)}', '${esc(p.name)}']:
        assert fragment in html, f"unescaped render near {fragment}"


def test_the_transcript_paints_the_mentions_the_server_resolved(html: str):
    """The page must not decide what a mention is. If it scraped `@word` out of
    the text it would light up an address or a pasted snippet as a mention of
    somebody who is not in the room — the exact thing resolving server-side
    against the participant list prevents."""
    fn = re.search(r"function withMentions\(e\)\{(.*?)\n\}", html, re.S)
    assert fn, "the transcript should render mentions through withMentions"
    body = fn.group(1)
    assert "e.mentions" in body, \
        "the highlight must come from the server's list, not from the text"
    assert "esc(e.text)" in body, \
        "escape before inserting markup — every word here is somebody else's"
    assert "${withMentions(e)}" in html, "nothing calls withMentions"


def test_a_mention_of_you_looks_different_from_a_mention_of_someone_else(html: str):
    assert re.search(r"\.mention\{[^}]+\}", html), "mentions need a style"
    assert re.search(r"\.mention\.me\{[^}]+\}", html), \
        "a mention of you must be distinct from a mention of anyone else"
    assert 'class="mention${mine ? " me" : ""}"' in html


def test_the_composer_completes_a_name_from_the_room(html: str):
    assert 'id="mentionMenu"' in html
    assert "snap.participants.filter" in html, \
        "the completion list must come from the room's participants"
    assert "pickMention(" in html and "closeMentions()" in html


def test_the_composer_says_when_a_mention_woke_nobody(html: str):
    """The Call button's honesty rule. A mention of an idle session reaches it
    only when it next reads, and a silent success is the defect this app has
    shipped repeatedly."""
    send = re.search(r"const send = guard\(async \(\) => \{(.*?)\n\}\);", html, re.S)
    assert send, "the send handler is gone"
    assert "r.note" in send.group(1) and "toast(" in send.group(1), \
        "send must surface the server's note about who was not reached"
