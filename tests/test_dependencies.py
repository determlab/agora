"""The zero-dependency promise, asserted rather than trusted.

It is the reason the MCP layer is hand-rolled: nothing here can be broken by an
SDK major, and `python -m agora.server` works on any Python 3.10+ with no install
step. That is easy to lose one convenient import at a time, and a review is not a
reliable place to catch it.

Checked by reading the imports, not by inspecting the environment — a CI runner's
image ships all sorts of packages, and what matters is what *this code* reaches
for.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([*(ROOT / "agora").rglob("*.py"), *(ROOT / "hooks").rglob("*.py")])

#: Allowed on top of the standard library: the package itself, and pytest inside
#: the suite. Nothing else — adding to this list is the decision this test exists
#: to make visible.
FIRST_PARTY = {"agora", "hooks", "tests"}


def _imported_modules(path: Path) -> set[str]:
    """Top-level module names this file imports, relative imports excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # `from .room import ...` — first-party
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def test_there_are_sources_to_check():
    assert SOURCES, "no sources found — this test would pass vacuously"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_every_import_is_standard_library(path: Path):
    outside = {
        name for name in _imported_modules(path)
        if name not in sys.stdlib_module_names and name not in FIRST_PARTY
    }
    assert not outside, (
        f"{path.relative_to(ROOT)} imports {sorted(outside)}, which is outside the "
        f"standard library. Agora has no runtime dependencies on purpose — that is "
        f"why the MCP layer is hand-rolled and why the app cannot be broken by an "
        f"SDK major. If this dependency is genuinely necessary, that is a decision "
        f"to take deliberately, not a line to slip in."
    )


def test_the_app_starts_from_a_bare_interpreter():
    """Importing the server is the real end of the promise: if anything it pulls
    in needs installing, `python -m agora.server` fails on a clean machine."""
    import agora.server  # noqa: F401  — the import IS the assertion
