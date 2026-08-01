from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from packaging.version import Version

from wheelforge_worker.sources import PackageSource


class CandidateRejectionCode(StrEnum):
    PRERELEASE = "PRERELEASE"
    YANKED = "YANKED"
    REQUIRES_PYTHON = "REQUIRES_PYTHON"
    NO_TARGET_WHEEL = "NO_TARGET_WHEEL"
    OUTSIDE_COMPATIBILITY_BOUNDARY = "OUTSIDE_COMPATIBILITY_BOUNDARY"
    MALFORMED_PROVIDER_DATA = "MALFORMED_PROVIDER_DATA"
    CANDIDATE_LIMIT = "CANDIDATE_LIMIT"


class CompatibilityFailureCode(StrEnum):
    STRICT_RESOLUTION = "STRICT_RESOLUTION"
    EXHAUSTED = "EXHAUSTED"
    ATTEMPT_LIMIT = "ATTEMPT_LIMIT"
    TIMEOUT = "TIMEOUT"


class VersionChangeKind(StrEnum):
    UPGRADE = "UPGRADE"
    DOWNGRADE = "DOWNGRADE"
    UNCHANGED = "UNCHANGED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ResolveLimits:
    max_candidates_per_requirement: int = 20
    max_resolution_attempts: int = 100
    timeout: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        if (
            type(self.max_candidates_per_requirement) is not int
            or not 1 <= self.max_candidates_per_requirement <= 20
        ):
            raise ValueError("candidate limit must be an integer from 1 through 20")
        if (
            type(self.max_resolution_attempts) is not int
            or not 1 <= self.max_resolution_attempts <= 100
        ):
            raise ValueError("attempt limit must be an integer from 1 through 100")
        if (
            not isinstance(self.timeout, timedelta)
            or self.timeout <= timedelta(0)
            or self.timeout > timedelta(minutes=10)
        ):
            raise ValueError("timeout must be positive and at most 10 minutes")


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    package: str
    version: Version


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    package: str
    version: str
    source: PackageSource
    code: CandidateRejectionCode
    reason: str


@dataclass(frozen=True, slots=True)
class ResolutionAttempt:
    selections: tuple[CandidateSelection, ...]
    source: PackageSource
    failure: CompatibilityFailureCode | None
    reason: str


@dataclass(frozen=True, slots=True)
class VersionChange:
    package: str
    kind: VersionChangeKind
    original_constraint: str
    original_version: Version | None
    resolved_version: Version | None
    reason: str
    source: PackageSource | None


@dataclass(frozen=True, slots=True)
class ArchiveHash:
    algorithm: str
    value: str


@dataclass(frozen=True, slots=True)
class ResolvedPackage:
    name: str
    version: Version
    requested: bool
    artifact_url: str
    wheel_filename: str
    requires_dist: tuple[str, ...]
    requires_python: str | None
    archive_hashes: tuple[ArchiveHash, ...]


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    report_version: str
    packages: tuple[ResolvedPackage, ...]
    attempts: tuple[ResolutionAttempt, ...] = ()
    rejections: tuple[CandidateRejection, ...] = ()
    changes: tuple[VersionChange, ...] = ()
    source: PackageSource | None = None
    rejections_omitted: int = 0
    observations_truncated: bool = False


class ResolverError(RuntimeError):
    """Base class for strict resolver failures."""


class PipReportError(ResolverError):
    pass


class InvalidPipReportError(PipReportError):
    pass


class PipReportVersionError(PipReportError):
    pass


class PipReportSchemaError(PipReportError):
    pass


class ResolverProcessError(ResolverError):
    pass


class ResolverCommandError(ResolverError):
    def __init__(self, return_code: int, stderr: str) -> None:
        self.return_code = return_code
        self.stderr = stderr[:4096]
        super().__init__(f"pip dry-run exited with status {return_code}")


class ResolverMissingReportError(ResolverError):
    pass


class CompatibilityResolutionError(ResolverError):
    def __init__(
        self,
        code: CompatibilityFailureCode,
        attempts: tuple[ResolutionAttempt, ...],
        rejections: tuple[CandidateRejection, ...],
        changes: tuple[VersionChange, ...],
        rejections_omitted: int = 0,
        observations_truncated: bool = False,
    ) -> None:
        self.code = code
        self.attempts = attempts
        self.rejections = rejections
        self.changes = changes
        self.rejections_omitted = rejections_omitted
        self.observations_truncated = observations_truncated
        super().__init__(f"compatibility resolution failed: {code.value}")
