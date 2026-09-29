"""``--allow-host NAME`` (ops#222): the page and API behind ``tailscale serve``.

``tailscale serve`` proxies https://NAME to 127.0.0.1:8095, so a request
arrives with Host ``NAME`` and Origin ``https://NAME``. Only a name given
with ``--allow-host`` is accepted that way; every other name is still refused
(the DNS-rebinding and foreign-origin checks), and without the flag nothing
changes.
"""
from __future__ import annotations

import json
import threading

import pytest

from test_chat_messages import ROOT, chat
from test_chat_page import _request
from test_chat_setup import HUMAN
from test_chat_up import _env_file

NAME = "mypc.tail1234.ts.net"


class Served:
    def __init__(self, db: str, allow_hosts=()):
        self.server = chat.make_server(db, port=0, poll_seconds=0.5, allow_hosts=allow_hosts)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.store = self.server.RequestHandlerClass.chat.s

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        with self.store.lock:
            self.store.db.close()


@pytest.fixture
def allowed(tmp_path):
    run = Served(str(tmp_path / "chat.sqlite3"), allow_hosts=[NAME.upper()])
    human = chat.add_human(run.store, HUMAN, "Test Human")
    yield run, human
    run.stop()


@pytest.fixture
def default(tmp_path):
    run = Served(str(tmp_path / "chat.sqlite3"))
    human = chat.add_human(run.store, HUMAN, "Test Human")
    yield run, human
    run.stop()


def test_the_allowed_name_is_served_bare_and_on_443(allowed):
    run, _ = allowed
    for host in (NAME, f"{NAME}:443", NAME.upper()):
        status, headers, raw = _request(run, "GET", "/", Host=host)
        assert status == 200, (host, raw)
        # The CSP is 'self'-relative: it holds on https://NAME as on 127.0.0.1.
        assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_the_local_names_are_still_served_with_the_flag(allowed):
    run, _ = allowed
    port = run.server.server_address[1]
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        status, _, _ = _request(run, "GET", "/", Host=host)
        assert status == 200, host


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:443", f"{NAME}:8095", f"{NAME}:80",
    f"x.{NAME}", "other.tail1234.ts.net", "tail1234.ts.net"])
def test_any_other_host_is_still_refused(allowed, host):
    run, _ = allowed
    status, _, raw = _request(run, "GET", "/", Host=host)
    assert status == 403, host
    assert json.loads(raw)["code"] == "FORBIDDEN"


def test_the_api_answers_the_https_origin_of_the_allowed_name(allowed):
    run, human = allowed
    status, _, raw = _request(run, "GET", "/api/v1/users/me", user=human,
                              Host=NAME, Origin=f"https://{NAME}")
    assert status == 200, raw
    assert json.loads(raw)["email"] == human["email"]


@pytest.mark.parametrize("origin", [
    f"http://{NAME}", f"https://{NAME}:8095", f"https://x.{NAME}",
    "https://evil.example", "https://other.tail1234.ts.net"])
def test_any_other_origin_is_refused_and_nothing_is_stored(allowed, origin):
    run, human = allowed
    stream = run.store.create_stream("feature", invite_only=True)
    run.store.subscribe(human["id"], stream)
    status, _, raw = _request(run, "POST", "/api/v1/messages",
                              {"type": "stream", "to": "feature", "topic": "t",
                               "content": "from elsewhere"}, human, Host=NAME, Origin=origin)
    assert status == 403, (origin, raw)
    assert run.store.one("SELECT 1 FROM messages WHERE content='from elsewhere'") is None


def test_without_the_flag_the_name_and_its_origin_are_refused(default):
    run, human = default
    status, _, _ = _request(run, "GET", "/", Host=NAME)
    assert status == 403
    status, _, _ = _request(run, "GET", "/api/v1/users/me", user=human,
                            Origin=f"https://{NAME}")
    assert status == 403
    port = run.server.server_address[1]
    status, _, _ = _request(run, "GET", "/api/v1/users/me", user=human,
                            Origin=f"http://127.0.0.1:{port}")
    assert status == 200


@pytest.mark.parametrize("bad", ["*.ts.net", f"https://{NAME}", f"{NAME}:443", "", "a b"])
def test_the_flag_takes_a_plain_name_only(bad, capsys):
    with pytest.raises(SystemExit) as e:
        chat.main(["serve", "--port", "0", "--allow-host", bad])
    assert e.value.code == 2
    assert "not a plain host name" in capsys.readouterr().err


def test_up_passes_every_allow_host_to_the_server(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    _env_file(env)
    seen = {}

    def fake_make_server(*a, **kw):
        seen.update(kw)
        raise OSError("stopped by the test")
    monkeypatch.setattr(chat, "make_server", fake_make_server)
    code = chat.main(["up", "--no-update", "--json", "--env", str(env),
                      "--db", str(tmp_path / "chat.sqlite3"), "--port", "0",
                      "--allow-host", NAME, "--allow-host", "Second.Example"])
    assert code != 0  # the fake refused to bind; what it was given is what counts
    assert seen["allow_hosts"] == [NAME, "second.example"]


def test_run_cmd_passes_agora_allow_host_only_when_set():
    text = (ROOT / "chat" / "run.cmd").read_text(encoding="utf-8")
    assert 'if not "%AGORA_ALLOW_HOST%"=="" set ALLOW_HOST=--allow-host "%AGORA_ALLOW_HOST%"' \
        in text
    assert "\nset ALLOW_HOST=\n" in text.replace("\r\n", "\n")
    assert "%ALLOW_HOST% %*" in text
