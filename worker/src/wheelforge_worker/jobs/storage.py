from __future__ import annotations

import hashlib
import errno
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_CHUNK_SIZE = 64 * 1024
_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_GENERATED_STAGE = re.compile(
    rf"\.wf-stage-(?:(?P<owner>{_UUID_PATTERN})-)?{_UUID_PATTERN}\Z"
)
_GENERATED_WORKSPACE = re.compile(rf"wf-execution-({_UUID_PATTERN})\Z")
_GENERATED_ARTIFACT = re.compile(
    rf"artifacts/(?P<task>{_UUID_PATTERN})/"
    rf"(?:(?P<owner>{_UUID_PATTERN})/)?{_UUID_PATTERN}\.zip\Z"
)


class InvalidObjectKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublishedObject:
    object_key: str
    size_bytes: int
    sha256: str


@dataclass(slots=True)
class _ParentBinding:
    path: Path
    descriptor: int | None

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


class RootedLocalStorage:
    def __init__(
        self,
        root: Path,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        _require_supported_host()
        self._root_descriptor: int | None
        self.root, descriptor = _open_stable_root(Path(root), "data root")
        self._root_descriptor = descriptor
        self._root_identity = _identity(os.fstat(self._root_descriptor))
        self._uuid_factory = uuid_factory

    def close(self) -> None:
        if self._root_descriptor is not None:
            os.close(self._root_descriptor)
            self._root_descriptor = None

    def __del__(self) -> None:
        descriptor = getattr(self, "_root_descriptor", None)
        if descriptor is not None:
            _best_effort(os.close, descriptor)

    def _duplicate_root(self) -> int:
        if self._root_descriptor is None:
            raise RuntimeError("data root is closed")
        return os.dup(self._root_descriptor)

    @property
    def root_identity(self) -> tuple[int, int]:
        return self._root_identity

    def read_bytes(self, object_key: str) -> bytes:
        parts = _object_key_parts(object_key)
        parent = self._open_parent(parts[:-1], create=False)
        try:
            descriptor = _open_regular(parent, parts[-1], os.O_RDONLY)
            try:
                before = os.fstat(descriptor)
                with os.fdopen(os.dup(descriptor), "rb") as handle:
                    content = handle.read()
                after = os.fstat(descriptor)
                _require_same_regular(before, after)
                _require_named_identity(parent, parts[-1], _identity(after))
                return content
            finally:
                os.close(descriptor)
        finally:
            parent.close()

    def publish_bytes(
        self,
        object_key: str,
        content: bytes,
        *,
        owner_execution_id: str | None = None,
    ) -> PublishedObject:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        return self._publish(
            object_key,
            lambda handle: _write_bytes(handle, content),
            owner_execution_id=owner_execution_id,
        )

    def publish_or_reuse_bytes(
        self,
        object_key: str,
        content: bytes,
        *,
        owner_execution_id: str | None = None,
    ) -> PublishedObject:
        try:
            return self.publish_bytes(
                object_key, content, owner_execution_id=owner_execution_id
            )
        except FileExistsError:
            observed = self.read_bytes(object_key)
            digest = hashlib.sha256(content).hexdigest()
            if len(observed) != len(content) or hashlib.sha256(observed).hexdigest() != digest:
                raise
            return PublishedObject(object_key, len(content), digest)

    def publish_file(
        self,
        object_key: str,
        source: Path,
        *,
        owner_execution_id: str | None = None,
    ) -> PublishedObject:
        source = Path(source)

        def copy(handle: BinaryIO) -> tuple[int, str]:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source_descriptor = os.open(source, flags)
            try:
                before = os.fstat(source_descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise OSError("artifact source must be a regular file")
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(os.dup(source_descriptor), "rb") as input_handle:
                    while chunk := input_handle.read(_CHUNK_SIZE):
                        handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                after = os.fstat(source_descriptor)
                _require_same_regular(before, after)
                return size, digest.hexdigest()
            finally:
                os.close(source_descriptor)

        return self._publish(
            object_key, copy, owner_execution_id=owner_execution_id
        )

    def delete_if_owned(self, object_key: str, expected_sha256: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return False
        parts = _object_key_parts(object_key)
        try:
            parent = self._open_parent(parts[:-1], create=False)
        except FileNotFoundError:
            return False
        try:
            try:
                descriptor = _open_regular(parent, parts[-1], os.O_RDONLY)
            except FileNotFoundError:
                return False
            try:
                before = os.fstat(descriptor)
                digest = _hash_descriptor(descriptor)
                after = os.fstat(descriptor)
                _require_same_regular(before, after)
                identity = _identity(after)
                _require_named_identity(parent, parts[-1], identity)
                if digest != expected_sha256:
                    return False
                _unlink_named(parent, parts[-1])
                _fsync_parent(parent)
                return True
            finally:
                os.close(descriptor)
        finally:
            parent.close()

    def _publish(
        self,
        object_key: str,
        writer: Callable[[BinaryIO], tuple[int, str]],
        *,
        owner_execution_id: str | None,
    ) -> PublishedObject:
        parts = _object_key_parts(object_key)
        parent = self._open_parent(parts[:-1], create=True)
        owner = str(UUID(owner_execution_id)) if owner_execution_id else None
        stage_name = ".wf-stage-{}{}".format(
            f"{owner}-" if owner else "", self._uuid_factory()
        )
        descriptor: int | None = None
        staged = False
        try:
            descriptor = _create_exclusive(parent, stage_name)
            staged = True
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                size, digest = writer(handle)
                handle.flush()
                os.fsync(handle.fileno())
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size != size:
                raise OSError("published object changed while it was copied")
            _require_named_identity(parent, stage_name, _identity(status))
            _link_no_replace(parent, stage_name, parts[-1])
            _unlink_named(parent, stage_name)
            _fsync_parent(parent)
            staged = False
            return PublishedObject(object_key, size, digest)
        finally:
            if descriptor is not None:
                _best_effort(os.close, descriptor)
            if staged:
                _best_effort(_unlink_named, parent, stage_name)
            _best_effort(parent.close)

    def _open_parent(self, parts: tuple[str, ...], *, create: bool) -> _ParentBinding:
        supports_dir_fd = os.open in os.supports_dir_fd
        if supports_dir_fd:
            descriptor = self._duplicate_root()
            current = self.root
            try:
                for part in parts:
                    if create:
                        try:
                            os.mkdir(part, 0o700, dir_fd=descriptor)
                        except FileExistsError:
                            pass
                    next_descriptor = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                    os.close(descriptor)
                    descriptor = next_descriptor
                    current /= part
                return _ParentBinding(current, descriptor)
            except BaseException:
                os.close(descriptor)
                raise

        raise RuntimeError("safe descriptor-relative storage is unavailable")

    def sweep_abandoned(
        self,
        cutoff: datetime,
        referenced_artifact_keys: frozenset[str],
        *,
        active_execution_ids: frozenset[str],
        active_build_executions: frozenset[tuple[str, str]],
    ) -> None:
        cutoff_timestamp = cutoff.timestamp()
        root_descriptor = self._duplicate_root()
        try:
            for directory, directories, filenames, descriptor in os.fwalk(
                ".", topdown=True, follow_symlinks=False, dir_fd=root_descriptor
            ):
                safe_directories: list[str] = []
                for name in directories:
                    status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(status.st_mode) and not stat.S_ISLNK(status.st_mode):
                        safe_directories.append(name)
                directories[:] = safe_directories
                relative_directory = Path(directory)
                for name in filenames:
                    relative = (relative_directory / name).as_posix()
                    relative = relative.removeprefix("./")
                    stage_match = _GENERATED_STAGE.fullmatch(name)
                    artifact_match = _GENERATED_ARTIFACT.fullmatch(relative)
                    if stage_match is None and artifact_match is None:
                        continue
                    if (
                        stage_match is not None
                        and stage_match.group("owner") in active_execution_ids
                    ):
                        continue
                    if artifact_match is not None and (
                        relative in referenced_artifact_keys
                        or (
                            artifact_match.group("owner") is not None
                            and (
                                artifact_match.group("task"),
                                artifact_match.group("owner"),
                            )
                            in active_build_executions
                        )
                    ):
                        continue
                    status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(status.st_mode)
                        or status.st_mtime > cutoff_timestamp
                    ):
                        continue
                    os.unlink(name, dir_fd=descriptor)
                    _fsync_descriptor(descriptor)
        finally:
            os.close(root_descriptor)


@dataclass(slots=True)
class OwnedWorkspace:
    path: Path
    _root_descriptor: int | None
    _identity: tuple[int, int]
    _cleaned: bool = False

    def cleanup(self) -> None:
        if self._cleaned:
            return
        if self._root_descriptor is None:
            raise RuntimeError("workspace root is closed")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("safe descriptor-relative cleanup is unavailable")
        root_descriptor = os.dup(self._root_descriptor)
        try:
            current = os.stat(
                self.path.name, dir_fd=root_descriptor, follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _is_reparse(current)
                or _identity(current) != self._identity
            ):
                raise RuntimeError("workspace identity changed")
            shutil.rmtree(self.path.name, dir_fd=root_descriptor)
            _fsync_descriptor(root_descriptor)
            self._cleaned = True
            self.close()
        finally:
            os.close(root_descriptor)

    def close(self) -> None:
        if self._root_descriptor is not None:
            os.close(self._root_descriptor)
            self._root_descriptor = None

    def __del__(self) -> None:
        descriptor = getattr(self, "_root_descriptor", None)
        if descriptor is not None:
            _best_effort(os.close, descriptor)


class WorkspaceManager:
    def __init__(
        self,
        root: Path,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        _require_supported_host()
        self._root_descriptor: int | None
        self.root, descriptor = _open_stable_root(Path(root), "workspace root")
        self._root_descriptor = descriptor
        self._root_identity = _identity(os.fstat(self._root_descriptor))
        self._uuid_factory = uuid_factory

    def close(self) -> None:
        if self._root_descriptor is not None:
            os.close(self._root_descriptor)
            self._root_descriptor = None

    def __del__(self) -> None:
        descriptor = getattr(self, "_root_descriptor", None)
        if descriptor is not None:
            _best_effort(os.close, descriptor)

    def _duplicate_root(self) -> int:
        if self._root_descriptor is None:
            raise RuntimeError("workspace root is closed")
        return os.dup(self._root_descriptor)

    @property
    def root_identity(self) -> tuple[int, int]:
        return self._root_identity

    def allocate(self, execution_id: str | None = None) -> OwnedWorkspace:
        attempts = 1 if execution_id is not None else 128
        root_descriptor = self._duplicate_root()
        try:
            for _attempt in range(attempts):
                identifier = (
                    UUID(execution_id)
                    if execution_id is not None
                    else self._uuid_factory()
                )
                name = f"wf-execution-{identifier}"
                path = self.root / name
                try:
                    os.mkdir(name, 0o700, dir_fd=root_descriptor)
                except FileExistsError:
                    continue
                try:
                    status = os.stat(
                        name, dir_fd=root_descriptor, follow_symlinks=False
                    )
                    if (
                        not stat.S_ISDIR(status.st_mode)
                        or stat.S_ISLNK(status.st_mode)
                        or _is_reparse(status)
                    ):
                        raise RuntimeError(
                            "allocated workspace is not a plain directory"
                        )
                    self._require_path_binding(path, _identity(status))
                    return OwnedWorkspace(
                        path,
                        os.dup(root_descriptor),
                        _identity(status),
                    )
                except BaseException:
                    _best_effort(shutil.rmtree, name, dir_fd=root_descriptor)
                    raise
            raise RuntimeError("could not allocate a unique workspace")
        finally:
            os.close(root_descriptor)

    def _require_path_binding(
        self, workspace_path: Path, workspace_identity: tuple[int, int]
    ) -> None:
        root_status = self.root.lstat()
        if _identity(root_status) != self._root_identity:
            raise RuntimeError("workspace root identity changed")
        workspace_status = workspace_path.lstat()
        if _identity(workspace_status) != workspace_identity:
            raise RuntimeError("workspace root identity changed")

    def sweep_abandoned(
        self, cutoff: datetime, active_execution_ids: frozenset[str]
    ) -> None:
        cutoff_timestamp = cutoff.timestamp()
        root_descriptor = self._duplicate_root()
        try:
            with os.scandir(root_descriptor) as entries:
                for entry in entries:
                    match = _GENERATED_WORKSPACE.fullmatch(entry.name)
                    if match is None or match.group(1) in active_execution_ids:
                        continue
                    status = entry.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISDIR(status.st_mode)
                        or entry.is_symlink()
                        or status.st_mtime > cutoff_timestamp
                    ):
                        continue
                    current = os.stat(
                        entry.name,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    if _identity(current) != _identity(status):
                        continue
                    shutil.rmtree(entry.name, dir_fd=root_descriptor)
            _fsync_descriptor(root_descriptor)
        finally:
            os.close(root_descriptor)


def _object_key_parts(object_key: str) -> tuple[str, ...]:
    if not isinstance(object_key, str) or not object_key or "\x00" in object_key:
        raise InvalidObjectKey("object key must be a non-empty relative POSIX path")
    if "\\" in object_key:
        raise InvalidObjectKey("object key must use relative POSIX components")
    parts = tuple(object_key.split("/"))
    if any(part in ("", ".", "..") or _DRIVE_PREFIX.match(part) for part in parts):
        raise InvalidObjectKey("object key must use safe relative components")
    return parts


def _require_supported_host() -> None:
    if os.name == "nt":
        raise RuntimeError(
            "Windows Worker hosts are not supported; Windows remains a target platform"
        )
    required = (os.open, os.stat, os.mkdir, os.unlink, os.link)
    if any(operation not in os.supports_dir_fd for operation in required):
        raise RuntimeError("safe descriptor-relative filesystem operations are unavailable")


def _open_stable_root(path: Path, label: str) -> tuple[Path, int]:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    component, flags, dir_fd=descriptor
                )
            except OSError as error:
                raise ValueError(f"{label} must not traverse links") from error
            os.close(descriptor)
            descriptor = next_descriptor
            status = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(status.st_mode)
                or stat.S_ISLNK(status.st_mode)
                or _is_reparse(status)
            ):
                raise ValueError(f"{label} must not traverse links")
        return path.absolute(), descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_plain_directory(path: Path) -> None:
    status = path.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or _is_reparse(status)
    ):
        raise ValueError("path must be a plain directory")


def _is_reparse(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _open_regular(parent: _ParentBinding, name: str, flags: int) -> int:
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if parent.descriptor is not None:
        descriptor = os.open(name, flags, dir_fd=parent.descriptor)
    else:
        descriptor = os.open(parent.path / name, flags)
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode):
        os.close(descriptor)
        raise OSError("object must be a regular file")
    return descriptor


def _create_exclusive(parent: _ParentBinding, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if parent.descriptor is not None:
        return os.open(name, flags, 0o600, dir_fd=parent.descriptor)
    return os.open(parent.path / name, flags, 0o600)


def _require_named_identity(
    parent: _ParentBinding, name: str, expected: tuple[int, int]
) -> None:
    if parent.descriptor is not None:
        status = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
    else:
        status = (parent.path / name).lstat()
    if not stat.S_ISREG(status.st_mode) or _identity(status) != expected:
        raise OSError("object identity changed")


def _link_no_replace(parent: _ParentBinding, source: str, target: str) -> None:
    if parent.descriptor is not None and os.link in os.supports_dir_fd:
        os.link(
            source,
            target,
            src_dir_fd=parent.descriptor,
            dst_dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    else:
        os.link(parent.path / source, parent.path / target, follow_symlinks=False)


def _unlink_named(parent: _ParentBinding, name: str) -> None:
    if parent.descriptor is not None and os.unlink in os.supports_dir_fd:
        os.unlink(name, dir_fd=parent.descriptor)
    else:
        os.unlink(parent.path / name)


def _fsync_parent(parent: _ParentBinding) -> None:
    if parent.descriptor is None:
        raise RuntimeError("safe parent directory handle is unavailable")
    try:
        os.fsync(parent.descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
            raise


def _fsync_descriptor(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
            raise


def _write_bytes(handle: BinaryIO, content: bytes) -> tuple[int, str]:
    handle.write(content)
    return len(content), hashlib.sha256(content).hexdigest()


def _best_effort(
    operation: Callable[..., object], *args: object, **kwargs: object
) -> None:
    try:
        operation(*args, **kwargs)
    except Exception:
        # Exact generated entries are reconciled by age-qualified maintenance.
        pass


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _require_same_regular(before: os.stat_result, after: os.stat_result) -> None:
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or _identity(before) != _identity(after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise OSError("regular file changed while it was open")
