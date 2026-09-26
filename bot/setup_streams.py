"""Idempotent setup for the four Zulip streams, three role bots and the
Watchdog bot this repo's chat rebuild needs (issues #40, #49).

**The issue body describes nine per-repo streams. That is superseded** — a
later comment quoting the founder directly ("i dont need per repo i just need
CMO CTO and you") replaced it with four streams total:

  #cto #cmo #coo   one per role session; the founder talks to a person, not
                   a repository.
  #status          one pinned message the ops watchdog rewrites; nobody
                   else posts there.

Role bot users are unchanged in name (COO, CTO, CMO) but each is subscribed
to **its own stream only** — the CTO bot cannot read or write #cmo. A fourth
bot, **Watchdog** (issue #49, COO amendment), is the one poster in #status. It
is subscribed to #status and #coo and nothing else: it rewrites the status
message, and it posts to #coo to wake the COO session — a bot's own posts
never wake its own session (#50), so this cannot be the COO bot. No other bot
may be subscribed to #status, and the only bot besides a role's own bot
allowed in a role stream is Watchdog, in #coo. Pool workers get no bot user
and no subscription (RFC-004: their state is the label, their voice is the
PR). See docs/DECISIONS.md D13.

The topic inside a stream is still the issue number, e.g. `#141 record
shape`; a decision gets a ledger id, e.g. `D12 event queue resume`
(topic = the issue number — unchanged from the original issue body).

Idempotent: run twice, the second run creates nothing. Every "does X exist"
check reads Zulip's own state back (list_streams / list_users /
stream_subscribers) rather than trusting a previous run's exit code, so it
stays correct even if a stream or bot was created by hand in between.

Usage:
  .venv/Scripts/python.exe bot/setup_streams.py            # create what's missing
  .venv/Scripts/python.exe bot/setup_streams.py --check     # verify only, exit 1 if incomplete

Needs an ADMIN account's credentials (realm-admin actions: creating streams,
subscribing other users), not a bot's — ZULIP_ADMIN_EMAIL / ZULIP_ADMIN_API_KEY,
read from the environment or from bot/.env next to this file. Never hardcode
a key: bot/.env is gitignored, same as the bot credentials already there.
"""

from __future__ import annotations

import os
import sys

from zulip_client import ZulipClient, ZulipError

# stream name -> is it a role stream (a bot belongs here) or the watchdog-only one
STREAMS = ["cto", "cmo", "coo", "status"]

# (bot full_name, short_name). short_name is also the stream the bot owns —
# short_name becomes the email's local part (Zulip's own convention:
# "<short_name>-bot@<realm domain>"), confirmed against the live stack rather
# than assumed.
ROLE_BOTS = [("CTO", "cto"), ("CMO", "cmo"), ("COO", "coo")]

STATUS_STREAM = "status"

# The ops watchdog's own bot (issue #49): the one poster in #status, and it
# posts to #coo to wake the COO. Same (full_name, short_name) convention as
# ROLE_BOTS, so its email is "watchdog-bot@<realm domain>".
WATCHDOG_BOT = ("Watchdog", "watchdog")
WATCHDOG_STREAMS = {STATUS_STREAM, "coo"}


def _load_dotenv(path: str) -> None:
    """Tiny stdlib .env loader — no python-dotenv (D2). Only fills in
    variables not already set, so a real environment variable always wins."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def load_admin_client() -> ZulipClient:
    _load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    site = os.environ.get("ZULIP_SITE", "http://zulip.localhost:8090")
    email = os.environ.get("ZULIP_ADMIN_EMAIL", "")
    api_key = os.environ.get("ZULIP_ADMIN_API_KEY", "")
    if not email or not api_key:
        sys.exit(
            "ZULIP_ADMIN_EMAIL and ZULIP_ADMIN_API_KEY must be set (bot/.env). "
            "The admin is the founder's own account: he copies his key from "
            "Zulip Settings > Account & privacy > API key. See bot/README.md; "
            "never hardcode a key."
        )
    return ZulipClient(site, email, api_key)


# -- idempotent primitives -------------------------------------------------


def ensure_stream(client: ZulipClient, name: str) -> tuple[int, str]:
    """Returns (stream_id, action) where action is "created", "made private"
    or "already exists". Reads state first — never creates blind. A stream
    that exists but is public (the first build of #40 created them public) is
    converted in place: the issue asks for "all private"."""
    streams = {s["name"]: s for s in client.list_streams()}
    if name in streams:
        stream = streams[name]
        if not stream.get("invite_only"):
            client.make_private(stream["stream_id"])
            return stream["stream_id"], "made private"
        return stream["stream_id"], "already exists"
    client.create_stream(name)
    streams = {s["name"]: s["stream_id"] for s in client.list_streams()}
    return streams[name], "created"


def ensure_bot(client: ZulipClient, full_name: str, short_name: str) -> tuple[str, int, bool]:
    """Returns (email, user_id, created). Matched by full_name — realm-
    agnostic, unlike guessing the email domain from ZULIP_SITE."""
    for u in client.list_users():
        if u.get("is_bot") and u["full_name"] == full_name:
            return u["email"], u["user_id"], False
    result = client.create_bot(full_name, short_name)
    user_id = result["user_id"]
    for u in client.list_users():
        if u["user_id"] == user_id:
            return u["email"], user_id, True
    raise RuntimeError(f"bot {full_name} created (user_id={user_id}) but not found in list_users")


def ensure_subscribed(client: ZulipClient, stream_id: int, bot_email: str, bot_user_id: int) -> bool:
    """Returns True if a subscribe call was made. Checked against the
    stream's actual subscriber list, not against whether this script has
    already run — a bot subscribed by hand still counts as done."""
    if bot_user_id in client.stream_subscribers(stream_id):
        return False
    client.subscribe(stream_id_to_name(client, stream_id), [bot_email])
    return True


def stream_id_to_name(client: ZulipClient, stream_id: int) -> str:
    for s in client.list_streams():
        if s["stream_id"] == stream_id:
            return s["name"]
    raise RuntimeError(f"stream_id {stream_id} not found")


# -- setup ------------------------------------------------------------------


def run_setup(client: ZulipClient) -> None:
    stream_ids: dict[str, int] = {}
    for name in STREAMS:
        stream_id, action = ensure_stream(client, name)
        stream_ids[name] = stream_id
        print(f"[setup] stream #{name}: {action} (id={stream_id})")

    for full_name, short_name in ROLE_BOTS:
        email, user_id, created = ensure_bot(client, full_name, short_name)
        print(f"[setup] bot {full_name}: {'created' if created else 'already exists'} ({email})")

        own_stream_id = stream_ids[short_name]
        subscribed = ensure_subscribed(client, own_stream_id, email, user_id)
        print(
            f"[setup]   subscribed {full_name} to #{short_name}: "
            f"{'done now' if subscribed else 'already subscribed'}"
        )

        # The hard version of chair-mute (issue #40): confirm the bot is NOT
        # in any of the other three streams, including #status. A bot that
        # ends up subscribed elsewhere (by hand, or by a future bug) can read
        # and post into a conversation it should not — this is the actual
        # invariant, not just "the one subscribe call we made succeeded".
        for other_name, other_id in stream_ids.items():
            if other_name == short_name:
                continue
            if user_id in client.stream_subscribers(other_id):
                print(
                    f"[setup]   WARNING {full_name} is also subscribed to "
                    f"#{other_name} — expected only #{short_name}. Not removing "
                    f"automatically; a human should look at this."
                )

    full_name, short_name = WATCHDOG_BOT
    email, user_id, created = ensure_bot(client, full_name, short_name)
    print(f"[setup] bot {full_name}: {'created' if created else 'already exists'} ({email})")
    for name in sorted(WATCHDOG_STREAMS):
        subscribed = ensure_subscribed(client, stream_ids[name], email, user_id)
        print(
            f"[setup]   subscribed {full_name} to #{name}: "
            f"{'done now' if subscribed else 'already subscribed'}"
        )
    for other_name, other_id in stream_ids.items():
        if other_name in WATCHDOG_STREAMS:
            continue
        if user_id in client.stream_subscribers(other_id):
            print(
                f"[setup]   WARNING {full_name} is also subscribed to "
                f"#{other_name} — expected only {sorted(WATCHDOG_STREAMS)}. Not "
                f"removing automatically; a human should look at this."
            )


# -- check --------------------------------------------------------------


def run_check(client: ZulipClient) -> bool:
    ok = True
    all_streams = client.list_streams()
    streams = {s["name"]: s["stream_id"] for s in all_streams}
    private = {s["name"]: bool(s.get("invite_only")) for s in all_streams}
    users = {u["full_name"]: u for u in client.list_users() if u.get("is_bot")}

    for name in STREAMS:
        if name not in streams:
            print(f"[check] stream #{name}: MISSING — run bot/setup_streams.py to create it")
            ok = False
        elif not private[name]:
            print(
                f"[check] stream #{name}: PUBLIC — must be private (any member could read "
                f"and join it); run bot/setup_streams.py to convert it"
            )
            ok = False
        else:
            print(f"[check] stream #{name}: OK, private (id={streams[name]})")

    for full_name, short_name in ROLE_BOTS:
        bot = users.get(full_name)
        if bot is None:
            print(f"[check] bot {full_name}: MISSING")
            ok = False
            continue
        print(f"[check] bot {full_name}: OK ({bot['email']})")

        subscribed_to = [
            name for name, sid in streams.items() if bot["user_id"] in client.stream_subscribers(sid)
        ]
        expected = {short_name}
        if set(subscribed_to) == expected:
            print(f"[check]   subscriptions: OK ({subscribed_to})")
        else:
            print(f"[check]   subscriptions: WRONG — expected {sorted(expected)}, got {sorted(subscribed_to)}")
            ok = False

    # The Watchdog bot (issue #49) must exist and be in exactly #status and
    # #coo. Missing is a FAIL, not a pass: without it nothing can post the
    # status message or wake the COO, and an empty #status would look fine.
    wd_name, _ = WATCHDOG_BOT
    watchdog = users.get(wd_name)
    if watchdog is None:
        print(f"[check] bot {wd_name}: MISSING — run bot/setup_streams.py to create it")
        ok = False
    else:
        print(f"[check] bot {wd_name}: OK ({watchdog['email']})")
        subscribed_to = [
            name for name, sid in streams.items() if watchdog["user_id"] in client.stream_subscribers(sid)
        ]
        if set(subscribed_to) == WATCHDOG_STREAMS:
            print(f"[check]   subscriptions: OK ({sorted(subscribed_to)})")
        else:
            print(
                f"[check]   subscriptions: WRONG — expected {sorted(WATCHDOG_STREAMS)}, "
                f"got {sorted(subscribed_to)}"
            )
            ok = False

    # Per stream, over ALL bots in the realm (not only the ones named above),
    # since a stray bot is exactly the failure this check exists for:
    # #status holds the Watchdog bot and no other bot; a role stream holds its
    # own role bot, plus Watchdog in #coo only.
    watchdog_id = watchdog["user_id"] if watchdog else None
    role_bot_ids = {short: users[full]["user_id"] for full, short in ROLE_BOTS if full in users}
    for name in STREAMS:
        if name not in streams:
            continue
        subs = set(client.stream_subscribers(streams[name]))
        allowed = set()
        if name in role_bot_ids:
            allowed.add(role_bot_ids[name])
        if name in WATCHDOG_STREAMS and watchdog_id is not None:
            allowed.add(watchdog_id)
        stray = sorted(full for full, u in users.items() if u["user_id"] in subs and u["user_id"] not in allowed)
        if stray:
            print(f"[check] #{name}: FAIL — bots that must not be subscribed: {stray}")
            ok = False
        elif name == STATUS_STREAM:
            if watchdog_id is not None and watchdog_id in subs:
                print(f"[check] #{name}: OK (only {wd_name} subscribed)")
            else:
                print(f"[check] #{name}: FAIL — {wd_name} is not subscribed, so nothing can post here")
                ok = False

    return ok


def main() -> None:
    client = load_admin_client()
    try:
        if "--check" in sys.argv:
            ok = run_check(client)
            sys.exit(0 if ok else 1)
        run_setup(client)
    except ZulipError as e:
        sys.exit(
            f"setup_streams: {e}\n"
            "If this is a permission error, ZULIP_ADMIN_* must belong to a realm "
            "administrator, not a bot."
        )


if __name__ == "__main__":
    main()
