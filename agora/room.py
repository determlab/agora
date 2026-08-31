"""Room state: messages, participants, notes. Append-only on disk, in memory for reads.

One JSONL file per room under ``rooms/``. Every mutation is an event appended to
that file, so a restart replays the room exactly and a transcript is greppable
without the server running.

Threading: one lock + condition per room. ``wait_for`` is the long-poll primitive
that makes an agent a live participant instead of a poller — it blocks until the
sequence number moves or the timeout expires.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Event kinds that appear in the transcript.
MESSAGE = "message"      # someone said something
SYSTEM = "system"        # joins, leaves, kicks, agenda changes
NOTE = "note"            # a side note, not part of the conversation
SUMMARY = "summary"      # a summary posted by an agent or generated locally

HUMAN = "human"
AGENT = "agent"

#: A participant is "online" if it has been heard from this recently. The same
#: number decides whether a lobby seat counts as parked, so it lives here rather
#: than in two places that can drift apart.
ONLINE_WINDOW = 95.0


@dataclass
class Event:
    seq: int
    ts: float
    kind: str
    author: str
    text: str
    role: str = AGENT
    provider: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Participant:
    name: str
    role: str = AGENT
    provider: str = ""
    joined: float = 0.0
    last_seen: float = 0.0
    muted: bool = False
    # Where this participant came from, when we know: a Claude Code session id.
    session_id: str = ""
    cwd: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["online"] = ((time.time() - self.last_seen) < ONLINE_WINDOW
                       if self.last_seen else False)
        return d


class Room:
    """One meeting. Everything a participant can see lives here."""

    def __init__(self, room_id: str, title: str, path: Path, agenda: str = "") -> None:
        self.id = room_id
        self.title = title
        self.agenda = agenda
        self.created = time.time()
        self.closed = False
        self.archived = False
        self.events: list[Event] = []
        self.participants: dict[str, Participant] = {}
        self._path = path
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq = 0

    # ---- persistence -------------------------------------------------------

    def _append_disk(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, path: Path) -> Room | None:
        """Replay a room from its JSONL. Returns None for an unreadable file."""
        if not path.exists():
            return None
        room: Room | None = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("t") == "room":
                    room = cls(rec["id"], rec["title"], path, rec.get("agenda", ""))
                    room.created = rec.get("created", time.time())
                elif room is None:
                    continue
                elif rec["t"] == "event":
                    ev = Event(**rec["e"])
                    room.events.append(ev)
                    room._seq = max(room._seq, ev.seq)
                elif rec["t"] == "participant":
                    p = Participant(**rec["p"])
                    room.participants[p.name] = p
                elif rec["t"] == "meta":
                    room.title = rec.get("title", room.title)
                    room.agenda = rec.get("agenda", room.agenda)
                    room.closed = rec.get("closed", room.closed)
                    room.archived = rec.get("archived", room.archived)
        except (json.JSONDecodeError, KeyError, TypeError):
            # A truncated last line is normal after a hard kill; keep what parsed.
            pass
        return room

    # ---- mutation ----------------------------------------------------------

    def post(self, author: str, text: str, *, kind: str = MESSAGE,
             role: str = AGENT, provider: str = "",
             meta: dict[str, Any] | None = None) -> Event:
        with self._cond:
            if self.closed and kind != SYSTEM:
                raise RoomClosed(f"room {self.id} is closed")
            p = self.participants.get(author)
            if kind == MESSAGE and role != HUMAN:
                # Agents must hold a seat to speak. Without this a kick was
                # cosmetic: `leave` removes the participant, and a missing
                # participant used to skip the mute check entirely, so the
                # kicked agent could keep talking.
                if p is None:
                    raise NotSeated(
                        f"{author} is not in {self.id} — call room_join first "
                        f"(if you were removed, the chair did that on purpose)")
                if p.muted:
                    raise Muted(
                        f"{author} is muted in {self.id}. This is not an error: "
                        f"keep reading with room_wait and the chair will unmute "
                        f"you when it is your turn.")
            self._seq += 1
            ev = Event(seq=self._seq, ts=time.time(), kind=kind, author=author,
                       text=text, role=role, provider=provider, meta=meta or {})
            self.events.append(ev)
            if p is not None:
                p.last_seen = ev.ts
            self._append_disk({"t": "event", "e": ev.as_dict()})
            self._cond.notify_all()
            return ev

    def join(self, name: str, *, role: str = AGENT, provider: str = "",
             session_id: str = "", cwd: str = "") -> Participant:
        """Seat someone. **Agents arrive muted.**

        An agent that joins talking is a meeting where five people start at
        once. The chair unmutes whoever should speak, which is what chairing
        is. A human joins unmuted — the chair is not going to mute themselves.
        """
        with self._cond:
            now = time.time()
            p = self.participants.get(name)
            if p is None:
                p = Participant(name=name, role=role, provider=provider,
                                joined=now, last_seen=now,
                                session_id=session_id, cwd=cwd,
                                muted=(role != HUMAN))
                self.participants[name] = p
                # asdict, not as_dict: `online` is derived and must not persist.
                self._append_disk({"t": "participant", "p": asdict(p)})
                new = True
            else:
                p.last_seen = now
                if provider:
                    p.provider = provider
                if session_id:
                    p.session_id = session_id
                new = False
        if new:
            self.post("agora", f"{name} joined — muted" if p.muted
                      else f"{name} joined", kind=SYSTEM, role=HUMAN)
        return p

    def prune(self, older_than: float = 900.0) -> list[str]:
        """Drop participants that have not been seen in *older_than* seconds.

        A renamed or restarted session leaves its old seat behind: the name is
        gone from the machine but the room still holds it. Without this the
        roster accumulates ghosts and the chair cannot tell who is actually
        there.
        """
        cutoff = time.time() - older_than
        with self._cond:
            gone = [n for n, p in self.participants.items()
                    if p.role != HUMAN and p.last_seen < cutoff]
            for n in gone:
                self.participants.pop(n, None)
        for n in gone:
            self.post("agora", f"{n} dropped — not seen in "
                               f"{int(older_than // 60)} min",
                      kind=SYSTEM, role=HUMAN)
        return gone

    def leave(self, name: str) -> None:
        with self._cond:
            p = self.participants.pop(name, None)
        if p is not None:
            self.post("agora", f"{name} left", kind=SYSTEM, role=HUMAN)

    def touch(self, name: str) -> None:
        with self._cond:
            p = self.participants.get(name)
            if p is not None:
                p.last_seen = time.time()

    def set_meta(self, *, title: str | None = None, agenda: str | None = None,
                 closed: bool | None = None, archived: bool | None = None) -> None:
        with self._cond:
            if title is not None:
                self.title = title
            if agenda is not None:
                self.agenda = agenda
            if closed is not None:
                self.closed = closed
            if archived is not None:
                # Archiving hides a finished meeting from the list. It never
                # deletes: the transcript is the record, and a room you cannot
                # read is worse than a list you have to scroll.
                self.archived = archived
            self._append_disk({"t": "meta", "title": self.title,
                               "agenda": self.agenda, "closed": self.closed,
                               "archived": self.archived})
            self._cond.notify_all()

    def set_muted(self, name: str, muted: bool, by: str = "the chair") -> bool:
        with self._cond:
            p = self.participants.get(name)
            if p is None:
                return False
            if p.muted == muted:
                return True  # no-op: do not announce a state that did not change
            p.muted = muted
        # Name the actor. A participant that goes quiet mid-conversation needs to
        # know whether a person did that or something in the system did.
        self.post("agora", f"{name} {'muted' if muted else 'unmuted'} by {by}",
                  kind=SYSTEM, role=HUMAN)
        return True

    # ---- reads -------------------------------------------------------------

    def since(self, seq: int) -> list[Event]:
        with self._lock:
            return [e for e in self.events if e.seq > seq]

    def wait_for(self, seq: int, timeout: float) -> list[Event]:
        """Block until an event newer than *seq* exists, or *timeout* elapses.

        This is what makes an agent a participant rather than a poller: it parks
        in one tool call and returns the moment somebody speaks.
        """
        deadline = time.time() + timeout
        with self._cond:
            while True:
                fresh = [e for e in self.events if e.seq > seq]
                if fresh or self.closed:
                    return fresh
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=min(remaining, 5.0))

    def snapshot(self, *, limit: int | None = None) -> dict[str, Any]:
        with self._lock:
            evs = self.events[-limit:] if limit else self.events
            return {
                "id": self.id,
                "title": self.title,
                "agenda": self.agenda,
                "created": self.created,
                "closed": self.closed,
                "archived": self.archived,
                "seq": self._seq,
                "participants": [p.as_dict() for p in self.participants.values()],
                "events": [e.as_dict() for e in evs],
            }

    def transcript(self, *, kinds: tuple[str, ...] = (MESSAGE, SUMMARY)) -> str:
        with self._lock:
            lines = []
            for e in self.events:
                if e.kind not in kinds:
                    continue
                stamp = time.strftime("%H:%M", time.localtime(e.ts))
                lines.append(f"[{stamp}] {e.author}: {e.text}")
            return "\n".join(lines)

    def local_summary(self) -> str:
        """A deterministic digest. Not a substitute for an agent's summary — it
        is what you get when no agent is in the room to write one, and it never
        invents anything: counts and first lines only."""
        with self._lock:
            msgs = [e for e in self.events if e.kind == MESSAGE]
            notes = [e for e in self.events if e.kind == NOTE]
            by_author: dict[str, int] = {}
            for e in msgs:
                by_author[e.author] = by_author.get(e.author, 0) + 1
            parts = [f"# {self.title}"]
            if self.agenda:
                parts.append(f"\n**Agenda:** {self.agenda}")
            parts.append(f"\n{len(msgs)} messages from {len(by_author)} participants.")
            if by_author:
                parts.append("\n" + "\n".join(
                    f"- {a}: {n}" for a, n in
                    sorted(by_author.items(), key=lambda kv: -kv[1])))
            if notes:
                parts.append("\n**Notes**\n" + "\n".join(f"- {n.text}" for n in notes))
            parts.append("\n*Generated locally — counts only, no interpretation. "
                         "Ask a participant for a real summary.*")
            return "\n".join(parts)


class RoomClosed(Exception):
    pass


class Muted(Exception):
    pass


class NotSeated(Exception):
    """An agent tried to speak in a room it does not hold a seat in."""


#: The waiting room. Always exists, never closes, and is not a meeting — it is
#: how the chair reaches an idle agent using only tools every session already
#: has. A new MCP tool would not help: a client fetches `tools/list` once when
#: it connects, so a tool added to a running server is invisible to every
#: session that connected before it. `room_wait` on the lobby needs nothing new.
LOBBY = "lobby"


class Hub:
    """All rooms. Loads what is on disk at startup."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.rooms: dict[str, Room] = {}
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)
        for path in sorted(root.glob("*.jsonl")):
            room = Room.load(path)
            if room is not None:
                self.rooms[room.id] = room
        if LOBBY not in self.rooms:
            self.create("Lobby", "Wait here when idle. The chair calls you from "
                                 "this room into a meeting.", room_id=LOBBY)

    def create(self, title: str, agenda: str = "", room_id: str = "") -> Room:
        with self._lock:
            rid = room_id or uuid.uuid4().hex[:8]
            if rid in self.rooms:
                return self.rooms[rid]
            path = self.root / f"{rid}.jsonl"
            room = Room(rid, title, path, agenda)
            room._append_disk({"t": "room", "id": rid, "title": title,
                               "agenda": agenda, "created": room.created})
            self.rooms[rid] = room
        return room

    def delete(self, room_id: str) -> bool:
        """Remove a room and its transcript. Archive hides; this destroys."""
        with self._lock:
            room = self.rooms.pop(room_id, None)
        if room is None:
            return False
        try:
            room._path.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def get(self, room_id: str) -> Room | None:
        return self.rooms.get(room_id)

    def resolve(self, ref: str) -> Room | None:
        """By id, or by exact title — agents are given a name, not a hex id."""
        room = self.rooms.get(ref)
        if room is not None:
            return room
        for r in self.rooms.values():
            if r.title == ref:
                return r
        return None

    def listing(self) -> list[dict[str, Any]]:
        return [{"id": r.id, "title": r.title, "agenda": r.agenda,
                 "closed": r.closed, "archived": r.archived, "created": r.created,
                 "participants": len(r.participants),
                 "messages": sum(1 for e in r.events if e.kind == MESSAGE)}
                for r in sorted(self.rooms.values(), key=lambda x: -x.created)]
