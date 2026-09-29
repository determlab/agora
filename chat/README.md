# chat/ — our own chat server (issue #61, M1)

One stdlib Python file, `server.py`, that speaks the part of Zulip's REST API
our tools use (`/api/v1/`, same field names, same error shape), so
`bot/zulip.py`, `bot/setup_streams.py`, `bot/durability_test.py` and the hook
work against it with only `ZULIP_SITE` changed. One SQLite file
(`chat/data/chat.sqlite3`, gitignored), bound to 127.0.0.1 only. Event queues
live in the database: they survive a restart and expire after 7 days unpolled.

## Quickstart (issue #72)

    python chat/server.py up

That is the whole setup, and running it again changes nothing. It continues
the message ids above Zulip's newest (asking Zulip once, if it still answers;
the stored floor otherwise, never lower), creates each bot in `bot/.env` with
its existing email and key, creates the streams `bot/setup_streams.py` creates
and puts each bot in its own, puts every human in every stream, and serves on
http://127.0.0.1:8095. It never creates a human: the first time, it prints the
one command the founder runs, once:

    python chat/server.py add-human --email <you> --name "<your name>" --write-env

That makes his account, puts it in every stream and appends
`ZULIP_FOUNDER_EMAIL` / `ZULIP_FOUNDER_API_KEY` to `bot/.env` (never over an
existing pair: then it exits non-zero and changes nothing). Open
http://127.0.0.1:8095/ and log in with those two values.

Optional, start it at every Windows login (one `.cmd` in your Startup folder,
running `up` minimised and logging to `chat/data/server.log`; no admin, no
scheduled task):

    python chat/server.py autostart install
    python chat/server.py autostart status --json   # installed? answering?
    python chat/server.py autostart remove

`chat\run.cmd` is `up` with its flags passed through. While it serves, `up`
runs `git pull --ff-only` every 10 minutes when the clone is on `main` (a
failed pull is logged, never forced) and restarts itself when
`chat/server.py` changed; the page needs no restart. `--no-update` turns that
off. For agents: `up --no-serve --json` (set up, one JSON line, exit 0) and
`up --json` (the same line, then serve). `--port N`, `--db PATH` and
`--env FILE` pick another port, database and env file.

`serve --port 8095 --seed-ids 600` still serves without any setup.

## The dashboard

Start `up` (or `serve`) with `--dashboard-cmd "<command>"` and the server runs
that command every `--dashboard-every` seconds (default 300); `POST
/api/v1/dashboard/sync` runs it now and answers `{"result":"success",
"last_sync":…}`, or an error naming the command's last stderr line. `GET
/api/v1/dashboard`, with the same auth as every endpoint, returns the newest
document (`doc`) with `last_sync` (when it was stored), `stale` (true past two
hours) and `last_error`; read `last_sync`, not the document's own date, to know
how old it is. A failed run stores nothing: the previous document stays and
`last_error` says why. `chat\run.cmd` passes this PC's `ops/tools/dashboard.py
--json`. A queue registered with `dashboard` in its `event_types` gets
`{"type":"dashboard","last_sync":…}` after each good sync.

## Archive a stream or a topic

Archive hides a room from the lists; no message is deleted. A stream: `DELETE
/api/v1/streams/{id}` (Zulip's "archive a channel"; admins only), and `PATCH
/api/v1/streams/{id}` with `is_archived=false` brings it back. Posting to an
archived stream is refused with `STREAM_ARCHIVED`. A topic: `POST` / `DELETE
/api/v1/streams/{id}/archived_topics` with `topic=…` (any member of the
stream); `GET` there lists the archived ones. `GET
/api/v1/users/me/subscriptions`, `GET /api/v1/streams` and `GET
/api/v1/users/me/{id}/topics` leave archived rooms out, unless asked with
`include_archived=true` (`exclude_archived=false` for `/streams`); each row
carries `is_archived`. A new message to an archived topic un-archives it. From
a shell: `python bot/zulip.py archive|unarchive --stream S [--topic T] --json`.

## Unread messages (issue #168)

Read state is per user, stored in the `read_messages` table (never touched by
sending: a sender's own messages are simply excluded from their own unread
count, not written as read). A message at or below a user's `read_floor` for
that stream (its own table, one row per user *and* stream) is read
regardless of `read_messages`. The floor covers two kinds of "before I could
read this": a database that already had users before `read_floor` existed
gets one migrated in for every stream a user was already subscribed to, at
that moment's newest message id, so every message from before per-user read
state existed is read for everyone rather than an unread pile of old
history; and joining a stream (`POST /api/v1/users/me/subscriptions`, or a
new user's first subscription) sets that stream's floor to its newest
message id right away, so the stream's history from before the join is read,
not an unread pile, and only a message sent after the join counts. `GET
/api/v1/unread` returns unread counts per stream and per topic for the
caller, never counting an archived stream (`is_archived`) or an archived
topic — both are hidden from the lists, so a bubble for one could never
clear:
```json
{"streams": [{"stream_id": 3, "name": "feature", "unread": 4,
              "topics": [{"name": "ops#44 routes", "unread": 4}]}]}
```
`POST /api/v1/mark_topic_as_read` with `stream_id` and `topic_name` (Zulip's
own endpoint and argument names) marks every message in that topic, not sent
by the caller, read; `POST /api/v1/mark_stream_as_read` with `stream_id` does
the same for every topic in the stream — the sidebar's "mark all as read".
The page shows a small round bubble next to each stream and topic with
unread messages (hidden at 0), starts every stream's topic list collapsed on
load, marks a topic read when it is opened (and when a message arrives while
it is already open), loads every count from the server on page load so a
reload never disagrees with what was on screen, and shows the total in the
tab title as `(N) Agora`. From a shell: `python bot/zulip.py unread --json`
and `python bot/zulip.py mark-read --stream S [--topic T]`.

## Accounts, by hand

    python chat/server.py bootstrap --json                      # an admin bot
    python chat/server.py bootstrap --json --from-env bot/.env  # + every ZULIP_<ROLE>_EMAIL/_API_KEY pair

`--from-env` creates each role's bot with **today's email and key**, so the
switch changes nothing but `ZULIP_SITE`. `ZULIP_ADMIN_*` is skipped: that is
the founder's own account, and only he makes it:

    python chat/server.py add-human --email <you> --name "<your name>" --json

It prints an admin `api_key` (no password). For `bot/setup_streams.py` the
founder pastes it into `bot/.env` as `ZULIP_ADMIN_EMAIL` /
`ZULIP_ADMIN_API_KEY`; after `up`, `python bot/setup_streams.py --check`
passes.

## Point a tool at it

    set ZULIP_SITE=http://127.0.0.1:8095
    python bot/zulip.py --as COO send --stream coo --topic "#61" --text "hello"

`python -m pytest -q tests/test_chat_compat.py` runs every tool against a
fresh server and names the command that fails.

## D12 by hand

With the server running and a bot subscribed to a stream (bootstrap, then
`setup_streams.py`, or the bot's own stream), run the unchanged
`bot/durability_test.py` with `ZULIP_SITE=http://127.0.0.1:8095`,
`ZULIP_BOT_EMAIL`, `ZULIP_BOT_API_KEY` and `ZULIP_CHANNEL=<its stream>`. It
waits a real 3 minutes; it must print `PASS — all 6 messages received, in order`.
