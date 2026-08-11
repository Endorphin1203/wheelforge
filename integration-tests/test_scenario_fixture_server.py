from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import pytest

from helpers.fixture_server import create_server


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "test-fixtures" / "index"
HTTP = build_opener(ProxyHandler({}))


@contextmanager
def _server() -> Iterator[str]:
    server = create_server(INDEX, "127.0.0.1", 0, delay_seconds=0.01)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str) -> bytes:
    with HTTP.open(url, timeout=2) as response:
        return response.read()


def test_server_exposes_source_scoped_simple_index() -> None:
    with _server() as base_url:
        page = _get(f"{base_url}/tsinghua/simple/demo-direct/").decode()
        wheel = _get(f"{base_url}/tsinghua/packages/demo_direct-1.0.0-py3-none-any.whl")

    assert "demo_direct-1.0.0-py3-none-any.whl" in page
    assert wheel.startswith(b"PK")


def test_tsinghua_metadata_fails_and_aliyun_metadata_lists_candidates() -> None:
    with _server() as base_url:
        with pytest.raises(HTTPError) as failure:
            _get(f"{base_url}/tsinghua/pypi/upgrade-demo/json")
        payload = json.loads(_get(f"{base_url}/aliyun/pypi/upgrade-demo/json"))

    assert failure.value.code == 503
    assert set(payload["releases"]) == {"1.2.3", "1.2.4"}
    assert payload["releases"]["1.2.4"][0]["packagetype"] == "bdist_wheel"


def test_server_rejects_path_traversal() -> None:
    with _server() as base_url:
        with pytest.raises(HTTPError) as failure:
            _get(f"{base_url}/tsinghua/simple/%2e%2e/packages/secret")

    assert failure.value.code in {400, 404}


def test_partial_wheel_succeeds_once_then_fails() -> None:
    path = "/tsinghua/packages/partial_demo-1.0.0-py3-none-any.whl"
    with _server() as base_url:
        assert _get(base_url + path).startswith(b"PK")
        with pytest.raises(HTTPError) as failure:
            _get(base_url + path)

    assert failure.value.code == 503


def test_slow_wheel_delays_only_after_resolver_fetch() -> None:
    path = "/tsinghua/packages/slow_demo-1.0.0-py3-none-any.whl"
    server = create_server(INDEX, "127.0.0.1", 0, delay_seconds=0.05)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        _get(base_url + path)
        started = time.monotonic()
        _get(base_url + path)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert elapsed >= 0.04
