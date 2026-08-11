from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Iterator

import pytest

from helpers.api import ApiClient, ApiError


class _Handler(BaseHTTPRequestHandler):
    responses: list[tuple[int, bytes]] = []
    requests: list[tuple[str, str, bytes]] = []

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append((self.command, self.path, body))
        status, content = type(self).responses.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _server(*responses: tuple[int, object]) -> Iterator[tuple[str, type[_Handler]]]:
    handler = type("Handler", (_Handler,), {"responses": [], "requests": []})
    handler.responses = [
        (status, body if isinstance(body, bytes) else json.dumps(body).encode())
        for status, body in responses
    ]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_login_sets_bearer_token_for_later_requests() -> None:
    with _server(
        (200, {"accessToken": "secret", "expiresAt": "2030-01-01T00:00:00Z"}),
        (200, []),
    ) as (url, handler):
        api = ApiClient(url)
        api.login("alice", "password")
        assert api.target_profiles() == []

    assert handler.requests[0][:2] == ("POST", "/api/auth/login")
    assert json.loads(handler.requests[0][2]) == {
        "username": "alice",
        "password": "password",
    }


def test_client_rejects_duplicate_json_keys() -> None:
    with _server((200, b'{"accessToken":"a","accessToken":"b"}')) as (url, _):
        with pytest.raises(ApiError, match="duplicate"):
            ApiClient(url).login("alice", "password")


def test_client_surfaces_http_error_code_without_secrets() -> None:
    with _server((409, {"code": "NOT_READY", "message": "still parsing"})) as (
        url,
        _,
    ):
        api = ApiClient(url, token="do-not-print")
        with pytest.raises(ApiError, match="NOT_READY") as failure:
            api.target_profiles()

    assert "do-not-print" not in str(failure.value)


def test_wait_for_parse_times_out_with_last_status() -> None:
    pending = {"id": "file", "parseStatus": "PENDING"}
    with _server(*[(200, pending) for _ in range(5)]) as (url, _):
        api = ApiClient(url, token="token", poll_interval=0.01, timeout=0.03)
        with pytest.raises(TimeoutError, match="PENDING"):
            api.wait_for_parse("file", "PARSED")
