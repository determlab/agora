"""`.agent-loop.yml` points at files, and nothing checked that they are there.

It named `CLAUDE.md`, which this repo has never had, so every reviewer run read
one standards source short — and read it short *quietly*, because a source that
is not on disk looks exactly like a source with nothing to say. That is this
repo's own recurring defect wearing a config file: a declaration reporting
coverage it does not have.

Same class of check as `tests/test_ui.py`, one layer out: there, every URL the
page calls must be a route the server serves; here, every path the loop is told
to read must be a path that exists.

What this does NOT cover, said plainly because a config test invites the
assumption that the config is covered: it checks that a path *resolves*, never
that a key is *read*. A key the loop plugin ignores has no path to check and
passes here in silence — `review.hard_stop_when` carries this repo's four
semantic invariants and appears in no plugin file at all, and
`hard_stops.flag_on_public_api_change` sits between two keys that are read and
is read by nothing. A test that covers half a class, quietly, is the same defect
it was written to fix.

The config is parsed by hand rather than with PyYAML — Agora's dev dependency is
pytest and nothing else — which is safe only because the shape read here is two
known lists of plain scalars.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".agent-loop.yml"


def _paths_under(key: str) -> list[str]:
    """The `- item` entries nested under `key:`, comments and blanks skipped.

    Ends the block at the first content line indented no deeper than the key,
    which is what closes `protected_paths` — its list is followed by a long
    comment block and then a sibling key at the same indent.
    """
    items: list[str] = []
    depth: int | None = None
    for raw in CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if depth is None:
            if line == f"{key}:":
                depth = indent
            continue
        if indent <= depth:
            break
        if line.startswith("- "):
            items.append(line[2:].strip().strip('"').strip("'"))
    assert depth is not None, f"{key} is no longer declared in {CONFIG.name}"
    return items


@pytest.mark.parametrize("key", ["standards_sources", "protected_paths"])
def test_every_path_the_loop_declares_exists(key: str):
    listed = _paths_under(key)
    assert listed, f"{key} is empty — this test would pass vacuously"
    missing = [p for p in listed if not (ROOT / p).exists()]
    assert not missing, (
        f"{CONFIG.name} lists {missing} under {key}, which is not on disk. A "
        f"missing standards source is invisible: the reviewer silently reviews "
        f"against less than it was told to, and a protected path that matches "
        f"nothing guards nothing. Either the file moved and this list should "
        f"follow it, or the entry should go."
    )
