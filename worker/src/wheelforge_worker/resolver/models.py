from __future__ import annotations

from dataclasses import dataclass

from packaging.version import Version


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
