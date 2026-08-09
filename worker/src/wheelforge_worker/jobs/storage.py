from __future__ import annotations

import ctypes
import hashlib
import errno
import fcntl
import json
import os
import re
import stat
import sys
import time
import threading
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
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
_QUARANTINE = re.compile(rf"\.wf-quarantine-v1-(?P<id>{_UUID_PATTERN})\Z")
_QUARANTINE_XATTR = b"user.wheelforge.quarantine-v1"
_SWEEP_XATTR = b"user.wheelforge.sweep-v1"
_MAX_QUARANTINE_METADATA_BYTES = 8192
_MAX_SWEEP_STATE_BYTES = 32768
_RECOVERY_CANDIDATE = ".wf-recovery-candidate"


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class InvalidObjectKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublishedObject:
    object_key: str
    size_bytes: int
    sha256: str


@dataclass(slots=True)
class StorageSweepStats:
    quarantines_restored: int = 0
    quarantines_protected: int = 0
    quarantine_conflicts: int = 0
    invalid_quarantines: int = 0
    empty_quarantines_removed: int = 0
    quarantines_examined: int = 0
    bytes_hashed: int = 0
    budget_exhausted: bool = False
    directories_scanned: int = 0
    entries_scanned: int = 0
    mutations: int = 0


@dataclass(frozen=True, slots=True)
class StorageSweepBudget:
    max_quarantines: int = 32
    max_bytes: int = 16 * 1024 * 1024
    max_seconds: float = 1.0
    max_directories: int = 128
    max_entries: int = 1024
    max_mutations: int = 64

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.max_quarantines,
                self.max_bytes,
                self.max_seconds,
                self.max_directories,
                self.max_entries,
                self.max_mutations,
            )
        ):
            raise ValueError("storage sweep budgets must be positive")


@dataclass(frozen=True, slots=True)
class ExternalWorkspace:
    path: Path
    inherited_fds: tuple[int, ...]


class WorkspaceCapability:
    def __init__(
        self, name: str, descriptor: int, identity: tuple[int, int]
    ) -> None:
        self.name = name
        self._descriptor: int | None = descriptor
        self._identity = identity

    def close(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def __del__(self) -> None:
        descriptor = getattr(self, "_descriptor", None)
        if descriptor is not None:
            _best_effort(os.close, descriptor)

    def mkdir(self, relative: str | Path) -> None:
        parts = _workspace_parts(relative)
        parent = self._open_parent(parts[:-1], create=True)
        try:
            os.mkdir(parts[-1], 0o700, dir_fd=parent)
            _fsync_descriptor(parent)
        finally:
            os.close(parent)

    def write_bytes(self, relative: str | Path, content: bytes) -> None:
        if not isinstance(content, bytes):
            raise TypeError("workspace content must be bytes")
        parts = _workspace_parts(relative)
        parent = self._open_parent(parts[:-1], create=True)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                parts[-1],
                os.O_WRONLY
                | os.O_CREAT
                | os.O_TRUNC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("workspace object must be a regular file")
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_descriptor(parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    def open_regular(self, relative: str | Path) -> int:
        parts = _workspace_parts(relative)
        parent = self._open_parent(parts[:-1], create=False)
        try:
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise OSError("workspace object must be a regular file")
            return descriptor
        finally:
            os.close(parent)

    def open_external_regular(self, path: Path) -> int:
        supplied = Path(path)
        if supplied.is_absolute():
            descriptor = self._require_descriptor()
            parts = supplied.parts
            prefix = (supplied.anchor, "proc", "self", "fd", str(descriptor))
            if (
                not sys.platform.startswith("linux")
                or len(parts) <= len(prefix)
                or parts[: len(prefix)] != prefix
            ):
                raise OSError("workspace path is not bound to the owned descriptor")
            relative = Path(*parts[len(prefix) :])
        else:
            relative = supplied
        try:
            return self.open_regular(relative)
        except (InvalidObjectKey, OSError, RuntimeError) as error:
            raise OSError("workspace descendant cannot be opened safely") from error

    def duplicate_directory(self) -> int:
        return os.dup(self._require_descriptor())

    def external(self, relative: str | Path = Path(".")) -> ExternalWorkspace:
        descriptor = self._require_descriptor()
        require_external_workspace_support()
        base = Path(f"/proc/self/fd/{descriptor}")
        try:
            status = base.stat()
        except OSError as error:
            raise RuntimeError(
                "external workspace stages require Linux /proc/self/fd support"
            ) from error
        if _identity(status) != self._identity:
            raise RuntimeError("workspace descriptor path identity changed")
        parts = _workspace_parts(relative, allow_current=True)
        path = base.joinpath(*parts) if parts else base
        return ExternalWorkspace(path, (descriptor,))

    def _open_parent(self, parts: tuple[str, ...], *, create: bool) -> int:
        descriptor = os.dup(self._require_descriptor())
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _require_descriptor(self) -> int:
        if self._descriptor is None:
            raise RuntimeError("workspace capability is closed")
        status = os.fstat(self._descriptor)
        if not stat.S_ISDIR(status.st_mode) or _identity(status) != self._identity:
            raise RuntimeError("workspace capability identity changed")
        return self._descriptor


@dataclass(slots=True)
class _ParentBinding:
    path: Path
    descriptor: int | None

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


@dataclass(slots=True)
class _QuarantineBinding:
    name: str
    descriptor: int


@dataclass(frozen=True, slots=True)
class _QuarantineMetadata:
    quarantine_id: str
    object_key: str
    original_name: str
    expected_sha256: str
    owner_execution_id: str | None
    created_at: datetime


class RootedLocalStorage:
    def __init__(
        self,
        root: Path,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        _require_supported_host()
        _require_atomic_no_replace()
        self._root_descriptor: int | None
        self.root, descriptor = _open_stable_root(Path(root), "data root")
        self._root_descriptor = descriptor
        self._root_identity = _identity(os.fstat(self._root_descriptor))
        self._uuid_factory = uuid_factory
        self._clock = clock
        self._monotonic = monotonic
        self._thread_lock = threading.RLock()
        self._quarantine_cache: dict[str, tuple[tuple[int, int, int, int], str]] = {}
        self._quarantine_cursor: str | None = None
        self._quarantine_cursor_directory: str | None = None
        self._directory_cursor: str | None = None
        self._directory_entry_cursor: str | None = None
        self._directory_entry_cursor_directory: str | None = None
        self._file_cursor: str | None = None
        self._file_cursor_directory: str | None = None
        try:
            with _exclusive_descriptor_gate(
                self._thread_lock, descriptor, timeout_seconds=1.0
            ) as acquired:
                if not acquired:
                    raise RuntimeError("data root mutation lock is unavailable")
                _probe_root_capabilities(descriptor)
                self._load_sweep_state(descriptor)
        except BaseException as error:
            self.close()
            raise RuntimeError("data root capability probe failed") from error

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

    def _root_descriptor_or_raise(self) -> int:
        if self._root_descriptor is None:
            raise RuntimeError("data root is closed")
        return self._root_descriptor

    def _load_sweep_state(self, descriptor: int) -> None:
        payload = _get_descriptor_xattr(
            descriptor, _SWEEP_XATTR, max_bytes=_MAX_SWEEP_STATE_BYTES
        )
        if payload is None:
            return
        try:
            parsed = json.loads(payload)
            if (
                not isinstance(parsed, dict)
                or parsed.get("version") != 1
                or not isinstance(parsed.get("cache"), dict)
            ):
                raise ValueError
            cursor = parsed.get("cursor")
            quarantine_cursor_directory = parsed.get("cursorDirectory")
            directory_cursor = parsed.get("directoryCursor")
            directory_entry_cursor = parsed.get("directoryEntryCursor")
            directory_entry_cursor_directory = parsed.get(
                "directoryEntryCursorDirectory"
            )
            file_cursor = parsed.get("fileCursor")
            file_cursor_directory = parsed.get("fileCursorDirectory")
            if cursor is not None and not isinstance(cursor, str):
                raise ValueError
            if quarantine_cursor_directory is not None and not isinstance(
                quarantine_cursor_directory, str
            ):
                raise ValueError
            if directory_cursor is not None and not isinstance(
                directory_cursor, str
            ):
                raise ValueError
            if directory_entry_cursor is not None and not isinstance(
                directory_entry_cursor, str
            ):
                raise ValueError
            if directory_entry_cursor_directory is not None and not isinstance(
                directory_entry_cursor_directory, str
            ):
                raise ValueError
            if file_cursor is not None and not isinstance(file_cursor, str):
                raise ValueError
            if file_cursor_directory is not None and not isinstance(
                file_cursor_directory, str
            ):
                raise ValueError
            cache: dict[str, tuple[tuple[int, int, int, int], str]] = {}
            for key, value in list(parsed["cache"].items())[:128]:
                if (
                    not isinstance(key, str)
                    or re.fullmatch(r"[0-9a-f]{64}", key) is None
                    or not isinstance(value, list)
                    or len(value) != 5
                    or not all(isinstance(item, int) for item in value[:4])
                    or value[4] not in {"conflict", "invalid"}
                ):
                    raise ValueError
                cache[key] = (
                    (value[0], value[1], value[2], value[3]),
                    value[4],
                )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError("persistent sweep state is invalid") from error
        self._quarantine_cursor = cursor
        self._quarantine_cursor_directory = quarantine_cursor_directory
        self._directory_cursor = directory_cursor
        self._directory_entry_cursor = directory_entry_cursor
        self._directory_entry_cursor_directory = directory_entry_cursor_directory
        self._file_cursor = file_cursor
        self._file_cursor_directory = file_cursor_directory
        self._quarantine_cache = cache

    def _persist_sweep_state(self, descriptor: int) -> None:
        payload = json.dumps(
            {
                "version": 1,
                "cursor": self._quarantine_cursor,
                "cursorDirectory": self._quarantine_cursor_directory,
                "directoryCursor": self._directory_cursor,
                "directoryEntryCursor": self._directory_entry_cursor,
                "directoryEntryCursorDirectory": (
                    self._directory_entry_cursor_directory
                ),
                "fileCursor": self._file_cursor,
                "fileCursorDirectory": self._file_cursor_directory,
                "cache": {
                    key: [*fingerprint, outcome]
                    for key, (fingerprint, outcome) in self._quarantine_cache.items()
                },
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _MAX_SWEEP_STATE_BYTES:
            raise RuntimeError("persistent sweep state exceeds its byte limit")
        _put_descriptor_xattr(descriptor, _SWEEP_XATTR, payload)
        os.fsync(descriptor)

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

    def publish_descriptor(
        self,
        object_key: str,
        source_descriptor: int,
        *,
        owner_execution_id: str | None = None,
    ) -> PublishedObject:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("artifact source must be a regular file")

        def copy(handle: BinaryIO) -> tuple[int, str]:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(os.dup(source_descriptor), "rb") as input_handle:
                while chunk := input_handle.read(_CHUNK_SIZE):
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            _require_same_regular(before, os.fstat(source_descriptor))
            return size, digest.hexdigest()

        return self._publish(
            object_key, copy, owner_execution_id=owner_execution_id
        )

    def delete_if_owned(self, object_key: str, expected_sha256: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return False
        if self._root_descriptor is None:
            raise RuntimeError("data root is closed")
        with _exclusive_descriptor_gate(
            self._thread_lock, self._root_descriptor, timeout_seconds=5.0
        ) as acquired:
            if not acquired:
                raise TimeoutError("data root mutation lock timed out")
            return self._delete_if_owned_locked(object_key, expected_sha256)

    def _delete_if_owned_locked(
        self, object_key: str, expected_sha256: str
    ) -> bool:
        parts = _object_key_parts(object_key)
        deleted = False
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
                artifact = _GENERATED_ARTIFACT.fullmatch(object_key)
                owner_execution_id = (
                    artifact.group("owner") if artifact is not None else None
                )
                quarantine_id = str(self._uuid_factory())
                quarantine = _quarantine_named(
                    parent,
                    parts[-1],
                    _QuarantineMetadata(
                        quarantine_id=quarantine_id,
                        object_key=object_key,
                        original_name=parts[-1],
                        expected_sha256=expected_sha256,
                        owner_execution_id=owner_execution_id,
                        created_at=self._clock(),
                    ),
                )
                if quarantine is None:
                    return False
                restored = False
                try:
                    _isolate_quarantine_candidate(
                        quarantine.descriptor, parts[-1]
                    )
                    isolated = os.stat(
                        _RECOVERY_CANDIDATE,
                        dir_fd=quarantine.descriptor,
                        follow_symlinks=False,
                    )
                    isolated_descriptor = os.open(
                        _RECOVERY_CANDIDATE,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=quarantine.descriptor,
                    )
                    try:
                        isolated_digest = _hash_descriptor(isolated_descriptor)
                        isolated_after = os.fstat(isolated_descriptor)
                    finally:
                        os.close(isolated_descriptor)
                    if (
                        stat.S_ISREG(isolated.st_mode)
                        and _identity(isolated) == identity
                        and _identity(isolated_after) == identity
                        and isolated_digest == expected_sha256
                    ):
                        os.unlink(
                            _RECOVERY_CANDIDATE,
                            dir_fd=quarantine.descriptor,
                        )
                        _fsync_descriptor(quarantine.descriptor)
                        deleted = True
                    else:
                        return False
                finally:
                    os.close(quarantine.descriptor)
                if deleted or restored:
                    _remove_empty_quarantine_directory(parent, quarantine.name)
            finally:
                os.close(descriptor)
        finally:
            parent.close()
        artifact = _GENERATED_ARTIFACT.fullmatch(object_key)
        if deleted and artifact is not None and artifact.group("owner") is not None:
            root_descriptor = self._duplicate_root()
            try:
                _prune_artifact_directories(
                    root_descriptor,
                    {(artifact.group("task"), artifact.group("owner"))},
                )
            finally:
                os.close(root_descriptor)
        return deleted

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
        budget: StorageSweepBudget | None = None,
    ) -> StorageSweepStats:
        if self._root_descriptor is None:
            raise RuntimeError("data root is closed")
        limits = budget or StorageSweepBudget()
        with _exclusive_descriptor_gate(
            self._thread_lock,
            self._root_descriptor,
            timeout_seconds=limits.max_seconds,
        ) as acquired:
            if not acquired:
                return StorageSweepStats(budget_exhausted=True)
            self._load_sweep_state(self._root_descriptor)
            return self._sweep_abandoned_locked(
                cutoff,
                referenced_artifact_keys,
                active_execution_ids=active_execution_ids,
                active_build_executions=active_build_executions,
                budget=limits,
            )

    def _sweep_abandoned_locked(
        self,
        cutoff: datetime,
        referenced_artifact_keys: frozenset[str],
        *,
        active_execution_ids: frozenset[str],
        active_build_executions: frozenset[tuple[str, str]],
        budget: StorageSweepBudget,
    ) -> StorageSweepStats:
        cutoff_timestamp = cutoff.timestamp()
        root_descriptor = self._duplicate_root()
        prune_candidates: set[tuple[str, str | None]] = set()
        stats = StorageSweepStats()
        deadline = self._monotonic() + budget.max_seconds
        scan_completed = True
        try:
            for directory, directories, filenames, descriptor in os.fwalk(
                ".", topdown=True, follow_symlinks=False, dir_fd=root_descriptor
            ):
                relative_directory_key = Path(directory).as_posix().removeprefix("./")
                relative_directory_key = relative_directory_key or "."
                if (
                    self._directory_cursor is not None
                    and relative_directory_key <= self._directory_cursor
                ):
                    continue
                if (
                    self._monotonic() >= deadline
                    or stats.directories_scanned >= budget.max_directories
                ):
                    stats.budget_exhausted = True
                    scan_completed = False
                    break
                stats.directories_scanned += 1
                directories.sort()
                safe_directories: list[str] = []
                for name in directories:
                    relative_entry = (
                        Path(directory) / name
                    ).as_posix().removeprefix("./")
                    quarantine_match = _QUARANTINE.fullmatch(name)
                    entry_cursor = (
                        self._quarantine_cursor
                        if quarantine_match is not None
                        else self._directory_entry_cursor
                    )
                    cursor_directory = (
                        self._quarantine_cursor_directory
                        if quarantine_match is not None
                        else self._directory_entry_cursor_directory
                    )
                    if (
                        cursor_directory == relative_directory_key
                        and
                        entry_cursor is not None
                        and relative_entry <= entry_cursor
                    ):
                        if quarantine_match is None:
                            safe_directories.append(name)
                        continue
                    if stats.entries_scanned >= budget.max_entries:
                        stats.budget_exhausted = True
                        scan_completed = False
                        break
                    stats.entries_scanned += 1
                    status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if quarantine_match is not None:
                        relative_quarantine = relative_entry
                        if (
                            self._quarantine_cursor is not None
                            and relative_quarantine <= self._quarantine_cursor
                        ):
                            continue
                        if (
                            stats.quarantines_examined >= budget.max_quarantines
                            or stats.mutations >= budget.max_mutations
                            or self._monotonic() >= deadline
                        ):
                            stats.budget_exhausted = True
                            scan_completed = False
                            break
                        payload_size = _quarantine_payload_size(descriptor, name)
                        fingerprint = _quarantine_payload_fingerprint(
                            descriptor, name
                        )
                        cache_key = hashlib.sha256(
                            relative_quarantine.encode("utf-8")
                        ).hexdigest()
                        cached = self._quarantine_cache.get(cache_key)
                        if cached is not None and cached[0] == fingerprint:
                            stats.quarantines_examined += 1
                            if cached[1] == "conflict":
                                stats.quarantine_conflicts += 1
                            else:
                                stats.invalid_quarantines += 1
                            self._quarantine_cursor = relative_quarantine
                            self._quarantine_cursor_directory = relative_directory_key
                            self._persist_sweep_state(root_descriptor)
                            continue
                        if stats.bytes_hashed + payload_size > budget.max_bytes:
                            stats.budget_exhausted = True
                            scan_completed = False
                            break
                        stats.quarantines_examined += 1
                        conflicts_before = stats.quarantine_conflicts
                        invalid_before = stats.invalid_quarantines
                        restored_before = stats.quarantines_restored
                        empty_before = stats.empty_quarantines_removed
                        _recover_quarantine(
                            descriptor,
                            Path(directory),
                            name,
                            quarantine_match.group("id"),
                            cutoff,
                            referenced_artifact_keys,
                            active_execution_ids,
                            active_build_executions,
                            stats,
                            prune_candidates,
                        )
                        stats.mutations += (
                            stats.quarantines_restored
                            - restored_before
                            + stats.empty_quarantines_removed
                            - empty_before
                        )
                        outcome = None
                        if stats.quarantine_conflicts > conflicts_before:
                            outcome = "conflict"
                        elif stats.invalid_quarantines > invalid_before:
                            outcome = "invalid"
                        if outcome is not None:
                            self._quarantine_cache[cache_key] = (
                                _quarantine_payload_fingerprint(descriptor, name),
                                outcome,
                            )
                            while len(self._quarantine_cache) > 128:
                                self._quarantine_cache.pop(next(iter(self._quarantine_cache)))
                        self._quarantine_cursor = relative_quarantine
                        self._quarantine_cursor_directory = relative_directory_key
                        self._persist_sweep_state(root_descriptor)
                        continue
                    if stat.S_ISDIR(status.st_mode) and not stat.S_ISLNK(status.st_mode):
                        safe_directories.append(name)
                    self._directory_entry_cursor = relative_entry
                    self._directory_entry_cursor_directory = relative_directory_key
                    self._persist_sweep_state(root_descriptor)
                directories[:] = safe_directories
                if not scan_completed:
                    break
                relative_directory = Path(directory)
                filenames.sort()
                for name in filenames:
                    relative = (relative_directory / name).as_posix()
                    relative = relative.removeprefix("./")
                    if (
                        self._file_cursor_directory == relative_directory_key
                        and
                        self._file_cursor is not None
                        and relative <= self._file_cursor
                    ):
                        continue
                    if (
                        stats.entries_scanned >= budget.max_entries
                        or stats.mutations >= budget.max_mutations
                        or self._monotonic() >= deadline
                    ):
                        stats.budget_exhausted = True
                        scan_completed = False
                        break
                    stats.entries_scanned += 1
                    stage_match = _GENERATED_STAGE.fullmatch(name)
                    artifact_match = _GENERATED_ARTIFACT.fullmatch(relative)
                    protected_stage = (
                        stage_match is not None
                        and stage_match.group("owner") in active_execution_ids
                    )
                    protected_artifact = (
                        artifact_match is not None
                        and (
                            relative in referenced_artifact_keys
                            or (
                                artifact_match.group("owner") is not None
                                and (
                                    artifact_match.group("task"),
                                    artifact_match.group("owner"),
                                )
                                in active_build_executions
                            )
                        )
                    )
                    if (
                        (stage_match is not None or artifact_match is not None)
                        and not protected_stage
                        and not protected_artifact
                    ):
                        status = os.stat(
                            name, dir_fd=descriptor, follow_symlinks=False
                        )
                        if (
                            stat.S_ISREG(status.st_mode)
                            and status.st_mtime <= cutoff_timestamp
                        ):
                            os.unlink(name, dir_fd=descriptor)
                            _fsync_descriptor(descriptor)
                            stats.mutations += 1
                            if artifact_match is not None:
                                prune_candidates.add(
                                    (
                                        artifact_match.group("task"),
                                        artifact_match.group("owner"),
                                    )
                                )
                    self._file_cursor = relative
                    self._file_cursor_directory = relative_directory_key
                    self._persist_sweep_state(root_descriptor)
                if not scan_completed:
                    break
                self._directory_cursor = relative_directory_key
                self._persist_sweep_state(root_descriptor)
            _prune_artifact_directories(root_descriptor, prune_candidates)
        finally:
            os.close(root_descriptor)
        if scan_completed:
            self._quarantine_cursor = None
            self._quarantine_cursor_directory = None
            self._directory_cursor = None
            self._directory_entry_cursor = None
            self._directory_entry_cursor_directory = None
            self._file_cursor = None
            self._file_cursor_directory = None
            self._persist_sweep_state(self._root_descriptor_or_raise())
        return stats


@dataclass(slots=True)
class OwnedWorkspace:
    path: Path
    _root_descriptor: int | None
    _identity: tuple[int, int]
    _thread_lock: Any
    capability: WorkspaceCapability
    _cleaned: bool = False

    def cleanup(self) -> None:
        if self._cleaned:
            return
        if self._root_descriptor is None:
            raise RuntimeError("workspace root is closed")
        with _exclusive_descriptor_gate(
            self._thread_lock, self._root_descriptor, timeout_seconds=5.0
        ) as acquired:
            if not acquired:
                raise TimeoutError("workspace mutation lock timed out")
            root_descriptor = os.dup(self._root_descriptor)
            workspace_descriptor = self.capability.duplicate_directory()
            try:
                _clear_directory_descriptor(workspace_descriptor)
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
                try:
                    os.rmdir(self.path.name, dir_fd=root_descriptor)
                except OSError as error:
                    raise RuntimeError(
                        "workspace identity changed during cleanup"
                    ) from error
                _fsync_descriptor(root_descriptor)
                self._cleaned = True
            finally:
                os.close(workspace_descriptor)
                os.close(root_descriptor)
        self.close()

    def close(self) -> None:
        self.capability.close()
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
        self._thread_lock = threading.RLock()

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
        if self._root_descriptor is None:
            raise RuntimeError("workspace root is closed")
        with _exclusive_descriptor_gate(
            self._thread_lock, self._root_descriptor, timeout_seconds=5.0
        ) as acquired:
            if not acquired:
                raise TimeoutError("workspace mutation lock timed out")
            return self._allocate_locked(execution_id)

    def _allocate_locked(self, execution_id: str | None = None) -> OwnedWorkspace:
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
                child_descriptor: int | None = None
                owned_root_descriptor: int | None = None
                capability: WorkspaceCapability | None = None
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
                    child_descriptor = os.open(
                        name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=root_descriptor,
                    )
                    owned_root_descriptor = os.dup(root_descriptor)
                    capability = WorkspaceCapability(
                        name, child_descriptor, _identity(status)
                    )
                    child_descriptor = None
                    owned = OwnedWorkspace(
                        path,
                        owned_root_descriptor,
                        _identity(status),
                        self._thread_lock,
                        capability,
                    )
                    owned_root_descriptor = None
                    capability = None
                    return owned
                except BaseException:
                    if child_descriptor is not None:
                        _best_effort(os.close, child_descriptor)
                    if capability is not None:
                        _best_effort(capability.close)
                    if owned_root_descriptor is not None:
                        _best_effort(os.close, owned_root_descriptor)
                    _best_effort(_remove_workspace_name, root_descriptor, name, None)
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
        self,
        cutoff: datetime,
        active_execution_ids: frozenset[str],
        *,
        budget: StorageSweepBudget | None = None,
    ) -> None:
        if self._root_descriptor is None:
            raise RuntimeError("workspace root is closed")
        limits = budget or StorageSweepBudget()
        with _exclusive_descriptor_gate(
            self._thread_lock,
            self._root_descriptor,
            timeout_seconds=limits.max_seconds,
        ) as acquired:
            if not acquired:
                return
            self._sweep_abandoned_locked(cutoff, active_execution_ids, limits)

    def _sweep_abandoned_locked(
        self,
        cutoff: datetime,
        active_execution_ids: frozenset[str],
        budget: StorageSweepBudget,
    ) -> None:
        cutoff_timestamp = cutoff.timestamp()
        root_descriptor = self._duplicate_root()
        deadline = time.monotonic() + budget.max_seconds
        entries_seen = 0
        mutations = 0
        try:
            with os.scandir(root_descriptor) as entries:
                for entry in entries:
                    if (
                        time.monotonic() >= deadline
                        or entries_seen >= budget.max_entries
                        or mutations >= budget.max_mutations
                    ):
                        break
                    entries_seen += 1
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
                    _remove_workspace_name(
                        root_descriptor, entry.name, _identity(status)
                    )
                    mutations += 1
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


def _workspace_parts(
    relative: str | Path, *, allow_current: bool = False
) -> tuple[str, ...]:
    value = relative.as_posix() if isinstance(relative, Path) else relative
    if allow_current and value == ".":
        return ()
    return _object_key_parts(value)


def _prune_artifact_directories(
    root_descriptor: int, candidates: set[tuple[str, str | None]]
) -> None:
    if not candidates:
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        artifacts_descriptor = os.open("artifacts", flags, dir_fd=root_descriptor)
    except FileNotFoundError:
        return
    try:
        for task, execution in sorted(candidates):
            try:
                task_descriptor = os.open(
                    task, flags, dir_fd=artifacts_descriptor
                )
            except OSError:
                continue
            try:
                if execution is not None:
                    _rmdir_if_empty(task_descriptor, execution)
            finally:
                os.close(task_descriptor)
            _rmdir_if_empty(artifacts_descriptor, task)
    finally:
        os.close(artifacts_descriptor)


def _rmdir_if_empty(parent_descriptor: int, name: str) -> None:
    try:
        os.rmdir(name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return
        raise
    _fsync_descriptor(parent_descriptor)


def _require_supported_host() -> None:
    if os.name == "nt":
        raise RuntimeError(
            "Windows Worker hosts are not supported; Windows remains a target platform"
        )
    required = (
        os.open,
        os.stat,
        os.mkdir,
        os.unlink,
        os.rmdir,
        os.link,
        os.rename,
    )
    if any(operation not in os.supports_dir_fd for operation in required):
        raise RuntimeError("safe descriptor-relative filesystem operations are unavailable")


@contextmanager
def _exclusive_descriptor_gate(
    thread_lock: threading.RLock,
    descriptor: int,
    *,
    timeout_seconds: float,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    acquired_thread = thread_lock.acquire(timeout=timeout_seconds)
    if not acquired_thread:
        yield False
        return
    acquired_file = False
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired_file = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield False
                    return
                time.sleep(min(0.01, remaining))
        yield True
    finally:
        if acquired_file:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        thread_lock.release()


def require_external_workspace_support() -> None:
    if not sys.platform.startswith("linux") or not Path("/proc/self/fd").is_dir():
        raise RuntimeError(
            "external workspace stages require Linux /proc/self/fd support"
        )


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


def _quarantine_named(
    parent: _ParentBinding,
    object_name: str,
    metadata: _QuarantineMetadata,
) -> _QuarantineBinding | None:
    if parent.descriptor is None:
        raise RuntimeError("safe parent directory handle is unavailable")
    quarantine_name = f".wf-quarantine-v1-{metadata.quarantine_id}"
    os.mkdir(quarantine_name, 0o700, dir_fd=parent.descriptor)
    quarantine_descriptor = -1
    moved = False
    try:
        quarantine_descriptor = os.open(
            quarantine_name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent.descriptor,
        )
        _write_quarantine_metadata(quarantine_descriptor, metadata)
        _fsync_descriptor(quarantine_descriptor)
        _fsync_parent(parent)
        try:
            _rename_no_replace(
                parent.descriptor,
                object_name,
                quarantine_descriptor,
                object_name,
            )
        except FileNotFoundError:
            os.close(quarantine_descriptor)
            quarantine_descriptor = -1
            _remove_empty_quarantine_directory(parent, quarantine_name)
            return None
        moved = True
        _fsync_parent(parent)
        _fsync_descriptor(quarantine_descriptor)
        return _QuarantineBinding(quarantine_name, quarantine_descriptor)
    except BaseException:
        if quarantine_descriptor != -1:
            os.close(quarantine_descriptor)
        if not moved:
            _best_effort(
                _remove_empty_quarantine_directory, parent, quarantine_name
            )
        raise


def _restore_quarantined_no_replace(
    parent: _ParentBinding,
    quarantine: _QuarantineBinding,
    object_name: str,
) -> None:
    if parent.descriptor is None:
        raise RuntimeError("safe parent directory handle is unavailable")
    metadata = _read_quarantine_metadata(
        quarantine.descriptor,
        quarantine.name.removeprefix(".wf-quarantine-v1-"),
    )
    if metadata is None:
        raise OSError("quarantine metadata is unavailable")
    _isolate_quarantine_candidate(quarantine.descriptor, object_name)
    descriptor = os.open(
        _RECOVERY_CANDIDATE,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=quarantine.descriptor,
    )
    try:
        before = os.fstat(descriptor)
        digest = _hash_descriptor(descriptor)
        after = os.fstat(descriptor)
        _require_same_regular(before, after)
        named = os.stat(
            _RECOVERY_CANDIDATE,
            dir_fd=quarantine.descriptor,
            follow_symlinks=False,
        )
        if _identity(named) != _identity(after) or digest != metadata.expected_sha256:
            raise OSError("quarantine candidate failed ownership validation")
    finally:
        os.close(descriptor)
    try:
        _rename_no_replace(
            quarantine.descriptor,
            _RECOVERY_CANDIDATE,
            parent.descriptor,
            object_name,
        )
    except FileExistsError as error:
        raise OSError(
            "compensation ownership changed and quarantine could not be restored"
        ) from error
    _fsync_descriptor(quarantine.descriptor)
    _fsync_parent(parent)


def _isolate_quarantine_candidate(descriptor: int, object_name: str) -> None:
    try:
        os.stat(_RECOVERY_CANDIDATE, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        _rename_no_replace(
            descriptor,
            object_name,
            descriptor,
            _RECOVERY_CANDIDATE,
        )


def _remove_empty_quarantine_directory(parent: _ParentBinding, name: str) -> None:
    if parent.descriptor is None:
        raise RuntimeError("safe parent directory handle is unavailable")
    os.rmdir(name, dir_fd=parent.descriptor)
    _fsync_parent(parent)


def _require_atomic_no_replace() -> None:
    symbol, _flag = _atomic_rename_configuration()
    if getattr(ctypes.CDLL(None), symbol, None) is None:
        raise RuntimeError("atomic descriptor-relative no-replace rename is unavailable")


def _probe_root_capabilities(root_descriptor: int) -> None:
    name = f".wf-capability-probe-{uuid4()}"
    directory = -1
    created = False
    failure: BaseException | None = None
    try:
        contender = os.open(
            ".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=root_descriptor
        )
        try:
            _require_lock_contention(contender)
        finally:
            os.close(contender)
        os.mkdir(name, 0o700, dir_fd=root_descriptor)
        created = True
        directory = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        for leaf, content in (
            ("source", b"source"),
            ("second", b"second"),
            ("occupied", b"occupied"),
        ):
            descriptor = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
            try:
                os.write(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        source_status = os.stat("source", dir_fd=directory, follow_symlinks=False)
        second_status = os.stat("second", dir_fd=directory, follow_symlinks=False)
        occupied_status = os.stat("occupied", dir_fd=directory, follow_symlinks=False)
        _rename_no_replace(directory, "source", directory, "moved")
        moved_status = os.stat("moved", dir_fd=directory, follow_symlinks=False)
        if _identity(moved_status) != _identity(source_status):
            raise RuntimeError("no-replace rename changed source identity")
        moved = os.open("moved", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            if os.read(moved, 16) != b"source":
                raise RuntimeError("no-replace rename changed source content")
        finally:
            os.close(moved)
        try:
            _rename_no_replace(directory, "second", directory, "occupied")
        except FileExistsError:
            pass
        else:
            raise RuntimeError("no-replace rename replaced an occupied destination")
        if _identity(os.stat("second", dir_fd=directory)) != _identity(second_status):
            raise RuntimeError("EEXIST changed the source")
        if _identity(os.stat("occupied", dir_fd=directory)) != _identity(
            occupied_status
        ):
            raise RuntimeError("EEXIST changed the destination")
        marker = b"wheelforge-capability-v1"
        _set_descriptor_xattr(directory, _QUARANTINE_XATTR, marker)
        try:
            _set_descriptor_xattr(directory, _QUARANTINE_XATTR, marker)
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
        else:
            raise RuntimeError("xattr create did not preserve EEXIST semantics")
        if _get_descriptor_xattr(directory, _QUARANTINE_XATTR) != marker:
            raise RuntimeError("descriptor xattr round trip failed")
        os.fsync(directory)
        os.fsync(root_descriptor)
    except BaseException as error:
        failure = error
    finally:
        cleanup_errors: list[BaseException] = []
        if directory != -1:
            for leaf in ("source", "second", "occupied", "moved"):
                try:
                    os.unlink(leaf, dir_fd=directory)
                except FileNotFoundError:
                    pass
                except BaseException as error:
                    cleanup_errors.append(error)
            try:
                os.close(directory)
            except BaseException as error:
                cleanup_errors.append(error)
        if created:
            try:
                os.rmdir(name, dir_fd=root_descriptor)
                os.fsync(root_descriptor)
            except BaseException as error:
                cleanup_errors.append(error)
        if failure is not None and cleanup_errors:
            raise BaseExceptionGroup("capability probe and cleanup failed", [failure, *cleanup_errors])
        if failure is not None:
            raise failure
        if cleanup_errors:
            raise BaseExceptionGroup("capability probe cleanup failed", cleanup_errors)


def _require_lock_contention(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    raise RuntimeError("data root lock contention semantics are unavailable")


def _quarantine_payload_size(parent_descriptor: int, quarantine_name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(quarantine_name, flags, dir_fd=parent_descriptor)
    except OSError:
        return 0
    try:
        for name in (_RECOVERY_CANDIDATE,):
            try:
                status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            return status.st_size if stat.S_ISREG(status.st_mode) else 0
        with os.scandir(descriptor) as entries:
            for entry in entries:
                status = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(status.st_mode):
                    return status.st_size
        return 0
    finally:
        os.close(descriptor)


def _quarantine_payload_fingerprint(
    parent_descriptor: int, quarantine_name: str
) -> tuple[int, int, int, int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(quarantine_name, flags, dir_fd=parent_descriptor)
    except OSError:
        return (0, 0, 0, 0)
    try:
        names = (_RECOVERY_CANDIDATE,)
        for name in names:
            try:
                status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            return (*_identity(status), status.st_size, status.st_mtime_ns)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                status = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(status.st_mode):
                    return (*_identity(status), status.st_size, status.st_mtime_ns)
        status = os.fstat(descriptor)
        return (*_identity(status), 0, status.st_mtime_ns)
    finally:
        os.close(descriptor)


def _atomic_rename_configuration() -> tuple[str, int]:
    if sys.platform == "darwin":
        return "renameatx_np", 0x00000004
    if sys.platform.startswith("linux"):
        return "renameat2", 1
    raise RuntimeError("atomic descriptor-relative no-replace rename is unavailable")


def _rename_no_replace(
    source_parent: int,
    source: str,
    destination_parent: int,
    destination: str,
) -> None:
    symbol, flag = _atomic_rename_configuration()
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, symbol, None)
    if function is None:
        raise RuntimeError("atomic descriptor-relative no-replace rename is unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if (
        function(
            source_parent,
            os.fsencode(source),
            destination_parent,
            os.fsencode(destination),
            flag,
        )
        != 0
    ):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), source)


def _write_quarantine_metadata(
    descriptor: int, metadata: _QuarantineMetadata
) -> None:
    payload = json.dumps(
        {
            "schemaVersion": 1,
            "quarantineId": metadata.quarantine_id,
            "objectKey": metadata.object_key,
            "originalName": metadata.original_name,
            "expectedSha256": metadata.expected_sha256,
            "ownerExecutionId": metadata.owner_execution_id,
            "createdAt": metadata.created_at.isoformat(timespec="microseconds"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _MAX_QUARANTINE_METADATA_BYTES:
        raise ValueError("quarantine metadata exceeds its byte limit")
    _set_descriptor_xattr(descriptor, _QUARANTINE_XATTR, payload)


def _read_quarantine_metadata(
    descriptor: int, expected_id: str
) -> _QuarantineMetadata | None:
    payload = _get_descriptor_xattr(descriptor, _QUARANTINE_XATTR)
    if payload is None:
        return None
    if len(payload) > _MAX_QUARANTINE_METADATA_BYTES:
        raise ValueError("quarantine metadata exceeds its byte limit")
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("quarantine metadata is malformed") from error
    expected_keys = {
        "schemaVersion",
        "quarantineId",
        "objectKey",
        "originalName",
        "expectedSha256",
        "ownerExecutionId",
        "createdAt",
    }
    if not isinstance(parsed, dict) or set(parsed) != expected_keys:
        raise ValueError("quarantine metadata shape is invalid")
    if parsed["schemaVersion"] != 1 or parsed["quarantineId"] != expected_id:
        raise ValueError("quarantine metadata identity is invalid")
    object_key = parsed["objectKey"]
    original_name = parsed["originalName"]
    expected_sha256 = parsed["expectedSha256"]
    owner = parsed["ownerExecutionId"]
    created = parsed["createdAt"]
    if not isinstance(object_key, str):
        raise ValueError("quarantine object key is invalid")
    parts = _object_key_parts(object_key)
    if not isinstance(original_name, str) or parts[-1] != original_name:
        raise ValueError("quarantine original name is invalid")
    if not isinstance(expected_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ) is None:
        raise ValueError("quarantine digest is invalid")
    if owner is not None:
        if not isinstance(owner, str):
            raise ValueError("quarantine execution owner is invalid")
        owner = str(UUID(owner))
    if not isinstance(created, str):
        raise ValueError("quarantine creation time is invalid")
    try:
        created_at = datetime.fromisoformat(created)
    except ValueError as error:
        raise ValueError("quarantine creation time is invalid") from error
    return _QuarantineMetadata(
        quarantine_id=expected_id,
        object_key=object_key,
        original_name=original_name,
        expected_sha256=expected_sha256,
        owner_execution_id=owner,
        created_at=created_at,
    )


def _set_descriptor_xattr(descriptor: int, name: bytes, value: bytes) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "fsetxattr", None)
    if function is None:
        raise RuntimeError("durable quarantine metadata is unavailable")
    buffer = ctypes.create_string_buffer(value)
    pointer = ctypes.cast(buffer, ctypes.c_void_p)
    if sys.platform == "darwin":
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        arguments: tuple[Any, ...] = (
            descriptor,
            name,
            pointer,
            len(value),
            0,
            0x0002,
        )
    else:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        ]
        arguments = (descriptor, name, pointer, len(value), 1)
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(*arguments) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def _get_descriptor_xattr(
    descriptor: int,
    name: bytes,
    *,
    max_bytes: int = _MAX_QUARANTINE_METADATA_BYTES,
) -> bytes | None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "fgetxattr", None)
    if function is None:
        raise RuntimeError("durable quarantine metadata is unavailable")
    if sys.platform == "darwin":
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        suffix: tuple[int, ...] = (0, 0)
    else:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        suffix = ()
    function.restype = ctypes.c_ssize_t
    ctypes.set_errno(0)
    size = function(descriptor, name, None, 0, *suffix)
    if size < 0:
        number = ctypes.get_errno()
        missing = {getattr(errno, "ENODATA", -1), getattr(errno, "ENOATTR", 93)}
        if number in missing:
            return None
        raise OSError(number, os.strerror(number))
    if size > max_bytes:
        raise ValueError("quarantine metadata exceeds its byte limit")
    buffer = ctypes.create_string_buffer(size)
    ctypes.set_errno(0)
    observed = function(descriptor, name, buffer, size, *suffix)
    if observed < 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return bytes(buffer.raw[:observed])


def _put_descriptor_xattr(descriptor: int, name: bytes, value: bytes) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "fsetxattr", None)
    if function is None:
        raise RuntimeError("durable storage metadata is unavailable")
    buffer = ctypes.create_string_buffer(value)
    pointer = ctypes.cast(buffer, ctypes.c_void_p)
    if sys.platform == "darwin":
        function.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_int,
        ]
        arguments: tuple[Any, ...] = (
            descriptor, name, pointer, len(value), 0, 0,
        )
    else:
        function.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_int,
        ]
        arguments = (descriptor, name, pointer, len(value), 0)
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(*arguments) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def _recover_quarantine(
    parent_descriptor: int,
    relative_directory: Path,
    quarantine_name: str,
    quarantine_id: str,
    cutoff: datetime,
    referenced_artifact_keys: frozenset[str],
    active_execution_ids: frozenset[str],
    active_build_executions: frozenset[tuple[str, str]],
    stats: StorageSweepStats,
    prune_candidates: set[tuple[str, str | None]],
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(quarantine_name, flags, dir_fd=parent_descriptor)
    except OSError:
        stats.invalid_quarantines += 1
        return None
    remove_empty = False
    metadata: _QuarantineMetadata | None = None
    restored = False
    try:
        try:
            metadata = _read_quarantine_metadata(descriptor, quarantine_id)
        except (OSError, RuntimeError, ValueError):
            stats.invalid_quarantines += 1
            metadata = None
        status = os.fstat(descriptor)
        if metadata is None:
            if status.st_mtime <= cutoff.timestamp() and _descriptor_directory_empty(
                descriptor
            ):
                remove_empty = True
            return None
        expected_parent = Path(*_object_key_parts(metadata.object_key)[:-1])
        observed_parent = Path(
            relative_directory.as_posix().removeprefix("./") or "."
        )
        if expected_parent != observed_parent:
            stats.invalid_quarantines += 1
            return None
        if metadata.created_at.timestamp() > cutoff.timestamp():
            return None
        artifact = _GENERATED_ARTIFACT.fullmatch(metadata.object_key)
        active_build = (
            artifact is not None
            and artifact.group("owner") is not None
            and (artifact.group("task"), artifact.group("owner"))
            in active_build_executions
        )
        if (
            metadata.object_key in referenced_artifact_keys
            or metadata.owner_execution_id in active_execution_ids
            or active_build
        ):
            stats.quarantines_protected += 1
            return None
        try:
            _isolate_quarantine_candidate(descriptor, metadata.original_name)
            payload_descriptor = os.open(
                _RECOVERY_CANDIDATE,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except FileNotFoundError:
            if _descriptor_directory_empty(descriptor):
                remove_empty = True
            else:
                stats.invalid_quarantines += 1
            return None
        try:
            before = os.fstat(payload_descriptor)
            stats.bytes_hashed += before.st_size
            digest = _hash_descriptor(payload_descriptor)
            after = os.fstat(payload_descriptor)
            _require_same_regular(before, after)
            named = os.stat(
                _RECOVERY_CANDIDATE,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(named.st_mode)
                or _identity(named) != _identity(after)
                or digest != metadata.expected_sha256
            ):
                stats.quarantine_conflicts += 1
                return None
        finally:
            os.close(payload_descriptor)
        try:
            _rename_no_replace(
                descriptor,
                _RECOVERY_CANDIDATE,
                parent_descriptor,
                metadata.original_name,
            )
        except FileExistsError:
            stats.quarantine_conflicts += 1
            return None
        _fsync_descriptor(descriptor)
        _fsync_descriptor(parent_descriptor)
        restored = True
        remove_empty = True
        stats.quarantines_restored += 1
        return None
    finally:
        os.close(descriptor)
        if remove_empty:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_descriptor)
            except OSError as error:
                if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    stats.quarantine_conflicts += 1
                elif error.errno != errno.ENOENT:
                    stats.invalid_quarantines += 1
            else:
                _fsync_descriptor(parent_descriptor)
                if not restored:
                    stats.empty_quarantines_removed += 1
                    candidate = _quarantine_prune_candidate(
                        relative_directory, metadata
                    )
                    if candidate is not None:
                        prune_candidates.add(candidate)


def _quarantine_prune_candidate(
    relative_directory: Path, metadata: _QuarantineMetadata | None
) -> tuple[str, str] | None:
    if metadata is not None:
        artifact = _GENERATED_ARTIFACT.fullmatch(metadata.object_key)
        if artifact is not None and artifact.group("owner") is not None:
            return artifact.group("task"), artifact.group("owner")
    parts = tuple(
        part
        for part in relative_directory.as_posix().removeprefix("./").split("/")
        if part
    )
    if len(parts) != 3 or parts[0] != "artifacts":
        return None
    if re.fullmatch(_UUID_PATTERN, parts[1]) is None or re.fullmatch(
        _UUID_PATTERN, parts[2]
    ) is None:
        return None
    return parts[1], parts[2]


def _descriptor_directory_empty(descriptor: int) -> bool:
    with os.scandir(descriptor) as entries:
        return next(entries, None) is None


def _clear_directory_descriptor(descriptor: int) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(status.st_mode) and not stat.S_ISLNK(status.st_mode):
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                child_identity = _identity(os.fstat(child))
                _clear_directory_descriptor(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _identity(current) != child_identity:
                    raise RuntimeError("workspace descendant identity changed")
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=descriptor)
    _fsync_descriptor(descriptor)


def _remove_workspace_name(
    root_descriptor: int,
    name: str,
    expected_identity: tuple[int, int] | None,
) -> None:
    if _GENERATED_WORKSPACE.fullmatch(name) is None:
        raise RuntimeError("workspace name is not generated")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_descriptor,
    )
    try:
        identity = _identity(os.fstat(descriptor))
        if expected_identity is not None and identity != expected_identity:
            raise RuntimeError("workspace identity changed")
        _clear_directory_descriptor(descriptor)
        current = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        if _identity(current) != identity:
            raise RuntimeError("workspace identity changed")
        os.rmdir(name, dir_fd=root_descriptor)
        _fsync_descriptor(root_descriptor)
    finally:
        os.close(descriptor)


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
