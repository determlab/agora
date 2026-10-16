"""A client that hangs up before its reply (ops#324).

A phone tab that sleeps, or a long-poll that is cancelled, closes its socket
while the server is still answering. That is a normal disconnect: no
traceback on stderr and no second attempt to send a 500 on the dead socket.
Any other exception still prints and still answers 500.
"""
from __future__ import annotations

import socket
import struct
import threading

import pytest
from test_chat_allow_host import Served
from test_chat_page import _request


@pytest.fixture
def run(tmp_path):
    served = Served(str(tmp_path / "chat.sqlite3"))
    yield served
    served.stop()


def _watch_errors(run) -> threading.Event:
    """Set once the server has handled an exception that left the handler."""
    seen = threading.Event()
    handle_error = run.server.handle_error

    def wrapped(request, client_address):
        try:
            handle_error(request, client_address)
        finally:
            seen.set()
    run.server.handle_error = wrapped
    return seen


def _hang_up(sock: socket.socket) -> None:
    # SO_LINGER 0: close() sends a RST, as a vanished client does.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def test_a_client_closing_before_the_reply_is_quiet(run, monkeypatch, capsys):
    handler = run.server.RequestHandlerClass
    entered, gone, done = threading.Event(), threading.Event(), threading.Event()
    sends = []
    send = handler._send
    serve_page = handler._serve_page

    def counting_send(self, status, *a, **kw):
        sends.append(status)
        send(self, status, *a, **kw)

    def slow_page(self):
        entered.set()
        gone.wait(10)
        try:
            serve_page(self)
        finally:
            done.set()
    monkeypatch.setattr(handler, "_send", counting_send)
    monkeypatch.setattr(handler, "_serve_page", slow_page)

    port = run.server.server_address[1]
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    assert entered.wait(10)
    _hang_up(sock)
    gone.set()
    assert done.wait(10)

    assert sends == [200]  # the page only: no 500 after the disconnect
    assert "Traceback" not in capsys.readouterr().err


def test_a_reset_before_the_request_line_is_quiet(run, capsys):
    seen = _watch_errors(run)
    sock = socket.create_connection(("127.0.0.1", run.server.server_address[1]), timeout=10)
    sock.sendall(b"GET / HT")  # mid request line, then the RST
    _hang_up(sock)
    assert seen.wait(10)
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("error", [ConnectionAbortedError, ConnectionResetError,
                                   BrokenPipeError])
def test_each_disconnect_error_is_dropped_quietly(run, error, capsys):
    try:
        raise error("client went away")
    except error:
        run.server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""


def test_any_other_exception_still_prints(run, capsys):
    try:
        raise ValueError("a real bug")
    except ValueError:
        run.server.handle_error(None, ("127.0.0.1", 1))
    err = capsys.readouterr().err
    assert "Traceback" in err and "ValueError: a real bug" in err


def test_any_other_exception_still_answers_500(run, monkeypatch):
    def broken(self):
        raise RuntimeError("a real bug")
    monkeypatch.setattr(run.server.RequestHandlerClass, "_serve_page", broken)
    port = run.server.server_address[1]
    status, _, raw = _request(run, "GET", "/", Host=f"127.0.0.1:{port}")
    assert status == 500
    assert b"INTERNAL_SERVER_ERROR" in raw and b"a real bug" in raw

