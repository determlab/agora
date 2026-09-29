"""A role session's voice in Zulip: ``send`` and ``read``, called through Bash.

A small CLI rather than an MCP server (CTO ruling, 2026-09-10, issue #42): a
tool added to a running MCP server is invisible to every session already
connected (D1). A script is read fresh on every call.

Usage:
  python bot/zulip.py send --stream coo --topic "#141 record shape" --text "..."
  python bot/zulip.py read --stream coo --topic "#141 record shape" [--since ID]
  python bot/zulip.py read --mentions [--since ID]
  python bot/zulip.py --as POOL read --stream pool --since ID --json
  python bot/zulip.py wait [--session KEY] [--seconds N]
  python bot/zulip.py archive|unarchive --stream coo [--topic "old"] [--json]
  python bot/zulip.py unread [--json]
  python bot/zulip.py mark-read --stream coo --topic "#141 record shape" [--json]
  python bot/zulip.py mark-read --stream coo [--json]   # every topic in the stream

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
        sys.exit(f"error: {prefix}_EMAIL and {prefix}_API_KEY are not set "
                 f"(environment or bot/.env).")
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


def cmd_send(client: ZulipClient, stream: str, topic: str, text: str) -> int:
    stream = stream.lstrip("#")
    if stream not in _subscribed(client):
        if _stream_exists(client, stream):
            print(f"refused: {client.email} is not subscribed to #{stream}. "
                  f"Nothing was sent.", file=sys.stderr)
        else:
            print(f"refused: #{stream} does not exist, or {client.email} "
                  f"cannot see it. Nothing was sent.", file=sys.stderr)
        return 2
    sent = client.send_message(stream, text, topic=topic)
    msg_id = sent.get("id")
    if not msg_id:
        print(f"refused: Zulip answered without a message id: {sent}",
              file=sys.stderr)
        return 1
    print(f"sent #{stream} › {topic} id={msg_id}")
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
    send = sub.add_parser("send", help="post to a stream and topic")
    send.add_argument("--stream", required=True)
    send.add_argument("--topic", required=True)
    send.add_argument("--text", required=True)
    read = sub.add_parser("read", help="print messages, oldest first")
    where = read.add_mutually_exclusive_group(required=True)
    where.add_argument("--stream")
    where.add_argument("--mentions", action="store_true",
                       help="everything that mentioned this bot, across streams")
    read.add_argument("--topic")
    read.add_argument("--since", type=int, help="only messages after this id")
    read.add_argument("--json", action="store_true",
                      help="one JSON object per message per line (JSON Lines)")
    wait = sub.add_parser("wait", help="park until this bot is mentioned or DMed")
    wait.add_argument("--session", help="lock and queue key (default: cli-<bot>)")
    wait.add_argument("--seconds", type=float, help="give up after this long")
    for name, what in (("archive", "hide a stream, or one topic with --topic, from the "
                                   "lists (messages are kept)"),
                       ("unarchive", "bring an archived stream or topic back")):
        arc = sub.add_parser(name, help=what)
        arc.add_argument("--stream", required=True)
        arc.add_argument("--topic", help="only this topic, not the whole stream")
        arc.add_argument("--json", action="store_true", help="one JSON object on stdout")
    unread = sub.add_parser("unread", help="print unread counts per stream and topic")
    unread.add_argument("--json", action="store_true", help="one JSON object on stdout")
    mark = sub.add_parser("mark-read", help="mark a topic, or a whole stream, read")
    mark.add_argument("--stream", required=True)
    mark.add_argument("--topic", help="only this topic; every topic in the stream without it")
    mark.add_argument("--json", action="store_true", help="one JSON object on stdout")
    args = parser.parse_args(argv)

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
        if args.cmd == "send":
            return cmd_send(client, args.stream, args.topic, args.text)
        if args.cmd == "wait":
            return cmd_wait(client, args.as_name, args.session, args.seconds)
        if args.cmd == "unread":
            return cmd_unread(client, args.json)
        if args.cmd == "mark-read":
            return cmd_mark_read(client, args.stream, args.topic, args.json)
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
