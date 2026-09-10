"""A tiny, stdlib-only client for the slice of Zulip's REST API this bot needs.

Why hand-rolled rather than `pip install zulip`: this repo's whole identity is
zero runtime dependencies (see docs/DECISIONS.md D2, `agora/mcp.py`). Zulip's
API is plain HTTP + HTTP Basic Auth + JSON/form bodies — entirely reachable
with `urllib.request`. The one place the official client would have bought
something is long-poll resume semantics (`register` + `GET /json/events`),
and that is exactly what `tests/test_resume.py` exists to pin down: the
"resume from last_event_id, never drop or duplicate" contract is small enough
to hand-write and test without a live server.

No dependency was added. If this judgment turns out wrong, that is a decision
for a human, not something to silently reach for later (see the issue).
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ZulipError(RuntimeError):
    """Raised when Zulip's API returns result != "success"."""


class ZulipClient:
    """Minimal REST client: register a queue, long-poll events, send messages."""

    def __init__(self, site: str, email: str, api_key: str, timeout: float = 90.0):
        self.site = site.rstrip("/")
        self._auth_header = self._basic_auth_header(email, api_key)
        self.timeout = timeout

    @staticmethod
    def _basic_auth_header(email: str, api_key: str) -> str:
        token = base64.b64encode(f"{email}:{api_key}".encode()).decode()
        return f"Basic {token}"

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.site}/api/v1/{path.lstrip('/')}"
        body: bytes | None = None
        if params is not None:
            # Zulip's REST API takes form-encoded params for POST, and query
            # string for GET; list/dict values must be JSON-encoded strings.
            encoded = {
                k: json.dumps(v) if isinstance(v, (list, dict)) else str(v)
                for k, v in params.items()
                if v is not None
            }
            qs = urllib.parse.urlencode(encoded)
            if method == "GET":
                url = f"{url}?{qs}"
            else:
                body = qs.encode()

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self._auth_header)
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read().decode())

        if payload.get("result") != "success":
            raise ZulipError(f"{method} {path} failed: {payload}")
        return payload

    # -- the three calls the bot needs -----------------------------------

    def register(
        self, event_types: list[str], narrow: list[list[str]] | None = None
    ) -> dict[str, Any]:
        """POST /register. Returns a payload with queue_id and last_event_id.

        Per Zulip's docs, once registered the server queues events for this
        queue_id on its own — a bot that stops calling /events does not lose
        anything until the queue expires (idle timeout, default ~10 minutes),
        which is the whole premise this issue is testing.
        """
        params: dict[str, Any] = {"event_types": event_types}
        if narrow:
            params["narrow"] = narrow
        return self._request("POST", "register", params)

    def get_events(
        self,
        queue_id: str,
        last_event_id: int,
        dont_block: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /events — long-polls until an event arrives (or times out)
        unless dont_block is set. Resumes strictly after last_event_id.
        """
        payload = self._request(
            "GET",
            "events",
            {
                "queue_id": queue_id,
                "last_event_id": last_event_id,
                "dont_block": "true" if dont_block else "false",
            },
        )
        return payload["events"]

    def send_message(
        self, to: str, content: str, topic: str = "general", type_: str = "stream"
    ) -> dict[str, Any]:
        params = {"type": type_, "to": to, "content": content}
        if type_ == "stream":
            params["topic"] = topic
        return self._request("POST", "messages", params)

    # -- admin calls, added for bot/setup_streams.py (issue #40) ----------
    #
    # These need an *admin* account's credentials, not a bot's — creating
    # streams and subscribing other users are realm-admin actions. Kept on
    # the same client rather than a second class because they are the same
    # three primitives (GET/POST/DELETE against /api/v1/*) with different
    # endpoints, and setup_streams.py is the only caller.

    def list_streams(self) -> list[dict[str, Any]]:
        """All active streams in the realm (admin-only param), not just the
        caller's own subscriptions — needed to check "does it exist" without
        first subscribing to it."""
        payload = self._request("GET", "streams", {"include_all_active": "true"})
        return payload["streams"]

    def create_stream(self, name: str) -> None:
        """Zulip has no separate "create stream" call: subscribing to a name
        that doesn't exist yet creates it (as a public stream, the default).
        This subscribes the admin account too — harmless, and it is what
        lets the admin see/manage the stream afterwards."""
        self._request("POST", "users/me/subscriptions", {"subscriptions": [{"name": name}]})

    def list_users(self) -> list[dict[str, Any]]:
        """Every account in the realm, bots included (`is_bot`) — used to
        check whether a bot already exists before creating it."""
        payload = self._request("GET", "users")
        return payload["members"]

    def create_bot(self, full_name: str, short_name: str) -> dict[str, Any]:
        """POST /bots. `short_name` becomes the email's local part
        (`<short_name>-bot@<realm>`); `bot_type=1` is Zulip's "generic bot"
        type — the only kind this repo needs, no incoming webhook."""
        return self._request(
            "POST", "bots", {"full_name": full_name, "short_name": short_name, "bot_type": 1}
        )

    def subscribe(self, stream: str, principals: list[str]) -> None:
        """Subscribe other accounts (`principals`, a list of emails) to an
        existing stream. Also admin-only; a bot cannot subscribe itself to a
        stream it isn't in, by design (that's the point of this issue)."""
        self._request(
            "POST",
            "users/me/subscriptions",
            {"subscriptions": [{"name": stream}], "principals": principals},
        )

    def stream_subscribers(self, stream_id: int) -> list[int]:
        """GET /streams/{id}/members — the user ids subscribed to a stream.
        This is how --check confirms a bot is subscribed to its own stream
        and nowhere else, without trusting a subscribe call's own report."""
        payload = self._request("GET", f"streams/{stream_id}/members")
        return payload["subscribers"]
