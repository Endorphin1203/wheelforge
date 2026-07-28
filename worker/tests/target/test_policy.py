from __future__ import annotations

from importlib import import_module
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError


TARGETS = (
    {
        "os": "LINUX",
        "architecture": "X86_64",
        "platform_tag": "manylinux2014_x86_64",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_system": "Linux",
        "sys_platform": "linux",
    },
    {
        "os": "LINUX",
        "architecture": "AARCH64",
        "platform_tag": "manylinux2014_aarch64",
        "os_name": "posix",
        "platform_machine": "aarch64",
        "platform_system": "Linux",
        "sys_platform": "linux",
    },
    {
        "os": "WINDOWS",
        "architecture": "AMD64",
        "platform_tag": "win_amd64",
        "os_name": "nt",
        "platform_machine": "AMD64",
        "platform_system": "Windows",
        "sys_platform": "win32",
    },
    {
        "os": "WINDOWS",
        "architecture": "ARM64",
        "platform_tag": "win_arm64",
        "os_name": "nt",
        "platform_machine": "ARM64",
        "platform_system": "Windows",
        "sys_platform": "win32",
    },
)


def import_target_policy() -> tuple[type[Any], Any, Any]:
    try:
        target_module = import_module("wheelforge_worker.target")
    except ModuleNotFoundError as error:
        pytest.fail(str(error))

    return (
        target_module.TargetProfile,
        target_module.marker_environment,
        target_module.wheel_is_compatible,
    )


def make_snapshot(
    *,
    os: str = "LINUX",
    architecture: str = "AARCH64",
    python_version: str = "3.11",
    python_full_version: str = "3.11.9",
    platform_tag: str = "manylinux2014_aarch64",
    abi_tags: list[str] | None = None,
    validation_type: str = "STATIC",
    validation_policy_version: str = "wheel-tags-v1",
    profile_version: int = 0,
    python_implementation: str = "CPYTHON",
) -> dict[str, Any]:
    major, minor = python_version.split(".")
    return {
        "profileId": str(uuid4()),
        "profileCode": f"{os.lower()}-{architecture.lower()}-cp{major}{minor}",
        "os": os,
        "architecture": architecture,
        "pythonImplementation": python_implementation,
        "pythonVersion": python_version,
        "pythonFullVersion": python_full_version,
        "platformTag": platform_tag,
        "abiTags": abi_tags or [f"cp{major}{minor}", "abi3", "none"],
        "validationType": validation_type,
        "validationPolicyVersion": validation_policy_version,
        "profileVersion": profile_version,
    }


def make_profile(**overrides: Any) -> Any:
    target_profile, _, _ = import_target_policy()
    return target_profile.model_validate(make_snapshot(**overrides))


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("python_version", ["3.9", "3.10", "3.11", "3.12", "3.13"])
def test_target_profile_accepts_supported_target_matrix(
    target: dict[str, str], python_version: str
) -> None:
    target_profile, _, _ = import_target_policy()
    major, minor = python_version.split(".")
    snapshot = make_snapshot(
        os=target["os"],
        architecture=target["architecture"],
        python_version=python_version,
        python_full_version=f"{python_version}.9",
        platform_tag=target["platform_tag"],
        abi_tags=["none", f"cp{major}{minor}", "abi3"],
        profile_version=0,
    )

    profile = target_profile.model_validate(snapshot)

    assert profile.os == target["os"]
    assert profile.architecture == target["architecture"]
    assert profile.python_version == python_version
    assert profile.python_full_version == f"{python_version}.9"
    assert profile.platform_tag == target["platform_tag"]
    assert tuple(profile.abi_tags) == (f"cp{major}{minor}", "abi3", "none")


def test_target_profile_accepts_snake_case_inputs() -> None:
    target_profile, _, _ = import_target_policy()

    profile = target_profile(
        profile_id=str(uuid4()),
        profile_code="windows-amd64-cp311",
        os="WINDOWS",
        architecture="AMD64",
        python_implementation="CPYTHON",
        python_version="3.11",
        python_full_version="3.11.9",
        platform_tag="win_amd64",
        abi_tags=["cp311", "abi3", "none"],
        validation_type="STATIC",
        validation_policy_version="wheel-tags-v1",
        profile_version=0,
    )

    assert profile.model_dump(by_alias=True)["validationPolicyVersion"] == "wheel-tags-v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("validation_type", "DYNAMIC"),
        ("validation_policy_version", "v1"),
        ("profile_version", -1),
        ("profile_id", "not-a-uuid"),
        ("python_implementation", "PYPY"),
        ("python_version", "3.8"),
        ("python_version", "3.14"),
        ("python_full_version", "3.12.1"),
        ("platform_tag", "win_amd64"),
        ("abi_tags", ["cp311", "abi3"]),
        ("abi_tags", ["cp311", "abi3", "none", "cp310"]),
        ("abi_tags", ["cp310", "abi3", "none"]),
    ],
)
def test_target_profile_rejects_invalid_snapshot_values(
    field: str, value: Any
) -> None:
    target_profile, _, _ = import_target_policy()
    snapshot = make_snapshot()

    if field == "profile_id":
        snapshot["profileId"] = value
    elif field == "python_implementation":
        snapshot["pythonImplementation"] = value
    elif field == "python_version":
        snapshot["pythonVersion"] = value
        snapshot["pythonFullVersion"] = f"{value}.9"
        major, minor = value.split(".")
        snapshot["abiTags"] = [f"cp{major}{minor}", "abi3", "none"]
    elif field == "python_full_version":
        snapshot["pythonFullVersion"] = value
    elif field == "platform_tag":
        snapshot["platformTag"] = value
    elif field == "abi_tags":
        snapshot["abiTags"] = value
    elif field == "validation_type":
        snapshot["validationType"] = value
    elif field == "validation_policy_version":
        snapshot["validationPolicyVersion"] = value
    elif field == "profile_version":
        snapshot["profileVersion"] = value
    else:
        raise AssertionError(f"unexpected field {field}")

    with pytest.raises(ValidationError):
        target_profile.model_validate(snapshot)


def test_target_profile_rejects_python_full_version_major_minor_mismatch() -> None:
    target_profile, _, _ = import_target_policy()
    snapshot = make_snapshot(python_version="3.11", python_full_version="3.10.9")

    with pytest.raises(ValidationError):
        target_profile.model_validate(snapshot)


@pytest.mark.parametrize("target", TARGETS)
def test_marker_environment_is_deterministic_for_each_target(
    target: dict[str, str],
) -> None:
    profile = make_profile(
        os=target["os"],
        architecture=target["architecture"],
        platform_tag=target["platform_tag"],
        python_version="3.11",
        python_full_version="3.11.9",
    )
    _, marker_environment, _ = import_target_policy()

    environment = marker_environment(profile)

    assert environment == {
        "implementation_name": "cpython",
        "implementation_version": "3.11.9",
        "os_name": target["os_name"],
        "platform_machine": target["platform_machine"],
        "platform_python_implementation": "CPython",
        "platform_release": "0",
        "platform_system": target["platform_system"],
        "platform_version": "0",
        "python_full_version": "3.11.9",
        "python_version": "3.11",
        "sys_platform": target["sys_platform"],
    }


def test_linux_arm64_wheel_policy_accepts_native_abi3_pure_and_compressed_tags() -> None:
    profile = make_profile()
    _, _, wheel_is_compatible = import_target_policy()

    assert wheel_is_compatible(
        "demo-1.0-cp311-cp311-manylinux2014_aarch64.whl", profile
    )
    assert wheel_is_compatible("demo-1.0-cp39-abi3-manylinux2014_aarch64.whl", profile)
    assert wheel_is_compatible(
        "demo-1.0-cp311-none-manylinux_2_17_aarch64.whl", profile
    )
    assert wheel_is_compatible("demo-1.0-py311-none-any.whl", profile)
    assert wheel_is_compatible("demo-1.0-py3-none-any.whl", profile)
    assert wheel_is_compatible("demo-1.0-py3.py311-none-any.whl", profile)
    assert wheel_is_compatible(
        "demo-1.0-py3-none-manylinux2014_aarch64.whl", profile
    )


@pytest.mark.parametrize("abi3_interpreter", ["cp32", "cp38"])
def test_wheel_policy_accepts_stable_abi_baselines_from_cp32(
    abi3_interpreter: str,
) -> None:
    profile = make_profile(
        python_version="3.13",
        python_full_version="3.13.9",
    )
    _, _, wheel_is_compatible = import_target_policy()

    assert wheel_is_compatible(
        f"demo-1.0-{abi3_interpreter}-abi3-manylinux2014_aarch64.whl",
        profile,
    )


def test_wheel_policy_rejects_stable_abi_baseline_newer_than_target() -> None:
    profile = make_profile(
        python_version="3.13",
        python_full_version="3.13.9",
    )
    _, _, wheel_is_compatible = import_target_policy()

    assert not wheel_is_compatible(
        "demo-1.0-cp314-abi3-manylinux2014_aarch64.whl",
        profile,
    )


def test_linux_manylinux_policy_accepts_older_pep600_baselines_but_not_newer_ones() -> None:
    profile = make_profile(os="LINUX", architecture="X86_64", platform_tag="manylinux2014_x86_64")
    _, _, wheel_is_compatible = import_target_policy()

    assert wheel_is_compatible("demo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl", profile)
    assert wheel_is_compatible("demo-1.0-cp311-cp311-manylinux_2_12_x86_64.whl", profile)
    assert not wheel_is_compatible(
        "demo-1.0-cp311-cp311-manylinux_2_18_x86_64.whl", profile
    )
    assert not wheel_is_compatible(
        "demo-1.0-cp311-cp311-musllinux_1_2_x86_64.whl", profile
    )
    assert not wheel_is_compatible(
        "demo-1.0-cp311-cp311-manylinux2014_aarch64.whl", profile
    )
    assert not wheel_is_compatible("demo-1.0-cp311-cp311-win_amd64.whl", profile)
    assert not wheel_is_compatible(
        "demo-1.0-cp311-cp311-macosx_11_0_x86_64.whl", profile
    )


def test_wheel_policy_rejects_wrong_minor_newer_abi3_and_non_wheel_inputs() -> None:
    profile = make_profile()
    _, _, wheel_is_compatible = import_target_policy()

    assert not wheel_is_compatible(
        "demo-1.0-cp310-cp310-manylinux2014_aarch64.whl", profile
    )
    assert not wheel_is_compatible("demo-1.0-cp312-abi3-manylinux2014_aarch64.whl", profile)
    assert not wheel_is_compatible("demo-1.0.tar.gz", profile)
    assert not wheel_is_compatible("not-a-wheel", profile)


def test_windows_targets_only_accept_matching_platform_or_any() -> None:
    amd64_profile = make_profile(
        os="WINDOWS",
        architecture="AMD64",
        platform_tag="win_amd64",
    )
    arm64_profile = make_profile(
        os="WINDOWS",
        architecture="ARM64",
        platform_tag="win_arm64",
    )
    _, _, wheel_is_compatible = import_target_policy()

    assert wheel_is_compatible("demo-1.0-cp311-cp311-win_amd64.whl", amd64_profile)
    assert wheel_is_compatible("demo-1.0-py3-none-any.whl", amd64_profile)
    assert wheel_is_compatible("demo-1.0-cp39-abi3-win_arm64.whl", arm64_profile)
    assert not wheel_is_compatible("demo-1.0-cp311-cp311-win_arm64.whl", amd64_profile)
    assert not wheel_is_compatible("demo-1.0-cp311-cp311-manylinux2014_x86_64.whl", amd64_profile)
    assert not wheel_is_compatible(
        "demo-1.0-cp311-cp311-macosx_11_0_arm64.whl", arm64_profile
    )
