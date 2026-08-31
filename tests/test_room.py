"""The room state machine: seating, muting, persistence, and the long poll."""
from __future__ import annotations

import threading
import time

import pytest

from agora.room import (AGENT, HUMAN, LOBBY, MESSAGE, NOTE, SUMMARY, Hub, Muted,
                        NotSeated, Room, RoomClosed)


def test_hub_always_has_a_lobby(hub: Hub):
    assert hub.get(LOBBY) is not None
    assert hub.get(LOBBY).title == "Lobby"


def test_agents_arrive_muted_humans_do_not(hub: Hub):
    room = hub.create("m")
    agent = room.join("bot", role=AGENT)
    chair = room.join("hemi", role=HUMAN)
    assert agent.muted is True
    assert chair.muted is False


def test_a_muted_agent_cannot_speak_and_is_told_why(hub: Hub):
    room = hub.create("m")
    room.join("bot", role=AGENT)
    with pytest.raises(Muted) as exc:
        room.post("bot", "hello", role=AGENT)
    # The message matters: an agent that reads this as an error will work around
    # it instead of waiting.
    assert "not an error" in str(exc.value)
    room.set_muted("bot", False)
    assert room.post("bot", "hello", role=AGENT).text == "hello"


def test_a_kicked_agent_cannot_keep_talking(hub: Hub):
    """The regression this was written for: kick removed the participant, and a
    missing participant used to skip the mute check entirely."""
    room = hub.create("m")
    room.join("bot", role=AGENT)
    room.set_muted("bot", False)
    room.post("bot", "still here", role=AGENT)
    room.leave("bot")
    with pytest.raises(NotSeated):
        room.post("bot", "and again", role=AGENT)


def test_the_chair_can_speak_without_joining(hub: Hub):
    room = hub.create("m")
    assert room.post("hemi", "hi", role=HUMAN).seq > 0


def test_a_closed_room_refuses_messages_but_accepts_system_events(hub: Hub):
    room = hub.create("m")
    room.join("hemi", role=HUMAN)
    room.set_meta(closed=True)
    with pytest.raises(RoomClosed):
        room.post("hemi", "hi", role=HUMAN)
    room.post("agora", "closed", kind="system", role=HUMAN)  # must not raise


def test_muting_is_idempotent_and_names_the_actor(hub: Hub):
    room = hub.create("m")
    room.join("bot", role=AGENT)          # arrives muted
    before = len(room.events)
    assert room.set_muted("bot", True) is True   # already muted: a no-op
    assert len(room.events) == before, "a no-op mute must not announce anything"
    room.set_muted("bot", False, by="Hemi")
    assert "unmuted by Hemi" in room.events[-1].text


def test_set_muted_on_a_stranger_reports_failure(hub: Hub):
    assert hub.create("m").set_muted("nobody", True) is False


def test_prune_drops_stale_agents_and_keeps_humans(hub: Hub):
    room = hub.create("m")
    room.join("hemi", role=HUMAN)
    room.join("bot", role=AGENT)
    room.participants["bot"].last_seen = time.time() - 10_000
    room.participants["hemi"].last_seen = time.time() - 10_000
    assert room.prune(older_than=900) == ["bot"]
    assert "hemi" in room.participants, "the chair is never pruned"


def test_wait_for_returns_the_instant_someone_speaks(hub: Hub):
    room = hub.create("m")
    room.join("hemi", role=HUMAN)
    got: list = []

    def waiter():
        got.extend(room.wait_for(room.snapshot()["seq"], timeout=5.0))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    started = time.time()
    room.post("hemi", "wake up", role=HUMAN)
    t.join(timeout=5)
    assert time.time() - started < 1.0, "the wait must return on notify, not poll"
    assert [e.text for e in got] == ["wake up"]


def test_wait_for_returns_empty_on_timeout(hub: Hub):
    room = hub.create("m")
    assert room.wait_for(room.snapshot()["seq"], timeout=0.2) == []


def test_wait_for_returns_immediately_when_the_room_closes(hub: Hub):
    """A parked agent must not hang for the full timeout after the meeting ends."""
    room = hub.create("m")
    room.set_meta(closed=True)
    started = time.time()
    room.wait_for(room.snapshot()["seq"], timeout=5.0)
    assert time.time() - started < 1.0


def test_a_room_replays_from_disk_exactly(hub: Hub, tmp_path):
    room = hub.create("persisted", agenda="does it come back")
    room.join("hemi", role=HUMAN)
    room.join("bot", role=AGENT)
    room.set_muted("bot", False)
    room.post("hemi", "a message", role=HUMAN)
    room.post("bot", "a reply", role=AGENT)
    room.post("hemi", "a note", kind=NOTE, role=HUMAN)
    room.post("bot", "a summary", kind=SUMMARY, role=AGENT)
    room.set_meta(archived=True, closed=True)

    reloaded = Hub(tmp_path / "rooms").get(room.id)
    assert reloaded is not None
    assert reloaded.title == "persisted"
    assert reloaded.agenda == "does it come back"
    assert reloaded.closed is True and reloaded.archived is True
    assert set(reloaded.participants) == {"hemi", "bot"}
    assert [(e.kind, e.author, e.text) for e in reloaded.events] == \
           [(e.kind, e.author, e.text) for e in room.events]


def test_a_truncated_transcript_still_loads(hub: Hub, tmp_path):
    """A hard kill mid-write leaves half a line. Losing that line is fine;
    losing the room is not."""
    room = hub.create("crashy")
    room.post("hemi", "one", role=HUMAN)
    room.post("hemi", "two", role=HUMAN)
    with room._path.open("a", encoding="utf-8") as fh:
        fh.write('{"t": "event", "e": {"seq": 99, "ts": 1, ')  # torn write

    reloaded = Hub(tmp_path / "rooms").get(room.id)
    assert [e.text for e in reloaded.events] == ["one", "two"]


def test_sequence_numbers_survive_a_reload(hub: Hub, tmp_path):
    room = hub.create("seq")
    room.post("hemi", "one", role=HUMAN)
    reloaded = Hub(tmp_path / "rooms").get(room.id)
    nxt = reloaded.post("hemi", "two", role=HUMAN)
    assert nxt.seq == 2, "a reload must not restart numbering and overwrite history"


def test_concurrent_posts_get_unique_sequence_numbers(hub: Hub):
    room = hub.create("race")
    room.join("hemi", role=HUMAN)

    def spam():
        for i in range(25):
            room.post("hemi", str(i), role=HUMAN)

    threads = [threading.Thread(target=spam) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e.seq for e in room.events]
    assert len(seqs) == len(set(seqs)) == 101  # 100 posts + the join event


def test_local_summary_counts_and_never_interprets(hub: Hub):
    room = hub.create("digest")
    room.join("hemi", role=HUMAN)
    room.post("hemi", "one", role=HUMAN)
    room.post("hemi", "two", role=HUMAN)
    room.post("hemi", "an action item", kind=NOTE, role=HUMAN)
    text = room.local_summary()
    assert "2 messages" in text
    assert "an action item" in text
    assert "Generated locally" in text, "it must say it is not a real summary"


def test_delete_removes_the_room_and_its_transcript(hub: Hub):
    room = hub.create("temporary")
    path = room._path
    assert path.exists()
    assert hub.delete(room.id) is True
    assert hub.get(room.id) is None
    assert not path.exists()
    assert hub.delete(room.id) is False


def test_resolve_finds_a_room_by_title_as_well_as_id(hub: Hub):
    room = hub.create("Ship 0.2.2?")
    assert hub.resolve(room.id) is room
    assert hub.resolve("Ship 0.2.2?") is room
    assert hub.resolve("nope") is None


def test_listing_reports_message_counts_not_event_counts(hub: Hub):
    room = hub.create("counts")
    room.join("hemi", role=HUMAN)          # a system event
    room.post("hemi", "one", role=HUMAN)   # a message
    row = next(r for r in hub.listing() if r["id"] == room.id)
    assert row["messages"] == 1
