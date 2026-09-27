"""Every container in the local Zulip stack has a memory cap (issue #57).

The founder's machine is shared with other projects, and `vmmemWSL` was taking
the room they need. The fix is a `mem_limit` on each of the five services in
`bot/docker-compose.yml`, about 2 GB in total. The failure this guards is the
quiet one: a sixth service added later, or one limit dropped in an edit, and
the stack is uncapped again with nothing saying so until the machine slows.

What this does NOT prove, said plainly: that Docker applied the limits. It
reads the file, never the running containers, and a limit changes nothing
until `docker compose up -d` recreates the container. `docker stats
--no-stream` is the check for that, and it cannot run in CI.

Parsed by hand rather than with PyYAML — pytest is the only dev dependency —
which is safe only because the shape read here is fixed: two-space service
names under `services:`, each with a plain-scalar `mem_limit:` directly inside.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "bot" / "docker-compose.yml"

SERVICES = {"zulip", "database", "redis", "rabbitmq", "memcached"}
_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _limits() -> dict[str, str | None]:
    """Each service's `mem_limit` value, or None if it declares none."""
    found: dict[str, str | None] = {}
    in_services = False
    current: str | None = None
    for raw in COMPOSE.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0:
            in_services = line == "services:"
            current = None
            continue
        if not in_services:
            continue
        if indent == 2 and line.endswith(":"):
            current = line[:-1]
            found[current] = None
        elif indent == 4 and current and line.startswith("mem_limit:"):
            found[current] = line.split(":", 1)[1].strip().strip('"').strip("'")
    return found


def _bytes(value: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([bkmg]?)b?", value.lower())
    assert m, f"mem_limit {value!r} is not a size this test understands"
    return int(m.group(1)) * _UNITS[m.group(2)]


def test_the_compose_file_declares_the_five_services():
    # Guards the parser as much as the file: if the shape changed and nothing
    # was found, every assertion below would pass on an empty dict.
    assert set(_limits()) == SERVICES


def test_every_service_has_a_memory_limit():
    missing = sorted(s for s, v in _limits().items() if not v)
    assert not missing, (
        f"{missing} in {COMPOSE.name} have no mem_limit. An uncapped container "
        f"can take the memory the founder needs for other projects (#57)."
    )


def test_the_limits_total_about_two_gigabytes():
    total = sum(_bytes(v) for v in _limits().values() if v)
    # A band, not an exact figure: the issue says tune from `docker stats`,
    # so a service may move a little. Leaving the band is a decision, and it
    # should be made in the issue, not by an edit to this file.
    assert 1.5 * 1024**3 <= total <= 2.5 * 1024**3, (
        f"the Zulip stack's limits total {total / 1024**3:.2f} GB; #57 asks "
        f"for about 2 GB."
    )
