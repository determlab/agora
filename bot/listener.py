"""The bot: register a queue, then long-poll GET /json/events forever,
echoing every non-bot message in ZULIP_CHANNEL back with the event id it
resumed from.

This is the thing issue #37 exists to prove: the bot does not need to
acknowledge within seconds (a Slack/Discord constraint) because Zulip queues
events for a registered-but-not-polling queue on its own, and the bot resumes
from `last_event_id` whenever it comes back — no chat product can push into a
busy Claude Code session, so the bot has to come and collect. See
bot/README.md and bot/durability_test.py for the proof.

Usage:  .venv/Scripts/python.exe bot/listener.py
Stop:   Ctrl+C  (or just stop the process — the queue survives on the server)
"""

from __future__ import annotations

import os
import sys

from resume import process_events
from zulip_client import ZulipClient


def load_config() -> dict[str, str]:
    site = os.environ.get("ZULIP_SITE", "http://zulip.localhost:8090")
    email = os.environ.get("ZULIP_BOT_EMAIL", "")
    api_key = os.environ.get("ZULIP_BOT_API_KEY", "")
    channel = os.environ.get("ZULIP_CHANNEL", "general")
    if not email or not api_key:
        sys.exit(
            "ZULIP_BOT_EMAIL and ZULIP_BOT_API_KEY must be set (bot/.env). "
            "See bot/README.md."
        )
    return {"site": site, "email": email, "api_key": api_key, "channel": channel}


def run(client: ZulipClient, channel: str, bot_email: str) -> None:
    reg = client.register(event_types=["message"])
    queue_id = reg["queue_id"]
    last_event_id = reg["last_event_id"]
    print(f"[listener] registered queue_id={queue_id} last_event_id={last_event_id}")

    while True:
        events = client.get_events(queue_id, last_event_id)
        last_event_id, echoes = process_events(events, last_event_id, bot_email)
        for echo in echoes:
            reply = f"echo (resumed from event {echo.event_id}): {echo.content}"
            client.send_message(to=channel, content=reply, topic="general")
            print(f"[listener] echoed event {echo.event_id}: {echo.content!r}")


def main() -> None:
    cfg = load_config()
    client = ZulipClient(cfg["site"], cfg["email"], cfg["api_key"])
    run(client, cfg["channel"], cfg["email"])


if __name__ == "__main__":
    main()
