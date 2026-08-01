from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    case,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.sql import Select

from wheelforge_worker.parser import ParsedRequirements
from wheelforge_worker.contracts import bounded_database_payload_json
from wheelforge_worker.download import DownloadedWheel
from wheelforge_worker.resolver import ResolutionResult, VersionChangeKind
from wheelforge_worker.validation import StaticValidationReport

from .errors import sanitize_structure, sanitize_text
from .maintenance import MaintenanceSnapshot
from .storage import PublishedObject


metadata = MetaData()

requirement_files = Table(
    "requirement_files",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36)),
    Column("original_name", String(255)),
    Column("detected_encoding", String(20)),
    Column("size_bytes", BigInteger),
    Column("sha256", String(64)),
    Column("original_object_key", String(512)),
    Column("normalized_object_key", String(512)),
    Column("parse_status", String(20), nullable=False),
    Column("parse_error", String(2000)),
    Column("created_at", DateTime),
    Column("version_no", BigInteger, nullable=False, default=0),
)

build_tasks = Table(
    "build_tasks",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36)),
    Column("requirement_file_id", String(36), nullable=False),
    Column("target_profile_id", String(36), nullable=False),
    Column("source_task_id", String(36)),
    Column("execution_id", String(36)),
    Column("status", String(30), nullable=False),
    Column("progress", Integer, nullable=False, default=0),
    Column("current_stage", String(50)),
    Column("solve_mode", String(20), nullable=False),
    Column("target_snapshot", JSON, nullable=False),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("failure_code", String(100)),
    Column("failure_message", String(2000)),
    Column("created_at", DateTime),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("deleted_at", DateTime),
    Column("version_no", BigInteger, nullable=False, default=0),
)

build_jobs = Table(
    "build_jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_type", String(30), nullable=False),
    Column("payload_version", Integer, nullable=False),
    Column("subject_id", String(36), nullable=False),
    Column("payload_json", JSON, nullable=False),
    Column("status", String(20), nullable=False),
    Column("priority_no", Integer, nullable=False, default=100),
    Column("available_at", DateTime, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=3),
    Column("lease_owner", String(100)),
    Column("execution_id", String(36)),
    Column("lease_expires_at", DateTime),
    Column("heartbeat_at", DateTime),
    Column("last_error", String(2000)),
    Column("created_at", DateTime, nullable=False),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("version_no", BigInteger, nullable=False, default=0),
)

requirement_items = Table(
    "requirement_items",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("requirement_file_id", String(36), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("normalized_name", String(255), nullable=False),
    Column("extras_json", JSON, nullable=False),
    Column("specifier", String(500), nullable=False),
    Column("marker_text", String(1000)),
    Column("original_text", String(2000), nullable=False),
    Column("supported", Boolean, nullable=False),
    Column("error_code", String(100)),
    Column("error_message", String(2000)),
)

resolved_packages = Table(
    "resolved_packages",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("build_task_id", String(36), nullable=False),
    Column("normalized_name", String(255), nullable=False),
    Column("final_version", String(100)),
    Column("dependency_type", String(20), nullable=False),
    Column("original_constraint", String(500)),
    Column("strict_version", String(100)),
    Column("change_direction", String(30), nullable=False),
    Column("change_reason", String(1000)),
    Column("attempts_json", JSON, nullable=False),
    Column("wheel_filename", String(500)),
    Column("wheel_tags", JSON),
    Column("package_source_code", String(50)),
    Column("sha256", String(64)),
    Column("wheel_status", String(30), nullable=False),
    Column("error_message", String(2000)),
)

build_logs = Table(
    "build_logs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("build_task_id", String(36), nullable=False),
    Column("sequence_no", BigInteger, nullable=False),
    Column("stage", String(50), nullable=False),
    Column("level", String(20), nullable=False),
    Column("message", String(4000), nullable=False),
    Column("context_json", JSON),
    Column("created_at", DateTime, nullable=False),
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("build_task_id", String(36), nullable=False),
    Column("artifact_type", String(30), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("object_key", String(512), nullable=False, unique=True),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("build_status", String(30), nullable=False),
    Column("validation_type", String(30), nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("download_count", BigInteger, nullable=False, default=0),
    Column("cleaned_at", DateTime),
    Column("created_at", DateTime, nullable=False),
    Column("version_no", BigInteger, nullable=False, default=0),
)


@dataclass(frozen=True, slots=True)
class JobLease:
    id: str
    job_type: str
    subject_id: str
    payload_version: int
    payload_json: str
    worker_id: str
    execution_id: str
    attempts: int
    max_attempts: int
    reclaimed: bool = False
    previous_execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParseSubject:
    id: str
    user_id: str
    original_object_key: str
    normalized_object_key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BuildSubject:
    id: str
    user_id: str
    requirement_file_id: str
    original_object_key: str
    normalized_object_key: str
    original_size_bytes: int
    original_sha256: str
    solve_mode: str
    target_profile_id: str
    target_snapshot: dict[str, Any]


class LostLeaseError(RuntimeError):
    pass


def claim_candidate_statement(now: datetime) -> Select[tuple[Any, ...]]:
    due = or_(
        and_(build_jobs.c.status == "READY", build_jobs.c.available_at <= now),
        and_(
            build_jobs.c.status == "RUNNING",
            build_jobs.c.lease_expires_at <= now,
        ),
    )
    return (
        select(build_jobs)
        .where(due)
        .order_by(
            case((build_jobs.c.status == "READY", 0), else_=1),
            build_jobs.c.priority_no,
            build_jobs.c.created_at,
            build_jobs.c.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )


class JobRepository:
    def __init__(
        self,
        engine: Engine,
        *,
        lease_seconds: int,
        clock: Callable[[], datetime] = datetime.utcnow,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.engine = engine
        self.lease_seconds = lease_seconds
        self._clock = clock
        self._uuid_factory = uuid_factory

    def claim_next(self, worker_id: str) -> JobLease | None:
        if not worker_id or len(worker_id) > 100:
            raise ValueError("worker_id must contain at most 100 characters")
        while True:
            now = self._clock()
            with self.engine.begin() as connection:
                row = (
                    connection.execute(claim_candidate_statement(now))
                    .mappings()
                    .first()
                )
                if row is None:
                    return None
                if row["attempts"] >= row["max_attempts"]:
                    self._terminalize_exhausted(connection, row, now)
                    continue

                execution_id = str(self._uuid_factory())
                reclaimed = row["status"] == "RUNNING"
                previous_execution_id = (
                    str(row["execution_id"])
                    if reclaimed and row["execution_id"] is not None
                    else None
                )
                attempts = int(row["attempts"]) + 1
                result = connection.execute(
                    update(build_jobs)
                    .where(
                        build_jobs.c.id == row["id"],
                        build_jobs.c.version_no == row["version_no"],
                    )
                    .values(
                        status="RUNNING",
                        attempts=attempts,
                        lease_owner=worker_id,
                        execution_id=execution_id,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                        heartbeat_at=now,
                        started_at=func.coalesce(build_jobs.c.started_at, now),
                        finished_at=None,
                        version_no=build_jobs.c.version_no + 1,
                    )
                )
                if result.rowcount != 1:
                    continue
                payload = row["payload_json"]
                if not isinstance(payload, str):
                    try:
                        payload = bounded_database_payload_json(payload)
                    except ValueError:
                        # Keep the lease so the pipeline can terminalize malformed input.
                        payload = "{}"
                return JobLease(
                    id=str(row["id"]),
                    job_type=str(row["job_type"]),
                    subject_id=str(row["subject_id"]),
                    payload_version=int(row["payload_version"]),
                    payload_json=payload,
                    worker_id=worker_id,
                    execution_id=execution_id,
                    attempts=attempts,
                    max_attempts=int(row["max_attempts"]),
                    reclaimed=reclaimed,
                    previous_execution_id=previous_execution_id,
                )

    def heartbeat(self, lease: JobLease) -> bool:
        now = self._clock()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(build_jobs)
                .where(*self._ownership(lease, now))
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    version_no=build_jobs.c.version_no + 1,
                )
            )
            return result.rowcount == 1

    def complete(self, lease: JobLease) -> bool:
        now = self._clock()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(build_jobs)
                .where(*self._ownership(lease, now))
                .values(
                    status="COMPLETED",
                    lease_owner=None,
                    execution_id=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    finished_at=now,
                    version_no=build_jobs.c.version_no + 1,
                )
            )
            return result.rowcount == 1

    def retry_or_fail(
        self,
        lease: JobLease,
        error: str,
        *,
        retryable: bool,
        retry_delay_seconds: int = 5,
    ) -> bool:
        now = self._clock()
        bounded_error = sanitize_text(error, limit=2000)
        with self.engine.begin() as connection:
            if retryable and lease.attempts < lease.max_attempts:
                values = {
                    "status": "READY",
                    "available_at": now + timedelta(seconds=retry_delay_seconds),
                    "lease_owner": None,
                    "execution_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "last_error": bounded_error,
                    "version_no": build_jobs.c.version_no + 1,
                }
                result = connection.execute(
                    update(build_jobs)
                    .where(*self._ownership(lease, now))
                    .values(**values)
                )
                if result.rowcount == 1:
                    self._reset_subject_for_retry(connection, lease)
                return result.rowcount == 1

            result = connection.execute(
                update(build_jobs)
                .where(*self._ownership(lease, now))
                .values(
                    status="FAILED",
                    lease_owner=None,
                    execution_id=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    last_error=bounded_error,
                    finished_at=now,
                    version_no=build_jobs.c.version_no + 1,
                )
            )
            if result.rowcount == 1:
                self._terminalize_subject(
                    connection,
                    lease.job_type,
                    lease.subject_id,
                    now,
                    "JOB_FAILED",
                    bounded_error,
                )
                return True
            return False

    def fail_terminal(self, lease: JobLease, code: str, error: str) -> bool:
        now = self._clock()
        message = sanitize_text(error, limit=2000)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(build_jobs)
                .where(*self._ownership(lease, now))
                .values(
                    status="FAILED",
                    lease_owner=None,
                    execution_id=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    last_error=message,
                    finished_at=now,
                    version_no=build_jobs.c.version_no + 1,
                )
            )
            if result.rowcount != 1:
                return False
            self._terminalize_subject(
                connection, lease.job_type, lease.subject_id, now, code, message
            )
            return True

    def claim_parse_subject(self, lease: JobLease) -> ParseSubject | None:
        now = self._clock()
        with self.engine.begin() as connection:
            if not self._owns(connection, lease, now):
                return None
            row = (
                connection.execute(
                    select(requirement_files)
                    .where(requirement_files.c.id == lease.subject_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            expected_status = "PARSING" if lease.reclaimed else "PENDING"
            if (
                row is None
                or row["parse_status"] != expected_status
                or (lease.reclaimed and lease.previous_execution_id is None)
            ):
                self._fail_subject_claim(
                    connection, lease, now, "parse subject cannot be claimed"
                )
                return None
            result = connection.execute(
                update(requirement_files)
                .where(
                    requirement_files.c.id == lease.subject_id,
                    requirement_files.c.version_no == row["version_no"],
                    requirement_files.c.parse_status == expected_status,
                )
                .values(
                    parse_status="PARSING",
                    parse_error=None,
                    version_no=requirement_files.c.version_no + 1,
                )
            )
            if result.rowcount != 1:
                self._fail_subject_claim(
                    connection, lease, now, "parse subject claim raced"
                )
                return None
            return ParseSubject(
                id=str(row["id"]),
                user_id=str(row["user_id"]),
                original_object_key=str(row["original_object_key"]),
                normalized_object_key=str(row["normalized_object_key"]),
                size_bytes=int(row["size_bytes"]),
                sha256=str(row["sha256"]),
            )

    def claim_requirement_file(self, lease: JobLease) -> bool:
        return self.claim_parse_subject(lease) is not None

    def complete_parse(
        self,
        lease: JobLease,
        parsed: ParsedRequirements,
        normalized_object_key: str,
    ) -> bool:
        now = self._clock()
        with self.engine.begin() as connection:
            if not self._owns(connection, lease, now):
                return False
            locked = connection.execute(
                select(requirement_files.c.version_no)
                .where(
                    requirement_files.c.id == lease.subject_id,
                    requirement_files.c.parse_status == "PARSING",
                )
                .with_for_update()
            ).first()
            if locked is None:
                return False
            connection.execute(
                delete(requirement_items).where(
                    requirement_items.c.requirement_file_id == lease.subject_id
                )
            )
            for item in parsed.items:
                connection.execute(
                    insert(requirement_items).values(
                        id=str(self._uuid_factory()),
                        requirement_file_id=lease.subject_id,
                        line_no=item.line_no,
                        normalized_name=item.name,
                        extras_json=list(item.extras),
                        specifier=item.specifier,
                        marker_text=item.marker,
                        original_text=_bounded(item.original_text, 2000),
                        supported=True,
                    )
                )
            result = connection.execute(
                update(requirement_files)
                .where(
                    requirement_files.c.id == lease.subject_id,
                    requirement_files.c.parse_status == "PARSING",
                    requirement_files.c.version_no == locked.version_no,
                )
                .values(
                    detected_encoding=parsed.encoding,
                    normalized_object_key=normalized_object_key,
                    parse_status="PARSED",
                    parse_error=None,
                    version_no=requirement_files.c.version_no + 1,
                )
            )
            if result.rowcount != 1 or not self._complete_job(connection, lease, now):
                raise LostLeaseError("parse publication lost job ownership")
            return True

    def claim_build_subject(self, lease: JobLease) -> BuildSubject | None:
        now = self._clock()
        with self.engine.begin() as connection:
            if not self._owns(connection, lease, now):
                return None
            row = (
                connection.execute(
                    select(
                        build_tasks.c.id,
                        build_tasks.c.user_id,
                        build_tasks.c.requirement_file_id,
                        build_tasks.c.target_profile_id,
                        build_tasks.c.execution_id,
                        build_tasks.c.status,
                        build_tasks.c.solve_mode,
                        build_tasks.c.target_snapshot,
                        build_tasks.c.deleted_at,
                        build_tasks.c.version_no,
                        requirement_files.c.user_id.label("file_user_id"),
                        requirement_files.c.original_object_key,
                        requirement_files.c.normalized_object_key,
                        requirement_files.c.size_bytes,
                        requirement_files.c.sha256,
                        requirement_files.c.parse_status,
                    )
                    .select_from(
                        build_tasks.join(
                            requirement_files,
                            requirement_files.c.id
                            == build_tasks.c.requirement_file_id,
                        )
                    )
                    .where(build_tasks.c.id == lease.subject_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            nonterminal = {
                "QUEUED",
                "RESOLVING",
                "DOWNLOADING",
                "VALIDATING",
                "PACKAGING",
            }
            initial = (
                not lease.reclaimed
                and row is not None
                and row["status"] == "QUEUED"
                and row["execution_id"] is None
            )
            takeover = (
                lease.reclaimed
                and lease.previous_execution_id is not None
                and row is not None
                and row["status"] in nonterminal
                and row["execution_id"] == lease.previous_execution_id
            )
            valid_binding = (
                row is not None
                and row["deleted_at"] is None
                and row["parse_status"] == "PARSED"
                and row["normalized_object_key"] is not None
                and row["user_id"] == row["file_user_id"]
                and isinstance(row["target_snapshot"], (dict, str))
            )
            if not valid_binding or not (initial or takeover):
                self._fail_subject_claim(
                    connection, lease, now, "build subject cannot be claimed"
                )
                return None
            assert row is not None
            result = connection.execute(
                update(build_tasks)
                .where(
                    build_tasks.c.id == lease.subject_id,
                    build_tasks.c.version_no == row["version_no"],
                    build_tasks.c.status == row["status"],
                    (
                        build_tasks.c.execution_id
                        == lease.previous_execution_id
                        if takeover
                        else build_tasks.c.execution_id.is_(None)
                    ),
                )
                .values(
                    execution_id=lease.execution_id,
                    status="RESOLVING",
                    progress=5,
                    current_stage="PARSING",
                    started_at=func.coalesce(build_tasks.c.started_at, now),
                    failure_code=None,
                    failure_message=None,
                    version_no=build_tasks.c.version_no + 1,
                )
            )
            if result.rowcount != 1:
                self._fail_subject_claim(
                    connection, lease, now, "build subject claim raced"
                )
                return None
            snapshot = row["target_snapshot"]
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("profileId") != row["target_profile_id"]
            ):
                self._fail_subject_claim(
                    connection, lease, now, "build target snapshot is malformed"
                )
                return None
            return BuildSubject(
                id=str(row["id"]),
                user_id=str(row["user_id"]),
                requirement_file_id=str(row["requirement_file_id"]),
                original_object_key=str(row["original_object_key"]),
                normalized_object_key=str(row["normalized_object_key"]),
                original_size_bytes=int(row["size_bytes"]),
                original_sha256=str(row["sha256"]),
                solve_mode=str(row["solve_mode"]),
                target_profile_id=str(row["target_profile_id"]),
                target_snapshot=snapshot,
            )

    def claim_build(self, lease: JobLease) -> bool:
        return self.claim_build_subject(lease) is not None

    def is_cancel_requested(self, lease: JobLease) -> bool:
        now = self._clock()
        with self.engine.connect() as connection:
            if not self._owns(connection, lease, now):
                raise LostLeaseError("job lease is no longer owned")
            value = connection.scalar(
                select(build_tasks.c.cancel_requested).where(
                    build_tasks.c.id == lease.subject_id,
                    build_tasks.c.execution_id == lease.execution_id,
                )
            )
            if value is None:
                raise LostLeaseError("build execution is no longer owned")
            return bool(value)

    def cancel_build(self, lease: JobLease) -> bool:
        now = self._clock()
        with self.engine.begin() as connection:
            if not self._owns(connection, lease, now):
                return False
            result = connection.execute(
                update(build_tasks)
                .where(
                    build_tasks.c.id == lease.subject_id,
                    build_tasks.c.execution_id == lease.execution_id,
                    build_tasks.c.cancel_requested.is_(True),
                )
                .values(
                    status="CANCELLED",
                    progress=100,
                    current_stage="CANCELLED",
                    finished_at=now,
                    version_no=build_tasks.c.version_no + 1,
                )
            )
            if result.rowcount != 1:
                return False
            job = connection.execute(
                update(build_jobs)
                .where(*self._ownership(lease, now))
                .values(
                    status="CANCELLED",
                    lease_owner=None,
                    execution_id=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    finished_at=now,
                    version_no=build_jobs.c.version_no + 1,
                )
            )
            if job.rowcount != 1:
                raise LostLeaseError("cancellation lost job ownership")
            return True

    def advance_build(
        self,
        lease: JobLease,
        status: str,
        progress: int,
        stage: str,
        message: str,
    ) -> None:
        now = self._clock()
        with self.engine.begin() as connection:
            heartbeat = connection.execute(
                update(build_jobs)
                .where(*self._ownership(lease, now))
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    version_no=build_jobs.c.version_no + 1,
                )
            )
            if heartbeat.rowcount != 1:
                raise LostLeaseError("could not renew job lease")
            task = connection.execute(
                update(build_tasks)
                .where(
                    build_tasks.c.id == lease.subject_id,
                    build_tasks.c.execution_id == lease.execution_id,
                    build_tasks.c.status.not_in(
                        ("SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED")
                    ),
                )
                .values(
                    status=status,
                    progress=progress,
                    current_stage=stage,
                    version_no=build_tasks.c.version_no + 1,
                )
            )
            if task.rowcount != 1:
                raise LostLeaseError("build execution is no longer owned")
            self._append_log(connection, lease, stage, "INFO", message, None, now)

    def append_log(
        self,
        lease: JobLease,
        stage: str,
        level: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> int:
        now = self._clock()
        with self.engine.begin() as connection:
            if not self._owns(connection, lease, now):
                raise LostLeaseError("job lease is no longer owned")
            return self._append_log(
                connection, lease, stage, level, message, context, now
            )

    def persist_resolution(self, lease: JobLease, resolution: ResolutionResult) -> None:
        changes = {change.package: change for change in resolution.changes}
        audit = _resolution_audit(resolution)
        now = self._clock()
        with self.engine.begin() as connection:
            if not self._owns(connection, lease, now):
                raise LostLeaseError("job lease is no longer owned")
            connection.execute(
                delete(resolved_packages).where(
                    resolved_packages.c.build_task_id == lease.subject_id
                )
            )
            for package in resolution.packages:
                change = changes.get(package.name)
                connection.execute(
                    insert(resolved_packages).values(
                        id=str(self._uuid_factory()),
                        build_task_id=lease.subject_id,
                        normalized_name=package.name,
                        final_version=str(package.version),
                        dependency_type="DIRECT" if package.requested else "TRANSITIVE",
                        original_constraint=(
                            change.original_constraint if change else None
                        ),
                        strict_version=(
                            str(change.original_version)
                            if change and change.original_version is not None
                            else None
                        ),
                        change_direction=(
                            change.kind.value
                            if change is not None
                            else VersionChangeKind.UNCHANGED.value
                        ),
                        change_reason=(change.reason if change else None),
                        attempts_json=audit,
                        wheel_filename=package.wheel_filename,
                        package_source_code=(
                            resolution.source.value if resolution.source else None
                        ),
                        wheel_status="RESOLVED",
                    )
                )

    def persist_package_results(
        self,
        lease: JobLease,
        resolution: ResolutionResult,
        wheels: tuple[DownloadedWheel, ...],
        validation: StaticValidationReport,
        download_failures: dict[str, str],
    ) -> None:
        now = self._clock()
        observed = {wheel.package: wheel for wheel in wheels}
        issues: dict[str, list[str]] = {}
        for issue in validation.issues:
            issues.setdefault(issue.package, []).append(
                sanitize_text(f"{issue.code.value}: {issue.detail}", limit=2000)
            )
        with self.engine.begin() as connection:
            if not self._owns(connection, lease, now):
                raise LostLeaseError("job lease is no longer owned")
            for package in resolution.packages:
                wheel = observed.get(package.name)
                package_issues = issues.get(package.name, [])
                if wheel is None:
                    status = "MISSING"
                    error = download_failures.get(package.name) or (
                        "; ".join(package_issues) if package_issues else "Wheel is missing"
                    )
                elif package_issues:
                    status = "STATIC_FAILED"
                    error = "; ".join(package_issues)
                else:
                    status = "STATIC_PASSED"
                    error = None
                result = connection.execute(
                    update(resolved_packages)
                    .where(
                        resolved_packages.c.build_task_id == lease.subject_id,
                        resolved_packages.c.normalized_name == package.name,
                    )
                    .values(
                        wheel_filename=(wheel.filename if wheel else package.wheel_filename),
                        wheel_tags=(
                            sorted(str(tag) for tag in wheel.tags) if wheel else None
                        ),
                        package_source_code=(
                            wheel.source.value
                            if wheel is not None
                            else resolution.source.value
                            if resolution.source is not None
                            else None
                        ),
                        sha256=(wheel.sha256 if wheel else None),
                        wheel_status=status,
                        error_message=(
                            sanitize_text(error, limit=2000) if error else None
                        ),
                    )
                )
                if result.rowcount != 1:
                    raise LostLeaseError("resolved package audit row is unavailable")

    def publish_artifact_terminal(
        self,
        lease: JobLease,
        artifact_id: str,
        published: PublishedObject,
        filename: str,
        status: str,
        expires_at: datetime,
    ) -> str | None:
        now = self._clock()
        with self.engine.begin() as connection:
            if not self._owns(connection, lease, now):
                return None
            task = connection.execute(
                select(build_tasks.c.cancel_requested)
                .where(
                    build_tasks.c.id == lease.subject_id,
                    build_tasks.c.execution_id == lease.execution_id,
                )
                .with_for_update()
            ).first()
            if task is None or task.cancel_requested:
                return None
            connection.execute(
                insert(artifacts).values(
                    id=artifact_id,
                    build_task_id=lease.subject_id,
                    artifact_type="OFFLINE_WHEEL_BUNDLE",
                    filename=_bounded(filename, 255),
                    object_key=published.object_key,
                    size_bytes=published.size_bytes,
                    sha256=published.sha256,
                    build_status=status,
                    validation_type="STATIC",
                    expires_at=expires_at,
                    download_count=0,
                    created_at=now,
                    version_no=0,
                )
            )
            terminal = connection.execute(
                update(build_tasks)
                .where(
                    build_tasks.c.id == lease.subject_id,
                    build_tasks.c.execution_id == lease.execution_id,
                )
                .values(
                    status=status,
                    progress=100,
                    current_stage="COMPLETED",
                    finished_at=now,
                    version_no=build_tasks.c.version_no + 1,
                )
            )
            if terminal.rowcount != 1 or not self._complete_job(connection, lease, now):
                raise LostLeaseError("artifact publication lost execution ownership")
            return artifact_id

    def requirement_original_key(self, requirement_file_id: str) -> str | None:
        with self.engine.connect() as connection:
            value = connection.scalar(
                select(requirement_files.c.original_object_key).where(
                    requirement_files.c.id == requirement_file_id
                )
            )
            return str(value) if value is not None else None

    def maintenance_snapshot(self) -> MaintenanceSnapshot:
        now = self._clock()
        with self.engine.connect() as connection:
            active = connection.scalars(
                select(build_jobs.c.execution_id).where(
                    build_jobs.c.status == "RUNNING",
                    build_jobs.c.execution_id.is_not(None),
                    build_jobs.c.lease_expires_at > now,
                )
            ).all()
            referenced = connection.scalars(
                select(artifacts.c.object_key).where(artifacts.c.cleaned_at.is_(None))
            ).all()
        return MaintenanceSnapshot(
            frozenset(str(value) for value in active if value is not None),
            frozenset(str(value) for value in referenced),
        )

    def _append_log(
        self,
        connection: Connection,
        lease: JobLease,
        stage: str,
        level: str,
        message: str,
        context: dict[str, Any] | None,
        now: datetime,
    ) -> int:
        locked = connection.execute(
            select(build_tasks.c.id)
            .where(
                build_tasks.c.id == lease.subject_id,
                build_tasks.c.execution_id == lease.execution_id,
            )
            .with_for_update()
        ).first()
        if locked is None:
            raise LostLeaseError("build execution is no longer owned")
        sequence = (
            int(
                connection.scalar(
                    select(func.coalesce(func.max(build_logs.c.sequence_no), 0)).where(
                        build_logs.c.build_task_id == lease.subject_id
                    )
                )
                or 0
            )
            + 1
        )
        safe_context: dict[str, Any] | None = None
        if context is not None:
            encoded = json.dumps(
                sanitize_structure(context), ensure_ascii=True, default=str
            )
            safe_context = (
                json.loads(encoded[:8000])
                if len(encoded) <= 8000
                else {"truncated": True}
            )
        connection.execute(
            insert(build_logs).values(
                id=str(self._uuid_factory()),
                build_task_id=lease.subject_id,
                sequence_no=sequence,
                stage=_bounded(stage, 50),
                level=_bounded(level, 20),
                message=sanitize_text(message, limit=4000),
                context_json=safe_context,
                created_at=now,
            )
        )
        return sequence

    def _owns(
        self,
        connection: Connection,
        lease: JobLease,
        now: datetime | None = None,
    ) -> bool:
        observed_at = self._clock() if now is None else now
        return connection.scalar(
            self._owned_job_statement(lease, observed_at)
        ) is not None

    @staticmethod
    def _owned_job_statement(
        lease: JobLease, observed_at: datetime
    ) -> Select[tuple[Any, ...]]:
        return (
            select(build_jobs.c.id)
            .where(*JobRepository._ownership(lease, observed_at))
            .with_for_update()
        )

    def _complete_job(
        self, connection: Connection, lease: JobLease, now: datetime
    ) -> bool:
        result = connection.execute(
            update(build_jobs)
            .where(*self._ownership(lease, now))
            .values(
                status="COMPLETED",
                lease_owner=None,
                execution_id=None,
                lease_expires_at=None,
                heartbeat_at=None,
                finished_at=now,
                version_no=build_jobs.c.version_no + 1,
            )
        )
        return result.rowcount == 1

    def _fail_subject_claim(
        self,
        connection: Connection,
        lease: JobLease,
        now: datetime,
        message: str,
    ) -> None:
        result = connection.execute(
            update(build_jobs)
            .where(*self._ownership(lease, now))
            .values(
                status="FAILED",
                lease_owner=None,
                execution_id=None,
                lease_expires_at=None,
                heartbeat_at=None,
                last_error=sanitize_text(message, limit=2000),
                finished_at=now,
                version_no=build_jobs.c.version_no + 1,
            )
        )
        if result.rowcount == 1:
            self._terminalize_subject(
                connection,
                lease.job_type,
                lease.subject_id,
                now,
                "SUBJECT_CLAIM_FAILED",
                message,
            )

    @staticmethod
    def _reset_subject_for_retry(connection: Connection, lease: JobLease) -> None:
        if lease.job_type == "BUILD":
            connection.execute(
                update(build_tasks)
                .where(
                    build_tasks.c.id == lease.subject_id,
                    build_tasks.c.execution_id == lease.execution_id,
                )
                .values(
                    execution_id=None,
                    status="QUEUED",
                    progress=0,
                    current_stage=None,
                    version_no=build_tasks.c.version_no + 1,
                )
            )
        elif lease.job_type == "REQUIREMENT_PARSE":
            connection.execute(
                update(requirement_files)
                .where(
                    requirement_files.c.id == lease.subject_id,
                    requirement_files.c.parse_status == "PARSING",
                )
                .values(
                    parse_status="PENDING",
                    version_no=requirement_files.c.version_no + 1,
                )
            )

    @staticmethod
    def _ownership(lease: JobLease, now: datetime) -> tuple[Any, ...]:
        return (
            build_jobs.c.id == lease.id,
            build_jobs.c.status == "RUNNING",
            build_jobs.c.lease_owner == lease.worker_id,
            build_jobs.c.execution_id == lease.execution_id,
            build_jobs.c.lease_expires_at.is_not(None),
            build_jobs.c.lease_expires_at > now,
        )

    def _terminalize_exhausted(
        self, connection: Connection, row: Any, now: datetime
    ) -> None:
        message = "job attempt limit exhausted"
        result = connection.execute(
            update(build_jobs)
            .where(
                build_jobs.c.id == row["id"],
                build_jobs.c.version_no == row["version_no"],
            )
            .values(
                status="FAILED",
                lease_owner=None,
                execution_id=None,
                lease_expires_at=None,
                heartbeat_at=None,
                last_error=message,
                finished_at=now,
                version_no=build_jobs.c.version_no + 1,
            )
        )
        if result.rowcount == 1:
            self._terminalize_subject(
                connection,
                str(row["job_type"]),
                str(row["subject_id"]),
                now,
                "JOB_ATTEMPTS_EXHAUSTED",
                message,
            )

    @staticmethod
    def _terminalize_subject(
        connection: Connection,
        job_type: str,
        subject_id: str,
        now: datetime,
        failure_code: str,
        message: str,
    ) -> None:
        if job_type == "REQUIREMENT_PARSE":
            connection.execute(
                update(requirement_files)
                .where(
                    requirement_files.c.id == subject_id,
                    requirement_files.c.parse_status.not_in(("PARSED", "FAILED")),
                )
                .values(
                    parse_status="FAILED",
                    parse_error=message,
                    version_no=requirement_files.c.version_no + 1,
                )
            )
        elif job_type == "BUILD":
            connection.execute(
                update(build_tasks)
                .where(
                    build_tasks.c.id == subject_id,
                    build_tasks.c.status.not_in(
                        ("SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED")
                    ),
                )
                .values(
                    status="FAILED",
                    failure_code=failure_code,
                    failure_message=message,
                    finished_at=now,
                    version_no=build_tasks.c.version_no + 1,
                )
            )


def _bounded(value: str, limit: int) -> str:
    sanitized = value.replace("\x00", "?")
    return sanitized[:limit]


def _resolution_audit(resolution: ResolutionResult) -> list[dict[str, Any]]:
    attempts = [
        {
            "source": attempt.source.value,
            "failure": attempt.failure.value if attempt.failure else None,
            "reason": sanitize_text(attempt.reason, limit=1000),
            "selections": [
                {"package": item.package, "version": str(item.version)}
                for item in attempt.selections[:500]
            ],
        }
        for attempt in resolution.attempts[:100]
    ]
    rejections = [
        {
            "package": item.package,
            "version": item.version,
            "source": item.source.value,
            "code": item.code.value,
            "reason": sanitize_text(item.reason, limit=1000),
        }
        for item in resolution.rejections[:2000]
    ]
    return [{"attempts": attempts, "rejections": rejections}]
