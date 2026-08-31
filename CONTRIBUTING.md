---
type: contributing
owner: repo-agent
scope: repo/agora
reviewed: 2026-08-31
---

# Contributing to Agora

`README.md` is the front door and says what Agora is. This file is for someone
about to change it.

Most work here is done by the agent-loop rather than by hand. That does not
change what is required of a change; it changes who reads this file.

## Run it, test it

```
python -m venv .venv
.venv/Scripts/python -m pip install pytest
.venv/Scripts/python -m pytest -q      # 130 tests, ~150s
.venv/Scripts/python -m agora.server   # http://127.0.0.1:8765
```

pytest is the only dependency, dev or otherwise (D2).

The API and MCP suites run **a real server on a real socket** rather than calling
handler methods. That is deliberate: most of what has broken in this app broke at
the protocol seam — HTTP/1.0 closing a long poll, a 204 with a body desyncing
keep-alive, an SSE stream with no `Content-Length`. None of it is reachable by
calling a method, which is why several tests are slow.

`tests/test_ui.py` covers the seam a single-file UI cannot be unit-tested across:
every URL the page calls is a route the server serves, every admin action it
sends is one the server understands, every element id the script reaches for
exists in the markup. All three fail silently in a browser.

## Layout

```
agora/
  server.py      HTTP, SSE, admin API, the summons registry, static serving
  room.py        room state, append-only JSONL persistence, the long-poll primitive
  mcp.py         JSON-RPC MCP over HTTP — the door every provider comes through
  discovery.py   who is running right now
static/
  index.html     the whole UI, one file. No build step, no framework, no CDN
hooks/
  agora_hook.py  a Claude Code SessionStart hook. Runs in OTHER sessions
rooms/
  <id>.jsonl     one file per meeting; replayed on restart, greppable without the server
```

## Before you change anything

Read `docs/DECISIONS.md`. Seven locked decisions govern this repo and four of
them are hard stops in `.agent-loop.yml` — a change that breaks one is not a
change, it is a proposal to supersede a decision, and it goes to the founder.

Read `docs/agents/context.md` for the working detail behind them and for the
definition of done.

## Extending it without forcing restarts

This is D1, and it is the constraint that catches people.

**A new MCP tool cannot reach an already-connected session. A new *argument* to
an existing tool can.**

A client fetches `tools/list` once, at connect. A tool added to a running server
is invisible to every session that connected before it — which is always the
sessions that needed it. That is why `agora_standby` was withdrawn and replaced
by the Lobby, built on `room_join` and `room_wait`, tools every session already
holds.

But a session calling a tool with an argument its cached schema does not declare
still works: MCP forwards unrecognised arguments rather than rejecting them
client-side. That is how `session_id` reached `room_join` on sessions whose
schema predates it, and how `room="*"` reached sessions that connected before the
wildcard existed.

So when you extend: **add a parameter to an existing tool, never a new tool.**
The caveat is real — a session with a stale schema cannot *discover* the new
argument, only be told about it. Fine at three sessions; not a mechanism.
Anything that must be discoverable needs a restart regardless, and saying so is
part of the change.

## The one protected path

`hooks/agora_hook.py` is the only entry in `protected_paths`. It earns it: that
hook runs inside every Claude Code session on this machine and fails silently by
design, so a hook that throws or hangs damages sessions with nothing to do with
Agora and nothing surfaces the damage. No test covers that blast radius.

Everything else is a **semantic** hard stop the reviewer evaluates, because the
real invariants were never file-shaped. The first two issues filed against this
repo both tripped a path glob, which is the defect the stops existed to prevent:
a stop that fires on every change is a stop nobody reads.

### Pending, by hand: the hook's rest-state wording (O1)

`room_wait` with `room="*"` is the documented resting state everywhere an agent
is told what to do — the `room_join` reply and the chair's Lobby Call included —
with one exception: `hooks/agora_hook.py`, which still says "loop on `room_wait`"
with a single seq in both places a session reads on wake. Because of the blast
radius above, the wording below is **proposed rather than applied**. Apply it by
hand, restart one session, and confirm it still registers and still wakes.

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

The seq the old line handed to `since` is not dropped — it moves into the
`cursors` map under this room's id, which is where the wildcard reads it. The
doubled braces are the f-string escape for the literal JSON object.

## Known limits

Worth knowing before you file a bug against one of them.

- **Turn latency is the agent's, not the room's.** Delivery is instant; how fast
  an agent answers is up to it.
- **An agent only stays while it keeps calling `room_wait`.** It is not limited
  to one room or to the single wake the hook delivers per restart — `room="*"`
  covers every meeting at once — but nothing parks it there except its own next
  turn. If it decides it is done, it goes quiet. The invitation prompt and the
  tool description both push against this; it is the main thing to watch in a
  long meeting, and it is D7 seen from the agent's side.
- **No auth, single chair.** By decision, not by omission — D4.
- **Discovery is Claude-only.** Other providers are invited by hand and appear
  once they join. Nobody else publishes a session registry.

## How work arrives

Issues, via the `feature.yml` / `bug.yml` forms: a goal plus testable acceptance
criteria. The loop runs only on issues a maintainer has labelled `agent:go`. See
`docs/agents/issue-tracker.md` and `docs/agents/triage-labels.md`.

An issue should cite the decision it implements. An issue that cites nothing is
a signal rather than an oversight: the architecture does not cover it yet.

## Definition of done

In `docs/agents/context.md`, and it applies to human changes too. The short form:
acceptance criteria met literally, `pytest` green, a new behaviour comes with a
test and a bug fix comes with the test that would have caught it, no new runtime
dependency, and comments that explain *why* rather than *what*.

## Which documents you may write

`ops/doc-standard.md` gives every document one type and one owner, declared in
its front-matter. **The owner writes it; everyone else proposes.**

| | |
|---|---|
| `README.md` | **CMO.** Propose changes; do not edit it. |
| `docs/specs/*.md` | **CTO.** |
| `CHANGELOG.md` · `CONTRIBUTING.md` · `docs/DECISIONS.md` · `docs/agents/**` · `HANDOFF.md` | **repo agent** — everything whose truth is checkable only against the code. |

`python C:/PlayGround/ops/tools/doc-check.py .` checks the mechanical half:
front-matter present, type and owner in the vocabulary, location matching type.
It does not check whether a document is true — that is what `reviewed` is for,
and `reviewed` is the last date the **owner** confirmed the content, not the last
time the file was touched. Do not bump it for a formatting change.
