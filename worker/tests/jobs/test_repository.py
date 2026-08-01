from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import UUID

import pytest
from packaging.tags import Tag
from packaging.version import Version
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import StaticPool

import wheelforge_worker.jobs.repository as repository_module
from wheelforge_worker.jobs.repository import (
    MAX_BUILD_AUDIT_BYTES,
    MAX_PACKAGE_AUDIT_BYTES,
    MAX_RESOLVED_PACKAGES,
    JobRepository,
    LostLeaseError,
    artifacts,
    build_jobs,
    build_logs,
    build_tasks,
    claim_candidate_statement,
    metadata,
    requirement_files,
    resolved_packages,
)
from wheelforge_worker.jobs.storage import PublishedObject
from wheelforge_worker.download import DownloadedWheel
from wheelforge_worker.resolver import (
    CandidateRejection,
    CandidateRejectionCode,
    CandidateSelection,
    CompatibilityFailureCode,
    ResolutionAttempt,
    ResolvedPackage,
    ResolutionResult,
)
from wheelforge_worker.sources import PackageSource
from wheelforge_worker.validation import (
    StaticValidationReport,
    ValidationIssue,
    ValidationIssueCode,
)


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
            insert(requirement_files).values(
                id="30000000-0000-4000-8000-000000000001",
                user_id="70000000-0000-4000-8000-000000000001",
                original_name="requirements.txt",
                size_bytes=13,
                sha256="a" * 64,
                original_object_key="requirements/original/input.txt",
                normalized_object_key="requirements/normalized/input.txt",
                parse_status="PARSED",
                created_at=NOW,
                version_no=0,
            )
        )
        connection.execute(
            insert(build_tasks).values(
                id=TASK_ID,
                user_id="70000000-0000-4000-8000-000000000001",
                requirement_file_id="30000000-0000-4000-8000-000000000001",
                target_profile_id="40000000-0000-4000-8000-000000000001",
                status="QUEUED",
                progress=0,
                solve_mode="COMPATIBLE",
                target_snapshot=json.loads(_payload())["payload"]["targetSnapshot"],
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


def test_decoded_oversized_payload_is_leased_as_malformed_without_serializing_it() -> None:
    repository = _repository()
    with repository.engine.begin() as connection:
        connection.execute(
            update(build_jobs)
            .where(build_jobs.c.id == JOB_ID)
            .values(payload_json={"payload": "x" * 5000})
        )

    lease = repository.claim_next("worker-a")

    assert lease is not None
    assert lease.payload_json == "{}"


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
    assert successor.reclaimed is True
    assert successor.previous_execution_id == first.execution_id


def test_repository_sanitizes_persisted_errors_and_log_context() -> None:
    repository = _repository()
    lease = repository.claim_next("worker-a")
    assert lease is not None
    assert repository.claim_build(lease)
    secret = "mysql://user:db-password@db/wf?access_token=query-token"

    repository.append_log(
        lease,
        "DOWNLOADING",
        "ERROR",
        secret,
        {"nested": {"authorization": "Bearer bearer-token"}},
    )
    assert repository.retry_or_fail(lease, secret, retryable=True)

    with repository.engine.connect() as connection:
        persisted_error = connection.scalar(select(build_jobs.c.last_error))
        log = connection.execute(
            select(build_logs.c.message, build_logs.c.context_json)
        ).one()
    combined = f"{persisted_error} {log.message} {log.context_json}"
    assert "db-password" not in combined
    assert "query-token" not in combined
    assert "bearer-token" not in combined


@pytest.mark.parametrize(
    "stage", ["RESOLVING", "DOWNLOADING", "VALIDATING", "PACKAGING"]
)
def test_expired_build_reclaims_exact_prior_execution(stage: str) -> None:
    repository = _repository()
    first = repository.claim_next("worker-a")
    assert first is not None
    with repository.engine.begin() as connection:
        connection.execute(
            update(build_tasks)
            .where(build_tasks.c.id == TASK_ID)
            .values(status=stage, execution_id=first.execution_id)
        )
    repository._clock = lambda: NOW + timedelta(seconds=61)

    successor = repository.claim_next("worker-b")
    assert successor is not None
    subject = repository.claim_build_subject(successor)

    assert subject is not None
    assert subject.id == TASK_ID
    with repository.engine.connect() as connection:
        task = connection.execute(
            select(build_tasks.c.status, build_tasks.c.execution_id)
        ).one()
    assert task == ("RESOLVING", successor.execution_id)


def test_expired_parse_recovers_parsing_subject_from_same_job() -> None:
    repository = _repository()
    with repository.engine.begin() as connection:
        connection.execute(
            insert(requirement_files).values(
                id="30000000-0000-4000-8000-000000000002",
                user_id="70000000-0000-4000-8000-000000000001",
                original_name="requirements.txt",
                size_bytes=4,
                sha256="a" * 64,
                original_object_key="requirements/original/a.txt",
                normalized_object_key="requirements/normalized/a.txt",
                parse_status="PARSING",
                created_at=NOW,
                version_no=0,
            )
        )
        connection.execute(
            update(build_jobs).where(build_jobs.c.id == JOB_ID).values(
                job_type="REQUIREMENT_PARSE",
                subject_id="30000000-0000-4000-8000-000000000002",
                status="RUNNING",
                attempts=1,
                lease_owner="dead-worker",
                execution_id="80000000-0000-4000-8000-000000000001",
                lease_expires_at=NOW - timedelta(seconds=1),
                heartbeat_at=NOW - timedelta(seconds=61),
            )
        )

    successor = repository.claim_next("worker-b")
    assert successor is not None and successor.reclaimed
    subject = repository.claim_parse_subject(successor)

    assert subject is not None
    assert subject.id == successor.subject_id
    assert subject.original_object_key == "requirements/original/a.txt"


def test_subject_claim_failure_terminalizes_newly_reclaimed_job() -> None:
    repository = _repository()
    first = repository.claim_next("worker-a")
    assert first is not None
    with repository.engine.begin() as connection:
        connection.execute(
            update(build_tasks)
            .where(build_tasks.c.id == TASK_ID)
            .values(status="DOWNLOADING", execution_id="wrong-execution")
        )
    repository._clock = lambda: NOW + timedelta(seconds=61)
    successor = repository.claim_next("worker-b")
    assert successor is not None

    assert repository.claim_build_subject(successor) is None

    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "FAILED"


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


def test_owned_subject_operations_lock_the_exact_unexpired_job_row() -> None:
    repository = _repository()
    lease = repository.claim_next("worker-a")
    assert lease is not None

    sql = str(
        repository._owned_job_statement(lease, NOW).compile(
            dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).upper()

    assert "FOR UPDATE" in sql
    assert "LEASE_EXPIRES_AT" in sql
    assert "EXECUTION_ID" in sql


def test_package_audit_persists_attempts_and_final_static_outcomes() -> None:
    repository = _repository()
    lease = repository.claim_next("worker-a")
    assert lease is not None
    assert repository.claim_build_subject(lease) is not None
    alpha = ResolvedPackage(
        "alpha",
        Version("1.0"),
        True,
        "https://files.pythonhosted.org/alpha.whl",
        "alpha-1.0-py3-none-any.whl",
        (),
        None,
        (),
    )
    beta = ResolvedPackage(
        "beta",
        Version("2.0"),
        False,
        "https://files.pythonhosted.org/beta.whl",
        "beta-2.0-py3-none-any.whl",
        (),
        None,
        (),
    )
    resolution = ResolutionResult(
        "1",
        (alpha, beta),
        attempts=(
            ResolutionAttempt(
                (CandidateSelection("alpha", Version("1.0")),),
                PackageSource.PYPI,
                CompatibilityFailureCode.STRICT_RESOLUTION,
                "first source failed",
            ),
        ),
        source=PackageSource.PYPI,
    )
    repository.persist_resolution(lease, resolution)
    wheel = DownloadedWheel(
        "alpha",
        Version("1.0"),
        alpha.wheel_filename,
        Path("/trusted/alpha.whl"),
        PackageSource.PYPI,
        5,
        "b" * 64,
        frozenset({Tag("py3", "none", "any")}),
    )
    validation = StaticValidationReport(
        (
            ValidationIssue(
                ValidationIssueCode.WHEEL_MISSING,
                "beta",
                "no matching Wheel was downloaded",
            ),
        ),
        False,
    )

    repository.persist_package_results(
        lease,
        resolution,
        (wheel,),
        validation,
        {"beta": "no target Wheel"},
    )

    with repository.engine.connect() as connection:
        rows = connection.execute(
            select(
                resolved_packages.c.normalized_name,
                resolved_packages.c.wheel_status,
                resolved_packages.c.sha256,
                resolved_packages.c.wheel_tags,
                resolved_packages.c.error_message,
                resolved_packages.c.attempts_json,
            ).order_by(resolved_packages.c.normalized_name)
        ).mappings().all()
        audit_logs = connection.execute(
            select(build_logs.c.stage, build_logs.c.context_json).where(
                build_logs.c.stage == "RESOLUTION_AUDIT"
            )
        ).all()
    assert rows[0]["wheel_status"] == "STATIC_PASSED"
    assert rows[0]["sha256"] == "b" * 64
    assert rows[0]["wheel_tags"] == ["py3-none-any"]
    assert rows[0]["attempts_json"]
    assert rows[1]["wheel_status"] == "MISSING"
    assert rows[1]["error_message"] == "no target Wheel"
    assert len(audit_logs) == 1
    assert len(json.dumps(audit_logs[0].context_json).encode("utf-8")) <= 8000


def test_package_audit_is_filtered_explainable_and_strictly_byte_bounded() -> None:
    alpha = ResolvedPackage(
        "alpha", Version("1.0"), True, "https://example/alpha", "alpha.whl", (), None, ()
    )
    attempts = tuple(
        ResolutionAttempt(
            (
                CandidateSelection("alpha", Version(f"1.{index}")),
                CandidateSelection("beta", Version(f"2.{index}")),
            ),
            PackageSource.PYPI,
            CompatibilityFailureCode.STRICT_RESOLUTION,
            "失败" * 1000,
        )
        for index in range(100)
    )
    rejections = tuple(
        CandidateRejection(
            "alpha" if index % 2 == 0 else "beta",
            f"alpha-only-{index}" if index % 2 == 0 else f"beta-only-{index}",
            PackageSource.ALIYUN,
            CandidateRejectionCode.NO_TARGET_WHEEL,
            "拒绝" * 1000,
        )
        for index in range(2000)
    )
    resolution = ResolutionResult("1", (alpha,), attempts, rejections)

    audit = repository_module._package_resolution_audit(resolution, "alpha")
    encoded = json.dumps(audit, ensure_ascii=True).encode("utf-8")

    assert len(encoded) <= MAX_PACKAGE_AUDIT_BYTES
    assert audit["package"] == "alpha"
    assert audit["omitted"]["attempts"] > 0
    assert audit["omitted"]["rejections"] > 0
    assert "beta-only" not in encoded.decode("ascii")
    assert any(item["selectedVersion"] == "1.0" for item in audit["attempts"])


def test_worst_case_package_audit_total_growth_has_a_fixed_upper_bound() -> None:
    packages = tuple(
        ResolvedPackage(
            f"package-{index}",
            Version("1.0"),
            True,
            "https://example.invalid/wheel",
            f"package-{index}-1.0-py3-none-any.whl",
            (),
            None,
            (),
        )
        for index in range(MAX_RESOLVED_PACKAGES)
    )
    selections = tuple(
        CandidateSelection(package.name, package.version) for package in packages
    )
    resolution = ResolutionResult(
        "1",
        packages,
        attempts=(
            ResolutionAttempt(
                selections,
                PackageSource.PYPI,
                CompatibilityFailureCode.STRICT_RESOLUTION,
                "x" * 10000,
            ),
        ),
    )

    total = sum(
        len(
            json.dumps(
                repository_module._package_resolution_audit(resolution, package.name),
                ensure_ascii=True,
            ).encode("utf-8")
        )
        for package in packages
    )

    assert total <= MAX_RESOLVED_PACKAGES * MAX_PACKAGE_AUDIT_BYTES


def test_full_resolution_audit_is_stored_once_under_a_strict_byte_limit() -> None:
    package = ResolvedPackage(
        "alpha", Version("1.0"), True, "https://example/alpha", "alpha.whl", (), None, ()
    )
    attempts = tuple(
        ResolutionAttempt(
            (CandidateSelection("alpha", Version(f"1.{index}")),),
            PackageSource.PYPI,
            CompatibilityFailureCode.STRICT_RESOLUTION,
            "失败" * 1000,
        )
        for index in range(100)
    )
    rejections = tuple(
        CandidateRejection(
            "alpha",
            f"{index}.0",
            PackageSource.PYPI,
            CandidateRejectionCode.NO_TARGET_WHEEL,
            "拒绝" * 1000,
        )
        for index in range(2000)
    )

    audit = repository_module._resolution_summary_audit(
        ResolutionResult("1", (package,), attempts, rejections)
    )

    assert len(json.dumps(audit, ensure_ascii=True).encode("utf-8")) <= MAX_BUILD_AUDIT_BYTES
    assert audit["totals"] == {"attempts": 100, "rejections": 2000}
    assert audit["omitted"]["attempts"] > 0
    assert audit["omitted"]["rejections"] > 0


def test_high_cardinality_audit_streams_into_fixed_size_accumulators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = ResolvedPackage(
        "alpha", Version("1.0"), True, "https://example/alpha", "alpha.whl", (), None, ()
    )
    resolution = ResolutionResult(
        "1",
        (package,),
        attempts=tuple(
            ResolutionAttempt(
                (CandidateSelection("alpha", Version(f"1.{index}")),),
                PackageSource.PYPI,
                CompatibilityFailureCode.STRICT_RESOLUTION,
                "x" * 1000,
            )
            for index in range(10_000)
        ),
        rejections=tuple(
            CandidateRejection(
                "alpha",
                str(index),
                PackageSource.PYPI,
                CandidateRejectionCode.NO_TARGET_WHEEL,
                "y" * 1000,
            )
            for index in range(10_000)
        ),
        rejections_omitted=123,
        observations_truncated=True,
    )
    maximum_retained = 0
    original_add = repository_module._AuditAccumulator.add

    def tracking_add(self: object, entry: dict[str, object]) -> None:
        nonlocal maximum_retained
        original_add(self, entry)
        maximum_retained = max(maximum_retained, len(self.entries))

    monkeypatch.setattr(repository_module._AuditAccumulator, "add", tracking_add)

    package_audit = repository_module._package_resolution_audit(resolution, "alpha")
    summary = repository_module._resolution_summary_audit(resolution)

    assert maximum_retained < 100
    assert package_audit["omitted"]["rejections"] >= 123
    assert summary["truncated"] == {
        "observations": True,
        "rejections": 123,
    }
    assert len(json.dumps(package_audit, ensure_ascii=True).encode()) <= MAX_PACKAGE_AUDIT_BYTES
    assert len(json.dumps(summary, ensure_ascii=True).encode()) <= MAX_BUILD_AUDIT_BYTES


def test_persist_resolution_rejects_package_count_before_database_writes() -> None:
    repository = _repository()
    lease = repository.claim_next("worker-a")
    assert lease is not None
    assert repository.claim_build_subject(lease) is not None
    package = ResolvedPackage(
        "alpha", Version("1.0"), True, "https://example/alpha", "alpha.whl", (), None, ()
    )
    resolution = ResolutionResult(
        "1", tuple(package for _ in range(MAX_RESOLVED_PACKAGES + 1))
    )

    with pytest.raises(ValueError, match="package count"):
        repository.persist_resolution(lease, resolution)

    with repository.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(resolved_packages)) == 0
