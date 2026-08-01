from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from typing import Iterator
from uuid import uuid4

import pytest
from packaging.version import Version
from sqlalchemy import Engine, create_engine, event, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from wheelforge_worker.contracts import JobPayload
from wheelforge_worker.jobs.repository import (
    JobRepository,
    LostLeaseError,
    artifacts,
    build_jobs,
    build_logs,
    build_tasks,
    claim_candidate_statement,
    requirement_files,
    resolved_packages,
)
from wheelforge_worker.jobs.storage import PublishedObject, RootedLocalStorage
from wheelforge_worker.resolver import ResolvedPackage, ResolutionResult


DATABASE_URL = os.getenv("WF_TEST_DATABASE_URL")
DISPOSABLE = os.getenv("WF_TEST_DATABASE_DISPOSABLE") == "1"
pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not DISPOSABLE,
    reason="requires explicit disposable WF_TEST_DATABASE_URL",
)
NOW = datetime(2026, 8, 1, 8, 0, 0)
MIGRATION = (
    Path(__file__).parents[3]
    / "backend/src/main/resources/db/migration/V1__baseline.sql"
)
TABLES = (
    "system_config",
    "download_records",
    "artifacts",
    "package_sources",
    "build_logs",
    "resolved_packages",
    "requirement_items",
    "build_jobs",
    "build_tasks",
    "target_profiles",
    "requirement_files",
    "users",
)


@pytest.fixture(scope="module")
def mysql_engine() -> Iterator[Engine]:
    assert DATABASE_URL is not None
    url = make_url(DATABASE_URL)
    database = url.database or ""
    if not re.search(r"(?:^|[_-])test(?:$|[_-])", database, re.IGNORECASE):
        pytest.fail("WF_TEST_DATABASE_URL must name an explicitly disposable test database")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5)
    _drop_known_tables(engine)
    with engine.begin() as connection:
        for statement in MIGRATION.read_text().split(";"):
            if statement.strip():
                connection.execute(text(statement))
    try:
        yield engine
    finally:
        _drop_known_tables(engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_rows(mysql_engine: Engine) -> Iterator[None]:
    _truncate_known_tables(mysql_engine)
    yield
    _truncate_known_tables(mysql_engine)


def test_mysql_json_and_concurrent_skip_locked(mysql_engine: Engine) -> None:
    first_job, _first_task = _seed_build(mysql_engine, ordinal=1)
    second_job, second_task = _seed_build(mysql_engine, ordinal=2)
    repository = JobRepository(mysql_engine, lease_seconds=60, clock=lambda: NOW)

    with mysql_engine.connect() as blocker:
        transaction = blocker.begin()
        locked = blocker.execute(claim_candidate_statement(NOW)).mappings().one()
        assert str(locked["id"]) == first_job

        claimed = repository.claim_next("worker-b")

        assert claimed is not None
        assert claimed.id == second_job
        assert claimed.subject_id == second_task
        assert JobPayload.model_validate_json(claimed.payload_json).payload[
            "targetSnapshot"
        ]["architecture"] == "AARCH64"
        transaction.rollback()


def test_mysql_expired_takeover_stale_owner_logs_and_atomic_terminal(
    mysql_engine: Engine,
) -> None:
    _job_id, task_id = _seed_build(mysql_engine, ordinal=3)
    clock = [NOW]
    repository = JobRepository(
        mysql_engine, lease_seconds=60, clock=lambda: clock[0]
    )
    stale = repository.claim_next("worker-a")
    assert stale is not None
    assert repository.claim_build(stale)
    clock[0] = NOW + timedelta(seconds=61)

    successor = repository.claim_next("worker-b")

    assert successor is not None
    assert successor.reclaimed is True
    assert successor.previous_execution_id == stale.execution_id
    assert repository.heartbeat(stale) is False
    with pytest.raises(LostLeaseError):
        repository.append_log(stale, "RESOLVING", "INFO", "stale")
    assert repository.claim_build(successor)
    assert repository.append_log(successor, "RESOLVING", "INFO", "one") == 1
    assert repository.append_log(successor, "RESOLVING", "INFO", "two") == 2

    artifact_id = str(uuid4())
    published = PublishedObject(
        f"artifacts/{task_id}/{artifact_id}.zip", 4, "a" * 64
    )
    assert repository.publish_artifact_terminal(
        successor,
        artifact_id,
        published,
        "bundle.zip",
        "SUCCESS",
        clock[0] + timedelta(days=7),
    ) == artifact_id

    with mysql_engine.connect() as connection:
        job_status = connection.scalar(
            select(build_jobs.c.status).where(build_jobs.c.id == successor.id)
        )
        task_status = connection.scalar(
            select(build_tasks.c.status).where(build_tasks.c.id == task_id)
        )
        sequences = connection.scalars(
            select(build_logs.c.sequence_no)
            .where(build_logs.c.build_task_id == task_id)
            .order_by(build_logs.c.sequence_no)
        ).all()
        artifact_count = connection.scalar(
            select(artifacts.c.id).where(artifacts.c.id == artifact_id)
        )
    assert (job_status, task_status, sequences, artifact_count) == (
        "COMPLETED",
        "SUCCESS",
        [1, 2],
        artifact_id,
    )


def test_mysql_concurrent_log_sequence_allocation_is_monotonic(
    mysql_engine: Engine,
) -> None:
    _job_id, task_id = _seed_build(mysql_engine, ordinal=4)
    repository = JobRepository(mysql_engine, lease_seconds=60, clock=lambda: NOW)
    lease = repository.claim_next("worker-a")
    assert lease is not None
    assert repository.claim_build(lease)
    ready = Barrier(2)

    def append(message: str) -> int:
        ready.wait(timeout=2)
        return repository.append_log(lease, "RESOLVING", "INFO", message)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(append, "one")
        second = executor.submit(append, "two")
        sequences = sorted((first.result(timeout=5), second.result(timeout=5)))

    with mysql_engine.connect() as connection:
        persisted = connection.scalars(
            select(build_logs.c.sequence_no)
            .where(build_logs.c.build_task_id == task_id)
            .order_by(build_logs.c.sequence_no)
        ).all()
    assert sequences == [1, 2]
    assert persisted == [1, 2]


def test_mysql_package_audit_failure_rolls_back_prior_rows(
    mysql_engine: Engine,
) -> None:
    _job_id, task_id = _seed_build(mysql_engine, ordinal=5)
    repository = JobRepository(mysql_engine, lease_seconds=60, clock=lambda: NOW)
    lease = repository.claim_next("worker-a")
    assert lease is not None
    assert repository.claim_build(lease)
    original = _resolved("alpha", "1.0")
    repository.persist_resolution(lease, ResolutionResult("1", (original,)))

    with pytest.raises(IntegrityError):
        repository.persist_resolution(
            lease,
            ResolutionResult("1", (_resolved("alpha", "2.0"), _resolved("alpha", "3.0"))),
        )

    with mysql_engine.connect() as connection:
        rows = connection.execute(
            select(
                resolved_packages.c.normalized_name,
                resolved_packages.c.final_version,
            ).where(resolved_packages.c.build_task_id == task_id)
        ).all()
    assert rows == [("alpha", "1.0")]


def test_mysql_expired_job_cannot_be_reclaimed_during_audit_transaction(
    mysql_engine: Engine,
) -> None:
    _job_id, _task_id = _seed_build(mysql_engine, ordinal=6)
    clock = [NOW]
    repository = JobRepository(mysql_engine, lease_seconds=60, clock=lambda: clock[0])
    lease = repository.claim_next("worker-a")
    assert lease is not None
    assert repository.claim_build(lease)
    insert_started = Event()
    release_insert = Event()

    def pause_audit_insert(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "INSERT INTO resolved_packages" in statement:
            insert_started.set()
            assert release_insert.wait(5)

    event.listen(mysql_engine, "before_cursor_execute", pause_audit_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            persistence = executor.submit(
                repository.persist_resolution,
                lease,
                ResolutionResult("1", (_resolved("alpha", "1.0"),)),
            )
            assert insert_started.wait(5)
            clock[0] = NOW + timedelta(seconds=61)
            takeover = executor.submit(repository.claim_next, "worker-b")
            with pytest.raises(FutureTimeoutError):
                takeover.result(timeout=0.2)
            release_insert.set()
            persistence.result(timeout=5)
            successor = takeover.result(timeout=5)
    finally:
        release_insert.set()
        event.remove(mysql_engine, "before_cursor_execute", pause_audit_insert)

    assert successor is not None
    assert successor.reclaimed is True
    assert successor.previous_execution_id == lease.execution_id
    assert repository.heartbeat(lease) is False


def test_mysql_metadata_failure_allows_exact_local_artifact_compensation(
    mysql_engine: Engine, tmp_path: Path
) -> None:
    _job_id, task_id = _seed_build(mysql_engine, ordinal=7)
    repository = JobRepository(mysql_engine, lease_seconds=60, clock=lambda: NOW)
    lease = repository.claim_next("worker-a")
    assert lease is not None
    assert repository.claim_build(lease)
    duplicate_id = str(uuid4())
    with mysql_engine.begin() as connection:
        connection.execute(
            insert(artifacts).values(
                id=duplicate_id,
                build_task_id=task_id,
                artifact_type="OFFLINE_WHEEL_BUNDLE",
                filename="existing.zip",
                object_key=f"artifacts/{task_id}/{uuid4()}.zip",
                size_bytes=1,
                sha256="b" * 64,
                build_status="SUCCESS",
                validation_type="STATIC",
                expires_at=NOW + timedelta(days=7),
                download_count=0,
                created_at=NOW,
                version_no=0,
            )
        )
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    object_key = f"artifacts/{task_id}/{uuid4()}.zip"
    published = storage.publish_bytes(object_key, b"orphan")

    with pytest.raises(IntegrityError):
        repository.publish_artifact_terminal(
            lease,
            duplicate_id,
            published,
            "new.zip",
            "SUCCESS",
            NOW + timedelta(days=7),
        )

    assert storage.delete_if_owned(object_key, published.sha256) is True
    assert not (root / object_key).exists()
    with mysql_engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "RUNNING"
        assert connection.scalar(select(build_tasks.c.status)) == "RESOLVING"


def _seed_build(engine: Engine, *, ordinal: int) -> tuple[str, str]:
    user_id = f"70000000-0000-4000-8000-{ordinal:012d}"
    file_id = f"30000000-0000-4000-8000-{ordinal:012d}"
    profile_id = f"40000000-0000-4000-8000-{ordinal:012d}"
    task_id = f"20000000-0000-4000-8000-{ordinal:012d}"
    job_id = f"10000000-0000-4000-8000-{ordinal:012d}"
    target = {
        "profileId": profile_id,
        "profileCode": f"linux-aarch64-cp312-{ordinal}",
        "os": "LINUX",
        "architecture": "AARCH64",
        "pythonImplementation": "CPYTHON",
        "pythonVersion": "3.12",
        "pythonFullVersion": "3.12.11",
        "platformTag": "manylinux2014_aarch64",
        "abiTags": ["cp312", "abi3", "none"],
        "validationType": "STATIC",
        "validationPolicyVersion": "wheel-tags-v1",
        "profileVersion": 0,
    }
    payload = {
        "schemaVersion": 1,
        "jobType": "BUILD",
        "subjectId": task_id,
        "createdAt": "2026-08-01T08:00:00Z",
        "payload": {
            "requirementFileId": file_id,
            "normalizedObjectKey": f"requirements/{ordinal}/normalized.txt",
            "solveMode": "COMPATIBLE",
            "targetSnapshot": target,
        },
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "insert into users(id, username, password_hash, role, status, created_at) "
                "values (:id, :username, 'hash', 'USER', 'ACTIVE', :created_at)"
            ),
            {"id": user_id, "username": f"mysql-user-{ordinal}", "created_at": NOW},
        )
        connection.execute(
            text(
                "insert into target_profiles(id, code, os, architecture, "
                "python_implementation, python_version, python_full_version, platform_tag, "
                "abi_tags, validation_type, validation_policy_version, enabled, version_no) "
                "values (:id, :code, 'LINUX', 'AARCH64', 'CPYTHON', '3.12', "
                "'3.12.11', 'manylinux2014_aarch64', :abi_tags, 'STATIC', "
                "'wheel-tags-v1', true, 0)"
            ),
            {"id": profile_id, "code": target["profileCode"], "abi_tags": json.dumps(target["abiTags"])},
        )
        connection.execute(
            insert(requirement_files).values(
                id=file_id,
                user_id=user_id,
                original_name="requirements.txt",
                size_bytes=13,
                sha256="a" * 64,
                original_object_key=f"requirements/{ordinal}/original.txt",
                normalized_object_key=f"requirements/{ordinal}/normalized.txt",
                parse_status="PARSED",
                created_at=NOW,
                version_no=0,
            )
        )
        connection.execute(
            insert(build_tasks).values(
                id=task_id,
                user_id=user_id,
                requirement_file_id=file_id,
                target_profile_id=profile_id,
                status="QUEUED",
                progress=0,
                solve_mode="COMPATIBLE",
                target_snapshot=target,
                cancel_requested=False,
                created_at=NOW,
                version_no=0,
            )
        )
        connection.execute(
            insert(build_jobs).values(
                id=job_id,
                job_type="BUILD",
                payload_version=1,
                subject_id=task_id,
                payload_json=payload,
                status="READY",
                priority_no=100,
                available_at=NOW,
                attempts=0,
                max_attempts=3,
                created_at=NOW + timedelta(microseconds=ordinal),
                version_no=0,
            )
        )
    return job_id, task_id


def _resolved(name: str, version: str) -> ResolvedPackage:
    return ResolvedPackage(
        name,
        Version(version),
        True,
        f"https://files.pythonhosted.org/{name}.whl",
        f"{name}-{version}-py3-none-any.whl",
        (),
        None,
        (),
    )


def _drop_known_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("set foreign_key_checks=0"))
        for table in TABLES:
            connection.execute(text(f"drop table if exists `{table}`"))
        connection.execute(text("set foreign_key_checks=1"))


def _truncate_known_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("set foreign_key_checks=0"))
        for table in TABLES:
            connection.execute(text(f"truncate table `{table}`"))
        connection.execute(text("set foreign_key_checks=1"))
