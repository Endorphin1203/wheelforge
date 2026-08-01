from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.pool import StaticPool

from wheelforge_worker.artifact import ArtifactBuildContext, BuiltArtifact
from wheelforge_worker.contracts import BuildStatus
from wheelforge_worker.resolver import ResolutionResult
from wheelforge_worker.validation import StaticValidationReport
import wheelforge_worker.jobs.pipeline as pipeline_module
from wheelforge_worker.download import DownloadedWheel
from wheelforge_worker.jobs.pipeline import (
    BuildValidation,
    DefaultBuildStages,
    JobPipeline,
    PipelineResult,
)
from wheelforge_worker.jobs.repository import (
    JobLease,
    JobRepository,
    artifacts,
    build_jobs,
    build_logs,
    build_tasks,
    metadata,
    requirement_files,
    requirement_items,
)
from wheelforge_worker.jobs.storage import RootedLocalStorage, WorkspaceManager
from wheelforge_worker.target import TargetProfile
from wheelforge_worker.validation import WheelArchiveValidationError


NOW = datetime(2026, 8, 1, 8, 0, 0)
TASK_ID = "20000000-0000-4000-8000-000000000001"
FILE_ID = "30000000-0000-4000-8000-000000000001"
PROFILE_ID = "40000000-0000-4000-8000-000000000001"
JOB_ID = "10000000-0000-4000-8000-000000000001"


def _target() -> dict[str, object]:
    return {
        "profileId": PROFILE_ID,
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
    }


def _wire(job_type: str, subject_id: str, payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "jobType": job_type,
            "subjectId": subject_id,
            "createdAt": "2026-08-01T08:00:00Z",
            "payload": payload,
        }
    )


class RecordingStages:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(
        self, parsed: object, target: object, workspace: Path
    ) -> ResolutionResult:
        self.calls.append("resolve")
        return ResolutionResult("1", ())

    def download(
        self,
        resolution: ResolutionResult,
        target: object,
        workspace: Path,
        cancel: object,
    ) -> tuple[object, ...]:
        self.calls.append("download")
        return ()

    def validate(
        self,
        resolution: ResolutionResult,
        wheels: tuple[object, ...],
        target: object,
    ) -> BuildValidation:
        self.calls.append("validate")
        return BuildValidation((), StaticValidationReport((), True))

    def package(
        self, context: ArtifactBuildContext, output_directory: Path
    ) -> BuiltArtifact:
        self.calls.append("package")
        path = output_directory / "result.zip"
        content = b"verified zip bytes"
        path.write_bytes(content)
        return BuiltArtifact(path, hashlib.sha256(content).hexdigest(), {})


@dataclass
class CleanupSnapshot:
    cleanup_calls: int = 0
    fail_cleanup: bool = False

    def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self.fail_cleanup:
            raise RuntimeError("secondary cleanup failure")


@dataclass
class SnapshotReport:
    snapshot: CleanupSnapshot


def test_default_validation_cleans_prior_snapshots_when_later_wheel_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = CleanupSnapshot(fail_cleanup=True)
    second = CleanupSnapshot()
    calls = 0

    def validate_archive(*_args: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SnapshotReport(first)
        if calls == 2:
            return SnapshotReport(second)
        raise WheelArchiveValidationError("third Wheel is invalid")

    monkeypatch.setattr(pipeline_module, "validate_wheel_archive", validate_archive)
    wheels = cast(
        tuple[DownloadedWheel, ...],
        (
            SimpleNamespace(path=Path("one.whl")),
            SimpleNamespace(path=Path("two.whl")),
            SimpleNamespace(path=Path("three.whl")),
        ),
    )

    with pytest.raises(WheelArchiveValidationError, match="third Wheel is invalid"):
        DefaultBuildStages().validate(
            ResolutionResult("1", ()),
            wheels,
            cast(TargetProfile, object()),
        )

    assert first.cleanup_calls == 1
    assert second.cleanup_calls == 1


def _environment(
    tmp_path: Path,
) -> tuple[JobRepository, RootedLocalStorage, WorkspaceManager]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir(mode=0o700)
    workspace_root.mkdir(mode=0o700)
    ids = (UUID(f"50000000-0000-4000-8000-{value:012d}") for value in count(1))
    repository = JobRepository(
        engine,
        lease_seconds=60,
        clock=lambda: NOW,
        uuid_factory=lambda: next(ids),
    )
    return repository, RootedLocalStorage(data_root), WorkspaceManager(workspace_root)


def _seed_build(repository: JobRepository, payload_json: str | None = None) -> JobLease:
    wire = payload_json or _wire(
        "BUILD",
        TASK_ID,
        {
            "requirementFileId": FILE_ID,
            "normalizedObjectKey": "requirements/normalized/input.txt",
            "solveMode": "COMPATIBLE",
            "targetSnapshot": _target(),
        },
    )
    with repository.engine.begin() as connection:
        connection.execute(
            insert(build_tasks).values(
                id=TASK_ID,
                requirement_file_id=FILE_ID,
                target_profile_id=PROFILE_ID,
                status="QUEUED",
                progress=0,
                solve_mode="COMPATIBLE",
                target_snapshot=_target(),
                cancel_requested=False,
                created_at=NOW,
                version_no=0,
            )
        )
        connection.execute(
            insert(build_jobs).values(
                id=JOB_ID,
                job_type="BUILD",
                payload_version=1,
                subject_id=TASK_ID,
                payload_json=wire,
                status="READY",
                priority_no=100,
                available_at=NOW,
                attempts=0,
                max_attempts=3,
                created_at=NOW,
                version_no=0,
            )
        )
    lease = repository.claim_next("worker-a")
    assert lease is not None
    return lease


def test_requirement_parse_publishes_normalized_text_and_items(tmp_path: Path) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    original = storage.publish_bytes(
        "requirements/original/input.txt", b"NumPy==1.26.4\r\n"
    )
    with repository.engine.begin() as connection:
        connection.execute(
            insert(requirement_files).values(
                id=FILE_ID,
                original_object_key=original.object_key,
                normalized_object_key="requirements/normalized/input.txt",
                parse_status="PENDING",
                size_bytes=original.size_bytes,
                sha256=original.sha256,
                created_at=NOW,
                version_no=0,
            )
        )
        connection.execute(
            insert(build_jobs).values(
                id=JOB_ID,
                job_type="REQUIREMENT_PARSE",
                payload_version=1,
                subject_id=FILE_ID,
                payload_json=_wire(
                    "REQUIREMENT_PARSE",
                    FILE_ID,
                    {
                        "originalObjectKey": original.object_key,
                        "normalizedObjectKey": "requirements/normalized/input.txt",
                    },
                ),
                status="READY",
                priority_no=100,
                available_at=NOW,
                attempts=0,
                max_attempts=3,
                created_at=NOW,
                version_no=0,
            )
        )
    lease = repository.claim_next("worker-a")
    assert lease is not None
    pipeline = JobPipeline(repository, storage, workspaces, RecordingStages())

    result = pipeline.run(lease)

    assert result == PipelineResult(BuildStatus.SUCCESS, None)
    assert storage.read_bytes("requirements/normalized/input.txt") == b"numpy==1.26.4\n"
    with repository.engine.connect() as connection:
        file_status = connection.execute(
            select(
                requirement_files.c.parse_status, requirement_files.c.detected_encoding
            )
        ).one()
        item = connection.execute(
            select(requirement_items.c.normalized_name, requirement_items.c.specifier)
        ).one()
        job_status = connection.scalar(select(build_jobs.c.status))
    assert file_status == ("PARSED", "utf-8")
    assert item == ("numpy", "==1.26.4")
    assert job_status == "COMPLETED"


def test_malformed_payload_terminally_fails_job_and_subject(tmp_path: Path) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    lease = _seed_build(repository, payload_json='{"schemaVersion": 1}')
    pipeline = JobPipeline(repository, storage, workspaces, RecordingStages())

    result = pipeline.run(lease)

    assert result.status is BuildStatus.FAILED
    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "FAILED"
        assert connection.scalar(select(build_tasks.c.status)) == "FAILED"


def test_successful_build_publishes_artifact_and_terminal_states(
    tmp_path: Path,
) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    storage.publish_bytes("requirements/normalized/input.txt", b"numpy==1.26.4\n")
    lease = _seed_build(repository)
    stages = RecordingStages()
    pipeline = JobPipeline(repository, storage, workspaces, stages)

    result = pipeline.run(lease)

    assert result.status is BuildStatus.SUCCESS
    assert result.artifact_id is not None
    assert stages.calls == ["resolve", "download", "validate", "package"]
    with repository.engine.connect() as connection:
        artifact = connection.execute(
            select(artifacts.c.object_key, artifacts.c.sha256)
        ).one()
        assert connection.scalar(select(build_tasks.c.status)) == "SUCCESS"
        assert connection.scalar(select(build_jobs.c.status)) == "COMPLETED"
    assert storage.read_bytes(artifact.object_key) == b"verified zip bytes"
    assert artifact.sha256 == hashlib.sha256(b"verified zip bytes").hexdigest()
    assert list(workspaces.root.iterdir()) == []


class ExpireOnHeartbeatRepository(JobRepository):
    def __init__(self, source: JobRepository, expired: Event) -> None:
        super().__init__(
            source.engine,
            lease_seconds=source.lease_seconds,
            clock=source._clock,
        )
        self.expired = expired

    def heartbeat(self, lease: JobLease) -> bool:
        self._clock = lambda: NOW + timedelta(seconds=61)
        result = super().heartbeat(lease)
        self.expired.set()
        return result


class WaitForLostLeaseStages(RecordingStages):
    def __init__(self, expired: Event) -> None:
        super().__init__()
        self.expired = expired

    def resolve(
        self, parsed: object, target: object, workspace: Path
    ) -> ResolutionResult:
        self.calls.append("resolve")
        assert self.expired.wait(1)
        return ResolutionResult("1", ())


def test_lost_heartbeat_stops_pipeline_before_terminal_publication(
    tmp_path: Path,
) -> None:
    source, storage, workspaces = _environment(tmp_path)
    storage.publish_bytes("requirements/normalized/input.txt", b"numpy==1.26.4\n")
    lease = _seed_build(source)
    expired = Event()
    repository = ExpireOnHeartbeatRepository(source, expired)
    stages = WaitForLostLeaseStages(expired)
    pipeline = JobPipeline(
        repository,
        storage,
        workspaces,
        stages,
        heartbeat_interval_seconds=1,
        heartbeat_wait=lambda _stop, _seconds: False,
    )

    result = pipeline.run(lease)

    assert result == PipelineResult(BuildStatus.FAILED, None)
    assert stages.calls == ["resolve"]
    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "RUNNING"
        assert connection.scalar(select(artifacts.c.id)) is None


class CancelAtRepository(JobRepository):
    def __init__(self, source: JobRepository, cancel_at: int) -> None:
        super().__init__(
            source.engine,
            lease_seconds=source.lease_seconds,
            clock=source._clock,
        )
        self.cancel_at = cancel_at
        self.checks = 0

    def is_cancel_requested(self, lease: JobLease) -> bool:
        self.checks += 1
        if self.checks != self.cancel_at:
            return False
        with self.engine.begin() as connection:
            connection.execute(
                update(build_tasks)
                .where(build_tasks.c.id == lease.subject_id)
                .values(cancel_requested=True)
            )
        return True


@pytest.mark.parametrize("cancel_at", range(1, 8))
def test_cancellation_at_each_publication_boundary_never_leaves_artifact(
    tmp_path: Path, cancel_at: int
) -> None:
    source, storage, workspaces = _environment(tmp_path)
    storage.publish_bytes("requirements/normalized/input.txt", b"numpy==1.26.4\n")
    lease = _seed_build(source)
    repository = CancelAtRepository(source, cancel_at)
    pipeline = JobPipeline(repository, storage, workspaces, RecordingStages())

    result = pipeline.run(lease)

    assert result == PipelineResult(BuildStatus.CANCELLED, None)
    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_tasks.c.status)) == "CANCELLED"
        assert connection.scalar(select(build_jobs.c.status)) == "CANCELLED"
        assert connection.scalar(select(artifacts.c.id)) is None
    artifact_directory = storage.root / "artifacts"
    assert not artifact_directory.exists() or not list(
        artifact_directory.rglob("*.zip")
    )
    assert list(workspaces.root.iterdir()) == []


class RollbackRepository(JobRepository):
    def publish_artifact_terminal(self, *args: object, **kwargs: object) -> str | None:
        raise RuntimeError("database rolled back")


def test_artifact_file_is_compensated_when_database_publication_rolls_back(
    tmp_path: Path,
) -> None:
    source, storage, workspaces = _environment(tmp_path)
    storage.publish_bytes("requirements/normalized/input.txt", b"numpy==1.26.4\n")
    lease = _seed_build(source)
    repository = RollbackRepository(
        source.engine,
        lease_seconds=source.lease_seconds,
        clock=source._clock,
    )
    pipeline = JobPipeline(repository, storage, workspaces, RecordingStages())

    result = pipeline.run(lease)

    assert result.status is BuildStatus.QUEUED
    artifact_directory = storage.root / "artifacts"
    assert not artifact_directory.exists() or not list(
        artifact_directory.rglob("*.zip")
    )


def test_build_logs_receive_strictly_increasing_sequences(tmp_path: Path) -> None:
    repository, _storage, _workspaces = _environment(tmp_path)
    lease = _seed_build(repository)
    assert repository.claim_build(lease)

    repository.append_log(lease, "RESOLVING", "INFO", "first", {"value": 1})
    repository.append_log(lease, "RESOLVING", "INFO", "second", {"value": 2})

    with repository.engine.connect() as connection:
        rows = connection.execute(
            select(build_logs.c.sequence_no, build_logs.c.message).order_by(
                build_logs.c.sequence_no
            )
        ).all()
    assert rows == [(1, "first"), (2, "second")]
