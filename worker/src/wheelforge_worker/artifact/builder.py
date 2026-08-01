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
import secrets
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Protocol, cast
from uuid import UUID
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename

from wheelforge_worker.resolver import VersionChange
from wheelforge_worker.validation import (
    ArchiveValidationReport,
    StaticValidationReport,
    ValidatedWheelSnapshot,
    validate_closure,
)

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


_FileIdentity = tuple[int, int]


class _WindowsApi(Protocol):
    def create_stage(self, path: Path) -> int: ...

    def open_directory(self, path: Path) -> int: ...

    def close_handle(self, handle: int) -> None: ...

    def descriptor_handle(self, descriptor: int) -> int: ...

    def handle_identity(self, handle: int) -> _FileIdentity: ...

    def handle_is_reparse(self, handle: int) -> bool: ...

    def path_identity(
        self, path: Path, *, directory: bool
    ) -> _FileIdentity | None: ...

    def rename_handle(
        self, handle: int, directory_handle: int, final_name: str
    ) -> None: ...

    def delete_handle(self, handle: int) -> None: ...


class ArtifactBuilder:
    def __init__(self) -> None:
        self._publication_backend = _select_publication_backend()

    def build(self, context: ArtifactBuildContext, output_dir: Path) -> BuiltArtifact:
        if not isinstance(context, ArtifactBuildContext):
            raise ArtifactBuildError("artifact build context is required")
        snapshots = _discover_snapshots(context.wheels)
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
        validation = _validate_context(context)
        final_path = output_dir / f"wheelforge-{context.build_id}.zip"
        if final_path.exists():
            raise FileExistsError(f"artifact already exists: {final_path.name}")

        packaged = _packaged_by_name(context)
        intended = _intended_packages(context, packaged)
        complete = validation.complete and len(packaged) == len(intended)
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

        publication = _open_publication_backend(self._publication_backend, output_dir)
        try:
            descriptor, staged_path = publication.create_stage()
            try:
                try:
                    staged_identity = publication.stage_identity(
                        descriptor, staged_path
                    )
                except BaseException:
                    publication.cleanup_unbound_stage(descriptor, staged_path)
                    raise
                try:
                    with _duplicate_stream(descriptor, "w+b") as staged:
                        _write_zip(staged, entries, package_entries)
                        staged.flush()
                        os.fsync(staged.fileno())
                    publication.require_stage(
                        descriptor, staged_path, staged_identity
                    )
                    with _duplicate_stream(descriptor, "rb") as staged:
                        _verify_zip(staged, expected_names, hashes)
                    publication.require_stage(
                        descriptor, staged_path, staged_identity
                    )
                    publication.require_output()
                    with _duplicate_stream(descriptor, "rb") as staged:
                        digest = _hash_stream(staged)
                    publication.require_stage(
                        descriptor, staged_path, staged_identity
                    )
                    publication.require_output()
                    publication.publish(
                        descriptor, staged_path, final_path, staged_identity
                    )
                    return BuiltArtifact(final_path, digest, manifest)
                except BaseException:
                    publication.cleanup_stage(
                        descriptor, staged_path, staged_identity
                    )
                    raise
            finally:
                _cleanup_preserving_primary(lambda: os.close(descriptor))
        finally:
            _cleanup_preserving_primary(publication.close)


def _discover_snapshots(wheels: object) -> tuple[ValidatedWheelSnapshot, ...]:
    if not isinstance(wheels, tuple):
        return ()
    snapshots: list[ValidatedWheelSnapshot] = []
    snapshot_ids: set[int] = set()
    for item in wheels:
        if not isinstance(item, ValidatedWheel) or not isinstance(
            item.report, ArchiveValidationReport
        ):
            continue
        snapshot = item.report.snapshot
        if isinstance(snapshot, ValidatedWheelSnapshot) and id(snapshot) not in snapshot_ids:
            snapshots.append(snapshot)
            snapshot_ids.add(id(snapshot))
    return tuple(snapshots)


def _select_publication_backend() -> str:
    if sys.platform == "darwin":
        if _unix_publication_capabilities("renameatx_np"):
            return "unix"
    elif sys.platform.startswith("linux"):
        if _unix_publication_capabilities("renameat2"):
            return "unix"
    elif os.name == "nt" and sys.platform == "win32":
        if _windows_publication_capabilities():
            return "windows"
        raise ArtifactBuildError("safe Windows publication backend is unavailable")
    raise ArtifactBuildError("safe host publication backend is unavailable")


def _unix_publication_capabilities(rename_symbol: str) -> bool:
    required_dir_fd = (os.stat, os.unlink)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        return False
    try:
        return getattr(ctypes.CDLL(None), rename_symbol, None) is not None
    except OSError:
        return False


def _windows_publication_capabilities() -> bool:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return False
    try:
        kernel32 = win_dll("kernel32", use_last_error=True)
    except OSError:
        return False
    return all(
        getattr(kernel32, name, None) is not None
        for name in (
            "CreateFileW",
            "CloseHandle",
            "GetFileInformationByHandle",
            "ReOpenFile",
            "SetFileInformationByHandle",
        )
    )


class _PublicationBackend(Protocol):
    def create_stage(self) -> tuple[int, Path]: ...

    def stage_identity(self, descriptor: int, staged: Path) -> _FileIdentity: ...

    def require_output(self) -> None: ...

    def require_stage(
        self, descriptor: int, staged: Path, expected: _FileIdentity
    ) -> None: ...

    def cleanup_stage(
        self, descriptor: int, staged: Path, expected: _FileIdentity
    ) -> None: ...

    def cleanup_unbound_stage(self, descriptor: int, staged: Path) -> None: ...

    def publish(
        self,
        descriptor: int,
        staged: Path,
        final: Path,
        expected: _FileIdentity,
    ) -> None: ...

    def close(self) -> None: ...


def _open_publication_backend(kind: str, output_dir: Path) -> _PublicationBackend:
    if kind == "unix":
        return _UnixPublicationBackend(output_dir)
    if kind == "windows":
        return _WindowsPublicationBackend(output_dir, _WindowsNativeApi())
    raise ArtifactBuildError("safe host publication backend is unavailable")


class _UnixPublicationBackend:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._directory_descriptor = _open_output_directory(output_dir)

    def create_stage(self) -> tuple[int, Path]:
        descriptor, name = tempfile.mkstemp(
            prefix=".wheelforge-artifact-",
            suffix=".tmp",
            dir=self._output_dir,
        )
        return descriptor, Path(name)

    def stage_identity(self, descriptor: int, staged: Path) -> _FileIdentity:
        expected = _identity(os.fstat(descriptor))
        self.require_stage(descriptor, staged, expected)
        return expected

    def require_output(self) -> None:
        _require_output_directory(self._output_dir, self._directory_descriptor)

    def require_stage(
        self, descriptor: int, staged: Path, expected: _FileIdentity
    ) -> None:
        if _identity(os.fstat(descriptor)) != expected:
            raise ArtifactBuildError("staged ZIP descriptor identity changed")
        _require_owned_stage(self._directory_descriptor, staged.name, expected)

    def cleanup_stage(
        self, descriptor: int, staged: Path, expected: _FileIdentity
    ) -> None:
        _unlink_if_owned(
            staged, expected, self._directory_descriptor, staged.name
        )

    def cleanup_unbound_stage(self, descriptor: int, staged: Path) -> None:
        try:
            expected = _identity(os.fstat(descriptor))
        except OSError:
            return
        _unlink_if_owned(
            staged, expected, self._directory_descriptor, staged.name
        )

    def publish(
        self,
        descriptor: int,
        staged: Path,
        final: Path,
        expected: _FileIdentity,
    ) -> None:
        self.require_stage(descriptor, staged, expected)
        _publish_no_replace(
            staged,
            final,
            self._directory_descriptor,
            expected,
            self._output_dir,
        )

    def close(self) -> None:
        os.close(self._directory_descriptor)


class _WindowsPublicationBackend:
    def __init__(self, output_dir: Path, api: _WindowsApi) -> None:
        self._output_dir = output_dir
        self._api = api
        self._directory_handle = api.open_directory(output_dir)
        self._stage_handles: dict[int, int] = {}
        try:
            if api.handle_is_reparse(self._directory_handle):
                raise ArtifactBuildError(
                    "Windows reparse output directories are not allowed"
                )
            self._directory_identity = api.handle_identity(self._directory_handle)
            self.require_output()
        except BaseException:
            _cleanup_preserving_primary(
                lambda: api.close_handle(self._directory_handle)
            )
            raise

    def create_stage(self) -> tuple[int, Path]:
        for _attempt in range(128):
            staged = self._output_dir / (
                f".wheelforge-artifact-{secrets.token_hex(16)}.tmp"
            )
            try:
                return self._api.create_stage(staged), staged
            except OSError as error:
                if error.errno == errno.EEXIST or getattr(
                    error, "winerror", None
                ) in {80, 183}:
                    continue
                raise
        raise ArtifactBuildError("could not allocate a private staged ZIP")

    def _stage_handle(self, descriptor: int) -> int:
        handle = self._stage_handles.get(descriptor)
        if handle is None:
            handle = self._api.descriptor_handle(descriptor)
            self._stage_handles[descriptor] = handle
        return handle

    def stage_identity(self, descriptor: int, staged: Path) -> _FileIdentity:
        handle = self._stage_handle(descriptor)
        expected = self._api.handle_identity(handle)
        self.require_stage(descriptor, staged, expected)
        return expected

    def require_output(self) -> None:
        if (
            self._api.handle_is_reparse(self._directory_handle)
            or self._api.handle_identity(self._directory_handle)
            != self._directory_identity
            or self._api.path_identity(self._output_dir, directory=True)
            != self._directory_identity
        ):
            raise ArtifactBuildError("output directory identity changed")

    def require_stage(
        self, descriptor: int, staged: Path, expected: _FileIdentity
    ) -> None:
        self.require_output()
        handle = self._stage_handle(descriptor)
        if (
            self._api.handle_identity(handle) != expected
            or self._api.path_identity(staged, directory=False) != expected
        ):
            raise ArtifactBuildError("staged ZIP identity changed")

    def cleanup_stage(
        self, descriptor: int, staged: Path, expected: _FileIdentity
    ) -> None:
        try:
            handle = self._stage_handle(descriptor)
            if self._api.handle_identity(handle) == expected:
                self._api.delete_handle(handle)
        except (OSError, ArtifactBuildError):
            pass

    def cleanup_unbound_stage(self, descriptor: int, staged: Path) -> None:
        try:
            self._api.delete_handle(self._stage_handle(descriptor))
        except (OSError, ArtifactBuildError):
            pass

    def publish(
        self,
        descriptor: int,
        staged: Path,
        final: Path,
        expected: _FileIdentity,
    ) -> None:
        self.require_stage(descriptor, staged, expected)
        try:
            final_identity = self._api.path_identity(final, directory=False)
        except OSError as error:
            raise ArtifactBuildError(
                "artifact destination could not be inspected"
            ) from error
        if final_identity is not None:
            raise FileExistsError(f"artifact already exists: {final.name}")
        handle = self._stage_handle(descriptor)
        try:
            self._api.rename_handle(handle, self._directory_handle, final.name)
        except OSError as error:
            if error.errno in {errno.EEXIST, errno.ENOTEMPTY} or getattr(
                error, "winerror", None
            ) in {80, 183}:
                raise FileExistsError(
                    f"artifact already exists: {final.name}"
                ) from error
            raise ArtifactBuildError("artifact publication failed") from error
        self.require_output()
        if self._api.path_identity(final, directory=False) != expected:
            raise ArtifactBuildError("published ZIP identity changed")

    def close(self) -> None:
        first_error: BaseException | None = None
        handles = (*self._stage_handles.values(), self._directory_handle)
        self._stage_handles.clear()
        for handle in handles:
            try:
                self._api.close_handle(handle)
            except BaseException as error:
                first_error = first_error or error
        if first_error is not None:
            raise first_error


class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_uint32),
        ("creation_time_low", ctypes.c_uint32),
        ("creation_time_high", ctypes.c_uint32),
        ("access_time_low", ctypes.c_uint32),
        ("access_time_high", ctypes.c_uint32),
        ("write_time_low", ctypes.c_uint32),
        ("write_time_high", ctypes.c_uint32),
        ("volume_serial_number", ctypes.c_uint32),
        ("file_size_high", ctypes.c_uint32),
        ("file_size_low", ctypes.c_uint32),
        ("number_of_links", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    ]


class _WindowsRenameInformation(ctypes.Structure):
    _fields_ = [
        ("replace_if_exists", ctypes.c_ubyte),
        ("root_directory", ctypes.c_void_p),
        ("file_name_length", ctypes.c_uint32),
        ("file_name", ctypes.c_wchar * 1),
    ]


class _WindowsDispositionInformation(ctypes.Structure):
    _fields_ = [("delete_file", ctypes.c_ubyte)]


class _WindowsNativeApi:
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_ALL = 0x00000007
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_ATTRIBUTE_TEMPORARY = 0x00000100
    _FILE_RENAME_INFORMATION = 3
    _FILE_DISPOSITION_INFORMATION = 4
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self) -> None:
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise ArtifactBuildError("safe Windows publication backend is unavailable")
        self._kernel32 = win_dll("kernel32", use_last_error=True)
        self._configure_functions()

    def _configure_functions(self) -> None:
        self._kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._kernel32.CreateFileW.restype = ctypes.c_void_p
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32.GetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_WindowsFileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = ctypes.c_int
        self._kernel32.ReOpenFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._kernel32.ReOpenFile.restype = ctypes.c_void_p
        self._kernel32.SetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._kernel32.SetFileInformationByHandle.restype = ctypes.c_int

    def open_directory(self, path: Path) -> int:
        return self._open_path(
            path,
            self._FILE_LIST_DIRECTORY | self._FILE_READ_ATTRIBUTES,
            self._FILE_FLAG_BACKUP_SEMANTICS
            | self._FILE_FLAG_OPEN_REPARSE_POINT,
        )

    def create_stage(self, path: Path) -> int:
        handle = self._open_path(
            path,
            self._GENERIC_READ | self._GENERIC_WRITE | self._DELETE,
            self._FILE_FLAG_OPEN_REPARSE_POINT | self._FILE_ATTRIBUTE_TEMPORARY,
            creation=self._CREATE_NEW,
            share=self._FILE_SHARE_ALL,
        )
        try:
            try:
                import msvcrt
            except ImportError as error:
                raise ArtifactBuildError(
                    "safe Windows publication backend is unavailable"
                ) from error
            return msvcrt.open_osfhandle(  # type: ignore[attr-defined]
                handle, os.O_RDWR | getattr(os, "O_BINARY", 0)
            )
        except BaseException:
            _cleanup_preserving_primary(lambda: self.delete_handle(handle))
            _cleanup_preserving_primary(lambda: self.close_handle(handle))
            raise

    def _open_path(
        self,
        path: Path,
        access: int,
        flags: int,
        *,
        creation: int = _OPEN_EXISTING,
        share: int = _FILE_SHARE_ALL,
    ) -> int:
        handle = self._kernel32.CreateFileW(
            str(path),
            access,
            share,
            None,
            creation,
            flags,
            None,
        )
        if handle == self._INVALID_HANDLE_VALUE:
            self._raise_last_error(f"could not open {path}")
        return int(handle)

    def close_handle(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            self._raise_last_error("could not close Windows file handle")

    def descriptor_handle(self, descriptor: int) -> int:
        try:
            import msvcrt
        except ImportError as error:
            raise ArtifactBuildError(
                "safe Windows publication backend is unavailable"
            ) from error
        source = msvcrt.get_osfhandle(descriptor)  # type: ignore[attr-defined]
        handle = self._kernel32.ReOpenFile(
            source,
            self._GENERIC_READ
            | self._GENERIC_WRITE
            | self._DELETE
            | self._FILE_READ_ATTRIBUTES,
            self._FILE_SHARE_ALL,
            0,
        )
        if handle == self._INVALID_HANDLE_VALUE:
            self._raise_last_error("could not bind staged ZIP handle")
        return int(handle)

    def handle_identity(self, handle: int) -> _FileIdentity:
        information = self._handle_information(handle)
        file_index = (information.file_index_high << 32) | information.file_index_low
        return information.volume_serial_number, file_index

    def handle_is_reparse(self, handle: int) -> bool:
        information = self._handle_information(handle)
        return bool(
            information.file_attributes & self._FILE_ATTRIBUTE_REPARSE_POINT
        )

    def _handle_information(self, handle: int) -> _WindowsFileInformation:
        information = _WindowsFileInformation()
        if not self._kernel32.GetFileInformationByHandle(
            handle, ctypes.byref(information)
        ):
            self._raise_last_error("could not inspect Windows file handle")
        return information

    def path_identity(
        self, path: Path, *, directory: bool
    ) -> _FileIdentity | None:
        try:
            handle = self._open_path(
                path,
                self._FILE_READ_ATTRIBUTES,
                self._FILE_FLAG_OPEN_REPARSE_POINT
                | (self._FILE_FLAG_BACKUP_SEMANTICS if directory else 0),
            )
        except OSError as error:
            if getattr(error, "winerror", None) in {2, 3}:
                return None
            raise
        try:
            if self.handle_is_reparse(handle):
                raise ArtifactBuildError(
                    "Windows reparse path substitutions are not allowed"
                )
            return self.handle_identity(handle)
        finally:
            _cleanup_preserving_primary(lambda: self.close_handle(handle))

    def rename_handle(
        self, handle: int, directory_handle: int, final_name: str
    ) -> None:
        encoded_name = final_name.encode("utf-16-le")
        name_offset = _WindowsRenameInformation.file_name.offset
        size = name_offset + len(encoded_name)
        buffer = ctypes.create_string_buffer(size)
        ctypes.c_ubyte.from_buffer(
            buffer, _WindowsRenameInformation.replace_if_exists.offset
        ).value = 0
        ctypes.c_void_p.from_buffer(
            buffer, _WindowsRenameInformation.root_directory.offset
        ).value = directory_handle
        ctypes.c_uint32.from_buffer(
            buffer, _WindowsRenameInformation.file_name_length.offset
        ).value = len(encoded_name)
        ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
        if not self._kernel32.SetFileInformationByHandle(
            handle, self._FILE_RENAME_INFORMATION, buffer, size
        ):
            self._raise_last_error("could not publish staged ZIP")

    def delete_handle(self, handle: int) -> None:
        information = _WindowsDispositionInformation(1)
        if not self._kernel32.SetFileInformationByHandle(
            handle,
            self._FILE_DISPOSITION_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            self._raise_last_error("could not remove staged ZIP")

    @staticmethod
    def _raise_last_error(message: str) -> None:
        get_last_error = getattr(ctypes, "get_last_error", None)
        number = 1 if get_last_error is None else get_last_error()
        raise OSError(number, message, None, number)


def _validate_output_directory(output_dir: Path) -> None:
    try:
        status = output_dir.lstat()
    except OSError as error:
        raise ArtifactBuildError("output directory must already exist") from error
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ArtifactBuildError("output directory must be a real directory")


def _validate_context(context: ArtifactBuildContext) -> StaticValidationReport:
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
    if context.version_changes != context.resolution.changes:
        raise ArtifactBuildError(
            "version comparison does not match resolution changes"
        )

    resolved: dict[str, Any] = {}
    for package in context.resolution.packages:
        name = canonicalize_name(package.name)
        if name in resolved:
            raise ArtifactBuildError("resolved package identities must be unique")
        resolved[name] = package

    packaged_names: set[str] = set()
    portable_paths: set[str] = set()
    for item in context.wheels:
        _validate_pair(item)
        name = canonicalize_name(item.download.package)
        if name in packaged_names or name not in resolved:
            raise ArtifactBuildError("validated package identities are inconsistent")
        package = resolved[name]
        if item.download.version != package.version:
            raise ArtifactBuildError("closure validation package version mismatch")
        if item.download.filename != package.wheel_filename:
            raise ArtifactBuildError("resolved Wheel filename does not match validation")
        packaged_names.add(name)
        package_path = f"packages/{item.download.filename}"
        key = package_path.casefold()
        if key in portable_paths or not _safe_zip_path(package_path):
            raise ArtifactBuildError("validated Wheel path is unsafe or duplicate")
        portable_paths.add(key)
    if context.validation.complete and packaged_names != set(resolved):
        raise ArtifactBuildError("complete validation requires every resolved Wheel")
    try:
        recomputed = validate_closure(
            context.resolution,
            (item.download for item in context.wheels),
            context.target,
        )
    except ValueError as error:
        raise ArtifactBuildError("closure validation inputs are invalid") from error
    if recomputed != context.validation:
        raise ArtifactBuildError("closure validation report does not match recomputed result")
    return recomputed


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
                item.code.value,
                item.package,
                item.detail,
                (0, "") if item.filename is None else (1, item.filename),
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
    location = (
        "#!/bin/sh\nset -eu\n"
        'SCRIPT_DIR=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd) || '
        "{ echo 'Artifact directory is unavailable' >&2; exit 1; }\n"
        'cd -- "$SCRIPT_DIR" || { echo \'Artifact directory is unavailable\' >&2; exit 1; }\n'
    )
    if not complete:
        failure = (
            location
            + "echo 'Incomplete artifact: installation is unavailable.' >&2\nexit 1\n"
        )
        return failure, failure
    machine = {"X86_64": "x86_64", "AARCH64": "aarch64"}[context.target.architecture]
    checks = (
        location
        +
        f"[ \"$(uname -s)\" = \"Linux\" ] || {{ echo 'Target OS mismatch' >&2; exit 1; }}\n"
        f"[ \"$(uname -m)\" = \"{machine}\" ] || {{ echo 'Target architecture mismatch' >&2; exit 1; }}\n"
        f"python -c \"import sys; raise SystemExit(sys.version_info[:2] != ({context.target.python_version.replace('.', ', ')}))\" || {{ echo 'Target Python mismatch' >&2; exit 1; }}\n"
    )
    install = checks + "python -m pip install --no-index --find-links packages --require-hashes -r requirements-resolved.txt\n"
    verify = checks + "sha256sum -c checksums.sha256\npython -m pip check\n"
    return install, verify


def _windows_scripts(context: ArtifactBuildContext, complete: bool) -> tuple[str, str]:
    location = (
        "@echo off\r\nsetlocal\r\n"
        'cd /d "%~dp0" || (echo Artifact directory is unavailable 1>&2 & exit /b 1)\r\n'
    )
    if not complete:
        failure = (
            location
            + "echo Incomplete artifact: installation is unavailable. 1>&2\r\nexit /b 1\r\n"
        )
        return failure, failure
    machine = {"AMD64": "AMD64", "ARM64": "ARM64"}[context.target.architecture]
    major, minor = context.target.python_version.split(".")
    checks = (
        location
        +
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
    staged: BinaryIO, expected_names: set[str], expected_hashes: dict[str, str]
) -> None:
    try:
        staged.seek(0)
        with ZipFile(staged) as archive:
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


def _hash_stream(staged: BinaryIO) -> str:
    staged.seek(0)
    digest = hashlib.sha256()
    while block := staged.read(_COPY_CHUNK_BYTES):
        digest.update(block)
    return digest.hexdigest()


def _duplicate_stream(descriptor: int, mode: str) -> BinaryIO:
    duplicate = -1
    try:
        duplicate = os.dup(descriptor)
        stream = os.fdopen(duplicate, mode)
        duplicate = -1
        return cast(BinaryIO, stream)
    except BaseException:
        if duplicate != -1:
            _cleanup_preserving_primary(lambda: os.close(duplicate))
        raise


def _cleanup_preserving_primary(operation: Callable[[], None]) -> None:
    active_error = sys.exception()
    try:
        operation()
    except BaseException:
        if active_error is None:
            raise


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
            _cleanup_preserving_primary(lambda: os.close(descriptor))
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
    try:
        published = os.stat(
            final.name, dir_fd=directory_descriptor, follow_symlinks=False
        )
    except OSError as error:
        raise ArtifactBuildError("published ZIP identity changed") from error
    if not stat.S_ISREG(published.st_mode) or _identity(published) != staged_identity:
        raise ArtifactBuildError("published ZIP identity changed")


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
