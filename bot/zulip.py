"""A role session's voice in Zulip: ``send`` and ``read``, called through Bash.

A small CLI rather than an MCP server (CTO ruling, 2026-09-10, issue #42): a
tool added to a running MCP server is invisible to every session already
connected (D1). A script is read fresh on every call.

Rooms (ops#322): a room is a stream whose messages all go to one fixed topic,
``chat``. ``--room`` is the way to name one; ``--stream``/``--topic`` still work.

Usage:
  python bot/zulip.py send --room coo --text "..."
  python bot/zulip.py --as PM send --dm founder --text "..." [--json]   # sends one private message to the founder
  python bot/zulip.py read --room coo [--since ID] [--json]     # the whole room, every old topic too
  python bot/zulip.py wait --room coo [--seconds N] [--json]    # exit 2 with the next message there
  python bot/zulip.py invite --room coo --who CTO --json        # refused: the founder invites from the page
  python bot/zulip.py send --stream coo --topic "#141 record shape" --text "..."
  python bot/zulip.py send --stream coo --topic "#141 record shape" --text "..." --reply-to ID
  python bot/zulip.py read --stream coo --topic "#141 record shape" [--since ID]
  python bot/zulip.py read --mentions [--since ID]
  python bot/zulip.py --as POOL read --stream pool --since ID --json
  python bot/zulip.py wait [--session KEY] [--seconds N]
  python bot/zulip.py archive|unarchive --stream coo [--topic "old"] [--json]
  python bot/zulip.py unread [--json]
  python bot/zulip.py mark-read --stream coo --topic "#141 record shape" [--json]
  python bot/zulip.py mark-read --stream coo [--json]   # every topic in the stream
  python bot/zulip.py react --message ID --emoji thumbs_up [--json]      # ops#249
  python bot/zulip.py unreact --message ID --emoji thumbs_up [--json]   # ops#249
  python bot/zulip.py typing --stream coo --topic "#141 record shape" [--status working] [--json]
  python bot/zulip.py typing --stream coo --topic "#141 record shape" --stop [--json]

Posts and reads as the bot named by ``ZULIP_BOT_EMAIL`` / ``ZULIP_BOT_API_KEY``,
or with ``--as COO`` as the bot in ``ZULIP_COO_EMAIL`` / ``ZULIP_COO_API_KEY``.
The environment wins; the gitignored ``bot/.env`` fills in the rest. Never a
key in this file.

A refusal looks like a refusal (D3): a stream that does not exist, or one the
bot is not subscribed to, is named on stderr and the exit code is non-zero.
Nothing is printed as sent unless Zulip returned the new message's id.

Stdlib only, through ``zulip_client`` (D2).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.error
from datetime import datetime, timezone
from typing import Any

from zulip_client import ZulipClient, ZulipError

DOTENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
READ_LIMIT = 100  # newest N when there is no --since
ROOM_TOPIC = "chat"  # ops#322: every message in a room goes to this one topic
# The role bots `invite` would take (setup_streams.py makes them). Never a person.
INVITE_ROLES = ("COO", "CTO", "PM", "CMO", "CPSO", "PA")
INVITE_OFF = ("a role session cannot invite: the founder invites {who} with the Invite "
              "button in the room #{room} on the chat page. Ask him to.")


def _dotenv(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
    return out


def load_client(as_name: str | None) -> ZulipClient:
    """The bot for this call. Exits 2 with the variable names if unset."""
    env = _dotenv(DOTENV)

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or env.get(key) or default

    prefix = "ZULIP_BOT"
    if as_name:
        prefix = "ZULIP_" + re.sub(r"[^A-Z0-9]+", "_", as_name.upper()).strip("_")
    email, api_key = get(f"{prefix}_EMAIL"), get(f"{prefix}_API_KEY")
    if not email or not api_key:
        msg = (f"error: {prefix}_EMAIL and {prefix}_API_KEY are not set "
               f"(environment or bot/.env).")
        if as_name and as_name.upper() == "PA":
            msg += (f"\nAdd the two lines {prefix}_EMAIL=... and {prefix}_API_KEY=... to bot/.env; "
                    f"`python bot/setup_streams.py` (the founder runs it) creates the PA bot and "
                    f"prints them.")
        sys.exit(msg)
    site = get("ZULIP_SITE", "http://zulip.localhost:8090")
    client = ZulipClient(site, email, api_key, timeout=30.0)
    client.email = email
    client.api_key = api_key
    return client


def _subscribed(client: ZulipClient) -> set[str]:
    payload = client._request("GET", "users/me/subscriptions")
    return {s["name"] for s in payload.get("subscriptions", [])}


def _stream_exists(client: ZulipClient, stream: str) -> bool:
    try:
        client._request("GET", "get_stream_id", {"stream": stream})
        return True
    except ZulipError:
        return False


# The page's own convention (chat/page.html's parseQuote/quoteBlock, ops#249
# batch 1): "@_**Name** [said](#near/ID):" (the link is this app's own
# marker, not a real Zulip narrow URL — there is no narrow router here),
# then a ```quote fence, then the reply. A quote predating the link has no
# id and is simply not a jump target; still parses.
_QUOTE_RE = re.compile(
    r"^@_\*\*([^*|]+)(?:\|\d+)?\*\*(?: \[said\]\(#near/(\d+)\))?[^\n]*:\n"
    r"```quote\n(.*?)\n```\n?(.*)$", re.S)


def _quote_rest(content: str) -> str:
    """The reply half of ``content``, with any existing quote stripped —
    quoting a quoted message quotes only the one message, not its chain."""
    m = _QUOTE_RE.match(content)
    return m.group(4) if m else content


def quote_block(name: str, content: str, message_id: int) -> str:
    return f"@_**{name}** [said](#near/{message_id}):\n```quote\n{_quote_rest(content)}\n```\n"


def cmd_send(client: ZulipClient, stream: str, topic: str, text: str,
            reply_to: int | None = None) -> int:
    stream = stream.lstrip("#")
    if stream not in _subscribed(client):
        if _stream_exists(client, stream):
            print(f"refused: {client.email} is not subscribed to #{stream}. "
                  f"Nothing was sent.", file=sys.stderr)
        else:
            print(f"refused: #{stream} does not exist, or {client.email} "
                  f"cannot see it. Nothing was sent.", file=sys.stderr)
        return 2
    if reply_to:
        try:
            orig = client._request("GET", f"messages/{reply_to}")["message"]
        except ZulipError as exc:
            print(f"refused: cannot read message {reply_to} to quote: {exc}",
                  file=sys.stderr)
            return 2
        text = quote_block(orig["sender_full_name"], orig["content"], reply_to) + text
    sent = client.send_message(stream, text, topic=topic)
    msg_id = sent.get("id")
    if not msg_id:
        print(f"refused: Zulip answered without a message id: {sent}",
              file=sys.stderr)
        return 1
    where = f"#{stream}" if topic == ROOM_TOPIC else f"#{stream} › {topic}"
    print(f"sent {where} id={msg_id}")
    return 0


def cmd_send_dm(client: ZulipClient, who: str, text: str, as_json: bool = False) -> int:
    """``send --dm founder`` (issue #150): one private message to the founder,
    addressed by ``FOUNDER_ZULIP_ID``. Any other recipient is refused."""
    if who != "founder":
        print(f"refused: --dm takes only 'founder', not {who!r}. Nothing was sent.",
              file=sys.stderr)
        return 2
    raw = os.environ.get("FOUNDER_ZULIP_ID") or _dotenv(DOTENV).get("FOUNDER_ZULIP_ID", "")
    if not raw.isdigit():
        print("refused: FOUNDER_ZULIP_ID is not set to a number (environment or bot/.env). "
              "Nothing was sent.", file=sys.stderr)
        return 2
    sent = client.send_message([int(raw)], text, type_="private")
    msg_id = sent.get("id")
    if not msg_id:
        print(f"refused: Zulip answered without a message id: {sent}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"ok": True, "id": msg_id, "dm": "founder"}, ensure_ascii=False))
    else:
        print(f"sent direct message to the founder id={msg_id}")
    return 0


def cmd_invite(room: str, who: str, as_json: bool = False) -> int:
    """``invite --room X --who ROLE`` (ops#322). Role bots only, never a
    person, and refused outright: an invite is the founder's, from the page's
    Invite button. Exit 2, nothing changed."""
    room = room.lstrip("#")
    if who.upper() not in INVITE_ROLES:
        error = (f"{who!r} is not a role bot: invite takes one of "
                 f"{', '.join(INVITE_ROLES)}, never a person.")
        code = "NOT_A_ROLE"
    else:
        error = INVITE_OFF.format(who=who.upper(), room=room)
        code = "INVITE_OFF"
    if as_json:
        print(json.dumps({"ok": False, "code": code, "room": room, "who": who,
                          "error": error}, ensure_ascii=False))
    print(f"refused: {error} Nothing was changed.", file=sys.stderr)
    return 2


def cmd_wait_room(client: ZulipClient, room: str, seconds: float | None,
                  as_json: bool = False, clock=None, sleep=None) -> int:
    """Park until a message from someone else lands in ``room`` (any topic).
    Exit 2 with it printed, 0 when ``--seconds`` runs out. The queue is
    this call's own and is deleted on the way out. It polls once a second
    without blocking, so ``--seconds`` holds whatever the server's long-poll
    timeout is."""
    import time
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    room = room.lstrip("#")
    if room not in _subscribed(client):
        print(f"refused: {client.email} is not in the room #{room}.", file=sys.stderr)
        return 2
    reg = client.register(event_types=["message"], narrow=[["stream", room]])
    qid, last = reg["queue_id"], reg["last_event_id"]
    deadline = None if seconds is None else clock() + seconds
    try:
        while deadline is None or clock() < deadline:
            for e in client.get_events(qid, last, dont_block=True):
                last = max(last, int(e.get("id", last)))
                m = e.get("message") if e.get("type") == "message" else None
                if m and m.get("sender_email", "").lower() != client.email.lower():
                    print(_json_line(m) if as_json else _line(m))
                    return 2
            sleep(1.0)
    finally:
        try:
            client._request("DELETE", "events", {"queue_id": qid})
        except (ZulipError, OSError, ValueError):
            pass
    return 0


def cmd_react(client: ZulipClient, message_id: int, emoji: str, remove: bool,
             as_json: bool = False) -> int:
    """POST (add) or DELETE (remove) a reaction (ops#249 batch 1) — the
    agent path for the page's one-tap emoji reaction. A ZulipError (no such
    reaction to remove, a message this bot cannot see) propagates to
    main()'s own handler, same as archive/unarchive."""
    client._request("DELETE" if remove else "POST", f"messages/{message_id}/reactions",
                    {"emoji_name": emoji})
    if as_json:
        print(json.dumps({"ok": True, "message_id": message_id, "emoji": emoji,
                          "removed": remove}, ensure_ascii=False))
    else:
        print(f"{'removed' if remove else 'added'} {emoji} on message {message_id}")
    return 0


def cmd_typing(client: ZulipClient, stream: str, topic: str, stop: bool, status: str,
              as_json: bool = False) -> int:
    """POST /typing (ops#249 batch 1) — the agent path for "typing / working
    on it". A ``start`` clears itself out server-side after a short timeout,
    so an agent need not remember to send ``--stop`` when it is done; it is
    still the faster way to clear the indicator right away."""
    stream = stream.lstrip("#")
    client._request("POST", "typing", {"type": "stream", "to": stream, "topic": topic,
                                       "op": "stop" if stop else "start", "status": status})
    if as_json:
        print(json.dumps({"ok": True, "stream": stream, "topic": topic,
                          "op": "stop" if stop else "start", "status": status},
                         ensure_ascii=False))
    else:
        print(f"{'stopped' if stop else 'started'} {status} in #{stream} › {topic}")
    return 0


def _fetch(client: ZulipClient, narrow: list[dict[str, str]],
           since: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"narrow": narrow, "apply_markdown": "false"}
    if since is None:
        params.update(anchor="newest", num_before=READ_LIMIT, num_after=0)
    else:
        # Strictly after `since`: the caller passes back the last id it saw.
        params.update(anchor=since, include_anchor="false",
                      num_before=0, num_after=1000)
    messages = client._request("GET", "messages", params).get("messages", [])
    if since is not None:
        messages = [m for m in messages if m["id"] > since]
    return sorted(messages, key=lambda m: m["id"])


def _line(msg: dict[str, Any]) -> str:
    when = datetime.fromtimestamp(msg.get("timestamp", 0), tz=timezone.utc)
    where = " (direct message)"
    if msg.get("type") == "stream":
        where = f" #{msg.get('display_recipient')} › {msg.get('subject', '')}"
    return (f"[{msg['id']}] {when:%Y-%m-%d %H:%M} UTC{where} "
            f"{msg.get('sender_full_name')} <{msg.get('sender_email')}>:\n"
            f"{msg.get('content', '')}")


def _json_line(msg: dict[str, Any]) -> str:
    """One message as one JSON object on one line (issue #80): what a daemon
    parses, where ``_line`` is what a session reads. The same fields."""
    stream = msg.get("type") == "stream"
    return json.dumps({
        "id": msg["id"], "timestamp": msg.get("timestamp", 0),
        "sender_full_name": msg.get("sender_full_name"),
        "sender_email": msg.get("sender_email"),
        "stream": msg.get("display_recipient") if stream else None,
        "topic": msg.get("subject", "") if stream else None,
        "content": msg.get("content", "")}, ensure_ascii=False)


def cmd_read(client: ZulipClient, stream: str | None, topic: str | None,
             mentions: bool, since: int | None, as_json: bool = False) -> int:
    if mentions:
        narrow = [{"operator": "is", "operand": "mentioned"}]
    else:
        stream = (stream or "").lstrip("#")
        if stream not in _subscribed(client):
            print(f"refused: {client.email} is not subscribed to #{stream}.",
                  file=sys.stderr)
            return 2
        narrow = [{"operator": "channel", "operand": stream}]
        if topic:
            narrow.append({"operator": "topic", "operand": topic})
    messages = _fetch(client, narrow, since)
    for msg in messages:
        if as_json:
            print(_json_line(msg))
            continue
        print(_line(msg))
        print()
    if not messages:
        print("(no messages)", file=sys.stderr)
    return 0


def cmd_archive(client: ZulipClient, stream: str, topic: str | None, archive: bool,
                as_json: bool = False) -> int:
    """Hide a stream or one topic from the lists, or bring it back. Messages
    are never deleted. Side effect: changes what every member's list shows."""
    stream = stream.lstrip("#")
    sid = client._request("GET", "get_stream_id", {"stream": stream})["stream_id"]
    if topic:
        client._request("POST" if archive else "DELETE",
                        f"streams/{sid}/archived_topics", {"topic": topic})
    elif archive:
        client._request("DELETE", f"streams/{sid}")
    else:
        client._request("PATCH", f"streams/{sid}", {"is_archived": "false"})
    action = "archived" if archive else "restored"
    if as_json:
        print(json.dumps({"ok": True, "action": "archive" if archive else "unarchive",
                          "stream": stream, "stream_id": sid, "topic": topic},
                         ensure_ascii=False))
    else:
        print(f"{action} #{stream}" + (f" › {topic}" if topic else ""))
    return 0


def cmd_unread(client: ZulipClient, as_json: bool = False) -> int:
    """Unread counts per stream and topic (issue #168), from GET /unread."""
    data = client._request("GET", "unread")
    streams = data.get("streams", [])
    if as_json:
        print(json.dumps({"streams": streams}, ensure_ascii=False))
        return 0
    if not streams:
        print("(no unread messages)", file=sys.stderr)
        return 0
    for s in streams:
        print(f"#{s['name']}: {s['unread']} unread")
        for t in s["topics"]:
            print(f"  › {t['name']}: {t['unread']}")
    return 0


def cmd_mark_read(client: ZulipClient, stream: str, topic: str | None,
                  as_json: bool = False) -> int:
    """Mark read: one topic (the way opening it in the page does, issue #168),
    or the whole stream without ``--topic`` (the sidebar's "mark all as
    read") — both only messages not sent by ``client`` itself."""
    stream = stream.lstrip("#")
    sid = client._request("GET", "get_stream_id", {"stream": stream})["stream_id"]
    if topic:
        client._request("POST", "mark_topic_as_read", {"stream_id": sid, "topic_name": topic})
    else:
        client._request("POST", "mark_stream_as_read", {"stream_id": sid})
    if as_json:
        print(json.dumps({"ok": True, "stream": stream, "topic": topic}, ensure_ascii=False))
    else:
        print(f"marked read: #{stream}" + (f" › {topic}" if topic else " (all topics)"))
    return 0


def _hook():
    """hooks/agora_hook.py, loaded by path: ``wait`` runs the hook's own code
    (issue #47), never a copy of it."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "hooks", "agora_hook.py")
    spec = importlib.util.spec_from_file_location("agora_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_wait(client: ZulipClient, as_name: str | None, session: str | None,
             seconds: float | None) -> int:
    """Exit 2 with the message on a mention or DM, 0 when --seconds runs out
    or another wait already serves this session."""
    name = as_name or client.email.split("@")[0]
    creds = (client.site, client.email, client.api_key)
    return _hook().wait_for(creds, name, session or f"cli-{name}", seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zulip.py", description=__doc__.split("\n")[0])
    parser.add_argument("--as", dest="as_name",
                        help="bot to act as, e.g. COO -> ZULIP_COO_EMAIL / _API_KEY")
    sub = parser.add_subparsers(dest="cmd", required=True)
    send = sub.add_parser("send", help="post to a room (or a stream and topic); --dm founder sends one private message to the founder")
    to = send.add_mutually_exclusive_group(required=True)
    to.add_argument("--dm", metavar="WHO",
                    help="'founder' only: sends one private message to the founder")
    to.add_argument("--room", help="the room; the message goes to its one topic")
    to.add_argument("--stream")
    send.add_argument("--topic", help="with --stream only")
    send.add_argument("--text", required=True)
    send.add_argument("--json", action="store_true", help="with --dm: one JSON object on stdout")
    send.add_argument("--reply-to", type=int, metavar="ID",
                      help="quote this message above the text (ops#249), same as the "
                           "page's own quote button")
    read = sub.add_parser("read", help="print messages, oldest first")
    where = read.add_mutually_exclusive_group(required=True)
    where.add_argument("--room", help="the whole room: every message, old topics too")
    where.add_argument("--stream")
    where.add_argument("--mentions", action="store_true",
                       help="everything that mentioned this bot, across streams")
    read.add_argument("--topic")
    read.add_argument("--since", type=int, help="only messages after this id")
    read.add_argument("--json", action="store_true",
                      help="one JSON object per message per line (JSON Lines)")
    wait = sub.add_parser("wait", help="park until this bot is mentioned or DMed, or with "
                                       "--room until anyone else writes in that room")
    wait.add_argument("--room", help="wait for the next message in this room")
    wait.add_argument("--session", help="lock and queue key (default: cli-<bot>)")
    wait.add_argument("--seconds", type=float, help="give up after this long")
    wait.add_argument("--json", action="store_true", help="the message as one JSON line")
    invite = sub.add_parser("invite", help="invite a role bot into a room (refused: the "
                                           "founder invites from the page's Invite button)")
    invite.add_argument("--room", required=True)
    invite.add_argument("--who", required=True, metavar="ROLE",
                        help="one of " + ", ".join(INVITE_ROLES))
    invite.add_argument("--json", action="store_true", help="one JSON object on stdout")
    for name, what in (("archive", "hide a stream, or one topic with --topic, from the "
                                   "lists (messages are kept)"),
                       ("unarchive", "bring an archived stream or topic back")):
        arc = sub.add_parser(name, help=what)
        arc.add_argument("--stream", required=True)
        arc.add_argument("--topic", help="only this topic, not the whole stream")
        arc.add_argument("--json", action="store_true", help="one JSON object on stdout")
    unread = sub.add_parser("unread", help="print unread counts per stream and topic")
    unread.add_argument("--json", action="store_true", help="one JSON object on stdout")
    for name, remove in (("react", False), ("unreact", True)):
        rc = sub.add_parser(name, help=("remove" if remove else "add") + " an emoji reaction (ops#249)")
        rc.add_argument("--message", type=int, required=True, metavar="ID")
        rc.add_argument("--emoji", required=True, metavar="NAME",
                        help="Zulip's emoji_name, e.g. thumbs_up")
        rc.add_argument("--json", action="store_true", help="one JSON object on stdout")
    typing_p = sub.add_parser("typing", help="set the typing/working indicator (ops#249)")
    typing_to = typing_p.add_mutually_exclusive_group(required=True)
    typing_to.add_argument("--room")
    typing_to.add_argument("--stream")
    typing_p.add_argument("--topic", help="with --stream only")
    typing_p.add_argument("--status", choices=("typing", "working"), default="working",
                          help="shown to readers as '<name> is typing/working on it' "
                               "(default: working, the agent case)")
    typing_p.add_argument("--stop", action="store_true",
                          help="clear it now, rather than waiting for the server's timeout")
    typing_p.add_argument("--json", action="store_true", help="one JSON object on stdout")
    mark = sub.add_parser("mark-read", help="mark a topic, or a whole stream, read")
    mark.add_argument("--stream", required=True)
    mark.add_argument("--topic", help="only this topic; every topic in the stream without it")
    mark.add_argument("--json", action="store_true", help="one JSON object on stdout")
    args = parser.parse_args(argv)
    if args.cmd == "invite":  # refused before any credential or network is needed
        return cmd_invite(args.room, args.who, args.json)
    if getattr(args, "room", None):
        args.stream, args.topic = args.room, (ROOM_TOPIC if args.cmd in ("send", "typing")
                                              else None)
    elif args.cmd in ("send", "typing") and not args.topic and not getattr(args, "dm", None):
        parser.error(f"{args.cmd} --stream needs --topic (or use --room)")

    client = load_client(args.as_name)
    try:
        if args.cmd in ("archive", "unarchive"):
            try:
                return cmd_archive(client, args.stream, args.topic,
                                   args.cmd == "archive", args.json)
            except ZulipError as exc:
                if args.json:
                    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
                raise
        if args.cmd == "send" and args.dm:
            return cmd_send_dm(client, args.dm, args.text, args.json)
        if args.cmd == "send":
            return cmd_send(client, args.stream, args.topic, args.text, args.reply_to)
        if args.cmd == "wait":
            if args.room:
                return cmd_wait_room(client, args.room, args.seconds, args.json)
            return cmd_wait(client, args.as_name, args.session, args.seconds)
        if args.cmd == "unread":
            return cmd_unread(client, args.json)
        if args.cmd == "mark-read":
            return cmd_mark_read(client, args.stream, args.topic, args.json)
        if args.cmd in ("react", "unreact"):
            try:
                return cmd_react(client, args.message, args.emoji, args.cmd == "unreact", args.json)
            except ZulipError as exc:
                if args.json:
                    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
                raise
        if args.cmd == "typing":
            return cmd_typing(client, args.stream, args.topic, args.stop, args.status, args.json)
        return cmd_read(client, args.stream, args.topic, args.mentions, args.since,
                        args.json)
    except ZulipError as exc:
        print(f"refused by Zulip: {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"error: cannot reach Zulip at {client.site}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
