from collections.abc import Callable, Iterable
from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from wheelforge_worker.parser import ParsedRequirements, parse_requirements
from wheelforge_worker.resolver.candidates import CandidateMetadata, ordered_candidates
from wheelforge_worker.resolver.compatible import (
    MAX_RESOLUTION_OBSERVATIONS,
    MAX_RESOLUTION_REJECTIONS,
    CompatibleResolver,
)
from wheelforge_worker.resolver.models import (
    CandidateRejectionCode,
    CompatibilityFailureCode,
    CompatibilityResolutionError,
    PackageSource,
    ResolutionResult,
    ResolveLimits,
    ResolvedPackage,
    ResolverCommandError,
    VersionChangeKind,
)
from wheelforge_worker.target import TargetProfile


def versions(*values: str) -> list[Version]:
    return [Version(value) for value in values]


def profile(
    python: str = "3.11.9", os: str = "LINUX", architecture: str = "AARCH64"
) -> TargetProfile:
    minor = ".".join(python.split(".")[:2])
    platform = {
        ("LINUX", "AARCH64"): "manylinux2014_aarch64",
        ("LINUX", "X86_64"): "manylinux2014_x86_64",
        ("WINDOWS", "ARM64"): "win_arm64",
        ("WINDOWS", "AMD64"): "win_amd64",
    }[(os, architecture)]
    cp_tag = f"cp{minor.replace('.', '')}"
    return TargetProfile.model_validate(
        {
            "profileId": "12345678-1234-4234-8234-123456789abc",
            "profileCode": f"{os}-{architecture}-{minor}",
            "os": os,
            "architecture": architecture,
            "pythonImplementation": "CPYTHON",
            "pythonVersion": minor,
            "pythonFullVersion": python,
            "platformTag": platform,
            "abiTags": [cp_tag, "abi3", "none"],
            "validationType": "STATIC",
            "validationPolicyVersion": "wheel-tags-v1",
            "profileVersion": 1,
        }
    )


def candidate(
    version: str,
    *,
    yanked: bool = False,
    requires_python: str | None = None,
    wheels: tuple[str, ...] | None = None,
) -> CandidateMetadata:
    filenames = wheels or (
        f"demo-{version}-py3-none-any.whl",
    )
    return CandidateMetadata(version, yanked, requires_python, filenames)


def result_for(**packages: str) -> ResolutionResult:
    return ResolutionResult(
        "1",
        tuple(
            ResolvedPackage(
                name=name,
                version=Version(version),
                requested=True,
                artifact_url=f"https://files.pythonhosted.org/{name}.whl",
                wheel_filename=f"{name}-{version}-py3-none-any.whl",
                requires_dist=(),
                requires_python=None,
                archive_hashes=(),
            )
            for name, version in packages.items()
        ),
    )


class RecordingProvider:
    def __init__(
        self,
        releases: dict[tuple[str, PackageSource], Iterable[CandidateMetadata]],
    ) -> None:
        self.releases = releases
        self.calls: list[tuple[str, PackageSource]] = []

    def candidates(
        self, package: str, source: PackageSource
    ) -> Iterable[CandidateMetadata]:
        self.calls.append((package, source))
        return self.releases.get((package, source), ())


class RecordingStrictResolver:
    def __init__(
        self,
        success: Callable[[ParsedRequirements, str], ResolutionResult | None],
    ) -> None:
        self.success = success
        self.calls: list[tuple[ParsedRequirements, TargetProfile, str]] = []

    def resolve(
        self, parsed: ParsedRequirements, target: TargetProfile, source: str
    ) -> ResolutionResult:
        self.calls.append((parsed, target, source))
        result = self.success(parsed, source)
        if result is None:
            raise ResolverCommandError(1, "dependency conflict")
        return result


class AdjustableClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def pinned_versions(parsed: ParsedRequirements) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in parsed.items:
        exact = [
            specifier.version
            for specifier in Requirement(item.original_text).specifier
            if specifier.operator == "=="
        ]
        if exact:
            found[item.name] = exact[0]
    return found


def test_prefers_nearest_patch_up_then_down_before_other_minor() -> None:
    available = versions("1.25.9", "1.26.3", "1.26.5", "1.27.0", "2.0.0")

    assert ordered_candidates(Version("1.26.4"), available) == versions(
        "1.26.5", "1.26.3", "1.27.0", "1.25.9"
    )


def test_zero_major_never_crosses_minor() -> None:
    available = versions("0.28.9", "0.29.3", "0.29.5", "0.30.0")

    assert ordered_candidates(Version("0.29.4"), available) == versions(
        "0.29.5", "0.29.3"
    )


def test_orders_all_same_minor_patches_before_nearest_other_minors() -> None:
    available = versions(
        "1.24.20",
        "1.25.1",
        "1.26.1",
        "1.26.3",
        "1.26.5",
        "1.26.8",
        "1.27.2",
        "1.28.0",
    )

    assert ordered_candidates(Version("1.26.4"), available) == versions(
        "1.26.5",
        "1.26.8",
        "1.26.3",
        "1.26.1",
        "1.27.2",
        "1.28.0",
        "1.25.1",
        "1.24.20",
    )


def test_stable_original_excludes_prerelease_dev_and_original() -> None:
    available = versions("1.2.3", "1.2.4rc1", "1.2.4.dev1", "1.2.4")

    assert ordered_candidates(Version("1.2.3"), available) == versions("1.2.4")


def test_prerelease_original_permits_prerelease_candidates() -> None:
    available = versions("1.2.3rc1", "1.2.3rc2", "1.2.3", "1.2.4rc1")

    assert ordered_candidates(Version("1.2.3rc1"), available) == versions(
        "1.2.3rc2", "1.2.3", "1.2.4rc1"
    )


@pytest.mark.parametrize("original", ["1", "1.0"])
def test_short_stable_release_uses_zero_filled_minor_and_patch(original: str) -> None:
    available = versions("1.0.1", "1.1", "2.0")

    assert ordered_candidates(Version(original), available) == versions(
        "1.0.1", "1.1"
    )


@pytest.mark.parametrize("original", ["0", "0.0"])
def test_short_zero_release_stays_within_zero_filled_minor(original: str) -> None:
    available = versions("0.0.1", "0.1")

    assert ordered_candidates(Version(original), available) == versions("0.0.1")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_candidates_per_requirement": 0}, "candidate"),
        ({"max_candidates_per_requirement": 21}, "candidate"),
        ({"max_resolution_attempts": 0}, "attempt"),
        ({"max_resolution_attempts": 101}, "attempt"),
        ({"timeout": timedelta(0)}, "timeout"),
        ({"timeout": timedelta(minutes=11)}, "timeout"),
        ({"max_resolution_attempts": True}, "attempt"),
    ],
)
def test_resolve_limits_are_strictly_validated(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ResolveLimits(**kwargs)  # type: ignore[arg-type]


def test_resolve_limits_and_candidate_metadata_are_immutable() -> None:
    limits = ResolveLimits()
    metadata = candidate("1.2.3")

    with pytest.raises(FrozenInstanceError):
        limits.max_resolution_attempts = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        metadata.yanked = True  # type: ignore[misc]


def test_original_graph_uses_source_fallback_before_candidate_discovery() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(
        lambda requirements, source: result_for(demo="1.0")
        if source == "ALIYUN"
        else None
    )
    provider = RecordingProvider({})

    result = CompatibleResolver(strict, provider).resolve(
        parsed,
        profile(),
        (PackageSource.TSINGHUA, PackageSource.ALIYUN, PackageSource.PYPI),
        ResolveLimits(),
    )

    assert [call[2] for call in strict.calls] == ["TSINGHUA", "ALIYUN"]
    assert provider.calls == []
    assert [attempt.source for attempt in result.attempts] == [
        PackageSource.TSINGHUA,
        PackageSource.ALIYUN,
    ]
    assert result.attempts[0].failure is CompatibilityFailureCode.STRICT_RESOLUTION
    assert result.attempts[1].failure is None
    assert result.source is PackageSource.ALIYUN
    assert result.changes[0].kind is VersionChangeKind.UNCHANGED


def test_candidate_source_invocations_each_consume_one_attempt() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(
        lambda requirements, source: result_for(demo="1.1")
        if pinned_versions(requirements) == {"demo": "1.1"} and source == "PYPI"
        else None
    )
    provider = RecordingProvider(
        {
            ("demo", PackageSource.TSINGHUA): (candidate("1.1"),),
            ("demo", PackageSource.PYPI): (candidate("1.1"),),
        }
    )

    result = CompatibleResolver(strict, provider).resolve(
        parsed,
        profile(),
        (PackageSource.TSINGHUA, PackageSource.PYPI),
        ResolveLimits(),
    )

    assert len(strict.calls) == 4
    assert [attempt.source for attempt in result.attempts] == [
        PackageSource.TSINGHUA,
        PackageSource.PYPI,
        PackageSource.TSINGHUA,
        PackageSource.PYPI,
    ]
    assert result.changes[0].kind is VersionChangeKind.UPGRADE


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("1", "1.0.1"),
        ("1.0", "1.0.1"),
        ("0", "0.0.1"),
        ("0.0", "0.0.1"),
    ],
)
def test_short_exact_pin_resolves_end_to_end(
    original: str, replacement: str
) -> None:
    parsed = parse_requirements(f"demo=={original}\n".encode())
    strict = RecordingStrictResolver(
        lambda requirements, _source: result_for(demo=replacement)
        if pinned_versions(requirements) == {"demo": replacement}
        else None
    )
    provider = RecordingProvider(
        {("demo", PackageSource.PYPI): (candidate(replacement),)}
    )

    result = CompatibleResolver(strict, provider).resolve(
        parsed, profile(), (PackageSource.PYPI,), ResolveLimits()
    )

    assert result.packages[0].version == Version(replacement)
    assert result.changes[0].kind is VersionChangeKind.UPGRADE
    assert len(strict.calls) == 2


@pytest.mark.parametrize(
    ("python", "accepted"),
    [
        ("3.9.19", False),
        ("3.10.14", False),
        ("3.11.9", False),
        ("3.12.4", True),
        ("3.13.0", True),
    ],
)
def test_requires_python_is_evaluated_against_target(
    python: str, accepted: bool
) -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(
        lambda requirements, _source: result_for(demo="1.1")
        if pinned_versions(requirements) == {"demo": "1.1"}
        else None
    )
    provider = RecordingProvider(
        {
            ("demo", PackageSource.PYPI): (
                candidate("1.1", requires_python=">=3.12"),
            )
        }
    )

    if accepted:
        result = CompatibleResolver(strict, provider).resolve(
            parsed, profile(python), (PackageSource.PYPI,), ResolveLimits()
        )
        assert result.packages[0].version == Version("1.1")
    else:
        with pytest.raises(CompatibilityResolutionError) as captured:
            CompatibleResolver(strict, provider).resolve(
                parsed, profile(python), (PackageSource.PYPI,), ResolveLimits()
            )
        assert captured.value.rejections[0].code is (
            CandidateRejectionCode.REQUIRES_PYTHON
        )


@pytest.mark.parametrize(
    ("target", "accepted_wheel", "rejected_wheel"),
    [
        (
            profile(os="LINUX", architecture="AARCH64"),
            "demo-1.1-cp311-cp311-manylinux2014_aarch64.whl",
            "demo-1.1-cp311-cp311-manylinux2014_x86_64.whl",
        ),
        (
            profile(os="LINUX", architecture="X86_64"),
            "demo-1.1-cp311-cp311-manylinux2014_x86_64.whl",
            "demo-1.1-cp311-cp311-win_amd64.whl",
        ),
        (
            profile(os="WINDOWS", architecture="ARM64"),
            "demo-1.1-cp39-abi3-win_arm64.whl",
            "demo-1.1-cp311-cp311-win_amd64.whl",
        ),
        (
            profile(os="WINDOWS", architecture="AMD64"),
            "demo-1.1-cp311-cp311-win_amd64.whl",
            "demo-1.1-cp311-cp311-manylinux2014_x86_64.whl",
        ),
    ],
)
def test_candidate_requires_a_wheel_for_the_fixed_target(
    target: TargetProfile, accepted_wheel: str, rejected_wheel: str
) -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(
        lambda requirements, _source: result_for(demo="1.2")
        if pinned_versions(requirements) == {"demo": "1.2"}
        else None
    )
    provider = RecordingProvider(
        {
            ("demo", PackageSource.PYPI): (
                candidate("1.1", wheels=(rejected_wheel,)),
                candidate("1.2", wheels=(accepted_wheel.replace("1.1", "1.2"),)),
            )
        }
    )

    result = CompatibleResolver(strict, provider).resolve(
        parsed, target, (PackageSource.PYPI,), ResolveLimits()
    )

    assert result.packages[0].version == Version("1.2")
    assert any(
        rejection.version == "1.1"
        and rejection.code is CandidateRejectionCode.NO_TARGET_WHEEL
        for rejection in result.rejections
    )


def test_prefilter_records_prerelease_yanked_boundary_and_malformed_data() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(lambda _requirements, _source: None)
    provider = RecordingProvider(
        {
            ("demo", PackageSource.PYPI): (
                candidate("1.1rc1"),
                candidate("1.1", yanked=True),
                candidate("2.0"),
                candidate("not-a-version"),
                candidate("1.2", requires_python="not a specifier"),
                candidate("1.3", wheels=("not-a-wheel",)),
            )
        }
    )

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, provider).resolve(
            parsed, profile(), (PackageSource.PYPI,), ResolveLimits()
        )

    codes = {rejection.code for rejection in captured.value.rejections}
    assert {
        CandidateRejectionCode.PRERELEASE,
        CandidateRejectionCode.YANKED,
        CandidateRejectionCode.OUTSIDE_COMPATIBILITY_BOUNDARY,
        CandidateRejectionCode.MALFORMED_PROVIDER_DATA,
        CandidateRejectionCode.NO_TARGET_WHEEL,
    } <= codes


def test_yanked_alternative_is_allowed_when_original_pin_is_yanked() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(
        lambda requirements, _source: result_for(demo="1.1")
        if pinned_versions(requirements) == {"demo": "1.1"}
        else None
    )
    provider = RecordingProvider(
        {
            ("demo", PackageSource.PYPI): (
                candidate("1.0", yanked=True),
                candidate("1.1", yanked=True),
            )
        }
    )

    result = CompatibleResolver(strict, provider).resolve(
        parsed, profile(), (PackageSource.PYPI,), ResolveLimits()
    )

    assert result.packages[0].version == Version("1.1")
    assert not any(
        rejection.code is CandidateRejectionCode.YANKED
        for rejection in result.rejections
    )


def test_candidate_cannot_borrow_eligibility_from_another_source() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(
        lambda requirements, source: result_for(demo="1.1")
        if pinned_versions(requirements) == {"demo": "1.1"}
        and source == "TSINGHUA"
        else None
    )
    provider = RecordingProvider(
        {
            ("demo", PackageSource.TSINGHUA): (
                candidate("1.1", yanked=True),
            ),
            ("demo", PackageSource.PYPI): (candidate("1.1"),),
        }
    )

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, provider).resolve(
            parsed,
            profile(),
            (PackageSource.TSINGHUA, PackageSource.PYPI),
            ResolveLimits(),
        )

    candidate_calls = [
        (pinned_versions(requirements), source)
        for requirements, _target, source in strict.calls
        if pinned_versions(requirements) != {"demo": "1.0"}
    ]
    assert candidate_calls == [({"demo": "1.1"}, "PYPI")]
    assert any(
        rejection.source is PackageSource.TSINGHUA
        and rejection.version == "1.1"
        and rejection.code is CandidateRejectionCode.YANKED
        for rejection in captured.value.rejections
    )


def test_yanked_original_policy_is_source_local() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(
        lambda requirements, source: result_for(demo="1.1")
        if pinned_versions(requirements) == {"demo": "1.1"}
        and source == "TSINGHUA"
        else None
    )
    provider = RecordingProvider(
        {
            ("demo", PackageSource.TSINGHUA): (
                candidate("1.0", yanked=True),
                candidate("1.1", yanked=True),
            ),
            ("demo", PackageSource.PYPI): (
                candidate("1.0"),
                candidate("1.1", yanked=True),
            ),
        }
    )

    result = CompatibleResolver(strict, provider).resolve(
        parsed,
        profile(),
        (PackageSource.TSINGHUA, PackageSource.PYPI),
        ResolveLimits(),
    )

    assert result.source is PackageSource.TSINGHUA
    assert any(
        rejection.source is PackageSource.PYPI
        and rejection.version == "1.1"
        and rejection.code is CandidateRejectionCode.YANKED
        for rejection in result.rejections
    )


def test_combination_requires_every_substitution_on_the_same_source() -> None:
    parsed = parse_requirements(b"alpha==1.0\nbeta==2.0\n")
    strict = RecordingStrictResolver(
        lambda requirements, _source: result_for(alpha="1.1", beta="2.1")
        if pinned_versions(requirements) == {"alpha": "1.1", "beta": "2.1"}
        else None
    )
    provider = RecordingProvider(
        {
            ("alpha", PackageSource.TSINGHUA): (
                candidate("1.1", wheels=("alpha-1.1-py3-none-any.whl",)),
            ),
            ("beta", PackageSource.PYPI): (
                candidate("2.1", wheels=("beta-2.1-py3-none-any.whl",)),
            ),
        }
    )

    with pytest.raises(CompatibilityResolutionError):
        CompatibleResolver(strict, provider).resolve(
            parsed,
            profile(),
            (PackageSource.TSINGHUA, PackageSource.PYPI),
            ResolveLimits(),
        )

    attempted_pins = [pinned_versions(requirements) for requirements, _, _ in strict.calls]
    assert {"alpha": "1.1", "beta": "2.1"} not in attempted_pins
    assert len(strict.calls) == 4


def test_rebuild_preserves_ranges_compatible_extras_and_marker() -> None:
    parsed = parse_requirements(
        b'demo[security]==1.0,>=0.9; python_version < "3.12"\nhelper~=2.0\n'
    )

    def succeed(requirements: ParsedRequirements, _source: str) -> ResolutionResult | None:
        if pinned_versions(requirements) != {"demo": "1.1"}:
            return None
        demo, helper = requirements.items
        assert demo.extras == ("security",)
        assert demo.marker == 'python_version < "3.12"'
        assert ">=0.9" in demo.specifier
        assert helper.specifier == "~=2.0"
        assert requirements.normalized_text.endswith("\n")
        return result_for(demo="1.1", helper="2.4")

    strict = RecordingStrictResolver(succeed)
    provider = RecordingProvider(
        {("demo", PackageSource.PYPI): (candidate("1.1"),)}
    )

    result = CompatibleResolver(strict, provider).resolve(
        parsed, profile(), (PackageSource.PYPI,), ResolveLimits()
    )

    assert [change.package for change in result.changes] == ["demo", "helper"]
    assert result.changes[0].kind is VersionChangeKind.UPGRADE
    assert result.changes[1].kind is VersionChangeKind.UNCHANGED


def test_full_graph_backtracking_never_returns_a_partial_success() -> None:
    parsed = parse_requirements(b"alpha==1.0\nbeta==2.0\n")
    successful_pins = {"alpha": "1.1", "beta": "2.1"}
    strict = RecordingStrictResolver(
        lambda requirements, _source: result_for(alpha="1.1", beta="2.1")
        if pinned_versions(requirements) == successful_pins
        else None
    )
    provider = RecordingProvider(
        {
            ("alpha", PackageSource.PYPI): (
                candidate("1.1", wheels=("alpha-1.1-py3-none-any.whl",)),
            ),
            ("beta", PackageSource.PYPI): (
                candidate("2.1", wheels=("beta-2.1-py3-none-any.whl",)),
            ),
        }
    )

    result = CompatibleResolver(strict, provider).resolve(
        parsed, profile(), (PackageSource.PYPI,), ResolveLimits()
    )

    assert pinned_versions(strict.calls[-1][0]) == successful_pins
    assert len(strict.calls) == 4
    assert {package.name for package in result.packages} == {"alpha", "beta"}


def test_success_records_upgrade_downgrade_and_unchanged() -> None:
    parsed = parse_requirements(b"alpha==1.0\nbeta==2.1\ngamma==3.0\n")
    successful_pins = {"alpha": "1.1", "beta": "2.0", "gamma": "3.0"}
    strict = RecordingStrictResolver(
        lambda requirements, _source: result_for(alpha="1.1", beta="2.0", gamma="3.0")
        if pinned_versions(requirements) == successful_pins
        else None
    )
    provider = RecordingProvider(
        {
            ("alpha", PackageSource.PYPI): (
                candidate("1.1", wheels=("alpha-1.1-py3-none-any.whl",)),
            ),
            ("beta", PackageSource.PYPI): (
                candidate("2.0", wheels=("beta-2.0-py3-none-any.whl",)),
            ),
        }
    )

    result = CompatibleResolver(strict, provider).resolve(
        parsed, profile(), (PackageSource.PYPI,), ResolveLimits()
    )

    assert {change.package: change.kind for change in result.changes} == {
        "alpha": VersionChangeKind.UPGRADE,
        "beta": VersionChangeKind.DOWNGRADE,
        "gamma": VersionChangeKind.UNCHANGED,
    }
    assert all(change.source is PackageSource.PYPI for change in result.changes)


def test_failure_carries_complete_audit_and_unresolved_records() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(lambda _requirements, _source: None)
    provider = RecordingProvider(
        {("demo", PackageSource.PYPI): (candidate("1.1"),)}
    )

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, provider).resolve(
            parsed, profile(), (PackageSource.PYPI,), ResolveLimits()
        )

    failure = captured.value
    assert failure.code is CompatibilityFailureCode.EXHAUSTED
    assert len(failure.attempts) == 2
    assert failure.changes[0].kind is VersionChangeKind.UNRESOLVED
    assert failure.changes[0].resolved_version is None


def test_candidate_limit_applies_after_ordering_and_is_audited() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(lambda _requirements, _source: None)
    provider = RecordingProvider(
        {
            ("demo", PackageSource.PYPI): (
                candidate("1.3"),
                candidate("1.1"),
                candidate("1.2"),
            )
        }
    )

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, provider).resolve(
            parsed,
            profile(),
            (PackageSource.PYPI,),
            ResolveLimits(max_candidates_per_requirement=2),
        )

    tried = [pinned_versions(call[0])["demo"] for call in strict.calls]
    assert tried == ["1.0", "1.1", "1.2"]
    assert any(
        rejection.version == "1.3"
        and rejection.code is CandidateRejectionCode.CANDIDATE_LIMIT
        for rejection in captured.value.rejections
    )


def test_total_attempt_limit_counts_original_and_candidate_source_calls() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(lambda _requirements, _source: None)
    provider = RecordingProvider(
        {("demo", PackageSource.PYPI): (candidate("1.1"), candidate("1.2"))}
    )

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, provider).resolve(
            parsed,
            profile(),
            (PackageSource.PYPI,),
            ResolveLimits(max_resolution_attempts=2),
        )

    assert captured.value.code is CompatibilityFailureCode.ATTEMPT_LIMIT
    assert len(strict.calls) == 2
    assert pinned_versions(strict.calls[-1][0]) == {"demo": "1.1"}


def test_monotonic_deadline_stops_before_candidate_discovery() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(lambda _requirements, _source: None)
    provider = RecordingProvider(
        {("demo", PackageSource.PYPI): (candidate("1.1"),)}
    )
    ticks = iter((0.0, 0.0, 2.0))

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, provider, clock=lambda: next(ticks)).resolve(
            parsed,
            profile(),
            (PackageSource.PYPI,),
            ResolveLimits(timeout=timedelta(seconds=1)),
        )

    assert captured.value.code is CompatibilityFailureCode.TIMEOUT
    assert len(strict.calls) == 1
    assert provider.calls == []


def test_late_strict_success_is_rejected_before_acceptance() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    clock = AdjustableClock()

    def finish_late(
        _requirements: ParsedRequirements, _source: str
    ) -> ResolutionResult:
        clock.value = 2.0
        return result_for(demo="1.0")

    strict = RecordingStrictResolver(finish_late)

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, RecordingProvider({}), clock=clock).resolve(
            parsed,
            profile(),
            (PackageSource.PYPI,),
            ResolveLimits(
                max_resolution_attempts=1, timeout=timedelta(seconds=1)
            ),
        )

    assert captured.value.code is CompatibilityFailureCode.TIMEOUT
    assert len(strict.calls) == len(captured.value.attempts) == 1
    attempt = captured.value.attempts[0]
    assert attempt.source is PackageSource.PYPI
    assert [(selection.package, selection.version) for selection in attempt.selections] == [
        ("demo", Version("1.0"))
    ]
    assert attempt.failure is CompatibilityFailureCode.TIMEOUT
    assert attempt.reason == "strict invocation exceeded compatibility deadline"


def test_late_strict_failure_is_rejected_before_recording() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    clock = AdjustableClock()

    def fail_late(
        _requirements: ParsedRequirements, _source: str
    ) -> ResolutionResult | None:
        clock.value = 2.0
        return None

    strict = RecordingStrictResolver(fail_late)

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, RecordingProvider({}), clock=clock).resolve(
            parsed,
            profile(),
            (PackageSource.PYPI,),
            ResolveLimits(
                max_resolution_attempts=1, timeout=timedelta(seconds=1)
            ),
        )

    assert captured.value.code is CompatibilityFailureCode.TIMEOUT
    assert len(strict.calls) == len(captured.value.attempts) == 1
    attempt = captured.value.attempts[0]
    assert attempt.source is PackageSource.PYPI
    assert [(selection.package, selection.version) for selection in attempt.selections] == [
        ("demo", Version("1.0"))
    ]
    assert attempt.failure is CompatibilityFailureCode.TIMEOUT
    assert attempt.reason == "strict invocation exceeded compatibility deadline"


def test_provider_release_count_and_strings_are_bounded() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(lambda _requirements, _source: None)
    oversized = "1." + ("0" * 600)
    releases = [candidate(oversized)] + [candidate(f"1.{minor}") for minor in range(2100)]
    provider = RecordingProvider({("demo", PackageSource.PYPI): releases})

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, provider).resolve(
            parsed, profile(), (PackageSource.PYPI,), ResolveLimits()
        )

    reasons = [rejection.reason for rejection in captured.value.rejections]
    assert any("version" in reason and "limit" in reason for reason in reasons)
    assert any("release count" in reason for reason in reasons)


def test_high_cardinality_provider_has_global_observation_and_rejection_budgets() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    strict = RecordingStrictResolver(lambda _requirements, _source: None)
    malformed_wheels = tuple(f"not-a-wheel-{index}" for index in range(3))
    releases = tuple(
        candidate(f"1.{index}", wheels=malformed_wheels)
        for index in range(MAX_RESOLUTION_OBSERVATIONS + 100)
    )
    provider = RecordingProvider(
        {
            ("demo", PackageSource.PYPI): releases,
            ("demo", PackageSource.ALIYUN): releases,
        }
    )

    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(strict, provider).resolve(
            parsed,
            profile(),
            (PackageSource.PYPI, PackageSource.ALIYUN),
            ResolveLimits(),
        )

    failure = captured.value
    assert len(failure.rejections) <= MAX_RESOLUTION_REJECTIONS
    assert failure.code is CompatibilityFailureCode.RESOURCE_LIMIT
    assert failure.observations_truncated is True


def test_observation_budget_stops_before_next_provider_with_resource_limit() -> None:
    parsed = parse_requirements(b"alpha==1.0\nbeta==1.0\n")
    strict = RecordingStrictResolver(lambda _requirements, _source: None)

    class BoundedProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, PackageSource, int]] = []

        def candidates_bounded(
            self, package: str, source: PackageSource, limit: int
        ) -> Iterable[CandidateMetadata]:
            self.calls.append((package, source, limit))
            return (candidate("1.1", wheels=(f"{package}-1.1-py3-none-any.whl",)),)

        def candidates(
            self, package: str, source: PackageSource
        ) -> Iterable[CandidateMetadata]:
            raise AssertionError("bounded provider path must be used")

    provider = BoundedProvider()
    with pytest.raises(CompatibilityResolutionError) as captured:
        CompatibleResolver(
            strict,
            provider,
            max_observations=1,
        ).resolve(
            parsed,
            profile(),
            (PackageSource.PYPI, PackageSource.ALIYUN),
            ResolveLimits(),
        )

    assert captured.value.code is CompatibilityFailureCode.RESOURCE_LIMIT
    assert provider.calls == [("alpha", PackageSource.PYPI, 1)]
    assert captured.value.observations_truncated is True


def test_target_profile_is_never_mutated_or_substituted() -> None:
    parsed = parse_requirements(b"demo==1.0\n")
    target = profile("3.13.0", os="WINDOWS", architecture="ARM64")
    before = target.model_dump()
    strict = RecordingStrictResolver(
        lambda requirements, _source: result_for(demo="1.1")
        if pinned_versions(requirements) == {"demo": "1.1"}
        else None
    )
    provider = RecordingProvider(
        {
            ("demo", PackageSource.PYPI): (
                candidate("1.1", wheels=("demo-1.1-py3-none-any.whl",)),
            )
        }
    )

    CompatibleResolver(strict, provider).resolve(
        parsed, target, (PackageSource.PYPI,), ResolveLimits()
    )

    assert target.model_dump() == before
    assert all(call[1] is target for call in strict.calls)


@pytest.mark.parametrize(
    "sources",
    [(), ("PYPI",), (PackageSource.PYPI, PackageSource.PYPI)],
)
def test_sources_must_be_nonempty_unique_builtin_identities(
    sources: tuple[object, ...]
) -> None:
    resolver = CompatibleResolver(
        RecordingStrictResolver(lambda _requirements, _source: None),
        RecordingProvider({}),
    )

    with pytest.raises(ValueError, match="source"):
        resolver.resolve(
            parse_requirements(b"demo==1.0\n"),
            profile(),
            sources,  # type: ignore[arg-type]
            ResolveLimits(),
        )
