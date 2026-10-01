"""ops#236: phone pairing, so the founder logs in on his phone (through
Tailscale, ``--allow-host``) without ever copying the API key. ``POST
/api/v1/pair`` (Basic auth, like any other endpoint) hands the PC a one-time
code and the allowed host to embed in a QR; ``POST /api/v1/pair/redeem`` (no
auth — the code itself is the credential) trades that code for
``{email, api_key}``, once, within five minutes, over the same Host the QR
points at. The code is stored only as a hash, one per user, and redeem is
rate-limited against guessing.
"""
from __future__ import annotations

import hashlib
import json
import time

from test_chat_allow_host import NAME, allowed, default  # noqa: F401  (fixtures)
from test_chat_messages import chat
from test_chat_page import _request


def _pair(run, human, **headers):
    """As the page itself would call it: the allowed Host and its https
    Origin, both overridable for the one test that has neither."""
    headers.setdefault("Host", NAME)
    headers.setdefault("Origin", f"https://{NAME}")
    status, _, raw = _request(run, "POST", "/api/v1/pair", user=human, **headers)
    return status, json.loads(raw)


def _redeem(run, code, host=NAME):
    status, _, raw = _request(run, "POST", "/api/v1/pair/redeem", {"pair": code}, Host=host)
    return status, json.loads(raw)


def test_pair_without_an_allow_host_names_the_fix(default):
    run, human = default
    status, _, raw = _request(run, "POST", "/api/v1/pair", user=human)
    body = json.loads(raw)
    assert status == 400, body
    assert body["code"] == "NO_ALLOW_HOST"
    assert "AGORA_ALLOW_HOST" in body["msg"] and "tailscale serve --bg 8095" in body["msg"]


def test_pair_with_an_allow_host_returns_a_code_and_that_host(allowed):
    run, human = allowed
    status, body = _pair(run, human)
    assert status == 200, body
    assert body["host"] == NAME.lower()
    assert body["expires_in"] == chat.PAIR_TTL
    assert isinstance(body["code"], str) and len(body["code"]) > 20


def test_only_the_codes_hash_is_stored(allowed):
    run, human = allowed
    _, body = _pair(run, human)
    code = body["code"]
    row = run.store.one("SELECT * FROM pairs WHERE user_id=?", (human["id"],))
    assert row is not None
    assert row["code_hash"] == hashlib.sha256(code.encode()).hexdigest()
    assert code not in row["code_hash"]


def test_redeem_with_the_right_code_and_host_needs_no_auth_at_all(allowed):
    run, human = allowed
    _, pair = _pair(run, human)
    status, body = _redeem(run, pair["code"])
    assert status == 200, body
    assert body["email"] == human["email"]
    assert body["api_key"] == human["api_key"]


def test_a_redeemed_code_is_single_use(allowed):
    run, human = allowed
    _, pair = _pair(run, human)
    status, body = _redeem(run, pair["code"])
    assert status == 200, body
    status, body = _redeem(run, pair["code"])
    assert status == 404, body
    assert body["code"] == "BAD_REQUEST"


def test_a_wrong_host_is_refused_but_does_not_burn_the_code(allowed):
    run, human = allowed
    _, pair = _pair(run, human)
    port = run.server.server_address[1]
    status, body = _redeem(run, pair["code"], host=f"127.0.0.1:{port}")
    assert status == 404, body
    # Not consumed by the wrong-Host attempt: the right Host still redeems it.
    status, body = _redeem(run, pair["code"], host=NAME)
    assert status == 200, body
    assert body["email"] == human["email"]


def test_an_unknown_code_is_a_plain_404(allowed):
    run, _ = allowed
    status, body = _redeem(run, "not-a-real-code")
    assert status == 404
    assert body["code"] == "BAD_REQUEST"


def test_an_expired_code_is_refused(allowed):
    run, human = allowed
    _, pair = _pair(run, human)
    old = int(time.time()) - chat.PAIR_TTL - 1
    run.store.x("UPDATE pairs SET created=? WHERE user_id=?", (old, human["id"]))
    status, body = _redeem(run, pair["code"])
    assert status == 404, body


def test_a_new_code_replaces_the_old_one(allowed):
    run, human = allowed
    _, first = _pair(run, human)
    _, second = _pair(run, human)
    assert first["code"] != second["code"]
    assert len(run.store.q("SELECT * FROM pairs WHERE user_id=?", (human["id"],))) == 1
    status, body = _redeem(run, first["code"])
    assert status == 404, body
    status, body = _redeem(run, second["code"])
    assert status == 200, body
    assert body["email"] == human["email"]


def test_redeem_is_rate_limited_per_address(allowed):
    run, human = allowed
    for _ in range(chat.PAIR_RATE_LIMIT):
        status, body = _redeem(run, "wrong-guess")
        assert status == 404, body
    _, pair = _pair(run, human)  # a real, otherwise-valid code
    status, body = _redeem(run, pair["code"])
    assert status == 429, body
    assert body["code"] == "RATE_LIMITED"
