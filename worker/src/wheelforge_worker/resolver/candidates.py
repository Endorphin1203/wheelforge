from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from packaging.version import Version

from .models import PackageSource


@dataclass(frozen=True, slots=True)
class CandidateMetadata:
    version: str
    yanked: bool
    requires_python: str | None
    wheel_filenames: tuple[str, ...]


class CandidateProvider(Protocol):
    def candidates(
        self, package: str, source: PackageSource
    ) -> Iterable[CandidateMetadata]: ...


def ordered_candidates(
    original: Version, available: Iterable[Version]
) -> list[Version]:
    candidates = {
        candidate
        for candidate in available
        if candidate != original
        and (original.is_prerelease or not candidate.is_prerelease)
        and _within_boundary(original, candidate)
    }
    same_minor_higher = sorted(
        (
            candidate
            for candidate in candidates
            if _same_minor(original, candidate) and candidate > original
        )
    )
    same_minor_lower = sorted(
        (
            candidate
            for candidate in candidates
            if _same_minor(original, candidate) and candidate < original
        ),
        reverse=True,
    )
    higher_minors = sorted(
        candidate
        for candidate in candidates
        if candidate.release[1] > original.release[1]
    )
    lower_minors = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.release[1] < original.release[1]
        ),
        reverse=True,
    )
    return same_minor_higher + same_minor_lower + higher_minors + lower_minors


def _within_boundary(original: Version, candidate: Version) -> bool:
    if original.release[0] == 0:
        return candidate.release[:2] == original.release[:2]
    return candidate.release[0] == original.release[0]


def _same_minor(original: Version, candidate: Version) -> bool:
    return candidate.release[:2] == original.release[:2]
