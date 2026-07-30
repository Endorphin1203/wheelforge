from __future__ import annotations

import csv
import ctypes
import errno
import hashlib
import html
import io
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from uuid import UUID
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename

from wheelforge_worker.resolver import VersionChange

from .models import ArtifactBuildContext, BuiltArtifact, ValidatedWheel
from .templates import HTML_DOCUMENT, STATIC_VALIDATION_MESSAGE


_COPY_CHUNK_BYTES = 64 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o100644
_SCRIPT_MODE = 0o100755
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_WHEEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl\Z")


class ArtifactBuildError(RuntimeError):
    pass


class ArtifactBuilder:
    def build(self, context: ArtifactBuildContext, output_dir: Path) -> BuiltArtifact:
        snapshots = []
        snapshot_ids: set[int] = set()
        for item in context.wheels:
            snapshot = item.report.snapshot
            if snapshot is not None and id(snapshot) not in snapshot_ids:
                snapshots.append(snapshot)
                snapshot_ids.add(id(snapshot))
        try:
            return self._build(context, Path(output_dir))
        finally:
            active_error = sys.exception()
            cleanup_error: Exception | None = None
            for snapshot in snapshots:
                try:
                    snapshot.cleanup()
                except Exception as error:
                    cleanup_error = cleanup_error or error
            if cleanup_error is not None and active_error is None:
                raise cleanup_error

    def _build(self, context: ArtifactBuildContext, output_dir: Path) -> BuiltArtifact:
        _validate_output_directory(output_dir)
        _validate_context(context)
        final_path = output_dir / f"wheelforge-{context.build_id}.zip"
        if final_path.exists():
            raise FileExistsError(f"artifact already exists: {final_path.name}")

        packaged = _packaged_by_name(context)
        intended = _intended_packages(context, packaged)
        complete = context.validation.complete and len(packaged) == len(intended)
        manifest = _manifest(context, intended, packaged, complete)
        entries = _text_entries(context, intended, manifest, complete)
        package_entries = {
            f"packages/{item.download.filename}": item for item in packaged.values()
        }
        hashes = {
            path: hashlib.sha256(content).hexdigest() for path, content in entries.items()
        }
        hashes.update(
            {path: item.download.sha256 for path, item in package_entries.items()}
        )
        entries["checksums.sha256"] = _text(
            "".join(f"{hashes[path]}  {path}\n" for path in sorted(hashes))
        )
        expected_names = set(entries) | set(package_entries)

        output_descriptor = _open_output_directory(output_dir)
        try:
            descriptor, staged_name = tempfile.mkstemp(
                prefix=".wheelforge-artifact-", suffix=".tmp", dir=output_dir
            )
            staged_path = Path(staged_name)
            staged_identity = _identity(os.fstat(descriptor))
            try:
                with os.fdopen(descriptor, "w+b") as staged:
                    _write_zip(staged, entries, package_entries)
                    staged.flush()
                    os.fsync(staged.fileno())
                _require_owned_stage(
                    output_descriptor, staged_path.name, staged_identity
                )
                _verify_zip(staged_path, expected_names, hashes)
                _require_owned_stage(
                    output_descriptor, staged_path.name, staged_identity
                )
                _require_output_directory(output_dir, output_descriptor)
                digest = _hash_file(staged_path)
                _require_owned_stage(
                    output_descriptor, staged_path.name, staged_identity
                )
                _require_output_directory(output_dir, output_descriptor)
                _publish_no_replace(
                    staged_path,
                    final_path,
                    output_descriptor,
                    staged_identity,
                    output_dir,
                )
                return BuiltArtifact(final_path, digest, manifest)
            except BaseException:
                _unlink_if_owned(
                    staged_path,
                    staged_identity,
                    output_descriptor,
                    staged_path.name,
                )
                raise
        finally:
            os.close(output_descriptor)


def _validate_output_directory(output_dir: Path) -> None:
    try:
        status = output_dir.lstat()
    except OSError as error:
        raise ArtifactBuildError("output directory must already exist") from error
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ArtifactBuildError("output directory must be a real directory")


def _validate_context(context: ArtifactBuildContext) -> None:
    if not isinstance(context, ArtifactBuildContext):
        raise ArtifactBuildError("artifact build context is required")
    try:
        build_id = UUID(context.build_id)
    except (ValueError, AttributeError) as error:
        raise ArtifactBuildError("build identifier must be a canonical UUID") from error
    if str(build_id) != context.build_id:
        raise ArtifactBuildError("build identifier must be a canonical UUID")
    if context.target.os not in {"LINUX", "WINDOWS"}:
        raise ArtifactBuildError("artifact target OS is unsupported")

    resolved_names: set[str] = set()
    for package in context.resolution.packages:
        name = canonicalize_name(package.name)
        if name in resolved_names:
            raise ArtifactBuildError("resolved package identities must be unique")
        resolved_names.add(name)

    packaged_names: set[str] = set()
    portable_paths: set[str] = set()
    for item in context.wheels:
        _validate_pair(item)
        name = canonicalize_name(item.download.package)
        if name in packaged_names or name not in resolved_names:
            raise ArtifactBuildError("validated package identities are inconsistent")
        packaged_names.add(name)
        package_path = f"packages/{item.download.filename}"
        key = package_path.casefold()
        if key in portable_paths or not _safe_zip_path(package_path):
            raise ArtifactBuildError("validated Wheel path is unsafe or duplicate")
        portable_paths.add(key)
    if context.validation.complete and packaged_names != resolved_names:
        raise ArtifactBuildError("complete validation requires every resolved Wheel")


def _validate_pair(item: ValidatedWheel) -> None:
    if not isinstance(item, ValidatedWheel):
        raise ArtifactBuildError("validated Wheels must use ValidatedWheel pairs")
    download = item.download
    report = item.report
    snapshot = report.snapshot
    if snapshot is None:
        raise ArtifactBuildError("validated Wheel snapshot is required")
    try:
        filename_name, filename_version, _build, filename_tags = parse_wheel_filename(
            download.filename
        )
    except InvalidWheelFilename as error:
        raise ArtifactBuildError("download and archive validation report disagree") from error
    if (
        canonicalize_name(download.package) != canonicalize_name(report.name)
        or canonicalize_name(filename_name) != canonicalize_name(download.package)
        or download.version != report.version
        or filename_version != download.version
        or filename_tags != download.tags
        or download.filename != snapshot.path.name
        or download.byte_size != snapshot.byte_size
        or download.sha256 != snapshot.sha256
        or download.tags != report.tags
        or not _SHA256.fullmatch(download.sha256)
        or not _SAFE_WHEEL.fullmatch(download.filename)
    ):
        raise ArtifactBuildError("download and archive validation report disagree")


def _packaged_by_name(context: ArtifactBuildContext) -> dict[str, ValidatedWheel]:
    return {
        canonicalize_name(item.download.package): item for item in context.wheels
    }


def _intended_packages(
    context: ArtifactBuildContext, packaged: dict[str, ValidatedWheel]
) -> list[dict[str, Any]]:
    intended: list[dict[str, Any]] = []
    for package in sorted(
        context.resolution.packages, key=lambda item: canonicalize_name(item.name)
    ):
        name = canonicalize_name(package.name)
        wheel = packaged.get(name)
        digest = wheel.download.sha256 if wheel else _resolver_sha256(package.archive_hashes)
        intended.append(
            {
                "name": name,
                "version": str(package.version),
                "sha256": digest,
                "packaged": wheel is not None,
            }
        )
    return intended


def _resolver_sha256(hashes: tuple[Any, ...]) -> str:
    values = {
        item.value.lower()
        for item in hashes
        if item.algorithm.lower() == "sha256" and _SHA256.fullmatch(item.value.lower())
    }
    if len(values) != 1:
        raise ArtifactBuildError("missing package requires one unambiguous resolver SHA-256")
    return values.pop()


def _manifest(
    context: ArtifactBuildContext,
    intended: list[dict[str, Any]],
    packaged: dict[str, ValidatedWheel],
    complete: bool,
) -> dict[str, Any]:
    packaged_wheels = [
        {
            "name": name,
            "version": str(item.download.version),
            "filename": item.download.filename,
            "path": f"packages/{item.download.filename}",
            "size": item.download.byte_size,
            "sha256": item.download.sha256,
        }
        for name, item in sorted(packaged.items())
    ]
    issues = [
        {
            "code": issue.code.value,
            "package": issue.package,
            "detail": issue.detail,
            "filename": issue.filename,
        }
        for issue in sorted(
            context.validation.issues,
            key=lambda item: (
                canonicalize_name(item.package),
                item.code.value,
                item.filename or "",
                item.detail,
            ),
        )
    ]
    return {
        "buildId": context.build_id,
        "complete": complete,
        "installVerified": False,
        "intendedPackages": intended,
        "packagedWheels": packaged_wheels,
        "target": {
            "os": context.target.os,
            "architecture": context.target.architecture,
            "pythonVersion": context.target.python_version,
        },
        "validationIssues": issues,
        "validationLevel": "STATIC",
        "validationMessage": STATIC_VALIDATION_MESSAGE,
    }


def _text_entries(
    context: ArtifactBuildContext,
    intended: list[dict[str, Any]],
    manifest: dict[str, Any],
    complete: bool,
) -> dict[str, bytes]:
    requirements = "".join(
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
        for item in intended
    )
    entries = {
        "README.md": _text(_readme(context, complete)),
        "requirements-original.txt": _text(context.original_requirements),
        "requirements-resolved.txt": _text(requirements),
        "version-comparison.csv": _comparison_csv(context),
        "build-report.html": _text(_html_report(context, manifest, complete)),
        "manifest.json": _text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ),
    }
    entries.update(_scripts(context, complete))
    return entries


def _readme(context: ArtifactBuildContext, complete: bool) -> str:
    status = "COMPLETE" if complete else "PARTIAL - NOT INSTALLABLE"
    return (
        f"# WheelForge offline artifact\n\nStatus: {status}\n\n"
        f"{STATIC_VALIDATION_MESSAGE}\n"
    )


def _comparison_csv(context: ArtifactBuildContext) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        ["package", "original_constraint", "original_version", "resolved_version", "change_kind", "source", "reason"]
    )
    for change in sorted(context.version_changes, key=_version_change_key):
        writer.writerow(
            [
                canonicalize_name(change.package),
                change.original_constraint,
                "" if change.original_version is None else str(change.original_version),
                "" if change.resolved_version is None else str(change.resolved_version),
                change.kind.value,
                "" if change.source is None else change.source.value,
                change.reason,
            ]
        )
    return _text(output.getvalue())


def _html_report(
    context: ArtifactBuildContext, manifest: dict[str, Any], complete: bool
) -> str:
    status = "Complete artifact" if complete else "Partial artifact - not installable"
    changes = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(value, quote=True)}</td>"
            for value in (
                canonicalize_name(change.package),
                change.original_constraint,
                "" if change.resolved_version is None else str(change.resolved_version),
                change.kind.value,
                "" if change.source is None else change.source.value,
                change.reason,
            )
        )
        + "</tr>"
        for change in sorted(context.version_changes, key=_version_change_key)
    )
    issues = "".join(
        "<li>"
        + html.escape(
            f"{item['code']}: {item['package']}: {item['detail']}", quote=True
        )
        + "</li>"
        for item in manifest["validationIssues"]
    ) or "<li>None</li>"
    body = (
        f"<body><h1>{status}</h1><p>{html.escape(STATIC_VALIDATION_MESSAGE)}</p>"
        "<h2>Dependency version comparison</h2><table><thead><tr><th>Package</th><th>Original</th>"
        "<th>Resolved</th><th>Change</th><th>Source</th><th>Reason</th></tr></thead>"
        f"<tbody>{changes}</tbody></table><h2>Static validation issues</h2><ul>{issues}</ul></body></html>\n"
    )
    return HTML_DOCUMENT.format(body=body.removesuffix("</html>\n"))


def _scripts(context: ArtifactBuildContext, complete: bool) -> dict[str, bytes]:
    if context.target.os == "LINUX":
        names = ("install.sh", "verify.sh")
        values = _linux_scripts(context, complete)
    else:
        names = ("install.bat", "verify.bat")
        values = _windows_scripts(context, complete)
    return {name: _text(value) for name, value in zip(names, values, strict=True)}


def _version_change_key(change: VersionChange) -> tuple[str, ...]:
    return (
        canonicalize_name(change.package),
        change.original_constraint,
        "" if change.original_version is None else str(change.original_version),
        "" if change.resolved_version is None else str(change.resolved_version),
        change.kind.value,
        "" if change.source is None else change.source.value,
        change.reason,
    )


def _linux_scripts(context: ArtifactBuildContext, complete: bool) -> tuple[str, str]:
    if not complete:
        failure = "#!/bin/sh\necho 'Incomplete artifact: installation is unavailable.' >&2\nexit 1\n"
        return failure, failure
    machine = {"X86_64": "x86_64", "AARCH64": "aarch64"}[context.target.architecture]
    checks = (
        "#!/bin/sh\nset -eu\n"
        f"[ \"$(uname -s)\" = \"Linux\" ] || {{ echo 'Target OS mismatch' >&2; exit 1; }}\n"
        f"[ \"$(uname -m)\" = \"{machine}\" ] || {{ echo 'Target architecture mismatch' >&2; exit 1; }}\n"
        f"python -c \"import sys; raise SystemExit(sys.version_info[:2] != ({context.target.python_version.replace('.', ', ')}))\" || {{ echo 'Target Python mismatch' >&2; exit 1; }}\n"
    )
    install = checks + "python -m pip install --no-index --find-links packages --require-hashes -r requirements-resolved.txt\n"
    verify = checks + "sha256sum -c checksums.sha256\npython -m pip check\n"
    return install, verify


def _windows_scripts(context: ArtifactBuildContext, complete: bool) -> tuple[str, str]:
    if not complete:
        failure = "@echo off\r\necho Incomplete artifact: installation is unavailable. 1>&2\r\nexit /b 1\r\n"
        return failure, failure
    machine = {"AMD64": "AMD64", "ARM64": "ARM64"}[context.target.architecture]
    major, minor = context.target.python_version.split(".")
    checks = (
        "@echo off\r\nsetlocal\r\n"
        "if /I not \"%OS%\"==\"Windows_NT\" (echo Target OS mismatch 1>&2 & exit /b 1)\r\n"
        f"python -c \"import platform,sys; raise SystemExit(platform.machine().upper() != '{machine}' or sys.version_info[:2] != ({major}, {minor}))\"\r\n"
        "if errorlevel 1 (echo Target architecture or Python mismatch 1>&2 & exit /b 1)\r\n"
    )
    install = checks + "python -m pip install --no-index --find-links packages --require-hashes -r requirements-resolved.txt\r\n"
    verify = checks + "python -m pip check\r\n"
    return install, verify


def _text(value: str) -> bytes:
    return value.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _zip_info(path: str, *, script: bool = False) -> ZipInfo:
    if not _safe_zip_path(path):
        raise ArtifactBuildError("generated ZIP path is unsafe")
    info = ZipInfo(path, _ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = ZIP_DEFLATED
    info.external_attr = ((_SCRIPT_MODE if script else _FILE_MODE) << 16)
    return info


def _write_zip(
    staged: BinaryIO,
    entries: dict[str, bytes],
    packages: dict[str, ValidatedWheel],
) -> None:
    with ZipFile(staged, "w", compression=ZIP_DEFLATED, compresslevel=9, strict_timestamps=True) as archive:
        for path in sorted(set(entries) | set(packages)):
            info = _zip_info(path, script=path.endswith((".sh", ".bat")))
            if path in entries:
                archive.writestr(info, entries[path], compress_type=ZIP_DEFLATED, compresslevel=9)
                continue
            snapshot = packages[path].report.snapshot
            assert snapshot is not None
            with snapshot.open() as source, archive.open(info, "w", force_zip64=True) as target:
                while block := source.read(_COPY_CHUNK_BYTES):
                    target.write(block)


def _verify_zip(
    path: Path, expected_names: set[str], expected_hashes: dict[str, str]
) -> None:
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != expected_names:
                raise ArtifactBuildError("staged ZIP member contract is invalid")
            if names != sorted(names) or any(not _safe_zip_path(name) for name in names):
                raise ArtifactBuildError("staged ZIP paths are unsafe or unsorted")
            for info in archive.infolist():
                expected_mode = (
                    _SCRIPT_MODE
                    if info.filename.endswith((".sh", ".bat"))
                    else _FILE_MODE
                )
                if (
                    info.date_time != _ZIP_TIMESTAMP
                    or info.create_system != 3
                    or info.compress_type != ZIP_DEFLATED
                    or info.external_attr >> 16 != expected_mode
                ):
                    raise ArtifactBuildError("staged ZIP member metadata is invalid")
            checksums = _parse_checksums(archive.read("checksums.sha256"))
            if set(checksums) != expected_names - {"checksums.sha256"}:
                raise ArtifactBuildError("staged ZIP checksum coverage is incomplete")
            if checksums != expected_hashes:
                raise ArtifactBuildError("staged ZIP does not contain expected content")
            for name, digest in checksums.items():
                if _hash_zip_member(archive, name) != digest:
                    raise ArtifactBuildError("staged ZIP checksum verification failed")
            if archive.testzip() is not None:
                raise ArtifactBuildError("staged ZIP contains corrupt data")
    except (BadZipFile, OSError, KeyError, UnicodeError) as error:
        raise ArtifactBuildError("staged ZIP verification failed") from error


def _parse_checksums(content: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in content.decode("utf-8").splitlines():
        digest, separator, path = line.partition("  ")
        if not separator or not _SHA256.fullmatch(digest) or not _safe_zip_path(path) or path in result:
            raise ArtifactBuildError("staged ZIP checksum file is invalid")
        result[path] = digest
    return result


def _safe_zip_path(path: str) -> bool:
    if not path or "\\" in path or any(ord(character) < 32 for character in path):
        return False
    parsed = PurePosixPath(path)
    return (
        not parsed.is_absolute()
        and bool(parsed.parts)
        and all(part not in {"", ".", ".."} for part in parsed.parts)
        and ":" not in parsed.parts[0]
        and str(parsed) == path
    )


def _hash_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _hash_zip_member(archive: ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name) as member:
        while block := member.read(_COPY_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _open_output_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _identity(opened) != _identity(current)
        ):
            raise ArtifactBuildError("output directory identity changed")
        return descriptor
    except BaseException:
        if "descriptor" in locals():
            os.close(descriptor)
        raise


def _require_output_directory(path: Path, descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError as error:
        raise ArtifactBuildError("output directory identity changed") from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or _identity(opened) != _identity(current)
    ):
        raise ArtifactBuildError("output directory identity changed")


def _require_owned_stage(
    directory_descriptor: int, name: str, expected: tuple[int, int]
) -> None:
    try:
        status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as error:
        raise ArtifactBuildError("staged ZIP identity changed") from error
    if not stat.S_ISREG(status.st_mode) or _identity(status) != expected:
        raise ArtifactBuildError("staged ZIP identity changed")


def _unlink_if_owned(
    path: Path,
    expected: tuple[int, int],
    directory_descriptor: int,
    name: str,
) -> None:
    try:
        status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if stat.S_ISREG(status.st_mode) and _identity(status) == expected:
            os.unlink(name, dir_fd=directory_descriptor)
            return
    except OSError:
        pass
    try:
        status = path.lstat()
    except OSError:
        return
    if stat.S_ISREG(status.st_mode) and _identity(status) == expected:
        try:
            path.unlink()
        except OSError:
            pass


def _publish_no_replace(
    staged: Path,
    final: Path,
    directory_descriptor: int,
    staged_identity: tuple[int, int],
    output_dir: Path,
) -> None:
    _require_output_directory(output_dir, directory_descriptor)
    try:
        os.stat(final.name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ArtifactBuildError("artifact destination could not be inspected") from error
    else:
        raise FileExistsError(f"artifact already exists: {final.name}")
    try:
        if sys.platform == "darwin":
            _rename_no_replace(
                staged, final, "renameatx_np", 0x00000004, directory_descriptor
            )
        elif sys.platform.startswith("linux"):
            _rename_no_replace(staged, final, "renameat2", 1, directory_descriptor)
        elif os.name == "nt":
            os.rename(staged, final)
        else:
            raise ArtifactBuildError("atomic no-overwrite publication is unavailable")
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(f"artifact already exists: {final.name}") from error
        raise ArtifactBuildError("artifact publication failed") from error
    try:
        _require_output_directory(output_dir, directory_descriptor)
    except ArtifactBuildError:
        _unlink_named_if_owned(
            directory_descriptor, final.name, staged_identity
        )
        raise


def _rename_no_replace(
    staged: Path,
    final: Path,
    symbol: str,
    flag: int,
    directory_descriptor: int,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, symbol, None)
    if function is None:
        raise ArtifactBuildError("atomic no-overwrite publication is unavailable")
    if symbol == "renameatx_np":
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
    else:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
    arguments: tuple[Any, ...] = (
        directory_descriptor,
        os.fsencode(staged.name),
        directory_descriptor,
        os.fsencode(final.name),
        flag,
    )
    function.restype = ctypes.c_int
    if function(*arguments) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def _unlink_named_if_owned(
    directory_descriptor: int, name: str, expected: tuple[int, int]
) -> None:
    try:
        status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if stat.S_ISREG(status.st_mode) and _identity(status) == expected:
            os.unlink(name, dir_fd=directory_descriptor)
    except OSError:
        pass
