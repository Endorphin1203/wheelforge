from __future__ import annotations

import hashlib
import os
import secrets
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
_DIRECTORY_HANDLE_SUPPORTED = bool(
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.listdir in os.supports_fd
)


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


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


@dataclass(slots=True)
class _DestinationBinding:
    path: Path
    identity: _FileIdentity
    descriptor: int | None

    def revalidate(self) -> None:
        try:
            status = self.path.lstat()
        except OSError as error:
            raise DownloadValidationError("destination identity changed") from error
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or _identity(status) != self.identity
        ):
            raise DownloadValidationError("destination identity changed")
        if self.descriptor is not None:
            try:
                descriptor_status = os.fstat(self.descriptor)
            except OSError as error:
                raise DownloadValidationError("destination handle is invalid") from error
            if (
                not stat.S_ISDIR(descriptor_status.st_mode)
                or _identity(descriptor_status) != self.identity
            ):
                raise DownloadValidationError("destination handle identity changed")

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


@dataclass(slots=True)
class _AttemptBinding:
    name: str
    path: Path
    identity: _FileIdentity
    descriptor: int | None

    def revalidate(self, destination: _DestinationBinding) -> None:
        destination.revalidate()
        try:
            if destination.descriptor is not None:
                status = os.stat(
                    self.name,
                    dir_fd=destination.descriptor,
                    follow_symlinks=False,
                )
            else:
                status = self.path.lstat()
        except OSError as error:
            raise DownloadValidationError("download attempt identity changed") from error
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or _identity(status) != self.identity
        ):
            raise DownloadValidationError("download attempt identity changed")
        if self.descriptor is not None:
            try:
                descriptor_status = os.fstat(self.descriptor)
            except OSError as error:
                raise DownloadValidationError("download attempt handle is invalid") from error
            if (
                not stat.S_ISDIR(descriptor_status.st_mode)
                or _identity(descriptor_status) != self.identity
            ):
                raise DownloadValidationError("download attempt handle identity changed")

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


@dataclass(frozen=True, slots=True)
class _Publication:
    wheel: DownloadedWheel
    identity: _FileIdentity


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
        work_directory = work_directory.absolute()
        if _has_symlink_component(work_directory):
            raise ValueError("work directory must not contain symlinks")
        try:
            work_status = work_directory.lstat()
        except OSError as error:
            raise ValueError("work directory could not be inspected") from error
        if not stat.S_ISDIR(work_status.st_mode) or stat.S_ISLNK(work_status.st_mode):
            raise ValueError("work directory must not be a symlink")
        if os.name != "nt" and stat.S_IMODE(work_status.st_mode) & 0o077:
            raise ValueError("work directory must be private")
        if (
            hasattr(os, "geteuid")
            and hasattr(work_status, "st_uid")
            and work_status.st_uid != os.geteuid()
        ):
            raise ValueError("work directory must be owned by the Worker user")
        if not isinstance(timeout, timedelta) or not timedelta(0) < timeout <= _MAX_TIMEOUT:
            raise ValueError("download timeout must be positive and at most 10 minutes")
        if not isinstance(limits, DownloadLimits):
            raise ValueError("download limits are required")
        self._work_directory = work_directory
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
        staging = self._prepare_staging_root()
        try:
            destination_binding = self._prepare_destination(destination)
            published: list[_Publication] = []
            try:
                total = 0
                for package in packages:
                    _raise_if_cancelled(cancel)
                    publication = self._download_one(
                        package,
                        profile,
                        staging,
                        destination_binding,
                        cancel,
                        self._limits.max_total_bytes - total,
                    )
                    total += publication.wheel.byte_size
                    published.append(publication)
                return [publication.wheel for publication in published]
            except BaseException:
                for publication in reversed(published):
                    _unlink_if_owned(destination_binding, publication)
                raise
            finally:
                destination_binding.close()
        finally:
            staging.close()

    def download_one(
        self,
        package: ResolvedPackage,
        profile: TargetProfile,
        destination: Path,
        cancel: Callable[[], bool],
    ) -> DownloadedWheel:
        _validate_package(package)
        _validate_profile_and_cancel(profile, cancel)
        staging = self._prepare_staging_root()
        try:
            destination_binding = self._prepare_destination(destination)
            try:
                return self._download_one(
                    package,
                    profile,
                    staging,
                    destination_binding,
                    cancel,
                    self._limits.max_total_bytes,
                ).wheel
            finally:
                destination_binding.close()
        finally:
            staging.close()

    def _download_one(
        self,
        package: ResolvedPackage,
        profile: TargetProfile,
        staging: _DestinationBinding,
        destination: _DestinationBinding,
        cancel: Callable[[], bool],
        remaining_total: int,
    ) -> _Publication:
        _validate_package(package)
        if remaining_total <= 0:
            raise DownloadValidationError("total downloaded Wheel byte limit exceeded")
        errors: list[str] = []
        for source in SOURCE_ORDER:
            _raise_if_cancelled(cancel)
            staging.revalidate()
            destination.revalidate()
            attempt = _create_attempt(staging)
            try:
                attempt.revalidate(staging)
                destination.revalidate()
                argv = build_download_argv(package, profile, source, attempt.path)
                try:
                    result = self._runner.run(
                        argv, attempt.path, self._timeout, dict(_PIP_ENVIRONMENT)
                    )
                except (ProcessExecutionError, ProcessTimeoutError) as error:
                    attempt.revalidate(staging)
                    destination.revalidate()
                    errors.append(f"{source.value}: {error}")
                    continue
                attempt.revalidate(staging)
                destination.revalidate()
                if result.return_code != 0:
                    errors.append(f"{source.value}: pip exited with {result.return_code}")
                    continue
                _raise_if_cancelled(cancel)
                observed = _inspect_attempt_output(
                    attempt,
                    staging,
                    package,
                    profile,
                    min(self._limits.max_wheel_bytes, remaining_total),
                )
                _raise_if_cancelled(cancel)
                attempt.revalidate(staging)
                destination.revalidate()
                return _publish_wheel(
                    observed, staging, destination, source, cancel
                )
            finally:
                _remove_attempt(staging, attempt)
        detail = "; ".join(errors)[:4096]
        raise WheelDownloadError(
            f"could not download {package.name}=={package.version} from configured sources: {detail}"
        )

    def _prepare_destination(self, destination: Path) -> _DestinationBinding:
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
        return _bind_directory(destination, status, "destination")

    def _prepare_staging_root(self) -> _DestinationBinding:
        try:
            status = self._work_directory.lstat()
        except OSError as error:
            raise DownloadValidationError("staging root could not be inspected") from error
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or (os.name != "nt" and stat.S_IMODE(status.st_mode) & 0o077)
        ):
            raise DownloadValidationError("staging root is not private and stable")
        return _bind_directory(self._work_directory, status, "staging root")


def _bind_directory(
    path: Path, status: os.stat_result, label: str
) -> _DestinationBinding:
    identity = _identity(status)
    descriptor: int | None = None
    if _DIRECTORY_HANDLE_SUPPORTED:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            descriptor_status = os.fstat(descriptor)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise DownloadValidationError(f"{label} could not be bound safely") from error
        if (
            not stat.S_ISDIR(descriptor_status.st_mode)
            or _identity(descriptor_status) != identity
        ):
            os.close(descriptor)
            raise DownloadValidationError(f"{label} changed while being bound")
    binding = _DestinationBinding(path, identity, descriptor)
    try:
        binding.revalidate()
    except BaseException:
        binding.close()
        raise
    return binding


@dataclass(frozen=True, slots=True)
class _ObservedWheel:
    filename: str
    path: Path
    package: str
    version: Version
    tags: frozenset[Tag]
    byte_size: int
    sha256: str
    file_identity: _FileIdentity
    attempt: _AttemptBinding


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
    seen: set[str] = set()
    for package in packages:
        _validate_package(package)
        if package.name in seen:
            raise DownloadValidationError("resolution result contains duplicate packages")
        seen.add(package.name)
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


def _identity(status: os.stat_result) -> _FileIdentity:
    return _FileIdentity(status.st_dev, status.st_ino)


def _create_attempt(destination: _DestinationBinding) -> _AttemptBinding:
    if destination.descriptor is not None:
        for _ in range(100):
            name = f".download-{secrets.token_hex(8)}"
            try:
                os.mkdir(name, 0o700, dir_fd=destination.descriptor)
            except FileExistsError:
                continue
            except OSError as error:
                raise DownloadValidationError("download attempt could not be created") from error
            descriptor: int | None = None
            try:
                status = os.stat(
                    name,
                    dir_fd=destination.descriptor,
                    follow_symlinks=False,
                )
                flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(name, flags, dir_fd=destination.descriptor)
                descriptor_status = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(status.st_mode)
                    or stat.S_ISLNK(status.st_mode)
                    or _identity(status) != _identity(descriptor_status)
                ):
                    raise DownloadValidationError("download attempt changed while being bound")
                return _AttemptBinding(
                    name,
                    destination.path / name,
                    _identity(status),
                    descriptor,
                )
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    os.rmdir(name, dir_fd=destination.descriptor)
                except OSError:
                    pass
                raise
        raise DownloadValidationError("download attempt name allocation was exhausted")

    path = Path(tempfile.mkdtemp(prefix=".download-", dir=destination.path))
    try:
        path.chmod(0o700)
        status = path.lstat()
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        shutil.rmtree(path, ignore_errors=True)
        raise DownloadValidationError("download attempt must be a real directory")
    attempt = _AttemptBinding(path.name, path, _identity(status), None)
    attempt.revalidate(destination)
    return attempt


def _remove_attempt(
    destination: _DestinationBinding, attempt: _AttemptBinding
) -> None:
    try:
        if destination.descriptor is not None and attempt.descriptor is not None:
            try:
                status = os.stat(
                    attempt.name,
                    dir_fd=destination.descriptor,
                    follow_symlinks=False,
                )
                if _identity(status) != attempt.identity or not stat.S_ISDIR(status.st_mode):
                    return
                _clear_directory_handle(attempt.descriptor)
                status = os.stat(
                    attempt.name,
                    dir_fd=destination.descriptor,
                    follow_symlinks=False,
                )
                if _identity(status) == attempt.identity:
                    os.rmdir(attempt.name, dir_fd=destination.descriptor)
            except OSError:
                pass
            return

        try:
            attempt.revalidate(destination)
        except DownloadValidationError:
            return
        shutil.rmtree(attempt.path, ignore_errors=True)
    finally:
        attempt.close()


def _clear_directory_handle(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        try:
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode) and not stat.S_ISLNK(status.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
                child = os.open(name, flags, dir_fd=descriptor)
                try:
                    if _identity(os.fstat(child)) != _identity(status):
                        continue
                    _clear_directory_handle(child)
                finally:
                    os.close(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _identity(current) == _identity(status):
                    os.rmdir(name, dir_fd=descriptor)
            else:
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _identity(current) == _identity(status):
                    os.unlink(name, dir_fd=descriptor)
        except OSError:
            continue


def _inspect_attempt_output(
    attempt: _AttemptBinding,
    staging: _DestinationBinding,
    package: ResolvedPackage,
    profile: TargetProfile,
    maximum_bytes: int,
) -> _ObservedWheel:
    attempt.revalidate(staging)
    try:
        if attempt.descriptor is not None:
            names = os.listdir(attempt.descriptor)
        else:
            names = [entry.name for entry in attempt.path.iterdir()]
    except OSError as error:
        raise DownloadValidationError("download output could not be inspected") from error
    if len(names) != 1:
        raise DownloadValidationError("successful source attempt must produce exactly one Wheel")
    filename = names[0]
    path = attempt.path / filename
    if not _safe_filename(filename):
        raise DownloadValidationError("download output filename is unsafe")
    try:
        file_status = _stat_attempt_entry(attempt, filename)
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
    size, digest = _hash_regular_file(attempt, filename, file_status, maximum_bytes)
    attempt.revalidate(staging)
    current_status = _stat_attempt_entry(attempt, filename)
    if _identity(current_status) != _identity(file_status):
        raise DownloadValidationError("download output changed during inspection")
    return _ObservedWheel(
        filename,
        path,
        package.name,
        package.version,
        frozenset(tags),
        size,
        digest,
        _identity(file_status),
        attempt,
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


def _stat_attempt_entry(attempt: _AttemptBinding, filename: str) -> os.stat_result:
    if attempt.descriptor is not None:
        return os.stat(filename, dir_fd=attempt.descriptor, follow_symlinks=False)
    return (attempt.path / filename).lstat()


def _open_attempt_entry(attempt: _AttemptBinding, filename: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if attempt.descriptor is not None:
        return os.open(filename, flags, dir_fd=attempt.descriptor)
    return os.open(attempt.path / filename, flags)


def _hash_regular_file(
    attempt: _AttemptBinding,
    filename: str,
    expected: os.stat_result,
    maximum_bytes: int,
) -> tuple[int, str]:
    try:
        descriptor = _open_attempt_entry(attempt, filename)
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
    staging: _DestinationBinding,
    destination: _DestinationBinding,
    source: PackageSource,
    cancel: Callable[[], bool],
) -> _Publication:
    _raise_if_cancelled(cancel)
    observed.attempt.revalidate(staging)
    _reject_destination_collision(destination, observed.filename)
    destination.revalidate()
    final_path = destination.path / observed.filename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        if destination.descriptor is not None:
            descriptor = os.open(
                observed.filename,
                flags,
                0o600,
                dir_fd=destination.descriptor,
            )
        else:
            descriptor = os.open(final_path, flags, 0o600)
        published_identity = _identity(os.fstat(descriptor))
    except FileExistsError as error:
        raise DownloadValidationError("destination already contains this Wheel") from error
    except OSError as error:
        raise DownloadValidationError("Wheel could not be published") from error
    try:
        _copy_verified(observed, descriptor)
        destination.revalidate()
        published_status = _stat_destination_entry(destination, observed.filename)
        if (
            not stat.S_ISREG(published_status.st_mode)
            or stat.S_ISLNK(published_status.st_mode)
            or _identity(published_status) != published_identity
        ):
            raise DownloadValidationError("published Wheel identity changed")
    except BaseException:
        _unlink_destination_entry_if_identity(
            destination, observed.filename, published_identity
        )
        raise
    return _Publication(
        DownloadedWheel(
            observed.package,
            observed.version,
            observed.filename,
            final_path,
            source,
            observed.byte_size,
            observed.sha256,
            observed.tags,
        ),
        published_identity,
    )


def _reject_destination_collision(
    destination: _DestinationBinding, filename: str
) -> None:
    destination.revalidate()
    try:
        if destination.descriptor is not None:
            entries = os.listdir(destination.descriptor)
        else:
            entries = [entry.name for entry in destination.path.iterdir()]
        names = [name for name in entries if not name.startswith(".download-")]
    except OSError as error:
        raise DownloadValidationError("destination could not be inspected") from error
    if filename in names or filename.casefold() in {name.casefold() for name in names}:
        raise DownloadValidationError("destination contains a colliding Wheel filename")


def _copy_verified(observed: _ObservedWheel, descriptor: int) -> None:
    try:
        source_descriptor = _open_attempt_entry(
            observed.attempt, observed.filename
        )
    except OSError as error:
        os.close(descriptor)
        raise DownloadValidationError("download output could not be published safely") from error
    try:
        with os.fdopen(source_descriptor, "rb") as source, os.fdopen(descriptor, "wb") as target:
            source_status = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(source_status.st_mode)
                or source_status.st_nlink != 1
                or _identity(source_status) != observed.file_identity
            ):
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


def _stat_destination_entry(
    destination: _DestinationBinding, filename: str
) -> os.stat_result:
    if destination.descriptor is not None:
        return os.stat(
            filename,
            dir_fd=destination.descriptor,
            follow_symlinks=False,
        )
    return (destination.path / filename).lstat()


def _unlink_destination_entry_if_identity(
    destination: _DestinationBinding,
    filename: str,
    identity: _FileIdentity,
) -> None:
    try:
        if destination.descriptor is None:
            destination.revalidate()
        status = _stat_destination_entry(destination, filename)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or _identity(status) != identity
        ):
            return
        if destination.descriptor is not None:
            current = _stat_destination_entry(destination, filename)
            if _identity(current) == identity:
                os.unlink(filename, dir_fd=destination.descriptor)
        else:
            current = (destination.path / filename).lstat()
            if _identity(current) == identity:
                (destination.path / filename).unlink()
    except (FileNotFoundError, DownloadValidationError, OSError):
        return


def _unlink_if_owned(
    destination: _DestinationBinding, publication: _Publication
) -> None:
    _unlink_destination_entry_if_identity(
        destination, publication.wheel.filename, publication.identity
    )
