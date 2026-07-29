from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from packaging.version import Version

import wheelforge_worker.download.wheels as wheels_module
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


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_post_run_destination_symlink_swap_cannot_escape_bound_directory(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    destination = tmp_path / "out"
    moved_destination = tmp_path / "moved-out"
    escape = tmp_path / "escape"

    def action(argv: list[str], _cwd: Path) -> object:
        attempt = Path(argv[argv.index("--dest") + 1])
        destination.rename(moved_destination)
        escape.mkdir()
        destination.symlink_to(escape, target_is_directory=True)
        attempt.mkdir(exist_ok=True)
        (attempt / "demo-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
        return None

    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=FakeRunner(action)).download_one(
            package(), profile_cp311_arm64, destination, lambda: False
        )

    assert list(escape.rglob("*.whl")) == []
    assert not list(moved_destination.glob(".download-*"))
    assert not list(tmp_path.glob(".download-*"))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_destination_swap_is_rejected_by_cross_platform_fallback(
    profile_cp311_arm64: TargetProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "out"
    moved_destination = tmp_path / "moved-out"
    escape = tmp_path / "escape"
    monkeypatch.setattr(
        wheels_module, "_DIRECTORY_HANDLE_SUPPORTED", False, raising=False
    )

    def action(argv: list[str], _cwd: Path) -> object:
        attempt = Path(argv[argv.index("--dest") + 1])
        destination.rename(moved_destination)
        escape.mkdir()
        destination.symlink_to(escape, target_is_directory=True)
        attempt.mkdir(exist_ok=True)
        (attempt / "demo-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
        return None

    with pytest.raises(DownloadValidationError):
        WheelDownloader(tmp_path, runner=FakeRunner(action)).download_one(
            package(), profile_cp311_arm64, destination, lambda: False
        )

    assert list(escape.rglob("*.whl")) == []
    assert not list(tmp_path.glob(".download-*"))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_fallback_final_create_swap_leaves_no_wheel_in_replacement_destination(
    profile_cp311_arm64: TargetProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "out"
    moved_destination = tmp_path / "moved-out"
    escape = tmp_path / "escape"
    real_open = os.open
    swapped = False
    monkeypatch.setattr(
        wheels_module, "_DIRECTORY_HANDLE_SUPPORTED", False, raising=False
    )

    def swapping_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and Path(path).name == "demo-1.2.3-py3-none-any.whl"  # type: ignore[arg-type]
            and flags & os.O_EXCL
        ):
            destination.rename(moved_destination)
            escape.mkdir()
            destination.symlink_to(escape, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wheels_module.os, "open", swapping_open)

    with pytest.raises(DownloadValidationError):
        WheelDownloader(
            tmp_path,
            runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl")),
        ).download_one(package(), profile_cp311_arm64, destination, lambda: False)

    assert swapped is True
    assert list(escape.rglob("*.whl")) == []
    assert list(moved_destination.rglob("*.whl")) == []
    assert not list(tmp_path.glob(".download-*"))


def test_download_succeeds_through_cross_platform_path_fallback(
    profile_cp311_arm64: TargetProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wheels_module, "_DIRECTORY_HANDLE_SUPPORTED", False, raising=False
    )

    result = WheelDownloader(
        tmp_path,
        runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl")),
    ).download_one(package(), profile_cp311_arm64, tmp_path / "out", lambda: False)

    assert result.path.read_bytes() == b"wheel"


@pytest.mark.skipif(
    not wheels_module._DIRECTORY_HANDLE_SUPPORTED,
    reason="directory handles are unavailable",
)
def test_staging_and_destination_directory_handles_are_closed_independently(
    profile_cp311_arm64: TargetProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    destination = staging / "out"
    real_open = os.open
    captured: dict[Path, int] = {}

    def recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("dir_fd") is None and isinstance(path, (str, os.PathLike)):
            candidate = Path(path)
            if candidate in {staging, destination} and flags & os.O_DIRECTORY:
                captured[candidate] = descriptor
        return descriptor

    monkeypatch.setattr(wheels_module.os, "open", recording_open)

    WheelDownloader(
        staging,
        runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl")),
    ).download_one(package(), profile_cp311_arm64, destination, lambda: False)

    assert set(captured) == {staging, destination}
    for descriptor in captured.values():
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(
    not wheels_module._DIRECTORY_HANDLE_SUPPORTED,
    reason="directory handles are unavailable",
)
def test_staging_handle_is_closed_when_initial_identity_revalidation_fails(
    profile_cp311_arm64: TargetProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    moved_staging = tmp_path / "moved-staging"
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    real_open = os.open
    real_fstat = os.fstat
    staging_descriptor: int | None = None
    swapped = False

    def recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal staging_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("dir_fd") is None and Path(path) == staging:  # type: ignore[arg-type]
            staging_descriptor = descriptor
        return descriptor

    def swapping_fstat(descriptor: int) -> os.stat_result:
        nonlocal swapped
        status = real_fstat(descriptor)
        if descriptor == staging_descriptor and not swapped:
            staging.rename(moved_staging)
            staging.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return status

    monkeypatch.setattr(wheels_module.os, "open", recording_open)
    monkeypatch.setattr(wheels_module.os, "fstat", swapping_fstat)

    with pytest.raises(DownloadValidationError, match="identity changed"):
        WheelDownloader(
            staging,
            runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl")),
        ).download_one(package(), profile_cp311_arm64, tmp_path / "out", lambda: False)

    assert staging_descriptor is not None
    with pytest.raises(OSError):
        real_fstat(staging_descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX private mode check")
def test_worker_staging_root_must_be_private(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o755)

    with pytest.raises(ValueError, match="private"):
        WheelDownloader(
            staging,
            runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl")),
        )


def test_worker_staging_root_must_not_use_a_symlink(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    linked_staging = tmp_path / "linked-staging"
    linked_staging.symlink_to(staging, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        WheelDownloader(
            linked_staging,
            runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl")),
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


def test_rollback_preserves_a_replacement_of_an_invocation_published_wheel(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    destination = tmp_path / "out"
    replacement = b"replacement owned by another actor"
    replaced = False

    def action(argv: list[str], _cwd: Path) -> object:
        nonlocal replaced
        if argv[-1] == "alpha==1.0":
            return write_one("alpha-1.0-py3-none-any.whl")(argv, _cwd)
        if not replaced:
            published = destination / "alpha-1.0-py3-none-any.whl"
            published.unlink(missing_ok=True)
            published.write_bytes(replacement)
            replaced = True
        return 1

    with pytest.raises(WheelDownloadError):
        WheelDownloader(tmp_path, runner=FakeRunner(action)).download(
            resolution(package("alpha", "1.0"), package("beta", "2.0")),
            profile_cp311_arm64,
            destination,
            lambda: False,
        )

    assert (destination / "alpha-1.0-py3-none-any.whl").read_bytes() == replacement


def test_publication_failure_preserves_a_replacement_file(
    profile_cp311_arm64: TargetProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "out"
    replacement = b"replacement after exclusive create"

    def replace_then_fail(observed: object, descriptor: int) -> None:
        final_path = destination / "demo-1.2.3-py3-none-any.whl"
        final_path.unlink(missing_ok=True)
        final_path.write_bytes(replacement)
        raise DownloadValidationError("injected copy failure")

    monkeypatch.setattr(wheels_module, "_copy_verified", replace_then_fail)

    with pytest.raises(DownloadValidationError, match="injected copy failure"):
        WheelDownloader(
            tmp_path,
            runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl")),
        ).download_one(package(), profile_cp311_arm64, destination, lambda: False)

    assert (destination / "demo-1.2.3-py3-none-any.whl").read_bytes() == replacement


@pytest.mark.skipif(
    not wheels_module._DIRECTORY_HANDLE_SUPPORTED,
    reason="directory handles are unavailable",
)
def test_rollback_final_unlink_cannot_delete_a_replacement(
    profile_cp311_arm64: TargetProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "out"
    replacement = b"replacement at final unlink boundary"
    real_unlink = os.unlink
    real_fstat = os.fstat
    replaced = False

    def action(argv: list[str], cwd: Path) -> object:
        nonlocal replaced
        if argv[-1] == "alpha==1.0":
            return write_one("alpha-1.0-py3-none-any.whl")(argv, cwd)
        final_path = destination / "alpha-1.0-py3-none-any.whl"
        if not final_path.exists():
            final_path.write_bytes(replacement)
            replaced = True
        return 1

    def replacing_unlink(
        path: object, *args: object, **kwargs: object
    ) -> None:
        nonlocal replaced
        directory_descriptor = kwargs.get("dir_fd")
        is_destination = False
        if directory_descriptor is not None and destination.exists():
            directory_status = real_fstat(int(directory_descriptor))
            destination_status = destination.lstat()
            is_destination = (
                directory_status.st_dev == destination_status.st_dev
                and directory_status.st_ino == destination_status.st_ino
            )
        if (
            not replaced
            and Path(path).name == "alpha-1.0-py3-none-any.whl"  # type: ignore[arg-type]
            and is_destination
        ):
            real_unlink(destination / "alpha-1.0-py3-none-any.whl")
            (destination / "alpha-1.0-py3-none-any.whl").write_bytes(replacement)
            replaced = True
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wheels_module.os, "unlink", replacing_unlink)

    with pytest.raises(WheelDownloadError):
        WheelDownloader(
            tmp_path,
            runner=FakeRunner(action),
        ).download(
            resolution(package("alpha", "1.0"), package("beta", "2.0")),
            profile_cp311_arm64,
            destination,
            lambda: False,
        )

    assert replaced is True
    assert (destination / "alpha-1.0-py3-none-any.whl").read_bytes() == replacement


def test_publication_fstat_failure_closes_descriptor_and_leaves_no_artifact(
    profile_cp311_arm64: TargetProfile,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "out"
    real_open = os.open
    real_fstat = os.fstat
    publication_descriptor: int | None = None

    def recording_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal publication_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if Path(path).name == "demo-1.2.3-py3-none-any.whl":  # type: ignore[arg-type]
            publication_descriptor = descriptor
        return descriptor

    def failing_fstat(descriptor: int) -> os.stat_result:
        if descriptor == publication_descriptor:
            raise OSError("injected publication fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(wheels_module.os, "open", recording_open)
    monkeypatch.setattr(wheels_module.os, "fstat", failing_fstat)

    with pytest.raises(DownloadValidationError):
        WheelDownloader(
            tmp_path,
            runner=FakeRunner(write_one("demo-1.2.3-py3-none-any.whl")),
        ).download_one(package(), profile_cp311_arm64, destination, lambda: False)

    assert publication_descriptor is not None
    with pytest.raises(OSError):
        real_fstat(publication_descriptor)
    assert not destination.exists()
    assert list(tmp_path.rglob("*.whl")) == []


def test_duplicate_canonical_package_name_across_versions_is_rejected_before_download(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    runner = FakeRunner(lambda _argv, _cwd: AssertionError("runner must not be called"))

    with pytest.raises(DownloadValidationError, match="duplicate"):
        WheelDownloader(tmp_path, runner=runner).download(
            resolution(package("demo", "1.0"), package("demo", "2.0")),
            profile_cp311_arm64,
            tmp_path / "out",
            lambda: False,
        )

    assert runner.calls == []


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
