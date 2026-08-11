from __future__ import annotations

import hashlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.request import ProxyHandler, build_opener

import pytest

from helpers.api import ApiClient
from helpers.artifact import ArtifactReader
from helpers.harness import (
    DatabaseSettings,
    assert_isolated_environment,
    isolated_environment,
)


ROOT = Path(__file__).resolve().parents[1]
WORKER_PYTHON = ROOT / "worker" / ".venv" / "bin" / "python"
TARGET_CODE = "linux-arm64-cp311-manylinux2014"
_APP_TABLES_CHILD_FIRST = (
    "download_records",
    "artifacts",
    "build_logs",
    "resolved_packages",
    "build_jobs",
    "build_tasks",
    "requirement_items",
    "requirement_files",
    "system_config",
    "target_profiles",
    "package_sources",
    "users",
)
_HTTP = build_opener(ProxyHandler({}))


@dataclass
class _ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]
    output_thread: threading.Thread
    output: bytearray
    output_lock: threading.Lock
    secrets: tuple[str, ...]

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        self.output_thread.join(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.output_thread.is_alive():
            self.output_thread.join(timeout=1)

    def tail(self) -> str:
        with self.output_lock:
            content = bytes(self.output)
        text = content.decode("utf-8", "replace")
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text[-64 * 1024 :]


@dataclass(frozen=True)
class NativeStack:
    api: ApiClient
    projects: Path


@pytest.fixture(scope="session")
def artifact_reader() -> type[ArtifactReader]:
    return ArtifactReader


@pytest.fixture()
def native_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[NativeStack]:
    if not sys.platform.startswith("linux"):
        pytest.skip("native Worker integration requires Linux /proc/self/fd support")
    configured = any(
        name.startswith("WF_TEST_DATABASE") or name == "WF_TEST_JDBC_URL"
        for name in os.environ
    )
    if not configured:
        pytest.skip("explicit disposable native MySQL credentials are not configured")
    try:
        database = DatabaseSettings.from_environment(os.environ)
    except ValueError as error:
        pytest.fail(str(error))

    root = tmp_path_factory.mktemp("wheelforge-native-stack")
    data_root = root / "data"
    workspace_root = root / "workspaces"
    data_root.mkdir(mode=0o700)
    workspace_root.mkdir(mode=0o700)
    _clean_database(database)

    processes: list[_ManagedProcess] = []
    try:
        fixture_port = _free_port()
        fixture_url = f"http://127.0.0.1:{fixture_port}"
        fixture = _start(
            "fixture-index",
            [
                str(WORKER_PYTHON),
                str(Path(__file__).parent / "helpers" / "fixture_server.py"),
                str(ROOT / "test-fixtures" / "index"),
                "--host",
                "127.0.0.1",
                "--port",
                str(fixture_port),
            ],
            isolated_environment(os.environ, {"PYTHONUNBUFFERED": "1"}),
            (),
        )
        processes.append(fixture)
        _wait_http(f"{fixture_url}/tsinghua/simple/", fixture)

        api_port = _free_port()
        username = "wheelforge-integration"
        password = (
            "integration-password-"
            + hashlib.sha256(str(root).encode()).hexdigest()[:24]
        )
        token_secret = (
            "integration-token-"
            + hashlib.sha256((str(root) + "token").encode()).hexdigest()
        )
        api_environment = isolated_environment(
            os.environ,
            {
                "SERVER_PORT": str(api_port),
                "MANAGEMENT_ENDPOINT_HEALTH_PROBES_ENABLED": "true",
                "WF_AUTH_TOKEN_SECRET": token_secret,
                "WF_BOOTSTRAP_ADMIN_USERNAME": username,
                "WF_BOOTSTRAP_ADMIN_PASSWORD": password,
                "WF_DATA_ROOT": str(data_root),
                "WF_DATABASE_PASSWORD": database.password,
                "WF_DATABASE_USER": database.username,
                "WF_JDBC_URL": database.jdbc_url,
                "WF_RETENTION_ENABLED": "false",
            },
        )
        api_process = _start(
            "spring-api",
            [str(ROOT / "mvnw"), "-q", "-pl", "backend", "spring-boot:run"],
            api_environment,
            (database.password, password, token_secret),
        )
        processes.append(api_process)
        base_url = f"http://127.0.0.1:{api_port}"
        _wait_http(f"{base_url}/actuator/health/readiness", api_process, timeout=90)
        _seed_target_and_sources(database, fixture_url)

        api = ApiClient(base_url, timeout=90, poll_interval=0.1)
        api.login(username, password)

        worker_environment = isolated_environment(
            os.environ,
            {
                "PYTHONUNBUFFERED": "1",
                "WF_DATABASE_URL": database.worker_url,
                "WF_DATA_ROOT": str(data_root),
                "WF_WORKSPACE_ROOT": str(workspace_root),
                "WF_WORKER_ID": "integration-worker",
                "WF_QUEUE_POLL_SECONDS": "1",
                "WF_JOB_LEASE_SECONDS": "60",
                "WF_MAINTENANCE_AGE_SECONDS": "3600",
                "WF_TEST_FIXTURE_BASE_URL": fixture_url,
            },
        )
        worker_process = _start(
            "python-worker",
            [
                str(WORKER_PYTHON),
                str(Path(__file__).parent / "helpers" / "worker_entry.py"),
            ],
            worker_environment,
            (database.password,),
        )
        processes.append(worker_process)
        time.sleep(0.5)
        if worker_process.process.poll() is not None:
            raise RuntimeError(
                f"Worker exited during startup:\n{worker_process.tail()}"
            )

        yield NativeStack(api, ROOT / "test-fixtures" / "projects")
    finally:
        for process in reversed(processes):
            process.stop()
        _clean_database(database)


def _start(
    name: str, command: list[str], environment: dict[str, str], secrets: tuple[str, ...]
) -> _ManagedProcess:
    assert_isolated_environment(environment)
    output = bytearray()
    output_lock = threading.Lock()
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        raise
    output_thread = threading.Thread(
        target=_drain_output,
        args=(process, output, output_lock),
        name=f"{name}-output",
        daemon=True,
    )
    try:
        output_thread.start()
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise
    return _ManagedProcess(name, process, output_thread, output, output_lock, secrets)


def _drain_output(
    process: subprocess.Popen[bytes], output: bytearray, lock: threading.Lock
) -> None:
    assert process.stdout is not None
    while chunk := os.read(process.stdout.fileno(), 8192):
        with lock:
            output.extend(chunk)
            if len(output) > 64 * 1024:
                del output[: len(output) - 64 * 1024]


def _wait_http(url: str, process: _ManagedProcess, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not contacted"
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise RuntimeError(
                f"{process.name} exited during startup:\n{process.tail()}"
            )
        try:
            with _HTTP.open(url, timeout=2) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except OSError as error:
            last_error = type(error).__name__
        time.sleep(0.1)
    raise TimeoutError(
        f"{process.name} did not become ready ({last_error}):\n{process.tail()}"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _connect(settings: DatabaseSettings) -> Any:
    import pymysql

    connection = pymysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.username,
        password=settings.password,
        database=settings.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
    )
    with connection.cursor() as cursor:
        cursor.execute("select database()")
        observed = cursor.fetchone()[0]
    if observed != settings.database:
        connection.close()
        raise RuntimeError("MySQL connection selected an unexpected database")
    return connection


def _clean_database(settings: DatabaseSettings) -> None:
    with _connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select table_name from information_schema.tables where table_schema = %s",
                (settings.database,),
            )
            existing = {str(row[0]) for row in cursor.fetchall()}
            for table in _APP_TABLES_CHILD_FIRST:
                if table in existing:
                    cursor.execute(f"delete from `{table}`")
        connection.commit()


def _seed_target_and_sources(settings: DatabaseSettings, fixture_url: str) -> None:
    with _connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into target_profiles (
                  id, code, os, architecture, python_implementation, python_version,
                  python_full_version, platform_tag, abi_tags, validation_type,
                  validation_policy_version, enabled, version_no
                ) values (%s, %s, 'LINUX', 'AARCH64', 'CPYTHON', '3.11', '3.11.0',
                          'manylinux2014_aarch64', %s, 'STATIC', 'wheel-tags-v1', true, 0)
                """,
                (
                    "40000000-0000-4000-8000-000000000311",
                    TARGET_CODE,
                    '["cp311","abi3","none"]',
                ),
            )
            for priority, code in enumerate(("TSINGHUA", "ALIYUN", "PYPI"), start=1):
                cursor.execute(
                    """
                    insert into package_sources (
                      id, code, display_name, base_url, priority_no, enabled,
                      timeout_seconds, failure_count, version_no, updated_at
                    ) values (%s, %s, %s, %s, %s, true, 5, 0, 0, utc_timestamp(6))
                    on duplicate key update base_url=values(base_url), enabled=true,
                                            priority_no=values(priority_no)
                    """,
                    (
                        f"10000000-0000-4000-8000-{priority:012d}",
                        code,
                        f"Fixture {code}",
                        f"{fixture_url}/{code.lower()}/simple",
                        priority,
                    ),
                )
        connection.commit()
