from __future__ import annotations

import json
import hashlib
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Protocol, TypeVar
from urllib.parse import quote
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    TimeoutError as SQLAlchemyTimeoutError,
)

from wheelforge_worker.artifact import (
    ArtifactBuildContext,
    ArtifactBuilder,
    BuiltArtifact,
    ValidatedWheel,
)
from wheelforge_worker.contracts import (
    BuildPayload,
    BuildStatus,
    JobPayload,
    JobType,
    RequirementParsePayload,
)
from wheelforge_worker.download import (
    DownloadCancelledError,
    DownloadedWheel,
    WheelDownloadError,
    WheelDownloader,
)
from wheelforge_worker.parser import (
    ParsedRequirements,
    RequirementsParseError,
    parse_requirements,
)
from wheelforge_worker.resolver import (
    CandidateMetadata,
    CompatibleResolver,
    ResolveLimits,
    ResolvedPackage,
    ResolutionResult,
    ResolverError,
    StrictResolver,
)
from wheelforge_worker.sources import PackageSource, SOURCE_ORDER
from wheelforge_worker.target import TargetProfile
from wheelforge_worker.validation import (
    ArchiveLimits,
    StaticValidationReport,
    UnsafeWheelArchive,
    WheelArchiveValidationError,
    validate_closure,
    validate_wheel_archive_descriptor,
)

from .errors import contains_exception, sanitize_error
from .repository import (
    MAX_RESOLVED_PACKAGES,
    JobLease,
    JobRepository,
    LostLeaseError,
)
from .heartbeat import LeaseHeartbeat
from .storage import RootedLocalStorage, WorkspaceCapability, WorkspaceManager


_MAX_INDEX_BYTES = 8 * 1024 * 1024
_INDEX_JSON_BASES = {
    PackageSource.TSINGHUA: "https://pypi.tuna.tsinghua.edu.cn/pypi",
    PackageSource.ALIYUN: "https://mirrors.aliyun.com/pypi",
    PackageSource.PYPI: "https://pypi.org/pypi",
}
_StageResult = TypeVar("_StageResult")
_HeartbeatWait = Callable[[Event, float], bool]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    status: BuildStatus
    artifact_id: str | None


class SubjectIntegrityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BuildValidation:
    wheels: tuple[ValidatedWheel, ...]
    report: StaticValidationReport
    download_failures: tuple[PackageFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class PackageFailure:
    package: str
    error: str


@dataclass(frozen=True, slots=True)
class BuildDownload:
    wheels: tuple[DownloadedWheel, ...]
    failures: tuple[PackageFailure, ...]


class _Downloader(Protocol):
    def download_one(
        self,
        package: ResolvedPackage,
        profile: TargetProfile,
        destination: Path,
        cancel: Callable[[], bool],
    ) -> DownloadedWheel: ...


class BuildStages(Protocol):
    def resolve(
        self,
        parsed: ParsedRequirements,
        target: TargetProfile,
        workspace: WorkspaceCapability,
    ) -> ResolutionResult: ...

    def download(
        self,
        resolution: ResolutionResult,
        target: TargetProfile,
        workspace: WorkspaceCapability,
        cancel: Callable[[], bool],
    ) -> BuildDownload: ...

    def validate(
        self,
        resolution: ResolutionResult,
        wheels: tuple[DownloadedWheel, ...],
        target: TargetProfile,
        workspace: WorkspaceCapability,
    ) -> BuildValidation: ...

    def package(
        self, context: ArtifactBuildContext, workspace: WorkspaceCapability
    ) -> BuiltArtifact: ...


class BuiltinCandidateProvider:
    def __init__(self, *, timeout_seconds: int = 15) -> None:
        self._timeout_seconds = timeout_seconds

    def candidates(
        self, package: str, source: PackageSource
    ) -> Iterable[CandidateMetadata]:
        return self.candidates_bounded(package, source, 2000)

    def candidates_bounded(
        self, package: str, source: PackageSource, limit: int
    ) -> Iterable[CandidateMetadata]:
        if source not in _INDEX_JSON_BASES:
            raise ValueError("source must be a builtin package source")
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("candidate observation limit is out of bounds")
        safe_package = quote(package, safe="")
        url = f"{_INDEX_JSON_BASES[source]}/{safe_package}/json"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "WheelForge/0.1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                content = response.read(_MAX_INDEX_BYTES + 1)
        except (OSError, urllib.error.URLError) as error:
            raise OSError(
                f"builtin package metadata source {source.value} failed"
            ) from error
        if len(content) > _MAX_INDEX_BYTES:
            raise ValueError("package metadata response exceeds the byte limit")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("package metadata response is not valid JSON") from error
        releases = payload.get("releases") if isinstance(payload, dict) else None
        if not isinstance(releases, dict) or len(releases) > 2000:
            raise ValueError("package metadata releases are malformed")
        result: list[CandidateMetadata] = []
        for version, files in releases.items():
            if not isinstance(version, str) or not isinstance(files, list):
                continue
            wheel_files = [
                item
                for item in files
                if isinstance(item, dict) and item.get("packagetype") == "bdist_wheel"
            ]
            filenames = tuple(
                str(item["filename"])
                for item in wheel_files[:256]
                if isinstance(item.get("filename"), str)
            )
            requires_python = next(
                (
                    str(item["requires_python"])
                    for item in wheel_files
                    if isinstance(item.get("requires_python"), str)
                ),
                None,
            )
            result.append(
                CandidateMetadata(
                    version=version,
                    yanked=bool(wheel_files)
                    and all(bool(item.get("yanked")) for item in wheel_files),
                    requires_python=requires_python,
                    wheel_filenames=filenames,
                )
            )
            if len(result) >= limit:
                break
        return tuple(result)


class DefaultBuildStages:
    def __init__(
        self,
        candidate_provider: BuiltinCandidateProvider | None = None,
        *,
        downloader_factory: Callable[[Path], _Downloader] | None = None,
    ) -> None:
        self._candidate_provider = candidate_provider or BuiltinCandidateProvider()
        self._downloader_factory = downloader_factory

    def resolve(
        self,
        parsed: ParsedRequirements,
        target: TargetProfile,
        workspace: WorkspaceCapability,
    ) -> ResolutionResult:
        external = workspace.external()
        strict = StrictResolver(
            external.path, inherited_fds=external.inherited_fds
        )
        resolver = CompatibleResolver(strict, self._candidate_provider)
        return resolver.resolve(parsed, target, SOURCE_ORDER, ResolveLimits())

    def download(
        self,
        resolution: ResolutionResult,
        target: TargetProfile,
        workspace: WorkspaceCapability,
        cancel: Callable[[], bool],
    ) -> BuildDownload:
        workspace.mkdir("download-work")
        external = workspace.external("download-work")
        download_root = external.path
        if len(resolution.packages) > 500:
            raise ValueError("resolved package count exceeds the limit")
        downloader = (
            WheelDownloader(
                download_root,
                inherited_fds=external.inherited_fds,
                trusted_fd_bound=True,
            )
            if self._downloader_factory is None
            else self._downloader_factory(download_root)
        )
        wheels: list[DownloadedWheel] = []
        failures: list[PackageFailure] = []
        total_bytes = 0
        for index, package in enumerate(resolution.packages):
            try:
                wheel = downloader.download_one(
                    package,
                    target,
                    download_root / f"wheels-{index:04d}",
                    cancel,
                )
            except DownloadCancelledError:
                raise
            except WheelDownloadError as error:
                failures.append(PackageFailure(package.name, str(error)[:2000]))
                continue
            total_bytes += wheel.byte_size
            if total_bytes > 2 * 1024 * 1024 * 1024:
                raise ValueError("total downloaded Wheel byte limit exceeded")
            wheels.append(wheel)
        return BuildDownload(tuple(wheels), tuple(failures))

    def validate(
        self,
        resolution: ResolutionResult,
        wheels: tuple[DownloadedWheel, ...],
        target: TargetProfile,
        workspace: WorkspaceCapability,
    ) -> BuildValidation:
        validated: list[ValidatedWheel] = []
        try:
            for wheel in wheels:
                descriptor = -1
                snapshot_parent = -1
                try:
                    descriptor = workspace.open_external_regular(wheel.path)
                    snapshot_parent = workspace.duplicate_directory()
                except (OSError, RuntimeError) as error:
                    if descriptor != -1:
                        os.close(descriptor)
                    if snapshot_parent != -1:
                        os.close(snapshot_parent)
                    raise UnsafeWheelArchive(
                        "workspace Wheel descendant cannot be opened safely"
                    ) from error
                try:
                    report = validate_wheel_archive_descriptor(
                        descriptor,
                        wheel,
                        ArchiveLimits(),
                        snapshot_parent,
                    )
                finally:
                    if descriptor != -1:
                        os.close(descriptor)
                    if snapshot_parent != -1:
                        os.close(snapshot_parent)
                validated.append(
                    ValidatedWheel(
                        wheel,
                        report,
                    )
                )
            static_report = validate_closure(resolution, wheels, target)
        except BaseException:
            for item in validated:
                snapshot = item.report.snapshot
                if snapshot is None:
                    continue
                try:
                    snapshot.cleanup()
                except Exception:
                    pass
            raise
        return BuildValidation(
            tuple(validated),
            static_report,
        )

    def package(
        self, context: ArtifactBuildContext, workspace: WorkspaceCapability
    ) -> BuiltArtifact:
        external = workspace.external("artifact")
        built = ArtifactBuilder().build(context, external.path)
        return replace(built, path=Path("artifact") / built.path.name)

class JobPipeline:
    def __init__(
        self,
        repository: JobRepository,
        storage: RootedLocalStorage,
        workspaces: WorkspaceManager,
        stages: BuildStages,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
        artifact_ttl: timedelta = timedelta(days=7),
        heartbeat_interval_seconds: float | None = None,
        heartbeat_wait: _HeartbeatWait | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._workspaces = workspaces
        self._stages = stages
        self._uuid_factory = uuid_factory
        self._artifact_ttl = artifact_ttl
        default_interval = max(1.0, repository.lease_seconds / 3)
        self._heartbeat_interval = (
            default_interval
            if heartbeat_interval_seconds is None
            else heartbeat_interval_seconds
        )
        self._heartbeat_wait = heartbeat_wait

    def run(self, lease: JobLease) -> PipelineResult:
        try:
            wire = JobPayload.model_validate_json(lease.payload_json)
            if (
                wire.job_type.value != lease.job_type
                or str(wire.subject_id) != lease.subject_id
                or lease.payload_version != 1
            ):
                raise ValueError("job envelope disagrees with the leased row")
            if wire.job_type is JobType.REQUIREMENT_PARSE:
                parse_payload = RequirementParsePayload.model_validate(wire.payload)
                return self._run_parse(lease, parse_payload)
            build_payload = BuildPayload.model_validate(wire.payload)
            return self._run_build(lease, build_payload)
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            self._repository.fail_terminal(
                lease, "MALFORMED_JOB_PAYLOAD", sanitize_error(error)
            )
            return PipelineResult(BuildStatus.FAILED, None)

    def _run_parse(
        self, lease: JobLease, payload: RequirementParsePayload
    ) -> PipelineResult:
        subject = self._repository.claim_parse_subject(lease)
        if subject is None:
            return PipelineResult(BuildStatus.FAILED, None)
        published = None
        try:
            if (
                payload.original_object_key != subject.original_object_key
                or payload.normalized_object_key != subject.normalized_object_key
            ):
                raise SubjectIntegrityError(
                    "parse payload disagrees with database subject"
                )
            original = self._storage.read_bytes(subject.original_object_key)
            _require_file_observation(
                original, subject.size_bytes, subject.sha256, "original requirements"
            )
            parsed = parse_requirements(original)
            try:
                published = self._storage.publish_or_reuse_bytes(
                    subject.normalized_object_key,
                    parsed.normalized_text.encode("utf-8"),
                    owner_execution_id=lease.execution_id,
                )
            except FileExistsError as error:
                raise SubjectIntegrityError(
                    "normalized requirements object conflicts with parsed content"
                ) from error
            completed = self._repository.complete_parse(
                lease, parsed, subject.normalized_object_key
            )
            if not completed:
                raise LostLeaseError("parse result could not be published")
            return PipelineResult(BuildStatus.SUCCESS, None)
        except RequirementsParseError as error:
            self._repository.fail_terminal(
                lease, "INVALID_REQUIREMENTS", sanitize_error(error)
            )
            return PipelineResult(BuildStatus.FAILED, None)
        except SubjectIntegrityError as error:
            code = "PARSE_SUBJECT_INTEGRITY_FAILURE"
            self._repository.fail_terminal(
                lease, code, f"{code}: {sanitize_error(error)}"
            )
            return PipelineResult(BuildStatus.FAILED, None)
        except BaseException as error:
            if published is not None:
                orphan = published
                _best_effort_cleanup(
                    lambda: self._storage.delete_if_owned(
                        orphan.object_key, orphan.sha256
                    )
                )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if _contains_lost_lease(error):
                return PipelineResult(BuildStatus.FAILED, None)
            retryable = _retryable(error)
            updated = self._repository.retry_or_fail(
                lease, sanitize_error(error), retryable=retryable
            )
            queued = updated and retryable and lease.attempts < lease.max_attempts
            return PipelineResult(
                BuildStatus.QUEUED if queued else BuildStatus.FAILED, None
            )

    def _run_build(self, lease: JobLease, payload: BuildPayload) -> PipelineResult:
        subject = self._repository.claim_build_subject(lease)
        if subject is None:
            return PipelineResult(BuildStatus.FAILED, None)
        owned_workspace = self._workspaces.allocate(lease.execution_id)
        published = None
        validation_snapshots: tuple[object, ...] = ()
        try:
            payload_target = payload.target_snapshot.model_dump(by_alias=True)
            if (
                payload.requirement_file_id != subject.requirement_file_id
                or payload.normalized_object_key != subject.normalized_object_key
                or payload.solve_mode != subject.solve_mode
                or payload_target != subject.target_snapshot
                or payload.target_snapshot.profile_id != subject.target_profile_id
            ):
                raise ValueError("build payload disagrees with database subject")
            target = TargetProfile.model_validate(subject.target_snapshot)
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            original = self._storage.read_bytes(subject.original_object_key)
            _require_file_observation(
                original,
                subject.original_size_bytes,
                subject.original_sha256,
                "original requirements",
            )
            original_parsed = parse_requirements(original)
            normalized = self._storage.read_bytes(subject.normalized_object_key)
            if normalized != original_parsed.normalized_text.encode("utf-8"):
                raise ValueError("normalized requirements content is not database-bound")
            parsed = parse_requirements(normalized)
            original_text = _decode_requirements(original)
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            self._repository.advance_build(
                lease, "RESOLVING", 15, "RESOLVING", "Resolving dependencies"
            )
            resolution = self._during_lease(
                lease,
                lambda: self._stages.resolve(
                    parsed, target, owned_workspace.capability
                ),
            )
            if len(resolution.packages) > MAX_RESOLVED_PACKAGES:
                raise ValueError("resolved package count exceeds the limit")
            self._during_lease(
                lease,
                lambda: self._repository.persist_resolution(lease, resolution),
            )
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            self._repository.advance_build(
                lease, "DOWNLOADING", 40, "DOWNLOADING", "Downloading target Wheels"
            )
            download = self._during_lease(
                lease,
                lambda: self._stages.download(
                    resolution,
                    target,
                    owned_workspace.capability,
                    lambda: self._repository.is_cancel_requested(lease),
                ),
            )
            downloaded = download.wheels
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            self._repository.advance_build(
                lease, "VALIDATING", 70, "VALIDATING", "Validating Wheel archives"
            )
            validation = self._during_lease(
                lease,
                lambda: self._stages.validate(
                    resolution,
                    downloaded,
                    target,
                    owned_workspace.capability,
                ),
            )
            validation_snapshots = _validation_snapshots(validation.wheels)
            validation = BuildValidation(
                validation.wheels,
                validation.report,
                (*download.failures, *validation.download_failures),
            )
            self._repository.persist_package_results(
                lease,
                resolution,
                downloaded,
                validation.report,
                {item.package: item.error for item in validation.download_failures},
            )
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            if not validation.report.complete and not validation.wheels:
                self._repository.fail_terminal(
                    lease,
                    "STATIC_VALIDATION_FAILED",
                    "Static validation found no useful Wheel output",
                )
                return PipelineResult(BuildStatus.FAILED, None)
            terminal = (
                BuildStatus.SUCCESS
                if validation.report.complete
                else BuildStatus.PARTIAL_SUCCESS
            )
            self._repository.advance_build(
                lease, "PACKAGING", 85, "PACKAGING", "Packaging offline artifact"
            )
            owned_workspace.capability.mkdir("artifact")
            context = ArtifactBuildContext(
                build_id=lease.subject_id,
                original_requirements=original_text,
                resolution=resolution,
                version_changes=resolution.changes,
                target=target,
                validation=validation.report,
                wheels=validation.wheels,
            )
            built = self._during_lease(
                lease,
                lambda: self._stages.package(
                    context, owned_workspace.capability
                ),
            )
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            artifact_id = str(self._uuid_factory())
            object_key = (
                f"artifacts/{lease.subject_id}/{lease.execution_id}/{artifact_id}.zip"
            )
            source_descriptor = owned_workspace.capability.open_regular(built.path)
            try:
                published = self._during_lease(
                    lease,
                    lambda: self._storage.publish_descriptor(
                        object_key,
                        source_descriptor,
                        owner_execution_id=lease.execution_id,
                    ),
                )
            finally:
                os.close(source_descriptor)
            if published.sha256 != built.sha256:
                raise OSError("artifact digest changed during local publication")
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                orphan = published
                _best_effort_cleanup(
                    lambda: self._storage.delete_if_owned(
                        orphan.object_key, orphan.sha256
                    )
                )
                published = None
                return cancelled

            artifact = self._repository.publish_artifact_terminal(
                lease,
                artifact_id,
                published,
                built.path.name,
                terminal.value,
                self._repository._clock() + self._artifact_ttl,
            )
            if artifact is None:
                raise LostLeaseError("artifact metadata was not published")
            published = None
            return PipelineResult(terminal, artifact)
        except DownloadCancelledError:
            result = self._cancel_if_requested(lease)
            return result or PipelineResult(BuildStatus.CANCELLED, None)
        except (RequirementsParseError, WheelArchiveValidationError) as error:
            self._repository.fail_terminal(
                lease, "PERMANENT_BUILD_FAILURE", sanitize_error(error)
            )
            return PipelineResult(BuildStatus.FAILED, None)
        except BaseException as error:
            if published is not None:
                orphan = published
                _best_effort_cleanup(
                    lambda: self._storage.delete_if_owned(
                        orphan.object_key, orphan.sha256
                    )
                )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if _contains_lost_lease(error):
                return PipelineResult(BuildStatus.FAILED, None)
            retryable = _retryable(error)
            updated = self._repository.retry_or_fail(
                lease, sanitize_error(error), retryable=retryable
            )
            queued = updated and retryable and lease.attempts < lease.max_attempts
            return PipelineResult(
                BuildStatus.QUEUED if queued else BuildStatus.FAILED, None
            )
        finally:
            _best_effort_cleanup(
                lambda: _close_validation_snapshots(validation_snapshots)
            )
            _best_effort_cleanup(owned_workspace.cleanup)

    def _cancel_if_requested(self, lease: JobLease) -> PipelineResult | None:
        if not self._repository.is_cancel_requested(lease):
            return None
        if not self._repository.cancel_build(lease):
            raise LostLeaseError("cancelled build could not publish its terminal state")
        return PipelineResult(BuildStatus.CANCELLED, None)

    def _during_lease(
        self, lease: JobLease, operation: Callable[[], _StageResult]
    ) -> _StageResult:
        with LeaseHeartbeat(
            self._repository,
            lease,
            interval_seconds=self._heartbeat_interval,
            wait=self._heartbeat_wait,
        ) as heartbeat:
            result = operation()
            heartbeat.check()
            return result

def _decode_requirements(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("requirements text cannot be decoded")


def _require_file_observation(
    content: bytes, expected_size: int, expected_sha256: str, label: str
) -> None:
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise SubjectIntegrityError(
            f"{label} does not match its database observation"
        )


def _retryable(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(
            _retryable(child) for child in error.exceptions
        )
    if isinstance(error, LostLeaseError):
        return False
    if isinstance(error, IntegrityError):
        return False
    if isinstance(error, (OperationalError, SQLAlchemyTimeoutError)):
        return True
    if isinstance(error, DBAPIError):
        return bool(error.connection_invalidated)
    if isinstance(error, (OSError, ResolverError, RuntimeError)):
        return True
    return False


def _contains_lost_lease(error: BaseException) -> bool:
    return contains_exception(error, LostLeaseError)


def _best_effort_cleanup(operation: Callable[[], object]) -> None:
    try:
        operation()
    except Exception:
        # Generated storage/workspace entries are covered by startup maintenance.
        pass


def _validation_snapshots(wheels: tuple[ValidatedWheel, ...]) -> tuple[object, ...]:
    snapshots: list[object] = []
    identities: set[int] = set()
    for wheel in wheels:
        snapshot = getattr(wheel.report, "snapshot", None)
        if snapshot is None or not callable(getattr(snapshot, "close", None)):
            continue
        if id(snapshot) in identities:
            continue
        identities.add(id(snapshot))
        snapshots.append(snapshot)
    return tuple(snapshots)


def _close_validation_snapshots(snapshots: tuple[object, ...]) -> None:
    first_error: Exception | None = None
    for snapshot in snapshots:
        try:
            snapshot.close()  # type: ignore[attr-defined]
        except Exception as error:
            first_error = first_error or error
    if first_error is not None:
        raise first_error
