from __future__ import annotations

from pathlib import Path

import pytest
from packaging.tags import Tag
from packaging.version import Version

from wheelforge_worker.download import DownloadedWheel
from wheelforge_worker.resolver import ResolvedPackage, ResolutionResult
from wheelforge_worker.target import TargetProfile
from wheelforge_worker.validation import ValidationIssueCode, validate_closure


@pytest.fixture
def profile_cp311_arm64() -> TargetProfile:
    return TargetProfile.model_validate(
        {
            "profileId": "d9428888-122b-11e1-b85c-61cd3cbb3210",
            "profileCode": "linux-arm64-cp311",
            "os": "LINUX",
            "architecture": "AARCH64",
            "pythonImplementation": "CPYTHON",
            "pythonVersion": "3.11",
            "pythonFullVersion": "3.11.9",
            "platformTag": "manylinux2014_aarch64",
            "abiTags": ["cp311", "abi3", "none"],
            "validationType": "STATIC",
            "validationPolicyVersion": "wheel-tags-v1",
            "profileVersion": 1,
        }
    )


def package(
    name: str,
    version: str,
    *,
    requires_dist: tuple[str, ...] = (),
    requires_python: str | None = None,
) -> ResolvedPackage:
    return ResolvedPackage(
        name=name,
        version=Version(version),
        requested=True,
        artifact_url=f"https://files.pythonhosted.org/{name}.whl",
        wheel_filename=f"{name}-{version}-py3-none-any.whl",
        requires_dist=requires_dist,
        requires_python=requires_python,
        archive_hashes=(),
    )


def wheel(name: str, version: str, filename: str | None = None) -> DownloadedWheel:
    filename = filename or f"{name}-{version}-py3-none-any.whl"
    return DownloadedWheel(
        package=name,
        version=Version(version),
        filename=filename,
        path=Path("/trusted") / filename,
        source="PYPI",
        byte_size=1,
        sha256="0" * 64,
        tags=frozenset({Tag("py3", "none", "any")}),
    )


def test_static_report_invariants_and_complete_closure(
    profile_cp311_arm64: TargetProfile,
) -> None:
    resolved = ResolutionResult(
        "1", (package("root", "1.0", requires_dist=("dep>=2",)), package("dep", "2.1"))
    )
    report = validate_closure(resolved, (wheel("root", "1.0"), wheel("dep", "2.1")), profile_cp311_arm64)

    assert report.complete is True
    assert report.issues == ()
    assert report.validation_level == "STATIC"
    assert report.install_verified is False


def test_target_markers_select_only_active_dependencies(profile_cp311_arm64: TargetProfile) -> None:
    resolved = ResolutionResult(
        "1",
        (
            package("root", "1.0", requires_dist=("linuxdep>=1 ; sys_platform == 'linux'", "windep>=1 ; sys_platform == 'win32'")),
            package("linuxdep", "1.0"),
        ),
    )
    report = validate_closure(resolved, (wheel("root", "1.0"), wheel("linuxdep", "1.0")), profile_cp311_arm64)

    assert report.complete is True


@pytest.mark.parametrize(
    ("requires_python", "expected"),
    [(">=3.12", ValidationIssueCode.REQUIRES_PYTHON_UNSATISFIED), (">=>3", ValidationIssueCode.REQUIRES_PYTHON_INVALID)],
)
def test_requires_python_is_checked(
    requires_python: str, expected: ValidationIssueCode, profile_cp311_arm64: TargetProfile
) -> None:
    report = validate_closure(
        ResolutionResult("1", (package("root", "1.0", requires_python=requires_python),)),
        (wheel("root", "1.0"),),
        profile_cp311_arm64,
    )
    assert [issue.code for issue in report.issues] == [expected]


@pytest.mark.parametrize(
    ("requires_dist", "expected"),
    [
        (("dep>=2",), ValidationIssueCode.DEPENDENCY_VERSION_MISMATCH),
        (("missing>=1",), ValidationIssueCode.DEPENDENCY_MISSING),
        (("not a valid requirement @@@",), ValidationIssueCode.REQUIRES_DIST_INVALID),
    ],
)
def test_requires_dist_is_checked(
    requires_dist: tuple[str, ...], expected: ValidationIssueCode, profile_cp311_arm64: TargetProfile
) -> None:
    resolved = ResolutionResult("1", (package("root", "1.0", requires_dist=requires_dist), package("dep", "1.0")))
    report = validate_closure(resolved, (wheel("root", "1.0"), wheel("dep", "1.0")), profile_cp311_arm64)
    assert expected in [issue.code for issue in report.issues]


@pytest.mark.parametrize(
    "requires_dist",
    (
        "dep; python_version ~= 'wat'",
        "dep; implementation_version ~= 'wat'",
        "dep; unknown_variable == 'x'",
    ),
)
def test_marker_parse_and_evaluation_errors_become_validation_issues(
    requires_dist: str, profile_cp311_arm64: TargetProfile
) -> None:
    resolved = ResolutionResult(
        "1",
        (
            package("root", "1.0", requires_dist=(requires_dist,)),
            package("dep", "1.0"),
        ),
    )

    report = validate_closure(
        resolved,
        (wheel("root", "1.0"), wheel("dep", "1.0")),
        profile_cp311_arm64,
    )

    assert report.complete is False
    assert ValidationIssueCode.REQUIRES_DIST_INVALID in {
        issue.code for issue in report.issues
    }


def test_duplicate_canonical_package_name_across_versions_is_a_resolution_issue(
    profile_cp311_arm64: TargetProfile,
) -> None:
    resolved = ResolutionResult(
        "1", (package("demo", "1.0"), package("demo", "2.0"))
    )

    report = validate_closure(
        resolved,
        (wheel("demo", "1.0"), wheel("demo", "2.0")),
        profile_cp311_arm64,
    )

    assert report.complete is False
    assert ValidationIssueCode.RESOLUTION_DUPLICATE_PACKAGE in {
        issue.code for issue in report.issues
    }


def test_missing_duplicate_unexpected_and_wrong_platform_wheels_are_reported(
    profile_cp311_arm64: TargetProfile,
) -> None:
    resolved = ResolutionResult("1", (package("root", "1.0"), package("missing", "1.0")))
    report = validate_closure(
        resolved,
        (
            wheel("root", "1.0"),
            wheel("root", "1.0", "root-1.0-py3-none-any.whl"),
            wheel("extra", "1.0"),
            wheel("bad", "1.0", "bad-1.0-cp311-cp311-win_amd64.whl"),
        ),
        profile_cp311_arm64,
    )

    codes = [issue.code for issue in report.issues]
    assert ValidationIssueCode.WHEEL_MISSING in codes
    assert ValidationIssueCode.WHEEL_DUPLICATE in codes
    assert ValidationIssueCode.WHEEL_UNEXPECTED in codes
    assert ValidationIssueCode.WHEEL_WRONG_PLATFORM in codes


def test_wheel_filename_name_and_version_mismatch_are_reported(
    profile_cp311_arm64: TargetProfile,
) -> None:
    report = validate_closure(
        ResolutionResult("1", (package("root", "1.0"),)),
        (wheel("root", "1.0", "other-2.0-py3-none-any.whl"),),
        profile_cp311_arm64,
    )
    codes = [issue.code for issue in report.issues]
    assert ValidationIssueCode.WHEEL_MISSING in codes
    assert ValidationIssueCode.WHEEL_UNEXPECTED in codes
    assert ValidationIssueCode.WHEEL_NAME_VERSION_MISMATCH in codes
