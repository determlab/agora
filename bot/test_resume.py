"""Unit tests for the listener's resume logic. No live Zulip instance
required — the HTTP layer is never touched; these test resume.py directly.

Run: .venv/Scripts/python.exe -m pytest bot/test_resume.py -q
"""

from resume import advance_last_event_id, message_events, process_events

BOT_EMAIL = "coo-bot-bot@zulip.local"


def _msg_event(event_id: int, content: str, sender: str = "human@zulip.local"):
    return {
        "id": event_id,
        "type": "message",
        "message": {"content": content, "sender_email": sender},
    }


def _heartbeat_event(event_id: int):
    return {"id": event_id, "type": "heartbeat"}


def test_advance_last_event_id_tracks_max_seen():
    events = [_msg_event(5, "a"), _msg_event(3, "b"), _msg_event(9, "c")]
    assert advance_last_event_id(events, last_event_id=1) == 9


def test_advance_last_event_id_empty_batch_is_noop():
    assert advance_last_event_id([], last_event_id=42) == 42


def test_advance_last_event_id_counts_non_message_events():
    # A heartbeat's id still must be resumed past, or the next poll refetches it.
    events = [_heartbeat_event(7)]
    assert advance_last_event_id(events, last_event_id=1) == 7


def test_message_events_extracts_in_order():
    events = [_msg_event(1, "first"), _heartbeat_event(2), _msg_event(3, "second")]
    echoes = message_events(events)
    assert [e.content for e in echoes] == ["first", "second"]
    assert [e.event_id for e in echoes] == [1, 3]


def test_process_events_never_echoes_the_bots_own_messages():
    events = [
        _msg_event(1, "hello", sender="human@zulip.local"),
        _msg_event(2, "echo: hello", sender=BOT_EMAIL),
    ]
    _, echoes = process_events(events, last_event_id=0, bot_email=BOT_EMAIL)
    assert [e.content for e in echoes] == ["hello"]


def test_process_events_resumes_without_gaps_or_duplicates():
    """Simulates two successive poll batches, as would happen across a
    listener restart: last_event_id from batch 1 must be exactly what batch
    2 resumes from, with no event repeated and none skipped."""
    batch1 = [_msg_event(10, "one"), _msg_event(11, "two")]
    last_event_id, echoes1 = process_events(batch1, last_event_id=9, bot_email=BOT_EMAIL)
    assert last_event_id == 11

    batch2 = [_msg_event(12, "three")]
    last_event_id, echoes2 = process_events(batch2, last_event_id, bot_email=BOT_EMAIL)
    assert last_event_id == 12

    all_content = [e.content for e in echoes1 + echoes2]
    assert all_content == ["one", "two", "three"]


def test_process_events_empty_batch_advances_nothing():
    last_event_id, echoes = process_events([], last_event_id=5, bot_email=BOT_EMAIL)
    assert last_event_id == 5
    assert echoes == []


def test_process_events_preserves_order_across_mixed_events():
    events = [
        _msg_event(1, "a"),
        _heartbeat_event(2),
        _msg_event(3, "b", sender=BOT_EMAIL),  # filtered out
        _msg_event(4, "c"),
    ]
    last_event_id, echoes = process_events(events, last_event_id=0, bot_email=BOT_EMAIL)
    assert last_event_id == 4
    assert [e.content for e in echoes] == ["a", "c"]
