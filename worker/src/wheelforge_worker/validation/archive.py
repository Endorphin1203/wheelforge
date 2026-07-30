from __future__ import annotations

import base64
import csv
import hashlib
import io
import math
import os
import re
import stat
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import BinaryIO

from packaging.tags import Tag, parse_tag
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from wheelforge_worker.download import DownloadedWheel


_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_DEFAULT_MAX_ENTRIES = 20_000
_DEFAULT_MAX_TOTAL_BYTES = 2 * _GIB
_DEFAULT_MAX_ENTRY_BYTES = 512 * _MIB
_DEFAULT_MAX_COMPRESSION_RATIO = 200
_MAX_HEADER_BYTES = 4 * _MIB
_RECORD_FIELD_BYTES = 4096
_SHA256_RECORD_DIGEST = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class WheelArchiveValidationError(RuntimeError):
    """Base class for read-only Wheel archive validation failures."""


class UnsafeWheelArchive(WheelArchiveValidationError):
    """The Wheel file or an archive member is unsafe to package."""


class ArchiveResourceLimitError(WheelArchiveValidationError):
    """The Wheel exceeds an archive resource limit."""


class ArchiveMetadataError(WheelArchiveValidationError):
    """The Wheel's dist-info metadata is missing, malformed, or inconsistent."""


class WheelRecordMismatch(WheelArchiveValidationError):
    """The Wheel RECORD does not account for its archive files faithfully."""


def _bounded_positive_integer(name: str, value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be a positive bounded integer")


def _bounded_ratio(name: str, value: float | int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite bounded number")
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{name} must be a positive finite bounded number")


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_entries: int = _DEFAULT_MAX_ENTRIES
    max_total_uncompressed_bytes: int = _DEFAULT_MAX_TOTAL_BYTES
    max_entry_uncompressed_bytes: int = _DEFAULT_MAX_ENTRY_BYTES
    max_compression_ratio: float | int = _DEFAULT_MAX_COMPRESSION_RATIO

    def __post_init__(self) -> None:
        _bounded_positive_integer("max_entries", self.max_entries, _DEFAULT_MAX_ENTRIES)
        _bounded_positive_integer(
            "max_total_uncompressed_bytes",
            self.max_total_uncompressed_bytes,
            _DEFAULT_MAX_TOTAL_BYTES,
        )
        _bounded_positive_integer(
            "max_entry_uncompressed_bytes",
            self.max_entry_uncompressed_bytes,
            _DEFAULT_MAX_ENTRY_BYTES,
        )
        _bounded_ratio("max_compression_ratio", self.max_compression_ratio, _DEFAULT_MAX_COMPRESSION_RATIO)


@dataclass(frozen=True, slots=True)
class ArchiveValidationReport:
    name: str
    version: Version
    requires_dist: tuple[str, ...]
    requires_python: str | None
    tags: frozenset[Tag]
    entry_count: int
    total_uncompressed_bytes: int
    validation_level: str = "STATIC"
    install_verified: bool = False

    def __post_init__(self) -> None:
        if self.validation_level != "STATIC" or self.install_verified is not False:
            raise ValueError("archive validation reports cannot claim installation verification")
        if not self.name or not isinstance(self.version, Version):
            raise ValueError("archive report identity is invalid")
        if self.entry_count < 0 or self.total_uncompressed_bytes < 0:
            raise ValueError("archive report sizes must be non-negative")


@dataclass(slots=True)
class _ReadTracker:
    limits: ArchiveLimits
    actual_total: int = 0
    observed: dict[str, tuple[int, str]] = field(default_factory=dict)

    def read(self, archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, collect_limit: int | None = None) -> tuple[bytes | None, tuple[int, str]]:
        key = _portable_key(_portable_path(_raw_member_name(info), info.is_dir()))
        existing = self.observed.get(key)
        if existing is not None:
            return None, existing
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if collect_limit is not None else None
        actual_size = 0
        try:
            with archive.open(info, "r") as member:
                while block := member.read(64 * 1024):
                    actual_size += len(block)
                    if actual_size > self.limits.max_entry_uncompressed_bytes:
                        raise ArchiveResourceLimitError("member actual size exceeds per-entry limit")
                    if self.actual_total + actual_size > self.limits.max_total_uncompressed_bytes:
                        raise ArchiveResourceLimitError("archive actual size exceeds total limit")
                    digest.update(block)
                    if chunks is not None:
                        assert collect_limit is not None
                        if actual_size > collect_limit:
                            raise ArchiveResourceLimitError("metadata member exceeds parser memory limit")
                        chunks.append(block)
        except ArchiveResourceLimitError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise UnsafeWheelArchive(f"cannot read archive member {_raw_member_name(info)!r}") from error
        if actual_size != info.file_size:
            raise UnsafeWheelArchive("archive member declared size differs from streamed size")
        self.actual_total += actual_size
        result = (actual_size, digest.hexdigest())
        self.observed[key] = result
        return b"".join(chunks) if chunks is not None else None, result


def validate_wheel_archive(
    path: Path,
    expected: DownloadedWheel,
    limits: ArchiveLimits,
) -> ArchiveValidationReport:
    """Read and verify a downloaded Wheel without extracting or executing it."""
    if not isinstance(expected, DownloadedWheel):
        raise ValueError("expected must be a DownloadedWheel")
    if not isinstance(limits, ArchiveLimits):
        raise ValueError("limits must be ArchiveLimits")
    supplied = Path(path)
    _require_expected_path(supplied, expected)

    with _open_bound_archive(supplied, expected) as handle:
        try:
            with zipfile.ZipFile(handle, "r") as archive:
                infos = tuple(archive.infolist())
                normalized = _validate_archive_members(infos, limits)
                tracker = _ReadTracker(limits)
                metadata_info, wheel_info, record_info = _core_infos(normalized, expected)
                metadata_bytes, _metadata_observation = tracker.read(
                    archive, metadata_info, collect_limit=_MAX_HEADER_BYTES
                )
                wheel_bytes, _wheel_observation = tracker.read(
                    archive, wheel_info, collect_limit=_MAX_HEADER_BYTES
                )
                record_bytes, _record_observation = tracker.read(
                    archive,
                    record_info,
                    collect_limit=min(
                        limits.max_entry_uncompressed_bytes,
                        limits.max_entries * _RECORD_FIELD_BYTES * 3,
                    ),
                )
                if metadata_bytes is None or wheel_bytes is None or record_bytes is None:
                    raise UnsafeWheelArchive("archive core member could not be read")
                wheel_tags = _parse_wheel(wheel_bytes, expected)
                record_rows = _parse_record(record_bytes, limits)
                _validate_record_paths(record_rows, normalized, record_info)
                _validate_record_values(record_rows, record_info, tracker, normalized)
                for normalized_name, info in normalized.items():
                    if not info.is_dir() and _portable_key(normalized_name) not in tracker.observed:
                        tracker.read(archive, info)
                _verify_record_hashes(record_rows, record_info, tracker)
                name, version, requires_dist, requires_python = _parse_metadata(metadata_bytes, expected)
                return ArchiveValidationReport(
                    name=name,
                    version=version,
                    requires_dist=requires_dist,
                    requires_python=requires_python,
                    tags=wheel_tags,
                    entry_count=len(infos),
                    total_uncompressed_bytes=tracker.actual_total,
                )
        except WheelArchiveValidationError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise UnsafeWheelArchive("invalid Wheel ZIP archive") from error


def _require_expected_path(path: Path, expected: DownloadedWheel) -> None:
    if path.absolute() != expected.path.absolute():
        raise UnsafeWheelArchive("supplied path does not match downloaded Wheel path")
    if path.name != expected.filename:
        raise UnsafeWheelArchive("downloaded Wheel filename differs from observation")


def _open_bound_archive(path: Path, expected: DownloadedWheel) -> BinaryIO:
    _validate_path_ancestors(path)
    try:
        before = path.lstat()
    except OSError as error:
        raise UnsafeWheelArchive("downloaded Wheel is unavailable") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise UnsafeWheelArchive("downloaded Wheel must be a regular non-symlink file")
    if getattr(before, "st_nlink", 1) != 1:
        raise UnsafeWheelArchive("downloaded Wheel must not have hard links")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise UnsafeWheelArchive("downloaded Wheel cannot be opened without following links") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or getattr(opened, "st_nlink", 1) != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise UnsafeWheelArchive("downloaded Wheel identity changed before validation")
        if opened.st_size != expected.byte_size:
            raise UnsafeWheelArchive("downloaded Wheel byte size differs from observation")
        handle = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        actual_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        if not _valid_sha256(expected.sha256) or actual_sha256 != expected.sha256.lower():
            raise UnsafeWheelArchive("downloaded Wheel SHA-256 differs from observation")
        handle.seek(0)
        return handle
    except BaseException:
        if descriptor != -1:
            os.close(descriptor)
        raise


def _validate_path_ancestors(path: Path) -> None:
    absolute = path.absolute()
    current = absolute.parent
    while True:
        try:
            status = current.lstat()
        except OSError as error:
            raise UnsafeWheelArchive("downloaded Wheel parent is unavailable") from error
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise UnsafeWheelArchive("downloaded Wheel has an unsafe path ancestor")
        if current == current.parent:
            return
        current = current.parent


def _valid_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))


def _validate_archive_members(
    infos: tuple[zipfile.ZipInfo, ...], limits: ArchiveLimits
) -> dict[str, zipfile.ZipInfo]:
    if len(infos) > limits.max_entries:
        raise ArchiveResourceLimitError("archive entry count exceeds limit")
    total_declared = 0
    raw_names: set[str] = set()
    portable_names: dict[str, str] = {}
    portable_files: set[str] = set()
    portable_ancestor_prefixes: set[str] = set()
    result: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        raw_name = _raw_member_name(info)
        if raw_name in raw_names:
            if _is_dist_info_core_path(raw_name):
                raise ArchiveMetadataError("archive contains duplicate dist-info core metadata")
            raise UnsafeWheelArchive("archive contains duplicate raw member names")
        raw_names.add(raw_name)
        normalized = _portable_path(raw_name, info.is_dir())
        portable = _portable_key(normalized)
        if portable in portable_names:
            if _is_dist_info_core_path(raw_name) or _is_dist_info_core_path(
                portable_names[portable]
            ):
                raise ArchiveMetadataError(
                    "archive contains duplicate portable dist-info core metadata"
                )
            raise UnsafeWheelArchive("archive contains a duplicate portable member path")
        ancestors = tuple(
            "/".join(portable.split("/")[:depth])
            for depth in range(1, len(portable.split("/")))
        )
        if any(ancestor in portable_files for ancestor in ancestors):
            raise UnsafeWheelArchive("archive member is nested beneath a file")
        if not info.is_dir() and portable in portable_ancestor_prefixes:
            raise UnsafeWheelArchive("archive file is the parent of another member")
        portable_names[portable] = raw_name
        portable_ancestor_prefixes.update(ancestors)
        if not info.is_dir():
            portable_files.add(portable)
        if normalized in result:
            raise UnsafeWheelArchive("archive contains a duplicate normalized member path")
        if getattr(info, "volume", 0) != 0:
            raise UnsafeWheelArchive("multi-disk ZIP members are not supported")
        if info.flag_bits & 0x1:
            raise UnsafeWheelArchive("encrypted ZIP members are not supported")
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            raise UnsafeWheelArchive("ZIP symlink members are not supported")
        if info.file_size < 0 or info.compress_size < 0:
            raise UnsafeWheelArchive("archive member size is invalid")
        if info.file_size > limits.max_entry_uncompressed_bytes:
            raise ArchiveResourceLimitError("archive member declared size exceeds per-entry limit")
        total_declared += info.file_size
        if total_declared > limits.max_total_uncompressed_bytes:
            raise ArchiveResourceLimitError("archive declared size exceeds total limit")
        if info.file_size > 0:
            if info.compress_size == 0 or info.file_size / info.compress_size > limits.max_compression_ratio:
                raise ArchiveResourceLimitError("archive member compression ratio exceeds limit")
        result[normalized] = info
    return result


def _raw_member_name(info: zipfile.ZipInfo) -> str:
    return getattr(info, "orig_filename", info.filename)


def _portable_path(name: str, is_directory: bool) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeWheelArchive("archive member path is unsafe")
    if name.startswith("/") or name.startswith("//") or re.match(r"^[A-Za-z]:", name):
        raise UnsafeWheelArchive("archive member path is absolute or drive-qualified")
    if is_directory:
        if not name.endswith("/"):
            raise UnsafeWheelArchive("directory member name is malformed")
        name = name[:-1]
    if not name:
        raise UnsafeWheelArchive("archive member path is empty")
    components = name.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise UnsafeWheelArchive("archive member path contains an unsafe component")
    for component in components:
        _validate_windows_component(component)
    normalized = "/".join(components)
    return normalized + "/" if is_directory else normalized


def _portable_key(name: str) -> str:
    return unicodedata.normalize("NFC", name.removesuffix("/")).casefold()


def _validate_windows_component(component: str) -> None:
    if component.endswith((".", " ")):
        raise UnsafeWheelArchive("archive member has a Windows-unsafe trailing character")
    if any(ord(character) < 32 or character in '<>:"|?*' for character in component):
        raise UnsafeWheelArchive("archive member contains a Windows-illegal character")
    basename = component.split(".", 1)[0].casefold()
    if basename in {"con", "prn", "aux", "nul"} or re.fullmatch(
        r"(?:com|lpt)[1-9]", basename
    ):
        raise UnsafeWheelArchive("archive member uses a reserved Windows device name")


def _is_dist_info_core_path(name: str) -> bool:
    directory, separator, filename = _portable_key(name).rpartition("/")
    return bool(
        separator
        and directory.endswith(".dist-info")
        and filename in {"metadata", "wheel", "record"}
    )


def _core_infos(
    members: dict[str, zipfile.ZipInfo], expected: DownloadedWheel
) -> tuple[zipfile.ZipInfo, zipfile.ZipInfo, zipfile.ZipInfo]:
    distribution = canonicalize_name(expected.package).replace("-", "_")
    identity = f"{distribution}-{expected.version}.dist-info"
    core_names = ("METADATA", "WHEEL", "RECORD")
    expected_names = {name: f"{identity}/{name}" for name in core_names}
    for normalized_name, info in members.items():
        if not _is_dist_info_core_path(normalized_name):
            continue
        _directory, filename = normalized_name.rsplit("/", 1)
        if filename.casefold() in {name.casefold() for name in core_names} and normalized_name not in expected_names.values():
            raise ArchiveMetadataError("archive contains foreign dist-info core metadata")
    selected: list[zipfile.ZipInfo] = []
    for core_name in core_names:
        core_info = members.get(expected_names[core_name])
        if core_info is None or core_info.is_dir():
            raise ArchiveMetadataError(f"required {core_name} is missing")
        selected.append(core_info)
    return selected[0], selected[1], selected[2]


def _parse_metadata(value: bytes, expected: DownloadedWheel) -> tuple[str, Version, tuple[str, ...], str | None]:
    message = _parse_headers(value, "METADATA")
    name = _singleton_header(message, "Name", "METADATA")
    version_value = _singleton_header(message, "Version", "METADATA")
    _reject_folded_identity(value, ("name", "version"), "METADATA")
    try:
        version = Version(version_value)
    except InvalidVersion as error:
        raise ArchiveMetadataError("METADATA Version is invalid") from error
    if canonicalize_name(name) != canonicalize_name(expected.package) or version != expected.version:
        raise ArchiveMetadataError("METADATA identity disagrees with downloaded Wheel")
    requires_python = _optional_singleton_header(message, "Requires-Python", "METADATA")
    return canonicalize_name(name), version, tuple(message.get_all("Requires-Dist", [])), requires_python


def _parse_wheel(value: bytes, expected: DownloadedWheel) -> frozenset[Tag]:
    message = _parse_headers(value, "WHEEL")
    _singleton_header(message, "Wheel-Version", "WHEEL")
    raw_tags = message.get_all("Tag", [])
    if not raw_tags:
        raise ArchiveMetadataError("WHEEL has no Tag headers")
    parsed: set[Tag] = set()
    for raw_tag in raw_tags:
        try:
            tags = parse_tag(raw_tag)
        except (TypeError, ValueError) as error:
            raise ArchiveMetadataError("WHEEL Tag is malformed") from error
        if not tags:
            raise ArchiveMetadataError("WHEEL Tag is malformed")
        parsed.update(tags)
    try:
        _name, _version, _build, filename_tags = parse_wheel_filename(expected.filename)
    except InvalidWheelFilename as error:
        raise ArchiveMetadataError("downloaded Wheel filename is invalid") from error
    if frozenset(filename_tags) != expected.tags or frozenset(parsed) != expected.tags:
        raise ArchiveMetadataError("WHEEL Tag headers disagree with downloaded Wheel tags")
    return frozenset(parsed)


def _parse_headers(value: bytes, label: str):
    try:
        value.decode("utf-8", "strict")
        message = BytesParser(policy=policy.strict).parsebytes(value)
    except (UnicodeDecodeError, ValueError) as error:
        raise ArchiveMetadataError(f"{label} is not valid UTF-8 headers") from error
    if message.defects:
        raise ArchiveMetadataError(f"{label} contains malformed headers")
    return message


def _singleton_header(message, header: str, label: str) -> str:
    values = message.get_all(header, [])
    if len(values) != 1 or not values[0] or "\r" in values[0] or "\n" in values[0]:
        raise ArchiveMetadataError(f"{label} requires one valid {header} header")
    return values[0]


def _optional_singleton_header(message, header: str, label: str) -> str | None:
    values = message.get_all(header, [])
    if len(values) > 1 or any("\r" in value or "\n" in value for value in values):
        raise ArchiveMetadataError(f"{label} has invalid {header} headers")
    return values[0] if values else None


def _reject_folded_identity(value: bytes, identities: Iterable[str], label: str) -> None:
    active_identity = False
    names = set(identities)
    for line in value.decode("utf-8", "strict").splitlines():
        if line[:1] in {" ", "\t"} and active_identity:
            raise ArchiveMetadataError(f"{label} identity headers cannot be folded")
        if not line:
            return
        header, separator, _rest = line.partition(":")
        active_identity = bool(separator) and header.casefold() in names


@dataclass(frozen=True, slots=True)
class _RecordRow:
    path: str
    digest: str
    size: str


def _parse_record(value: bytes, limits: ArchiveLimits) -> dict[str, _RecordRow]:
    try:
        text = value.decode("utf-8", "strict")
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        parsed: dict[str, _RecordRow] = {}
        for row_count, row in enumerate(rows, start=1):
            if row_count > limits.max_entries:
                raise WheelRecordMismatch("RECORD has too many rows")
            if len(row) != 3 or any(len(field.encode("utf-8")) > _RECORD_FIELD_BYTES for field in row):
                raise WheelRecordMismatch("RECORD rows must have three bounded fields")
            normalized = _portable_path(row[0], False)
            portable = _portable_key(normalized)
            if portable in parsed:
                raise WheelRecordMismatch("RECORD has duplicate portable paths")
            parsed[portable] = _RecordRow(normalized, row[1], row[2])
    except (UnicodeDecodeError, csv.Error, UnsafeWheelArchive) as error:
        raise WheelRecordMismatch("RECORD is malformed") from error
    return parsed


def _validate_record_paths(
    rows: dict[str, _RecordRow], members: dict[str, zipfile.ZipInfo], record: zipfile.ZipInfo
) -> None:
    files = {
        _portable_key(name)
        for name, info in members.items()
        if not info.is_dir()
    }
    if set(rows) != files:
        raise WheelRecordMismatch("RECORD paths do not exactly match archive files")
    record_path = _portable_key(_portable_path(_raw_member_name(record), False))
    if record_path not in rows:
        raise WheelRecordMismatch("RECORD does not include itself")


def _validate_record_values(
    rows: dict[str, _RecordRow], record: zipfile.ZipInfo, tracker: _ReadTracker, members: dict[str, zipfile.ZipInfo]
) -> None:
    record_path = _portable_key(_portable_path(_raw_member_name(record), False))
    for portable, row in rows.items():
        if portable == record_path:
            if (not row.digest and not row.size) or (row.digest and row.size):
                if not row.digest and not row.size:
                    continue
            else:
                raise WheelRecordMismatch("RECORD self row must provide both hash and size or neither")
        if not row.digest or not row.size:
            raise WheelRecordMismatch("RECORD file row is missing hash or size")
        if not row.digest.startswith("sha256="):
            raise WheelRecordMismatch("RECORD only supports sha256 hashes")
        encoded_digest = row.digest.removeprefix("sha256=")
        if not _SHA256_RECORD_DIGEST.fullmatch(encoded_digest):
            raise WheelRecordMismatch("RECORD sha256 digest is malformed")
        try:
            decoded = base64.urlsafe_b64decode(encoded_digest + "=")
        except (ValueError, TypeError) as error:
            raise WheelRecordMismatch("RECORD sha256 digest is malformed") from error
        if len(decoded) != hashlib.sha256().digest_size:
            raise WheelRecordMismatch("RECORD sha256 digest length is invalid")
        canonical_digest = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if canonical_digest != encoded_digest:
            raise WheelRecordMismatch("RECORD sha256 digest is not canonical URL-safe base64")
        if not _DECIMAL.fullmatch(row.size):
            raise WheelRecordMismatch("RECORD size is not canonical decimal")
        member = members.get(row.path)
        if member is None:
            raise WheelRecordMismatch("RECORD path is absent from the archive")
        if int(row.size) != member.file_size:
            raise WheelRecordMismatch("RECORD size differs from archive member size")


def _verify_record_hashes(
    rows: dict[str, _RecordRow], record: zipfile.ZipInfo, tracker: _ReadTracker
) -> None:
    record_path = _portable_key(_portable_path(_raw_member_name(record), False))
    for portable, row in rows.items():
        if portable == record_path and not row.digest and not row.size:
            continue
        actual = tracker.observed.get(portable)
        if actual is None:
            raise WheelRecordMismatch("RECORD path was not streamed from archive")
        actual_size, actual_hash = actual
        encoded = row.digest.removeprefix("sha256=")
        expected_hash = base64.urlsafe_b64decode(encoded + "=").hex()
        if actual_size != int(row.size) or actual_hash != expected_hash:
            raise WheelRecordMismatch("RECORD digest or size does not match archive content")
