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
from packaging.tags import Tag
from packaging.version import Version
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.pool import StaticPool

from wheelforge_worker.artifact import ArtifactBuildContext, BuiltArtifact, ValidatedWheel
from wheelforge_worker.contracts import BuildStatus
from wheelforge_worker.resolver import ResolvedPackage, ResolutionResult
from wheelforge_worker.sources import PackageSource
from wheelforge_worker.validation import StaticValidationReport, validate_closure
import wheelforge_worker.jobs.pipeline as pipeline_module
from wheelforge_worker.download import DownloadedWheel, WheelDownloadError
from wheelforge_worker.jobs.pipeline import (
    BuildDownload,
    BuildValidation,
    DefaultBuildStages,
    JobPipeline,
    PackageFailure,
    PipelineResult,
)
from wheelforge_worker.jobs.repository import (
    MAX_RESOLVED_PACKAGES,
    JobLease,
    JobRepository,
    artifacts,
    build_jobs,
    build_logs,
    build_tasks,
    metadata,
    requirement_files,
    requirement_items,
    resolved_packages,
)
from wheelforge_worker.jobs.storage import RootedLocalStorage, WorkspaceManager
from wheelforge_worker.target import TargetProfile
from wheelforge_worker.validation import WheelArchiveValidationError


NOW = datetime(2026, 8, 1, 8, 0, 0)
TASK_ID = "20000000-0000-4000-8000-000000000001"
FILE_ID = "30000000-0000-4000-8000-000000000001"
PROFILE_ID = "40000000-0000-4000-8000-000000000001"
JOB_ID = "10000000-0000-4000-8000-000000000001"
BUILD_REQUIREMENTS = b"numpy==1.26.4\n"


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
    ) -> BuildDownload:
        self.calls.append("download")
        return BuildDownload((), ())

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


def _resolved_package(name: str, version: str) -> ResolvedPackage:
    filename = f"{name}-{version}-py3-none-any.whl"
    return ResolvedPackage(
        name=name,
        version=Version(version),
        requested=True,
        artifact_url=f"https://files.pythonhosted.org/{filename}",
        wheel_filename=filename,
        requires_dist=(),
        requires_python=">=3.9",
        archive_hashes=(),
    )


def test_default_download_retains_successes_when_later_package_is_missing(
    tmp_path: Path,
) -> None:
    first = _resolved_package("alpha", "1.0")
    second = _resolved_package("beta", "2.0")

    class PartialDownloader:
        def download_one(
            self,
            package: ResolvedPackage,
            target: object,
            destination: Path,
            cancel: object,
        ) -> DownloadedWheel:
            if package.name == "beta":
                raise WheelDownloadError("no target Wheel")
            destination.mkdir()
            path = destination / package.wheel_filename
            path.write_bytes(b"wheel")
            return DownloadedWheel(
                package=package.name,
                version=package.version,
                filename=package.wheel_filename,
                path=path,
                source=PackageSource.PYPI,
                byte_size=5,
                sha256=hashlib.sha256(b"wheel").hexdigest(),
                tags=frozenset({Tag("py3", "none", "any")}),
            )

    work = tmp_path / "work"
    work.mkdir()
    stages = DefaultBuildStages(
        downloader_factory=lambda _root: PartialDownloader()
    )

    result = stages.download(
        ResolutionResult("1", (first, second)),
        cast(TargetProfile, object()),
        work,
        lambda: False,
    )

    assert [wheel.package for wheel in result.wheels] == ["alpha"]
    assert len(result.failures) == 1
    assert result.failures[0].package == "beta"
    assert result.failures[0].error == "no target Wheel"


def test_default_validation_retains_valid_wheel_and_reports_missing_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    present = _resolved_package("alpha", "1.0")
    missing = _resolved_package("beta", "2.0")
    path = tmp_path / present.wheel_filename
    path.write_bytes(b"validated by adapter stub")
    wheel = DownloadedWheel(
        package=present.name,
        version=present.version,
        filename=present.wheel_filename,
        path=path,
        source=PackageSource.PYPI,
        byte_size=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        tags=frozenset({Tag("py3", "none", "any")}),
    )
    monkeypatch.setattr(
        pipeline_module,
        "validate_wheel_archive",
        lambda *_args: SimpleNamespace(snapshot=None),
    )

    result = DefaultBuildStages().validate(
        ResolutionResult("1", (present, missing)),
        (wheel,),
        TargetProfile.model_validate(_target()),
    )

    assert len(result.wheels) == 1
    assert result.report.complete is False
    assert [(issue.package, issue.code.value) for issue in result.report.issues] == [
        ("beta", "WHEEL_MISSING")
    ]


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
            insert(requirement_files).values(
                id=FILE_ID,
                user_id="70000000-0000-4000-8000-000000000001",
                original_name="requirements.txt",
                size_bytes=len(BUILD_REQUIREMENTS),
                sha256=hashlib.sha256(BUILD_REQUIREMENTS).hexdigest(),
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


def _publish_build_inputs(
    storage: RootedLocalStorage,
    *,
    original: bytes = BUILD_REQUIREMENTS,
    normalized: bytes = BUILD_REQUIREMENTS,
) -> None:
    storage.publish_bytes("requirements/original/input.txt", original)
    storage.publish_bytes("requirements/normalized/input.txt", normalized)


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


@pytest.mark.parametrize("forgery", ["original", "normalized"])
def test_parse_rejects_payload_keys_that_disagree_with_subject(
    tmp_path: Path, forgery: str
) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    original = storage.publish_bytes(
        "requirements/original/input.txt", BUILD_REQUIREMENTS
    )
    payload = {
        "originalObjectKey": original.object_key,
        "normalizedObjectKey": "requirements/normalized/input.txt",
    }
    payload[f"{forgery}ObjectKey"] = f"requirements/{forgery}/forged.txt"
    with repository.engine.begin() as connection:
        connection.execute(
            insert(requirement_files).values(
                id=FILE_ID,
                user_id="70000000-0000-4000-8000-000000000001",
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
                payload_json=_wire("REQUIREMENT_PARSE", FILE_ID, payload),
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

    result = JobPipeline(repository, storage, workspaces, RecordingStages()).run(lease)

    assert result.status is BuildStatus.FAILED
    with repository.engine.connect() as connection:
        file_row = connection.execute(
            select(requirement_files.c.parse_status, requirement_files.c.parse_error)
        ).one()
        job_row = connection.execute(
            select(build_jobs.c.status, build_jobs.c.last_error)
        ).one()
    assert file_row.parse_status == "FAILED"
    assert "PARSE_SUBJECT_INTEGRITY_FAILURE" in file_row.parse_error
    assert job_row.status == "FAILED"
    assert "PARSE_SUBJECT_INTEGRITY_FAILURE" in job_row.last_error


def test_parse_rejects_tampered_original_file(tmp_path: Path) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    observed = b"numpy==1.26.4\n"
    storage.publish_bytes("requirements/original/input.txt", b"numpy==9.9.9\n")
    with repository.engine.begin() as connection:
        connection.execute(
            insert(requirement_files).values(
                id=FILE_ID,
                user_id="70000000-0000-4000-8000-000000000001",
                original_object_key="requirements/original/input.txt",
                normalized_object_key="requirements/normalized/input.txt",
                parse_status="PENDING",
                size_bytes=len(observed),
                sha256=hashlib.sha256(observed).hexdigest(),
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
                        "originalObjectKey": "requirements/original/input.txt",
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

    result = JobPipeline(repository, storage, workspaces, RecordingStages()).run(lease)

    assert result.status is BuildStatus.FAILED
    assert not (storage.root / "requirements/normalized/input.txt").exists()
    with repository.engine.connect() as connection:
        assert connection.scalar(select(requirement_files.c.parse_status)) == "FAILED"
        assert "PARSE_SUBJECT_INTEGRITY_FAILURE" in connection.scalar(
            select(build_jobs.c.last_error)
        )


def test_malformed_payload_terminally_fails_job_and_subject(tmp_path: Path) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    lease = _seed_build(repository, payload_json='{"schemaVersion": 1}')
    pipeline = JobPipeline(repository, storage, workspaces, RecordingStages())

    result = pipeline.run(lease)

    assert result.status is BuildStatus.FAILED
    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "FAILED"
        assert connection.scalar(select(build_tasks.c.status)) == "FAILED"


@pytest.mark.parametrize("forgery", ["requirement", "key", "target"])
def test_build_rejects_payload_fields_that_disagree_with_locked_subject(
    tmp_path: Path, forgery: str
) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    _publish_build_inputs(storage)
    payload: dict[str, object] = {
        "requirementFileId": FILE_ID,
        "normalizedObjectKey": "requirements/normalized/input.txt",
        "solveMode": "COMPATIBLE",
        "targetSnapshot": _target(),
    }
    if forgery == "requirement":
        payload["requirementFileId"] = "30000000-0000-4000-8000-000000000099"
    elif forgery == "key":
        payload["normalizedObjectKey"] = "requirements/normalized/forged.txt"
        storage.publish_bytes(
            "requirements/normalized/forged.txt", BUILD_REQUIREMENTS
        )
    else:
        target = _target()
        target["profileId"] = "40000000-0000-4000-8000-000000000099"
        payload["targetSnapshot"] = target
    lease = _seed_build(repository, _wire("BUILD", TASK_ID, payload))
    stages = RecordingStages()

    result = JobPipeline(repository, storage, workspaces, stages).run(lease)

    assert result == PipelineResult(BuildStatus.FAILED, None)
    assert stages.calls == []


def test_build_rejects_cross_user_requirement_binding(tmp_path: Path) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    _publish_build_inputs(storage)
    lease = _seed_build(repository)
    with repository.engine.begin() as connection:
        connection.execute(
            update(requirement_files)
            .where(requirement_files.c.id == FILE_ID)
            .values(user_id="70000000-0000-4000-8000-000000000099")
        )
    stages = RecordingStages()

    result = JobPipeline(repository, storage, workspaces, stages).run(lease)

    assert result == PipelineResult(BuildStatus.FAILED, None)
    assert stages.calls == []
    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "FAILED"


@pytest.mark.parametrize("tampered", ["original", "normalized"])
def test_build_rejects_tampered_requirement_objects(
    tmp_path: Path, tampered: str
) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    _publish_build_inputs(
        storage,
        original=(b"numpy==9.9.9\n" if tampered == "original" else BUILD_REQUIREMENTS),
        normalized=(
            b"numpy==9.9.9\n" if tampered == "normalized" else BUILD_REQUIREMENTS
        ),
    )
    lease = _seed_build(repository)
    stages = RecordingStages()

    result = JobPipeline(repository, storage, workspaces, stages).run(lease)

    assert result == PipelineResult(BuildStatus.FAILED, None)
    assert stages.calls == []


def test_successful_build_publishes_artifact_and_terminal_states(
    tmp_path: Path,
) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    _publish_build_inputs(storage)
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


def test_build_rejects_oversized_resolution_before_package_audit(
    tmp_path: Path,
) -> None:
    class OversizedResolutionStages(RecordingStages):
        def resolve(
            self, parsed: object, target: object, workspace: Path
        ) -> ResolutionResult:
            self.calls.append("resolve")
            package = _resolved_package("alpha", "1.0")
            return ResolutionResult(
                "1", tuple(package for _ in range(MAX_RESOLVED_PACKAGES + 1))
            )

    repository, storage, workspaces = _environment(tmp_path)
    _publish_build_inputs(storage)
    lease = _seed_build(repository)
    stages = OversizedResolutionStages()

    result = JobPipeline(repository, storage, workspaces, stages).run(lease)

    assert result.status is BuildStatus.FAILED
    assert stages.calls == ["resolve"]
    with repository.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(resolved_packages)) == 0


def test_partial_build_publishes_verified_successes_and_package_audit(
    tmp_path: Path,
) -> None:
    class PartialStages(RecordingStages):
        def __init__(self) -> None:
            super().__init__()
            self.resolution = ResolutionResult(
                "1", (_resolved_package("alpha", "1.0"), _resolved_package("beta", "2.0"))
            )

        def resolve(
            self, parsed: object, target: object, workspace: Path
        ) -> ResolutionResult:
            self.calls.append("resolve")
            return self.resolution

        def download(
            self,
            resolution: ResolutionResult,
            target: object,
            workspace: Path,
            cancel: object,
        ) -> BuildDownload:
            self.calls.append("download")
            package = resolution.packages[0]
            path = workspace / package.wheel_filename
            path.write_bytes(b"wheel")
            wheel = DownloadedWheel(
                package=package.name,
                version=package.version,
                filename=package.wheel_filename,
                path=path,
                source=PackageSource.PYPI,
                byte_size=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                tags=frozenset({Tag("py3", "none", "any")}),
            )
            return BuildDownload(
                (wheel,), (PackageFailure("beta", "no target Wheel"),)
            )

        def validate(
            self,
            resolution: ResolutionResult,
            wheels: tuple[DownloadedWheel, ...],
            target: TargetProfile,
        ) -> BuildValidation:
            self.calls.append("validate")
            report = validate_closure(resolution, wheels, target)
            validated = cast(
                ValidatedWheel,
                SimpleNamespace(download=wheels[0], report=SimpleNamespace(snapshot=None)),
            )
            return BuildValidation((validated,), report)

    repository, storage, workspaces = _environment(tmp_path)
    _publish_build_inputs(storage)
    lease = _seed_build(repository)

    result = JobPipeline(
        repository, storage, workspaces, PartialStages()
    ).run(lease)

    assert result.status is BuildStatus.PARTIAL_SUCCESS
    assert result.artifact_id is not None
    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_tasks.c.status)) == "PARTIAL_SUCCESS"
        rows = connection.execute(
            select(
                resolved_packages.c.normalized_name,
                resolved_packages.c.wheel_status,
            ).order_by(resolved_packages.c.normalized_name)
        ).all()
    assert rows == [("alpha", "STATIC_PASSED"), ("beta", "MISSING")]


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
    _publish_build_inputs(storage)
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
    _publish_build_inputs(storage)
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
    _publish_build_inputs(storage)
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


class CleanupFailureWorkspace:
    def __init__(self, root: Path) -> None:
        self.path = root / "wf-execution-cleanup-failure"
        self.path.mkdir()

    def cleanup(self) -> None:
        raise RuntimeError("workspace cleanup failed")


class CleanupFailureWorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root

    def allocate(self, execution_id: str | None = None) -> CleanupFailureWorkspace:
        return CleanupFailureWorkspace(self.root)


def test_workspace_cleanup_failure_does_not_replace_success_result(
    tmp_path: Path,
) -> None:
    repository, storage, workspaces = _environment(tmp_path)
    _publish_build_inputs(storage)
    lease = _seed_build(repository)
    pipeline = JobPipeline(
        repository,
        storage,
        cast(WorkspaceManager, CleanupFailureWorkspaceManager(workspaces.root)),
        RecordingStages(),
    )

    result = pipeline.run(lease)

    assert result.status is BuildStatus.SUCCESS
    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "COMPLETED"


class DeleteFailureStorage(RootedLocalStorage):
    def delete_if_owned(self, object_key: str, expected_sha256: str) -> bool:
        raise OSError("compensation delete failed")


def test_compensation_delete_failure_preserves_retry_state(tmp_path: Path) -> None:
    source, storage, workspaces = _environment(tmp_path)
    _publish_build_inputs(storage)
    lease = _seed_build(source)
    repository = RollbackRepository(
        source.engine,
        lease_seconds=source.lease_seconds,
        clock=source._clock,
    )
    failing_storage = DeleteFailureStorage(storage.root)

    result = JobPipeline(
        repository,
        failing_storage,
        workspaces,
        RecordingStages(),
    ).run(lease)

    assert result.status is BuildStatus.QUEUED
    with repository.engine.connect() as connection:
        assert connection.scalar(select(build_jobs.c.status)) == "READY"


def test_nested_infrastructure_exception_group_is_retryable() -> None:
    error = ExceptionGroup(
        "stage and heartbeat",
        [OSError("network failed"), ExceptionGroup("db", [RuntimeError("gone")])],
    )

    assert pipeline_module._retryable(error) is True


def test_nested_lost_lease_exception_group_is_never_retryable() -> None:
    error = ExceptionGroup(
        "stage and heartbeat",
        [OSError("network failed"), pipeline_module.LostLeaseError("expired")],
    )

    assert pipeline_module._retryable(error) is False
    assert pipeline_module._contains_lost_lease(error) is True


def test_mixed_permanent_exception_group_is_not_retryable() -> None:
    assert pipeline_module._retryable(
        ExceptionGroup("mixed", [OSError("network"), ValueError("bad archive")])
    ) is False


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
