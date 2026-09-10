"""The DoD test: register a queue, do NOT poll it for a real three minutes
while a separate thread posts N messages spaced across the gap, then resume
and assert every message comes back, in order, resumed from the stored
last_event_id.

This does not write any buffering of its own — the Zulip server queues events
for a registered-but-not-polling queue on its own. This script only proves
that behaviour, with a real 3-minute wall-clock gap (no shortcut).

Usage:  .venv/Scripts/python.exe bot/durability_test.py
Needs bot/.env populated (ZULIP_BOT_EMAIL, ZULIP_BOT_API_KEY, ZULIP_CHANNEL)
and the Zulip stack up (`docker compose up -d` in bot/).
"""

from __future__ import annotations

import os
import sys
import threading
import time

from resume import advance_last_event_id, message_events
from zulip_client import ZulipClient

GAP_SECONDS = 180  # the real 3-minute wait the issue asks for
N_MESSAGES = 6
SEND_INTERVAL = GAP_SECONDS / (N_MESSAGES + 1)  # spread across the gap


def load_client() -> tuple[ZulipClient, str, str]:
    site = os.environ.get("ZULIP_SITE", "http://zulip.localhost:8090")
    email = os.environ.get("ZULIP_BOT_EMAIL", "")
    api_key = os.environ.get("ZULIP_BOT_API_KEY", "")
    channel = os.environ.get("ZULIP_CHANNEL", "general")
    if not email or not api_key:
        sys.exit("ZULIP_BOT_EMAIL / ZULIP_BOT_API_KEY not set — see bot/README.md")
    return ZulipClient(site, email, api_key), channel, email


def sender_thread(client: ZulipClient, channel: str, sent: list[str]) -> None:
    """Runs concurrently with the sleep below, posting one message roughly
    every SEND_INTERVAL seconds so the gap is exercised throughout, not just
    at the very end."""
    for i in range(N_MESSAGES):
        time.sleep(SEND_INTERVAL)
        text = f"durability-test message {i} at {time.time():.3f}"
        client.send_message(to=channel, content=text, topic="durability-test")
        sent.append(text)
        print(f"[sender] sent #{i}: {text}")


def main() -> None:
    client, channel, _bot_email = load_client()

    reg = client.register(event_types=["message"], narrow=[["stream", channel]])
    queue_id = reg["queue_id"]
    last_event_id = reg["last_event_id"]
    print(f"[test] registered queue_id={queue_id} last_event_id={last_event_id}")
    print(f"[test] NOT polling for {GAP_SECONDS}s while {N_MESSAGES} messages send")

    sent: list[str] = []
    t = threading.Thread(target=sender_thread, args=(client, channel, sent))
    start = time.time()
    t.start()

    # The real wait. No shortcut — this is the thing being tested.
    while time.time() - start < GAP_SECONDS:
        time.sleep(1)
    t.join()

    print("[test] gap over, resuming from stored queue_id/last_event_id")
    events = client.get_events(queue_id, last_event_id, dont_block=True)
    # A single dont_block call only drains what's buffered right now; loop
    # until nothing new comes back, since a busy server may not return every
    # queued event in one response.
    all_events = list(events)
    cursor = last_event_id
    while events:
        cursor = advance_last_event_id(events, cursor)
        events = client.get_events(queue_id, cursor, dont_block=True)
        all_events.extend(events)

    # Not process_events / bot_email filtering here on purpose: the test
    # messages are sent *as* the bot (the only account this script holds),
    # so filtering by sender would discard every message it just sent. This
    # is proving delivery through the gap, not the listener's echo rule —
    # that rule is covered separately in tests/test_resume.py.
    received = [e.content for e in message_events(all_events)]

    print(f"[test] sent {len(sent)} messages, received {len(received)} back")
    ok = received == sent
    if ok:
        print(f"[test] PASS — all {len(sent)} messages received, in order")
    else:
        print("[test] FAIL")
        print(f"  sent:     {sent}")
        print(f"  received: {received}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
