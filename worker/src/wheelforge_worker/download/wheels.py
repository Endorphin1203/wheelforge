from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from packaging.tags import Tag
from packaging.utils import InvalidName, InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import Version

from wheelforge_worker.process import (
    ProcessExecutionError,
    ProcessResult,
    ProcessRunner,
    ProcessTimeoutError,
)
from wheelforge_worker.resolver.models import PackageSource, ResolvedPackage, ResolutionResult
from wheelforge_worker.target import TargetProfile, wheel_is_compatible

from .sources import SOURCE_ORDER, source_url


_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_PIP_ENVIRONMENT = {
    "PIP_CONFIG_FILE": os.devnull,
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
}
_MAX_TIMEOUT = timedelta(minutes=10)
_COPY_CHUNK_BYTES = 64 * 1024


def _require_limit(name: str, value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be a positive bounded integer")


class _Runner(Protocol):
    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: timedelta,
        env: dict[str, str],
    ) -> ProcessResult: ...


@dataclass(frozen=True, slots=True)
class DownloadLimits:
    max_packages: int = 500
    max_wheel_bytes: int = 512 * _MIB
    max_total_bytes: int = 2 * _GIB

    def __post_init__(self) -> None:
        _require_limit("max_packages", self.max_packages, 500)
        _require_limit("max_wheel_bytes", self.max_wheel_bytes, 512 * _MIB)
        _require_limit("max_total_bytes", self.max_total_bytes, 2 * _GIB)


@dataclass(frozen=True, slots=True)
class DownloadedWheel:
    package: str
    version: Version
    filename: str
    path: Path
    source: PackageSource
    byte_size: int
    sha256: str
    tags: frozenset[Tag]


class WheelDownloadError(RuntimeError):
    """A Wheel could not be acquired from any configured source."""


class DownloadCancelledError(WheelDownloadError):
    pass


class DownloadValidationError(WheelDownloadError):
    pass


class WheelDownloader:
    def __init__(
        self,
        work_directory: Path,
        *,
        runner: _Runner | None = None,
        timeout: timedelta = timedelta(seconds=60),
        limits: DownloadLimits = DownloadLimits(),
    ) -> None:
        if not isinstance(work_directory, Path) or not work_directory.is_dir():
            raise ValueError("work directory must be an existing directory")
        if not isinstance(timeout, timedelta) or not timedelta(0) < timeout <= _MAX_TIMEOUT:
            raise ValueError("download timeout must be positive and at most 10 minutes")
        if not isinstance(limits, DownloadLimits):
            raise ValueError("download limits are required")
        self._work_directory = work_directory.resolve()
        self._runner = ProcessRunner() if runner is None else runner
        self._timeout = timeout
        self._limits = limits

    def download(
        self,
        resolved: ResolutionResult,
        profile: TargetProfile,
        destination: Path,
        cancel: Callable[[], bool],
    ) -> list[DownloadedWheel]:
        packages = _resolved_packages(resolved)
        _validate_profile_and_cancel(profile, cancel)
        if len(packages) > self._limits.max_packages:
            raise DownloadValidationError("resolved package count exceeds the limit")
        destination = self._prepare_destination(destination)
        published: list[DownloadedWheel] = []
        total = 0
        try:
            for package in packages:
                _raise_if_cancelled(cancel)
                wheel = self._download_one(
                    package,
                    profile,
                    destination,
                    cancel,
                    self._limits.max_total_bytes - total,
                )
                total += wheel.byte_size
                published.append(wheel)
            return published
        except BaseException:
            for wheel in published:
                _unlink_if_same_regular_file(wheel.path)
            raise

    def download_one(
        self,
        package: ResolvedPackage,
        profile: TargetProfile,
        destination: Path,
        cancel: Callable[[], bool],
    ) -> DownloadedWheel:
        _validate_package(package)
        _validate_profile_and_cancel(profile, cancel)
        destination = self._prepare_destination(destination)
        return self._download_one(
            package, profile, destination, cancel, self._limits.max_total_bytes
        )

    def _download_one(
        self,
        package: ResolvedPackage,
        profile: TargetProfile,
        destination: Path,
        cancel: Callable[[], bool],
        remaining_total: int,
    ) -> DownloadedWheel:
        _validate_package(package)
        if remaining_total <= 0:
            raise DownloadValidationError("total downloaded Wheel byte limit exceeded")
        errors: list[str] = []
        for source in SOURCE_ORDER:
            _raise_if_cancelled(cancel)
            attempt = Path(tempfile.mkdtemp(prefix=".download-", dir=destination))
            try:
                attempt.chmod(0o700)
                argv = build_download_argv(package, profile, source, attempt)
                try:
                    result = self._runner.run(
                        argv, attempt, self._timeout, dict(_PIP_ENVIRONMENT)
                    )
                except (ProcessExecutionError, ProcessTimeoutError) as error:
                    errors.append(f"{source.value}: {error}")
                    continue
                if result.return_code != 0:
                    errors.append(f"{source.value}: pip exited with {result.return_code}")
                    continue
                _raise_if_cancelled(cancel)
                observed = _inspect_attempt_output(
                    attempt,
                    package,
                    profile,
                    min(self._limits.max_wheel_bytes, remaining_total),
                )
                _raise_if_cancelled(cancel)
                return _publish_wheel(observed, destination, source, cancel)
            finally:
                shutil.rmtree(attempt, ignore_errors=True)
        detail = "; ".join(errors)[:4096]
        raise WheelDownloadError(
            f"could not download {package.name}=={package.version} from configured sources: {detail}"
        )

    def _prepare_destination(self, destination: Path) -> Path:
        if not isinstance(destination, Path) or "\x00" in str(destination):
            raise ValueError("destination must be a trusted Path")
        if not destination.is_absolute():
            raise ValueError("destination must be an absolute trusted Path")
        parent = destination.parent
        if not parent.is_dir() or _has_symlink_component(parent):
            raise DownloadValidationError("destination parent must be a real directory")
        try:
            destination.mkdir(mode=0o700, exist_ok=True)
            status = destination.lstat()
        except OSError as error:
            raise DownloadValidationError("destination could not be prepared") from error
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise DownloadValidationError("destination must be a real directory")
        return destination


@dataclass(frozen=True, slots=True)
class _ObservedWheel:
    filename: str
    path: Path
    package: str
    version: Version
    tags: frozenset[Tag]
    byte_size: int
    sha256: str


def build_download_argv(
    package: ResolvedPackage,
    profile: TargetProfile,
    source: PackageSource,
    destination: Path,
) -> list[str]:
    _validate_package(package)
    if not isinstance(profile, TargetProfile) or profile.python_implementation != "CPYTHON":
        raise ValueError("only CPython target profiles are supported")
    if not isinstance(destination, Path) or "\x00" in str(destination):
        raise ValueError("download destination must be a trusted Path")
    return [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--no-deps",
        "--only-binary=:all:",
        "--dest",
        str(destination),
        "--platform",
        profile.platform_tag,
        "--python-version",
        profile.python_version,
        "--implementation",
        "cp",
        *[option for abi in profile.abi_tags for option in ("--abi", abi)],
        "--index-url",
        source_url(source),
        f"{package.name}=={package.version}",
    ]


def _resolved_packages(resolved: ResolutionResult) -> tuple[ResolvedPackage, ...]:
    if not isinstance(resolved, ResolutionResult):
        raise ValueError("resolution result is required")
    packages = tuple(resolved.packages)
    seen: set[tuple[str, Version]] = set()
    for package in packages:
        _validate_package(package)
        key = (package.name, package.version)
        if key in seen:
            raise DownloadValidationError("resolution result contains duplicate packages")
        seen.add(key)
    return packages


def _validate_package(package: ResolvedPackage) -> None:
    if not isinstance(package, ResolvedPackage):
        raise ValueError("resolved package is required")
    try:
        canonical = canonicalize_name(package.name, validate=True)
    except InvalidName as error:
        raise ValueError("resolved package name is invalid") from error
    if canonical != package.name or not isinstance(package.version, Version):
        raise ValueError("resolved package name or version is invalid")


def _validate_profile_and_cancel(profile: TargetProfile, cancel: Callable[[], bool]) -> None:
    if not isinstance(profile, TargetProfile):
        raise ValueError("target profile is required")
    if not callable(cancel):
        raise ValueError("cancel must be callable")


def _raise_if_cancelled(cancel: Callable[[], bool]) -> None:
    if cancel():
        raise DownloadCancelledError("download cancelled")


def _inspect_attempt_output(
    attempt: Path,
    package: ResolvedPackage,
    profile: TargetProfile,
    maximum_bytes: int,
) -> _ObservedWheel:
    try:
        entries = list(attempt.iterdir())
    except OSError as error:
        raise DownloadValidationError("download output could not be inspected") from error
    if len(entries) != 1:
        raise DownloadValidationError("successful source attempt must produce exactly one Wheel")
    path = entries[0]
    filename = path.name
    if not _safe_filename(filename):
        raise DownloadValidationError("download output filename is unsafe")
    try:
        file_status = path.lstat()
    except OSError as error:
        raise DownloadValidationError("download output could not be inspected") from error
    if (
        not stat.S_ISREG(file_status.st_mode)
        or stat.S_ISLNK(file_status.st_mode)
        or file_status.st_nlink != 1
        or not filename.lower().endswith(".whl")
    ):
        raise DownloadValidationError("download output must be one non-linked regular Wheel")
    try:
        parsed_name, parsed_version, _build, tags = parse_wheel_filename(filename)
    except InvalidWheelFilename as error:
        raise DownloadValidationError("download output Wheel filename is invalid") from error
    if canonicalize_name(parsed_name) != package.name or parsed_version != package.version:
        raise DownloadValidationError("download output does not match the resolved package")
    if not wheel_is_compatible(filename, profile):
        raise DownloadValidationError("download output is incompatible with the target")
    size, digest = _hash_regular_file(path, file_status, maximum_bytes)
    return _ObservedWheel(
        filename, path, package.name, package.version, frozenset(tags), size, digest
    )


def _safe_filename(filename: str) -> bool:
    return (
        bool(filename)
        and filename == Path(filename).name
        and filename not in {".", ".."}
        and "\x00" not in filename
        and "/" not in filename
        and "\\" not in filename
        and not Path(filename).is_absolute()
    )


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            status = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(status.st_mode):
            return True
    return False


def _hash_regular_file(path: Path, expected: os.stat_result, maximum_bytes: int) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DownloadValidationError("download output could not be opened safely") from error
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "rb") as stream:
            actual = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1
                or actual.st_ino != expected.st_ino
                or actual.st_dev != expected.st_dev
            ):
                raise DownloadValidationError("download output changed during inspection")
            while chunk := stream.read(_COPY_CHUNK_BYTES):
                size += len(chunk)
                if size > maximum_bytes:
                    raise DownloadValidationError("downloaded Wheel exceeds the byte limit")
                digest.update(chunk)
    except DownloadValidationError:
        raise
    except OSError as error:
        raise DownloadValidationError("download output could not be read safely") from error
    return size, digest.hexdigest()


def _publish_wheel(
    observed: _ObservedWheel,
    destination: Path,
    source: PackageSource,
    cancel: Callable[[], bool],
) -> DownloadedWheel:
    _raise_if_cancelled(cancel)
    _reject_destination_collision(destination, observed.filename)
    final_path = destination / observed.filename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(final_path, flags, 0o600)
    except FileExistsError as error:
        raise DownloadValidationError("destination already contains this Wheel") from error
    except OSError as error:
        raise DownloadValidationError("Wheel could not be published") from error
    try:
        _copy_verified(observed, descriptor)
    except BaseException:
        final_path.unlink(missing_ok=True)
        raise
    return DownloadedWheel(
        observed.package,
        observed.version,
        observed.filename,
        final_path,
        source,
        observed.byte_size,
        observed.sha256,
        observed.tags,
    )


def _reject_destination_collision(destination: Path, filename: str) -> None:
    try:
        names = [entry.name for entry in destination.iterdir() if not entry.name.startswith(".download-")]
    except OSError as error:
        raise DownloadValidationError("destination could not be inspected") from error
    if filename in names or filename.casefold() in {name.casefold() for name in names}:
        raise DownloadValidationError("destination contains a colliding Wheel filename")


def _copy_verified(observed: _ObservedWheel, descriptor: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(observed.path, flags)
    except OSError as error:
        os.close(descriptor)
        raise DownloadValidationError("download output could not be published safely") from error
    try:
        with os.fdopen(source_descriptor, "rb") as source, os.fdopen(descriptor, "wb") as target:
            source_status = os.fstat(source.fileno())
            if not stat.S_ISREG(source_status.st_mode) or source_status.st_nlink != 1:
                raise DownloadValidationError("download output changed before publication")
            copied = 0
            digest = hashlib.sha256()
            while chunk := source.read(_COPY_CHUNK_BYTES):
                copied += len(chunk)
                digest.update(chunk)
                target.write(chunk)
            if copied != observed.byte_size or digest.hexdigest() != observed.sha256:
                raise DownloadValidationError("download output changed before publication")
    except DownloadValidationError:
        raise
    except OSError as error:
        raise DownloadValidationError("Wheel could not be published") from error


def _unlink_if_same_regular_file(path: Path) -> None:
    try:
        status = path.lstat()
        if stat.S_ISREG(status.st_mode) and not stat.S_ISLNK(status.st_mode):
            path.unlink()
    except FileNotFoundError:
        pass
