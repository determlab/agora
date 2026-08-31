---
type: agent-context
owner: repo-agent
scope: repo/agora
reviewed: 2026-09-01
---

# Agora — context for the coder and the reviewer

Read this before touching anything. It is short on purpose; what it says about
failure modes matters more than the file map.

## What this is

A local web app where one human ("the chair") runs a **meeting** with live coding
agents. The chair sits in a browser. The agents are Claude Code, Codex, Cursor or
Gemini sessions that join over MCP.

One process serves three things:

| | |
|---|---|
| `/mcp` | JSON-RPC over HTTP. Every agent comes through here. |
| `/api/*` | REST + SSE for the browser: rooms, roster, chair controls. |
| `/` | the whole UI, one HTML file. |

Bound to `127.0.0.1`, unauthenticated on purpose: **the loopback bind is the
boundary.** Do not add auth without being asked, and do not bind to `0.0.0.0`.

## Architecture

```
agora/room.py        Room + Hub. Append-only JSONL per room, replayed on start.
                     `wait_for` is the long-poll primitive the whole app rests on.
agora/mcp.py         MCP over HTTP, hand-rolled JSON-RPC. No SDK, on purpose.
agora/discovery.py   Reads ~/.claude/sessions/*.json — who is running right now.
                     `availability()` says why the roster is empty when it is;
                     in a container the answer is never "nobody is running".
agora/server.py      HTTP, SSE, chair controls, the summons registry.
static/index.html    The entire UI. No build step, no framework, no CDN.
hooks/agora_hook.py  A Claude Code SessionStart hook. Runs in OTHER sessions.
tests/               149 tests. pytest, nothing else.
```

## Rules that are not negotiable

**Zero runtime dependencies.** Standard library only. This is why the app cannot
be broken by an SDK major, and it is the reason the MCP layer is hand-rolled.
pytest is the only dev dependency.

**Never report success for something that did not happen.** This app has shipped
the same defect three times, on three different surfaces: a control that reports
success while reaching nothing. Every one of them was found by a human clicking
a button repeatedly and wondering why nothing moved. If you add a control, its
result must distinguish *it worked*, *it is queued*, and *it reached nobody* —
and the UI must show which.

> **Green is not delivered. Called is not heard. Joined is not able to speak.**

Every failure here has had that one shape: a control reporting a state it is not
measuring. Before adding one, name what it measures and what it cannot.

**Agents arrive muted.** A muted `room_post` is refused, and the refusal text
tells the agent to keep reading rather than that it errored. Do not change this
to "warn and allow".

**Your read position comes from what you read, never from what you wrote.**
`room_post`, `room_note` and `room_summarize` return where the message landed;
only `room_wait` advances a cursor. Reusing a write's seq silently skips
everything said between the last wait and the write — it has already cost one
room a false "the CMO was woken".

**Identity comes from Claude Code's registry, never from a name a session picks.**
A self-chosen name forks the identity: two seats, one of which the Call button
can never reach. `room_join` takes `session_id` and the registry name wins.

**A blueprint of a rule: presence is polling, not membership.** A participant that
joined and then died is not present. Anything that reports reachability must be
derived from a recent poll, not from a row existing.

## The one non-obvious constraint

**A new MCP tool cannot reach an already-connected session.** A client fetches
`tools/list` once, at connect. A tool added to a running server is invisible to
every session that connected before it — which is always the sessions that needed
it. This already cost one feature (`agora_standby`, withdrawn).

**New *arguments* to existing tools do reach them**, because MCP forwards
unrecognised arguments. So: extend an existing tool, do not add one. If a change
genuinely needs a new tool, say so and flag it — it means every session must
restart.

## Build, test, run

```
python -m pip install pytest
python -m pytest              # 149 tests, ~150s (several exercise real long polls)
python -m agora.server        # http://127.0.0.1:8765
```

There is also a container, and the reason for it is above: this app is developed
inside a meeting held in itself, so the stable instance (8765) and the scratch
one (8766) have to be separate processes. `CONTRIBUTING.md`, "Running it in
Docker", has the build, the port convention, and the two things the container
costs — discovery does not work in it, and a restart still drops the summons
registry.

The API and MCP suites run a **real server on a real socket**. That is deliberate:
most of what has broken here broke at the protocol seam — HTTP/1.0 closing a long
poll, a 204 with a body desyncing keep-alive, an SSE stream with no
`Content-Length`. None of that is reachable by calling a handler method.

`tests/test_ui.py` checks the seam the page cannot be unit-tested across: every
URL the page calls is a route the server serves, every admin action it sends is
one the server understands, every element id the script reaches for exists in the
markup. All three fail silently in a browser.

## Who writes this file

The repo agent, and the role has a charter across the estate:
`ops/agents/repo-agent.md`, in the `determlab/ops` working copy. The
basis for what it owns is **verifiability, not seniority**: a sentence like *"the
denial path writes a run record"* cannot be written from a distance, so a
document whose truth is checkable only against the code belongs to the session
that can open the file. That covers this file, `docs/DECISIONS.md`,
`CHANGELOG.md`, `CONTRIBUTING.md` and `HANDOFF.md`.

Two consequences that bite in practice. `README.md` is **not** on that list — it
belongs to the CMO, so a wrong README is reported rather than fixed. And the repo
agent never applies `agent:go` to an issue, including in its own repo: approval
climbs a ladder (`ops/agents/decision-ledger-standard.md` §6) precisely so that
the session doing the work is not the one authorising it.

## Definition of done

- The issue's acceptance criteria are met, literally.
- `python -m pytest` is green. A new behaviour comes with a test; a bug fix comes
  with the test that would have caught it.
- No new runtime dependency.
- If you touched reachability, presence, or the summons registry: a test proving
  the honest-reporting rule above still holds.
- Comments explain *why*, not *what*. This codebase is dense with reasons; match
  it. A comment that restates the line above it is noise.
