"""Resume logic for the long-poll listener, split out so it is unit-testable
without a live Zulip instance (mock the HTTP layer, exercise this).

The contract this exists to prove: given a batch of events fetched from
`GET /json/events`, the listener must
  - advance `last_event_id` to the highest id seen, so the next poll resumes
    strictly after it (never re-fetches, never skips) — Zulip's docs warn
    that supplying anything but the highest-seen id can leave gaps;
  - only echo `message` events, and never echo the bot's own messages, or it
    would echo itself forever;
  - preserve arrival order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Echo:
    event_id: int
    content: str


def advance_last_event_id(events: list[dict[str, Any]], last_event_id: int) -> int:
    """last_event_id must track the max id seen across ALL events, not just
    message events — a non-message event (e.g. a heartbeat) still has an id
    that must be resumed past, or the next /events call re-fetches it.
    Safe to call with an empty list (a dont_block poll that found nothing).
    """
    if not events:
        return last_event_id
    return max([last_event_id] + [e["id"] for e in events])


def message_events(events: list[dict[str, Any]]) -> list[Echo]:
    """Every message event, in arrival order, regardless of sender."""
    return [
        Echo(event_id=e["id"], content=e["message"].get("content", ""))
        for e in events
        if e.get("type") == "message"
    ]


def process_events(
    events: list[dict[str, Any]], last_event_id: int, bot_email: str
) -> tuple[int, list[Echo]]:
    """Advance last_event_id and pick out the messages worth echoing.

    Returns (new_last_event_id, echoes) in arrival order. `events` may be
    empty (a long-poll timeout with dont_block, or a heartbeat) — that must
    still be safe to call and simply advance nothing.

    Used by the listener, which must never echo its own echoes. The
    durability test uses `advance_last_event_id` + `message_events` directly
    instead, without the bot_email filter — it sends its test messages *as*
    the bot (the only account available) and is proving delivery, not the
    listener's don't-echo-yourself rule.
    """
    new_last_event_id = advance_last_event_id(events, last_event_id)
    echoes = [
        Echo(event_id=e["id"], content=e["message"].get("content", ""))
        for e in events
        if e.get("type") == "message" and e["message"].get("sender_email") != bot_email
    ]
    return new_last_event_id, echoes
