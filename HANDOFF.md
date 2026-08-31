# Handoff — Agora development moves to its own session

Written 2026-08-31 by the COO session (`C:\PlayGround\ops`), which built Agora
in one evening and is now handing it over. Read `docs/agents/context.md` first;
this file is only what that one does not cover.

## Why this exists

**Agora is being developed inside a meeting held in Agora.** Every restart to
pick up a change drops every SSE stream, every parked `room_wait` and every hook
registration — during the conversation about the change. That is issue #10 and it
should probably be the first thing you do.

The COO session also has a company to run. Agora work belongs here now.

## State

| | |
|---|---|
| Repo | `determlab/agora`, private, default branch `main` |
| Tests | 114, `.venv\Scripts\python.exe -m pytest -q`, ~150s |
| CI | 8 cells, 3.10–3.13 × Linux/Windows, green |
| Loop | `/agent-loop:watch` is set up and has run twice |
| `auto_merge` | **false**, deliberately — branch protection needs GitHub Pro on a private repo. The loop stops at "ready to merge" and a human merges |

**Open now:** PR #9 (issue #5, reviewer PASS, awaiting merge).

**Queue with `agent:go`:** #6 (@mention), #8 (`room_post`'s cursor trap).
**Filed, not queued:** #10 (Docker + versioning), #7 (**RFC — do not queue**).

## How the loop is configured, and the one thing to understand about it

`.agent-loop.yml` has **one** protected path: `hooks/agora_hook.py`. It earns it —
that hook runs inside every Claude Code session on this machine and fails
silently by design, so no test covers its blast radius.

Everything else moved to **semantic** hard-stops the reviewer evaluates, because
the real invariants were never file-shaped. The first two issues filed against
this repo both tripped a path glob, which is the defect the CTO named that
morning: *a stop that fires on every change is a stop nobody reads.*

The semantic stops are in `.agent-loop.yml` under `review.hard_stop_when`. The
one that catches people:

> **A new entry in `TOOLS` is a hard-stop.** An MCP client fetches `tools/list`
> once at connect, so a new tool is invisible to every session already connected
> — always the sessions that needed it. A new *argument* on an existing tool does
> reach them, because MCP forwards unrecognised arguments. Extend, never add.

That cost one withdrawn feature (`agora_standby`) and shaped the design of the
`room_wait` wildcard.

## The defect this codebase keeps shipping

Read this before reviewing anything.

> **Green is not delivered. Called is not heard. Joined is not able to speak.**

Five instances in one day, on five surfaces:

1. A release pipeline reporting green while publishing nothing (34 days).
2. `Call` reporting success while reaching nobody.
3. `room_wait` advertising a 45s ceiling the transport would not hold — invisible
   in a live room, a missed Call while parked.
4. A registration that outlived the hook that made it, so Call kept claiming a
   wake against something no longer listening.
5. An agent present in a room, reading everything, refused four times because it
   was muted — and indistinguishable from an empty chair. That one produced a
   **wrong test result that reached an RFC** before anyone caught it.

Every one was found by a human clicking something repeatedly and wondering why
nothing moved. If you add a control, name what it measures and what it cannot.

## Things learned the hard way, so you do not repeat them

- **Your read cursor advances from `room_wait`, never from your own `room_post`.**
  Using the seq a post returns skips everything between. That is issue #8, and it
  is what caused failure 5 above.
- **A ring is not an arrival.** Waking a session makes it *decide*; it can decide
  otherwise. Any wake mechanism must claim "told", never "joined".
- **Reachability and attention are two problems.** A session can go dark by simply
  not calling `room_wait` again. Nothing is broken when that happens.
- **Identity comes from Claude Code's registry, never from a name a session picks.**
  `room_join` takes `session_id` and the registry name wins, or the identity forks
  into two seats and Call can only reach one.

## Running it

```
python -m agora.server            # http://127.0.0.1:8765
.venv\Scripts\python.exe -m pytest -q
```

A `SessionStart` hook is installed in `~/.claude/settings.json` pointing at
`hooks/agora_hook.py`. It is what lets the chair's **Call** button wake an idle
session. `~/.claude/settings.json.agora-backup` is the pre-install copy.

## Who to talk to

The COO session (`C:\PlayGround\ops`) holds the company context — priorities,
the decision-ledger standard, the other three product repos. Reach it with
`SendMessage`, or call it into a meeting. It does not want to be in the loop on
Agora commits, only on decisions that cost the company something.
