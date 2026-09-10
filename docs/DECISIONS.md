---
type: ledger
owner: repo-agent
scope: repo/agora
reviewed: 2026-09-10
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
| D4 | **The server binds to `127.0.0.1` and is deliberately unauthenticated — the loopback bind *is* the security boundary** — anyone who can reach the port can speak as the chair, so neither the bind nor the absence of auth changes except by a decision that supersedes this one. | `agora/server.py` (`main`, the `--host` default — a line number here drifted the first time the file was touched); `.agent-loop.yml` `hard_stop_when`; this doc. The container publishes to `127.0.0.1` on the host and binds `0.0.0.0` **inside its own network namespace**, which is the same boundary one layer out, not a change to it (D9's issue). |
| D5 | **Agents arrive muted, and a muted `room_post` is refused with text telling the agent to keep reading** — never "warn and allow". The cost is that a muted agent looks exactly like an empty chair, so `muted` rides on the participant row and must stay visible to the chair (D3). | `cfc2be1`; failure 5 in `HANDOFF.md` |
| D6 | **Identity comes from Claude Code's session registry, never from a name a session picks** — `room_join` takes `session_id` and the registry name wins; a self-chosen name forks one agent into two seats and Call can reach only one of them. | `f4a872f`; enforced in `agora/mcp.py:361`, not by instruction |
| D7 | **Presence is polling, not membership** — a participant that joined and then died is not present, so anything reporting reachability derives it from a recent poll and never from a row existing. A ring is not an arrival: a wake mechanism claims "told", never "joined". | `3cfcc91` (PR #9, issue #5); `HANDOFF.md` |
| D8 | **A write never hands back a read cursor** — `room_post`, `room_note` and `room_summarize` return the seq their message landed at, and no second, cursor-shaped number beside it. A cursor means "the highest seq I have actually received", which only `room_wait` and `room_history` know; the room's tip at write time is the write's own seq, so returning it as `tip` would hand back exactly the number that loses the gap. The warning rides on the reply, not only on the tool description, because a description is fetched once at connect (D1) and never reaches a session already parked. | issue #8; room `23c152bd` — `room_post` returned 25, 25 was used as the next cursor, and seq 24 (`"CMO joined — muted"`) was never delivered while the session reported the CMO woken |
| D9 | **`agora/__init__.py`'s `__version__` is the only version string, and the image tag is that string** — the MCP `serverInfo`, the HTTP `Server:` header, `/api/state` and the page all read it rather than repeating it, and `docker build --build-arg AGORA_VERSION=$(python -c "import agora;print(agora.__version__)")` is what keeps `agora:<tag>` in step, so the tag can never name a build the code disagrees with. Bumping the number is a change to one line. This settles **O2**: three numbers disagreed (`__version__` 0.1.0, `SERVER_INFO` 0.1.0, `Server: Agora/0.2`) and none of them was released, so the reconciled version is 0.3.0 rather than either. The chair must be able to answer "which version is this" from the browser, because Agora is developed inside a meeting held in Agora and behaviour changes under people mid-conversation. | issue #10; `tests/test_version.py` fails if the four surfaces or the Dockerfile drift apart |
| D10 | **A heartbeat row reports only what was actually measured — a field nobody sent is `not_reported`/`no_data`, never guessed as green or red.** `alive`/`listening` are real today, read from the roster and from `Room.touch`'s `last_seen` (the same evidence the roster pane already shows). `timers`/`loop` ride `/api/register`'s payload as two new *optional* fields — additive, so every existing caller including `hooks/agora_hook.py` is unaffected — but nothing sends them yet: sending them requires extending the hook itself, which is out of scope here because the hook is a protected path (`.agent-loop.yml`) and needs its own issue and a human's sign-off on that diff specifically. Queue depth and last-loop-PR are real only for this server's own working tree, via a short-timeout local `gh` call keyed off a session's reported `cwd`; every other repo this server cannot open reports `no_data`, never a fabricated number. The three red rules (not listening >10min in a room; a reported timer with no next fire; a reported loop stale past 2x a *stated* period with a non-empty queue) each refuse to fire on a field marked `not_reported`/`no_data` — inventing a red state from an absent measurement is the same defect as inventing a green one. This is D3 applied to timers and loops, after five real incidents of the same shape: something assumed running was not, and nothing said so. **Filed as D10; may need renumbering to D11 at merge time** if the D10 pending on the unmerged issue #25 branch lands first — this branch cannot know which merges first. | issue #26; `agora/heartbeat.py`; `tests/test_heartbeat.py`; `ops/priorities.md` "Operating model — week 1"; the 2026-09-09 all-hands |

| D11 | **The roadmap tab reads `ops/roadmap.md` from a local path, not the GitHub API** — the issue offered either a local path or `gh api repos/determlab/ops/contents/roadmap.md`, and the local path won because this server already runs on the same machine as `ops`, it has no token or rate-limit surface, and the file read can happen on every request while only the per-issue `gh` state lookups need the 10-minute cache. The path is configurable (`--roadmap-path` / `AGORA_ROADMAP_PATH`), default `../ops/roadmap.md` relative to this repo — this machine's actual layout, not a guess. A missing file is reported honestly (`available: false`, an error string) rather than crashing or rendering a fabricated empty roadmap (D3's shape). The renderer is a hand-rolled line-based markdown-lite subset (headings, tables, bold/italic/code, links, lists) rather than a pip dependency (D2), and every text node from the file goes through the same escape discipline as any other untrusted text this app renders, because a human-authored file in a repo this process does not control is still not trusted input. **This row was numbered D11, not D10, on purpose:** two other in-flight branches (`agent/25-rtl-direction`, `agent/26-heartbeat-page`) each add a pending D10 that has not landed on `main` yet; by the time this branch merges, at least one of them likely will have taken D10 first. | issue #31 |

| D12 | **The chat rebuild's step 1 (issue #37) proved the collecting works, and picked stdlib-only over the `zulip` pip client.** Founder decision: the chat is rebuilt on Zulip because its bot API is a long-poll event queue that holds messages while a bot is busy for minutes and resumes from the last event id when it returns — a Slack/Discord bot must acknowledge within three seconds or the message is retried and lost, and no chat product can push into a busy Claude Code session, so the bot has to come and collect. The durability test (`bot/durability_test.py`) is the number this was run to produce: register a queue, do **not** poll for a real 3 minutes while 6 messages send from a second thread spaced across the gap, resume — **all 6 arrived, in order, resumed from the stored event id.** Zulip's REST API (HTTP Basic auth, JSON/form bodies) is fully reachable with stdlib `urllib`+`json`; the one place the official `zulip` client would have bought something — long-poll resume semantics — is exactly what `bot/resume.py` + `bot/test_resume.py` (mocked HTTP, no live server) exist to pin down instead, so no `requirements.txt` was added (D2's shape, extended to a new top-level dir outside `agora/`/`hooks/` in spirit though not in D2's literal wording). Zulip itself runs in Docker (`bot/docker-compose.yml`, based on `zulip/docker-zulip`'s own published compose, 11.x branch) bound to `127.0.0.1:8090` only (D4's spirit, one layer out). Getting a *browser* logged in against that container needed three fixes in sequence, each confirmed by trying the alternative first: `EXTERNAL_HOST` needs a dot (`localhost` alone fails Zulip's own validation) but a domain that only *resolves* to loopback (`<ip>.nip.io`) is not a browser "potentially trustworthy origin," so Secure-flagged session cookies silently don't send over plain HTTP; `*.localhost` (RFC 6761) resolves to loopback with zero configuration *and* is treated as trustworthy, so it was the one hostname family that satisfied both Zulip and the browser at once. Out of scope here (later issues, per the issue itself): migrating rooms/transcripts/other roles, the status/roadmap tab, retiring the existing 8765 server. | issue #37; `bot/docker-compose.yml`, `bot/zulip_client.py`, `bot/resume.py`, `bot/durability_test.py`, `bot/test_resume.py`, `bot/README.md` |

## Open decisions
<!-- Named, not yet decided. An issue that needs one of these is NOT ready for agent:go. -->

- **O1** — `hooks/agora_hook.py` still teaches the single-room `room_wait` rest
  state, not `room="*"` (D1's shape). The replacement text is written out and
  deliberately unapplied, because that file is a protected path in
  `.agent-loop.yml`: it runs inside every Claude Code session on this machine and
  fails silently by design. Settled by a human applying it, restarting one
  session, and confirming it still registers and still wakes. Text in
  `CONTRIBUTING.md`.
- **O3** — the `agent:*` loop-state labels and the estate's triage labels
  (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`)
  are two axes in use across the estate with nothing reconciling them. Settled by
  the COO saying whether one vocabulary governs both, or they stay orthogonal by
  design. See `docs/agents/triage-labels.md`.

## Settled
<!-- An open decision that was decided. It keeps its old number so an issue that
     cited it still resolves, and it names the row that now governs. -->

- **O2** — *whether Agora is versioned at all.* Settled by issue #10 as **D9**:
  `agora/__init__.py` holds the only version string, everything else reads it,
  and the image tag follows it. The number is 0.3.0, not 0.1.0 or 0.2 — neither
  of those was ever released, so adopting either would have claimed a release
  that did not happen.

## Superseded
<!-- Keep the history. A decision that is replaced moves here with its replacement. -->

None yet. Nothing above has been superseded.
