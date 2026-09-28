"""chat/server.py's setup half: the endpoints bot/setup_streams.py calls, the
admin-only rule, and the CLI that makes accounts (issue #61 part 3).

setup_streams.py runs unchanged, as a process, as a test human made by
``add-human`` — in Zulip the admin *is* the founder, and ``--check`` counts
the caller as the founder in #feature. Never the founder's real account.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from test_chat_events import Running
from test_chat_messages import ROOT, SERVER, Api, chat

SETUP = ROOT / "bot" / "setup_streams.py"
HUMAN = "test-human@example.invalid"


def cli_env(base: str, **extra) -> dict:
    """Only the test server and temp credentials: no inherited ZULIP_*, so
    nothing reaches a real Zulip and bot/.env (if any) fills nothing."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ZULIP_")}
    env.update(ZULIP_SITE=base, NO_PROXY="127.0.0.1,localhost", PYTHONIOENCODING="utf-8",
               **extra)
    return env


def add_human(db: str, email: str = HUMAN):
    return subprocess.run([sys.executable, str(SERVER), "add-human", "--email", email,
                           "--name", "Test Human", "--json", "--db", db],
                          capture_output=True, text=True, encoding="utf-8", timeout=30)


@pytest.fixture
def run(tmp_path):
    r = Running(str(tmp_path / "chat.sqlite3"))
    yield r
    r.stop()


def _setup(env, *args):
    return subprocess.run([sys.executable, str(SETUP), *args], env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)


def test_setup_streams_builds_then_is_a_no_op_and_check_passes_then_catches_a_stray(run):
    made = add_human(run.db)
    assert made.returncode == 0, made.stderr
    human = json.loads(made.stdout)
    assert human["email"] == HUMAN and human["api_key"] and human["site"].startswith(
        "http://127.0.0.1:")
    env = cli_env(run.base, ZULIP_ADMIN_EMAIL=human["email"],
                  ZULIP_ADMIN_API_KEY=human["api_key"])

    first = _setup(env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "stream #feature: created" in first.stdout and "bot Watchdog: created" in first.stdout
    second = _setup(env)
    assert second.returncode == 0, second.stderr
    assert "created" not in second.stdout and "done now" not in second.stdout, second.stdout
    check = _setup(env, "--check")
    assert check.returncode == 0, check.stdout
    assert "FAIL" not in check.stdout and "WRONG" not in check.stdout

    # Created the Zulip way: <short_name>-bot@<realm>, which _wakes() relies on.
    emails = {r["full_name"]: r["email"] for r in run.store.q("SELECT * FROM users")}
    assert emails["CTO"] == "cto-bot@chat.localhost"
    assert emails["Watchdog"] == "watchdog-bot@chat.localhost"

    # The bootstrap admin bot never belongs in #feature: --check says so.
    admin_bot = chat.bootstrap(run.store)
    api = Api(run.base, human["email"], human["api_key"])
    status, body = api("POST", "users/me/subscriptions", subscriptions=[{"name": "feature"}],
                       principals=[admin_bot["email"]])
    assert status == 200, body
    stray = _setup(env, "--check")
    assert stray.returncode != 0
    assert "#feature: FAIL" in stray.stdout and "Admin" in stray.stdout


def test_add_human_twice_with_one_email_exits_non_zero_with_a_message(tmp_path):
    db = str(tmp_path / "chat.sqlite3")
    assert add_human(db).returncode == 0
    again = add_human(db)
    assert again.returncode != 0
    assert HUMAN in again.stderr and "already" in again.stderr
    assert again.stdout == ""
    store = chat.Store(db)
    [row] = store.q("SELECT * FROM users")
    assert (row["is_bot"], row["role"]) == (0, chat.ROLE_ADMIN)
    store.db.close()


def test_only_an_admin_creates_streams_and_bots(run):
    s = run.store
    member = s.create_user("cmo-bot@chat.localhost", "CMO", is_bot=True)
    admin = chat.add_human(s, HUMAN, "Test Human")
    bot, boss = run.api(member), run.api(admin)

    status, body = bot("POST", "users/me/subscriptions", subscriptions=[{"name": "new"}])
    assert status == 400 and "administrator" in body["msg"]
    assert bot("POST", "bots", full_name="X", short_name="x")[1]["result"] == "error"
    assert bot("GET", "streams", include_all_active="true")[1]["result"] == "error"
    assert s.one("SELECT count(*) FROM streams")[0] == 0

    status, made = boss("POST", "bots", full_name="Scout", short_name="scout", bot_type=1)
    assert status == 200 and made["api_key"], made
    scout = Api(run.base, "scout-bot@chat.localhost", made["api_key"])
    assert scout("GET", "users/me")[1]["user_id"] == made["user_id"]
    assert boss("POST", "bots", full_name="Scout", short_name="scout")[0] == 400


def test_subscriptions_streams_and_members(run):
    s = run.store
    admin = chat.add_human(s, HUMAN, "Test Human")
    member = s.create_user("cto-bot@chat.localhost", "CTO", is_bot=True)
    boss, bot = run.api(admin), run.api(member)

    status, body = boss("POST", "users/me/subscriptions", subscriptions=[{"name": "cto"}],
                        invite_only="true")
    assert status == 200 and body["subscribed"] == {HUMAN: ["cto"]}
    [cto] = boss("GET", "streams", include_all_active="true")[1]["streams"]
    assert cto["invite_only"] is True
    # A private stream is invisible to a non-member, even by id.
    assert bot("GET", "streams")[1]["streams"] == []
    assert bot("GET", f"streams/{cto['stream_id']}/members")[0] == 400
    assert bot("POST", "users/me/subscriptions",
               subscriptions=[{"name": "cto"}])[1]["code"] == "STREAM_DOES_NOT_EXIST"

    boss("POST", "users/me/subscriptions", subscriptions=[{"name": "cto"}],
         principals=[member["email"]])
    assert boss("GET", f"streams/{cto['stream_id']}/members")[1]["subscribers"] == \
        [admin["id"], member["id"]]
    again = boss("POST", "users/me/subscriptions", subscriptions=[{"name": "cto"}],
                 principals=[member["email"]])[1]
    assert again["already_subscribed"] == {member["email"]: ["cto"]}
    assert [x["name"] for x in bot("GET", "users/me/subscriptions")[1]["subscriptions"]] == \
        ["cto"]

    assert boss("PATCH", f"streams/{cto['stream_id']}", is_private="false")[0] == 200
    assert bot("PATCH", f"streams/{cto['stream_id']}", is_private="true")[0] == 400
    assert bot("GET", "streams")[1]["streams"][0]["invite_only"] is False

    status, body = boss("DELETE", "users/me/subscriptions", subscriptions=["cto"],
                        principals=[member["email"]])
    assert status == 200 and body["removed"] == ["cto"]
    assert bot("GET", "users/me/subscriptions")[1]["subscriptions"] == []


def test_bootstrap_from_env_keeps_todays_emails_and_keys(tmp_path, capsys):
    db, env_file = str(tmp_path / "chat.sqlite3"), tmp_path / "bot.env"
    env_file.write_text("# like bot/.env\n"
                        "ZULIP_SITE=http://zulip.localhost:8090\n"
                        "ZULIP_ADMIN_EMAIL=someone@example.invalid\n"
                        "ZULIP_ADMIN_API_KEY=admin-key\n"
                        "ZULIP_COO_EMAIL=coo-bot@zulip.localhost\n"
                        "ZULIP_COO_API_KEY=coo-key-from-zulip\n"
                        "ZULIP_WATCHDOG_EMAIL=watchdog-bot@zulip.localhost\n"
                        "ZULIP_WATCHDOG_API_KEY=wd-key\n", encoding="utf-8")
    assert chat.main(["bootstrap", "--json", "--from-env", str(env_file), "--db", db]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {b["email"]: b["action"] for b in out["bots"]} == {
        "coo-bot@zulip.localhost": "created", "watchdog-bot@zulip.localhost": "created"}
    store = chat.Store(db)
    users = {r["email"]: dict(r) for r in store.q("SELECT * FROM users")}
    assert users["coo-bot@zulip.localhost"]["api_key"] == "coo-key-from-zulip"
    assert users["coo-bot@zulip.localhost"]["full_name"] == "COO"
    assert users["watchdog-bot@zulip.localhost"]["full_name"] == "Watchdog"
    assert "someone@example.invalid" not in users  # the founder's account: never
    store.db.close()
    # Run twice: nothing new.
    assert chat.main(["bootstrap", "--json", "--from-env", str(env_file), "--db", db]) == 0
    assert {b["action"] for b in json.loads(capsys.readouterr().out)["bots"]} == \
        {"already exists"}


def _pool_members(db: str) -> dict:
    store = chat.Store(db)
    try:
        [stream] = store.q("SELECT * FROM streams WHERE name='pool'")
        assert stream["invite_only"] == 1
        return {r["email"]: r["full_name"] for r in store.q(
            "SELECT u.* FROM users u JOIN subscriptions x ON x.user_id=u.id "
            "WHERE x.stream_id=?", (stream["stream_id"],))}
    finally:
        store.db.close()


def test_bootstrap_creates_the_pool_bot_and_private_pool_with_founder_and_coo(
        tmp_path, capsys):
    """Issue #80: the pool daemon's bot and stream, for ops#85's pool control."""
    db, env_file = str(tmp_path / "chat.sqlite3"), tmp_path / "bot.env"
    env_file.write_text("ZULIP_COO_EMAIL=coo-bot@zulip.localhost\n"
                        "ZULIP_COO_API_KEY=coo-key\n", encoding="utf-8")
    assert add_human(db).returncode == 0  # the founder, before bootstrap
    assert chat.main(["bootstrap", "--json", "--from-env", str(env_file), "--db", db]) == 0
    pool = json.loads(capsys.readouterr().out)["pool"]
    assert pool["email"] == "pool-bot@chat.localhost" and pool["api_key"]
    assert pool["stream"] == "pool" and pool["missing"] == []
    members = _pool_members(db)
    assert members == {"pool-bot@chat.localhost": "Pool", "coo-bot@zulip.localhost": "COO",
                       HUMAN: "Test Human"}
    assert sorted(pool["members"]) == sorted(members)
    store = chat.Store(db)
    try:
        # The key printed is the key that works: `--as POOL` uses exactly it.
        row = store.one("SELECT * FROM users WHERE email=?", (pool["email"],))
        assert row["api_key"] == pool["api_key"] and row["is_bot"] == 1
    finally:
        store.db.close()

    # Run twice: the same bot and key, nothing new.
    assert chat.main(["bootstrap", "--json", "--from-env", str(env_file), "--db", db]) == 0
    again = json.loads(capsys.readouterr().out)["pool"]
    assert (again["email"], again["api_key"]) == (pool["email"], pool["api_key"])
    assert _pool_members(db) == members


def test_bootstrap_uses_the_env_pool_pair_and_names_who_is_not_in_pool_yet(
        tmp_path, capsys):
    """No founder and no COO yet: #pool says so rather than listing them; the
    founder's add-human later joins him, and ZULIP_POOL_* is the Pool bot."""
    db, env_file = str(tmp_path / "chat.sqlite3"), tmp_path / "bot.env"
    env_file.write_text("ZULIP_POOL_EMAIL=pool-bot@zulip.localhost\n"
                        "ZULIP_POOL_API_KEY=pool-key\n", encoding="utf-8")
    assert chat.main(["bootstrap", "--from-env", str(env_file), "--db", db]) == 0
    out = capsys.readouterr().out
    assert "ZULIP_POOL_EMAIL=pool-bot@zulip.localhost\nZULIP_POOL_API_KEY=pool-key" in out
    assert "# #pool: no COO yet" in out and "# #pool: no founder yet" in out
    assert _pool_members(db) == {"pool-bot@zulip.localhost": "Pool"}
    assert add_human(db).returncode == 0
    assert _pool_members(db)[HUMAN] == "Test Human"
