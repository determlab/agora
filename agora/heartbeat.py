"""Issue #26 — "make the sessions boring": one honest row per session.

This exists because of five real incidents, all the same shape: something was
assumed to be running and was not, and nothing said so. The fix is not a
prettier dashboard, it is the same discipline this repo already enforces
elsewhere (D3) applied to timers and loops: **report only what was actually
measured, and say plainly when something was not.**

Two things are real today and computed here honestly:

* ``alive`` / ``listening`` — from the roster this server already builds
  (``discovery.roster``, ``Summons``) and from ``Participant.last_seen``,
  which `room_wait` touches on every poll (``Room.touch``). This is the same
  data the left-hand roster pane already shows; nothing new is measured.
* ``queue_depth`` / loop activity **for this repo only** — derived locally via
  ``gh``/``git`` from the working tree this server itself runs from. Other
  sessions (COO, CTO, CMO, SHAL, Bricks, AOS, ADK, ...) live in other repos
  this process has no way to open, so their rows say ``no_data`` rather than
  guessing.

Two things are NOT real yet, on purpose:

* ``timers`` — a session's cron registrations (watchdog / daily / weekly /
  loop). The issue asks for these to ride the hook's registration payload,
  but ``hooks/agora_hook.py`` is a protected path (it runs inside every Claude
  Code session on this machine and fails silently by design) and extending it
  is explicitly out of scope for this change. `/api/register` will *accept*
  ``timers``/``loop`` if a caller sends them (nothing does yet), so the shape
  below is ready the day something does. Until then every row says
  ``not_reported`` — not red, not green, a third state, because reporting
  "armed" for something nobody told us about is exactly the defect this issue
  exists to remove.
* ``loop`` (a session's own last-run/outcome) — same story, same reason.

The three red rules from the issue are evaluated in `evaluate_red`, and every
one of them refuses to fire on data marked ``not_reported`` / ``no_data`` —
see the acceptance criteria: "do not invent a red state for data marked
'not reported'".
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

#: "not listening for > 10 min while in a room" (the issue's own number).
#: `Room.touch` refreshes `last_seen` on every `room_wait`, so this measures
#: real polling activity, not a heartbeat file nobody wrote.
LISTENING_STALE_AFTER = 10 * 60.0

#: How long a locally-derived `gh` snapshot is trusted before asking again.
#: `gh` is a network call; a heartbeat page refreshing every few seconds must
#: not turn into a `gh` call every few seconds.
_REPO_CACHE_TTL = 20.0

#: Never let a shelled-out `gh` call hang the request/response cycle. Short and
#: fixed, per the issue's own constraint — fail to "no data", do not block.
_GH_TIMEOUT = 4.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_repo_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_repo_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# session-level: alive / listening (real, from data this server already has)
# ---------------------------------------------------------------------------

def listening(name: str, rooms: list[str], hub: Any) -> bool | None:
    """Whether *name* has polled `room_wait` recently, in any room it sits in.

    ``None`` means the rule does not apply — the issue's red rule is scoped to
    "while in a room", so a session that is alive but seated nowhere is
    neither green nor red on this axis, it is not measured. ``rooms`` includes
    the Lobby when the session is parked there, since a parked `room_wait` on
    the Lobby is exactly the "listening" this measures.
    """
    if not rooms:
        return None
    freshest = 0.0
    for room_id in rooms:
        room = hub.get(room_id)
        if room is None:
            continue
        p = room.participants.get(name)
        if p and p.last_seen:
            freshest = max(freshest, p.last_seen)
    if freshest == 0.0:
        # Seated somewhere, per the roster, but this process has never actually
        # heard a `room_wait` from it in that room. Measured absence, not a
        # missing measurement — that is still "not listening".
        return False
    return (time.time() - freshest) < LISTENING_STALE_AFTER


# ---------------------------------------------------------------------------
# timers / loop: real only once something sends them; "not_reported" until then
# ---------------------------------------------------------------------------

def timers_field(registration: dict[str, Any] | None) -> dict[str, Any]:
    """The `timers` a session's hook registration carried, if any did.

    Nothing sends this yet (see module docstring), so in practice this is
    always ``not_reported`` today. It stays a real read of `/api/register`'s
    stored payload rather than a placeholder, so the day a caller sends
    `timers` this starts reporting it with no further change here.
    """
    items = (registration or {}).get("timers")
    if not isinstance(items, list):
        return {"status": "not_reported", "items": []}
    return {"status": "reported", "items": items}


def loop_field(registration: dict[str, Any] | None) -> dict[str, Any]:
    """The `loop` a session's hook registration carried, if any did. See
    `timers_field` — same story, same reason, currently always not_reported."""
    loop = (registration or {}).get("loop")
    if not isinstance(loop, dict):
        return {"status": "not_reported"}
    return {"status": "reported", **loop}


# ---------------------------------------------------------------------------
# repo-derived: real for THIS repo's own working tree, no_data for any other
# ---------------------------------------------------------------------------

def _run(runner: Runner, args: list[str], cwd: Path) -> list[Any] | None:
    """One `gh` call, JSON-decoded. Any failure — not installed, not
    authenticated, no network, timeout, not a repo `gh` recognises — comes
    back as None rather than raising, because "no data" is the honest answer
    to all of those and this function has no way to tell them apart, nor does
    it need to (D3: it reports what it measured, and a failed call measured
    nothing)."""
    try:
        proc = runner(args, cwd=str(cwd), capture_output=True, text=True,
                      timeout=_GH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None


def repo_snapshot(root: Path, *, runner: Runner = subprocess.run,
                  fresh: bool = False) -> dict[str, Any]:
    """This repo's own queue depth and newest loop PR — never any other repo.

    Deliberately local and narrow: this server process cannot open COO's,
    CTO's, CMO's, SHAL's, Bricks', AOS's or ADK's repos, so it never tries.
    What it can honestly do is ask `gh`, from its own working tree, about
    itself — the same repo this code is running in.

    Cached briefly (`_REPO_CACHE_TTL`) because this is a network call and the
    heartbeat page may poll every few seconds.
    """
    key = str(root)
    if not fresh:
        with _repo_cache_lock:
            hit = _repo_cache.get(key)
            if hit and (time.time() - hit[0]) < _REPO_CACHE_TTL:
                return hit[1]

    queue_depth: dict[str, Any] = {"status": "no_data", "value": None}
    last_loop_pr: dict[str, Any] = {"status": "no_data"}

    if shutil.which("gh") is not None and (root / ".git").exists():
        issues = _run(runner, ["gh", "issue", "list", "--label", "agent:go",
                               "--state", "open", "--json", "number"], root)
        if issues is not None:
            queue_depth = {"status": "reported", "value": len(issues)}

        prs = _run(runner, ["gh", "pr", "list", "--state", "all", "--limit", "1",
                            "--json", "number,title,url,createdAt,mergedAt,state"],
                   root)
        if prs is not None:
            last_loop_pr = ({"status": "reported", **prs[0]} if prs
                            else {"status": "reported", "note": "no PRs yet"})

    snapshot = {"queue_depth": queue_depth, "last_loop_pr": last_loop_pr}
    with _repo_cache_lock:
        _repo_cache[key] = (time.time(), snapshot)
    return snapshot


def _is_this_repo(cwd: str, root: Path) -> bool:
    """Is *cwd* (a session's own reported working directory) this repo?

    The only honest way to attach repo-derived data to a row: a name like
    "SHAL" tells this server nothing about which repo that session is in, but
    a `cwd` it reported does. Rows with no `cwd` (most `room`-sourced rows,
    and any registration that never sent one) get no_data rather than a guess.

    `cwd` is client-supplied, so `/api/register` is unauthenticated by design
    (D4) — a crafted registration can claim this repo's root and pick up this
    server's own queue depth and last loop PR under whatever name it likes.
    That only discloses this server's own already-public repo state under the
    wrong label; it does not reach a shell (see `_repo_snapshot`, which runs a
    fixed argument list, never a string built from a session's fields) and it
    does not cost another row its real data. Cosmetic, not a hole.
    """
    if not cwd:
        return False
    try:
        return Path(cwd).resolve() == root.resolve()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# the three red rules — only on data actually present
# ---------------------------------------------------------------------------

def evaluate_red(row: dict[str, Any]) -> tuple[bool, list[str]]:
    """The issue's three red rules, applied only to data this row actually
    has. A field marked `not_reported` / `no_data` never turns a row red —
    that would be reporting a state nobody measured, the exact defect #26
    exists to remove.
    """
    reasons: list[str] = []

    if row.get("listening") is False:
        reasons.append("not listening for over 10 minutes while in a room")

    timers = row.get("timers") or {}
    if timers.get("status") == "reported":
        for t in timers.get("items", []):
            if isinstance(t, dict) and t.get("next"):
                continue
            tname = t.get("name", "?") if isinstance(t, dict) else "?"
            reasons.append(f"timer {tname!r} is not armed — no next fire time")

    loop = row.get("loop") or {}
    queue = row.get("queue_depth") or {}
    if (loop.get("status") == "reported" and queue.get("status") == "reported"
            and (queue.get("value") or 0) > 0):
        # A period is not derivable from a `cron` string without a parser this
        # repo does not carry (D2: stdlib only, no croniter). Rather than
        # guess one, this rule only fires when the loop payload states its own
        # `period_seconds` explicitly. Absent that, the rule is not evaluated
        # — not "not red", genuinely not checked, and that gap is real and
        # named in the PR notes rather than papered over.
        period = loop.get("period_seconds")
        last_run = loop.get("last_run")
        if isinstance(period, (int, float)) and period > 0 and isinstance(
                last_run, (int, float)):
            age = time.time() - last_run
            if age > 2 * period:
                reasons.append(
                    f"loop last ran {int(age)}s ago, more than 2x its "
                    f"{int(period)}s period, with {queue['value']} queued")

    return (bool(reasons), reasons)


def build_row(session_row: dict[str, Any], hub: Any,
             registrations: dict[str, dict[str, Any]], root: Path,
             repo_data: dict[str, Any]) -> dict[str, Any]:
    """One heartbeat row, built from data this server actually has.

    ``session_row`` is one entry from `discovery.roster()` (what the existing
    left-hand roster pane already renders) — reused rather than re-derived, so
    "alive" here can never disagree with "alive" there.
    """
    name = session_row["name"]
    rooms = session_row.get("rooms") or []
    reg = registrations.get(name)
    this_repo = _is_this_repo(session_row.get("cwd", ""), root)

    row = {
        "name": name,
        "provider": session_row.get("provider", ""),
        "source": session_row.get("source", ""),
        "rooms": rooms,
        # `liveness` is the same honest computation `_state()` already makes
        # (hooked / busy / idle / offline) — "alive" collapses it to the one
        # bit the heartbeat page needs, without recomputing it differently.
        "liveness": session_row.get("liveness"),
        "alive": session_row.get("liveness") != "offline",
        "listening": listening(name, rooms, hub),
        "timers": timers_field(reg),
        "loop": loop_field(reg),
        "queue_depth": (repo_data["queue_depth"] if this_repo
                        else {"status": "no_data", "value": None}),
        "last_loop_pr": (repo_data["last_loop_pr"] if this_repo
                         else {"status": "no_data"}),
        "this_repo": this_repo,
    }
    row["red"], row["red_reasons"] = evaluate_red(row)
    return row
