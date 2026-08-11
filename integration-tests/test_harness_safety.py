from __future__ import annotations

import os
import sys

import pytest

from conftest import _start
from helpers.harness import DatabaseSettings, isolated_environment


def _environment(**overrides: str) -> dict[str, str]:
    environment = {
        "WF_TEST_JDBC_URL": "jdbc:mysql://127.0.0.1:3306/wheelforge_test?connectionTimeZone=UTC",
        "WF_TEST_DATABASE_USER": "tester",
        "WF_TEST_DATABASE_PASSWORD": "password",
        "WF_TEST_DATABASE_DISPOSABLE": "1",
    }
    environment.update(overrides)
    return environment


def test_database_settings_convert_jdbc_url_for_worker() -> None:
    settings = DatabaseSettings.from_environment(_environment())

    assert settings.database == "wheelforge_test"
    assert settings.worker_url.startswith(
        "mysql+pymysql://tester:password@127.0.0.1:3306/"
    )
    assert settings.worker_url.endswith("wheelforge_test?charset=utf8mb4")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"WF_TEST_JDBC_URL": "jdbc:mysql://127.0.0.1:3306/wheelforge"},
            "test database",
        ),
        ({"WF_TEST_DATABASE_DISPOSABLE": "0"}, "DISPOSABLE"),
        ({"WF_TEST_JDBC_URL": "jdbc:postgresql://127.0.0.1/db_test"}, "JDBC"),
        ({"WF_TEST_JDBC_URL": "jdbc:mysql://db.example.com/db_test"}, "local MySQL"),
    ],
)
def test_database_settings_fail_closed(override: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DatabaseSettings.from_environment(_environment(**override))


def test_isolated_environment_removes_external_services_and_proxies() -> None:
    parent = {
        "PATH": "/bin",
        "HOME": "/tmp/home",
        "HTTP_PROXY": "http://proxy.example",
        "PIP_INDEX_URL": "https://pypi.example/simple",
        "REDIS_URL": "redis://example",
        "MINIO_ENDPOINT": "http://example",
        "AWS_ACCESS_KEY_ID": "secret",
        "DOCKER_HOST": "unix:///socket",
        "UNRELATED_SECRET": "must-not-be-inherited",
    }

    child = isolated_environment(parent, {"WF_DATA_ROOT": "/tmp/data"})

    assert child == {
        "HOME": "/tmp/home",
        "PATH": "/bin",
        "WF_DATA_ROOT": "/tmp/data",
    }


def test_process_output_is_bounded_and_sanitized() -> None:
    secret = "integration-secret-value"
    environment = isolated_environment(os.environ, {"PYTHONUNBUFFERED": "1"})
    process = _start(
        "bounded-output-test",
        [sys.executable, "-c", f"print('x' * 70000 + {secret!r})"],
        environment,
        (secret,),
    )
    try:
        assert process.process.wait(timeout=5) == 0
        process.output_thread.join(timeout=5)
        tail = process.tail()
        assert len(tail.encode()) <= 64 * 1024
        assert secret not in tail
        assert "[REDACTED]" in tail
    finally:
        process.stop()
