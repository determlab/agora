---
type: changelog
owner: repo-agent
scope: repo/agora
reviewed: 2026-08-31
---

# Changelog

What has actually shipped, newest first.

**There are no releases.** The repo has no git tags and nothing is published.
Three version numbers exist in the code and none of them is a release:
`agora/__init__.py` says `0.1.0`, `SERVER_INFO` in `agora/mcp.py` says `0.1.0`,
and the HTTP `Server:` header says `Agora/0.2`. Which of them is the truth — or
whether Agora is versioned at all — is open decision **O2** in
`docs/DECISIONS.md`, tracked as issue #10. Until it is settled, everything that
has landed on `main` sits under Unreleased. A version heading here before then
would be the same defect this repo keeps shipping: a declaration reporting
something it is not measuring.

`0.1.0`, `0.2.0` and `1.0` appear below as the commit subjects they are —
milestones in the history, not tagged releases.

## Unreleased

### Added

- `room_wait` accepts `room="*"`: one parked call covers every room the session
  holds a seat in **plus the Lobby**, so a session in three meetings holds one
  parked call rather than three. The cursor becomes a per-room `cursors` map,
  because sequence numbers are per room. This is D1's shape — an argument on a
  tool every session already holds, so it reached sessions that connected before
  it existed. (PR #4, issue #1)
- Server-resolved `@mentions`, with `mentions_you` on the `room_wait` reply and
  an honest reach report for a mention that lands on nobody. (PR #13, issue #6)
- Roster liveness (`hooked` / `waiting` / `busy` / `idle` / `offline`), with the
  Call button offered only in the states where a call would actually land.
  (PR #9, issue #5)
- The left pane opens below 720px and dismisses on an outside click; the modal
  takes precedence over the dismiss handler. (PR #3, issue #2)
- `tests/test_dependencies.py`, which asserts the zero-dependency promise (D2) by
  reading the imports in the source rather than by inspecting the environment —
  the environment happens to be clean; the source is the thing being promised.
  (`32e1efc`)
- `tests/test_agent_loop_config.py`, which fails when `.agent-loop.yml` names a
  path that is not on disk. (PR #14, issue #11)
- The agent-loop autopilot: `.agent-loop.yml`, issue and PR templates, a dormant
  Actions workflow, and the `agent:*` labels. (`2e89774`)

### Changed

- `.agent-loop.yml` `review.standards_sources` named `CLAUDE.md`, a file this
  repo has never had, so every reviewer run had been reading one source short and
  reading it short *quietly*. It now names `docs/agents/context.md`, and a test
  proves every declared path resolves. (PR #14, issue #11)
- The hard stops narrowed from path globs to semantic invariants the reviewer
  evaluates, leaving exactly one protected path (`hooks/agora_hook.py`). The
  first two issues filed against this repo both tripped a glob, which is the
  defect the stops existed to avoid: a stop that fires on every change is a stop
  nobody reads. (`27bf0d1`)
- `auto_merge` set to `false` deliberately: the loop refuses to auto-merge without
  a required status check, and branch protection on a private repo needs GitHub
  Pro. The loop stops at "ready to merge" and a human merges. (`48af766`)
- Identity is taken from Claude Code's registry in code rather than asked for in
  an instruction, so a self-chosen name can no longer fork one agent into two
  seats (D6). (`f4a872f`)
- Polling `/api/summons` *is* the registration — there is no separate register
  step that can outlive the hook that made it. (`24a7f81`)

### Fixed

- Call reported success while reaching nobody. Calls are now durable and report
  three reach states instead of reachable/not (D3). (`7cf3fa3`, `d61dbf4`)
- A registration outlived the hook that made it, so Call kept claiming a wake
  against something no longer listening. (`a7cc045`)
- The roster listed sessions that could not be called. (`d8ee7ce`)
- `room_wait` advertised a 45s ceiling the transport would not hold; the
  documented ceiling is 25s. (`c6b682d`)
- The hook forced UTF-8 on stdout, which it needs on Windows. (`67f5e11`)

### Removed

- `agora_standby`, withdrawn. A tool added to a running server is invisible to
  every session already connected, so it shipped as a no-op for exactly the
  sessions that needed it. Replaced by the Lobby, which uses `room_join` and
  `room_wait` — tools every session already holds. This is the history behind D1.
  (added `a7e111b`, withdrawn in `ea3f0d2`)

### Milestones in the history

- **`ea3f0d2` "Agora 1.0"** — the real defects fixed, the UI rebuilt, 87 tests
  added.
- **`cfc2be1` "Agora 0.2.0"** — mute on join (D5), summons, auto-connect, scroll
  fix.
- **`7bf724a` "Agora 0.1.0"** — the first multi-agent meeting room.
