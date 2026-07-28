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
    original_release = _normalized_release(original)
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
        if _normalized_release(candidate)[1] > original_release[1]
    )
    lower_minors = sorted(
        (
            candidate
            for candidate in candidates
            if _normalized_release(candidate)[1] < original_release[1]
        ),
        reverse=True,
    )
    return same_minor_higher + same_minor_lower + higher_minors + lower_minors


def _within_boundary(original: Version, candidate: Version) -> bool:
    original_release = _normalized_release(original)
    candidate_release = _normalized_release(candidate)
    if original_release[0] == 0:
        return candidate_release[:2] == original_release[:2]
    return candidate_release[0] == original_release[0]


def _same_minor(original: Version, candidate: Version) -> bool:
    return _normalized_release(candidate)[:2] == _normalized_release(original)[:2]


def _normalized_release(version: Version) -> tuple[int, int, int]:
    release = version.release + (0, 0, 0)
    return release[0], release[1], release[2]
