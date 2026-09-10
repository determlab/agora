"""Renders `determlab/ops` `roadmap.md` as a read-only tab (issue #31).

The file is the source of truth (COO-owned) and this module never holds its own
copy of the plan: every request reads the file fresh off disk and renders it.
What *is* cached is the expensive half — a milestone row's `<repo> #<N>`
references get their live GitHub state overlaid, and that means shelling out to
`gh`, which is slow and rate-limited. That half is cached for
`ISSUE_CACHE_TTL`; the file read and the render are not, which is what makes a
changed file show up on the very next refresh (D3 — never report a state you
did not measure, and a cached render of a file that has since changed is
exactly that).

No markdown library (D2): this is a line-based renderer for the specific subset
`roadmap.md` actually uses — headings, tables, bold/italic/code spans, links,
and ordered/unordered lists. The file is human-written in a repo this process
does not control, so every text node goes through `esc()` before anything else
touches it; the only HTML this module ever emits around that escaped text is
markup it built itself.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

#: How long a fetched issue's state is trusted before `gh` is asked again. One
#: clock for the whole batch, not one per reference, so a page left open for an
#: hour does not slowly leak `gh` calls one at a time — see IssueCache.batch.
ISSUE_CACHE_TTL = 600.0

#: A real ceiling on one `gh` call. A hung subprocess must not hang the request
#: asking for it; the chip falls back to "unknown" instead (D3).
GH_TIMEOUT = 8.0

#: `<repo> #<N>` — the repo name carries forward onto a bare `#N` that follows
#: in the same cell, which is how `shal #22, #123 (done)` reads as two refs in
#: `shal` rather than one ref plus a number nobody owns. Doc names with no `#`
#: (`adk.md R1`, `record.md, committed`) never match at all, which is the point:
#: not every "tracked-as" entry is an issue.
_REF_RE = re.compile(r"(?:([A-Za-z][\w.-]*)\s+)?#(\d+)")

_STATE_LABEL = {
    "open": "open", "working": "working",
    "needs-human": "needs human", "done": "done", "unknown": "unknown",
}


def esc(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def parse_refs(cell_text: str) -> list[tuple[str, int]]:
    """Every `<repo> #<N>` reference in one table cell, in order, repo-name
    inherited across a comma-separated run. See `_REF_RE` for why plain doc
    names never appear here."""
    refs: list[tuple[str, int]] = []
    last_repo: str | None = None
    for m in _REF_RE.finditer(cell_text):
        repo = m.group(1) or last_repo
        if repo is None:
            continue  # a bare #N before any repo has been named — not a ref
        last_repo = repo
        refs.append((repo, int(m.group(2))))
    return refs


def fetch_issue_state(repo: str, num: int, timeout: float = GH_TIMEOUT) -> str:
    """One issue's live state, honestly reported.

    Never a guess: `gh` missing, a timeout, a non-zero exit or unparsable JSON
    all come back as ``"unknown"`` rather than a silently wrong ``"open"`` — the
    same rule the rest of this app follows for reachability (D3). A real
    timeout, a list of literal arguments (never a shell string), never
    `shell=True` — the standard every other subprocess call in this codebase is
    held to, and this is the first one, so it sets it rather than assumes it.
    """
    try:
        proc = subprocess.run(
            ["gh", "issue", "view", str(num), "--repo", f"determlab/{repo}",
             "--json", "state,labels"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "unknown"
    labels = {str(entry.get("name", "")) for entry in data.get("labels", [])}
    if str(data.get("state", "")).upper() == "CLOSED" or "agent:done" in labels:
        return "done"
    if "agent:needs-human" in labels:
        return "needs-human"
    if "agent:working" in labels:
        return "working"
    return "open"


class IssueCache:
    """The batch cache `ISSUE_CACHE_TTL` describes.

    One roadmap render can name a dozen issues; without this each of the
    server's 10-minute refreshes would shell out a dozen times. Stale entries
    are re-fetched together as one batch per render rather than one at a time
    as each is noticed, which is what keeps a `gh` outage from turning into a
    dozen sequential timeouts.
    """

    def __init__(self, fetch=fetch_issue_state) -> None:
        self._fetch = fetch
        self._entries: dict[tuple[str, int], tuple[str, float]] = {}
        self._lock = threading.Lock()

    def batch(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        now = time.time()
        with self._lock:
            stale = [r for r in dict.fromkeys(refs)
                     if r not in self._entries
                     or now - self._entries[r][1] >= ISSUE_CACHE_TTL]
        # Fetched outside the lock: a `gh` call can take seconds, and holding
        # the lock across it would serialise every concurrent roadmap request
        # behind whichever one got there first.
        fresh = {ref: self._fetch(*ref) for ref in stale}
        with self._lock:
            for ref, state in fresh.items():
                self._entries[ref] = (state, now)
            return {ref: self._entries[ref][0] for ref in refs if ref in self._entries}


# ---- markdown-lite -----------------------------------------------------

def _inline(text: str) -> str:
    """Bold, italic, inline code, and `[text](http(s)://…)` links.

    Escaped first, so every regex below runs over text that is already safe —
    none of `*` `` ` `` `[` `]` `(` `)` are HTML-special, so escaping does not
    disturb them, and nothing after this function adds another layer of raw
    text into the output.
    """
    out = esc(text)
    out = re.sub(r"`([^`]+?)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
        out)
    return out


def _chips(refs: list[tuple[str, int]], states: dict[tuple[str, int], str]) -> str:
    if not refs:
        return ""
    spans = []
    for ref in refs:
        state = states.get(ref, "unknown")
        label = _STATE_LABEL.get(state, "unknown")
        spans.append(
            f'<span class="rm-chip rm-chip-{esc(state)}" dir="ltr">'
            f'{esc(ref[0])}#{ref[1]} · {esc(label)}</span>')
    return f'<span class="rm-chips">{"".join(spans)}</span>'


def _split_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+\.\s+(.*)$")
_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """Groups lines into table / heading / ul / ol / paragraph blocks.

    Line-based on purpose: every block type `roadmap.md` actually uses is a
    single line or a tight run of same-marker lines, never a wrapped paragraph
    that continues on the next line — checked against the file, not assumed.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[tuple[str, list[str]]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("|"):
            j = i
            group = []
            while j < n and lines[j].lstrip().startswith("|"):
                group.append(lines[j])
                j += 1
            blocks.append(("table", group))
            i = j
            continue
        if _HEADING_RE.match(line):
            blocks.append(("heading", [line]))
            i += 1
            continue
        if _UL_RE.match(line):
            j = i
            group = []
            while j < n and _UL_RE.match(lines[j]):
                group.append(lines[j])
                j += 1
            blocks.append(("ul", group))
            i = j
            continue
        if _OL_RE.match(line):
            j = i
            group = []
            while j < n and _OL_RE.match(lines[j]):
                group.append(lines[j])
                j += 1
            blocks.append(("ol", group))
            i = j
            continue
        blocks.append(("p", [line]))
        i += 1
    return blocks


def _tracked_column(header: list[str]) -> int | None:
    for idx, cell in enumerate(header):
        if cell.strip().lower() in ("tracked-as", "tracked as", "issues"):
            return idx
    return None


def _table_refs(group: list[str]) -> list[tuple[str, int]]:
    if len(group) < 2:
        return []
    header = _split_row(group[0])
    col = _tracked_column(header)
    if col is None:
        return []
    refs: list[tuple[str, int]] = []
    for line in group[2:]:  # [1] is the --- separator row
        cells = _split_row(line)
        if col < len(cells):
            refs += parse_refs(cells[col])
    return refs


def _render_table(group: list[str], states: dict[tuple[str, int], str]) -> str:
    if len(group) < 2 or not _SEP_RE.match(group[1]):
        # Not actually a table (no separator row) — fall back to paragraphs
        # rather than mis-render a `|`-containing line as a one-cell table.
        return "".join(f'<p dir="auto">{_inline(line)}</p>' for line in group)
    header = _split_row(group[0])
    col = _tracked_column(header)
    out = ["<table><thead><tr>"]
    out += [f'<th dir="auto">{_inline(c)}</th>' for c in header]
    out.append("</tr></thead><tbody>")
    for line in group[2:]:
        cells = _split_row(line)
        out.append("<tr>")
        for idx, c in enumerate(cells):
            body = _inline(c)
            if col is not None and idx == col:
                body += _chips(parse_refs(c), states)
            out.append(f'<td dir="auto">{body}</td>')
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


_FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)


def _strip_front_matter(text: str) -> str:
    """`ops/doc-standard.md` front-matter (`type`, `owner`, ...) is metadata for
    the doc tooling, not roadmap content — rendered as-is it reads as a wall of
    stray paragraphs above the actual heading."""
    return _FRONT_MATTER_RE.sub("", text, count=1)


def render_roadmap(text: str, cache: IssueCache) -> str:
    """The whole document as one escaped, self-contained HTML fragment."""
    blocks = _blocks(_strip_front_matter(text))
    all_refs: list[tuple[str, int]] = []
    for kind, group in blocks:
        if kind == "table":
            all_refs += _table_refs(group)
    states = cache.batch(all_refs) if all_refs else {}

    out: list[str] = ['<div class="roadmap-doc" dir="auto">']
    for kind, group in blocks:
        if kind == "table":
            out.append(_render_table(group, states))
        elif kind == "heading":
            m = _HEADING_RE.match(group[0])
            level = len(m.group(1))
            out.append(f'<h{level} dir="auto">{_inline(m.group(2).strip())}</h{level}>')
        elif kind == "ul":
            items = "".join(f'<li dir="auto">{_inline(_UL_RE.match(l).group(1))}</li>'
                            for l in group)
            out.append(f"<ul>{items}</ul>")
        elif kind == "ol":
            items = "".join(f'<li dir="auto">{_inline(_OL_RE.match(l).group(1))}</li>'
                            for l in group)
            out.append(f"<ol>{items}</ol>")
        else:
            out.append(f'<p dir="auto">{_inline(group[0])}</p>')
    out.append("</div>")
    return "".join(out)


def load_and_render(path: Path, cache: IssueCache) -> dict[str, Any]:
    """The `/api/roadmap` payload: an honest not-found rather than a crash or a
    fabricated empty roadmap (per the issue's own instruction)."""
    try:
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except OSError:
        return {"available": False, "path": str(path), "mtime": None,
                "html": None, "error": f"roadmap not found at {path}"}
    return {"available": True, "path": str(path), "mtime": mtime,
            "html": render_roadmap(text, cache), "error": None}
