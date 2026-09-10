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
