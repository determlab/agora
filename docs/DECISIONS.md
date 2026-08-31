---
type: ledger
owner: repo-agent
scope: repo/agora
reviewed: 2026-09-01
---

# Agora — Decision Ledger

Locked architectural decisions. **Append; never silently re-litigate.**
Issues cite these by number. Superseding a decision is itself a decision.

Every row below already governed this repo before this file existed — it was
spread across `.agent-loop.yml` under `review.hard_stop_when`, the "Rules that
are not negotiable" section of `docs/agents/context.md`, and one README section.
Writing them here numbers them; it does not add any.

| # | Decision | Source |
|---|---|---|
| D1 | **A new entry in `TOOLS` is a hard stop — extend an existing tool with a new argument instead** — an MCP client fetches `tools/list` once at connect, so a new tool is invisible to every session already connected, which is always the sessions that needed it, and every one of them must restart. Unrecognised *arguments* are forwarded, so an argument reaches them. | `9b30d8a`; `agora_standby` added in `a7e111b` and withdrawn in `ea3f0d2`; the `room="*"` wildcard (PR #4) is this decision's shape |
| D2 | **No third-party import anywhere in `agora/` or `hooks/`; pytest is the only dev dependency** — this is why the MCP layer is hand-rolled, and it is what makes the app unbreakable by an SDK major. | `32e1efc`; asserted by `tests/test_dependencies.py`, which reads imports rather than the environment |
| D3 | **A control's success path must distinguish "it worked" from "it is queued" from "it reached nobody", and the UI must show which** — a control may never report a state it is not measuring. | `7cf3fa3`, `d61dbf4`, `3cfcc91` (PR #9, issue #5); the five instances are listed in `HANDOFF.md` |
| D4 | **The server binds to `127.0.0.1` and is deliberately unauthenticated — the loopback bind *is* the security boundary** — anyone who can reach the port can speak as the chair, so neither the bind nor the absence of auth changes except by a decision that supersedes this one. | `agora/server.py:696`; `.agent-loop.yml` `hard_stop_when`; this doc |
| D5 | **Agents arrive muted, and a muted `room_post` is refused with text telling the agent to keep reading** — never "warn and allow". The cost is that a muted agent looks exactly like an empty chair, so `muted` rides on the participant row and must stay visible to the chair (D3). | `cfc2be1`; failure 5 in `HANDOFF.md` |
| D6 | **Identity comes from Claude Code's session registry, never from a name a session picks** — `room_join` takes `session_id` and the registry name wins; a self-chosen name forks one agent into two seats and Call can reach only one of them. | `f4a872f`; enforced in `agora/mcp.py:361`, not by instruction |
| D7 | **Presence is polling, not membership** — a participant that joined and then died is not present, so anything reporting reachability derives it from a recent poll and never from a row existing. A ring is not an arrival: a wake mechanism claims "told", never "joined". | `3cfcc91` (PR #9, issue #5); `HANDOFF.md` |
| D8 | **A write never hands back a read cursor** — `room_post`, `room_note` and `room_summarize` return the seq their message landed at, and no second, cursor-shaped number beside it. A cursor means "the highest seq I have actually received", which only `room_wait` and `room_history` know; the room's tip at write time is the write's own seq, so returning it as `tip` would hand back exactly the number that loses the gap. The warning rides on the reply, not only on the tool description, because a description is fetched once at connect (D1) and never reaches a session already parked. | issue #8; room `23c152bd` — `room_post` returned 25, 25 was used as the next cursor, and seq 24 (`"CMO joined — muted"`) was never delivered while the session reported the CMO woken |

## Open decisions
<!-- Named, not yet decided. An issue that needs one of these is NOT ready for agent:go. -->

- **O1** — `hooks/agora_hook.py` still teaches the single-room `room_wait` rest
  state, not `room="*"` (D1's shape). The replacement text is written out and
  deliberately unapplied, because that file is the one protected path in
  `.agent-loop.yml`: it runs inside every Claude Code session on this machine and
  fails silently by design. Settled by a human applying it, restarting one
  session, and confirming it still registers and still wakes. Text in
  `CONTRIBUTING.md`.
- **O2** — whether Agora is versioned at all. `agora/__init__.py` says `0.1.0`,
  `SERVER_INFO` says `0.1.0`, the HTTP `Server:` header says `Agora/0.2`, and
  there are no git tags — three numbers, no release. Issue #10 (Docker +
  versioning). Settled by picking one source of truth or dropping the numbers.
- **O3** — the `agent:*` loop-state labels and the estate's triage labels
  (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`)
  are two axes in use across the estate with nothing reconciling them. Settled by
  the COO saying whether one vocabulary governs both, or they stay orthogonal by
  design. See `docs/agents/triage-labels.md`.

## Superseded
<!-- Keep the history. A decision that is replaced moves here with its replacement. -->

None yet. Nothing above has been superseded.
