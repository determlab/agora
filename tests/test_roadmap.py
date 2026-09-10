"""Issue #31 — the roadmap tab.

Two things need their own coverage per the issue's DoD: the reference parser
(which `tracked-as`/`Issues` entries are actually `<repo> #<N>` issues, and
which are plain doc names) and the fetch path (honest `unknown` on any `gh`
failure, never a guess — D3). The rest is exercised through `/api/roadmap` on
a real server, matching how this repo tests everything else at the HTTP seam.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agora.roadmap import (IssueCache, fetch_issue_state, load_and_render,
                           parse_refs, render_roadmap)

# ---- the reference parser ------------------------------------------------

@pytest.mark.parametrize("cell,expected", [
    # The exact shapes ops/roadmap.md's "Issues" column actually uses.
    ("adk.md R1", []),
    ("adk.md R4", []),
    ("shal #5 (revive)", [("shal", 5)]),
    ("shal #22, #123 (done)", [("shal", 22), ("shal", 123)]),
    ("record.md, committed", []),
    ("record.md, first issue", []),
    ("posts-queue, listening note", []),
    ("shal #22 and bricks #9", [("shal", 22), ("bricks", 9)]),
])
def test_reference_parser_distinguishes_issue_refs_from_plain_doc_names(cell, expected):
    assert parse_refs(cell) == expected


def test_a_bare_hash_with_no_repo_named_anywhere_in_the_cell_is_not_a_ref():
    assert parse_refs("#22 alone") == []


# ---- the fetch path -------------------------------------------------------

def _run(returncode=0, stdout="", raise_=None):
    def fake(*a, **k):
        if raise_:
            raise raise_
        r = subprocess.CompletedProcess(a, returncode)
        r.stdout = stdout
        return r
    return fake


def test_a_closed_issue_is_done(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        _run(0, '{"state":"CLOSED","labels":[]}'))
    assert fetch_issue_state("shal", 1) == "done"


def test_agent_done_label_is_done_even_if_open(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        _run(0, '{"state":"OPEN","labels":[{"name":"agent:done"}]}'))
    assert fetch_issue_state("shal", 1) == "done"


def test_needs_human_label(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        _run(0, '{"state":"OPEN","labels":[{"name":"agent:needs-human"}]}'))
    assert fetch_issue_state("shal", 1) == "needs-human"


def test_working_label(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        _run(0, '{"state":"OPEN","labels":[{"name":"agent:working"}]}'))
    assert fetch_issue_state("shal", 1) == "working"


def test_a_plain_open_issue_with_no_labels(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run(0, '{"state":"OPEN","labels":[]}'))
    assert fetch_issue_state("shal", 1) == "open"


def test_gh_not_installed_is_unknown_never_a_guess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run(raise_=FileNotFoundError()))
    assert fetch_issue_state("shal", 1) == "unknown"


def test_a_gh_timeout_is_unknown_never_a_guess(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        _run(raise_=subprocess.TimeoutExpired(cmd="gh", timeout=8)))
    assert fetch_issue_state("shal", 1) == "unknown"


def test_a_nonzero_exit_is_unknown_never_a_guess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run(1, ""))
    assert fetch_issue_state("shal", 1) == "unknown"


def test_unparsable_json_is_unknown_never_a_guess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run(0, "not json"))
    assert fetch_issue_state("shal", 1) == "unknown"


def test_gh_is_called_with_a_real_timeout_and_no_shell(monkeypatch):
    """Same standard as any other subprocess call in this codebase: a real
    timeout, argv as a list, never shell=True, never a string-interpolated
    command."""
    seen = {}
    def fake(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        r = subprocess.CompletedProcess(cmd, 0)
        r.stdout = '{"state":"OPEN","labels":[]}'
        return r
    monkeypatch.setattr(subprocess, "run", fake)
    fetch_issue_state("shal", 22)
    assert isinstance(seen["cmd"], list)
    assert seen["kwargs"].get("shell", False) is False
    assert seen["kwargs"]["timeout"] and seen["kwargs"]["timeout"] > 0
    assert "determlab/shal" in seen["cmd"]
    assert "22" in seen["cmd"]


# ---- the batch cache -------------------------------------------------------

def test_the_batch_cache_fetches_each_ref_once_and_reuses_it():
    calls = []
    def counting(repo, num, timeout=8.0):
        calls.append((repo, num))
        return "open"
    cache = IssueCache(fetch=counting)
    refs = [("shal", 22), ("shal", 123), ("shal", 22)]  # a duplicate in one batch
    states = cache.batch(refs)
    assert states == {("shal", 22): "open", ("shal", 123): "open"}
    assert calls.count(("shal", 22)) == 1, "a ref repeated in one batch is fetched once"
    calls.clear()
    cache.batch(refs)
    assert calls == [], "a fresh cache entry must not be re-fetched inside the TTL"


# ---- rendering --------------------------------------------------------------

def test_render_escapes_hostile_content_in_the_source_file():
    """The file is written by a human in a repo this process does not control
    (ops/roadmap.md) — still escaped, same as anything else rendered from
    someone else's text."""
    cache = IssueCache(fetch=lambda repo, num, timeout=8.0: "open")
    html = render_roadmap("# <script>alert(1)</script>\n\nSome **bold** text.\n",
                          cache)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>bold</strong>" in html


def test_render_adds_a_chip_for_each_issue_ref_in_the_tracked_column():
    cache = IssueCache(fetch=lambda repo, num, timeout=8.0: "done" if num == 22 else "open")
    md = (
        "| # | Milestone | Issues |\n"
        "|---|---|---|\n"
        "| M1 | The ADK is named | adk.md R1 |\n"
        "| M7 | The first ten minutes | shal #22, #123 (done) |\n"
    )
    html = render_roadmap(md, cache)
    assert 'rm-chip-done" dir="ltr">shal#22' in html
    assert 'rm-chip-open" dir="ltr">shal#123' in html
    # M1's row names no issue ("adk.md R1" is a doc name, not a ref, per
    # parse_refs) and gets no chips at all — one row per <tr>...</tr>.
    m1_row = html.split("adk.md R1")[1].split("</tr>")[0]
    assert "rm-chips" not in m1_row


def test_a_milestone_with_a_failed_lookup_shows_unknown_not_a_guess():
    cache = IssueCache(fetch=lambda repo, num, timeout=8.0: "unknown")
    md = "| Issues |\n|---|\n| shal #9 |\n"
    html = render_roadmap(md, cache)
    assert "rm-chip-unknown" in html


def test_render_strips_the_doc_standard_front_matter():
    """`ops/doc-standard.md` front-matter is metadata for the doc tooling, not
    roadmap content — it should not show up as a wall of stray paragraphs."""
    html = render_roadmap(
        "---\ntype: plan\nowner: COO\n---\n\n# Roadmap\n\nBody text.\n",
        IssueCache(fetch=lambda r, n, timeout=8.0: "open"))
    assert "type: plan" not in html
    assert "owner: COO" not in html
    assert "<h1" in html and "Body text." in html


def test_render_handles_headings_lists_and_bold():
    html = render_roadmap(
        "## Stage 1\n\n- a bullet\n- another\n\n1. first\n2. second\n\n**bold** and *italic*.\n",
        IssueCache(fetch=lambda r, n, timeout=8.0: "open"))
    assert "<h2" in html
    assert "<li" in html and "<ul>" in html and "<ol>" in html
    assert "<strong>bold</strong>" in html and "<em>italic</em>" in html


# ---- load_and_render / the honest not-found state --------------------------

def test_a_missing_roadmap_file_is_reported_honestly_not_a_crash(tmp_path: Path):
    missing = tmp_path / "nope" / "roadmap.md"
    result = load_and_render(missing, IssueCache(fetch=lambda r, n, timeout=8.0: "open"))
    assert result["available"] is False
    assert result["html"] is None
    assert str(missing) in result["error"]


def test_an_existing_roadmap_file_renders(tmp_path: Path):
    f = tmp_path / "roadmap.md"
    f.write_text("# Roadmap\n\nSome text.\n", encoding="utf-8")
    result = load_and_render(f, IssueCache(fetch=lambda r, n, timeout=8.0: "open"))
    assert result["available"] is True
    assert "<h1" in result["html"]
    assert result["mtime"] is not None


# ---- the route --------------------------------------------------------------

def test_the_route_reads_the_real_local_file_and_returns_escaped_content(
        server, tmp_path: Path):
    f = tmp_path / "roadmap.md"
    f.write_text(
        "# Roadmap\n\n"
        "| # | Milestone | Issues |\n|---|---|---|\n"
        "| M1 | <b>x</b> | shal #22 |\n",
        encoding="utf-8")
    server.app.roadmap_path = f
    server.app.roadmap_cache = IssueCache(fetch=lambda r, n, timeout=8.0: "done")
    status, body = server.get("/api/roadmap")
    assert status == 200
    assert body["available"] is True
    assert "<h1" in body["html"]
    assert "&lt;b&gt;x&lt;/b&gt;" in body["html"]   # escaped, not executed
    assert "rm-chip-done" in body["html"]


def test_the_route_reports_not_found_honestly(server, tmp_path: Path):
    server.app.roadmap_path = tmp_path / "does" / "not" / "exist.md"
    status, body = server.get("/api/roadmap")
    assert status == 200
    assert body["available"] is False
    assert body["html"] is None
    assert "roadmap not found" in body["error"]
