"""The room state machine: seating, muting, persistence, and the long poll."""
from __future__ import annotations

import json
import threading
import time

import pytest

from agora.room import (AGENT, HUMAN, LOBBY, MESSAGE, NOTE, ONLINE_WINDOW,
                        SUMMARY, Hub, Muted, NotSeated, Room, RoomClosed,
                        mention_note)


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


def test_wait_any_covers_every_room_from_one_parked_call(hub: Hub):
    """The whole point of the wildcard: a session in three meetings parks once.

    One waiter, not one per room — a thread or a held lock per room is what
    deadlocks or leaks as soon as an agent sits in a few meetings.
    """
    rooms = [hub.create(f"m{i}") for i in range(3)]
    for room in rooms:
        room.join("bot", role=AGENT)
        room.join("hemi", role=HUMAN)
    _, cursors = hub.wait_any("bot", {}, timeout=0.1)   # park from now

    got: list = []
    threads_before = threading.active_count()

    def waiter():
        got.append(hub.wait_any("bot", cursors, timeout=5.0))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.2)
    threads_during = threading.active_count()
    started = time.time()
    rooms[2].post("hemi", "over here", role=HUMAN)
    t.join(timeout=5)

    assert time.time() - started < 1.0, "it must wake on notify, not poll"
    assert threads_during == threads_before + 1, "one parked call, not one per room"
    events, _ = got[0]
    assert [(rid, e.text) for rid, e in events] == [(rooms[2].id, "over here")]


def test_wait_any_cursors_are_per_room_and_never_re_read(hub: Hub):
    """A single integer cannot address several rooms — seqs are per room. The
    caller echoes the cursor map back and loops without seeing anything twice."""
    a, b = hub.create("a"), hub.create("b")
    for room in (a, b):
        room.join("bot", role=AGENT)
        room.join("hemi", role=HUMAN)

    _, cursors = hub.wait_any("bot", {}, timeout=0.1)
    a.post("hemi", "in a", role=HUMAN)
    b.post("hemi", "in b", role=HUMAN)

    first, cursors = hub.wait_any("bot", cursors, timeout=5.0)
    assert {(rid, e.text) for rid, e in first} == {(a.id, "in a"), (b.id, "in b")}
    second, _ = hub.wait_any("bot", cursors, timeout=0.3)
    assert second == [], "echoing the cursors back must not re-read those events"


def test_wait_any_watches_the_lobby_and_your_own_rooms_only(hub: Hub):
    """The Lobby is always in the set — that is where a call into a room you are
    not in yet arrives. A meeting you are not seated in is not."""
    mine = hub.create("mine")
    mine.join("bot", role=AGENT)
    theirs = hub.create("theirs")
    theirs.join("someone-else", role=AGENT)
    lobby = hub.get(LOBBY)

    _, cursors = hub.wait_any("bot", {}, timeout=0.1)
    assert set(cursors) == {LOBBY, mine.id}

    theirs.post("hemi", "not for you", role=HUMAN)
    events, cursors = hub.wait_any("bot", cursors, timeout=0.3)
    assert events == []

    lobby.post("hemi", "the chair calls you", role=HUMAN)
    events, _ = hub.wait_any("bot", cursors, timeout=5.0)
    assert [e.text for _, e in events] == ["the chair calls you"]


def test_wait_any_does_not_wake_you_for_your_own_words(hub: Hub):
    room = hub.create("echo")
    room.join("bot", role=AGENT)
    room.set_muted("bot", False)
    _, cursors = hub.wait_any("bot", {}, timeout=0.1)
    room.post("bot", "mine", role=AGENT)

    events, cursors = hub.wait_any("bot", cursors, timeout=0.3)
    assert events == []
    assert cursors[room.id] == room.tip(), "but the cursor still moves past it"


def test_wait_any_keeps_the_seats_it_is_waiting_on_alive(hub: Hub):
    """Presence is polling. One parked call has to refresh every seat it covers,
    or a session in three rooms is reported dead in two of them."""
    room = hub.create("presence")
    room.join("bot", role=AGENT)
    room.participants["bot"].last_seen = 0.0
    hub.wait_any("bot", {}, timeout=0.1)
    assert time.time() - room.participants["bot"].last_seen < 5.0


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


# ---- mentions --------------------------------------------------------------

def test_a_mention_is_resolved_against_the_participant_list(hub: Hub):
    room = hub.create("m")
    room.join("CTO", role=AGENT)
    room.join("hemi", role=HUMAN)
    ev = room.post("hemi", "@CTO can you look at this?", role=HUMAN)
    assert ev.mentions == ["CTO"]


def test_an_at_that_is_not_a_participant_stays_plain_text(hub: Hub):
    """The whole reason resolution is roster-first: an address or a pasted
    snippet must not become a mention of somebody who is not in the room."""
    room = hub.create("m")
    room.join("CTO", role=AGENT)
    room.join("hemi", role=HUMAN)
    ev = room.post("hemi", "mail hemi@example.com, and @nobody, and @property",
                   role=HUMAN)
    assert ev.mentions == []
    # An address whose domain happens to be a participant's name is still an
    # address: the `@` there does not start a word.
    assert room.post("hemi", "write to me@CTO.internal", role=HUMAN).mentions == []


def test_a_mention_does_not_match_a_longer_name(hub: Hub):
    room = hub.create("m")
    room.join("CT", role=AGENT)
    room.join("CTO", role=AGENT)
    room.join("hemi", role=HUMAN)
    assert room.post("hemi", "@CTO over to you", role=HUMAN).mentions == ["CTO"]
    assert room.post("hemi", "@CT over to you", role=HUMAN).mentions == ["CT"]


def test_several_mentions_come_back_in_the_order_they_were_written(hub: Hub):
    room = hub.create("m")
    for name in ("CTO", "QA", "hemi"):
        room.join(name, role=HUMAN if name == "hemi" else AGENT)
    ev = room.post("hemi", "(@QA) then @CTO, and @QA again", role=HUMAN)
    assert ev.mentions == ["QA", "CTO"], "each name once, first appearance wins"


def test_a_mention_of_a_seat_that_stopped_polling_is_reported_as_not_reached(hub: Hub):
    """Presence is polling, not membership. A seat left behind by a dead session
    is still in `participants` and hears nothing — saying otherwise is the false
    green this app keeps shipping."""
    room = hub.create("m")
    room.join("CTO", role=AGENT)
    room.join("hemi", role=HUMAN)
    ev = room.post("hemi", "@CTO you there?", role=HUMAN)
    assert room.mention_report(ev.mentions) == [{"name": "CTO", "listening": True}]
    assert mention_note(room.mention_report(ev.mentions)) == ""

    room.participants["CTO"].last_seen = time.time() - (ONLINE_WINDOW + 60)
    report = room.mention_report(ev.mentions)
    assert report == [{"name": "CTO", "listening": False}]
    assert "CTO" in mention_note(report) and "nothing woke" in mention_note(report)


def test_an_old_transcript_without_mentions_still_replays(hub: Hub):
    """Rooms are append-only JSONL. A line written before mentions existed has
    no such key, and it must load with an empty list rather than blow up."""
    room = hub.create("old")
    room.join("hemi", role=HUMAN)
    room.post("hemi", "@nobody", role=HUMAN)
    lines = room._path.read_text(encoding="utf-8").splitlines()
    stripped = []
    for line in lines:
        rec = json.loads(line)
        if rec.get("t") == "event":
            rec["e"].pop("mentions", None)
        stripped.append(json.dumps(rec))
    room._path.write_text("\n".join(stripped) + "\n", encoding="utf-8")

    replayed = Room.load(room._path)
    assert replayed is not None
    assert [e.mentions for e in replayed.events] == [[] for _ in replayed.events]
