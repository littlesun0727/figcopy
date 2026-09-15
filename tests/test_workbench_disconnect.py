"""Exercise client disconnects during normal and error HTTP responses without socket races."""

from __future__ import annotations

import io
import logging
import re

import pytest

from collage.core.errors import CollageError
from collage.studio.workbench.server import create_workbench_server


class RequestSocket:
    """Feed a real request handler and fail a chosen header or body write."""

    def __init__(self, request, *, error=None, fail_on=1):
        self.request = request
        self.error = error
        self.fail_on = fail_on
        self.writes = 0
        self.output = bytearray()

    def makefile(self, mode, *args):
        assert mode == "rb"
        return io.BytesIO(self.request)

    def sendall(self, data):
        self.writes += 1
        if self.error is not None and self.writes >= self.fail_on:
            raise self.error
        self.output.extend(data)


@pytest.fixture
def endpoint(tmp_path):
    server = create_workbench_server(tmp_path / "data", port=0)
    try:
        page = RequestSocket(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        server.RequestHandlerClass(page, ("127.0.0.1", 12345), server)
        token = re.search(rb'name="figcopy-csrf-token" content="([^"]+)"', page.output)
        assert token is not None

        def request(method, *, error=None, fail_on=1):
            path = (
                "/api/projects/example/diagnostics"
                if method == "GET"
                else "/api/projects/example/overlays/regenerate"
            )
            raw = (
                f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
                f"X-Figcopy-Token: {token[1].decode()}\r\n"
                "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
            ).encode()
            connection = RequestSocket(raw, error=error, fail_on=fail_on)
            handler = server.RequestHandlerClass(
                connection, ("127.0.0.1", 12345), server
            )
            return handler, connection

        yield server.figcopy_application, request
    finally:
        server.server_close()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("outcome", ["ok", "business_error", "unexpected_error"])
@pytest.mark.parametrize(
    "error_type", [ConnectionAbortedError, ConnectionResetError, BrokenPipeError]
)
@pytest.mark.parametrize("fail_on", [1, 2], ids=["headers", "body"])
def test_disconnect_does_not_retry_response_or_operation(
    endpoint, monkeypatch, caplog, method, outcome, error_type, fail_on
):
    application, request = endpoint
    calls = []

    def operation(*args, **kwargs):
        calls.append(args)
        if outcome == "business_error":
            raise CollageError("INVALID_REQUEST", "fixture request rejected")
        if outcome == "unexpected_error":
            raise RuntimeError("fixture operation failed")
        return {"id": "fixture"}

    monkeypatch.setattr(
        application,
        "diagnostics" if method == "GET" else "regenerate_overlay",
        operation,
    )
    caplog.set_level(logging.DEBUG, logger="collage.studio.workbench.server")
    handler, connection = request(
        method, error=error_type(10053, "connection closed"), fail_on=fail_on
    )

    assert handler.close_connection
    assert connection.writes == fail_on
    assert len(calls) == 1
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    if outcome == "unexpected_error":
        assert len(errors) == 1
        assert errors[0].exc_info[0] is RuntimeError
    else:
        assert not errors
    assert any(
        "CLIENT_DISCONNECTED" in record.getMessage() for record in caplog.records
    )

    # A subsequent connection must still be handled normally.
    _, following = request(method)
    expected_status = {
        "ok": 200 if method == "GET" else 202,
        "business_error": 400,
        "unexpected_error": 500,
    }
    assert following.output.startswith(f"HTTP/1.0 {expected_status[outcome]} ".encode())


def test_other_write_errors_are_not_silenced(endpoint, monkeypatch):
    application, request = endpoint
    monkeypatch.setattr(application, "diagnostics", lambda *args, **kwargs: {})
    handler, _ = request("GET")

    class BrokenWriter:
        def write(self, body):
            raise OSError("fixture non-connection failure")

    handler.wfile = BrokenWriter()
    with pytest.raises(OSError, match="fixture non-connection failure"):
        handler._json(200, {})
