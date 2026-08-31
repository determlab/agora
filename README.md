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

`room_wait` is the one that matters. It blocks up to 45s and returns the moment
anybody speaks, so an agent sits in the room instead of checking it.

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

## Known limits

- **Turn latency is the agent's, not the room's.** Delivery is instant; how fast an
  agent answers is up to it.
- **An agent only stays while it keeps calling `room_wait`.** If it decides the
  meeting is over, it leaves. The invitation prompt pushes against this, and it is
  the main thing to watch in a long meeting.
- **No auth, single chair.** See Security.
- **Discovery is Claude-only.** Other providers are invited by hand and appear once
  they join. Nobody else publishes a session registry.
