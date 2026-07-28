from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import product
from typing import Protocol

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, Specifier, SpecifierSet
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from wheelforge_worker.parser import parse_requirements
from wheelforge_worker.parser.models import ParsedRequirements, RequirementItem
from wheelforge_worker.target import TargetProfile, wheel_is_compatible

from .candidates import CandidateMetadata, CandidateProvider, ordered_candidates
from .models import (
    CandidateRejection,
    CandidateRejectionCode,
    CandidateSelection,
    CompatibilityFailureCode,
    CompatibilityResolutionError,
    PackageSource,
    ResolutionAttempt,
    ResolutionResult,
    ResolveLimits,
    ResolverError,
    VersionChange,
    VersionChangeKind,
)


_MAX_PROVIDER_RELEASES = 2000
_MAX_VERSION_CHARS = 512
_MAX_REQUIRES_PYTHON_CHARS = 1024
_MAX_WHEELS_PER_RELEASE = 256
_MAX_WHEEL_FILENAME_CHARS = 1024
_Clock = Callable[[], float]


class _StrictResolver(Protocol):
    def resolve(
        self, parsed: ParsedRequirements, profile: TargetProfile, source: str
    ) -> ResolutionResult: ...


@dataclass(frozen=True, slots=True)
class _RelaxablePin:
    item_index: int
    package: str
    original: Version


@dataclass(frozen=True, slots=True)
class _Observation:
    source: PackageSource
    metadata: CandidateMetadata
    version: Version


class _StopResolution(Exception):
    def __init__(self, code: CompatibilityFailureCode) -> None:
        self.code = code


class CompatibleResolver:
    def __init__(
        self,
        strict_resolver: _StrictResolver,
        candidate_provider: CandidateProvider,
        *,
        clock: _Clock = time.monotonic,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._strict_resolver = strict_resolver
        self._candidate_provider = candidate_provider
        self._clock = clock

    def resolve(
        self,
        parsed: ParsedRequirements,
        profile: TargetProfile,
        sources: Sequence[PackageSource],
        limits: ResolveLimits,
    ) -> ResolutionResult:
        source_order = _validated_sources(sources)
        if not isinstance(parsed, ParsedRequirements):
            raise ValueError("parsed requirements are required")
        if not isinstance(profile, TargetProfile):
            raise ValueError("target profile is required")
        if not isinstance(limits, ResolveLimits):
            raise ValueError("resolve limits are required")

        started = self._clock()
        attempts: list[ResolutionAttempt] = []
        rejections: list[CandidateRejection] = []
        pins = _relaxable_pins(parsed)
        original_versions = tuple(pin.original for pin in pins)

        try:
            original_selections = _selections(pins, original_versions)
            for source in source_order:
                self._guard(started, attempts, limits)
                result = self._attempt(
                    parsed,
                    profile,
                    source,
                    original_selections,
                    attempts,
                )
                if result is not None:
                    return _successful_result(
                        result,
                        parsed,
                        source,
                        attempts,
                        rejections,
                    )

            options: list[tuple[Version, ...]] = []
            for pin in pins:
                self._guard(started, attempts, limits)
                alternatives = self._discover_candidates(
                    pin, profile, source_order, limits, started, attempts, rejections
                )
                options.append((pin.original, *alternatives))

            for versions in product(*options):
                if versions == original_versions:
                    continue
                candidate_parsed = _rebuild_requirements(parsed, pins, versions)
                selections = _selections(pins, versions)
                for source in source_order:
                    self._guard(started, attempts, limits)
                    result = self._attempt(
                        candidate_parsed,
                        profile,
                        source,
                        selections,
                        attempts,
                    )
                    if result is not None:
                        return _successful_result(
                            result,
                            candidate_parsed,
                            source,
                            attempts,
                            rejections,
                            original=parsed,
                        )
        except _StopResolution as stopped:
            raise _resolution_failure(
                stopped.code, parsed, attempts, rejections
            ) from None

        raise _resolution_failure(
            CompatibilityFailureCode.EXHAUSTED, parsed, attempts, rejections
        )

    def _guard(
        self,
        started: float,
        attempts: list[ResolutionAttempt],
        limits: ResolveLimits,
    ) -> None:
        if self._clock() - started >= limits.timeout.total_seconds():
            raise _StopResolution(CompatibilityFailureCode.TIMEOUT)
        if len(attempts) >= limits.max_resolution_attempts:
            raise _StopResolution(CompatibilityFailureCode.ATTEMPT_LIMIT)

    def _attempt(
        self,
        parsed: ParsedRequirements,
        profile: TargetProfile,
        source: PackageSource,
        selections: tuple[CandidateSelection, ...],
        attempts: list[ResolutionAttempt],
    ) -> ResolutionResult | None:
        try:
            result = self._strict_resolver.resolve(parsed, profile, source.value)
        except ResolverError as error:
            attempts.append(
                ResolutionAttempt(
                    selections,
                    source,
                    CompatibilityFailureCode.STRICT_RESOLUTION,
                    f"{type(error).__name__}: {str(error)[:1024]}",
                )
            )
            return None
        attempts.append(ResolutionAttempt(selections, source, None, "strict graph resolved"))
        return result

    def _discover_candidates(
        self,
        pin: _RelaxablePin,
        profile: TargetProfile,
        sources: tuple[PackageSource, ...],
        limits: ResolveLimits,
        started: float,
        attempts: list[ResolutionAttempt],
        rejections: list[CandidateRejection],
    ) -> tuple[Version, ...]:
        observations: list[_Observation] = []
        for source in sources:
            self._guard(started, attempts, limits)
            try:
                releases = self._candidate_provider.candidates(pin.package, source)
                observations.extend(
                    _bounded_observations(pin.package, source, releases, rejections)
                )
            except Exception as error:
                rejections.append(
                    CandidateRejection(
                        pin.package,
                        "",
                        source,
                        CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                        f"candidate provider failed: {type(error).__name__}",
                    )
                )

        original_is_yanked = any(
            observation.version == pin.original and observation.metadata.yanked
            for observation in observations
        )
        eligible: set[Version] = set()
        source_by_version: dict[Version, PackageSource] = {}
        for observation in observations:
            metadata = observation.metadata
            version = observation.version
            if version == pin.original:
                continue
            if not pin.original.is_prerelease and version.is_prerelease:
                _reject(
                    rejections,
                    pin.package,
                    metadata.version,
                    observation.source,
                    CandidateRejectionCode.PRERELEASE,
                    "prerelease is not permitted for a stable original pin",
                )
                continue
            if not _within_boundary(pin.original, version):
                _reject(
                    rejections,
                    pin.package,
                    metadata.version,
                    observation.source,
                    CandidateRejectionCode.OUTSIDE_COMPATIBILITY_BOUNDARY,
                    "candidate crosses the allowed compatibility boundary",
                )
                continue
            if metadata.yanked and not original_is_yanked:
                _reject(
                    rejections,
                    pin.package,
                    metadata.version,
                    observation.source,
                    CandidateRejectionCode.YANKED,
                    "yanked candidate is not permitted for a non-yanked original pin",
                )
                continue
            if not _python_compatible(
                pin.package, observation, profile, rejections
            ):
                continue
            if not _has_target_wheel(pin.package, observation, profile, rejections):
                continue
            eligible.add(version)
            source_by_version.setdefault(version, observation.source)

        ordered = ordered_candidates(pin.original, eligible)
        accepted = ordered[: limits.max_candidates_per_requirement]
        for version in ordered[limits.max_candidates_per_requirement :]:
            source = source_by_version[version]
            _reject(
                rejections,
                pin.package,
                str(version),
                source,
                CandidateRejectionCode.CANDIDATE_LIMIT,
                "candidate omitted by the per-requirement candidate limit",
            )
        return tuple(accepted)


def _validated_sources(
    sources: Sequence[PackageSource],
) -> tuple[PackageSource, ...]:
    if isinstance(sources, (str, bytes)):
        raise ValueError("sources must be builtin package source identities")
    source_order = tuple(sources)
    if (
        not source_order
        or any(not isinstance(source, PackageSource) for source in source_order)
        or len(set(source_order)) != len(source_order)
    ):
        raise ValueError("sources must be nonempty unique builtin package source identities")
    return source_order


def _bounded_observations(
    package: str,
    source: PackageSource,
    releases: Iterable[CandidateMetadata],
    rejections: list[CandidateRejection],
) -> list[_Observation]:
    observations: list[_Observation] = []
    try:
        iterator = iter(releases)
    except TypeError:
        _reject(
            rejections,
            package,
            "",
            source,
            CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
            "candidate releases are not iterable",
        )
        return observations

    for index, metadata in enumerate(iterator):
        if index >= _MAX_PROVIDER_RELEASES:
            _reject(
                rejections,
                package,
                "",
                source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                f"candidate release count exceeds limit {_MAX_PROVIDER_RELEASES}",
            )
            break
        if not isinstance(metadata, CandidateMetadata):
            _reject(
                rejections,
                package,
                "",
                source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                "candidate entry has an invalid type",
            )
            continue
        if not isinstance(metadata.version, str) or not metadata.version:
            _reject(
                rejections,
                package,
                "",
                source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                "candidate version must be a nonempty string",
            )
            continue
        if len(metadata.version) > _MAX_VERSION_CHARS:
            _reject(
                rejections,
                package,
                metadata.version[:_MAX_VERSION_CHARS],
                source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                f"candidate version exceeds limit {_MAX_VERSION_CHARS}",
            )
            continue
        if type(metadata.yanked) is not bool:
            _reject(
                rejections,
                package,
                metadata.version,
                source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                "candidate yanked state must be boolean",
            )
            continue
        try:
            version = Version(metadata.version)
        except InvalidVersion:
            _reject(
                rejections,
                package,
                metadata.version,
                source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                "candidate version is malformed",
            )
            continue
        observations.append(_Observation(source, metadata, version))
    return observations


def _python_compatible(
    package: str,
    observation: _Observation,
    profile: TargetProfile,
    rejections: list[CandidateRejection],
) -> bool:
    value = observation.metadata.requires_python
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > _MAX_REQUIRES_PYTHON_CHARS:
        _reject(
            rejections,
            package,
            observation.metadata.version,
            observation.source,
            CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
            "Requires-Python is not a bounded string",
        )
        return False
    try:
        specifier = SpecifierSet(value)
    except InvalidSpecifier:
        _reject(
            rejections,
            package,
            observation.metadata.version,
            observation.source,
            CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
            "Requires-Python is malformed",
        )
        return False
    if not specifier.contains(Version(profile.python_full_version), prereleases=True):
        _reject(
            rejections,
            package,
            observation.metadata.version,
            observation.source,
            CandidateRejectionCode.REQUIRES_PYTHON,
            f"Requires-Python {value!r} excludes target {profile.python_full_version}",
        )
        return False
    return True


def _has_target_wheel(
    package: str,
    observation: _Observation,
    profile: TargetProfile,
    rejections: list[CandidateRejection],
) -> bool:
    filenames = observation.metadata.wheel_filenames
    if not isinstance(filenames, tuple) or len(filenames) > _MAX_WHEELS_PER_RELEASE:
        _reject(
            rejections,
            package,
            observation.metadata.version,
            observation.source,
            CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
            "Wheel filename collection is not a bounded tuple",
        )
        return False

    compatible = False
    for filename in filenames:
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > _MAX_WHEEL_FILENAME_CHARS
        ):
            _reject(
                rejections,
                package,
                observation.metadata.version,
                observation.source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                "Wheel filename is not a bounded nonempty string",
            )
            continue
        try:
            wheel_name, wheel_version, _, _ = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            _reject(
                rejections,
                package,
                observation.metadata.version,
                observation.source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                f"Wheel filename is malformed: {filename[:256]}",
            )
            continue
        if canonicalize_name(wheel_name) != package or wheel_version != observation.version:
            _reject(
                rejections,
                package,
                observation.metadata.version,
                observation.source,
                CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
                "Wheel filename does not match the candidate package and version",
            )
            continue
        compatible = compatible or wheel_is_compatible(filename, profile)

    if not compatible:
        _reject(
            rejections,
            package,
            observation.metadata.version,
            observation.source,
            CandidateRejectionCode.NO_TARGET_WHEEL,
            "candidate has no syntactically valid Wheel for the target profile",
        )
    return compatible


def _reject(
    rejections: list[CandidateRejection],
    package: str,
    version: str,
    source: PackageSource,
    code: CandidateRejectionCode,
    reason: str,
) -> None:
    rejections.append(CandidateRejection(package, version, source, code, reason))


def _within_boundary(original: Version, candidate: Version) -> bool:
    if original.release[0] == 0:
        return candidate.release[:2] == original.release[:2]
    return candidate.release[0] == original.release[0]


def _relaxable_pins(parsed: ParsedRequirements) -> tuple[_RelaxablePin, ...]:
    pins: list[_RelaxablePin] = []
    for index, item in enumerate(parsed.items):
        exact = [
            specifier
            for specifier in SpecifierSet(item.specifier)
            if specifier.operator == "=="
        ]
        if len(exact) == 1:
            pins.append(_RelaxablePin(index, item.name, Version(exact[0].version)))
    return tuple(pins)


def _selections(
    pins: tuple[_RelaxablePin, ...], versions: tuple[Version, ...]
) -> tuple[CandidateSelection, ...]:
    return tuple(
        CandidateSelection(pin.package, version)
        for pin, version in zip(pins, versions, strict=True)
    )


def _rebuild_requirements(
    original: ParsedRequirements,
    pins: tuple[_RelaxablePin, ...],
    versions: tuple[Version, ...],
) -> ParsedRequirements:
    replacements = {
        pin.item_index: version for pin, version in zip(pins, versions, strict=True)
    }
    lines: list[str] = []
    for index, item in enumerate(original.items):
        version = replacements.get(index)
        if version is None:
            lines.append(_format_item(item, item.specifier))
            continue
        components: list[str] = []
        for component in item.specifier.split(","):
            parsed_specifier = Specifier(component)
            components.append(
                f"=={version}" if parsed_specifier.operator == "==" else component
            )
        line = _format_item(item, ",".join(components))
        try:
            rebuilt = Requirement(line)
        except InvalidRequirement as error:
            raise ValueError("candidate requirement could not be rebuilt safely") from error
        if rebuilt.url is not None:
            raise ValueError("candidate requirement must not contain a URL")
        lines.append(line)
    return parse_requirements(("\n".join(lines) + "\n").encode("utf-8"))


def _format_item(item: RequirementItem, specifier: str) -> str:
    extras = f"[{','.join(item.extras)}]" if item.extras else ""
    marker = f"; {item.marker}" if item.marker is not None else ""
    return f"{item.name}{extras}{specifier}{marker}"


def _successful_result(
    result: ResolutionResult,
    selected: ParsedRequirements,
    source: PackageSource,
    attempts: list[ResolutionAttempt],
    rejections: list[CandidateRejection],
    *,
    original: ParsedRequirements | None = None,
) -> ResolutionResult:
    baseline = selected if original is None else original
    return replace(
        result,
        attempts=tuple(attempts),
        rejections=tuple(rejections),
        changes=_version_changes(baseline, result, source),
        source=source,
    )


def _version_changes(
    original: ParsedRequirements,
    result: ResolutionResult,
    source: PackageSource,
) -> tuple[VersionChange, ...]:
    resolved = {package.name: package.version for package in result.packages}
    changes: list[VersionChange] = []
    for item in original.items:
        exact = [
            specifier
            for specifier in SpecifierSet(item.specifier)
            if specifier.operator == "=="
        ]
        original_version = Version(exact[0].version) if len(exact) == 1 else None
        resolved_version = resolved.get(item.name)
        if resolved_version is None:
            kind = VersionChangeKind.UNRESOLVED
            reason = "direct requirement is inactive or absent from the strict result"
        elif original_version is None:
            kind = VersionChangeKind.UNCHANGED
            reason = "original non-exact constraint was preserved"
        elif resolved_version > original_version:
            kind = VersionChangeKind.UPGRADE
            reason = "strict full-graph resolution selected a higher compatible version"
        elif resolved_version < original_version:
            kind = VersionChangeKind.DOWNGRADE
            reason = "strict full-graph resolution selected a lower compatible version"
        else:
            kind = VersionChangeKind.UNCHANGED
            reason = "strict full-graph resolution retained the original version"
        changes.append(
            VersionChange(
                item.name,
                kind,
                item.specifier,
                original_version,
                resolved_version,
                reason,
                source,
            )
        )
    return tuple(changes)


def _resolution_failure(
    code: CompatibilityFailureCode,
    parsed: ParsedRequirements,
    attempts: list[ResolutionAttempt],
    rejections: list[CandidateRejection],
) -> CompatibilityResolutionError:
    changes = tuple(
        VersionChange(
            item.name,
            VersionChangeKind.UNRESOLVED,
            item.specifier,
            _exact_version(item),
            None,
            "no bounded strict full-graph attempt resolved this requirement",
            None,
        )
        for item in parsed.items
    )
    return CompatibilityResolutionError(
        code, tuple(attempts), tuple(rejections), changes
    )


def _exact_version(item: RequirementItem) -> Version | None:
    exact = [
        specifier
        for specifier in SpecifierSet(item.specifier)
        if specifier.operator == "=="
    ]
    return Version(exact[0].version) if len(exact) == 1 else None
