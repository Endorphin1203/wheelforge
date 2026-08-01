from __future__ import annotations

import base64
import csv
import hashlib
import os
import stat
import struct
import subprocess
import sys
import tracemalloc
import zipfile
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from packaging.tags import Tag
from packaging.utils import canonicalize_name
from packaging.version import Version

import wheelforge_worker.validation.archive as archive_module
from wheelforge_worker.download import DownloadedWheel
from wheelforge_worker.resolver import PackageSource
from wheelforge_worker.validation.archive import (
    ArchiveLimits,
    ArchiveMetadataError,
    ArchiveResourceLimitError,
    UnsafeWheelArchive,
    WheelArchiveValidationError,
    WheelRecordMismatch,
    validate_wheel_archive,
    validate_wheel_archive_descriptor,
)


def _digest(value: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode("ascii")


def _record(rows: dict[str, bytes], record_path: str, overrides: dict[str, tuple[str, str]] | None = None) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, content in rows.items():
        digest, size = _digest(content), str(len(content))
        if overrides and name in overrides:
            digest, size = overrides[name]
        writer.writerow((name, f"sha256={digest}", size))
    digest, size = ("", "") if not overrides or record_path not in overrides else overrides[record_path]
    writer.writerow((record_path, f"sha256={digest}" if digest else "", size))
    return output.getvalue().encode("utf-8")


def make_wheel(
    root: Path,
    *,
    distribution: str = "demo",
    version: str = "1.2.3",
    package_filename: str | None = None,
    entries: dict[str, bytes] | None = None,
    metadata: bytes | None = None,
    wheel: bytes | None = None,
    record: bytes | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
    entry_attributes: dict[str, int] | None = None,
) -> Path:
    canonical = canonicalize_name(distribution).replace("-", "_")
    dist_info = f"{canonical}-{version}.dist-info"
    filename = package_filename or f"{canonical}-{version}-py3-none-any.whl"
    wheel_entries = {
        f"{canonical}/__init__.py": b"VALUE = 1\n",
        f"{dist_info}/METADATA": metadata
        or f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\nRequires-Python: >=3.9\nRequires-Dist: dep>=1\n".encode(),
        f"{dist_info}/WHEEL": wheel
        or b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    wheel_entries.update(entries or {})
    record_path = f"{dist_info}/RECORD"
    if record is None:
        record = _record(wheel_entries, record_path)
    wheel_entries[record_path] = record
    path = root / filename
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, content in wheel_entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = compression
            if entry_attributes and name in entry_attributes:
                info.external_attr = entry_attributes[name]
            archive.writestr(info, content)
    return path


def observed(path: Path, package: str = "demo", version: str = "1.2.3") -> DownloadedWheel:
    return DownloadedWheel(
        package=canonicalize_name(package),
        version=Version(version),
        filename=path.name,
        path=path,
        source=PackageSource.PYPI,
        byte_size=path.stat().st_size,
        sha256=hashlib.file_digest(path.open("rb"), "sha256").hexdigest(),
        tags=frozenset({Tag("py3", "none", "any")}),
    )


def _rewrite_wheel(path: Path, content: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in content.items():
            archive.writestr(name, value)


def _corrupt_first_member_payload(path: Path) -> None:
    content = bytearray(path.read_bytes())
    local_header = content.index(b"PK\x03\x04")
    name_length, extra_length = struct.unpack_from("<HH", content, local_header + 26)
    payload_offset = local_header + 30 + name_length + extra_length
    content[payload_offset : payload_offset + 4] = b"\xff\xff\xff\xff"
    path.write_bytes(content)


def _make_zip64(path: Path, *, disk_number: int, total_disks: int) -> None:
    content = bytearray(path.read_bytes())
    eocd_offset = content.rfind(b"PK\x05\x06")
    (
        _signature,
        _disk_number,
        _central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = struct.unpack_from("<4s4H2LH", content, eocd_offset)
    assert comment_length == 0
    zip64_eocd = struct.pack(
        "<IQHHIIQQQQ",
        0x06064B50,
        44,
        45,
        45,
        disk_number,
        disk_number,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
    )
    zip64_locator = struct.pack(
        "<IIQI", 0x07064B50, disk_number, eocd_offset, total_disks
    )
    struct.pack_into(
        "<4H2L",
        content,
        eocd_offset + 4,
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
    )
    path.write_bytes(content[:eocd_offset] + zip64_eocd + zip64_locator + content[eocd_offset:])


class _FaultingBinaryFile:
    def __init__(self, wrapped: BinaryIO, failing_method: str | None) -> None:
        self._wrapped = wrapped
        self._failing_method = failing_method

    def __enter__(self) -> _FaultingBinaryFile:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)

    def read(self, size: int = -1) -> bytes:
        self._fail("read")
        return self._wrapped.read(size)

    def write(self, value: bytes) -> int:
        self._fail("write")
        return self._wrapped.write(value)

    def flush(self) -> None:
        self._fail("flush")
        self._wrapped.flush()

    def fileno(self) -> int:
        return self._wrapped.fileno()

    def close(self) -> None:
        self._wrapped.close()
        self._fail("close")

    def _fail(self, method: str) -> None:
        if self._failing_method == method:
            raise OSError(f"injected {method} failure")


def _assert_file_descriptors_closed(descriptors: list[int]) -> None:
    still_open: list[int] = []
    for descriptor in descriptors:
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        still_open.append(descriptor)
    try:
        assert not still_open
    finally:
        for descriptor in still_open:
            os.close(descriptor)


def test_valid_wheel_reports_static_metadata(tmp_path: Path) -> None:
    path = make_wheel(tmp_path)

    report = validate_wheel_archive(path, observed(path), ArchiveLimits())

    assert report.validation_level == "STATIC"
    assert report.install_verified is False
    assert report.name == "demo"
    assert report.version == Version("1.2.3")
    assert report.requires_python == ">=3.9"
    assert report.requires_dist == ("dep>=1",)
    assert report.tags == frozenset({Tag("py3", "none", "any")})
    assert report.entry_count == 4


def test_descriptor_validation_accepts_exact_file_without_trusting_path_ancestors(
    tmp_path: Path,
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    path = make_wheel(owned)
    unsafe_parent = tmp_path / "linked"
    unsafe_parent.symlink_to(owned, target_is_directory=True)
    expected = replace(observed(path), path=unsafe_parent / path.name)
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))

    try:
        report = validate_wheel_archive_descriptor(
            descriptor, expected, ArchiveLimits(), snapshots
        )
        assert report.name == "demo"
        assert report.snapshot is not None
        os.fstat(descriptor)
        report.snapshot.cleanup()
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("field", ["path", "filename", "byte_size", "sha256"])
def test_expected_download_observation_must_match_regular_file(tmp_path: Path, field: str) -> None:
    path = make_wheel(tmp_path)
    expected = observed(path)
    if field == "path":
        expected = replace(expected, path=tmp_path / "other.whl")
    elif field == "filename":
        expected = replace(expected, filename="other-1.2.3-py3-none-any.whl")
    elif field == "byte_size":
        expected = replace(expected, byte_size=expected.byte_size + 1)
    else:
        expected = replace(expected, sha256="0" * 64)

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, expected, ArchiveLimits())


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_rejects_symlinked_archive_path(tmp_path: Path) -> None:
    path = make_wheel(tmp_path)
    link = tmp_path / "link.whl"
    link.symlink_to(path)
    expected = observed(path)
    linked = DownloadedWheel(
        expected.package, expected.version, link.name, link, expected.source,
        expected.byte_size, expected.sha256, expected.tags,
    )

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(link, linked, ArchiveLimits())


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links are unavailable")
def test_rejects_hard_linked_archive_path(tmp_path: Path) -> None:
    path = make_wheel(tmp_path)
    link = tmp_path / "hard-link.whl"
    os.link(path, link)
    expected = observed(link)

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(link, expected, ArchiveLimits())


def test_rejects_invalid_zip_and_encrypted_member(tmp_path: Path) -> None:
    invalid = tmp_path / "demo-1.2.3-py3-none-any.whl"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(invalid, observed(invalid), ArchiveLimits())


@pytest.mark.parametrize(
    "member",
    ["../escape.py", "/absolute.py", "pkg\\bad.py", "C:/drive.py", "pkg/./same.py"],
)
def test_rejects_unsafe_member_paths(tmp_path: Path, member: str) -> None:
    path = make_wheel(tmp_path, entries={member: b"bad"})

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_duplicate_portable_member_path(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, entries={"pkg/NAME.py": b"one", "pkg/name.py": b"two"})

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_file_and_directory_with_same_portable_path(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, entries={"pkg": b"file", "pkg/": b""})

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_file_that_is_parent_of_an_archive_member(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, entries={"pkg": b"file", "pkg/x.py": b"child"})

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_portable_topology_validation_has_linear_prefix_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_wheel(
        tmp_path,
        entries={f"bulk/file_{index:04d}.py": b"x" for index in range(512)},
    )
    original_portable_key = archive_module._portable_key
    prefix_checks = 0

    class CountingPortablePath(str):
        def startswith(self, prefix: str, *args: int) -> bool:
            nonlocal prefix_checks
            prefix_checks += 1
            return super().startswith(prefix, *args)

    def counted_portable_key(name: str) -> str:
        return CountingPortablePath(original_portable_key(name))

    monkeypatch.setattr(archive_module, "_portable_key", counted_portable_key)

    report = validate_wheel_archive(path, observed(path), ArchiveLimits())

    assert prefix_checks <= report.entry_count * 4


def test_deep_legal_member_path_has_bounded_topology_memory(tmp_path: Path) -> None:
    deep_name = f"{'a/' * 1800}payload.py"
    path = make_wheel(tmp_path, entries={deep_name: b"x"})

    tracemalloc.start()
    try:
        report = validate_wheel_archive(path, observed(path), ArchiveLimits())
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    try:
        assert peak < 3 * 1024 * 1024
    finally:
        snapshot = getattr(report, "snapshot", None)
        if snapshot is not None:
            snapshot.cleanup()


@pytest.mark.parametrize(
    "member",
    [
        "CON",
        "prn.txt",
        "Aux.data",
        "nul",
        "COM1.py",
        "com9",
        "LPT1.txt",
        "lpt9",
        "pkg/trailing.",
        "pkg/trailing ",
        "pkg/name:stream.py",
        "pkg/less<than.py",
        "pkg/greater>than.py",
        'pkg/quote"name.py',
        "pkg/pipe|name.py",
        "pkg/question?.py",
        "pkg/star*.py",
        "pkg/control\x1f.py",
    ],
)
def test_rejects_windows_unsafe_member_components(tmp_path: Path, member: str) -> None:
    path = make_wheel(tmp_path, entries={member: b"unsafe"})

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_unix_symlink_member(tmp_path: Path) -> None:
    path = make_wheel(
        tmp_path,
        entries={"pkg/link": b"target"},
        entry_attributes={"pkg/link": (stat.S_IFLNK | 0o777) << 16},
    )

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_encrypted_flag_and_crc_failure(tmp_path: Path) -> None:
    encrypted_dir = tmp_path / "encrypted"
    encrypted_dir.mkdir()
    encrypted = make_wheel(encrypted_dir)
    bytes_with_flag = bytearray(encrypted.read_bytes())
    local = bytes_with_flag.index(b"PK\x03\x04")
    central = bytes_with_flag.index(b"PK\x01\x02")
    bytes_with_flag[local + 6] |= 1
    bytes_with_flag[central + 8] |= 1
    encrypted.write_bytes(bytes_with_flag)
    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(encrypted, observed(encrypted), ArchiveLimits())

    corrupted_dir = tmp_path / "corrupted"
    corrupted_dir.mkdir()
    corrupted = make_wheel(corrupted_dir, compression=zipfile.ZIP_STORED)
    contents = bytearray(corrupted.read_bytes())
    offset = contents.index(b"VALUE = 1\n")
    contents[offset] ^= 1
    corrupted.write_bytes(contents)
    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(corrupted, observed(corrupted), ArchiveLimits())


@pytest.mark.parametrize(
    "compression",
    [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA],
)
def test_codec_read_failures_are_typed_archive_errors(
    tmp_path: Path, compression: int
) -> None:
    path = make_wheel(tmp_path, compression=compression)
    _corrupt_first_member_payload(path)

    with pytest.raises(WheelArchiveValidationError):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_nonempty_directory_member_is_rejected_before_packaging(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, entries={"payload/": b"bad"})
    with zipfile.ZipFile(path, "r") as source:
        content = {info.filename: source.read(info) for info in source.infolist()}
    record_path = next(name for name in content if name.endswith("/RECORD"))
    content[record_path] = b"".join(
        line for line in content[record_path].splitlines(keepends=True)
        if not line.startswith(b"payload/,")
    )
    _rewrite_wheel(path, content)

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_non_nfc_member_component_is_rejected(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, entries={"pkg/cafe\u0301.txt": b"x"})

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


@pytest.mark.parametrize("zip64", [False, True])
def test_multidisk_end_records_are_rejected(tmp_path: Path, zip64: bool) -> None:
    path = make_wheel(tmp_path, compression=zipfile.ZIP_STORED)
    if zip64:
        _make_zip64(path, disk_number=1, total_disks=2)
    else:
        content = bytearray(path.read_bytes())
        eocd_offset = content.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", content, eocd_offset + 4, 1, 1)
        path.write_bytes(content)

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


@pytest.mark.parametrize(
    ("limits", "entries"),
    [
        (ArchiveLimits(max_entries=3), None),
        (ArchiveLimits(max_entry_uncompressed_bytes=4), {"pkg/large.py": b"12345"}),
        (ArchiveLimits(max_total_uncompressed_bytes=20), {"pkg/large.py": b"1234567890"}),
        (ArchiveLimits(max_compression_ratio=1), {"pkg/repeated.py": b"x" * 10000}),
    ],
)
def test_rejects_declared_archive_resource_limit(
    tmp_path: Path, limits: ArchiveLimits, entries: dict[str, bytes] | None
) -> None:
    path = make_wheel(tmp_path, entries=entries)

    with pytest.raises(ArchiveResourceLimitError):
        validate_wheel_archive(path, observed(path), limits)


@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), 20_001])
def test_limits_reject_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        ArchiveLimits(max_entries=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("core", ["METADATA", "WHEEL", "RECORD"])
def test_requires_matching_dist_info_core_files(tmp_path: Path, core: str) -> None:
    path = make_wheel(tmp_path)
    with zipfile.ZipFile(path, "r") as source:
        original = {info.filename: source.read(info) for info in source.infolist() if not info.filename.endswith(f"/{core}")}
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in original.items():
            archive.writestr(name, content)

    with pytest.raises(ArchiveMetadataError):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_duplicate_and_foreign_dist_info_core_files(tmp_path: Path) -> None:
    path = make_wheel(tmp_path)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("demo-1.2.3.dist-info/METADATA", b"Name: demo\\nVersion: 1.2.3\\n")
    with pytest.raises(ArchiveMetadataError):
        validate_wheel_archive(path, observed(path), ArchiveLimits())

    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    foreign = make_wheel(foreign_dir)
    with zipfile.ZipFile(foreign, "a") as archive:
        archive.writestr("other-1.0.dist-info/WHEEL", b"Wheel-Version: 1.0\\nTag: py3-none-any\\n")
    with pytest.raises(ArchiveMetadataError):
        validate_wheel_archive(foreign, observed(foreign), ArchiveLimits())


@pytest.mark.parametrize(
    "foreign_core",
    [
        "other-1.0.DIST-INFO/WHEEL",
        "OTHER-1.0.dist-info/RECORD",
        "DEMO-1.2.3.DIST-INFO/METADATA",
    ],
)
def test_rejects_case_variant_foreign_dist_info_core(
    tmp_path: Path, foreign_core: str
) -> None:
    path = make_wheel(tmp_path, entries={foreign_core: b"foreign"})

    with pytest.raises(ArchiveMetadataError):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


@pytest.mark.parametrize(
    "metadata,wheel",
    [
        (b"Metadata-Version: 2.1\nName: other\nVersion: 1.2.3\n", None),
        (b"Metadata-Version: 2.1\nName: demo\nVersion: 9.9\n", None),
        (b"Metadata-Version: 2.1\nName: demo\nName: other\nVersion: 1.2.3\n", None),
        (None, b"Wheel-Version: 1.0\nTag: cp311-cp311-manylinux2014_aarch64\n"),
    ],
)
def test_rejects_metadata_and_tag_mismatch(tmp_path: Path, metadata: bytes | None, wheel: bytes | None) -> None:
    path = make_wheel(tmp_path, metadata=metadata, wheel=wheel)

    with pytest.raises(ArchiveMetadataError):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


@pytest.mark.parametrize(
    "metadata",
    [
        b"Metadata-Version: 2.1\\nVersion: 1.2.3\\n",
        b"Metadata-Version: 2.1\\nName: demo\\nVersion: 1.2.3\\n folded\\n",
        b"Metadata-Version: 2.1\\nName: demo\\nVersion: 1.2.3\\nBrokenHeader\\n",
        b"Metadata-Version: 2.1\\nName: demo\\nVersion: \\xff\\n",
    ],
)
def test_rejects_malformed_identity_headers(tmp_path: Path, metadata: bytes) -> None:
    path = make_wheel(tmp_path, metadata=metadata)

    with pytest.raises(ArchiveMetadataError):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_missing_record_entry_and_bad_hash(tmp_path: Path) -> None:
    path = make_wheel(tmp_path)
    with zipfile.ZipFile(path, "r") as source:
        content = {info.filename: source.read(info) for info in source.infolist()}
    record_path = next(name for name in content if name.endswith("/RECORD"))
    content[record_path] = b"demo/__init__.py,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,10\n"
    broken_directory = tmp_path / "broken"
    broken_directory.mkdir()
    broken = broken_directory / path.name
    with zipfile.ZipFile(broken, "w") as archive:
        for name, value in content.items():
            archive.writestr(name, value)

    with pytest.raises(WheelRecordMismatch):
        validate_wheel_archive(broken, observed(broken), ArchiveLimits())


@pytest.mark.parametrize(
    "record",
    [
        b"demo/__init__.py,sha256=4T34xEr13qHkEkA5ELmcxaSPLMv2imazN01quc75_GU,10\\n"
        b"demo/__init__.py,sha256=4T34xEr13qHkEkA5ELmcxaSPLMv2imazN01quc75_GU,10\\n",
        b"demo/__init__.py,sha256=4T34xEr13qHkEkA5ELmcxaSPLMv2imazN01quc75_GU,10\\n"
        b"demo-1.2.3.dist-info/METADATA,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,1\\n"
        b"demo-1.2.3.dist-info/WHEEL,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,1\\n"
        b"demo-1.2.3.dist-info/RECORD,,\\n"
        b"unexpected.py,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,1\\n",
    ],
)
def test_rejects_duplicate_or_extra_record_paths(tmp_path: Path, record: bytes) -> None:
    path = make_wheel(tmp_path, record=record)

    with pytest.raises(WheelRecordMismatch):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_record_path_that_only_matches_archive_portably(tmp_path: Path) -> None:
    path = make_wheel(tmp_path)
    with zipfile.ZipFile(path, "r") as source:
        content = {info.filename: source.read(info) for info in source.infolist()}
    record_path = next(name for name in content if name.endswith("/RECORD"))
    content[record_path] = content[record_path].replace(b"demo/__init__.py", b"demo/__INIT__.py")
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in content.items():
            archive.writestr(name, value)

    with pytest.raises(WheelRecordMismatch):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_rejects_noncanonical_sha256_base64_in_record(tmp_path: Path) -> None:
    path = make_wheel(tmp_path)
    with zipfile.ZipFile(path, "r") as source:
        content = {info.filename: source.read(info) for info in source.infolist()}
    record_path = next(name for name in content if name.endswith("/RECORD"))
    canonical = _digest(content["demo/__init__.py"])
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    final_index = alphabet.index(canonical[-1])
    noncanonical = canonical[:-1] + alphabet[final_index + 1]
    content[record_path] = content[record_path].replace(
        canonical.encode("ascii"), noncanonical.encode("ascii")
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in content.items():
            archive.writestr(name, value)

    with pytest.raises(WheelRecordMismatch):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


@pytest.mark.parametrize(
    "record",
    [
        b"too,many,columns,here\n",
        b"demo/__init__.py,md5=abc,1\n",
        b"demo/__init__.py,sha256=not-base64!,1\n",
        b"demo/__init__.py,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,not-a-size\n",
    ],
)
def test_rejects_malformed_record_rows(tmp_path: Path, record: bytes) -> None:
    path = make_wheel(tmp_path, record=record)

    with pytest.raises(WheelRecordMismatch):
        validate_wheel_archive(path, observed(path), ArchiveLimits())


def test_record_rejects_first_invalid_digest_without_materializing_tail(
    tmp_path: Path,
) -> None:
    first_row = b"demo/__init__.py,md5=bad,10\n"
    filler = b"".join(
        f"p{index:04d}/".encode() + b"a" * 4080 + b"," + b"A" * 4096 + b",1\n"
        for index in range(1000)
    )
    path = make_wheel(
        tmp_path,
        record=first_row + filler,
        compression=zipfile.ZIP_STORED,
    )

    tracemalloc.start()
    try:
        with pytest.raises(WheelRecordMismatch):
            validate_wheel_archive(path, observed(path), ArchiveLimits())
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 16 * 1024 * 1024


def test_validation_snapshot_is_bound_to_verified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_dir = tmp_path / "original"
    alternate_dir = tmp_path / "alternate"
    original_dir.mkdir()
    alternate_dir.mkdir()
    path = make_wheel(
        original_dir, entries={"payload.txt": b"A"}, compression=zipfile.ZIP_STORED
    )
    alternate = make_wheel(
        alternate_dir, entries={"payload.txt": b"B"}, compression=zipfile.ZIP_STORED
    )
    original_bytes = path.read_bytes()
    alternate_bytes = alternate.read_bytes()
    assert len(original_bytes) == len(alternate_bytes)
    expected = observed(path)
    real_zip_file = zipfile.ZipFile
    swapped = False

    def swap_source_before_zip_parse(file: object, *args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            path.write_bytes(alternate_bytes)
            swapped = True
        return real_zip_file(file, *args, **kwargs)

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", swap_source_before_zip_parse)

    report = validate_wheel_archive(path, expected, ArchiveLimits())

    try:
        assert swapped is True
        assert path.read_bytes() == alternate_bytes
        assert report.snapshot.path.read_bytes() == original_bytes
        assert report.snapshot.sha256 == expected.sha256
        assert stat.S_IMODE(report.snapshot.path.stat().st_mode) == 0o400
    finally:
        report.snapshot.cleanup()
    assert not report.snapshot.path.exists()


def test_snapshot_revalidation_wraps_digest_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_wheel(tmp_path)
    report = validate_wheel_archive(path, observed(path), ArchiveLimits())

    def fail_digest(*args: object, **kwargs: object) -> object:
        raise OSError("snapshot read failed")

    monkeypatch.setattr(archive_module.hashlib, "file_digest", fail_digest)

    try:
        with pytest.raises(UnsafeWheelArchive) as caught:
            report.snapshot.open()
        assert isinstance(caught.value.__cause__, OSError)
    finally:
        report.snapshot.cleanup()


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (ArchiveMetadataError("typed failure"), ArchiveMetadataError),
        (OSError("primary read failure"), UnsafeWheelArchive),
        (KeyboardInterrupt(), KeyboardInterrupt),
        (SystemExit(), SystemExit),
    ],
)
def test_snapshot_revalidation_close_failure_preserves_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_type: type[BaseException],
) -> None:
    path = make_wheel(tmp_path)
    report = validate_wheel_archive(path, observed(path), ArchiveLimits())
    real_fdopen = os.fdopen

    def fail_digest(*args: object, **kwargs: object) -> object:
        raise failure

    def close_failing_fdopen(
        descriptor: int, *args: object, **kwargs: object
    ) -> _FaultingBinaryFile:
        wrapped = cast(
            BinaryIO,
            real_fdopen(descriptor, *args, **kwargs),  # type: ignore[call-overload]
        )
        return _FaultingBinaryFile(wrapped, "close")

    monkeypatch.setattr(archive_module.hashlib, "file_digest", fail_digest)
    monkeypatch.setattr(archive_module.os, "fdopen", close_failing_fdopen)

    try:
        with pytest.raises(expected_type) as caught:
            report.snapshot.open()
        if isinstance(failure, OSError):
            assert isinstance(caught.value, UnsafeWheelArchive)
            assert caught.value.__cause__ is failure
        else:
            assert caught.value is failure
    finally:
        report.snapshot.cleanup()


@pytest.mark.parametrize("failed_transfer", [1, 2], ids=["source", "destination"])
def test_fdopen_failure_closes_source_and_snapshot_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_transfer: int
) -> None:
    path = make_wheel(tmp_path)
    expected = observed(path)
    real_open = os.open
    real_fdopen = os.fdopen
    opened_descriptors: list[int] = []
    transfer_count = 0

    def tracking_open(
        file: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        descriptor = real_open(  # type: ignore[call-overload]
            file, flags, *args, **kwargs
        )
        opened_descriptors.append(descriptor)
        return descriptor

    def reject_transfer(
        descriptor: int, *args: object, **kwargs: object
    ) -> object:
        nonlocal transfer_count
        transfer_count += 1
        if transfer_count == failed_transfer:
            raise OSError("fdopen failed")
        return real_fdopen(descriptor, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(archive_module.os, "open", tracking_open)
    monkeypatch.setattr(archive_module.os, "fdopen", reject_transfer)

    with pytest.raises(UnsafeWheelArchive):
        validate_wheel_archive(path, expected, ArchiveLimits())

    assert len(opened_descriptors) == 2
    _assert_file_descriptors_closed(opened_descriptors)
    assert list(tmp_path.glob(".wheelforge-validated-*")) == []


@pytest.mark.parametrize(
    "failure_stage", ["source_read", "destination_write", "destination_flush", "fsync"]
)
def test_snapshot_copy_failure_closes_all_descriptors_and_removes_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    path = make_wheel(tmp_path)
    expected = observed(path)
    real_open = os.open
    real_fdopen = os.fdopen
    opened_descriptors: list[int] = []
    call_count = 0

    def tracking_open(
        file: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        descriptor = real_open(  # type: ignore[call-overload]
            file, flags, *args, **kwargs
        )
        opened_descriptors.append(descriptor)
        return descriptor

    def faulting_fdopen(
        descriptor: int, *args: object, **kwargs: object
    ) -> _FaultingBinaryFile:
        nonlocal call_count
        call_count += 1
        failing_method = None
        if call_count == 1 and failure_stage == "source_read":
            failing_method = "read"
        elif call_count == 2 and failure_stage == "destination_write":
            failing_method = "write"
        elif call_count == 2 and failure_stage == "destination_flush":
            failing_method = "flush"
        wrapped = cast(
            BinaryIO,
            real_fdopen(descriptor, *args, **kwargs),  # type: ignore[call-overload]
        )
        return _FaultingBinaryFile(wrapped, failing_method)

    def fail_fsync(descriptor: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(archive_module.os, "open", tracking_open)
    monkeypatch.setattr(archive_module.os, "fdopen", faulting_fdopen)
    if failure_stage == "fsync":
        monkeypatch.setattr(archive_module.os, "fsync", fail_fsync)

    with pytest.raises(UnsafeWheelArchive) as caught:
        validate_wheel_archive(path, expected, ArchiveLimits())

    assert isinstance(caught.value.__cause__, OSError)
    assert len(opened_descriptors) == 2
    _assert_file_descriptors_closed(opened_descriptors)
    assert list(tmp_path.glob(".wheelforge-validated-*")) == []


def test_rejects_report_claiming_install_verification() -> None:
    with pytest.raises(ValueError):
        from wheelforge_worker.validation.archive import ArchiveValidationReport

        ArchiveValidationReport(
            name="demo", version=Version("1"), requires_dist=(), requires_python=None,
            tags=frozenset(), entry_count=0, total_uncompressed_bytes=0,
            validation_level="STATIC", install_verified=True,
        )


def test_validation_never_extracts_imports_or_invokes_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_wheel(tmp_path)
    monkeypatch.setattr(zipfile.ZipFile, "extract", lambda *args, **kwargs: pytest.fail("extract called"))
    monkeypatch.setattr(zipfile.ZipFile, "extractall", lambda *args, **kwargs: pytest.fail("extractall called"))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess called"))
    before = set(sys.modules)

    validate_wheel_archive(path, observed(path), ArchiveLimits())

    assert set(sys.modules) == before
