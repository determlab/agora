# chat/ — our own chat server (issue #61, M1)

One stdlib Python file, `server.py`, that speaks the part of Zulip's REST API
our tools use (`/api/v1/`, same field names, same error shape), so
`bot/zulip.py`, `bot/setup_streams.py`, `bot/durability_test.py` and the hook
work against it with only `ZULIP_SITE` changed. One SQLite file
(`chat/data/chat.sqlite3`, gitignored), bound to 127.0.0.1 only. Event queues
live in the database: they survive a restart and expire after 7 days unpolled.

## Start

    chat\run.cmd                                   # 127.0.0.1:8095
    python chat/server.py serve --port 8095 --seed-ids 600

`--seed-ids N` makes message and event ids continue above N — use Zulip's last
message id, so the hook's saved floor and `zulip.py read --since` still see new
messages. `--db PATH` picks another database.

## Accounts

    python chat/server.py bootstrap --json                      # an admin bot
    python chat/server.py bootstrap --json --from-env bot/.env  # + every ZULIP_<ROLE>_EMAIL/_API_KEY pair

`--from-env` creates each role's bot with **today's email and key**, so the
switch changes nothing but `ZULIP_SITE`. `ZULIP_ADMIN_*` is skipped: that is
the founder's own account, and only he makes it:

    python chat/server.py add-human --email <you> --name "<your name>" --json

It prints an admin `api_key` (no password). The founder pastes it into
`bot/.env` as `ZULIP_ADMIN_EMAIL` / `ZULIP_ADMIN_API_KEY`; then
`python bot/setup_streams.py` builds the streams and `--check` verifies them.

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
