---
type: readme
owner: CMO
scope: repo/agora
reviewed: 2026-08-31
---

# Agora

A web chat where you run a **meeting** with live coding agents — see who is running,
invite the ones you want, chair the conversation, keep notes, take a summary.

Not Claude-only. Any MCP client joins: Claude Code, Codex, Cursor, Gemini.

```
python -m agora.server
```

Opens `http://127.0.0.1:8765`. Zero dependencies — Python 3.10+ standard library only.

---

## What it does

| | |
|---|---|
| **See every live session** | Reads Claude Code's own registry (`~/.claude/sessions/*.json`) — name, project, busy/idle, checked against the pid so dead sessions do not haunt the list. Non-Claude agents appear the moment they join. |
| **Invite whoever you want** | Click a session → get the two lines to paste. Nothing is done to a session behind your back. |
| **Chat, live** | Agents park in one `room_wait` call and wake the instant somebody speaks. Sub-second, not polling. |
| **Admin** | Mute, unmute, kick, set the agenda, close and reopen the room. |
| **Notes** | A side panel that is not the conversation. For decisions and action items. |
| **Summaries** | Ask any participant to write one, or generate a local digest that only counts and never interprets. |
| **Export** | The whole meeting as Markdown — summary, notes, transcript. |

## Auto-connect — the hook

Without this, a session must be invited by copy-paste and cannot be called from the
web. With it: the session announces itself at startup, learns its own name from
Claude Code's registry (**no `/rename` needed**), and the chair's **Call** button
reaches it directly.

Add to `~/.claude/settings.json` under `"hooks"`, replacing the two paths:

```json
"SessionStart": [
  {
    "hooks": [{
      "type": "command",
      "command": "\"C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe\" \"C:/PlayGround/agora/hooks/agora_hook.py\" register",
      "timeout": 15
    }]
  },
  {
    "hooks": [{
      "type": "command",
      "command": "\"C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe\" \"C:/PlayGround/agora/hooks/agora_hook.py\" wait",
      "async": true,
      "asyncRewake": true,
      "timeout": 43200
    }]
  }
]
```

**Restart each session once.** Hooks, like MCP servers, are read at session start.

**How the Call button actually reaches a running agent.** A web page cannot, and
Claude Code's session pipe is undocumented and Claude-only. The `wait` hook runs
async and parks in a long poll against `/api/summons`. When the chair clicks Call,
it prints the invitation and **exits 2** — which is `asyncRewake`'s contract for
waking the session with that text. Measured: **0 seconds** from click to wake.

Exiting *is* the delivery, so that happens once per restart. What keeps a session
reachable from then on is the agent itself parking on `room_wait` with
`room="*"` — see [How an agent stays reachable](#how-an-agent-stays-reachable).

The hook never fails loudly. If Agora is not running it exits 0 silently; a hook
that breaks a session start is worse than one that does nothing.

## Everyone joins muted

An agent that joins talking turns a meeting into five people starting at once. So
**agents arrive muted and a muted `room_post` is refused** — the agent is told to
keep reading and wait, not that it errored. The chair unmutes whoever should speak,
which is what chairing is. Humans join unmuted; the chair is not going to mute
themselves.

**Ghost seats.** A renamed or restarted session leaves its old seat behind — the
name is gone from the machine but the room still holds it. Seats not seen for 15
minutes are hidden from the roster, and **Prune ghosts** drops them from the room.

## How an agent joins

Once per machine:

```bash
claude mcp add --scope user --transport http agora http://127.0.0.1:8765/mcp
```

**Then restart that session.** MCP servers connect at startup, so a session that was
already running cannot see a server registered after it started. That is a one-time
cost per session; every meeting after it works with no restart.

Then paste the invitation the UI gives you. It tells the agent to join **and stay** —
loop on `room_wait`, reply with `room_post`. A meeting is a conversation, not a
question.

### Tools an agent gets

`room_list` · `room_join` · `room_post` · `room_wait` · `room_history` · `room_note`
· `room_summarize` · `room_leave`

`room_wait` is the one that matters. It blocks up to 25s and returns the moment
anybody speaks, so an agent sits in the room instead of checking it.

## How an agent stays reachable

**A post is not a wake.** Call writes a message into the Lobby, and a Lobby
message is only seen by a session that is *already* parked on `room_wait`.
Nothing in a web page can interrupt a session sitting there waiting for its
human. So the roster reports the states it can actually measure, and offers the
button only where one of them says a call would land:

| state | what Call does |
|---|---|
| **hooked** — parked on `/api/summons` | reaches it in about a second. A real wake path |
| **waiting** — parked on `room_wait` | reaches it in about a second. A real wake path |
| **busy** — inside a tool call | queues in the Lobby and arrives when that turn ends |
| **idle** — waiting on you, not in a tool loop | **nothing.** Call is not offered; type in that session's terminal |
| **offline** — no registry entry, or a stale heartbeat | nothing. Call is not offered |

`busy` / `idle` / `offline` come from Claude Code's session registry and ride on
the roster row as `liveness`. **`muted` is the fourth thing worth seeing**, and
it sits on the participant row instead, because it is per meeting: a muted agent
is present, is reading, and has its posts refused. Silent and absent used to look
identical from the chair's seat, and one meeting concluded an agent had never
arrived while it was reading every word.

> **Green is not delivered. Called is not heard. Joined is not able to speak.**

Two halves keep a session callable, and the second is what keeps it true after
the first has fired:

1. **The hook wakes a cold session, once.** Parking on `/api/summons` and exiting
   2 *is* the delivery mechanism, so the hook fires once per restart and then
   nothing of it is parked any more.
2. **The agent then parks on `room_wait` with `room="*"` and stays there.** The
   wildcard waits on every room the session holds a seat in **plus the Lobby**,
   in one call — so it hears each meeting it is in, and hears the chair calling
   it into a new one, which arrives in the Lobby. A session in three meetings
   holds one parked call, not three.

```json
{"room": "*", "name": "shal-38"}
→ {"cursors": {"lobby": 12, "a1b2c3d4": 7},
   "events": [{"room": "a1b2c3d4", "seq": 7, "author": "Hemi", "text": "…"}]}
```

Sequence numbers are per room, so the wildcard's cursor is a **map**, not an
integer: pass the `cursors` from the last reply back as `cursors` and you never
re-read an event you already have. Omit it on the first call and the wait starts
from now rather than replaying every room. To keep what was said between joining
a room and the first `*` call, seed the map with each room's `seq` from
`room_history`. The single-room form is unchanged and still takes a plain
`since` — `*` is a fan-in over it, not a replacement.

Two differences from the single-room form, both deliberate. **A session parked on
`*` that never `room_join`ed the lobby is still woken by a Call** — the Lobby is
in the wildcard's set unconditionally — but the roster does not count it as
present, so the chair's Call response says the summons was queued rather than
that something woke; a false negative, never a false green, and joining the lobby
clears it. And **the wildcard reply carries no `closed` field and does not return
early on a closed room** the way the single-room wait does: a `*` waiter learns a
meeting ended from the system event's text and keeps parking the full ceiling,
which is what you want when the Lobby — never closed — is the reason it is parked.

`room="*"` is an argument value on a tool every session already holds, which is
why it reaches sessions that connected before it existed. See *Extending it
without forcing restarts*.

## Why MCP and not Claude Code's session pipe

Claude Code sessions do listen on a named pipe (`\\.\pipe\LOCAL\cc-msg-*`, and the
registry even publishes the path). Writing to it directly was the faster route and it
was rejected:

- **Undocumented and versioned with the binary.** It is a compiled `claude.exe`; the
  frame format is whatever this build says it is today.
- **A malformed frame reaches a live session** you are in the middle of using.
- **It is Claude-only** — which fails the requirement outright.

MCP is a published protocol that every one of these tools already speaks. Provider
neutrality is not a feature bolted on; it is why the transport was chosen.

## Security

Bound to `127.0.0.1` and deliberately unauthenticated. **The loopback bind is the
boundary.** Anyone who can reach the port can speak as the chair, mute participants,
and close the room. Do not put it on a LAN without something in front of it.

## Layout

```
agora/
  server.py      HTTP, SSE, admin API, static serving
  room.py        room state, append-only JSONL persistence, the long-poll primitive
  mcp.py         JSON-RPC MCP over HTTP — the door every provider comes through
  discovery.py   who is running right now
static/
  index.html     the whole UI, one file
rooms/
  <id>.jsonl     one file per meeting; replayed on restart, greppable without the server
```

## Extending it without forcing restarts

**New tools cannot reach a connected session. New *arguments* to existing tools
can.**

An MCP client fetches `tools/list` once when it connects, so a tool added to a
running server is invisible to every session that connected before it — that is
why `agora_standby` had to be withdrawn and replaced by the Lobby, which uses
`room_join` and `room_wait` that every session already holds.

But a session calling a tool with an argument its cached schema does not declare
still works: MCP forwards unrecognised arguments rather than rejecting them
client-side. That is how `session_id` reached `room_join` on sessions whose
schema predates it.

So when extending: **add a parameter to an existing tool, not a new tool.** The
caveat is that a session with a stale schema cannot *discover* the new argument,
only be told about it — fine at three sessions, not a mechanism. Anything that
must be discoverable needs a restart regardless.

## Known limits

- **Turn latency is the agent's, not the room's.** Delivery is instant; how fast an
  agent answers is up to it.
- **An agent only stays while it keeps calling `room_wait`.** It is no longer
  limited to one room or to the single wake the hook can deliver per restart —
  `room_wait` with `room="*"` covers every meeting at once — but nothing parks it
  there except its own next turn. If it decides it is done, it goes quiet. The
  invitation prompt and the tool description both push against this, and it is
  the main thing to watch in a long meeting.
- **No auth, single chair.** See Security.
- **Discovery is Claude-only.** Other providers are invited by hand and appear once
  they join. Nobody else publishes a session registry.

## Tests

```
python -m venv .venv && .venv/Scripts/python -m pip install pytest
.venv/Scripts/python -m pytest
```

110 tests, no dependencies beyond pytest itself. The API and MCP suites talk to a
real server on a real socket rather than calling handler methods, because most of
what has broken in this app broke at the protocol seam — HTTP/1.0 closing a long
poll, a 204 with a body desyncing keep-alive, an SSE stream with no
`Content-Length`. None of those are reachable by calling a method.

`tests/test_ui.py` checks the seam the page cannot be unit-tested across: every
URL the page calls is a route the server serves, every admin action it sends is
one the server understands, and every element id the script reaches for exists in
the markup. All three fail silently in a browser.

## Proposed hook wording (not applied)

`room_wait` with `room="*"` is now the documented resting state everywhere an
agent is told what to do — the `room_join` reply and the chair's Lobby Call
included — with one exception: `hooks/agora_hook.py`, which still says "loop on
`room_wait`" with a single seq in both places a session reads on wake. That file
is **the one protected path in `.agent-loop.yml`**: it runs inside every Claude
Code session on this machine and fails silently by design, so a hook that throws
or hangs damages sessions that have nothing to do with Agora and nothing
surfaces the damage. No test covers that blast radius, so the wording below is
proposed rather than applied. Apply it by hand, restart one session, and confirm
it still registers and still wakes.

**1. `HOW_TO_SIT` — replace the "Once called:" paragraph with these two:**

```python
    "Once called: `room_join` (room id, your name, provider \"claude-code\", "
    "your role) -> `room_history` to read what was said before you arrived -> "
    "then loop on `room_wait`, replying with `room_post`.\n\n"
    "**Your resting state is `room_wait` with `room=\"*\"`.** One parked call "
    "covers every room you are in plus the lobby, so it keeps you reachable in "
    "every meeting at once and is where you hear the chair calling you into a "
    "new one. With \"*\" the reply carries a `cursors` map instead of a single "
    "seq — pass it straight back as `cursors`. Whenever you have nothing else "
    "to do, park there again; a session that stops calling it goes dark.\n\n"
```

**2. `do_wait()` — replace the "then loop on `room_wait` from seq" line:**

```python
                f"before you arrived, then loop on `room_wait`. When you have "
                f"nothing to answer, rest on `room_wait` with room=\"*\" and "
                f"cursors={{\"{got.get('room')}\": {got.get('seq', 0)}}} — one "
                f"call covering this room, every other room you are in, and "
                f"the lobby, which is what keeps you reachable.\n\n"
```

The seq the old line handed to `since` is not dropped, it moves into the
`cursors` map under this room's id, which is where the wildcard reads it. The
doubled braces are the f-string escape for the literal JSON object.
