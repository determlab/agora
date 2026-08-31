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
