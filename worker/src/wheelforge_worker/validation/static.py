from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from packaging.markers import UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from wheelforge_worker.download import DownloadedWheel
from wheelforge_worker.resolver.models import ResolvedPackage, ResolutionResult
from wheelforge_worker.target import TargetProfile, marker_environment, wheel_is_compatible


class ValidationIssueCode(StrEnum):
    RESOLUTION_DUPLICATE_PACKAGE = "RESOLUTION_DUPLICATE_PACKAGE"
    WHEEL_MISSING = "WHEEL_MISSING"
    WHEEL_DUPLICATE = "WHEEL_DUPLICATE"
    WHEEL_UNEXPECTED = "WHEEL_UNEXPECTED"
    WHEEL_NAME_VERSION_MISMATCH = "WHEEL_NAME_VERSION_MISMATCH"
    WHEEL_FILENAME_INVALID = "WHEEL_FILENAME_INVALID"
    WHEEL_WRONG_PLATFORM = "WHEEL_WRONG_PLATFORM"
    REQUIRES_PYTHON_INVALID = "REQUIRES_PYTHON_INVALID"
    REQUIRES_PYTHON_UNSATISFIED = "REQUIRES_PYTHON_UNSATISFIED"
    REQUIRES_DIST_INVALID = "REQUIRES_DIST_INVALID"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    DEPENDENCY_VERSION_MISMATCH = "DEPENDENCY_VERSION_MISMATCH"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: ValidationIssueCode
    package: str
    detail: str
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class StaticValidationReport:
    issues: tuple[ValidationIssue, ...]
    complete: bool
    validation_level: str = "STATIC"
    install_verified: bool = False

    def __post_init__(self) -> None:
        if self.validation_level != "STATIC" or self.install_verified is not False:
            raise ValueError("static validation reports cannot claim installation verification")
        if self.complete != (not self.issues):
            raise ValueError("static validation completeness must match its issues")


def validate_closure(
    resolved: ResolutionResult,
    wheels: Iterable[DownloadedWheel],
    profile: TargetProfile,
) -> StaticValidationReport:
    if not isinstance(resolved, ResolutionResult):
        raise ValueError("resolution result is required")
    if not isinstance(profile, TargetProfile):
        raise ValueError("target profile is required")
    packages = tuple(sorted(resolved.packages, key=lambda item: (item.name, item.version)))
    observations = tuple(wheels)
    issues: list[ValidationIssue] = []
    package_names: dict[str, int] = {}
    for item in packages:
        canonical_name = canonicalize_name(item.name)
        package_names[canonical_name] = package_names.get(canonical_name, 0) + 1
    duplicate_names = {
        name for name, occurrence_count in package_names.items() if occurrence_count > 1
    }
    for name in sorted(duplicate_names):
        issues.append(
            _issue(
                ValidationIssueCode.RESOLUTION_DUPLICATE_PACKAGE,
                name,
                "resolved set contains multiple versions of one package",
            )
        )
    expected = {
        (item.name, item.version): item
        for item in packages
        if canonicalize_name(item.name) not in duplicate_names
    }
    observed_by_actual: dict[tuple[str, Version], list[DownloadedWheel]] = {}

    for observed in observations:
        actual = _observe_wheel(observed, profile, issues)
        if actual is not None:
            observed_by_actual.setdefault(actual, []).append(observed)

    for key, package in expected.items():
        matching = observed_by_actual.get(key, [])
        if not matching:
            issues.append(_issue(ValidationIssueCode.WHEEL_MISSING, package.name, "no matching Wheel was downloaded"))
        elif len(matching) > 1:
            issues.append(_issue(ValidationIssueCode.WHEEL_DUPLICATE, package.name, "multiple matching Wheels were downloaded"))

    for key, matching in observed_by_actual.items():
        if key not in expected:
            for observed in matching:
                issues.append(_issue(ValidationIssueCode.WHEEL_UNEXPECTED, observed.package, "Wheel is not in the resolved set", observed.filename))

    for package in packages:
        _validate_requires_python(package, profile, issues)
        if canonicalize_name(package.name) not in duplicate_names:
            _validate_dependencies(package, expected, profile, issues)

    ordered = tuple(sorted(issues, key=lambda item: (item.package, item.code.value, item.filename or "", item.detail)))
    return StaticValidationReport(ordered, not ordered)


def _observe_wheel(
    observed: DownloadedWheel,
    profile: TargetProfile,
    issues: list[ValidationIssue],
) -> tuple[str, Version] | None:
    if not isinstance(observed, DownloadedWheel):
        raise ValueError("wheels must contain DownloadedWheel observations")
    try:
        name, version, _build, _tags = parse_wheel_filename(observed.filename)
    except InvalidWheelFilename:
        issues.append(_issue(ValidationIssueCode.WHEEL_FILENAME_INVALID, observed.package, "Wheel filename is invalid", observed.filename))
        return None
    actual = (canonicalize_name(name), version)
    if actual != (observed.package, observed.version):
        issues.append(_issue(ValidationIssueCode.WHEEL_NAME_VERSION_MISMATCH, observed.package, "Wheel filename disagrees with downloaded observation", observed.filename))
    if not wheel_is_compatible(observed.filename, profile):
        issues.append(_issue(ValidationIssueCode.WHEEL_WRONG_PLATFORM, observed.package, "Wheel tags are incompatible with the target", observed.filename))
    return actual


def _validate_requires_python(
    package: ResolvedPackage,
    profile: TargetProfile,
    issues: list[ValidationIssue],
) -> None:
    if package.requires_python is None:
        return
    try:
        specifier = SpecifierSet(package.requires_python)
    except InvalidSpecifier:
        issues.append(_issue(ValidationIssueCode.REQUIRES_PYTHON_INVALID, package.name, package.requires_python))
        return
    if not specifier.contains(profile.python_full_version, prereleases=True):
        issues.append(_issue(ValidationIssueCode.REQUIRES_PYTHON_UNSATISFIED, package.name, package.requires_python))


def _validate_dependencies(
    package: ResolvedPackage,
    expected: dict[tuple[str, Version], ResolvedPackage],
    profile: TargetProfile,
    issues: list[ValidationIssue],
) -> None:
    versions = {name: version for name, version in expected}
    environment = marker_environment(profile) | {"extra": ""}
    for raw in sorted(package.requires_dist):
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            issues.append(_issue(ValidationIssueCode.REQUIRES_DIST_INVALID, package.name, raw))
            continue
        if requirement.marker is not None:
            try:
                if not requirement.marker.evaluate(environment):
                    continue
            except (
                KeyError,
                UndefinedComparison,
                UndefinedEnvironmentName,
                InvalidVersion,
            ):
                issues.append(
                    _issue(
                        ValidationIssueCode.REQUIRES_DIST_INVALID,
                        package.name,
                        raw,
                    )
                )
                continue
        dependency = canonicalize_name(requirement.name)
        version = versions.get(dependency)
        if version is None:
            issues.append(_issue(ValidationIssueCode.DEPENDENCY_MISSING, package.name, raw))
        elif not requirement.specifier.contains(version, prereleases=True):
            issues.append(_issue(ValidationIssueCode.DEPENDENCY_VERSION_MISMATCH, package.name, raw))


def _issue(
    code: ValidationIssueCode, package: str, detail: str, filename: str | None = None
) -> ValidationIssue:
    return ValidationIssue(code, package, detail, filename)
