from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Protocol, TypeVar
from urllib.parse import quote
from uuid import UUID, uuid4

from pydantic import ValidationError

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
    ResolutionResult,
    ResolverError,
    StrictResolver,
)
from wheelforge_worker.sources import PackageSource, SOURCE_ORDER
from wheelforge_worker.target import TargetProfile
from wheelforge_worker.validation import (
    ArchiveLimits,
    StaticValidationReport,
    WheelArchiveValidationError,
    validate_closure,
    validate_wheel_archive,
)

from .repository import JobLease, JobRepository, LostLeaseError
from .heartbeat import LeaseHeartbeat
from .storage import RootedLocalStorage, WorkspaceManager


_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
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


@dataclass(frozen=True, slots=True)
class BuildValidation:
    wheels: tuple[ValidatedWheel, ...]
    report: StaticValidationReport


class BuildStages(Protocol):
    def resolve(
        self, parsed: ParsedRequirements, target: TargetProfile, workspace: Path
    ) -> ResolutionResult: ...

    def download(
        self,
        resolution: ResolutionResult,
        target: TargetProfile,
        workspace: Path,
        cancel: Callable[[], bool],
    ) -> Iterable[DownloadedWheel]: ...

    def validate(
        self,
        resolution: ResolutionResult,
        wheels: tuple[DownloadedWheel, ...],
        target: TargetProfile,
    ) -> BuildValidation: ...

    def package(
        self, context: ArtifactBuildContext, output_directory: Path
    ) -> BuiltArtifact: ...


class BuiltinCandidateProvider:
    def __init__(self, *, timeout_seconds: int = 15) -> None:
        self._timeout_seconds = timeout_seconds

    def candidates(
        self, package: str, source: PackageSource
    ) -> Iterable[CandidateMetadata]:
        if source not in _INDEX_JSON_BASES:
            raise ValueError("source must be a builtin package source")
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
        return tuple(result)


class DefaultBuildStages:
    def __init__(
        self, candidate_provider: BuiltinCandidateProvider | None = None
    ) -> None:
        self._candidate_provider = candidate_provider or BuiltinCandidateProvider()

    def resolve(
        self, parsed: ParsedRequirements, target: TargetProfile, workspace: Path
    ) -> ResolutionResult:
        strict = StrictResolver(workspace)
        resolver = CompatibleResolver(strict, self._candidate_provider)
        return resolver.resolve(parsed, target, SOURCE_ORDER, ResolveLimits())

    def download(
        self,
        resolution: ResolutionResult,
        target: TargetProfile,
        workspace: Path,
        cancel: Callable[[], bool],
    ) -> tuple[DownloadedWheel, ...]:
        download_root = workspace / "download-work"
        download_root.mkdir(mode=0o700)
        downloader = WheelDownloader(download_root)
        return tuple(
            downloader.download(resolution, target, download_root / "wheels", cancel)
        )

    def validate(
        self,
        resolution: ResolutionResult,
        wheels: tuple[DownloadedWheel, ...],
        target: TargetProfile,
    ) -> BuildValidation:
        validated: list[ValidatedWheel] = []
        try:
            for wheel in wheels:
                validated.append(
                    ValidatedWheel(
                        wheel,
                        validate_wheel_archive(wheel.path, wheel, ArchiveLimits()),
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
        self, context: ArtifactBuildContext, output_directory: Path
    ) -> BuiltArtifact:
        return ArtifactBuilder().build(context, output_directory)


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
                lease, "MALFORMED_JOB_PAYLOAD", _safe_error(error)
            )
            return PipelineResult(BuildStatus.FAILED, None)

    def _run_parse(
        self, lease: JobLease, payload: RequirementParsePayload
    ) -> PipelineResult:
        if not self._repository.claim_requirement_file(lease):
            return PipelineResult(BuildStatus.FAILED, None)
        published = None
        try:
            parsed = parse_requirements(
                self._storage.read_bytes(payload.original_object_key)
            )
            published = self._storage.publish_bytes(
                payload.normalized_object_key, parsed.normalized_text.encode("utf-8")
            )
            completed = self._repository.complete_parse(
                lease, parsed, payload.normalized_object_key
            )
            if not completed:
                raise LostLeaseError("parse result could not be published")
            return PipelineResult(BuildStatus.SUCCESS, None)
        except RequirementsParseError as error:
            self._repository.fail_terminal(
                lease, "INVALID_REQUIREMENTS", _safe_error(error)
            )
            return PipelineResult(BuildStatus.FAILED, None)
        except BaseException as error:
            if published is not None:
                self._storage.delete_if_owned(published.object_key, published.sha256)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            self._repository.retry_or_fail(
                lease,
                _safe_error(error),
                retryable=not isinstance(error, LostLeaseError),
            )
            status = (
                BuildStatus.QUEUED
                if lease.attempts < lease.max_attempts
                and not isinstance(error, LostLeaseError)
                else BuildStatus.FAILED
            )
            return PipelineResult(status, None)

    def _run_build(self, lease: JobLease, payload: BuildPayload) -> PipelineResult:
        if not self._repository.claim_build(lease):
            return PipelineResult(BuildStatus.FAILED, None)
        owned_workspace = self._workspaces.allocate()
        published = None
        try:
            target = TargetProfile.model_validate(
                payload.target_snapshot.model_dump(by_alias=True)
            )
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            normalized = self._storage.read_bytes(payload.normalized_object_key)
            parsed = parse_requirements(normalized)
            original_text = self._original_requirements(payload, normalized)
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            self._repository.advance_build(
                lease, "RESOLVING", 15, "RESOLVING", "Resolving dependencies"
            )
            resolution = self._during_lease(
                lease,
                lambda: self._stages.resolve(parsed, target, owned_workspace.path),
            )
            self._repository.persist_resolution(lease, resolution)
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            self._repository.advance_build(
                lease, "DOWNLOADING", 40, "DOWNLOADING", "Downloading target Wheels"
            )
            downloaded = tuple(
                self._during_lease(
                    lease,
                    lambda: self._stages.download(
                        resolution,
                        target,
                        owned_workspace.path,
                        lambda: self._repository.is_cancel_requested(lease),
                    ),
                )
            )
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            self._repository.advance_build(
                lease, "VALIDATING", 70, "VALIDATING", "Validating Wheel archives"
            )
            validation = self._during_lease(
                lease,
                lambda: self._stages.validate(resolution, downloaded, target),
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
            output = owned_workspace.path / "artifact"
            output.mkdir(mode=0o700)
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
                lease, lambda: self._stages.package(context, output)
            )
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                return cancelled

            artifact_id = str(self._uuid_factory())
            object_key = f"artifacts/{lease.subject_id}/{artifact_id}.zip"
            published = self._during_lease(
                lease,
                lambda: self._storage.publish_file(object_key, built.path),
            )
            if published.sha256 != built.sha256:
                raise OSError("artifact digest changed during local publication")
            cancelled = self._cancel_if_requested(lease)
            if cancelled is not None:
                self._storage.delete_if_owned(published.object_key, published.sha256)
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
                lease, "PERMANENT_BUILD_FAILURE", _safe_error(error)
            )
            return PipelineResult(BuildStatus.FAILED, None)
        except BaseException as error:
            if published is not None:
                self._storage.delete_if_owned(published.object_key, published.sha256)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            retryable = _retryable(error)
            updated = self._repository.retry_or_fail(
                lease, _safe_error(error), retryable=retryable
            )
            queued = updated and retryable and lease.attempts < lease.max_attempts
            return PipelineResult(
                BuildStatus.QUEUED if queued else BuildStatus.FAILED, None
            )
        finally:
            owned_workspace.cleanup()

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

    def _original_requirements(self, payload: BuildPayload, normalized: bytes) -> str:
        key = self._repository.requirement_original_key(payload.requirement_file_id)
        if key is None:
            return normalized.decode("utf-8")
        raw = self._storage.read_bytes(key)
        for encoding in ("utf-8-sig", "utf-8", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return normalized.decode("utf-8")


def _retryable(error: BaseException) -> bool:
    if isinstance(error, LostLeaseError):
        return False
    if isinstance(error, (OSError, ResolverError, RuntimeError)):
        return True
    return False


def _safe_error(error: BaseException) -> str:
    value = _CREDENTIALS.sub(r"\1***:***@", str(error))
    return value.replace("\x00", "?")[:2000]
