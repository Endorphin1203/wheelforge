from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from packaging.version import Version

from wheelforge_worker.download import (
    DownloadCancelledError,
    DownloadLimits,
    DownloadValidationError,
    WheelDownloadError,
    WheelDownloader,
    build_download_argv,
)
from wheelforge_worker.process import ProcessExecutionError, ProcessResult, ProcessTimeoutError
from wheelforge_worker.resolver import PackageSource, ResolvedPackage, ResolutionResult
from wheelforge_worker.target import TargetProfile


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


def package(name: str = "demo", version: str = "1.2.3") -> ResolvedPackage:
    return ResolvedPackage(
        name=name,
        version=Version(version),
        requested=True,
        artifact_url=f"https://files.pythonhosted.org/packages/{name}.whl",
        wheel_filename=f"{name}-{version}-py3-none-any.whl",
        requires_dist=(),
        requires_python=None,
        archive_hashes=(),
    )


def resolution(*packages: ResolvedPackage) -> ResolutionResult:
    return ResolutionResult("1", packages)


class FakeRunner:
    def __init__(self, action: Callable[[list[str], Path], object]) -> None:
        self.action = action
        self.calls: list[tuple[list[str], Path, timedelta, dict[str, str]]] = []

    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: timedelta,
        env: dict[str, str],
    ) -> ProcessResult:
        self.calls.append((argv, cwd, timeout, env))
        outcome = self.action(argv, cwd)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int):
            return ProcessResult(tuple(argv), outcome, "", "failed", timedelta(0))
        return ProcessResult(tuple(argv), 0, "", "", timedelta(0))


def write_one(filename: str, content: bytes = b"wheel") -> Callable[[list[str], Path], object]:
    def action(argv: list[str], _cwd: Path) -> object:
        attempt = Path(argv[argv.index("--dest") + 1])
        (attempt / filename).write_bytes(content)
        return None

    return action


def test_download_command_is_exact_targeted_binary_only(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    argv = build_download_argv(package(), profile_cp311_arm64, PackageSource.PYPI, tmp_path)

    assert argv == [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--no-deps",
        "--only-binary=:all:",
        "--dest",
        str(tmp_path),
        "--platform",
        "manylinux2014_aarch64",
        "--python-version",
        "3.11",
        "--implementation",
        "cp",
        "--abi",
        "cp311",
        "--abi",
        "abi3",
        "--abi",
        "none",
        "--index-url",
        "https://pypi.org/simple",
        "demo==1.2.3",
    ]


def test_runner_receives_isolated_environment_and_timeout(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    runner = FakeRunner(write_one("demo-1.2.3-py3-none-any.whl"))
    WheelDownloader(tmp_path, runner=runner, timeout=timedelta(seconds=7)).download_one(
        package(), profile_cp311_arm64, tmp_path / "out", lambda: False
    )

    _argv, _cwd, timeout, env = runner.calls[0]
    assert timeout == timedelta(seconds=7)
    assert env == {
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    }


def test_falls_back_per_package_and_restarts_at_tsinghua(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    def action(argv: list[str], _cwd: Path) -> object:
        source = argv[argv.index("--index-url") + 1]
        pin = argv[-1]
        if source == "https://pypi.tuna.tsinghua.edu.cn/simple":
            return 1
        attempt = Path(argv[argv.index("--dest") + 1])
        name, version = pin.split("==")
        (attempt / f"{name}-{version}-py3-none-any.whl").write_bytes(name.encode())
        return None

    runner = FakeRunner(action)
    wheels = WheelDownloader(tmp_path, runner=runner).download(
        resolution(package("alpha", "1.0"), package("beta", "2.0")),
        profile_cp311_arm64,
        tmp_path / "out",
        lambda: False,
    )

    assert [wheel.source for wheel in wheels] == [PackageSource.ALIYUN, PackageSource.ALIYUN]
    assert [call[0][call[0].index("--index-url") + 1] for call in runner.calls] == [
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple",
    ]


def test_falls_through_to_pypi_after_two_failed_sources(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    runner = FakeRunner(
        lambda argv, _cwd: 1
        if argv[argv.index("--index-url") + 1] != "https://pypi.org/simple"
        else write_one("demo-1.2.3-py3-none-any.whl")(argv, _cwd)
    )

    wheel = WheelDownloader(tmp_path, runner=runner).download_one(
        package(), profile_cp311_arm64, tmp_path / "out", lambda: False
    )

    assert wheel.source == PackageSource.PYPI


def test_download_command_rejects_unknown_source_and_untrusted_destination(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        build_download_argv(package(), profile_cp311_arm64, "https://bad.test/simple", tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WheelDownloader(tmp_path, runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl"))).download_one(
            package(), profile_cp311_arm64, Path("relative"), lambda: False
        )


def test_destination_with_a_symlinked_ancestor_is_rejected(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl"))).download_one(
            package(), profile_cp311_arm64, linked_parent / "out", lambda: False
        )


def test_hash_and_byte_accounting_are_exact(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    wheel = WheelDownloader(tmp_path, runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl", b"abc"))).download_one(
        package(), profile_cp311_arm64, tmp_path / "out", lambda: False
    )

    assert wheel.byte_size == 3
    assert wheel.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.mark.parametrize(
    "limits",
    [DownloadLimits(max_packages=1), DownloadLimits(max_wheel_bytes=3), DownloadLimits(max_total_bytes=3)],
)
def test_limits_reject_excess_without_publishing(
    limits: DownloadLimits, profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    packages = (package("demo", "1.2.3"), package("other", "1.0")) if limits.max_packages == 1 else (package(),)
    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl", b"four")), limits=limits).download(
            resolution(*packages), profile_cp311_arm64, tmp_path / "out", lambda: False
        )
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


@pytest.mark.parametrize("kwargs", [{"max_packages": 0}, {"max_wheel_bytes": True}, {"max_total_bytes": 2**32}])
def test_custom_limits_must_be_positive_bounded_integers(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        DownloadLimits(**kwargs)  # type: ignore[arg-type]


def test_cancellation_cleans_up_interrupted_attempt(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    cancelled = iter((False, False, True))
    with pytest.raises(DownloadCancelledError):
        WheelDownloader(tmp_path, runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl"))).download_one(
            package(), profile_cp311_arm64, tmp_path / "out", lambda: next(cancelled)
        )
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_whole_download_failure_removes_only_wheels_published_by_this_call(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    preserved = destination / "preserved-1.0-py3-none-any.whl"
    preserved.write_bytes(b"do not remove")

    def action(argv: list[str], _cwd: Path) -> object:
        if argv[-1] == "beta==2.0":
            return 1
        return write_one("alpha-1.0-py3-none-any.whl")(argv, _cwd)

    with pytest.raises(WheelDownloadError):
        WheelDownloader(tmp_path, runner=FakeRunner(action)).download(
            resolution(package("alpha", "1.0"), package("beta", "2.0")),
            profile_cp311_arm64,
            destination,
            lambda: False,
        )
    assert preserved.read_bytes() == b"do not remove"
    assert not (destination / "alpha-1.0-py3-none-any.whl").exists()


@pytest.mark.parametrize(
    "filename",
    [
        "wrong-1.2.3-py3-none-any.whl",
        "demo-1.2.4-py3-none-any.whl",
        "demo-1.2.3-win_amd64.whl",
        "not-a-wheel.txt",
    ],
)
def test_mismatched_or_wrong_platform_output_is_terminal(
    filename: str, profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    runner = FakeRunner(write_one(filename))
    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=runner).download_one(
            package(), profile_cp311_arm64, tmp_path / "out", lambda: False
        )
    assert len(runner.calls) == 1


def test_multiple_output_symlink_and_hard_link_are_rejected(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    def action(argv: list[str], _cwd: Path) -> object:
        attempt = Path(argv[argv.index("--dest") + 1])
        (attempt / "demo-1.2.3-py3-none-any.whl").write_bytes(b"one")
        (attempt / "extra-1.0-py3-none-any.whl").write_bytes(b"two")
        return None

    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=FakeRunner(action)).download_one(
            package(), profile_cp311_arm64, tmp_path / "out", lambda: False
        )

    def symlink_action(argv: list[str], _cwd: Path) -> object:
        attempt = Path(argv[argv.index("--dest") + 1])
        target = attempt.parent.parent / "symlink-target"
        target.write_bytes(b"wheel")
        (attempt / "demo-1.2.3-py3-none-any.whl").symlink_to(target)
        return None

    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=FakeRunner(symlink_action)).download_one(
            package(), profile_cp311_arm64, tmp_path / "out2", lambda: False
        )

    def hard_link_action(argv: list[str], _cwd: Path) -> object:
        attempt = Path(argv[argv.index("--dest") + 1])
        target = attempt.parent.parent / "hard-link-target"
        target.write_bytes(b"wheel")
        os.link(target, attempt / "demo-1.2.3-py3-none-any.whl")
        return None

    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=FakeRunner(hard_link_action)).download_one(
            package(), profile_cp311_arm64, tmp_path / "out3", lambda: False
        )


def test_duplicate_case_insensitive_name_and_existing_file_are_never_overwritten(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    existing = destination / "Demo-1.2.3-py3-none-any.whl"
    existing.write_bytes(b"keep")
    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl"))).download_one(
            package(), profile_cp311_arm64, destination, lambda: False
        )
    assert existing.read_bytes() == b"keep"


@pytest.mark.parametrize("error", [ProcessExecutionError("no pip"), ProcessTimeoutError("", "")])
def test_subprocess_errors_try_next_source_then_raise(
    error: BaseException, profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    with pytest.raises(WheelDownloadError):
        WheelDownloader(tmp_path, runner=FakeRunner(lambda _argv, _cwd: error)).download_one(
            package(), profile_cp311_arm64, tmp_path / "out", lambda: False
        )
