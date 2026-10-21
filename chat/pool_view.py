"""Reader for the ``pool-view/1`` JSON file (agora#196). Pure, no network.

``load(path, now)`` answers ``{"view", "stale", "age_s", "errors"}`` and never
raises; ``check(obj)`` returns one line per broken field, naming its path.
Fields the contract does not list are ignored.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

SCHEMA = "pool-view/1"
STALE_AFTER_S = 15 * 60
STAGES = ("queue", "working", "ci", "review", "needs_human", "merged")
ZONES = ("local", "runner", "cloud")


def parse_time(s):
    """A UTC ISO 8601 string ending in Z -> aware datetime, or None."""
    if not isinstance(s, str) or not s.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError:
        return None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _time(v, path, errs, nullable):
    if v is None and nullable:
        return
    if parse_time(v) is None:
        errs.append(f"{path}: must be a UTC ISO 8601 time ending in Z"
                    + (" or null" if nullable else ""))


def _chip(c, path, errs):
    if not isinstance(c, dict):
        errs.append(f"{path}: must be an object")
        return
    for k in ("id", "title", "url", "note"):
        if not isinstance(c.get(k), str):
            errs.append(f"{path}.{k}: required string")
    if isinstance(c.get("url"), str) and not c["url"].startswith("https://"):
        errs.append(f"{path}.url: must be an https URL")
    if "since" not in c:
        errs.append(f"{path}.since: required (ISO time or null)")
    else:
        _time(c["since"], f"{path}.since", errs, True)
    if c.get("where") not in (None, "local", "cloud"):
        errs.append(f'{path}.where: must be "local", "cloud" or null')
    if not isinstance(c.get("fix_round", False), bool):
        errs.append(f"{path}.fix_round: must be true or false")
    if not isinstance(c.get("actions", []), list):
        errs.append(f"{path}.actions: must be a list")


def _part(p, path, errs):
    if not isinstance(p, dict):
        errs.append(f"{path}: must be an object")
        return
    for k in ("name", "note"):
        if not isinstance(p.get(k), str):
            errs.append(f"{path}.{k}: required string")
    if "ok" not in p or p["ok"] not in (True, False, None):
        errs.append(f"{path}.ok: required, true, false or null")


def _keyed(obj, name, keys, inner, one, errs):
    seq = obj.get(name)
    if not isinstance(seq, list):
        errs.append(f"{name}: required list")
        return
    if len(seq) != len(keys):
        errs.append(f"{name}: must hold exactly {', '.join(keys)} in this order, found {len(seq)} entries")
    for i, ent in enumerate(seq):
        path = f"{name}[{i}]"
        if not isinstance(ent, dict):
            errs.append(f"{path}: must be an object")
            continue
        if i < len(keys) and ent.get("key") != keys[i]:
            errs.append(f"{path}.key: must be {keys[i]!r} here, found {ent.get('key')!r}")
        items = ent.get(inner)
        if not isinstance(items, list):
            errs.append(f"{path}.{inner}: required list")
            continue
        for j, it in enumerate(items):
            one(it, f"{path}.{inner}[{j}]", errs)


def check(obj) -> list[str]:
    """Field errors of a parsed pool-view/1 object; [] when it is valid."""
    if not isinstance(obj, dict):
        return ["top level: must be a JSON object"]
    errs: list[str] = []
    if obj.get("schema") != SCHEMA:
        return [f"schema: found {obj.get('schema')!r}, this server reads {SCHEMA}"]
    if "generated_at" not in obj:
        errs.append("generated_at: required")
    else:
        _time(obj["generated_at"], "generated_at", errs, False)
    s = obj.get("summary")
    if not isinstance(s, dict):
        errs.append("summary: required object")
    else:
        for k in ("running", "queued"):
            if not _is_int(s.get(k)):
                errs.append(f"summary.{k}: required integer >= 0")
    al = obj.get("alerts")
    if not isinstance(al, list):
        errs.append("alerts: required list")
    else:
        for i, a in enumerate(al):
            if not isinstance(a, dict) or not isinstance(a.get("text"), str):
                errs.append(f"alerts[{i}].text: required string")
            elif "since" not in a:
                errs.append(f"alerts[{i}].since: required (ISO time or null)")
            else:
                _time(a["since"], f"alerts[{i}].since", errs, True)
    _keyed(obj, "stages", STAGES, "items", _chip, errs)
    _keyed(obj, "zones", ZONES, "parts", _part, errs)
    return errs


def load(path, now: datetime) -> dict:
    """The answer of GET /api/v1/dashboard/pool; ``now`` is an aware datetime."""
    def fail(msg):
        return {"view": None, "stale": False, "age_s": None, "errors": [msg]}
    if not path:
        return fail("pool view not set up: set AGORA_POOL_VIEW to the pool-view JSON file and restart")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return fail(f"cannot read the pool view file {path}: {e.strerror or e}; "
                    "fix AGORA_POOL_VIEW or have the producer write it")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        return fail(f"{path} is not JSON ({e}); this server reads {SCHEMA}")
    errs = check(obj)
    if errs:
        return {"view": None, "stale": False, "age_s": None, "errors": errs[:1]}
    age = int((now - parse_time(obj["generated_at"])).total_seconds())
    return {"view": obj, "stale": age > STALE_AFTER_S, "age_s": age, "errors": []}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
