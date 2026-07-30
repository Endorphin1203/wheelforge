from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wheelforge_worker.download import DownloadedWheel
from wheelforge_worker.resolver import ResolutionResult, VersionChange
from wheelforge_worker.target import TargetProfile
from wheelforge_worker.validation import ArchiveValidationReport, StaticValidationReport


@dataclass(frozen=True, slots=True)
class ValidatedWheel:
    download: DownloadedWheel
    report: ArchiveValidationReport


@dataclass(frozen=True, slots=True)
class ArtifactBuildContext:
    build_id: str
    original_requirements: str
    resolution: ResolutionResult
    version_changes: tuple[VersionChange, ...]
    target: TargetProfile
    validation: StaticValidationReport
    wheels: tuple[ValidatedWheel, ...]


@dataclass(frozen=True, slots=True)
class BuiltArtifact:
    path: Path
    sha256: str
    manifest: dict[str, Any]
