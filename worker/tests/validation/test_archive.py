from __future__ import annotations

import base64
import csv
import hashlib
import os
import stat
import subprocess
import sys
import zipfile
from dataclasses import replace
from io import StringIO
from pathlib import Path

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
    WheelRecordMismatch,
    validate_wheel_archive,
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
