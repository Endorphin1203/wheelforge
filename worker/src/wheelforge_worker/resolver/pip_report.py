from __future__ import annotations

import json
import sys
from collections.abc import Mapping
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


_SOURCES = {
    "TSINGHUA": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "ALIYUN": "https://mirrors.aliyun.com/pypi/simple",
    "PYPI": "https://pypi.org/simple",
}
_SOURCE_URLS = frozenset(_SOURCES.values())
_MAX_INSTALL_ENTRIES = 2000
_PIP_ENVIRONMENT = {"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}

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

    packages: list[ResolvedPackage] = []
    seen: dict[str, ResolvedPackage] = {}
    for entry in install:
        package = _parse_install_entry(entry)
        existing = seen.get(package.name)
        if existing is not None:
            if (
                existing.version != package.version
                or existing.artifact_url != package.artifact_url
            ):
                raise PipReportSchemaError("duplicate package has conflicting artifacts")
            continue
        seen[package.name] = package
        packages.append(package)
    return ResolutionResult(report_version, tuple(packages))


class StrictResolver:
    def __init__(
        self,
        work_directory: Path,
        *,
        runner: _Runner | None = None,
        timeout: timedelta = timedelta(seconds=60),
    ) -> None:
        if not isinstance(work_directory, Path) or not work_directory.is_dir():
            raise ValueError("work directory must be an existing directory")
        if timeout.total_seconds() <= 0:
            raise ValueError("resolver timeout must be positive")
        self._work_directory = work_directory.resolve()
        self._runner = ProcessRunner() if runner is None else runner
        self._timeout = timeout

    def resolve(
        self,
        parsed: ParsedRequirements,
        profile: TargetProfile,
        source: str,
    ) -> ResolutionResult:
        requirements_path = self._work_directory / "requirements.txt"
        report_path = self._work_directory / "report.json"
        requirements_path.write_text(parsed.normalized_text, encoding="utf-8")
        report_path.unlink(missing_ok=True)
        argv = build_resolve_argv(requirements_path, report_path, profile, source)
        try:
            result = self._runner.run(
                argv, self._work_directory, self._timeout, dict(_PIP_ENVIRONMENT)
            )
        except (ProcessTimeoutError, ProcessExecutionError) as error:
            raise ResolverProcessError("pip dry-run could not be executed") from error
        if result.return_code != 0:
            raise ResolverCommandError(result.return_code, result.stderr)
        if not report_path.is_file():
            raise ResolverMissingReportError("pip dry-run did not produce a report")
        return parse_pip_report(report_path)


def _source_url(source: str) -> str:
    if not isinstance(source, str):
        raise ValueError("source must be a builtin source code or URL")
    if source in _SOURCES:
        return _SOURCES[source]
    if source in _SOURCE_URLS:
        return source
    raise ValueError("source is not a builtin package index")


def _load_report(payload: PipReportPayload) -> Mapping[str, object]:
    if isinstance(payload, Path):
        try:
            raw: object = payload.read_text(encoding="utf-8")
        except OSError as error:
            raise InvalidPipReportError("pip report could not be read") from error
    else:
        raw = payload
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvalidPipReportError("pip report is not UTF-8 JSON") from error
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
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
    if not isinstance(value, str) or not value.strip():
        raise PipReportSchemaError("metadata.name must be a non-empty string")
    try:
        canonical = canonicalize_name(value, validate=True)
    except InvalidName as error:
        raise PipReportSchemaError("metadata.name must be valid") from error
    if not canonical:
        raise PipReportSchemaError("metadata.name must be valid")
    return canonical


def _version(value: object) -> Version:
    if not isinstance(value, str) or not value:
        raise PipReportSchemaError("metadata.version must be a non-empty string")
    try:
        return Version(value)
    except InvalidVersion as error:
        raise PipReportSchemaError("metadata.version must be valid") from error


def _artifact_url(value: object) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise PipReportSchemaError("download_info.url must be a safe URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "file"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or (parsed.scheme == "https" and not parsed.hostname)
        or (parsed.scheme == "file" and parsed.netloc not in {"", "localhost"})
        or not parsed.path.lower().endswith(".whl")
    ):
        raise PipReportSchemaError("download_info.url must reference a wheel")
    filename = Path(parsed.path).name
    try:
        parse_wheel_filename(filename)
    except InvalidWheelFilename as error:
        raise PipReportSchemaError("wheel filename is invalid") from error
    return value, filename


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PipReportSchemaError(f"{field} must be a list of strings")
    return tuple(value)


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PipReportSchemaError(f"{field} must be a string")
    return value


def _archive_hashes(value: object) -> tuple[ArchiveHash, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise PipReportSchemaError("archive_info must be an object")
    hashes = value.get("hashes", {})
    if not isinstance(hashes, Mapping):
        raise PipReportSchemaError("archive_info.hashes must be an object")
    parsed: dict[str, str] = {}
    for algorithm, digest in hashes.items():
        if (
            not isinstance(algorithm, str)
            or not algorithm
            or not isinstance(digest, str)
            or not digest
        ):
            raise PipReportSchemaError("archive hashes must be non-empty strings")
        parsed[algorithm] = digest
    legacy_hash = value.get("hash")
    if legacy_hash is not None:
        if not isinstance(legacy_hash, str) or "=" not in legacy_hash:
            raise PipReportSchemaError("archive_info.hash must be algorithm=digest")
        algorithm, digest = legacy_hash.split("=", 1)
        if not algorithm or not digest:
            raise PipReportSchemaError("archive_info.hash must be algorithm=digest")
        parsed.setdefault(algorithm, digest)
    return tuple(ArchiveHash(algorithm, digest) for algorithm, digest in sorted(parsed.items()))
