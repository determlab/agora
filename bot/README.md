---
type: readme
owner: repo-agent
scope: repo/agora/bot
reviewed: 2026-09-10
---

# bot/ — Zulip, and the collector-bot proof (issue #37)

Step 1 of 4 in rebuilding the chat on a real chat product (founder decision).
This is the only step that had to pass before the other three could start:
it proves that a bot which is busy for minutes — a live Claude Code session —
does not lose messages sent while it isn't polling. **No chat product can
push into a busy session; the bot has to come and collect, and Zulip's
long-poll event queue is what makes "come and collect, days later if need
be" actually hold up.** See the root `docs/DECISIONS.md`, row **D12**.

Out of scope here (later issues): migrating rooms/transcripts/other roles,
the status/roadmap tab, retiring the existing 8765 server.

## What's in this directory

| file | what |
|---|---|
| `docker-compose.yml` | the Zulip stack — its own compose *project* (`agora-zulip`), separate from the repo root's `docker-compose.yml` (the agora app itself). Based on `zulip/docker-zulip`'s own published compose (11.x branch), not hand-rolled. |
| `.env` | **not committed** (`.gitignore`d) — stack secrets + the bot's credentials. Created once during setup below. |
| `zulip_client.py` | stdlib-only REST client (`urllib` + `json`) for the three calls the bot needs: `register`, `GET /events`, `POST /messages`. No `zulip` pip package — see "Why no dependency" below. |
| `resume.py` | the resume logic, split out so it's testable without a live server. |
| `listener.py` | the actual bot: register once, long-poll `/json/events` forever, echo every non-bot message back with the event id it resumed from. |
| `durability_test.py` | the proof: a real 3-minute gap with no polling, messages sent throughout by a second thread, then resume and check nothing was lost. |
| `test_resume.py` | unit tests for `resume.py` (mocked data, no live server — `pytest bot/test_resume.py`). |
| `setup_streams.py` | idempotent: creates the four streams and three role bots below, and subscribes each bot to its own stream only. `--check` verifies without creating and exits non-zero if anything is missing or wrong. |

## Streams and topics (issue #40)

**The issue body's nine-per-repo-stream scheme is superseded.** A later
comment quotes the founder directly — "i dont need per repo i just need CMO
CTO and you" — so the actual scheme is **four streams, not nine**:

| stream | who posts | who is subscribed |
|---|---|---|
| `#cto` | the founder, the CTO bot | the CTO bot only |
| `#cmo` | the founder, the CMO bot | the CMO bot only |
| `#coo` | the founder, the COO bot | the COO bot only |
| `#status` | the ops watchdog (one pinned message it rewrites) | no bot |

The founder talks to a person, not a repository — `#shal`, `#bricks`, `#aos`,
`#agora`, `#adk-lab`, `#pytest-shal`, `#ops` and `#founder` from the original
issue text are dropped. This is the hard version of the old chair-mute: a bot
that is not subscribed to a stream cannot read or post into it, so the
founder's conversation with the CMO is not something the CTO bot can see.

Pool workers get neither a bot user nor a stream subscription — they are
processes with no session to wake (RFC-004); their state is the label on the
issue, their voice is the PR.

**topic = the issue number** inside a role stream, e.g. `#cto 141 record
shape`. A decision that isn't tied to one issue uses the ledger id instead,
e.g. `D12 event queue resume`. Unchanged from the original issue body.

Run it (idempotent — a second run creates nothing):
```
.venv/Scripts/python.exe bot/setup_streams.py            # create what's missing
.venv/Scripts/python.exe bot/setup_streams.py --check     # verify, exit 1 if incomplete
```
Needs `ZULIP_ADMIN_EMAIL` / `ZULIP_ADMIN_API_KEY` (an admin account — creating
streams and subscribing other users are realm-admin actions, a bot cannot do
either). Regenerate the admin key via `manage.py shell` the same way the bot
user below is created — never hardcode it; it belongs in `bot/.env` only.

## Why no `zulip` pip dependency

This repo has zero runtime dependencies anywhere (`docs/DECISIONS.md` D2) —
the MCP layer is hand-rolled JSON-RPC for the same reason. Zulip's REST API
is plain HTTP with HTTP Basic Auth (`bot-email:api-key`) and JSON/form
bodies, fully reachable with `urllib.request`. The one place the official
client would have bought something is the long-poll resume semantics
(`register` + `GET /events`, easy to get subtly wrong by hand) — that's
exactly what `test_resume.py` exists to pin down instead. Judged practical
to do stdlib-only; no `requirements.txt` was added.

## First-run setup (do this once)

1. **Check the port is free**, then bring the stack up:
   ```
   cd bot
   docker compose up -d
   ```
   `docker compose ps` should show five containers, `zulip` bound to
   `127.0.0.1:8090` (loopback only — see the port/bind notes in
   `docker-compose.yml`'s header). First boot runs Zulip's own database
   migrations and can take a minute or two.

   Access it at **`http://zulip.localhost:8090/`** — not `127.0.0.1`, not
   `localhost` itself. `zulip.localhost` needs no setup: `*.localhost`
   resolves to loopback natively per RFC 6761 (confirmed: `nslookup
   zulip.localhost`), Zulip's `EXTERNAL_HOST` validation requires a domain
   with a dot (a bare `localhost` is rejected), and — the one that actually
   bit — browsers only treat plain HTTP as a "trustworthy origin" (so
   Secure-flagged session/CSRF cookies still work) for `localhost`,
   `*.localhost`, and literal loopback IPs; a domain that merely *resolves*
   to `127.0.0.1` (tried first: `127.0.0.1.nip.io`) does not qualify, and
   login 403s. `docker-compose.yml`'s `EXTERNAL_HOST` comment has the full
   trail of what was tried.

2. **Create the `.env` file** (this repo does not ship one — it's
   `.gitignore`d) with the stack's internal secrets:
   ```
   ZULIP_POSTGRES_PASSWORD=<random>
   ZULIP_MEMCACHED_PASSWORD=<random>
   ZULIP_RABBITMQ_PASSWORD=<random>
   ZULIP_REDIS_PASSWORD=<random>
   ZULIP_SECRET_KEY=<random, 50+ chars>
   ZULIP_SITE=http://zulip.localhost:8090
   ZULIP_BOT_EMAIL=
   ZULIP_BOT_API_KEY=
   ZULIP_CHANNEL=general
   ```
   (`docker compose up` reads this automatically — it sits next to
   `docker-compose.yml`.) Generate the random values however you like, e.g.
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

3. **Generate a realm creation link** (self-serve `/new/` is off by
   default in this image — confirmed by trying it, "Organization creation
   link required"):
   ```
   docker compose exec -u zulip zulip \
     /home/zulip/deployments/current/manage.py generate_realm_creation_link
   ```
   **Open the printed link in a browser** (swap `https://` for `http://` —
   the link generator always prints `https://` regardless of `DISABLE_HTTPS`)
   and click through "Create a new Zulip organization": org name, type,
   your own admin email, then the emailed confirmation step (no outgoing
   mail is configured, so instead of clicking a link in an email, open the
   URL the container printed to its log the same way — or just re-run
   `generate_realm_creation_link`'s twin, `manage.py print_email_address`,
   or read `/var/log/zulip/*.log` for the confirmation link if you're not
   watching the terminal). Finish with your name and a password. This is
   the "a human can open it and log in" DoD check — do it by hand in a real
   browser, not scripted.

4. **`general`** already exists — Zulip seeds it on realm creation.

5. **Create the bot user.** Scriptable and repeatable, so this was done via
   `manage.py shell` rather than clicking through Settings -> Bots (either
   works; the UI path is three clicks if you prefer it):
   ```
   docker compose exec -u zulip zulip /home/zulip/deployments/current/manage.py shell
   ```
   ```python
   from zerver.models import Realm, UserProfile
   from zerver.actions.create_user import do_create_user
   from zerver.actions.streams import bulk_add_subscriptions
   from zerver.lib.streams import ensure_stream

   realm = Realm.objects.get(string_id='')
   admin = UserProfile.objects.filter(
       realm=realm, role__lte=UserProfile.ROLE_REALM_ADMINISTRATOR
   ).order_by('role').first()

   bot = do_create_user(
       email='coo-bot-bot@zulip.localhost', password=None, realm=realm,
       full_name='coo-bot', bot_type=UserProfile.DEFAULT_BOT,
       bot_owner=admin, acting_user=admin,
   )
   print(bot.email, bot.api_key)  # copy into .env below

   stream = ensure_stream(realm, 'general', acting_user=admin)
   bulk_add_subscriptions(realm, [stream], [bot], acting_user=admin)
   ```
   Copy the printed email and `api_key` into `.env` as `ZULIP_BOT_EMAIL` and
   `ZULIP_BOT_API_KEY`.

## Running it

```
# the bot itself — long-polls forever, echoes every message in ZULIP_CHANNEL
.venv/Scripts/python.exe bot/listener.py

# the durability proof — real 3-minute gap, see docs/DECISIONS.md D12 for the result
.venv/Scripts/python.exe bot/durability_test.py
```

Both read `ZULIP_SITE` / `ZULIP_BOT_EMAIL` / `ZULIP_BOT_API_KEY` /
`ZULIP_CHANNEL` from the environment — either export them from `bot/.env`
first, or run with `env $(cat bot/.env | grep -v '^#' | xargs)` prefixed.

## Stopping it

```
cd bot
docker compose down       # stop the stack, keep the data (volumes)
docker compose down -v    # stop and wipe everything, start over clean
```
