from __future__ import annotations

import json
import ipaddress
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Protocol, TypeAlias
from urllib.parse import urlsplit

from packaging.utils import (
    InvalidName,
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from wheelforge_worker.parser.models import ParsedRequirements
from wheelforge_worker.process import (
    ProcessExecutionError,
    ProcessResult,
    ProcessRunner,
    ProcessTimeoutError,
)
from wheelforge_worker.target.models import TargetProfile

from wheelforge_worker.sources import resolver_source_url

from .models import (
    ArchiveHash,
    InvalidPipReportError,
    PipReportSchemaError,
    PipReportVersionError,
    ResolvedPackage,
    ResolutionResult,
    ResolverCommandError,
    ResolverMissingReportError,
    ResolverProcessError,
)


_MAX_INSTALL_ENTRIES = 2000
MAX_PIP_REPORT_BYTES = 8 * 1024 * 1024
MAX_RESOLVER_TIMEOUT = timedelta(minutes=10)
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 100_000
_MAX_JSON_OBJECT_FIELDS = 128
_MAX_JSON_LIST_ITEMS = 2000
_MAX_JSON_STRING_CHARS = 65_536
_MAX_PACKAGE_NAME_CHARS = 512
_MAX_VERSION_CHARS = 512
_MAX_ARTIFACT_URL_CHARS = 8192
_MAX_REQUIRES_DIST = 1000
_MAX_REQUIREMENT_CHARS = 8192
_MAX_REQUIRES_PYTHON_CHARS = 1024
_MAX_ARCHIVE_HASHES = 16
_MAX_HASH_ALGORITHM_CHARS = 64
_MAX_HASH_DIGEST_CHARS = 1024
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_PIP_ENVIRONMENT = {
    "PIP_CONFIG_FILE": os.devnull,
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
}

PipReportPayload: TypeAlias = (
    Mapping[str, object] | list[object] | str | bytes | int | float | bool | None | Path
)


class _Runner(Protocol):
    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: timedelta,
        env: dict[str, str],
        *,
        inherited_fds: tuple[int, ...] = (),
    ) -> ProcessResult: ...


def build_resolve_argv(
    requirements_path: Path,
    report_path: Path,
    profile: TargetProfile,
    source: str,
) -> list[str]:
    if not isinstance(requirements_path, Path) or not isinstance(report_path, Path):
        raise ValueError("resolver paths must be trusted Path values")
    if "\x00" in str(requirements_path) or "\x00" in str(report_path):
        raise ValueError("resolver paths must not contain NUL bytes")
    if profile.python_implementation != "CPYTHON":
        raise ValueError("only CPython target profiles are supported")

    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--report",
        str(report_path),
        "--only-binary=:all:",
        "--platform",
        profile.platform_tag,
        "--python-version",
        profile.python_version,
        "--implementation",
        "cp",
        *[option for abi in profile.abi_tags for option in ("--abi", abi)],
        "--index-url",
        _source_url(source),
        "-r",
        str(requirements_path),
    ]


def parse_pip_report(payload: PipReportPayload) -> ResolutionResult:
    report = _load_report(payload)
    version = report.get("version")
    if version == "1":
        report_version = "1"
    elif isinstance(version, int) and not isinstance(version, bool) and version == 1:
        report_version = "1"
    else:
        raise PipReportVersionError("unsupported pip report version")

    install = report.get("install")
    if not isinstance(install, list) or len(install) > _MAX_INSTALL_ENTRIES:
        raise PipReportSchemaError("install must be a bounded list")

    _validate_json_structure(report)
    packages: list[ResolvedPackage] = []
    seen: dict[str, int] = {}
    for entry in install:
        package = _parse_install_entry(entry)
        existing_index = seen.get(package.name)
        if existing_index is not None:
            existing = packages[existing_index]
            if not _same_observation(existing, package):
                raise PipReportSchemaError(
                    "duplicate package has conflicting observations"
                )
            packages[existing_index] = replace(
                existing, requested=existing.requested or package.requested
            )
            continue
        seen[package.name] = len(packages)
        packages.append(package)
    return ResolutionResult(report_version, tuple(packages))


class StrictResolver:
    def __init__(
        self,
        work_directory: Path,
        *,
        runner: _Runner | None = None,
        timeout: timedelta = timedelta(seconds=60),
        inherited_fds: tuple[int, ...] = (),
    ) -> None:
        if not isinstance(work_directory, Path) or not work_directory.is_dir():
            raise ValueError("work directory must be an existing directory")
        if not isinstance(timeout, timedelta):
            raise ValueError("resolver timeout must be a timedelta")
        if timeout.total_seconds() <= 0 or timeout > MAX_RESOLVER_TIMEOUT:
            raise ValueError("resolver timeout must be positive and at most 10 minutes")
        self._work_directory = work_directory
        self._runner = ProcessRunner() if runner is None else runner
        self._timeout = timeout
        self._inherited_fds = inherited_fds

    def resolve(
        self,
        parsed: ParsedRequirements,
        profile: TargetProfile,
        source: str,
    ) -> ResolutionResult:
        attempt_directory = Path(
            tempfile.mkdtemp(prefix=".resolve-", dir=self._work_directory)
        )
        try:
            attempt_directory.chmod(0o700)
            requirements_path = attempt_directory / "requirements.txt"
            report_path = attempt_directory / "report.json"
            _write_requirements_exclusive(requirements_path, parsed.normalized_text)
            argv = build_resolve_argv(
                requirements_path, report_path, profile, source
            )
            try:
                if self._inherited_fds:
                    result = self._runner.run(
                        argv,
                        attempt_directory,
                        self._timeout,
                        dict(_PIP_ENVIRONMENT),
                        inherited_fds=self._inherited_fds,
                    )
                else:
                    result = self._runner.run(
                        argv,
                        attempt_directory,
                        self._timeout,
                        dict(_PIP_ENVIRONMENT),
                    )
            except (ProcessTimeoutError, ProcessExecutionError) as error:
                raise ResolverProcessError("pip dry-run could not be executed") from error
            if result.return_code != 0:
                raise ResolverCommandError(result.return_code, result.stderr)
            try:
                report_status = report_path.lstat()
            except FileNotFoundError as error:
                raise ResolverMissingReportError(
                    "pip dry-run did not produce a report"
                ) from error
            except OSError as error:
                raise InvalidPipReportError("pip report could not be inspected") from error
            if not stat.S_ISREG(report_status.st_mode):
                raise InvalidPipReportError("pip report must be a regular file")
            return parse_pip_report(report_path)
        finally:
            shutil.rmtree(attempt_directory)


def _source_url(source: str) -> str:
    return resolver_source_url(source)


def _load_report(payload: PipReportPayload) -> Mapping[str, object]:
    if isinstance(payload, Path):
        raw: object = _read_report_path(payload)
    else:
        raw = payload
    if isinstance(raw, bytes):
        if len(raw) > MAX_PIP_REPORT_BYTES:
            raise InvalidPipReportError("pip report exceeds the byte limit")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvalidPipReportError("pip report is not UTF-8 JSON") from error
    if isinstance(raw, str):
        try:
            encoded_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise InvalidPipReportError("pip report is not valid UTF-8 text") from error
        if encoded_size > MAX_PIP_REPORT_BYTES:
            raise InvalidPipReportError("pip report exceeds the byte limit")
        try:
            raw = json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, RecursionError) as error:
            raise InvalidPipReportError("pip report is not valid JSON") from error
    if not isinstance(raw, Mapping):
        raise PipReportSchemaError("pip report must be a JSON object")
    return raw


def _parse_install_entry(entry: object) -> ResolvedPackage:
    if not isinstance(entry, Mapping):
        raise PipReportSchemaError("install entries must be objects")
    metadata = entry.get("metadata")
    download_info = entry.get("download_info")
    requested = entry.get("requested")
    if (
        not isinstance(metadata, Mapping)
        or not isinstance(download_info, Mapping)
        or not isinstance(requested, bool)
    ):
        raise PipReportSchemaError("install entry is missing required fields")

    name = _canonical_name(metadata.get("name"))
    version = _version(metadata.get("version"))
    artifact_url, wheel_filename = _artifact_url(download_info.get("url"))
    return ResolvedPackage(
        name=name,
        version=version,
        requested=requested,
        artifact_url=artifact_url,
        wheel_filename=wheel_filename,
        requires_dist=_string_tuple(metadata.get("requires_dist"), "requires_dist"),
        requires_python=_optional_string(metadata.get("requires_python"), "requires_python"),
        archive_hashes=_archive_hashes(download_info.get("archive_info")),
    )


def _canonical_name(value: object) -> str:
    name = _bounded_string(
        value, "metadata.name", _MAX_PACKAGE_NAME_CHARS, required=True
    )
    try:
        canonical = canonicalize_name(name, validate=True)
    except InvalidName as error:
        raise PipReportSchemaError("metadata.name must be valid") from error
    if not canonical:
        raise PipReportSchemaError("metadata.name must be valid")
    return canonical


def _version(value: object) -> Version:
    version = _bounded_string(
        value, "metadata.version", _MAX_VERSION_CHARS, required=True
    )
    try:
        return Version(version)
    except InvalidVersion as error:
        raise PipReportSchemaError("metadata.version must be valid") from error


def _artifact_url(value: object) -> tuple[str, str]:
    url = _bounded_string(
        value, "download_info.url", _MAX_ARTIFACT_URL_CHARS, required=True
    )
    if url != url.strip() or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in url
    ):
        raise PipReportSchemaError("download_info.url must be a safe URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise PipReportSchemaError("download_info.url must be well formed") from error
    if (
        parsed.scheme not in {"https", "file"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or (parsed.scheme == "https" and not _valid_https_host(hostname))
        or (parsed.scheme == "file" and parsed.netloc not in {"", "localhost"})
        or (parsed.scheme == "file" and not parsed.path.startswith("/"))
        or not parsed.path.lower().endswith(".whl")
    ):
        raise PipReportSchemaError("download_info.url must reference a wheel")
    filename = Path(parsed.path).name
    try:
        parse_wheel_filename(filename)
    except InvalidWheelFilename as error:
        raise PipReportSchemaError("wheel filename is invalid") from error
    return url, filename


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_REQUIRES_DIST:
        raise PipReportSchemaError(f"{field} must be a list of strings")
    return tuple(
        _bounded_string(item, field, _MAX_REQUIREMENT_CHARS) for item in value
    )


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, field, _MAX_REQUIRES_PYTHON_CHARS)


def _archive_hashes(value: object) -> tuple[ArchiveHash, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise PipReportSchemaError("archive_info must be an object")
    hashes = value.get("hashes", {})
    if not isinstance(hashes, Mapping) or len(hashes) > _MAX_ARCHIVE_HASHES:
        raise PipReportSchemaError("archive_info.hashes must be an object")
    parsed: dict[str, str] = {}
    for algorithm, digest in hashes.items():
        if (
            not isinstance(algorithm, str)
            or not algorithm
            or len(algorithm) > _MAX_HASH_ALGORITHM_CHARS
            or not isinstance(digest, str)
            or not digest
            or len(digest) > _MAX_HASH_DIGEST_CHARS
        ):
            raise PipReportSchemaError("archive hashes must be non-empty strings")
        parsed[algorithm] = digest
    legacy_hash = value.get("hash")
    if legacy_hash is not None:
        if (
            not isinstance(legacy_hash, str)
            or len(legacy_hash)
            > _MAX_HASH_ALGORITHM_CHARS + _MAX_HASH_DIGEST_CHARS + 1
            or "=" not in legacy_hash
        ):
            raise PipReportSchemaError("archive_info.hash must be algorithm=digest")
        algorithm, digest = legacy_hash.split("=", 1)
        if not algorithm or not digest:
            raise PipReportSchemaError("archive_info.hash must be algorithm=digest")
        if (
            len(algorithm) > _MAX_HASH_ALGORITHM_CHARS
            or len(digest) > _MAX_HASH_DIGEST_CHARS
        ):
            raise PipReportSchemaError("archive_info.hash exceeds its field limit")
        existing = parsed.setdefault(algorithm, digest)
        if existing != digest:
            raise PipReportSchemaError("archive hash values conflict")
    return tuple(ArchiveHash(algorithm, digest) for algorithm, digest in sorted(parsed.items()))


class _StrictJsonError(ValueError):
    pass


def _write_requirements_exclusive(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ResolverProcessError("requirements file could not be created") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read_report_path(path: Path) -> bytes:
    try:
        path_status = path.lstat()
    except OSError as error:
        raise InvalidPipReportError("pip report could not be inspected") from error
    if not stat.S_ISREG(path_status.st_mode):
        raise InvalidPipReportError("pip report must be a regular file")
    if path_status.st_size > MAX_PIP_REPORT_BYTES:
        raise InvalidPipReportError("pip report exceeds the byte limit")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InvalidPipReportError("pip report could not be opened safely") from error
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InvalidPipReportError("pip report must be a regular file")
        payload = stream.read(MAX_PIP_REPORT_BYTES + 1)
    if len(payload) > MAX_PIP_REPORT_BYTES:
        raise InvalidPipReportError("pip report exceeds the byte limit")
    return payload


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise _StrictJsonError(f"non-standard JSON constant: {value}")


def _validate_json_structure(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    aggregate_utf8_bytes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise PipReportSchemaError("pip report structure exceeds its limits")
        if isinstance(current, str):
            if len(current) > _MAX_JSON_STRING_CHARS:
                raise PipReportSchemaError("pip report string exceeds its limit")
            aggregate_utf8_bytes += _utf8_size(current)
        elif isinstance(current, Mapping):
            if len(current) > _MAX_JSON_OBJECT_FIELDS:
                raise PipReportSchemaError("pip report object has too many fields")
            for key, child in current.items():
                if not isinstance(key, str) or len(key) > _MAX_JSON_STRING_CHARS:
                    raise PipReportSchemaError("pip report object key is invalid")
                aggregate_utf8_bytes += _utf8_size(key)
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            if len(current) > _MAX_JSON_LIST_ITEMS:
                raise PipReportSchemaError("pip report list has too many items")
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise PipReportSchemaError("pip report number must be finite")
        elif current is None or isinstance(current, (bool, int)):
            continue
        else:
            raise PipReportSchemaError("pip report contains a non-JSON value")
        if aggregate_utf8_bytes > MAX_PIP_REPORT_BYTES:
            raise PipReportSchemaError("pip report exceeds the aggregate byte limit")


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise PipReportSchemaError("pip report string is not valid UTF-8") from error


def _same_observation(left: ResolvedPackage, right: ResolvedPackage) -> bool:
    return (
        left.version == right.version
        and left.artifact_url == right.artifact_url
        and left.wheel_filename == right.wheel_filename
        and left.requires_dist == right.requires_dist
        and left.requires_python == right.requires_python
        and left.archive_hashes == right.archive_hashes
    )


def _bounded_string(
    value: object, field: str, limit: int, *, required: bool = False
) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value):
        raise PipReportSchemaError(f"{field} must be a bounded string")
    return value


def _valid_https_host(hostname: str | None) -> bool:
    if hostname is None or len(hostname) > 253:
        return False
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return False
        return True
    if hostname.replace(".", "").isdigit():
        try:
            ipaddress.IPv4Address(hostname)
        except ValueError:
            return False
        return True
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return False
    return bool(ascii_hostname) and all(
        _HOST_LABEL.fullmatch(label) for label in ascii_hostname.split(".")
    )
