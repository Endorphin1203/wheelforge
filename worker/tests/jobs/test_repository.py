from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import count
from uuid import UUID

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import StaticPool

from wheelforge_worker.jobs.repository import (
    JobRepository,
    LostLeaseError,
    artifacts,
    build_jobs,
    build_logs,
    build_tasks,
    claim_candidate_statement,
    metadata,
)
from wheelforge_worker.jobs.storage import PublishedObject


NOW = datetime(2026, 8, 1, 8, 0, 0)
JOB_ID = "10000000-0000-4000-8000-000000000001"
TASK_ID = "20000000-0000-4000-8000-000000000001"


def _payload() -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "jobType": "BUILD",
            "subjectId": TASK_ID,
            "createdAt": "2026-08-01T08:00:00Z",
            "payload": {
                "requirementFileId": "30000000-0000-4000-8000-000000000001",
                "normalizedObjectKey": "requirements/normalized/input.txt",
                "solveMode": "COMPATIBLE",
                "targetSnapshot": {
                    "profileId": "40000000-0000-4000-8000-000000000001",
                    "profileCode": "linux-aarch64-cp312",
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
                },
            },
        }
    )


def _repository(*, attempts: int = 0, max_attempts: int = 3) -> JobRepository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(build_tasks).values(
                id=TASK_ID,
                requirement_file_id="30000000-0000-4000-8000-000000000001",
                target_profile_id="40000000-0000-4000-8000-000000000001",
                status="QUEUED",
                progress=0,
                solve_mode="COMPATIBLE",
                target_snapshot={},
                cancel_requested=False,
                version_no=0,
            )
        )
        connection.execute(
            insert(build_jobs).values(
                id=JOB_ID,
                job_type="BUILD",
                payload_version=1,
                subject_id=TASK_ID,
                payload_json=_payload(),
                status="READY",
                priority_no=100,
                available_at=NOW,
                attempts=attempts,
                max_attempts=max_attempts,
                created_at=NOW,
                version_no=0,
            )
        )
    ids = (UUID(f"50000000-0000-4000-8000-{value:012d}") for value in count(1))
    return JobRepository(
        engine,
        lease_seconds=60,
        clock=lambda: NOW,
        uuid_factory=lambda: next(ids),
    )


def test_only_one_worker_leases_a_ready_job() -> None:
    repository = _repository()

    first = repository.claim_next("worker-a")
    second = repository.claim_next("worker-b")

    assert first is not None
    assert first.id == JOB_ID
    assert first.attempts == 1
    assert second is None


def test_expired_lease_is_taken_over_with_a_new_execution() -> None:
    repository = _repository()
    first = repository.claim_next("worker-a")
    assert first is not None
    repository._clock = lambda: NOW + timedelta(seconds=61)

    successor = repository.claim_next("worker-b")

    assert successor is not None
    assert successor.id == first.id
    assert successor.execution_id != first.execution_id
    assert successor.attempts == 2


def test_stale_owner_cannot_heartbeat_or_complete_after_takeover() -> None:
    repository = _repository()
    stale = repository.claim_next("worker-a")
    assert stale is not None
    repository._clock = lambda: NOW + timedelta(seconds=61)
    successor = repository.claim_next("worker-b")
    assert successor is not None

    assert repository.heartbeat(stale) is False
    assert repository.complete(stale) is False
    assert repository.heartbeat(successor) is True
    assert repository.complete(successor) is True


def test_expired_owner_cannot_write_before_a_successor_claims() -> None:
    repository = _repository()
    expired = repository.claim_next("worker-a")
    assert expired is not None
    assert repository.claim_build(expired)
    repository._clock = lambda: NOW + timedelta(seconds=61)

    assert repository.heartbeat(expired) is False
    with pytest.raises(LostLeaseError):
        repository.append_log(expired, "PACKAGING", "INFO", "must not be written")
    assert repository.complete(expired) is False
    assert (
        repository.publish_artifact_terminal(
            expired,
            "60000000-0000-4000-8000-000000000001",
            PublishedObject("artifacts/stale.zip", 3, "a" * 64),
            "stale.zip",
            "SUCCESS",
            NOW + timedelta(days=7),
        )
        is None
    )

    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "RUNNING"
        assert connection.scalar(select(build_logs.c.id)) is None
        assert connection.scalar(select(artifacts.c.id)) is None


def test_exhausted_expired_job_fails_instead_of_being_released() -> None:
    repository = _repository(attempts=1, max_attempts=1)

    assert repository.claim_next("worker-a") is None

    with repository.engine.connect() as connection:
        job_status = connection.scalar(
            select(build_jobs.c.status).where(build_jobs.c.id == JOB_ID)
        )
        task_status = connection.scalar(
            select(build_tasks.c.status).where(build_tasks.c.id == TASK_ID)
        )
    assert job_status == "FAILED"
    assert task_status == "FAILED"


def test_retry_clears_ownership_and_exhaustion_is_terminal() -> None:
    repository = _repository(max_attempts=2)
    first = repository.claim_next("worker-a")
    assert first is not None

    assert repository.retry_or_fail(first, "temporary network failure", retryable=True)
    second = repository.claim_next("worker-b")
    assert second is None
    repository._clock = lambda: NOW + timedelta(seconds=6)
    second = repository.claim_next("worker-b")
    assert second is not None
    assert repository.retry_or_fail(second, "still unavailable", retryable=True)

    with repository.engine.connect() as connection:
        row = connection.execute(
            select(
                build_jobs.c.status,
                build_jobs.c.lease_owner,
                build_jobs.c.execution_id,
            ).where(build_jobs.c.id == JOB_ID)
        ).one()
        task_status = connection.scalar(
            select(build_tasks.c.status).where(build_tasks.c.id == TASK_ID)
        )
    assert row == ("FAILED", None, None)
    assert task_status == "FAILED"


def test_mysql_claim_query_uses_row_lock_skip_locked() -> None:
    sql = str(
        claim_candidate_statement(NOW).compile(
            dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).upper()

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "AVAILABLE_AT <=" in sql
    assert "LEASE_EXPIRES_AT <=" in sql
