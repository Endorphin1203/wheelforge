from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote, urlsplit


_SAFE_PARENT_KEYS = frozenset(
    {"HOME", "JAVA_HOME", "LANG", "LC_ALL", "M2_HOME", "PATH", "TMPDIR"}
)
_FORBIDDEN_ENV_FRAGMENTS = (
    "PROXY",
    "DOCKER",
    "REDIS",
    "MINIO",
    "S3",
    "AWS_",
    "PIP_INDEX",
    "PIP_EXTRA_INDEX",
)
_TEST_DATABASE = re.compile(r"(?:^|[_-])(test|integration)(?:$|[_-])", re.IGNORECASE)


@dataclass(frozen=True)
class DatabaseSettings:
    jdbc_url: str
    username: str
    password: str
    host: str
    port: int
    database: str
    worker_url: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "DatabaseSettings":
        required = (
            "WF_TEST_JDBC_URL",
            "WF_TEST_DATABASE_USER",
            "WF_TEST_DATABASE_PASSWORD",
        )
        missing = [name for name in required if not environment.get(name)]
        if missing:
            raise ValueError(f"missing native MySQL settings: {', '.join(missing)}")
        if environment.get("WF_TEST_DATABASE_DISPOSABLE") != "1":
            raise ValueError("WF_TEST_DATABASE_DISPOSABLE=1 is required")

        jdbc_url = environment["WF_TEST_JDBC_URL"]
        prefix = "jdbc:mysql://"
        if not jdbc_url.startswith(prefix):
            raise ValueError("WF_TEST_JDBC_URL must be a MySQL JDBC URL")
        parsed = urlsplit("mysql://" + jdbc_url[len(prefix) :])
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                "JDBC credentials must use the dedicated environment variables"
            )
        if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("integration tests require local MySQL")
        database = parsed.path.removeprefix("/")
        if not database or "/" in database or not _TEST_DATABASE.search(database):
            raise ValueError(
                "refusing a database name that is not visibly a test database"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", database):
            raise ValueError("test database name contains unsafe characters")
        port = parsed.port or 3306
        username = environment["WF_TEST_DATABASE_USER"]
        password = environment["WF_TEST_DATABASE_PASSWORD"]
        worker_host = (
            f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        )
        authority = f"{quote(username, safe='')}:{quote(password, safe='')}@{worker_host}:{port}"
        worker_url = f"mysql+pymysql://{authority}/{database}?charset=utf8mb4"
        return cls(
            jdbc_url, username, password, parsed.hostname, port, database, worker_url
        )


def isolated_environment(
    parent: Mapping[str, str], additions: Mapping[str, str]
) -> dict[str, str]:
    _assert_no_forbidden_keys(additions)
    result = {key: parent[key] for key in sorted(_SAFE_PARENT_KEYS) if parent.get(key)}
    result.update(additions)
    return result


def assert_isolated_environment(environment: Mapping[str, str]) -> None:
    _assert_no_forbidden_keys(environment)


def _assert_no_forbidden_keys(environment: Mapping[str, str]) -> None:
    forbidden = sorted(
        key
        for key in environment
        if any(fragment in key.upper() for fragment in _FORBIDDEN_ENV_FRAGMENTS)
    )
    if forbidden:
        raise ValueError(f"forbidden child-process settings: {', '.join(forbidden)}")
