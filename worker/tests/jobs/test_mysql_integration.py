from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, insert, select, text
from sqlalchemy.engine import make_url

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
)
from wheelforge_worker.jobs.storage import PublishedObject


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
