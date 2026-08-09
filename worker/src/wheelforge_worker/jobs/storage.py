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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from . import segmented_queue as _segmented_queue


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_CHUNK_SIZE = 64 * 1024
_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_GENERATED_STAGE = re.compile(
    rf"\.wf-stage-(?:(?P<owner>{_UUID_PATTERN})-)?{_UUID_PATTERN}\Z"
)
_GENERATED_WORKSPACE = re.compile(rf"wf-execution-({_UUID_PATTERN})\Z")
_RETIRED_WORKSPACE = re.compile(rf"\.wf-retired-v1-({_UUID_PATTERN})\Z")
_GENERATED_ARTIFACT = re.compile(
    rf"artifacts/(?P<task>{_UUID_PATTERN})/"
    rf"(?:(?P<owner>{_UUID_PATTERN})/)?{_UUID_PATTERN}\.zip\Z"
)
_QUARANTINE = re.compile(rf"\.wf-quarantine-v1-(?P<id>{_UUID_PATTERN})\Z")
_QUARANTINE_XATTR = b"user.wheelforge.quarantine-v1"
_LEGACY_QUARANTINE_STATE_XATTR = b"user.wheelforge.legacy-quarantine-v1"
_LEGACY_MIGRATION_STATE_XATTR = b"user.wheelforge.legacy-migration-v1"
_LEGACY_MIGRATION_COMPLETE = b"complete"
_LEGACY_MIGRATION_HELD_LIMIT = b"held-limit"
_WORKSPACE_SWEEP_XATTR = b"user.wheelforge.workspace-sweep-v1"
_QUEUE_ROOT = ".wf-maintenance-v2"
_WORKSPACE_QUEUE_ROOT = ".wf-workspace-maint-v2"
_QUEUE_STATE_XATTR = b"user.wheelforge.queue-v2"
_QUEUE_RECORD_MAX_BYTES = 8192
_QUEUE_SEGMENT_SIZE = 128
_QUEUE_MAX_SEGMENTS = 32
_QUEUE_MAX_RECORDS = _QUEUE_SEGMENT_SIZE * _QUEUE_MAX_SEGMENTS
_QUEUE_MAX_RECORD_BYTES = 16 * 1024 * 1024
_QUEUE_RECONCILE_MAX_MUTATIONS = _QUEUE_MAX_RECORDS * 4
_LEGACY_MIGRATION_MAX_DIRECTORIES = 4096
_LEGACY_MIGRATION_MAX_ENTRIES = 32768
_MAX_QUARANTINE_METADATA_BYTES = 8192
_MAX_SWEEP_STATE_BYTES = 32768
_RECOVERY_CANDIDATE = ".wf-recovery-candidate"
QueueCapacityError = _segmented_queue.QueueCapacityError


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _datetime_timestamp(value: datetime) -> float:
    try:
        return value.timestamp()
    except (OverflowError, ValueError):
        return float("inf") if value.year >= 9999 else float("-inf")


class InvalidObjectKey(ValueError):
    pass


class _LegacyMigrationLimitExceeded(RuntimeError):
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
    lock_contended: bool = False
    progress_made: bool = False
    backlog_entries: int = 0
    oversized_quarantines: int = 0
    quarantines_held: int = 0
    queue_record_bytes: int = 0
    queue_segments: int = 0
    queue_capacity_events: int = 0


@dataclass(slots=True)
class WorkspaceSweepStats:
    workspaces_examined: int = 0
    workspaces_removed: int = 0
    workspaces_protected: int = 0
    invalid_workspaces: int = 0
    directories_scanned: int = 0
    entries_scanned: int = 0
    mutations: int = 0
    lock_contended: bool = False
    budget_exhausted: bool = False
    progress_made: bool = False
    backlog_entries: int = 0
    workspaces_held: int = 0
    queue_record_bytes: int = 0
    queue_segments: int = 0
    queue_capacity_events: int = 0


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
    queue_descriptor: int | None = None
    queue_item: _QueueItem | None = None


@dataclass(frozen=True, slots=True)
class _QuarantineMetadata:
    quarantine_id: str
    object_key: str
    original_name: str
    expected_sha256: str
    owner_execution_id: str | None
    created_at: datetime
    source_dev: int | None = None
    source_ino: int | None = None
    source_size: int | None = None
    source_mtime_ns: int | None = None
    outcome: str | None = None
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    queue_descriptor: int | None = field(default=None, repr=False, compare=False)

    @property
    def source_identity(self) -> tuple[int, int] | None:
        if self.source_dev is None or self.source_ino is None:
            return None
        return self.source_dev, self.source_ino


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
        try:
            with _exclusive_descriptor_gate(
                self._thread_lock, descriptor, timeout_seconds=1.0
            ) as acquired:
                if not acquired:
                    raise RuntimeError("data root mutation lock is unavailable")
                _probe_root_capabilities(descriptor)
                _ensure_segmented_queue(descriptor, _QUEUE_ROOT)
                _migrate_nested_legacy_quarantines(
                    descriptor, monotonic=self._monotonic
                )
            _probe_lock_reacquire(descriptor)
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
                        source_dev=after.st_dev,
                        source_ino=after.st_ino,
                        source_size=after.st_size,
                        source_mtime_ns=after.st_mtime_ns,
                        queue_descriptor=self._root_descriptor_or_raise(),
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
                    if (
                        stat.S_ISREG(isolated.st_mode)
                        and _identity(isolated) == identity
                        and isolated.st_size == after.st_size
                        and isolated.st_mtime_ns == after.st_mtime_ns
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
                    if (
                        quarantine.queue_descriptor is not None
                        and quarantine.queue_item is not None
                    ):
                        _queue_complete(
                            quarantine.queue_descriptor,
                            _QUEUE_ROOT,
                            quarantine.queue_item,
                        )
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
        owner = str(UUID(owner_execution_id)) if owner_execution_id else None
        stage_name = ".wf-stage-{}{}".format(
            f"{owner}-" if owner else "", self._uuid_factory()
        )
        stage_key = "/".join((*parts[:-1], stage_name))
        root_descriptor = self._root_descriptor_or_raise()
        parent: _ParentBinding | None = None
        descriptor: int | None = None
        intent: _QueueItem | None = None
        try:
            with _exclusive_descriptor_gate(
                self._thread_lock, root_descriptor, timeout_seconds=5.0
            ) as acquired:
                if not acquired:
                    raise TimeoutError("data root mutation lock timed out")
                intent = _queue_enqueue(
                    root_descriptor,
                    _QUEUE_ROOT,
                    "ready",
                    {
                        "version": 2,
                        "kind": "publication",
                        "objectKey": object_key,
                        "stageObjectKey": stage_key,
                        "ownerExecutionId": owner,
                        "createdAt": self._clock().isoformat(
                            timespec="microseconds"
                        ),
                    },
                )
                parent = self._open_parent(parts[:-1], create=True)
                descriptor = _create_exclusive(parent, stage_name)
            assert parent is not None
            assert intent is not None
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                size, digest = writer(handle)
                handle.flush()
                os.fsync(handle.fileno())
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size != size:
                raise OSError("published object changed while it was copied")
            with _exclusive_descriptor_gate(
                self._thread_lock, root_descriptor, timeout_seconds=5.0
            ) as acquired:
                if not acquired:
                    raise TimeoutError("data root mutation lock timed out")
                _require_named_identity(parent, stage_name, _identity(status))
                snapshot = dict(intent.record)
                snapshot.update(
                    sourceDev=status.st_dev,
                    sourceIno=status.st_ino,
                    sourceSize=status.st_size,
                    sourceMtimeNs=status.st_mtime_ns,
                    sha256=digest,
                )
                intent = _queue_move(
                    root_descriptor,
                    _QUEUE_ROOT,
                    intent,
                    "ready",
                    snapshot,
                )
                _link_no_replace(parent, stage_name, parts[-1])
                _unlink_named(parent, stage_name)
                _fsync_parent(parent)
                if _GENERATED_ARTIFACT.fullmatch(object_key) is None:
                    _queue_complete(root_descriptor, _QUEUE_ROOT, intent)
            return PublishedObject(object_key, size, digest)
        except BaseException as primary:
            cleanup_errors: list[BaseException] = []
            if intent is not None and descriptor is None:
                try:
                    with _exclusive_descriptor_gate(
                        self._thread_lock, root_descriptor, timeout_seconds=5.0
                    ) as acquired:
                        if not acquired:
                            raise TimeoutError("data root cleanup lock timed out")
                        _queue_complete(root_descriptor, _QUEUE_ROOT, intent)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            elif descriptor is not None and parent is not None:
                try:
                    with _exclusive_descriptor_gate(
                        self._thread_lock, root_descriptor, timeout_seconds=5.0
                    ) as acquired:
                        if not acquired:
                            raise TimeoutError("data root cleanup lock timed out")
                        canonical_exists = True
                        try:
                            os.stat(
                                parts[-1],
                                dir_fd=parent.descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            canonical_exists = False
                        artifact = _GENERATED_ARTIFACT.fullmatch(object_key)
                        if artifact is None or not canonical_exists:
                            try:
                                _require_named_identity(
                                    parent, stage_name, _identity(os.fstat(descriptor))
                                )
                            except FileNotFoundError:
                                pass
                            else:
                                _unlink_named(parent, stage_name)
                                _fsync_parent(parent)
                                if intent is not None:
                                    _queue_complete(
                                        root_descriptor, _QUEUE_ROOT, intent
                                    )
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                primary.add_note(
                    "publication cleanup was deferred to durable maintenance"
                )
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent is not None:
                parent.close()

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
        deadline = self._monotonic() + limits.max_seconds
        with _exclusive_descriptor_gate(
            self._thread_lock,
            self._root_descriptor,
            timeout_seconds=max(0.0, deadline - self._monotonic()),
            deadline=deadline,
            monotonic=self._monotonic,
        ) as acquired:
            if not acquired:
                counts = _queue_counts(self._root_descriptor, _QUEUE_ROOT)
                return StorageSweepStats(
                    budget_exhausted=True,
                    lock_contended=True,
                    backlog_entries=counts["ready"] + counts["deferred"],
                    quarantines_held=counts["held"],
                    queue_record_bytes=counts["recordBytes"],
                    queue_segments=counts["segments"],
                    queue_capacity_events=counts["capacityEvents"],
                )
            return self._sweep_abandoned_locked(
                cutoff,
                referenced_artifact_keys,
                active_execution_ids=active_execution_ids,
                active_build_executions=active_build_executions,
                budget=limits,
                deadline=deadline,
            )

    def _sweep_abandoned_locked(
        self,
        cutoff: datetime,
        referenced_artifact_keys: frozenset[str],
        *,
        active_execution_ids: frozenset[str],
        active_build_executions: frozenset[tuple[str, str]],
        budget: StorageSweepBudget,
        deadline: float,
    ) -> StorageSweepStats:
        root_descriptor = self._duplicate_root()
        stats = StorageSweepStats()
        try:
            initial = _queue_counts(root_descriptor, _QUEUE_ROOT)
            lane_limits = {
                "deferred": initial["deferred"],
                "ready": initial["ready"],
            }
            for lane in ("ready", "deferred"):
                for _ in range(lane_limits[lane]):
                    if not self._data_budget_available(stats, budget, deadline):
                        stats.budget_exhausted = True
                        break
                    if lane == "deferred":
                        item, inspected = _queue_peek_due(
                            root_descriptor,
                            _QUEUE_ROOT,
                            lane,
                            self._clock(),
                            budget.max_entries - stats.entries_scanned,
                        )
                        stats.entries_scanned += inspected
                    else:
                        item = _queue_peek(root_descriptor, _QUEUE_ROOT, lane)
                        if item is not None:
                            stats.entries_scanned += 1
                    if item is None:
                        break
                    mutation_cost = _data_queue_mutation_cost(item.record)
                    if stats.mutations + mutation_cost > budget.max_mutations:
                        stats.budget_exhausted = True
                        break
                    self._process_data_queue_item(
                        root_descriptor,
                        item,
                        cutoff,
                        referenced_artifact_keys,
                        active_execution_ids,
                        active_build_executions,
                        stats,
                        budget,
                        mutation_cost,
                    )
                if stats.budget_exhausted:
                    break
            self._scan_legacy_root_entries(
                root_descriptor,
                cutoff,
                active_execution_ids,
                stats,
                budget,
                deadline,
            )
        finally:
            os.close(root_descriptor)
        counts = _queue_counts(self._root_descriptor_or_raise(), _QUEUE_ROOT)
        stats.backlog_entries = counts["ready"] + counts["deferred"]
        stats.quarantines_held = counts["held"]
        stats.queue_record_bytes = counts["recordBytes"]
        stats.queue_segments = counts["segments"]
        stats.queue_capacity_events = counts["capacityEvents"]
        if stats.backlog_entries:
            stats.budget_exhausted = True
        return stats

    def _data_budget_available(
        self,
        stats: StorageSweepStats,
        budget: StorageSweepBudget,
        deadline: float,
    ) -> bool:
        return (
            self._monotonic() < deadline
            and stats.entries_scanned < budget.max_entries
            and stats.quarantines_examined < budget.max_quarantines
            and stats.mutations < budget.max_mutations
        )

    def _process_data_queue_item(
        self,
        root_descriptor: int,
        item: _QueueItem,
        cutoff: datetime,
        referenced_artifact_keys: frozenset[str],
        active_execution_ids: frozenset[str],
        active_build_executions: frozenset[tuple[str, str]],
        stats: StorageSweepStats,
        budget: StorageSweepBudget,
        mutation_cost: int,
    ) -> None:
        kind = item.record.get("kind")
        if kind == "quarantine":
            stats.quarantines_examined += 1
            outcome = self._recover_queued_quarantine(
                root_descriptor,
                item,
                cutoff,
                referenced_artifact_keys,
                active_execution_ids,
                active_build_executions,
                stats,
                budget,
            )
        elif kind in {"artifact", "stage", "publication"}:
            outcome = self._recover_queued_file(
                root_descriptor,
                item,
                cutoff,
                referenced_artifact_keys,
                active_execution_ids,
                active_build_executions,
                stats,
            )
        else:
            stats.invalid_quarantines += 1
            outcome = "held"
        if outcome == "done":
            _queue_complete(root_descriptor, _QUEUE_ROOT, item)
        elif outcome == "deferred":
            record = dict(item.record)
            if kind == "quarantine":
                record["nextAttemptAt"] = (
                    self._clock() + timedelta(minutes=5)
                ).isoformat(timespec="microseconds")
            else:
                record.pop("nextAttemptAt", None)
            _queue_move(root_descriptor, _QUEUE_ROOT, item, "deferred", record)
        else:
            _queue_move(root_descriptor, _QUEUE_ROOT, item, "held")
            stats.quarantines_held += 1
        stats.mutations += mutation_cost
        stats.progress_made = True

    def _recover_queued_file(
        self,
        root_descriptor: int,
        item: _QueueItem,
        cutoff: datetime,
        referenced_artifact_keys: frozenset[str],
        active_execution_ids: frozenset[str],
        active_build_executions: frozenset[tuple[str, str]],
        stats: StorageSweepStats,
    ) -> str:
        object_key = item.record.get("objectKey")
        if not isinstance(object_key, str):
            return "held"
        if item.record.get("kind") == "publication":
            return self._recover_queued_publication(
                root_descriptor,
                item,
                cutoff,
                referenced_artifact_keys,
                active_execution_ids,
                active_build_executions,
            )
        try:
            parts = _object_key_parts(object_key)
            parent = self._open_parent(parts[:-1], create=False)
        except (InvalidObjectKey, FileNotFoundError):
            return "done"
        try:
            try:
                status = os.stat(
                    parts[-1], dir_fd=parent.descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                return "done"
            if not stat.S_ISREG(status.st_mode):
                return "held"
            if status.st_mtime > cutoff.timestamp():
                return "deferred"
            kind = item.record.get("kind")
            if kind == "stage":
                match = _GENERATED_STAGE.fullmatch(parts[-1])
                if match is None:
                    return "held"
                if match.group("owner") in active_execution_ids:
                    return "deferred"
            else:
                match = _GENERATED_ARTIFACT.fullmatch(object_key)
                if match is None:
                    return "held"
                protected = object_key in referenced_artifact_keys
                if match.group("owner") is not None:
                    protected = protected or (
                        match.group("task"), match.group("owner")
                    ) in active_build_executions
                if protected:
                    return "deferred"
            os.unlink(parts[-1], dir_fd=parent.descriptor)
            _fsync_parent(parent)
            if kind == "artifact" and match is not None:
                _prune_artifact_directories(
                    root_descriptor,
                    {(match.group("task"), match.group("owner"))},
                )
            return "done"
        finally:
            parent.close()

    def _recover_queued_publication(
        self,
        root_descriptor: int,
        item: _QueueItem,
        cutoff: datetime,
        referenced_artifact_keys: frozenset[str],
        active_execution_ids: frozenset[str],
        active_build_executions: frozenset[tuple[str, str]],
    ) -> str:
        object_key = item.record.get("objectKey")
        stage_key = item.record.get("stageObjectKey")
        if not isinstance(object_key, str) or not isinstance(stage_key, str):
            return "held"
        try:
            parts = _object_key_parts(object_key)
            stage_parts = _object_key_parts(stage_key)
        except InvalidObjectKey:
            return "held"
        if parts[:-1] != stage_parts[:-1]:
            return "held"
        stage_match = _GENERATED_STAGE.fullmatch(stage_parts[-1])
        if stage_match is None:
            return "held"
        try:
            parent = self._open_parent(parts[:-1], create=False)
        except FileNotFoundError:
            return "done"
        try:
            canonical = _optional_named_regular_status(parent, parts[-1])
            staged = _optional_named_regular_status(parent, stage_parts[-1])
            if canonical is None and staged is None:
                return "done"
            source_dev = item.record.get("sourceDev")
            source_ino = item.record.get("sourceIno")
            source_size = item.record.get("sourceSize")
            source_mtime_ns = item.record.get("sourceMtimeNs")
            if not all(
                isinstance(value, int)
                for value in (
                    source_dev,
                    source_ino,
                    source_size,
                    source_mtime_ns,
                )
            ):
                if canonical is not None:
                    return "held"
                if (
                    staged is not None
                    and (
                        staged.st_mtime > cutoff.timestamp()
                        or stage_match.group("owner") in active_execution_ids
                    )
                ):
                    return "deferred"
                if staged is not None:
                    os.unlink(stage_parts[-1], dir_fd=parent.descriptor)
                    _fsync_parent(parent)
                return "done"
            source_identity = (source_dev, source_ino)

            def is_owned(status: os.stat_result | None) -> bool:
                return (
                    status is not None
                    and stat.S_ISREG(status.st_mode)
                    and _identity(status) == source_identity
                    and status.st_size == source_size
                )

            artifact = _GENERATED_ARTIFACT.fullmatch(object_key)
            if canonical is not None:
                if not is_owned(canonical):
                    if is_owned(staged):
                        os.unlink(stage_parts[-1], dir_fd=parent.descriptor)
                        _fsync_parent(parent)
                        if artifact is None:
                            return "done"
                    elif staged is None and artifact is None:
                        return "done"
                    return "held"
                if staged is not None and not is_owned(staged):
                    return "held"
                if artifact is None:
                    if is_owned(staged):
                        os.unlink(stage_parts[-1], dir_fd=parent.descriptor)
                        _fsync_parent(parent)
                    return "done"
                protected = object_key in referenced_artifact_keys
                owner = artifact.group("owner")
                if owner is not None:
                    protected = protected or (
                        artifact.group("task"), owner
                    ) in active_build_executions
                if protected or canonical.st_mtime > cutoff.timestamp():
                    return "deferred"
                os.unlink(parts[-1], dir_fd=parent.descriptor)
                if is_owned(staged):
                    os.unlink(stage_parts[-1], dir_fd=parent.descriptor)
                _fsync_parent(parent)
                _prune_artifact_directories(
                    root_descriptor,
                    {(artifact.group("task"), artifact.group("owner"))},
                )
                return "done"
            if staged is None:
                return "done"
            if not is_owned(staged):
                return "held"
            if (
                staged.st_mtime > cutoff.timestamp()
                or stage_match.group("owner") in active_execution_ids
            ):
                return "deferred"
            os.unlink(stage_parts[-1], dir_fd=parent.descriptor)
            _fsync_parent(parent)
            return "done"
        finally:
            parent.close()

    def _recover_queued_quarantine(
        self,
        root_descriptor: int,
        item: _QueueItem,
        cutoff: datetime,
        referenced_artifact_keys: frozenset[str],
        active_execution_ids: frozenset[str],
        active_build_executions: frozenset[tuple[str, str]],
        stats: StorageSweepStats,
        budget: StorageSweepBudget,
    ) -> str:
        object_key = item.record.get("objectKey")
        quarantine_id = item.record.get("quarantineId")
        if not isinstance(object_key, str) or not isinstance(quarantine_id, str):
            stats.invalid_quarantines += 1
            return "held"
        try:
            parts = _object_key_parts(object_key)
            parent = self._open_parent(parts[:-1], create=False)
        except (InvalidObjectKey, FileNotFoundError):
            stats.invalid_quarantines += 1
            return "held"
        if parent.descriptor is None:
            parent.close()
            raise RuntimeError("safe parent directory handle is unavailable")
        quarantine_name = f".wf-quarantine-v1-{quarantine_id}"
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    quarantine_name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent.descriptor,
                )
            except FileNotFoundError:
                return "done"
            try:
                metadata = _read_quarantine_metadata(descriptor, quarantine_id)
            except (OSError, RuntimeError, ValueError):
                stats.invalid_quarantines += 1
                return "held"
            if metadata is None or metadata.object_key != object_key:
                stats.invalid_quarantines += 1
                return "held"
            if metadata.outcome in {"conflict", "invalid", "held"}:
                if metadata.outcome == "conflict":
                    stats.quarantine_conflicts += 1
                else:
                    stats.invalid_quarantines += 1
                return "held"
            if metadata.created_at > cutoff:
                return "deferred"
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
                return "deferred"
            try:
                _isolate_quarantine_candidate(descriptor, metadata.original_name)
                candidate = os.stat(
                    _RECOVERY_CANDIDATE,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if _descriptor_directory_empty(descriptor):
                    os.close(descriptor)
                    descriptor = -1
                    os.rmdir(quarantine_name, dir_fd=parent.descriptor)
                    _fsync_parent(parent)
                    stats.empty_quarantines_removed += 1
                    candidate_prune = _quarantine_prune_candidate(
                        Path(*parts[:-1]), metadata
                    )
                    if candidate_prune is not None:
                        _prune_artifact_directories(
                            root_descriptor, {candidate_prune}
                        )
                    return "done"
                stats.invalid_quarantines += 1
                return "held"
            observed_size = (
                metadata.source_size
                if metadata.source_size is not None
                else candidate.st_size
            )
            if observed_size > budget.max_bytes:
                stats.oversized_quarantines += 1
            if metadata.source_identity is None:
                _persist_quarantine_outcome(descriptor, metadata, "held")
                stats.invalid_quarantines += 1
                return "held"
            if (
                not stat.S_ISREG(candidate.st_mode)
                or _identity(candidate) != metadata.source_identity
                or candidate.st_size != metadata.source_size
                or candidate.st_mtime_ns != metadata.source_mtime_ns
            ):
                _persist_quarantine_outcome(descriptor, metadata, "conflict")
                stats.quarantine_conflicts += 1
                return "held"
            try:
                _rename_no_replace(
                    descriptor,
                    _RECOVERY_CANDIDATE,
                    parent.descriptor,
                    metadata.original_name,
                )
            except FileExistsError:
                _persist_quarantine_outcome(descriptor, metadata, "conflict")
                stats.quarantine_conflicts += 1
                return "held"
            _fsync_descriptor(descriptor)
            _fsync_parent(parent)
            os.close(descriptor)
            descriptor = -1
            os.rmdir(quarantine_name, dir_fd=parent.descriptor)
            _fsync_parent(parent)
            stats.quarantines_restored += 1
            candidate_prune = _quarantine_prune_candidate(
                Path(*parts[:-1]), metadata
            )
            if candidate_prune is not None:
                _prune_artifact_directories(root_descriptor, {candidate_prune})
            return "done"
        finally:
            if descriptor != -1:
                os.close(descriptor)
            parent.close()

    def _scan_legacy_root_entries(
        self,
        root_descriptor: int,
        cutoff: datetime,
        active_execution_ids: frozenset[str],
        stats: StorageSweepStats,
        budget: StorageSweepBudget,
        deadline: float,
    ) -> None:
        if not self._data_budget_available(stats, budget, deadline):
            return
        stats.directories_scanned += 1
        with os.scandir(root_descriptor) as entries:
            for entry in entries:
                if not self._data_budget_available(stats, budget, deadline):
                    stats.budget_exhausted = True
                    return
                stats.entries_scanned += 1
                match = _GENERATED_STAGE.fullmatch(entry.name)
                quarantine = _QUARANTINE.fullmatch(entry.name)
                if match is not None:
                    status = entry.stat(follow_symlinks=False)
                    if (
                        stat.S_ISREG(status.st_mode)
                        and status.st_mtime <= cutoff.timestamp()
                        and match.group("owner") not in active_execution_ids
                    ):
                        os.unlink(entry.name, dir_fd=root_descriptor)
                        _fsync_descriptor(root_descriptor)
                        stats.mutations += 2
                        stats.progress_made = True
                elif quarantine is not None:
                    descriptor = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=root_descriptor,
                    )
                    try:
                        try:
                            metadata = _read_quarantine_metadata(
                                descriptor, quarantine.group("id")
                            )
                        except (OSError, RuntimeError, ValueError):
                            metadata = None
                        old_enough = (
                            metadata is not None and metadata.created_at <= cutoff
                        ) or os.fstat(descriptor).st_mtime <= cutoff.timestamp()
                        if (
                            old_enough
                            and _descriptor_directory_empty(descriptor)
                        ):
                            os.close(descriptor)
                            descriptor = -1
                            os.rmdir(entry.name, dir_fd=root_descriptor)
                            _fsync_descriptor(root_descriptor)
                            stats.empty_quarantines_removed += 1
                            stats.mutations += 2
                            stats.progress_made = True
                        elif old_enough:
                            _index_legacy_quarantine(
                                root_descriptor,
                                descriptor,
                                quarantine.group("id"),
                                metadata,
                                stats,
                                budget,
                            )
                    finally:
                        if descriptor != -1:
                            os.close(descriptor)
        if not self._data_budget_available(stats, budget, deadline):
            return
        try:
            artifacts = os.open(
                "artifacts",
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
        except FileNotFoundError:
            return
        try:
            stats.directories_scanned += 1
            with os.scandir(artifacts) as entries:
                for entry in entries:
                    if not self._data_budget_available(stats, budget, deadline):
                        stats.budget_exhausted = True
                        return
                    stats.entries_scanned += 1
                    match = _QUARANTINE.fullmatch(entry.name)
                    if match is None:
                        continue
                    descriptor = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=artifacts,
                    )
                    try:
                        try:
                            metadata = _read_quarantine_metadata(
                                descriptor, match.group("id")
                            )
                        except (OSError, RuntimeError, ValueError):
                            metadata = None
                        old_enough = (
                            metadata is not None and metadata.created_at <= cutoff
                        ) or os.fstat(descriptor).st_mtime <= cutoff.timestamp()
                        if (
                            old_enough
                            and _descriptor_directory_empty(descriptor)
                        ):
                            os.close(descriptor)
                            descriptor = -1
                            os.rmdir(entry.name, dir_fd=artifacts)
                            _fsync_descriptor(artifacts)
                            stats.empty_quarantines_removed += 1
                            stats.mutations += 2
                            stats.progress_made = True
                        elif old_enough:
                            _index_legacy_quarantine(
                                root_descriptor,
                                descriptor,
                                match.group("id"),
                                metadata,
                                stats,
                                budget,
                            )
                    finally:
                        if descriptor != -1:
                            os.close(descriptor)
        finally:
            os.close(artifacts)


@dataclass(slots=True)
class OwnedWorkspace:
    path: Path
    _root_descriptor: int | None
    _identity: tuple[int, int]
    _thread_lock: Any
    capability: WorkspaceCapability
    _queue_item: _QueueItem
    _monotonic: Callable[[], float] = time.monotonic
    _cleaned: bool = False

    def cleanup(
        self, *, budget: StorageSweepBudget | None = None
    ) -> WorkspaceSweepStats:
        stats = WorkspaceSweepStats()
        if self._cleaned:
            return stats
        if self._root_descriptor is None:
            raise RuntimeError("workspace root is closed")
        limits = budget or StorageSweepBudget()
        deadline = self._monotonic() + limits.max_seconds
        with _exclusive_descriptor_gate(
            self._thread_lock,
            self._root_descriptor,
            timeout_seconds=max(0.0, deadline - self._monotonic()),
            deadline=deadline,
            monotonic=self._monotonic,
        ) as acquired:
            if not acquired:
                stats.lock_contended = True
                stats.budget_exhausted = True
                stats.backlog_entries = 1
                return stats
            root_descriptor = os.dup(self._root_descriptor)
            try:
                completed = _bounded_cleanup_workspace(
                    root_descriptor,
                    self.path.name,
                    self._identity,
                    stats,
                    limits,
                    deadline,
                    self._monotonic,
                )
                if completed:
                    _queue_complete(
                        root_descriptor,
                        _WORKSPACE_QUEUE_ROOT,
                        self._queue_item,
                    )
            finally:
                os.close(root_descriptor)
        stats.backlog_entries = 0 if completed else 1
        stats.budget_exhausted = not completed
        self._cleaned = True
        self.close()
        return stats

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
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        _require_supported_host()
        self._root_descriptor: int | None
        self.root, descriptor = _open_stable_root(Path(root), "workspace root")
        self._root_descriptor = descriptor
        self._root_identity = _identity(os.fstat(self._root_descriptor))
        self._uuid_factory = uuid_factory
        self._monotonic = monotonic
        self._clock = clock
        self._thread_lock = threading.RLock()
        try:
            with _exclusive_descriptor_gate(
                self._thread_lock, descriptor, timeout_seconds=1.0
            ) as acquired:
                if not acquired:
                    raise RuntimeError("workspace root mutation lock is unavailable")
                _probe_workspace_capabilities(descriptor)
                _ensure_segmented_queue(descriptor, _WORKSPACE_QUEUE_ROOT)
            _probe_lock_reacquire(descriptor)
        except BaseException as error:
            self.close()
            raise RuntimeError("workspace root capability probe failed") from error

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
                intent = _queue_enqueue(
                    root_descriptor,
                    _WORKSPACE_QUEUE_ROOT,
                    "ready",
                    {
                        "version": 2,
                        "recordId": str(identifier),
                        "kind": "workspace",
                        "workspaceName": name,
                        "executionId": str(identifier),
                        "createdAt": self._clock().isoformat(
                            timespec="microseconds"
                        ),
                    },
                )
                try:
                    os.mkdir(name, 0o700, dir_fd=root_descriptor)
                except FileExistsError:
                    _queue_complete(
                        root_descriptor, _WORKSPACE_QUEUE_ROOT, intent
                    )
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
                    bound_record = dict(intent.record)
                    bound_record.update(
                        sourceDev=status.st_dev,
                        sourceIno=status.st_ino,
                    )
                    intent = _queue_move(
                        root_descriptor,
                        _WORKSPACE_QUEUE_ROOT,
                        intent,
                        "ready",
                        bound_record,
                    )
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
                        intent,
                        self._monotonic,
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
    ) -> WorkspaceSweepStats:
        if self._root_descriptor is None:
            raise RuntimeError("workspace root is closed")
        limits = budget or StorageSweepBudget()
        deadline = self._monotonic() + limits.max_seconds
        with _exclusive_descriptor_gate(
            self._thread_lock,
            self._root_descriptor,
            timeout_seconds=max(0.0, deadline - self._monotonic()),
            deadline=deadline,
            monotonic=self._monotonic,
        ) as acquired:
            if not acquired:
                counts = _queue_counts(
                    self._root_descriptor, _WORKSPACE_QUEUE_ROOT
                )
                return WorkspaceSweepStats(
                    lock_contended=True,
                    budget_exhausted=True,
                    backlog_entries=counts["ready"] + counts["deferred"],
                    workspaces_held=counts["held"],
                    queue_record_bytes=counts["recordBytes"],
                    queue_segments=counts["segments"],
                    queue_capacity_events=counts["capacityEvents"],
                )
            return self._sweep_abandoned_locked(
                cutoff, active_execution_ids, limits, deadline
            )

    def _sweep_abandoned_locked(
        self,
        cutoff: datetime,
        active_execution_ids: frozenset[str],
        budget: StorageSweepBudget,
        deadline: float,
    ) -> WorkspaceSweepStats:
        root_descriptor = self._duplicate_root()
        stats = WorkspaceSweepStats()
        try:
            initial = _queue_counts(root_descriptor, _WORKSPACE_QUEUE_ROOT)
            for lane in ("ready", "deferred"):
                for _ in range(initial[lane]):
                    if (
                        self._monotonic() >= deadline
                        or stats.workspaces_examined >= budget.max_entries
                        or stats.mutations + 10 > budget.max_mutations
                    ):
                        stats.budget_exhausted = True
                        break
                    if lane == "deferred":
                        item, inspected = _queue_peek_due(
                            root_descriptor,
                            _WORKSPACE_QUEUE_ROOT,
                            lane,
                            self._clock(),
                            budget.max_entries - stats.workspaces_examined,
                        )
                        stats.workspaces_examined += inspected
                    else:
                        item = _queue_peek(
                            root_descriptor, _WORKSPACE_QUEUE_ROOT, lane
                        )
                        if item is not None:
                            stats.workspaces_examined += 1
                    if item is None:
                        break
                    record = item.record
                    execution_id = record.get("executionId")
                    name = record.get("workspaceName")
                    source_dev = record.get("sourceDev")
                    source_ino = record.get("sourceIno")
                    if (
                        record.get("kind") != "workspace"
                        or not isinstance(execution_id, str)
                        or not isinstance(name, str)
                        or not _workspace_record_name_matches(
                            name, execution_id
                        )
                        or (source_dev is not None and not isinstance(source_dev, int))
                        or (source_ino is not None and not isinstance(source_ino, int))
                        or ((source_dev is None) != (source_ino is None))
                    ):
                        stats.invalid_workspaces += 1
                        _queue_move(
                            root_descriptor,
                            _WORKSPACE_QUEUE_ROOT,
                            item,
                            "held",
                        )
                        stats.workspaces_held += 1
                        stats.mutations += 10
                        stats.progress_made = True
                        continue
                    if execution_id in active_execution_ids:
                        stats.workspaces_protected += 1
                        _queue_move(
                            root_descriptor,
                            _WORKSPACE_QUEUE_ROOT,
                            item,
                            "deferred",
                            _deferred_queue_record(item.record, self._clock()),
                        )
                        stats.mutations += 10
                        stats.progress_made = True
                        continue
                    expected_identity = (
                        (source_dev, source_ino)
                        if source_dev is not None and source_ino is not None
                        else None
                    )
                    observed = _workspace_status(
                        root_descriptor, name, expected_identity
                    )
                    if observed is None:
                        _queue_complete(
                            root_descriptor, _WORKSPACE_QUEUE_ROOT, item
                        )
                        stats.mutations += 5
                        stats.progress_made = True
                        continue
                    if expected_identity is None:
                        stats.invalid_workspaces += 1
                        _queue_move(
                            root_descriptor,
                            _WORKSPACE_QUEUE_ROOT,
                            item,
                            "held",
                        )
                        stats.workspaces_held += 1
                        stats.mutations += 10
                        stats.progress_made = True
                        continue
                    if observed.st_mtime > _datetime_timestamp(cutoff):
                        _queue_move(
                            root_descriptor,
                            _WORKSPACE_QUEUE_ROOT,
                            item,
                            "deferred",
                            _immediate_queue_record(item.record),
                        )
                        stats.mutations += 10
                        stats.progress_made = True
                        continue
                    completed = _bounded_cleanup_workspace(
                        root_descriptor,
                        name,
                        _identity(observed),
                        stats,
                        _reserved_workspace_budget(budget, reserve=10),
                        deadline,
                        self._monotonic,
                    )
                    if completed:
                        _queue_complete(
                            root_descriptor, _WORKSPACE_QUEUE_ROOT, item
                        )
                        stats.workspaces_removed += 1
                        stats.mutations += 5
                    else:
                        _queue_move(
                            root_descriptor,
                            _WORKSPACE_QUEUE_ROOT,
                            item,
                            "deferred",
                            _immediate_queue_record(item.record),
                        )
                        stats.mutations += 10
                    stats.progress_made = True
                if stats.budget_exhausted:
                    break
        finally:
            os.close(root_descriptor)
        counts = _queue_counts(
            self._root_descriptor if self._root_descriptor is not None else -1,
            _WORKSPACE_QUEUE_ROOT,
        )
        stats.backlog_entries = counts["ready"] + counts["deferred"]
        stats.workspaces_held = counts["held"]
        stats.queue_record_bytes = counts["recordBytes"]
        stats.queue_segments = counts["segments"]
        stats.queue_capacity_events = counts["capacityEvents"]
        if stats.backlog_entries:
            stats.budget_exhausted = True
        return stats


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



_QueueItem = _segmented_queue.QueueItem


def _queue_io() -> _segmented_queue.QueueIO:
    return _segmented_queue.QueueIO(
        get_xattr=lambda descriptor, name, max_bytes: _get_descriptor_xattr(
            descriptor, name, max_bytes=max_bytes
        ),
        set_xattr=_set_descriptor_xattr,
        put_xattr=_put_descriptor_xattr,
        fsync=_fsync_descriptor,
    )


def _queue_limits() -> _segmented_queue.QueueLimits:
    return _segmented_queue.QueueLimits(
        segment_size=_QUEUE_SEGMENT_SIZE,
        max_segments=_QUEUE_MAX_SEGMENTS,
        max_records=_QUEUE_MAX_RECORDS,
        max_record_bytes=_QUEUE_MAX_RECORD_BYTES,
        record_max_bytes=_QUEUE_RECORD_MAX_BYTES,
        state_max_bytes=_MAX_SWEEP_STATE_BYTES,
        reconcile_max_mutations=_QUEUE_RECONCILE_MAX_MUTATIONS,
    )


def _queue_initial_state() -> dict[str, Any]:
    return _segmented_queue.initial_state()


def _queue_record_is_due(record: dict[str, Any], now: datetime) -> bool:
    value = record.get("nextAttemptAt")
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > 64:
        return True
    try:
        next_attempt = datetime.fromisoformat(value)
    except ValueError:
        return True
    return next_attempt <= now


def _deferred_queue_record(
    record: dict[str, Any], now: datetime
) -> dict[str, Any]:
    deferred = dict(record)
    deferred["nextAttemptAt"] = (now + timedelta(minutes=5)).isoformat(
        timespec="microseconds"
    )
    return deferred


def _immediate_queue_record(record: dict[str, Any]) -> dict[str, Any]:
    immediate = dict(record)
    immediate.pop("nextAttemptAt", None)
    return immediate


def _data_queue_mutation_cost(record: dict[str, Any]) -> int:
    kind = record.get("kind")
    if kind == "quarantine":
        return 16
    if kind in {"artifact", "publication"}:
        return 12
    if kind == "stage":
        return 8
    return 5


def _ensure_segmented_queue(root_descriptor: int, name: str) -> None:
    _segmented_queue.ensure(
        root_descriptor,
        name,
        io=_queue_io(),
        limits=_queue_limits(),
        state_xattr=_QUEUE_STATE_XATTR,
        write_state=_write_queue_state,
    )


def _encode_queue_state(state: dict[str, Any]) -> bytes:
    return _segmented_queue.encode_state(state, _MAX_SWEEP_STATE_BYTES)


def _read_queue_state(queue_descriptor: int) -> dict[str, Any]:
    return _segmented_queue.read_state(
        queue_descriptor,
        io=_queue_io(),
        limits=_queue_limits(),
        state_xattr=_QUEUE_STATE_XATTR,
    )


def _write_queue_state(queue_descriptor: int, state: dict[str, Any]) -> None:
    _segmented_queue.write_state(
        queue_descriptor,
        state,
        io=_queue_io(),
        state_xattr=_QUEUE_STATE_XATTR,
        state_max_bytes=_MAX_SWEEP_STATE_BYTES,
    )


def _open_queue(root_descriptor: int, name: str) -> int:
    return _segmented_queue.open_queue(root_descriptor, name)


def _open_queue_lane(queue_descriptor: int, lane: str) -> int:
    return _segmented_queue.open_lane(queue_descriptor, lane)


def _queue_capacity() -> int:
    return _queue_limits().capacity


def _reconcile_queue_descriptor(queue_descriptor: int) -> None:
    _segmented_queue.reconcile(
        queue_descriptor,
        io=_queue_io(),
        limits=_queue_limits(),
        state_xattr=_QUEUE_STATE_XATTR,
        write_state=_write_queue_state,
    )


def _queue_enqueue(
    root_descriptor: int,
    queue_name: str,
    lane_name: str,
    record: dict[str, Any],
    *,
    deduplicate: bool = True,
    deduplicate_generation: bool = False,
) -> _QueueItem:
    return _segmented_queue.enqueue(
        root_descriptor,
        queue_name,
        lane_name,
        record,
        io=_queue_io(),
        limits=_queue_limits(),
        state_xattr=_QUEUE_STATE_XATTR,
        write_state_callback=_write_queue_state,
        deduplicate=deduplicate,
        deduplicate_generation=deduplicate_generation,
    )


def _queue_peek(
    root_descriptor: int, queue_name: str, lane_name: str
) -> _QueueItem | None:
    return _segmented_queue.peek(
        root_descriptor,
        queue_name,
        lane_name,
        io=_queue_io(),
        limits=_queue_limits(),
        state_xattr=_QUEUE_STATE_XATTR,
        reconcile_callback=_reconcile_queue_descriptor,
    )


def _queue_peek_due(
    root_descriptor: int,
    queue_name: str,
    lane_name: str,
    now: datetime,
    max_records: int,
) -> tuple[_QueueItem | None, int]:
    return _segmented_queue.peek_due(
        root_descriptor,
        queue_name,
        lane_name,
        now,
        max_records,
        io=_queue_io(),
        limits=_queue_limits(),
        state_xattr=_QUEUE_STATE_XATTR,
        is_due=_queue_record_is_due,
        reconcile_callback=_reconcile_queue_descriptor,
    )


def _queue_complete(
    root_descriptor: int, queue_name: str, item: _QueueItem
) -> None:
    _segmented_queue.complete(
        root_descriptor,
        queue_name,
        item,
        io=_queue_io(),
        limits=_queue_limits(),
        state_xattr=_QUEUE_STATE_XATTR,
        write_state_callback=_write_queue_state,
        reconcile_callback=_reconcile_queue_descriptor,
    )


def _queue_move(
    root_descriptor: int,
    queue_name: str,
    item: _QueueItem,
    destination: str,
    record: dict[str, Any] | None = None,
) -> _QueueItem:
    moved = dict(record or item.record)
    moved["recordId"] = item.record["recordId"]
    moved["generation"] = int(item.record["generation"]) + 1
    moved.pop("sequence", None)
    destination_item = _queue_enqueue(
        root_descriptor,
        queue_name,
        destination,
        moved,
        deduplicate=False,
        deduplicate_generation=True,
    )
    _queue_complete(root_descriptor, queue_name, item)
    return destination_item


def _queue_counts(root_descriptor: int, queue_name: str) -> dict[str, int]:
    return _segmented_queue.counts(
        root_descriptor,
        queue_name,
        io=_queue_io(),
        limits=_queue_limits(),
        state_xattr=_QUEUE_STATE_XATTR,
    )


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
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    absolute_deadline = (
        monotonic() + timeout_seconds if deadline is None else deadline
    )
    remaining = absolute_deadline - monotonic()
    if remaining <= 0:
        yield False
        return
    acquired_thread = thread_lock.acquire(
        timeout=remaining
    )
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
                remaining = absolute_deadline - monotonic()
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


def _optional_named_regular_status(
    parent: _ParentBinding, name: str
) -> os.stat_result | None:
    if parent.descriptor is None:
        raise RuntimeError("safe parent directory handle is unavailable")
    try:
        status = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise RuntimeError("maintenance queue target is not a regular file")
    return status


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
    queue_item: _QueueItem | None = None
    if metadata.queue_descriptor is not None:
        queue_item = _queue_enqueue(
            metadata.queue_descriptor,
            _QUEUE_ROOT,
            "ready",
            {
                "version": 2,
                "recordId": metadata.quarantine_id,
                "kind": "quarantine",
                "objectKey": metadata.object_key,
                "quarantineId": metadata.quarantine_id,
                "createdAt": metadata.created_at.isoformat(
                    timespec="microseconds"
                ),
            },
        )
    try:
        os.mkdir(quarantine_name, 0o700, dir_fd=parent.descriptor)
    except BaseException:
        if metadata.queue_descriptor is not None and queue_item is not None:
            _best_effort(
                _queue_complete,
                metadata.queue_descriptor,
                _QUEUE_ROOT,
                queue_item,
            )
        raise
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
            if metadata.queue_descriptor is not None and queue_item is not None:
                _queue_complete(
                    metadata.queue_descriptor, _QUEUE_ROOT, queue_item
                )
            return None
        moved = True
        _fsync_parent(parent)
        _fsync_descriptor(quarantine_descriptor)
        return _QuarantineBinding(
            quarantine_name,
            quarantine_descriptor,
            metadata.queue_descriptor,
            queue_item,
        )
    except BaseException:
        if quarantine_descriptor != -1:
            os.close(quarantine_descriptor)
        if not moved:
            _best_effort(
                _remove_empty_quarantine_directory, parent, quarantine_name
            )
            if metadata.queue_descriptor is not None and queue_item is not None:
                _best_effort(
                    _queue_complete,
                    metadata.queue_descriptor,
                    _QUEUE_ROOT,
                    queue_item,
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
    source_directory = -1
    destination_directory = -1
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
        os.mkdir("source-dir", 0o700, dir_fd=directory)
        os.mkdir("destination-dir", 0o700, dir_fd=directory)
        source_directory = os.open(
            "source-dir",
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        destination_directory = os.open(
            "destination-dir",
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        for leaf, content in (
            ("source", b"source"),
            ("second", b"second"),
        ):
            descriptor = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=source_directory,
            )
            try:
                os.write(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        occupied = os.open(
            "occupied",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=destination_directory,
        )
        try:
            os.write(occupied, b"occupied")
            os.fsync(occupied)
        finally:
            os.close(occupied)
        source_status = os.stat(
            "source", dir_fd=source_directory, follow_symlinks=False
        )
        second_status = os.stat(
            "second", dir_fd=source_directory, follow_symlinks=False
        )
        occupied_status = os.stat(
            "occupied", dir_fd=destination_directory, follow_symlinks=False
        )
        _rename_no_replace(
            source_directory, "source", destination_directory, "moved"
        )
        moved_status = os.stat(
            "moved", dir_fd=destination_directory, follow_symlinks=False
        )
        if _identity(moved_status) != _identity(source_status):
            raise RuntimeError("no-replace rename changed source identity")
        moved = os.open(
            "moved", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=destination_directory
        )
        try:
            if os.read(moved, 16) != b"source":
                raise RuntimeError("no-replace rename changed source content")
        finally:
            os.close(moved)
        try:
            _rename_no_replace(
                source_directory, "second", destination_directory, "occupied"
            )
        except FileExistsError:
            pass
        else:
            raise RuntimeError("no-replace rename replaced an occupied destination")
        if _identity(os.stat("second", dir_fd=source_directory)) != _identity(
            second_status
        ):
            raise RuntimeError("EEXIST changed the source")
        if _identity(os.stat("occupied", dir_fd=destination_directory)) != _identity(
            occupied_status
        ):
            raise RuntimeError("EEXIST changed the destination")
        try:
            _rename_no_replace(
                source_directory, "missing", destination_directory, "absent"
            )
        except FileNotFoundError as error:
            if error.errno != errno.ENOENT:
                raise RuntimeError("missing source returned the wrong errno") from error
        else:
            raise RuntimeError("missing source did not preserve ENOENT semantics")
        marker = b"wheelforge-capability-v1"
        _set_descriptor_xattr(source_directory, _QUARANTINE_XATTR, marker)
        try:
            _set_descriptor_xattr(source_directory, _QUARANTINE_XATTR, marker)
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
        else:
            raise RuntimeError("xattr create did not preserve EEXIST semantics")
        os.close(source_directory)
        source_directory = os.open(
            "source-dir",
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        if _get_descriptor_xattr(source_directory, _QUARANTINE_XATTR) != marker:
            raise RuntimeError("descriptor xattr round trip failed")
        replacement_marker = b"wheelforge-capability-v2"
        _put_descriptor_xattr(
            source_directory, _QUARANTINE_XATTR, replacement_marker
        )
        if (
            _get_descriptor_xattr(source_directory, _QUARANTINE_XATTR)
            != replacement_marker
        ):
            raise RuntimeError("descriptor xattr replacement failed")
        _fsync_descriptor(source_directory)
        _fsync_descriptor(destination_directory)
        os.fsync(directory)
        os.fsync(root_descriptor)
    except BaseException as error:
        failure = error
    finally:
        cleanup_errors: list[BaseException] = []
        if source_directory != -1:
            for leaf in ("source", "second"):
                try:
                    os.unlink(leaf, dir_fd=source_directory)
                except FileNotFoundError:
                    pass
                except BaseException as error:
                    cleanup_errors.append(error)
            try:
                os.close(source_directory)
            except BaseException as error:
                cleanup_errors.append(error)
        if destination_directory != -1:
            for leaf in ("occupied", "moved", "absent"):
                try:
                    os.unlink(leaf, dir_fd=destination_directory)
                except FileNotFoundError:
                    pass
                except BaseException as error:
                    cleanup_errors.append(error)
            try:
                os.close(destination_directory)
            except BaseException as error:
                cleanup_errors.append(error)
        if directory != -1:
            for leaf in ("source-dir", "destination-dir"):
                try:
                    os.rmdir(leaf, dir_fd=directory)
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


def _probe_workspace_capabilities(root_descriptor: int) -> None:
    name = f".wf-workspace-probe-{uuid4()}"
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
        if not stat.S_ISDIR(os.fstat(directory).st_mode):
            raise RuntimeError("workspace descriptor traversal is unavailable")
        child = os.open(
            "probe.bin",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            os.write(child, b"workspace")
            os.fsync(child)
        finally:
            os.close(child)
        reopened = os.open(
            "probe.bin",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        try:
            if os.read(reopened, 32) != b"workspace":
                raise RuntimeError("workspace descriptor traversal changed content")
        finally:
            os.close(reopened)
        os.unlink("probe.bin", dir_fd=directory)
        _fsync_descriptor(directory)
        os.close(directory)
        directory = -1
        os.rmdir(name, dir_fd=root_descriptor)
        created = False
        _fsync_descriptor(root_descriptor)
    except BaseException as error:
        failure = error
    finally:
        cleanup_errors: list[BaseException] = []
        if directory != -1:
            try:
                os.unlink("probe.bin", dir_fd=directory)
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
                _fsync_descriptor(root_descriptor)
            except BaseException as error:
                cleanup_errors.append(error)
        if failure is not None and cleanup_errors:
            raise BaseExceptionGroup(
                "workspace capability probe and cleanup failed",
                [failure, *cleanup_errors],
            )
        if failure is not None:
            raise failure
        if cleanup_errors:
            raise BaseExceptionGroup(
                "workspace capability probe cleanup failed", cleanup_errors
            )


def _require_lock_contention(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    raise RuntimeError("data root lock contention semantics are unavailable")


def _probe_lock_reacquire(descriptor: int) -> None:
    for _ in range(2):
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)


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
            "schemaVersion": 2,
            "quarantineId": metadata.quarantine_id,
            "objectKey": metadata.object_key,
            "originalName": metadata.original_name,
            "expectedSha256": metadata.expected_sha256,
            "ownerExecutionId": metadata.owner_execution_id,
            "createdAt": metadata.created_at.isoformat(timespec="microseconds"),
            "sourceDev": metadata.source_dev,
            "sourceIno": metadata.source_ino,
            "sourceSize": metadata.source_size,
            "sourceMtimeNs": metadata.source_mtime_ns,
            "outcome": metadata.outcome,
            "attemptCount": metadata.attempt_count,
            "nextAttemptAt": (
                metadata.next_attempt_at.isoformat(timespec="microseconds")
                if metadata.next_attempt_at is not None
                else None
            ),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _MAX_QUARANTINE_METADATA_BYTES:
        raise ValueError("quarantine metadata exceeds its byte limit")
    if _get_descriptor_xattr(descriptor, _QUARANTINE_XATTR) is None:
        _set_descriptor_xattr(descriptor, _QUARANTINE_XATTR, payload)
    else:
        _put_descriptor_xattr(descriptor, _QUARANTINE_XATTR, payload)


def _persist_quarantine_outcome(
    descriptor: int, metadata: _QuarantineMetadata, outcome: str
) -> None:
    _write_quarantine_metadata(
        descriptor,
        _QuarantineMetadata(
            quarantine_id=metadata.quarantine_id,
            object_key=metadata.object_key,
            original_name=metadata.original_name,
            expected_sha256=metadata.expected_sha256,
            owner_execution_id=metadata.owner_execution_id,
            created_at=metadata.created_at,
            source_dev=metadata.source_dev,
            source_ino=metadata.source_ino,
            source_size=metadata.source_size,
            source_mtime_ns=metadata.source_mtime_ns,
            outcome=outcome,
            attempt_count=min(metadata.attempt_count + 1, 1_000_000),
            next_attempt_at=metadata.next_attempt_at,
        ),
    )
    _fsync_descriptor(descriptor)


def _index_legacy_quarantine(
    root_descriptor: int,
    descriptor: int,
    quarantine_id: str,
    metadata: _QuarantineMetadata | None,
    stats: StorageSweepStats,
    budget: StorageSweepBudget,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    _require_legacy_migration_time(deadline, monotonic)
    # A complete V2 source snapshot is written only by the registered path,
    # before the canonical name is moved into quarantine.
    if metadata is not None and metadata.source_identity is not None:
        return
    if _get_descriptor_xattr(
        descriptor, _LEGACY_QUARANTINE_STATE_XATTR, max_bytes=32
    ) is not None:
        return
    if stats.mutations + 16 > budget.max_mutations:
        stats.budget_exhausted = True
        if deadline is not None:
            raise _LegacyMigrationLimitExceeded(
                "legacy quarantine migration mutation limit exceeded"
            )
        return
    _require_legacy_migration_time(deadline, monotonic)
    if metadata is None:
        _queue_enqueue(
            root_descriptor,
            _QUEUE_ROOT,
            "held",
            {
                "version": 2,
                "recordId": quarantine_id,
                "kind": "legacyHeld",
            },
        )
        _require_legacy_migration_time(deadline, monotonic)
        _set_descriptor_xattr(
            descriptor, _LEGACY_QUARANTINE_STATE_XATTR, b"held"
        )
        _fsync_descriptor(descriptor)
        stats.invalid_quarantines += 1
    else:
        lane = "ready" if metadata.source_identity is not None else "held"
        outcome = "deferred" if lane == "ready" else "held"
        _queue_enqueue(
            root_descriptor,
            _QUEUE_ROOT,
            lane,
            {
                "version": 2,
                "recordId": quarantine_id,
                "kind": "quarantine",
                "objectKey": metadata.object_key,
                "quarantineId": quarantine_id,
                "createdAt": metadata.created_at.isoformat(
                    timespec="microseconds"
                ),
            },
        )
        _require_legacy_migration_time(deadline, monotonic)
        _persist_quarantine_outcome(descriptor, metadata, outcome)
        _require_legacy_migration_time(deadline, monotonic)
        _set_descriptor_xattr(
            descriptor, _LEGACY_QUARANTINE_STATE_XATTR, lane.encode("ascii")
        )
        _fsync_descriptor(descriptor)
        if lane == "held":
            stats.invalid_quarantines += 1
    stats.mutations += 16
    stats.progress_made = True


def _migrate_nested_legacy_quarantines(
    root_descriptor: int,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    migration_state = _get_descriptor_xattr(
        root_descriptor, _LEGACY_MIGRATION_STATE_XATTR, max_bytes=32
    )
    if migration_state == _LEGACY_MIGRATION_COMPLETE:
        return
    if migration_state == _LEGACY_MIGRATION_HELD_LIMIT:
        raise RuntimeError("legacy quarantine migration is held at its scan limit")
    if migration_state is not None:
        raise RuntimeError("legacy quarantine migration state is invalid")
    try:
        artifacts = os.open(
            "artifacts",
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
    except FileNotFoundError:
        _set_descriptor_xattr(
            root_descriptor,
            _LEGACY_MIGRATION_STATE_XATTR,
            _LEGACY_MIGRATION_COMPLETE,
        )
        _fsync_descriptor(root_descriptor)
        return
    deadline = monotonic() + 60.0
    stats = StorageSweepStats()
    budget = StorageSweepBudget(
        max_quarantines=_QUEUE_MAX_RECORDS,
        max_bytes=_QUEUE_MAX_RECORD_BYTES,
        max_seconds=60.0,
        max_directories=_LEGACY_MIGRATION_MAX_DIRECTORIES,
        max_entries=_LEGACY_MIGRATION_MAX_ENTRIES,
        max_mutations=_QUEUE_MAX_RECORDS * 16,
    )
    directories = 0
    entries = 0

    def scan(parent: int, depth: int) -> None:
        nonlocal directories, entries
        _require_legacy_migration_time(deadline, monotonic)
        directories += 1
        if directories > _LEGACY_MIGRATION_MAX_DIRECTORIES:
            raise _LegacyMigrationLimitExceeded(
                "legacy quarantine migration directory limit exceeded"
            )
        with os.scandir(parent) as children:
            for child in children:
                _require_legacy_migration_time(deadline, monotonic)
                entries += 1
                if entries > _LEGACY_MIGRATION_MAX_ENTRIES:
                    raise _LegacyMigrationLimitExceeded(
                        "legacy quarantine migration entry limit exceeded"
                    )
                match = _QUARANTINE.fullmatch(child.name)
                if match is not None:
                    if not child.is_dir(follow_symlinks=False):
                        raise RuntimeError("legacy quarantine entry is invalid")
                    descriptor = os.open(
                        child.name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent,
                    )
                    try:
                        try:
                            metadata = _read_quarantine_metadata(
                                descriptor, match.group("id")
                            )
                        except (OSError, RuntimeError, ValueError):
                            metadata = None
                        _index_legacy_quarantine(
                            root_descriptor,
                            descriptor,
                            match.group("id"),
                            metadata,
                            stats,
                            budget,
                            deadline=deadline,
                            monotonic=monotonic,
                        )
                    finally:
                        os.close(descriptor)
                    continue
                if depth >= 2 or not child.is_dir(follow_symlinks=False):
                    continue
                try:
                    UUID(child.name)
                except ValueError:
                    continue
                descriptor = os.open(
                    child.name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
                try:
                    scan(descriptor, depth + 1)
                finally:
                    os.close(descriptor)

    try:
        try:
            scan(artifacts, 0)
        finally:
            os.close(artifacts)
        _require_legacy_migration_time(deadline, monotonic)
    except _LegacyMigrationLimitExceeded:
        _set_descriptor_xattr(
            root_descriptor,
            _LEGACY_MIGRATION_STATE_XATTR,
            _LEGACY_MIGRATION_HELD_LIMIT,
        )
        _fsync_descriptor(root_descriptor)
        raise
    _set_descriptor_xattr(
        root_descriptor,
        _LEGACY_MIGRATION_STATE_XATTR,
        _LEGACY_MIGRATION_COMPLETE,
    )
    _fsync_descriptor(root_descriptor)


def _require_legacy_migration_time(
    deadline: float | None, monotonic: Callable[[], float]
) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise _LegacyMigrationLimitExceeded(
            "legacy quarantine migration time limit exceeded"
        )


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
    legacy_keys = {
        "schemaVersion",
        "quarantineId",
        "objectKey",
        "originalName",
        "expectedSha256",
        "ownerExecutionId",
        "createdAt",
    }
    current_keys = legacy_keys | {
        "sourceDev",
        "sourceIno",
        "sourceSize",
        "sourceMtimeNs",
        "outcome",
        "attemptCount",
        "nextAttemptAt",
    }
    if not isinstance(parsed, dict) or (
        set(parsed) != legacy_keys and set(parsed) != current_keys
    ):
        raise ValueError("quarantine metadata shape is invalid")
    schema_version = parsed["schemaVersion"]
    if schema_version not in {1, 2} or parsed["quarantineId"] != expected_id:
        raise ValueError("quarantine metadata identity is invalid")
    if schema_version == 1 and set(parsed) != legacy_keys:
        raise ValueError("quarantine metadata shape is invalid")
    if schema_version == 2 and set(parsed) != current_keys:
        raise ValueError("quarantine metadata shape is invalid")
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
    source_values = (
        parsed.get("sourceDev"),
        parsed.get("sourceIno"),
        parsed.get("sourceSize"),
        parsed.get("sourceMtimeNs"),
    )
    if any(value is not None for value in source_values) and not all(
        isinstance(value, int) and value >= 0 for value in source_values
    ):
        raise ValueError("quarantine source snapshot is invalid")
    outcome = parsed.get("outcome")
    if outcome not in {None, "conflict", "invalid", "held", "deferred"}:
        raise ValueError("quarantine outcome is invalid")
    attempt_count = parsed.get("attemptCount", 0)
    if not isinstance(attempt_count, int) or not 0 <= attempt_count <= 1_000_000:
        raise ValueError("quarantine attempt count is invalid")
    next_attempt = parsed.get("nextAttemptAt")
    if next_attempt is not None and not isinstance(next_attempt, str):
        raise ValueError("quarantine next attempt is invalid")
    try:
        next_attempt_at = (
            datetime.fromisoformat(next_attempt) if next_attempt is not None else None
        )
    except ValueError as error:
        raise ValueError("quarantine next attempt is invalid") from error
    return _QuarantineMetadata(
        quarantine_id=expected_id,
        object_key=object_key,
        original_name=original_name,
        expected_sha256=expected_sha256,
        owner_execution_id=owner,
        created_at=created_at,
        source_dev=source_values[0],
        source_ino=source_values[1],
        source_size=source_values[2],
        source_mtime_ns=source_values[3],
        outcome=outcome,
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at,
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


def _retired_workspace_name(name: str) -> str:
    match = _GENERATED_WORKSPACE.fullmatch(name)
    if match is None:
        raise RuntimeError("workspace name is not generated")
    return f".wf-retired-v1-{match.group(1)}"


def _workspace_status(
    root_descriptor: int,
    name: str,
    expected_identity: tuple[int, int] | None,
) -> os.stat_result | None:
    retired = _retired_workspace_name(name)
    for candidate in (retired, name):
        try:
            status = os.stat(
                candidate, dir_fd=root_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or _is_reparse(status)
        ):
            raise RuntimeError("workspace identity changed")
        if expected_identity is None or _identity(status) == expected_identity:
            return status
        if candidate == retired:
            raise RuntimeError("workspace retired namespace is occupied")
    return None


def _workspace_record_name_matches(name: str, execution_id: str) -> bool:
    try:
        canonical = str(UUID(execution_id))
    except ValueError:
        return False
    return name == f"wf-execution-{canonical}"


def _bounded_cleanup_workspace(
    root_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
    stats: WorkspaceSweepStats,
    budget: StorageSweepBudget,
    deadline: float,
    monotonic: Callable[[], float],
) -> bool:
    retired = _retired_workspace_name(name)
    try:
        retired_status = os.stat(
            retired, dir_fd=root_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        try:
            current = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if _identity(current) != expected_identity or not stat.S_ISDIR(
            current.st_mode
        ):
            raise RuntimeError("workspace identity changed")
        if not _workspace_mutation_available(stats, budget, deadline, monotonic, 2):
            return False
        _rename_no_replace(root_descriptor, name, root_descriptor, retired)
        stats.mutations += 1
        _fsync_descriptor(root_descriptor)
        stats.mutations += 1
        stats.progress_made = True
        retired_status = os.stat(
            retired, dir_fd=root_descriptor, follow_symlinks=False
        )
    if _identity(retired_status) != expected_identity:
        raise RuntimeError("workspace identity changed")
    workspace = os.open(
        retired,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_descriptor,
    )
    try:
        if _identity(os.fstat(workspace)) != expected_identity:
            raise RuntimeError("workspace identity changed")
        complete = _bounded_clear_workspace(
            workspace, stats, budget, deadline, monotonic
        )
    finally:
        os.close(workspace)
    if not complete:
        return False
    if not _workspace_mutation_available(stats, budget, deadline, monotonic, 2):
        return False
    current = os.stat(retired, dir_fd=root_descriptor, follow_symlinks=False)
    if _identity(current) != expected_identity:
        raise RuntimeError("workspace identity changed")
    os.rmdir(retired, dir_fd=root_descriptor)
    stats.mutations += 1
    _fsync_descriptor(root_descriptor)
    stats.mutations += 1
    stats.progress_made = True
    return True


def _workspace_mutation_available(
    stats: WorkspaceSweepStats,
    budget: StorageSweepBudget,
    deadline: float,
    monotonic: Callable[[], float],
    needed: int,
) -> bool:
    return (
        monotonic() < deadline
        and stats.mutations + needed <= budget.max_mutations
    )


def _reserved_workspace_budget(
    budget: StorageSweepBudget, *, reserve: int
) -> StorageSweepBudget:
    return StorageSweepBudget(
        max_quarantines=budget.max_quarantines,
        max_bytes=budget.max_bytes,
        max_seconds=budget.max_seconds,
        max_directories=budget.max_directories,
        max_entries=budget.max_entries,
        max_mutations=max(1, budget.max_mutations - reserve),
    )


def _read_workspace_progress(descriptor: int) -> list[str]:
    payload = _get_descriptor_xattr(
        descriptor, _WORKSPACE_SWEEP_XATTR, max_bytes=_MAX_SWEEP_STATE_BYTES
    )
    if payload is None:
        return []
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("workspace cleanup state is invalid") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("stack"), list)
        or len(value["stack"]) > 256
        or not all(
            isinstance(part, str)
            and part not in {"", ".", ".."}
            and "/" not in part
            and "\\" not in part
            and len(part.encode("utf-8")) <= 255
            for part in value["stack"]
        )
    ):
        raise RuntimeError("workspace cleanup state is invalid")
    return value["stack"]


def _write_workspace_progress(descriptor: int, stack: list[str]) -> None:
    payload = json.dumps(
        {"version": 1, "stack": stack},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_SWEEP_STATE_BYTES:
        raise RuntimeError("workspace cleanup state exceeds its byte limit")
    _put_descriptor_xattr(descriptor, _WORKSPACE_SWEEP_XATTR, payload)
    _fsync_descriptor(descriptor)


def _open_workspace_stack(root: int, stack: list[str]) -> int:
    descriptor = os.dup(root)
    try:
        for part in stack:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _bounded_clear_workspace(
    workspace: int,
    stats: WorkspaceSweepStats,
    budget: StorageSweepBudget,
    deadline: float,
    monotonic: Callable[[], float],
) -> bool:
    stack = _read_workspace_progress(workspace)
    while True:
        if (
            monotonic() >= deadline
            or stats.entries_scanned >= budget.max_entries
            or stats.directories_scanned >= budget.max_directories
        ):
            return False
        current = _open_workspace_stack(workspace, stack)
        stats.directories_scanned += 1
        try:
            with os.scandir(current) as entries:
                entry = next(entries, None)
            if entry is None:
                if not stack:
                    return True
                if not _workspace_mutation_available(
                    stats, budget, deadline, monotonic, 4
                ):
                    return False
                child_name = stack[-1]
                os.close(current)
                current = -1
                parent = _open_workspace_stack(workspace, stack[:-1])
                try:
                    os.rmdir(child_name, dir_fd=parent)
                    stats.mutations += 1
                    _fsync_descriptor(parent)
                    stats.mutations += 1
                finally:
                    os.close(parent)
                stack.pop()
                _write_workspace_progress(workspace, stack)
                stats.mutations += 2
                stats.progress_made = True
                continue
            stats.entries_scanned += 1
            status = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode) and not entry.is_symlink():
                if not _workspace_mutation_available(
                    stats, budget, deadline, monotonic, 2
                ):
                    return False
                stack.append(entry.name)
                _write_workspace_progress(workspace, stack)
                stats.mutations += 2
                stats.progress_made = True
                continue
            if not _workspace_mutation_available(
                stats, budget, deadline, monotonic, 2
            ):
                return False
            os.unlink(entry.name, dir_fd=current)
            stats.mutations += 1
            _fsync_descriptor(current)
            stats.mutations += 1
            stats.progress_made = True
        finally:
            if current != -1:
                os.close(current)


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
