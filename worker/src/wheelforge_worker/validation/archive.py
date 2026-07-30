from __future__ import annotations

import base64
import csv
import hashlib
import io
import math
import os
import re
import stat
import struct
import tempfile
import threading
import unicodedata
import zipfile
from collections.abc import Buffer, Iterable
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import BinaryIO, IO

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
_COPY_CHUNK_BYTES = 64 * 1024
_EOCD_SIZE = 22
_MAX_ZIP_COMMENT_BYTES = 65_535
_ZIP64_LOCATOR_SIZE = 20
_ZIP64_EOCD_MIN_SIZE = 56
_SHA256_RECORD_DIGEST = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_CSV_FIELD_LIMIT_LOCK = threading.Lock()


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
        _bounded_ratio(
            "max_compression_ratio",
            self.max_compression_ratio,
            _DEFAULT_MAX_COMPRESSION_RATIO,
        )


@dataclass(frozen=True, slots=True)
class ValidatedWheelSnapshot:
    """Private immutable-by-policy Wheel bytes handed from validation to packaging."""

    path: Path
    byte_size: int
    sha256: str
    _directory: Path = field(repr=False)
    _device: int = field(repr=False)
    _inode: int = field(repr=False)

    def open(self) -> BinaryIO:
        """Open the same private snapshot after rechecking identity and digest."""
        try:
            before = self.path.lstat()
        except OSError as error:
            raise UnsafeWheelArchive("validated Wheel snapshot is unavailable") from error
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or getattr(before, "st_nlink", 1) != 1
            or (before.st_dev, before.st_ino) != (self._device, self._inode)
            or before.st_size != self.byte_size
        ):
            raise UnsafeWheelArchive("validated Wheel snapshot identity changed")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
        except OSError as error:
            raise UnsafeWheelArchive("validated Wheel snapshot cannot be opened") from error
        handle: BinaryIO | None = None
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or getattr(opened, "st_nlink", 1) != 1
                or (opened.st_dev, opened.st_ino) != (self._device, self._inode)
                or opened.st_size != self.byte_size
            ):
                raise UnsafeWheelArchive("validated Wheel snapshot identity changed")
            handle = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = -1
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if digest != self.sha256:
                raise UnsafeWheelArchive("validated Wheel snapshot content changed")
            handle.seek(0)
            return handle
        except WheelArchiveValidationError:
            if handle is not None:
                handle.close()
            elif descriptor != -1:
                os.close(descriptor)
            raise
        except Exception as error:
            if handle is not None:
                handle.close()
            elif descriptor != -1:
                os.close(descriptor)
            raise UnsafeWheelArchive(
                "validated Wheel snapshot revalidation failed"
            ) from error
        except BaseException:
            if handle is not None:
                handle.close()
            elif descriptor != -1:
                os.close(descriptor)
            raise

    def cleanup(self) -> None:
        """Delete this snapshot only while its recorded identity is still present."""
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            _remove_empty_snapshot_directory(self._directory)
            return
        except OSError as error:
            raise UnsafeWheelArchive("validated Wheel snapshot cannot be inspected") from error
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != (self._device, self._inode)
        ):
            raise UnsafeWheelArchive("validated Wheel snapshot identity changed")
        try:
            self.path.unlink()
        except OSError as error:
            raise UnsafeWheelArchive("validated Wheel snapshot cannot be removed") from error
        _remove_empty_snapshot_directory(self._directory)


@dataclass(frozen=True, slots=True)
class ArchiveValidationReport:
    name: str
    version: Version
    requires_dist: tuple[str, ...]
    requires_python: str | None
    tags: frozenset[Tag]
    entry_count: int
    total_uncompressed_bytes: int
    snapshot: ValidatedWheelSnapshot | None = None
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
class _TopologyNode:
    children: dict[str, _TopologyNode] = field(default_factory=dict)
    terminal_raw_name: str | None = None
    is_file: bool = False


@dataclass(slots=True)
class _ReadTracker:
    limits: ArchiveLimits
    actual_total: int = 0
    observed: dict[str, tuple[int, bytes]] = field(default_factory=dict)

    def read(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        *,
        collect_limit: int | None = None,
    ) -> tuple[bytes | None, tuple[int, bytes]]:
        key = _portable_key(_portable_path(_raw_member_name(info), info.is_dir()))
        existing = self.observed.get(key)
        if existing is not None:
            return None, existing
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if collect_limit is not None else None
        actual_size = 0
        try:
            with archive.open(info, "r") as member:
                while block := member.read(_COPY_CHUNK_BYTES):
                    actual_size += len(block)
                    self._check_actual_size(actual_size)
                    digest.update(block)
                    if chunks is not None:
                        assert collect_limit is not None
                        if actual_size > collect_limit:
                            raise ArchiveResourceLimitError(
                                "metadata member exceeds parser memory limit"
                            )
                        chunks.append(block)
        except WheelArchiveValidationError:
            raise
        except Exception as error:
            raise UnsafeWheelArchive(
                f"cannot read archive member {_raw_member_name(info)!r}"
            ) from error
        observation = self.finish(info, key, actual_size, digest.digest())
        return b"".join(chunks) if chunks is not None else None, observation

    def _check_actual_size(self, member_size: int) -> None:
        if member_size > self.limits.max_entry_uncompressed_bytes:
            raise ArchiveResourceLimitError("member actual size exceeds per-entry limit")
        if self.actual_total + member_size > self.limits.max_total_uncompressed_bytes:
            raise ArchiveResourceLimitError("archive actual size exceeds total limit")

    def finish(
        self,
        info: zipfile.ZipInfo,
        key: str,
        actual_size: int,
        digest: bytes,
    ) -> tuple[int, bytes]:
        if actual_size != info.file_size:
            raise UnsafeWheelArchive(
                "archive member declared size differs from streamed size"
            )
        self.actual_total += actual_size
        observation = (actual_size, digest)
        self.observed[key] = observation
        return observation


class _TrackedMemberReader(io.RawIOBase):
    def __init__(
        self,
        member: IO[bytes],
        info: zipfile.ZipInfo,
        key: str,
        tracker: _ReadTracker,
    ) -> None:
        super().__init__()
        self._member = member
        self._info = info
        self._key = key
        self._tracker = tracker
        self._digest = hashlib.sha256()
        self._actual_size = 0
        self._finished = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer)
        try:
            data = self._member.read(len(view))
        except WheelArchiveValidationError:
            raise
        except Exception as error:
            raise UnsafeWheelArchive(
                f"cannot read archive member {_raw_member_name(self._info)!r}"
            ) from error
        if not data:
            return 0
        self._actual_size += len(data)
        self._tracker._check_actual_size(self._actual_size)
        self._digest.update(data)
        view[: len(data)] = data
        return len(data)

    def finish(self) -> tuple[int, bytes]:
        if not self._finished:
            self._finished = True
            return self._tracker.finish(
                self._info,
                self._key,
                self._actual_size,
                self._digest.digest(),
            )
        existing = self._tracker.observed.get(self._key)
        if existing is None:
            raise UnsafeWheelArchive("archive member stream did not finish")
        return existing


@dataclass(frozen=True, slots=True)
class _RecordRow:
    path: str
    digest: bytes | None
    size: int | None


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
    snapshot = _create_verified_snapshot(supplied, expected)
    try:
        with snapshot.open() as handle:
            _validate_zip_end_records(handle)
            handle.seek(0)
            try:
                with zipfile.ZipFile(handle, "r") as archive:
                    infos = tuple(archive.infolist())
                    normalized = _validate_archive_members(infos, limits)
                    tracker = _ReadTracker(limits)
                    metadata_info, wheel_info, record_info = _core_infos(
                        normalized, expected
                    )
                    metadata_bytes, _metadata_observation = tracker.read(
                        archive, metadata_info, collect_limit=_MAX_HEADER_BYTES
                    )
                    wheel_bytes, _wheel_observation = tracker.read(
                        archive, wheel_info, collect_limit=_MAX_HEADER_BYTES
                    )
                    if metadata_bytes is None or wheel_bytes is None:
                        raise UnsafeWheelArchive("archive core member could not be read")
                    wheel_tags = _parse_wheel(wheel_bytes, expected)
                    record_rows = _stream_record(
                        archive, record_info, tracker, limits, normalized
                    )
                    _validate_record_paths(record_rows, normalized, record_info)
                    for normalized_name, info in normalized.items():
                        if _portable_key(normalized_name) not in tracker.observed:
                            tracker.read(archive, info)
                    _verify_record_hashes(record_rows, record_info, tracker)
                    name, version, requires_dist, requires_python = _parse_metadata(
                        metadata_bytes, expected
                    )
                    return ArchiveValidationReport(
                        name=name,
                        version=version,
                        requires_dist=requires_dist,
                        requires_python=requires_python,
                        tags=wheel_tags,
                        entry_count=len(infos),
                        total_uncompressed_bytes=tracker.actual_total,
                        snapshot=snapshot,
                    )
            except WheelArchiveValidationError:
                raise
            except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
                raise UnsafeWheelArchive("invalid Wheel ZIP archive") from error
    except BaseException:
        snapshot.cleanup()
        raise


def _require_expected_path(path: Path, expected: DownloadedWheel) -> None:
    if path.absolute() != expected.path.absolute():
        raise UnsafeWheelArchive("supplied path does not match downloaded Wheel path")
    if path.name != expected.filename:
        raise UnsafeWheelArchive("downloaded Wheel filename differs from observation")


def _create_verified_snapshot(
    path: Path, expected: DownloadedWheel
) -> ValidatedWheelSnapshot:
    _validate_path_ancestors(path)
    if not _valid_sha256(expected.sha256):
        raise UnsafeWheelArchive("downloaded Wheel SHA-256 observation is invalid")
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
        source_descriptor = os.open(path, flags)
    except OSError as error:
        raise UnsafeWheelArchive(
            "downloaded Wheel cannot be opened without following links"
        ) from error
    snapshot_directory: Path | None = None
    snapshot_path: Path | None = None
    snapshot_descriptor = -1
    try:
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or getattr(opened, "st_nlink", 1) != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise UnsafeWheelArchive("downloaded Wheel identity changed before validation")
        if opened.st_size != expected.byte_size:
            raise UnsafeWheelArchive("downloaded Wheel byte size differs from observation")
        snapshot_directory = Path(
            tempfile.mkdtemp(prefix=".wheelforge-validated-", dir=path.parent)
        )
        os.chmod(snapshot_directory, 0o700)
        snapshot_path = snapshot_directory / expected.filename
        snapshot_descriptor = os.open(
            snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        digest = hashlib.sha256()
        byte_size = 0
        try:
            source = os.fdopen(source_descriptor, "rb", closefd=True)
            source_descriptor = -1
            with source:
                destination = os.fdopen(snapshot_descriptor, "wb", closefd=True)
                snapshot_descriptor = -1
                with destination:
                    while block := source.read(_COPY_CHUNK_BYTES):
                        byte_size += len(block)
                        if byte_size > expected.byte_size:
                            raise UnsafeWheelArchive(
                                "downloaded Wheel grew while being snapshotted"
                            )
                        digest.update(block)
                        destination.write(block)
                    destination.flush()
                    os.fsync(destination.fileno())
        except WheelArchiveValidationError:
            raise
        except Exception as error:
            raise UnsafeWheelArchive("downloaded Wheel snapshot failed") from error
        if byte_size != expected.byte_size or digest.hexdigest() != expected.sha256.lower():
            raise UnsafeWheelArchive(
                "downloaded Wheel content changed while being snapshotted"
            )
        os.chmod(snapshot_path, 0o400)
        snapshot_status = snapshot_path.lstat()
        return ValidatedWheelSnapshot(
            path=snapshot_path,
            byte_size=byte_size,
            sha256=digest.hexdigest(),
            _directory=snapshot_directory,
            _device=snapshot_status.st_dev,
            _inode=snapshot_status.st_ino,
        )
    except BaseException:
        if source_descriptor != -1:
            os.close(source_descriptor)
        if snapshot_descriptor != -1:
            os.close(snapshot_descriptor)
        if snapshot_path is not None:
            try:
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                pass
        if snapshot_directory is not None:
            _remove_empty_snapshot_directory(snapshot_directory)
        raise


def _remove_empty_snapshot_directory(directory: Path) -> None:
    try:
        directory.rmdir()
    except FileNotFoundError:
        return
    except OSError as error:
        raise UnsafeWheelArchive(
            "validated Wheel snapshot directory cannot be removed"
        ) from error


def _validate_path_ancestors(path: Path) -> None:
    current = path.absolute().parent
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


def _validate_zip_end_records(handle: BinaryIO) -> None:
    try:
        handle.seek(0, os.SEEK_END)
        archive_size = handle.tell()
        tail_size = min(archive_size, _EOCD_SIZE + _MAX_ZIP_COMMENT_BYTES)
        handle.seek(archive_size - tail_size)
        tail = handle.read(tail_size)
    except Exception as error:
        raise UnsafeWheelArchive("cannot inspect Wheel ZIP end records") from error
    relative_offset = tail.rfind(b"PK\x05\x06")
    if relative_offset < 0 or len(tail) - relative_offset < _EOCD_SIZE:
        raise UnsafeWheelArchive("Wheel ZIP end record is missing")
    eocd_offset = archive_size - tail_size + relative_offset
    (
        _signature,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        comment_size,
    ) = struct.unpack_from("<4s4H2LH", tail, relative_offset)
    if eocd_offset + _EOCD_SIZE + comment_size != archive_size:
        raise UnsafeWheelArchive("Wheel ZIP end record is malformed")
    if disk_number != 0 or central_disk != 0:
        raise UnsafeWheelArchive("multi-disk ZIP archives are not supported")
    uses_zip64 = (
        entries_on_disk == 0xFFFF
        or total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    )
    if not uses_zip64:
        if entries_on_disk != total_entries:
            raise UnsafeWheelArchive("ZIP per-disk entry counts disagree")
        return
    locator_offset = eocd_offset - _ZIP64_LOCATOR_SIZE
    if locator_offset < 0:
        raise UnsafeWheelArchive("ZIP64 locator is missing")
    locator = _read_exact_at(handle, locator_offset, _ZIP64_LOCATOR_SIZE)
    signature, zip64_disk, zip64_offset, total_disks = struct.unpack(
        "<4sLQL", locator
    )
    if signature != b"PK\x06\x07":
        raise UnsafeWheelArchive("ZIP64 locator is missing")
    if zip64_disk != 0 or total_disks != 1:
        raise UnsafeWheelArchive("multi-disk ZIP64 archives are not supported")
    zip64 = _read_exact_at(handle, zip64_offset, _ZIP64_EOCD_MIN_SIZE)
    (
        zip64_signature,
        record_size,
        _version_made,
        _version_needed,
        zip64_disk_number,
        zip64_central_disk,
        zip64_entries_on_disk,
        zip64_total_entries,
        _zip64_central_size,
        _zip64_central_offset,
    ) = struct.unpack("<4sQ2H2L4Q", zip64)
    if zip64_signature != b"PK\x06\x06" or record_size < 44:
        raise UnsafeWheelArchive("ZIP64 end record is malformed")
    if zip64_disk_number != 0 or zip64_central_disk != 0:
        raise UnsafeWheelArchive("multi-disk ZIP64 archives are not supported")
    if zip64_entries_on_disk != zip64_total_entries:
        raise UnsafeWheelArchive("ZIP64 per-disk entry counts disagree")


def _read_exact_at(handle: BinaryIO, offset: int, size: int) -> bytes:
    try:
        handle.seek(offset)
        value = handle.read(size)
    except Exception as error:
        raise UnsafeWheelArchive("cannot read Wheel ZIP structure") from error
    if len(value) != size:
        raise UnsafeWheelArchive("Wheel ZIP structure is truncated")
    return value


def _validate_archive_members(
    infos: tuple[zipfile.ZipInfo, ...], limits: ArchiveLimits
) -> dict[str, zipfile.ZipInfo]:
    if len(infos) > limits.max_entries:
        raise ArchiveResourceLimitError("archive entry count exceeds limit")
    total_declared = 0
    raw_names: set[str] = set()
    topology = _TopologyNode()
    result: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        raw_name = _raw_member_name(info)
        if raw_name in raw_names:
            if _is_dist_info_core_path(raw_name):
                raise ArchiveMetadataError(
                    "archive contains duplicate dist-info core metadata"
                )
            raise UnsafeWheelArchive("archive contains duplicate raw member names")
        raw_names.add(raw_name)
        normalized = _portable_path(raw_name, info.is_dir())
        _insert_topology(topology, normalized, raw_name, info.is_dir())
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
        if info.is_dir() and info.file_size != 0:
            raise UnsafeWheelArchive("directory archive members must be empty")
        if info.file_size > limits.max_entry_uncompressed_bytes:
            raise ArchiveResourceLimitError(
                "archive member declared size exceeds per-entry limit"
            )
        total_declared += info.file_size
        if total_declared > limits.max_total_uncompressed_bytes:
            raise ArchiveResourceLimitError("archive declared size exceeds total limit")
        if info.file_size > 0 and (
            info.compress_size == 0
            or info.file_size / info.compress_size > limits.max_compression_ratio
        ):
            raise ArchiveResourceLimitError(
                "archive member compression ratio exceeds limit"
            )
        result[normalized] = info
    return result


def _insert_topology(
    root: _TopologyNode, normalized: str, raw_name: str, is_directory: bool
) -> None:
    node = root
    components = normalized.removesuffix("/").split("/")
    for component in components:
        if node.is_file:
            raise UnsafeWheelArchive("archive member is nested beneath a file")
        node = node.children.setdefault(component.casefold(), _TopologyNode())
    if node.terminal_raw_name is not None:
        if _is_dist_info_core_path(raw_name) or _is_dist_info_core_path(
            node.terminal_raw_name
        ):
            raise ArchiveMetadataError(
                "archive contains duplicate portable dist-info core metadata"
            )
        raise UnsafeWheelArchive("archive contains a duplicate portable member path")
    if not is_directory and node.children:
        raise UnsafeWheelArchive("archive file is the parent of another member")
    node.terminal_raw_name = raw_name
    node.is_file = not is_directory


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
        if unicodedata.normalize("NFC", component) != component:
            raise UnsafeWheelArchive("archive member path is not NFC-normalized")
        _validate_windows_component(component)
    normalized = "/".join(components)
    return normalized + "/" if is_directory else normalized


def _portable_key(name: str) -> str:
    return name.removesuffix("/").casefold()


def _validate_windows_component(component: str) -> None:
    if component.endswith((".", " ")):
        raise UnsafeWheelArchive(
            "archive member has a Windows-unsafe trailing character"
        )
    if any(ord(character) < 32 or character in '<>:"|?*' for character in component):
        raise UnsafeWheelArchive(
            "archive member contains a Windows-illegal character"
        )
    basename = component.split(".", 1)[0].casefold()
    if basename in {"con", "prn", "aux", "nul"} or re.fullmatch(
        r"(?:com|lpt)[1-9]", basename
    ):
        raise UnsafeWheelArchive(
            "archive member uses a reserved Windows device name"
        )


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
    expected_values = set(expected_names.values())
    for normalized_name in members:
        if _is_dist_info_core_path(normalized_name) and normalized_name not in expected_values:
            raise ArchiveMetadataError(
                "archive contains foreign dist-info core metadata"
            )
    selected: list[zipfile.ZipInfo] = []
    for core_name in core_names:
        core_info = members.get(expected_names[core_name])
        if core_info is None or core_info.is_dir():
            raise ArchiveMetadataError(f"required {core_name} is missing")
        selected.append(core_info)
    return selected[0], selected[1], selected[2]


def _parse_metadata(
    value: bytes, expected: DownloadedWheel
) -> tuple[str, Version, tuple[str, ...], str | None]:
    message = _parse_headers(value, "METADATA")
    name = _singleton_header(message, "Name", "METADATA")
    version_value = _singleton_header(message, "Version", "METADATA")
    _reject_folded_identity(value, ("name", "version"), "METADATA")
    try:
        version = Version(version_value)
    except InvalidVersion as error:
        raise ArchiveMetadataError("METADATA Version is invalid") from error
    if (
        canonicalize_name(name) != canonicalize_name(expected.package)
        or version != expected.version
    ):
        raise ArchiveMetadataError(
            "METADATA identity disagrees with downloaded Wheel"
        )
    requires_python = _optional_singleton_header(
        message, "Requires-Python", "METADATA"
    )
    return (
        canonicalize_name(name),
        version,
        tuple(message.get_all("Requires-Dist", [])),
        requires_python,
    )


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
        _name, _version, _build, filename_tags = parse_wheel_filename(
            expected.filename
        )
    except InvalidWheelFilename as error:
        raise ArchiveMetadataError("downloaded Wheel filename is invalid") from error
    if frozenset(filename_tags) != expected.tags or frozenset(parsed) != expected.tags:
        raise ArchiveMetadataError(
            "WHEEL Tag headers disagree with downloaded Wheel tags"
        )
    return frozenset(parsed)


def _parse_headers(value: bytes, label: str) -> Message:
    try:
        value.decode("utf-8", "strict")
        message = BytesParser(policy=policy.strict).parsebytes(value)
    except (UnicodeDecodeError, ValueError) as error:
        raise ArchiveMetadataError(f"{label} is not valid UTF-8 headers") from error
    if message.defects:
        raise ArchiveMetadataError(f"{label} contains malformed headers")
    return message


def _singleton_header(message: Message, header: str, label: str) -> str:
    values = message.get_all(header, [])
    if len(values) != 1 or not values[0] or "\r" in values[0] or "\n" in values[0]:
        raise ArchiveMetadataError(f"{label} requires one valid {header} header")
    return values[0]


def _optional_singleton_header(
    message: Message, header: str, label: str
) -> str | None:
    values = message.get_all(header, [])
    if len(values) > 1 or any("\r" in value or "\n" in value for value in values):
        raise ArchiveMetadataError(f"{label} has invalid {header} headers")
    return values[0] if values else None


def _reject_folded_identity(
    value: bytes, identities: Iterable[str], label: str
) -> None:
    active_identity = False
    names = set(identities)
    for line in value.decode("utf-8", "strict").splitlines():
        if line[:1] in {" ", "\t"} and active_identity:
            raise ArchiveMetadataError(
                f"{label} identity headers cannot be folded"
            )
        if not line:
            return
        header, separator, _rest = line.partition(":")
        active_identity = bool(separator) and header.casefold() in names


def _stream_record(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    tracker: _ReadTracker,
    limits: ArchiveLimits,
    members: dict[str, zipfile.ZipInfo],
) -> dict[str, _RecordRow]:
    record_path = _portable_key(_portable_path(_raw_member_name(info), False))
    parsed: dict[str, _RecordRow] = {}
    key = record_path
    try:
        with archive.open(info, "r") as member:
            tracked = _TrackedMemberReader(member, info, key, tracker)
            with io.BufferedReader(tracked, buffer_size=_COPY_CHUNK_BYTES) as buffered:
                with io.TextIOWrapper(
                    buffered, encoding="utf-8", errors="strict", newline=""
                ) as text:
                    with _CSV_FIELD_LIMIT_LOCK:
                        previous_limit = csv.field_size_limit(_RECORD_FIELD_BYTES)
                        try:
                            rows = csv.reader(text, strict=True)
                            for row_count, row in enumerate(rows, start=1):
                                if row_count > limits.max_entries:
                                    raise WheelRecordMismatch(
                                        "RECORD has too many rows"
                                    )
                                parsed_row, portable = _parse_record_row(
                                    row, record_path, members
                                )
                                if portable in parsed:
                                    raise WheelRecordMismatch(
                                        "RECORD has duplicate portable paths"
                                    )
                                parsed[portable] = parsed_row
                        finally:
                            csv.field_size_limit(previous_limit)
                    tracked.finish()
    except WheelArchiveValidationError:
        raise
    except (UnicodeDecodeError, csv.Error) as error:
        raise WheelRecordMismatch("RECORD is malformed") from error
    except Exception as error:
        raise UnsafeWheelArchive("cannot stream RECORD") from error
    return parsed


def _parse_record_row(
    row: list[str],
    record_path: str,
    members: dict[str, zipfile.ZipInfo],
) -> tuple[_RecordRow, str]:
    if len(row) != 3 or any(
        len(record_field.encode("utf-8")) > _RECORD_FIELD_BYTES
        for record_field in row
    ):
        raise WheelRecordMismatch("RECORD rows must have three bounded fields")
    try:
        normalized = _portable_path(row[0], False)
    except UnsafeWheelArchive as error:
        raise WheelRecordMismatch("RECORD path is malformed") from error
    portable = _portable_key(normalized)
    digest_field, size_field = row[1], row[2]
    if portable == record_path and not digest_field and not size_field:
        return _RecordRow(normalized, None, None), portable
    if not digest_field or not size_field:
        raise WheelRecordMismatch("RECORD file row is missing hash or size")
    digest = _parse_record_digest(digest_field)
    if not _DECIMAL.fullmatch(size_field):
        raise WheelRecordMismatch("RECORD size is not canonical decimal")
    size = int(size_field)
    member = members.get(normalized)
    if member is None:
        raise WheelRecordMismatch("RECORD path is absent from the archive")
    if size != member.file_size:
        raise WheelRecordMismatch(
            "RECORD size differs from archive member size"
        )
    return _RecordRow(normalized, digest, size), portable


def _parse_record_digest(value: str) -> bytes:
    if not value.startswith("sha256="):
        raise WheelRecordMismatch("RECORD only supports sha256 hashes")
    encoded = value.removeprefix("sha256=")
    if not _SHA256_RECORD_DIGEST.fullmatch(encoded):
        raise WheelRecordMismatch("RECORD sha256 digest is malformed")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=")
    except (ValueError, TypeError) as error:
        raise WheelRecordMismatch("RECORD sha256 digest is malformed") from error
    if len(decoded) != hashlib.sha256().digest_size:
        raise WheelRecordMismatch("RECORD sha256 digest length is invalid")
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != encoded:
        raise WheelRecordMismatch(
            "RECORD sha256 digest is not canonical URL-safe base64"
        )
    return decoded


def _validate_record_paths(
    rows: dict[str, _RecordRow],
    members: dict[str, zipfile.ZipInfo],
    record: zipfile.ZipInfo,
) -> None:
    files = {
        _portable_key(name) for name, info in members.items() if not info.is_dir()
    }
    if set(rows) != files:
        raise WheelRecordMismatch("RECORD paths do not exactly match archive files")
    record_path = _portable_key(_portable_path(_raw_member_name(record), False))
    if record_path not in rows:
        raise WheelRecordMismatch("RECORD does not include itself")


def _verify_record_hashes(
    rows: dict[str, _RecordRow], record: zipfile.ZipInfo, tracker: _ReadTracker
) -> None:
    record_path = _portable_key(_portable_path(_raw_member_name(record), False))
    for portable, row in rows.items():
        if portable == record_path and row.digest is None and row.size is None:
            continue
        actual = tracker.observed.get(portable)
        if actual is None or row.digest is None or row.size is None:
            raise WheelRecordMismatch("RECORD path was not streamed from archive")
        actual_size, actual_hash = actual
        if actual_size != row.size or actual_hash != row.digest:
            raise WheelRecordMismatch(
                "RECORD digest or size does not match archive content"
            )
