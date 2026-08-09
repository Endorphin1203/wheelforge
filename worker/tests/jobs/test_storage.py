from __future__ import annotations

import hashlib
import errno
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

import wheelforge_worker.jobs.storage as storage_module
from wheelforge_worker.jobs.storage import (
    InvalidObjectKey,
    RootedLocalStorage,
    StorageSweepBudget,
    WorkspaceManager,
)


@pytest.fixture
def storage(tmp_path: Path) -> RootedLocalStorage:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    return RootedLocalStorage(root, uuid_factory=lambda: UUID(int=1))


@pytest.mark.parametrize(
    "key",
    [
        "",
        ".",
        "../escape",
        "a/../escape",
        "/absolute",
        "a//b",
        "a/./b",
        "a/",
        "a\\b",
        "C:/x",
        "folder/C:/x",
        "a\x00b",
    ],
)
def test_object_keys_reject_non_relative_posix_paths(
    storage: RootedLocalStorage, key: str
) -> None:
    with pytest.raises(InvalidObjectKey):
        storage.read_bytes(key)


def test_read_rejects_symlink_traversal(tmp_path: Path) -> None:
    root = tmp_path / "data"
    outside = tmp_path / "outside"
    root.mkdir(mode=0o700)
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    storage = RootedLocalStorage(root)

    with pytest.raises(OSError):
        storage.read_bytes("linked/secret.txt")


def test_publish_is_no_replace_and_returns_verified_digest(
    storage: RootedLocalStorage,
) -> None:
    content = b"numpy==1.26.4\n"

    published = storage.publish_bytes("normalized/a.txt", content)

    assert published.size_bytes == len(content)
    assert published.sha256 == hashlib.sha256(content).hexdigest()
    assert storage.read_bytes("normalized/a.txt") == content
    with pytest.raises(FileExistsError):
        storage.publish_bytes("normalized/a.txt", b"different")
    assert storage.read_bytes("normalized/a.txt") == content


def test_publish_or_reuse_accepts_only_exact_existing_regular_file(
    storage: RootedLocalStorage,
) -> None:
    content = b"numpy==1.26.4\n"
    first = storage.publish_bytes("normalized/recover.txt", content)

    recovered = storage.publish_or_reuse_bytes("normalized/recover.txt", content)

    assert recovered == first
    with pytest.raises(FileExistsError):
        storage.publish_or_reuse_bytes(
            "normalized/recover.txt", b"numpy==2.0.0\n"
        )


def test_repeated_ordinary_reuse_does_not_leave_held_publication_records(
    storage: RootedLocalStorage,
) -> None:
    content = b"numpy==1.26.4\n"
    storage.publish_bytes("normalized/repeated.txt", content)

    for _ in range(6):
        storage.publish_or_reuse_bytes("normalized/repeated.txt", content)

    counts = storage_module._queue_counts(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    assert counts["ready"] + counts["deferred"] + counts["held"] == 0


def test_compensation_deletes_only_the_exact_owned_content(
    storage: RootedLocalStorage,
) -> None:
    first = storage.publish_bytes("artifacts/a.zip", b"first")

    assert storage.delete_if_owned("artifacts/a.zip", "0" * 64) is False
    assert storage.read_bytes("artifacts/a.zip") == b"first"
    assert storage.delete_if_owned("artifacts/a.zip", first.sha256) is True
    with pytest.raises(FileNotFoundError):
        storage.read_bytes("artifacts/a.zip")


def test_compensation_quarantines_name_before_deciding_which_inode_to_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    key = "artifacts/race.zip"
    published = storage.publish_bytes(key, b"owned")
    require_identity = storage_module._require_named_identity
    retained_name = "retained-owned.zip"
    replaced = False

    def replace_after_identity_check(
        parent: object, name: str, expected: tuple[int, int]
    ) -> None:
        nonlocal replaced
        require_identity(parent, name, expected)  # type: ignore[arg-type]
        if replaced:
            return
        replaced = True
        descriptor = parent.descriptor  # type: ignore[attr-defined]
        assert descriptor is not None
        os.rename(
            name,
            retained_name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
        )
        replacement = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=descriptor,
        )
        try:
            os.write(replacement, b"replacement")
            os.fsync(replacement)
        finally:
            os.close(replacement)

    monkeypatch.setattr(
        storage_module, "_require_named_identity", replace_after_identity_check
    )

    assert storage.delete_if_owned(key, published.sha256) is False

    assert not (root / "artifacts/race.zip").exists()
    assert (root / f"artifacts/{retained_name}").read_bytes() == b"owned"
    quarantine = next((root / "artifacts").glob(".wf-quarantine-*"))
    assert (quarantine / ".wf-recovery-candidate").read_bytes() == b"replacement"


def test_compensation_preserves_unverified_quarantine_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    key = "artifacts/race.zip"
    published = storage.publish_bytes(key, b"owned")
    require_identity = storage_module._require_named_identity
    retained_name = "retained-owned.zip"
    replaced = False

    def replace_after_identity_check(
        parent: object, name: str, expected: tuple[int, int]
    ) -> None:
        nonlocal replaced
        require_identity(parent, name, expected)  # type: ignore[arg-type]
        if replaced:
            return
        replaced = True
        descriptor = parent.descriptor  # type: ignore[attr-defined]
        assert descriptor is not None
        os.rename(
            name,
            retained_name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
        )
        replacement = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=descriptor,
        )
        try:
            os.write(replacement, b"replacement")
        finally:
            os.close(replacement)

    monkeypatch.setattr(
        storage_module, "_require_named_identity", replace_after_identity_check
    )
    assert storage.delete_if_owned(key, published.sha256) is False

    assert not (root / "artifacts/race.zip").exists()
    assert (root / f"artifacts/{retained_name}").read_bytes() == b"owned"
    quarantines = list((root / "artifacts").glob(".wf-quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / ".wf-recovery-candidate").read_bytes() == b"replacement"


def test_quarantine_isolation_never_replaces_an_occupied_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    published = storage.publish_bytes("artifacts/race.zip", b"owned")
    rename_no_replace = storage_module._rename_no_replace
    injected = False

    def occupy_destination(
        source_parent: int, source: str, destination_parent: int, destination: str
    ) -> None:
        nonlocal injected
        if not injected and source == destination == "race.zip":
            injected = True
            occupant = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_parent,
            )
            try:
                os.write(occupant, b"occupant")
            finally:
                os.close(occupant)
        rename_no_replace(source_parent, source, destination_parent, destination)

    monkeypatch.setattr(storage_module, "_rename_no_replace", occupy_destination)

    with pytest.raises(FileExistsError):
        storage.delete_if_owned(published.object_key, published.sha256)

    assert (root / published.object_key).read_bytes() == b"owned"
    quarantine = next((root / "artifacts").glob(".wf-quarantine-v1-*"))
    assert (quarantine / "race.zip").read_bytes() == b"occupant"


def test_storage_fails_closed_when_atomic_no_replace_primitive_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(
        storage_module,
        "_atomic_rename_configuration",
        lambda: ("missing_atomic_rename_symbol", 1),
    )

    with pytest.raises(RuntimeError, match="no-replace rename"):
        RootedLocalStorage(root)

    assert list(root.iterdir()) == []


@pytest.mark.parametrize("number", [errno.ENOSYS, errno.EINVAL, errno.ENOTSUP])
def test_storage_probe_rejects_filesystem_without_working_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)

    def unsupported(*_args: object) -> None:
        raise OSError(number, "unsupported")

    monkeypatch.setattr(storage_module, "_rename_no_replace", unsupported)
    with pytest.raises(RuntimeError, match="capability probe"):
        RootedLocalStorage(root)
    assert not list(root.iterdir())


def test_storage_probe_rejects_missing_descriptor_xattrs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(
        storage_module,
        "_set_descriptor_xattr",
        lambda *_args: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "unsupported")),
    )
    with pytest.raises(RuntimeError, match="capability probe"):
        RootedLocalStorage(root)
    assert not list(root.iterdir())


def test_storage_probe_exercises_real_lock_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    real_check = storage_module._require_lock_contention
    calls = 0

    def checked(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        real_check(descriptor)

    monkeypatch.setattr(storage_module, "_require_lock_contention", checked)
    RootedLocalStorage(root).close()
    assert calls == 1


def test_storage_probe_cleanup_failure_fails_startup_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    real_rmdir = storage_module.os.rmdir
    real_probe = storage_module._probe_root_capabilities

    def fail_probe_cleanup(name: str, *, dir_fd: int | None = None) -> None:
        if str(name).startswith(".wf-capability-probe-"):
            raise OSError("probe cleanup failed")
        real_rmdir(name, dir_fd=dir_fd)

    def probe(descriptor: int) -> None:
        monkeypatch.setattr(storage_module.os, "rmdir", fail_probe_cleanup)
        real_probe(descriptor)

    monkeypatch.setattr(storage_module, "_probe_root_capabilities", probe)
    with pytest.raises(RuntimeError, match="capability probe"):
        RootedLocalStorage(root)


def test_quarantine_isolation_handles_source_disappearance_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    published = storage.publish_bytes("artifacts/race.zip", b"owned")
    rename_no_replace = storage_module._rename_no_replace
    moved = False

    def move_source_first(
        source_parent: int, source: str, destination_parent: int, destination: str
    ) -> None:
        nonlocal moved
        if not moved and source == "race.zip":
            moved = True
            os.rename(
                source,
                "retained-owned.zip",
                src_dir_fd=source_parent,
                dst_dir_fd=source_parent,
            )
        rename_no_replace(source_parent, source, destination_parent, destination)

    monkeypatch.setattr(storage_module, "_rename_no_replace", move_source_first)

    assert storage.delete_if_owned(published.object_key, published.sha256) is False
    assert (root / "artifacts/retained-owned.zip").read_bytes() == b"owned"
    assert not list((root / "artifacts").glob(".wf-quarantine-v1-*"))


def test_quarantine_verification_preserves_replacement_installed_after_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    published = storage.publish_bytes("artifacts/race.zip", b"owned")
    rename_no_replace = storage_module._rename_no_replace
    replaced = False

    def replace_isolated_source(
        source_parent: int, source: str, destination_parent: int, destination: str
    ) -> None:
        nonlocal replaced
        rename_no_replace(source_parent, source, destination_parent, destination)
        if replaced or source != destination or source != "race.zip":
            return
        replaced = True
        os.rename(
            destination,
            "retained-owned.zip",
            src_dir_fd=destination_parent,
            dst_dir_fd=source_parent,
        )
        replacement = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=destination_parent,
        )
        try:
            os.write(replacement, b"replacement")
        finally:
            os.close(replacement)

    monkeypatch.setattr(
        storage_module, "_rename_no_replace", replace_isolated_source
    )

    assert storage.delete_if_owned(published.object_key, published.sha256) is False
    assert not (root / "artifacts/race.zip").exists()
    assert (root / "artifacts/retained-owned.zip").read_bytes() == b"owned"


def test_quarantine_mutations_are_serialized_across_storage_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    first = RootedLocalStorage(root)
    second = RootedLocalStorage(root)
    published = first.publish_bytes("artifacts/race.zip", b"owned")
    entered = threading.Event()
    release = threading.Event()
    real_quarantine = storage_module._quarantine_named

    def pause_after_isolation(*args: object, **kwargs: object) -> object:
        result = real_quarantine(*args, **kwargs)
        entered.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(storage_module, "_quarantine_named", pause_after_isolation)
    deletion = threading.Thread(
        target=first.delete_if_owned,
        args=(published.object_key, published.sha256),
    )
    swept = threading.Event()

    def sweep() -> None:
        second.sweep_abandoned(
            datetime(2026, 8, 2),
            frozenset(),
            active_execution_ids=frozenset(),
            active_build_executions=frozenset(),
        )
        swept.set()

    deletion.start()
    assert entered.wait(2)
    maintenance = threading.Thread(target=sweep)
    maintenance.start()
    assert not swept.wait(0.1)
    release.set()
    deletion.join(2)
    maintenance.join(2)
    assert swept.is_set()


def test_quarantine_mutations_are_serialized_between_threads_of_one_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    published = storage.publish_bytes("artifacts/threaded.zip", b"owned")
    entered = threading.Event()
    release = threading.Event()
    swept = threading.Event()
    real_quarantine = storage_module._quarantine_named

    def pause(*args: object, **kwargs: object) -> object:
        result = real_quarantine(*args, **kwargs)
        entered.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(storage_module, "_quarantine_named", pause)
    deletion = threading.Thread(
        target=storage.delete_if_owned,
        args=(published.object_key, published.sha256),
    )
    deletion.start()
    assert entered.wait(2)
    maintenance = threading.Thread(
        target=lambda: (
            storage.sweep_abandoned(
                datetime(2026, 8, 2), frozenset(),
                active_execution_ids=frozenset(),
                active_build_executions=frozenset(),
            ),
            swept.set(),
        )
    )
    maintenance.start()
    assert not swept.wait(0.1)
    release.set()
    deletion.join(2)
    maintenance.join(2)
    assert swept.is_set()


def test_maintenance_lock_wait_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    owner = RootedLocalStorage(root)
    waiter = RootedLocalStorage(root)
    published = owner.publish_bytes("artifacts/locked.zip", b"owned")
    entered = threading.Event()
    release = threading.Event()
    real_quarantine = storage_module._quarantine_named

    def pause(*args: object, **kwargs: object) -> object:
        result = real_quarantine(*args, **kwargs)
        entered.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(storage_module, "_quarantine_named", pause)
    deletion = threading.Thread(
        target=owner.delete_if_owned,
        args=(published.object_key, published.sha256),
    )
    deletion.start()
    assert entered.wait(2)
    started = time.monotonic()
    stats = waiter.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_seconds=0.05),
    )
    elapsed = time.monotonic() - started
    release.set()
    deletion.join(2)
    assert stats.budget_exhausted
    assert elapsed < 0.5


def test_lock_wait_and_scan_share_one_absolute_deadline(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    moments = iter((0.0, 0.0, 2.0))
    storage = RootedLocalStorage(root, monotonic=lambda: next(moments, 2.0))
    storage.publish_bytes("objects/deadline.whl", b"pending")

    result = storage.sweep_abandoned(
        datetime.max,
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_seconds=1),
    )

    assert result.lock_contended
    assert result.budget_exhausted
    assert result.entries_scanned == 0
    assert result.mutations == 0


def test_recovery_isolates_source_before_digest_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    clock = [datetime(2026, 8, 1)]
    storage = RootedLocalStorage(root, clock=lambda: clock[0])
    published = storage.publish_bytes("artifacts/race.zip", b"owned")
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated crash"):
        storage.delete_if_owned(published.object_key, published.sha256)
    monkeypatch.undo()
    real_rename = storage_module._rename_no_replace
    isolated = False

    def replace_before_candidate_isolation(
        source_parent: int, source: str, destination_parent: int, destination: str
    ) -> None:
        nonlocal isolated
        if destination == ".wf-recovery-candidate" and not isolated:
            isolated = True
            os.rename(
                source,
                "retained-owned.zip",
                src_dir_fd=source_parent,
                dst_dir_fd=source_parent,
            )
            replacement = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=source_parent,
            )
            os.write(replacement, b"replacement")
            os.close(replacement)
        real_rename(source_parent, source, destination_parent, destination)

    monkeypatch.setattr(
        storage_module, "_rename_no_replace", replace_before_candidate_isolation
    )
    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert isolated
    assert stats.quarantines_restored == 0
    assert stats.quarantine_conflicts == 1
    assert not (root / published.object_key).exists()
    quarantine = next(root.rglob(".wf-quarantine-v1-*"))
    assert (quarantine / "retained-owned.zip").read_bytes() == b"owned"
    assert (quarantine / ".wf-recovery-candidate").read_bytes() == b"replacement"


def _leave_quarantine_after_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine_named = storage_module._quarantine_named

    def crash(*args: object, **kwargs: object) -> object:
        quarantine = quarantine_named(*args, **kwargs)
        assert quarantine is not None
        os.close(quarantine.descriptor)
        raise RuntimeError("simulated crash after quarantine move")

    monkeypatch.setattr(storage_module, "_quarantine_named", crash)


@pytest.mark.parametrize("protection", ["active", "referenced"])
def test_quarantine_maintenance_protects_live_owner_then_restores_when_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    task = "20000000-0000-4000-8000-000000000071"
    execution = "10000000-0000-4000-8000-000000000071"
    artifact = "30000000-0000-4000-8000-000000000071"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"
    clock = [datetime(2026, 8, 1)]
    storage = RootedLocalStorage(root, clock=lambda: clock[0])
    published = storage.publish_bytes(key, b"owned")
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated crash"):
        storage.delete_if_owned(key, published.sha256)
    monkeypatch.undo()
    quarantine = next(root.rglob(".wf-quarantine-v1-*"))
    descriptor = os.open(
        quarantine, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        metadata = storage_module._read_quarantine_metadata(
            descriptor, quarantine.name.removeprefix(".wf-quarantine-v1-")
        )
    finally:
        os.close(descriptor)
    assert metadata is not None
    assert metadata.object_key == key
    assert metadata.original_name == f"{artifact}.zip"
    assert metadata.expected_sha256 == published.sha256
    assert metadata.owner_execution_id == execution
    assert metadata.created_at == datetime(2026, 8, 1)

    active = frozenset({execution}) if protection == "active" else frozenset()
    referenced = frozenset({key}) if protection == "referenced" else frozenset()
    protected = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        referenced,
        active_execution_ids=active,
        active_build_executions=frozenset({(task, execution)}) if active else frozenset(),
    )

    assert protected.quarantines_protected == 1
    assert not (root / key).exists()
    assert len(list(root.rglob(".wf-quarantine-v1-*"))) == 1

    clock[0] += timedelta(minutes=6)
    recovered = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert recovered.quarantines_restored == 1
    assert (root / key).read_bytes() == b"owned"
    assert not list(root.rglob(".wf-quarantine-v1-*"))


def test_expired_quarantine_restore_conflict_preserves_both_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    task = "20000000-0000-4000-8000-000000000072"
    execution = "10000000-0000-4000-8000-000000000072"
    artifact = "30000000-0000-4000-8000-000000000072"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    published = storage.publish_bytes(key, b"owned")
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated crash"):
        storage.delete_if_owned(key, published.sha256)
    monkeypatch.undo()
    storage.publish_bytes(key, b"replacement")

    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert stats.quarantine_conflicts == 1
    assert (root / key).read_bytes() == b"replacement"
    quarantine = next(root.rglob(".wf-quarantine-v1-*"))
    assert (quarantine / ".wf-recovery-candidate").read_bytes() == b"owned"


def test_maintenance_removes_empty_quarantine_left_after_owned_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    published = storage.publish_bytes("artifacts/race.zip", b"owned")
    real_unlink = storage_module.os.unlink
    failed = False

    def unlink_then_crash(name: str, *, dir_fd: int | None = None) -> None:
        nonlocal failed
        real_unlink(name, dir_fd=dir_fd)
        if name == ".wf-recovery-candidate" and not failed:
            failed = True
            raise OSError("simulated crash after owned delete")

    monkeypatch.setattr(storage_module.os, "unlink", unlink_then_crash)
    with pytest.raises(OSError, match="simulated crash"):
        storage.delete_if_owned(published.object_key, published.sha256)
    monkeypatch.undo()

    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert stats.empty_quarantines_removed == 1
    assert not list(root.rglob(".wf-quarantine-v1-*"))


def test_maintenance_prunes_artifact_directories_after_empty_quarantine_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    task = "20000000-0000-4000-8000-000000000073"
    execution = "10000000-0000-4000-8000-000000000073"
    artifact = "30000000-0000-4000-8000-000000000073"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    published = storage.publish_bytes(key, b"owned")
    real_unlink = storage_module.os.unlink
    failed = False

    def unlink_then_crash(name: str, *, dir_fd: int | None = None) -> None:
        nonlocal failed
        real_unlink(name, dir_fd=dir_fd)
        if name == ".wf-recovery-candidate" and not failed:
            failed = True
            raise OSError("simulated crash after owned delete")

    monkeypatch.setattr(storage_module.os, "unlink", unlink_then_crash)
    with pytest.raises(OSError, match="simulated crash"):
        storage.delete_if_owned(key, published.sha256)
    monkeypatch.undo()

    storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert not (root / f"artifacts/{task}/{execution}").exists()
    assert not (root / f"artifacts/{task}").exists()
    assert (root / "artifacts").is_dir()


def test_maintenance_removes_old_empty_quarantine_left_after_mkdir_crash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    quarantine = root / ".wf-quarantine-v1-00000000-0000-4000-8000-000000000074"
    quarantine.mkdir(mode=0o700)
    old = datetime(2026, 8, 1).timestamp()
    os.utime(quarantine, (old, old))

    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert stats.empty_quarantines_removed == 1
    assert not quarantine.exists()


def test_maintenance_removes_metadata_only_quarantine_without_moving_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    key = "artifacts/race.zip"
    source = storage.publish_bytes(key, b"owned")
    quarantine_id = "00000000-0000-4000-8000-000000000075"
    quarantine = root / "artifacts" / f".wf-quarantine-v1-{quarantine_id}"
    quarantine.mkdir(mode=0o700)
    descriptor = os.open(
        quarantine, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        storage_module._write_quarantine_metadata(
            descriptor,
            storage_module._QuarantineMetadata(
                quarantine_id,
                key,
                "race.zip",
                source.sha256,
                None,
                datetime(2026, 8, 1),
            ),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert stats.empty_quarantines_removed == 1
    assert (root / key).read_bytes() == b"owned"
    assert not quarantine.exists()


def test_crash_after_quarantine_verification_is_recovered_by_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    published = storage.publish_bytes("artifacts/race.zip", b"owned")
    real_unlink = storage_module.os.unlink

    def crash_before_delete(name: str, *, dir_fd: int | None = None) -> None:
        if name == ".wf-recovery-candidate":
            raise OSError("simulated crash after verification")
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(storage_module.os, "unlink", crash_before_delete)
    with pytest.raises(OSError, match="after verification"):
        storage.delete_if_owned(published.object_key, published.sha256)
    monkeypatch.undo()

    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert stats.quarantines_restored == 1
    assert (root / published.object_key).read_bytes() == b"owned"


def test_crash_after_atomic_restore_leaves_empty_quarantine_for_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    published = storage.publish_bytes("artifacts/race.zip", b"owned")
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated crash"):
        storage.delete_if_owned(published.object_key, published.sha256)
    monkeypatch.undo()
    rename_no_replace = storage_module._rename_no_replace
    crashed = False

    def restore_then_crash(
        source_parent: int, source: str, destination_parent: int, destination: str
    ) -> None:
        nonlocal crashed
        rename_no_replace(source_parent, source, destination_parent, destination)
        if (
            not crashed
            and source == ".wf-recovery-candidate"
            and destination == "race.zip"
        ):
            crashed = True
            raise OSError("simulated crash after restore")

    monkeypatch.setattr(storage_module, "_rename_no_replace", restore_then_crash)
    with pytest.raises(OSError, match="after restore"):
        storage.sweep_abandoned(
            datetime(2026, 8, 2),
            frozenset(),
            active_execution_ids=frozenset(),
            active_build_executions=frozenset(),
        )
    monkeypatch.undo()

    assert (root / published.object_key).read_bytes() == b"owned"
    assert len(list(root.rglob(".wf-quarantine-v1-*"))) == 1
    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )
    assert stats.empty_quarantines_removed == 1
    assert not list(root.rglob(".wf-quarantine-v1-*"))


def test_quarantine_recovery_obeys_count_and_byte_budgets_and_rotates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    ticks = iter((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    storage = RootedLocalStorage(
        root,
        clock=lambda: datetime(2026, 8, 1),
        monotonic=lambda: next(ticks, 0.0),
    )
    real_quarantine = storage_module._quarantine_named

    def crash(*args: object, **kwargs: object) -> object:
        quarantine = real_quarantine(*args, **kwargs)
        assert quarantine is not None
        os.close(quarantine.descriptor)
        raise RuntimeError("crash")

    for index in range(3):
        published = storage.publish_bytes(f"artifacts/{index}.zip", b"1234")
        monkeypatch.setattr(storage_module, "_quarantine_named", crash)
        with pytest.raises(RuntimeError, match="crash"):
            storage.delete_if_owned(published.object_key, published.sha256)
        monkeypatch.setattr(storage_module, "_quarantine_named", real_quarantine)

    budget = StorageSweepBudget(max_quarantines=1, max_bytes=4, max_seconds=1)
    first = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=budget,
    )
    second = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=budget,
    )
    assert first.quarantines_examined == second.quarantines_examined == 1
    assert first.bytes_hashed <= 4 and second.bytes_hashed <= 4
    assert first.budget_exhausted and second.budget_exhausted
    assert len(list(root.rglob(".wf-quarantine-v1-*"))) == 1


def test_repeated_quarantine_conflict_uses_cached_fingerprint_without_rehash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    published = storage.publish_bytes("artifacts/conflict.zip", b"owned")
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated crash"):
        storage.delete_if_owned(published.object_key, published.sha256)
    monkeypatch.undo()
    storage.publish_bytes(published.object_key, b"replacement")
    real_hash = storage_module._hash_descriptor
    reads = 0

    def counted_hash(descriptor: int) -> str:
        nonlocal reads
        reads += 1
        return real_hash(descriptor)

    monkeypatch.setattr(storage_module, "_hash_descriptor", counted_hash)
    arguments = dict(
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )
    first = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(), **arguments
    )
    second = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(), **arguments
    )

    assert first.quarantine_conflicts == 1
    assert second.quarantine_conflicts == 0
    assert second.quarantines_held == 1
    assert reads == 0
    assert second.bytes_hashed == 0

    storage.close()
    restarted = RootedLocalStorage(root)

    def forbidden_hash(_descriptor: int) -> str:
        raise AssertionError("unchanged conflict was rehashed after restart")

    monkeypatch.setattr(storage_module, "_hash_descriptor", forbidden_hash)
    third = restarted.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(), **arguments
    )
    assert third.quarantine_conflicts == 0
    assert third.quarantines_held == 1
    assert third.bytes_hashed == 0


def test_storage_instance_reloads_sweep_state_after_acquiring_shared_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    first = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    published = first.publish_bytes("artifacts/conflict.zip", b"owned")
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated crash"):
        first.delete_if_owned(published.object_key, published.sha256)
    monkeypatch.undo()
    first.publish_bytes(published.object_key, b"replacement")

    # This instance starts before the first sweep persists its conflict cache.
    second = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    real_hash = storage_module._hash_descriptor
    reads = 0

    def counted_hash(descriptor: int) -> str:
        nonlocal reads
        reads += 1
        return real_hash(descriptor)

    monkeypatch.setattr(storage_module, "_hash_descriptor", counted_hash)
    arguments = dict(
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )
    first_result = first.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(), **arguments
    )
    second_result = second.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(), **arguments
    )

    assert first_result.quarantine_conflicts == 1
    assert second_result.quarantine_conflicts == 0
    assert second_result.quarantines_held == 1
    assert second_result.bytes_hashed == 0
    assert reads == 0


def test_oversized_quarantine_uses_persisted_snapshot_without_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    published = storage.publish_bytes("artifacts/large.zip", b"12345")
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError):
        storage.delete_if_owned(published.object_key, published.sha256)
    monkeypatch.undo()
    reads = 0
    real_hash = storage_module._hash_descriptor

    def counted(_descriptor: int) -> str:
        nonlocal reads
        reads += 1
        raise AssertionError("oversized payload was hashed")

    monkeypatch.setattr(storage_module, "_hash_descriptor", counted)
    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_bytes=4),
    )
    assert stats.oversized_quarantines == 1
    assert stats.quarantines_restored == 1
    assert not stats.budget_exhausted and reads == 0
    storage.close()
    restarted = RootedLocalStorage(root)
    monkeypatch.setattr(storage_module, "_hash_descriptor", real_hash)
    recovered = restarted.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_bytes=5),
    )
    assert recovered.quarantines_restored == 0
    assert restarted.read_bytes(published.object_key) == b"12345"


def test_oversized_quarantine_uses_snapshot_without_blocking_later_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    real_quarantine = storage_module._quarantine_named
    published = []

    def crash(*args: object, **kwargs: object) -> object:
        binding = real_quarantine(*args, **kwargs)
        assert binding is not None
        os.close(binding.descriptor)
        raise RuntimeError("crash")

    for key, content in (
        ("artifacts/a-large.zip", b"12345"),
        ("artifacts/z-small.zip", b"x"),
    ):
        item = storage.publish_bytes(key, content)
        published.append(item)
        monkeypatch.setattr(storage_module, "_quarantine_named", crash)
        with pytest.raises(RuntimeError, match="crash"):
            storage.delete_if_owned(item.object_key, item.sha256)
        monkeypatch.setattr(storage_module, "_quarantine_named", real_quarantine)

    reads = 0
    real_hash = storage_module._hash_descriptor

    def counted_hash(descriptor: int) -> str:
        nonlocal reads
        reads += 1
        return real_hash(descriptor)

    monkeypatch.setattr(storage_module, "_hash_descriptor", counted_hash)
    result = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_quarantines=4, max_bytes=4),
    )

    assert result.oversized_quarantines == 1
    assert result.quarantines_held == 0
    assert result.backlog_entries == 0
    assert storage.read_bytes(published[0].object_key) == b"12345"
    assert storage.read_bytes(published[1].object_key) == b"x"
    assert reads == 0


def test_quarantine_metadata_persists_verified_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    item = storage.publish_bytes("artifacts/snapshot.zip", b"verified")
    original = (root / item.object_key).stat()
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError):
        storage.delete_if_owned(item.object_key, item.sha256)
    quarantine = next(root.rglob(".wf-quarantine-v1-*"))
    descriptor = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY)
    try:
        metadata = storage_module._read_quarantine_metadata(
            descriptor, quarantine.name.removeprefix(".wf-quarantine-v1-")
        )
    finally:
        os.close(descriptor)
    assert metadata is not None
    assert metadata.source_identity == (original.st_dev, original.st_ino)
    assert metadata.source_size == original.st_size
    assert metadata.source_mtime_ns == original.st_mtime_ns


def test_data_sweep_never_uses_unbounded_fwalk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    storage.publish_bytes("artifacts/probe.zip", b"old")
    monkeypatch.setattr(
        storage_module.os,
        "fwalk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unbounded fwalk used")
        ),
    )
    result = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_entries=1),
    )
    assert result.entries_scanned <= 1


def _queue_record(record_id: str, *, kind: str = "stage") -> dict[str, object]:
    return {
        "version": 2,
        "recordId": record_id,
        "generation": 0,
        "kind": kind,
        "objectKey": f"objects/.wf-stage-{record_id}",
        "createdAt": "2026-08-01T00:00:00.000000",
    }


def test_queue_reconcile_adopts_slot_fsynced_before_state_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    real_write_state = storage_module._write_queue_state

    def crash_after_slot_fsync(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash before queue state")

    monkeypatch.setattr(storage_module, "_write_queue_state", crash_after_slot_fsync)
    with pytest.raises(OSError, match="crash before queue state"):
        storage_module._queue_enqueue(
            storage._root_descriptor_or_raise(),
            storage_module._QUEUE_ROOT,
            "ready",
            _queue_record("00000000-0000-4000-8000-000000000501"),
        )
    storage.close()
    monkeypatch.setattr(storage_module, "_write_queue_state", real_write_state)

    restarted = RootedLocalStorage(root)
    counts = storage_module._queue_counts(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    item = storage_module._queue_peek(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT, "ready"
    )

    assert counts["ready"] == 1
    assert item is not None
    assert item.record["recordId"] == "00000000-0000-4000-8000-000000000501"


def test_queue_crash_before_final_link_discards_only_deterministic_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    real_link = storage_module._segmented_queue.os.link

    def crash_before_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash before final slot link")

    monkeypatch.setattr(storage_module._segmented_queue.os, "link", crash_before_link)
    with pytest.raises(OSError, match="crash before final slot link"):
        storage_module._queue_enqueue(
            storage._root_descriptor_or_raise(),
            storage_module._QUEUE_ROOT,
            "ready",
            _queue_record("00000000-0000-4000-8000-000000000617"),
        )
    temp_entries = list((root / storage_module._QUEUE_ROOT / "ready").rglob("*.tmp"))
    assert [path.name for path in temp_entries] == ["000.json.tmp"]
    storage.close()
    monkeypatch.setattr(storage_module._segmented_queue.os, "link", real_link)

    restarted = RootedLocalStorage(root)
    counts = storage_module._queue_counts(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )

    assert counts["ready"] == 0
    assert not list((root / storage_module._QUEUE_ROOT).rglob("*.tmp"))


def test_queue_crash_after_final_link_adopts_final_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    real_unlink = storage_module._segmented_queue.os.unlink

    def crash_before_temp_unlink(
        name: object, *args: object, **kwargs: object
    ) -> None:
        if name == "000.json.tmp":
            raise OSError("crash after final slot link")
        real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(
        storage_module._segmented_queue.os, "unlink", crash_before_temp_unlink
    )
    with pytest.raises(OSError, match="crash after final slot link"):
        storage_module._queue_enqueue(
            storage._root_descriptor_or_raise(),
            storage_module._QUEUE_ROOT,
            "ready",
            _queue_record("00000000-0000-4000-8000-000000000618"),
        )
    storage.close()
    monkeypatch.setattr(storage_module._segmented_queue.os, "unlink", real_unlink)

    restarted = RootedLocalStorage(root)
    item = storage_module._queue_peek(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT, "ready"
    )

    assert item is not None
    assert item.record["recordId"] == "00000000-0000-4000-8000-000000000618"
    assert not list((root / storage_module._QUEUE_ROOT).rglob("*.tmp"))


def test_queue_reconcile_finishes_unlink_before_head_state_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    descriptor = storage._root_descriptor_or_raise()
    storage_module._queue_enqueue(
        descriptor,
        storage_module._QUEUE_ROOT,
        "ready",
        _queue_record("00000000-0000-4000-8000-000000000502"),
    )
    item = storage_module._queue_peek(
        descriptor, storage_module._QUEUE_ROOT, "ready"
    )
    assert item is not None
    real_write_state = storage_module._write_queue_state

    def crash_after_slot_unlink(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash before head state")

    monkeypatch.setattr(storage_module, "_write_queue_state", crash_after_slot_unlink)
    with pytest.raises(OSError, match="crash before head state"):
        storage_module._queue_complete(
            descriptor, storage_module._QUEUE_ROOT, item
        )
    storage.close()
    monkeypatch.setattr(storage_module, "_write_queue_state", real_write_state)

    restarted = RootedLocalStorage(root)

    assert storage_module._queue_counts(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )["ready"] == 0
    assert storage_module._queue_peek(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT, "ready"
    ) is None


def test_queue_reconcile_deduplicates_move_by_stable_record_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    descriptor = storage._root_descriptor_or_raise()
    record_id = "00000000-0000-4000-8000-000000000503"
    storage_module._queue_enqueue(
        descriptor,
        storage_module._QUEUE_ROOT,
        "ready",
        _queue_record(record_id),
    )
    item = storage_module._queue_peek(
        descriptor, storage_module._QUEUE_ROOT, "ready"
    )
    assert item is not None
    real_complete = storage_module._queue_complete

    def crash_before_source_complete(*_args: object, **_kwargs: object) -> None:
        raise OSError("crash after destination enqueue")

    monkeypatch.setattr(storage_module, "_queue_complete", crash_before_source_complete)
    with pytest.raises(OSError, match="crash after destination enqueue"):
        storage_module._queue_move(
            descriptor, storage_module._QUEUE_ROOT, item, "deferred"
        )
    with pytest.raises(OSError, match="crash after destination enqueue"):
        storage_module._queue_move(
            descriptor, storage_module._QUEUE_ROOT, item, "deferred"
        )
    before_restart = storage_module._queue_counts(
        descriptor, storage_module._QUEUE_ROOT
    )
    assert before_restart["ready"] == 1
    assert before_restart["deferred"] == 1
    storage.close()
    monkeypatch.setattr(storage_module, "_queue_complete", real_complete)

    restarted = RootedLocalStorage(root)
    counts = storage_module._queue_counts(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    deferred = storage_module._queue_peek(
        restarted._root_descriptor_or_raise(),
        storage_module._QUEUE_ROOT,
        "deferred",
    )

    assert counts["ready"] == 0
    assert counts["deferred"] == 1
    assert sum(counts[lane] for lane in ("ready", "deferred", "held")) == 1
    assert deferred is not None
    assert deferred.record["recordId"] == record_id
    assert deferred.record["generation"] == 1


def test_queue_quota_fails_closed_and_persists_capacity_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(storage_module, "_QUEUE_MAX_RECORDS", 2)
    storage = RootedLocalStorage(root)
    descriptor = storage._root_descriptor_or_raise()
    for suffix in ("504", "505"):
        storage_module._queue_enqueue(
            descriptor,
            storage_module._QUEUE_ROOT,
            "held",
            _queue_record(f"00000000-0000-4000-8000-000000000{suffix}"),
        )

    with pytest.raises(storage_module.QueueCapacityError):
        storage_module._queue_enqueue(
            descriptor,
            storage_module._QUEUE_ROOT,
            "ready",
            _queue_record("00000000-0000-4000-8000-000000000506"),
        )

    counts = storage_module._queue_counts(descriptor, storage_module._QUEUE_ROOT)
    assert counts["held"] == 2
    assert counts["ready"] == 0
    assert counts["capacityEvents"] == 1


def test_queue_startup_reconcile_removes_empty_segment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    queue = storage_module._open_queue(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    lane = storage_module._open_queue_lane(queue, "ready")
    try:
        os.mkdir("0000000000000007", 0o700, dir_fd=lane)
    finally:
        os.close(lane)
        os.close(queue)
        storage.close()

    RootedLocalStorage(root).close()

    assert not (
        root
        / storage_module._QUEUE_ROOT
        / "ready"
        / "0000000000000007"
    ).exists()


def test_queue_startup_recovers_directory_crash_before_state_xattr(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    queue = root / storage_module._QUEUE_ROOT
    queue.mkdir(mode=0o700)
    for lane in ("ready", "deferred", "held"):
        (queue / lane).mkdir(mode=0o700)

    restarted = RootedLocalStorage(root)
    counts = storage_module._queue_counts(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )

    assert counts["ready"] == 0
    assert counts["deferred"] == 0
    assert counts["held"] == 0


def test_queue_fsyncs_new_segment_name_before_creating_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    queue = storage_module._open_queue(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    lane = storage_module._open_queue_lane(queue, "ready")
    lane_identity = storage_module._identity(os.fstat(lane))
    os.close(lane)
    os.close(queue)
    lane_fsynced = False
    real_fsync = storage_module._fsync_descriptor
    real_open = storage_module._segmented_queue.os.open

    def track_fsync(descriptor: int) -> None:
        nonlocal lane_fsynced
        if storage_module._identity(os.fstat(descriptor)) == lane_identity:
            lane_fsynced = True
        real_fsync(descriptor)

    def require_lane_fsync(name: object, *args: object, **kwargs: object) -> int:
        if isinstance(name, str) and name.endswith(".json"):
            assert lane_fsynced
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(storage_module, "_fsync_descriptor", track_fsync)
    monkeypatch.setattr(storage_module._segmented_queue.os, "open", require_lane_fsync)

    storage_module._queue_enqueue(
        storage._root_descriptor_or_raise(),
        storage_module._QUEUE_ROOT,
        "ready",
        _queue_record("00000000-0000-4000-8000-000000000613"),
    )

    assert lane_fsynced


def test_queue_record_write_retries_partial_os_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    real_write = storage_module._segmented_queue.os.write
    writes = 0

    def partial_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        length = max(1, len(payload) // 2)
        return real_write(descriptor, payload[:length])

    monkeypatch.setattr(storage_module._segmented_queue.os, "write", partial_write)
    expected_id = "00000000-0000-4000-8000-000000000614"
    storage_module._queue_enqueue(
        storage._root_descriptor_or_raise(),
        storage_module._QUEUE_ROOT,
        "ready",
        _queue_record(expected_id),
    )
    observed = storage_module._queue_peek(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT, "ready"
    )

    assert writes > 1
    assert observed is not None
    assert observed.record["recordId"] == expected_id


def test_publish_copy_does_not_hold_the_data_mutation_gate(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    first = RootedLocalStorage(root)
    second = RootedLocalStorage(root)
    observed_lock_contention: list[bool] = []

    def writer(handle: object) -> tuple[int, str]:
        result = second.sweep_abandoned(
            datetime(2000, 1, 1),
            frozenset(),
            active_execution_ids=frozenset(),
            active_build_executions=frozenset(),
            budget=StorageSweepBudget(max_seconds=0.02),
        )
        observed_lock_contention.append(result.lock_contended)
        content = b"copied outside the gate"
        handle.write(content)  # type: ignore[attr-defined]
        return len(content), hashlib.sha256(content).hexdigest()

    first._publish("objects/probe.bin", writer, owner_execution_id=None)

    assert observed_lock_contention == [False]


def test_planned_publication_with_no_paths_is_completed_not_held(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000615"
    execution = "10000000-0000-4000-8000-000000000615"
    artifact = "30000000-0000-4000-8000-000000000615"
    stage = "40000000-0000-4000-8000-000000000615"
    storage_module._queue_enqueue(
        storage._root_descriptor_or_raise(),
        storage_module._QUEUE_ROOT,
        "ready",
        {
            "version": 2,
            "kind": "publication",
            "objectKey": f"artifacts/{task}/{execution}/{artifact}.zip",
            "stageObjectKey": (
                f"artifacts/{task}/{execution}/"
                f".wf-stage-{execution}-{stage}"
            ),
            "ownerExecutionId": execution,
            "createdAt": datetime(2026, 8, 9).isoformat(),
        },
    )

    storage.sweep_abandoned(
        datetime(2100, 1, 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    counts = storage_module._queue_counts(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    assert counts["ready"] + counts["deferred"] + counts["held"] == 0


def test_unbound_publication_stage_is_cleaned_after_create_crash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000620"
    execution = "10000000-0000-4000-8000-000000000620"
    artifact = "30000000-0000-4000-8000-000000000620"
    stage_id = "40000000-0000-4000-8000-000000000620"
    parent = root / "artifacts" / task / execution
    parent.mkdir(parents=True)
    stage = parent / f".wf-stage-{execution}-{stage_id}"
    stage.write_bytes(b"partial")
    os.utime(stage, (1_000_000_000, 1_000_000_000))
    storage_module._queue_enqueue(
        storage._root_descriptor_or_raise(),
        storage_module._QUEUE_ROOT,
        "ready",
        {
            "version": 2,
            "kind": "publication",
            "objectKey": f"artifacts/{task}/{execution}/{artifact}.zip",
            "stageObjectKey": stage.relative_to(root).as_posix(),
            "ownerExecutionId": execution,
            "createdAt": datetime(2001, 1, 1).isoformat(),
        },
    )

    storage.sweep_abandoned(
        datetime.fromtimestamp(1_000_000_001),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    counts = storage_module._queue_counts(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    assert not stage.exists()
    assert counts["ready"] + counts["deferred"] + counts["held"] == 0


def test_preexisting_stage_collision_preserves_file_and_releases_intent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    stage_id = UUID("40000000-0000-4000-8000-000000000621")
    storage = RootedLocalStorage(root, uuid_factory=lambda: stage_id)
    parent = root / "objects"
    parent.mkdir()
    stage = parent / f".wf-stage-{stage_id}"
    stage.write_bytes(b"foreign")

    with pytest.raises(FileExistsError):
        storage.publish_bytes("objects/result.bin", b"ours")

    counts = storage_module._queue_counts(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    assert stage.read_bytes() == b"foreign"
    assert counts["ready"] + counts["deferred"] + counts["held"] == 0


def test_publish_link_crash_leaves_a_recoverable_artifact_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    now = datetime(2026, 8, 9, 10, 0, 0)
    storage = RootedLocalStorage(root, clock=lambda: now)
    task = "20000000-0000-4000-8000-000000000601"
    execution = "10000000-0000-4000-8000-000000000601"
    artifact = "30000000-0000-4000-8000-000000000601"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"
    real_link = storage_module._link_no_replace

    def crash_after_link(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)
        raise RuntimeError("injected publication crash")

    monkeypatch.setattr(storage_module, "_link_no_replace", crash_after_link)
    with pytest.raises(RuntimeError, match="injected publication crash"):
        storage.publish_bytes(key, b"artifact", owner_execution_id=execution)
    monkeypatch.setattr(storage_module, "_link_no_replace", real_link)

    storage.sweep_abandoned(
        datetime(2100, 1, 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert not (root / key).exists()
    descriptor = storage._duplicate_root()
    try:
        counts = storage_module._queue_counts(
            descriptor, storage_module._QUEUE_ROOT
        )
    finally:
        os.close(descriptor)
    assert counts["ready"] + counts["deferred"] == 0


def test_preoccupied_generated_artifact_is_never_claimed_by_publication_intent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000611"
    execution = "10000000-0000-4000-8000-000000000611"
    artifact = "30000000-0000-4000-8000-000000000611"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"
    foreign = root / key
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"foreign")

    with pytest.raises(FileExistsError):
        storage.publish_bytes(key, b"owned", owner_execution_id=execution)

    storage.sweep_abandoned(
        datetime(2100, 1, 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )
    counts = storage_module._queue_counts(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )

    assert foreign.read_bytes() == b"foreign"
    assert not list(foreign.parent.glob(".wf-stage-*"))
    assert counts["held"] == 1


def test_publish_unlink_crash_recovers_canonical_from_owned_inode_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000612"
    execution = "10000000-0000-4000-8000-000000000612"
    artifact = "30000000-0000-4000-8000-000000000612"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"
    real_unlink = storage_module._unlink_named

    def crash_after_unlink(*args: object, **kwargs: object) -> None:
        real_unlink(*args, **kwargs)
        raise RuntimeError("injected unlink crash")

    monkeypatch.setattr(storage_module, "_unlink_named", crash_after_unlink)
    with pytest.raises(RuntimeError, match="injected unlink crash"):
        storage.publish_bytes(key, b"artifact", owner_execution_id=execution)
    monkeypatch.setattr(storage_module, "_unlink_named", real_unlink)

    assert (root / key).read_bytes() == b"artifact"
    storage.sweep_abandoned(
        datetime(2100, 1, 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )
    counts = storage_module._queue_counts(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )

    assert not (root / key).exists()
    assert counts["ready"] + counts["deferred"] == 0


def test_successful_artifact_publish_keeps_one_two_phase_record(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000602"
    execution = "10000000-0000-4000-8000-000000000602"
    artifact = "30000000-0000-4000-8000-000000000602"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"

    storage.publish_bytes(key, b"artifact", owner_execution_id=execution)

    descriptor = storage._duplicate_root()
    try:
        counts = storage_module._queue_counts(
            descriptor, storage_module._QUEUE_ROOT
        )
    finally:
        os.close(descriptor)
    assert counts["ready"] == 1
    assert counts["deferred"] == 0
    item = storage_module._queue_peek(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT, "ready"
    )
    assert item is not None
    status = (root / key).stat()
    assert item.record["sourceDev"] == status.st_dev
    assert item.record["sourceIno"] == status.st_ino
    assert item.record["sourceSize"] == status.st_size
    assert item.record["sha256"] == hashlib.sha256(b"artifact").hexdigest()


def test_repeated_ordinary_publishes_do_not_consume_queue_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(storage_module, "_QUEUE_MAX_RECORDS", 2)
    storage = RootedLocalStorage(root)

    for index in range(6):
        storage.publish_bytes(f"requirements/normalized-{index}.txt", b"numpy\n")

    counts = storage_module._queue_counts(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    assert counts["ready"] == 0
    assert counts["deferred"] == 0
    assert counts["held"] == 0


def test_ordinary_publish_completion_failure_remains_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    key = "requirements/normalized.txt"
    real_complete = storage_module._queue_complete

    completions = 0

    def fail_complete(*args: object, **kwargs: object) -> None:
        nonlocal completions
        completions += 1
        if completions == 2:
            raise OSError("injected completion failure")
        real_complete(*args, **kwargs)

    monkeypatch.setattr(storage_module, "_queue_complete", fail_complete)
    with pytest.raises(OSError, match="injected completion failure"):
        storage.publish_bytes(key, b"numpy\n")
    monkeypatch.setattr(storage_module, "_queue_complete", real_complete)

    assert (root / key).read_bytes() == b"numpy\n"
    storage.sweep_abandoned(
        datetime(2100, 1, 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )
    counts = storage_module._queue_counts(
        storage._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    assert (root / key).read_bytes() == b"numpy\n"
    assert counts["ready"] + counts["deferred"] == 0


def test_publication_capacity_failure_precedes_generated_path_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    key = (
        "artifacts/20000000-0000-4000-8000-000000000603/"
        "10000000-0000-4000-8000-000000000603/"
        "30000000-0000-4000-8000-000000000603.zip"
    )

    def reject(*_args: object, **_kwargs: object) -> object:
        raise storage_module.QueueCapacityError("full")

    monkeypatch.setattr(storage_module, "_queue_enqueue", reject)

    with pytest.raises(storage_module.QueueCapacityError, match="full"):
        storage.publish_bytes(key, b"artifact")

    assert not (root / "artifacts").exists()


def test_quarantine_intent_is_durable_before_quarantine_name_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    published = storage.publish_bytes("objects/probe.bin", b"owned")
    real_enqueue = storage_module._queue_enqueue
    real_mkdir = storage_module.os.mkdir
    quarantine_intent_written = False

    def track_enqueue(*args: object, **kwargs: object) -> object:
        nonlocal quarantine_intent_written
        record = args[3]
        result = real_enqueue(*args, **kwargs)
        if isinstance(record, dict) and record.get("kind") == "quarantine":
            quarantine_intent_written = True
        return result

    def require_intent_before_mkdir(name: object, *args: object, **kwargs: object) -> object:
        if isinstance(name, str) and name.startswith(".wf-quarantine-v1-"):
            assert quarantine_intent_written
        return real_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(storage_module, "_queue_enqueue", track_enqueue)
    monkeypatch.setattr(storage_module.os, "mkdir", require_intent_before_mkdir)

    assert storage.delete_if_owned(published.object_key, published.sha256)


def test_quarantine_capacity_failure_does_not_create_or_move_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    published = storage.publish_bytes("objects/probe.bin", b"owned")
    real_enqueue = storage_module._queue_enqueue

    def reject_quarantine(*args: object, **kwargs: object) -> object:
        record = args[3]
        if isinstance(record, dict) and record.get("kind") == "quarantine":
            raise storage_module.QueueCapacityError("full")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(storage_module, "_queue_enqueue", reject_quarantine)

    with pytest.raises(storage_module.QueueCapacityError, match="full"):
        storage.delete_if_owned(published.object_key, published.sha256)

    assert (root / published.object_key).read_bytes() == b"owned"
    assert not list((root / "objects").glob(".wf-quarantine-v1-*"))


def test_deferred_record_before_next_attempt_has_zero_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    now = datetime(2026, 8, 9, 10, 0, 0)
    storage = RootedLocalStorage(root, clock=lambda: now)
    descriptor = storage._root_descriptor_or_raise()
    item = storage_module._queue_enqueue(
        descriptor,
        storage_module._QUEUE_ROOT,
        "deferred",
        {
            **_queue_record("00000000-0000-4000-8000-000000000604"),
            "nextAttemptAt": "2026-08-09T11:00:00.000000",
        },
    )

    def unexpected_mutation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("deferred record was mutated before nextAttemptAt")

    monkeypatch.setattr(storage_module, "_queue_move", unexpected_mutation)
    monkeypatch.setattr(storage_module, "_queue_complete", unexpected_mutation)

    result = storage.sweep_abandoned(
        datetime(2100, 1, 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )
    observed = storage_module._queue_peek(
        descriptor, storage_module._QUEUE_ROOT, "deferred"
    )

    assert not result.progress_made
    assert result.mutations == 0
    assert observed is not None
    assert observed.record["recordId"] == item.record["recordId"]
    assert observed.record["generation"] == item.record["generation"]


def test_due_deferred_record_is_not_hidden_by_future_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    now = datetime(2026, 8, 9, 10, 0, 0)
    storage = RootedLocalStorage(root, clock=lambda: now)
    descriptor = storage._root_descriptor_or_raise()
    future_id = "00000000-0000-4000-8000-000000000606"
    due_id = "00000000-0000-4000-8000-000000000607"
    storage_module._queue_enqueue(
        descriptor,
        storage_module._QUEUE_ROOT,
        "deferred",
        {
            **_queue_record(future_id),
            "nextAttemptAt": "2026-08-09T11:00:00.000000",
        },
    )
    storage_module._queue_enqueue(
        descriptor,
        storage_module._QUEUE_ROOT,
        "deferred",
        {
            **_queue_record(due_id),
            "nextAttemptAt": "2026-08-09T09:00:00.000000",
        },
    )

    result = storage.sweep_abandoned(
        datetime(2100, 1, 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )
    remaining = storage_module._queue_peek(
        descriptor, storage_module._QUEUE_ROOT, "deferred"
    )

    assert result.progress_made
    assert remaining is not None
    assert remaining.record["recordId"] == future_id
    assert storage_module._queue_counts(
        descriptor, storage_module._QUEUE_ROOT
    )["deferred"] == 1


def test_workspace_intent_precedes_allocated_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    manager = WorkspaceManager(root)
    real_enqueue = storage_module._queue_enqueue
    real_mkdir = storage_module.os.mkdir
    workspace_intent_written = False

    def track_enqueue(*args: object, **kwargs: object) -> object:
        nonlocal workspace_intent_written
        record = args[3]
        result = real_enqueue(*args, **kwargs)
        if isinstance(record, dict) and record.get("kind") == "workspace":
            workspace_intent_written = True
        return result

    def require_intent_before_mkdir(name: object, *args: object, **kwargs: object) -> object:
        if isinstance(name, str) and name.startswith("wf-execution-"):
            assert workspace_intent_written
        return real_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(storage_module, "_queue_enqueue", track_enqueue)
    monkeypatch.setattr(storage_module.os, "mkdir", require_intent_before_mkdir)

    owned = manager.allocate()
    owned.cleanup()


def test_workspace_mkdir_crash_is_held_without_unbound_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    identifier = UUID("10000000-0000-4000-8000-000000000608")
    manager = WorkspaceManager(root, uuid_factory=lambda: identifier)
    real_mkdir = storage_module.os.mkdir

    def crash_after_workspace_mkdir(
        name: object, *args: object, **kwargs: object
    ) -> object:
        result = real_mkdir(name, *args, **kwargs)
        if isinstance(name, str) and name.startswith("wf-execution-"):
            raise SystemExit("injected crash")
        return result

    monkeypatch.setattr(storage_module.os, "mkdir", crash_after_workspace_mkdir)
    with pytest.raises(SystemExit, match="injected crash"):
        manager.allocate()
    monkeypatch.setattr(storage_module.os, "mkdir", real_mkdir)
    manager.close()

    restarted = WorkspaceManager(root)
    result = restarted.sweep_abandoned(
        datetime(2100, 1, 1), active_execution_ids=frozenset()
    )

    assert result.workspaces_removed == 0
    assert result.workspaces_held == 1
    assert (root / f"wf-execution-{identifier}").is_dir()
    assert result.backlog_entries == 0


def test_workspace_planned_intent_never_deletes_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    identifier = UUID("10000000-0000-4000-8000-000000000619")
    manager = WorkspaceManager(root, uuid_factory=lambda: identifier)
    real_mkdir = storage_module.os.mkdir

    def crash_before_workspace_mkdir(
        name: object, *args: object, **kwargs: object
    ) -> object:
        if isinstance(name, str) and name.startswith("wf-execution-"):
            raise SystemExit("injected crash before mkdir")
        return real_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "mkdir", crash_before_workspace_mkdir)
    with pytest.raises(SystemExit, match="before mkdir"):
        manager.allocate()
    monkeypatch.setattr(storage_module.os, "mkdir", real_mkdir)
    manager.close()
    foreign = root / f"wf-execution-{identifier}"
    foreign.mkdir()
    (foreign / "foreign.txt").write_text("preserve")

    restarted = WorkspaceManager(root)
    result = restarted.sweep_abandoned(
        datetime(2100, 1, 1), active_execution_ids=frozenset()
    )

    assert result.workspaces_held == 1
    assert (foreign / "foreign.txt").read_text() == "preserve"


def test_startup_migrates_nested_legacy_quarantine_once_and_marks_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    task = "20000000-0000-4000-8000-000000000605"
    execution = "10000000-0000-4000-8000-000000000605"
    quarantine_id = "40000000-0000-4000-8000-000000000605"
    quarantine = (
        root
        / "artifacts"
        / task
        / execution
        / f".wf-quarantine-v1-{quarantine_id}"
    )
    quarantine.mkdir(parents=True)
    (quarantine / "legacy.zip").write_bytes(b"legacy")

    restarted = RootedLocalStorage(root)
    counts = storage_module._queue_counts(
        restarted._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )
    migration_marker = storage_module._get_descriptor_xattr(
        restarted._root_descriptor_or_raise(),
        storage_module._LEGACY_MIGRATION_STATE_XATTR,
        max_bytes=32,
    )
    quarantine_descriptor = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY)
    try:
        marker = storage_module._get_descriptor_xattr(
            quarantine_descriptor,
            storage_module._LEGACY_QUARANTINE_STATE_XATTR,
            max_bytes=32,
        )
    finally:
        os.close(quarantine_descriptor)
    restarted.close()

    real_open = storage_module.os.open

    def reject_repeated_artifact_scan(
        path: object, *args: object, **kwargs: object
    ) -> int:
        if path == "artifacts":
            raise AssertionError("completed legacy migration scanned artifacts again")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "open", reject_repeated_artifact_scan)
    monkeypatch.setattr(
        storage_module.os,
        "supports_dir_fd",
        storage_module.os.supports_dir_fd | {reject_repeated_artifact_scan},
    )
    again = RootedLocalStorage(root)
    repeated = storage_module._queue_counts(
        again._root_descriptor_or_raise(), storage_module._QUEUE_ROOT
    )

    assert marker == b"held"
    assert migration_marker == storage_module._LEGACY_MIGRATION_COMPLETE
    assert counts["held"] == 1
    assert repeated["held"] == 1


def test_legacy_migration_limit_is_persistently_held_without_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    nested = (
        root
        / "artifacts"
        / "20000000-0000-4000-8000-000000000606"
        / "10000000-0000-4000-8000-000000000606"
    )
    nested.mkdir(parents=True)
    (nested / "ordinary.zip").write_bytes(b"ordinary")
    monkeypatch.setattr(storage_module, "_LEGACY_MIGRATION_MAX_ENTRIES", 1)

    with pytest.raises(RuntimeError, match="data root capability probe failed"):
        RootedLocalStorage(root)

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        marker = storage_module._get_descriptor_xattr(
            descriptor,
            storage_module._LEGACY_MIGRATION_STATE_XATTR,
            max_bytes=32,
        )
    finally:
        os.close(descriptor)
    assert marker == storage_module._LEGACY_MIGRATION_HELD_LIMIT

    real_open = storage_module.os.open

    def reject_repeated_artifact_scan(
        path: object, *args: object, **kwargs: object
    ) -> int:
        if path == "artifacts":
            raise AssertionError("held legacy migration scanned artifacts again")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "open", reject_repeated_artifact_scan)
    monkeypatch.setattr(
        storage_module.os,
        "supports_dir_fd",
        storage_module.os.supports_dir_fd | {reject_repeated_artifact_scan},
    )
    with pytest.raises(RuntimeError, match="data root capability probe failed") as error:
        RootedLocalStorage(root)
    assert isinstance(error.value.__cause__, RuntimeError)
    assert "held" in str(error.value.__cause__)


def test_legacy_migration_never_marks_complete_after_record_budget_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    nested = (
        root
        / "artifacts"
        / "20000000-0000-4000-8000-000000000622"
        / "10000000-0000-4000-8000-000000000622"
    )
    for suffix in ("622", "623"):
        quarantine = nested / (
            ".wf-quarantine-v1-40000000-0000-4000-8000-000000000" + suffix
        )
        quarantine.mkdir(parents=True)
        (quarantine / "legacy.zip").write_bytes(b"legacy")
    monkeypatch.setattr(storage_module, "_QUEUE_MAX_RECORDS", 1)

    with pytest.raises(RuntimeError, match="data root capability probe failed"):
        RootedLocalStorage(root)

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        marker = storage_module._get_descriptor_xattr(
            descriptor,
            storage_module._LEGACY_MIGRATION_STATE_XATTR,
            max_bytes=32,
        )
    finally:
        os.close(descriptor)
    assert marker == storage_module._LEGACY_MIGRATION_HELD_LIMIT


def test_legacy_migration_enforces_absolute_time_budget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    nested = (
        root
        / "artifacts"
        / "20000000-0000-4000-8000-000000000624"
        / "10000000-0000-4000-8000-000000000624"
    )
    quarantine = (
        nested / ".wf-quarantine-v1-40000000-0000-4000-8000-000000000624"
    )
    quarantine.mkdir(parents=True)
    (quarantine / "legacy.zip").write_bytes(b"legacy")
    moments = iter((0.0, 61.0, 61.0, 61.0))

    with pytest.raises(RuntimeError, match="data root capability probe failed"):
        RootedLocalStorage(root, monotonic=lambda: next(moments, 61.0))

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        marker = storage_module._get_descriptor_xattr(
            descriptor,
            storage_module._LEGACY_MIGRATION_STATE_XATTR,
            max_bytes=32,
        )
    finally:
        os.close(descriptor)
    assert marker == storage_module._LEGACY_MIGRATION_HELD_LIMIT


def test_quarantine_queue_order_covers_dfs_lexical_counterexample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    real_quarantine = storage_module._quarantine_named

    def crash(*args: object, **kwargs: object) -> object:
        binding = real_quarantine(*args, **kwargs)
        assert binding is not None
        os.close(binding.descriptor)
        raise RuntimeError("crash")

    for key in ("a/z/first.whl", "a-/second.whl"):
        item = storage.publish_bytes(key, key.encode())
        monkeypatch.setattr(storage_module, "_quarantine_named", crash)
        with pytest.raises(RuntimeError, match="crash"):
            storage.delete_if_owned(item.object_key, item.sha256)
        monkeypatch.setattr(storage_module, "_quarantine_named", real_quarantine)

    for _ in range(8):
        result = storage.sweep_abandoned(
            datetime(2026, 8, 2), frozenset(),
            active_execution_ids=frozenset(),
            active_build_executions=frozenset(),
            budget=StorageSweepBudget(max_quarantines=1, max_entries=1),
        )
        if result.backlog_entries == 0:
            break

    assert storage.read_bytes("a/z/first.whl") == b"a/z/first.whl"
    assert storage.read_bytes("a-/second.whl") == b"a-/second.whl"
    assert result.backlog_entries == 0


def test_legacy_oversized_quarantine_is_persistently_held_without_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    item = storage.publish_bytes("legacy/payload.whl", b"12345")
    parent = storage._open_parent(("legacy",), create=False)
    try:
        binding = storage_module._quarantine_named(
            parent,
            "payload.whl",
            storage_module._QuarantineMetadata(
                "00000000-0000-4000-8000-000000000401",
                item.object_key,
                "payload.whl",
                item.sha256,
                None,
                datetime(2026, 8, 1),
                queue_descriptor=storage._root_descriptor_or_raise(),
            ),
        )
        assert binding is not None
        os.close(binding.descriptor)
    finally:
        parent.close()
    reads = 0

    def forbidden_hash(_descriptor: int) -> str:
        nonlocal reads
        reads += 1
        raise AssertionError("legacy oversized payload was read")

    monkeypatch.setattr(storage_module, "_hash_descriptor", forbidden_hash)
    first = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_bytes=1),
    )
    storage.close()
    restarted = RootedLocalStorage(root)
    second = restarted.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_bytes=1),
    )

    assert first.oversized_quarantines == 1
    assert first.quarantines_held == second.quarantines_held == 1
    assert first.backlog_entries == second.backlog_entries == 0
    assert reads == 0


def test_more_than_cache_sized_conflicts_are_persistently_held_without_rehash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root, clock=lambda: datetime(2026, 8, 1))
    real_quarantine = storage_module._quarantine_named

    def crash(*args: object, **kwargs: object) -> object:
        binding = real_quarantine(*args, **kwargs)
        assert binding is not None
        os.close(binding.descriptor)
        raise RuntimeError("crash")

    for index in range(129):
        key = f"bulk/{index:03d}.whl"
        item = storage.publish_bytes(key, b"owned")
        monkeypatch.setattr(storage_module, "_quarantine_named", crash)
        with pytest.raises(RuntimeError, match="crash"):
            storage.delete_if_owned(key, item.sha256)
        monkeypatch.setattr(storage_module, "_quarantine_named", real_quarantine)
        storage.publish_bytes(key, b"replacement")
    reads = 0

    def forbidden_hash(_descriptor: int) -> str:
        nonlocal reads
        reads += 1
        raise AssertionError("persisted conflict was rehashed")

    monkeypatch.setattr(storage_module, "_hash_descriptor", forbidden_hash)
    budget = StorageSweepBudget(
        max_quarantines=256,
        max_seconds=10,
        max_entries=1024,
        max_mutations=10_000,
    )
    first = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=budget,
    )
    storage.close()
    restarted = RootedLocalStorage(root)
    second = restarted.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=budget,
    )

    assert first.quarantine_conflicts == 129
    assert first.quarantines_held == second.quarantines_held == 129
    assert second.quarantine_conflicts == 0
    assert reads == 0


def test_data_sweep_mutation_budget_prevents_all_underlying_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000616"
    execution = "10000000-0000-4000-8000-000000000616"
    artifact = "30000000-0000-4000-8000-000000000616"
    storage.publish_bytes(
        f"artifacts/{task}/{execution}/{artifact}.zip",
        b"pending",
        owner_execution_id=execution,
    )
    operations = 0

    def count_call(function: object) -> object:
        def counted(*args: object, **kwargs: object) -> object:
            nonlocal operations
            operations += 1
            return function(*args, **kwargs)  # type: ignore[operator]

        return counted

    monkeypatch.setattr(storage_module, "_rename_no_replace", count_call(storage_module._rename_no_replace))
    monkeypatch.setattr(storage_module.os, "unlink", count_call(storage_module.os.unlink))
    monkeypatch.setattr(storage_module.os, "rmdir", count_call(storage_module.os.rmdir))
    monkeypatch.setattr(storage_module, "_put_descriptor_xattr", count_call(storage_module._put_descriptor_xattr))
    monkeypatch.setattr(storage_module, "_fsync_descriptor", count_call(storage_module._fsync_descriptor))

    result = storage.sweep_abandoned(
        datetime.max, frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_mutations=1),
    )

    assert result.budget_exhausted
    assert result.backlog_entries > 0
    assert operations == 0


def test_workspace_sweep_rotates_past_active_prefix_with_persisted_queue(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root)
    active_id = "00000000-0000-4000-8000-000000000001"
    stale_ids = (
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000003",
    )
    active = manager.allocate(active_id)
    stale = [manager.allocate(value) for value in stale_ids]
    for item in (active, *stale):
        item.close()
        os.utime(item.path, (1_000_000_000, 1_000_000_000))

    results = []
    for _ in range(8):
        results.append(
            manager.sweep_abandoned(
                datetime.fromtimestamp(1_000_000_001),
                active_execution_ids=frozenset({active_id}),
                budget=StorageSweepBudget(max_entries=1, max_mutations=16),
            )
        )
        if not any(item.path.exists() for item in stale):
            break

    assert active.path.exists()
    assert not any(item.path.exists() for item in stale)
    assert all(result.entries_scanned <= 1 for result in results)


def test_owned_workspace_cleanup_is_bounded_and_resumed_by_sweeper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root)
    owned = manager.allocate("00000000-0000-4000-8000-000000000004")
    for index in range(12):
        owned.capability.write_bytes(f"nested/{index:02d}.bin", b"x")

    first = owned.cleanup(
        budget=StorageSweepBudget(max_entries=2, max_mutations=16)
    )

    assert first.budget_exhausted
    assert first.entries_scanned <= 2
    for _ in range(16):
        result = manager.sweep_abandoned(
            datetime.max,
            active_execution_ids=frozenset(),
            budget=StorageSweepBudget(max_entries=2, max_mutations=16),
        )
        if result.backlog_entries == 0:
            break
    assert result.backlog_entries == 0
    assert not owned.path.exists()


def test_persisted_directory_cursor_progresses_under_one_directory_budget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    task = "20000000-0000-4000-8000-000000000091"
    execution = "10000000-0000-4000-8000-000000000091"
    artifact = "30000000-0000-4000-8000-000000000091"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"
    storage = RootedLocalStorage(root)
    storage.publish_bytes(key, b"old")
    os.utime(root / key, (1_000_000_000, 1_000_000_000))
    storage.close()
    budget = StorageSweepBudget(max_directories=1)

    for _attempt in range(6):
        storage = RootedLocalStorage(root)
        storage.sweep_abandoned(
            datetime.fromtimestamp(1_000_000_001), frozenset(),
            active_execution_ids=frozenset(),
            active_build_executions=frozenset(), budget=budget,
        )
        storage.close()
        if not (root / key).exists():
            break

    assert not (root / key).exists()


def test_quarantine_time_budget_expires_before_payload_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    times = iter((0.0, 2.0))
    storage = RootedLocalStorage(
        root,
        clock=lambda: datetime(2026, 8, 1),
        monotonic=lambda: next(times, 2.0),
    )
    published = storage.publish_bytes("artifacts/timed.zip", b"owned")
    _leave_quarantine_after_isolation(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated crash"):
        storage.delete_if_owned(published.object_key, published.sha256)
    monkeypatch.undo()
    reads = 0

    def forbidden_hash(_descriptor: int) -> str:
        nonlocal reads
        reads += 1
        raise AssertionError("payload was read after the deadline")

    monkeypatch.setattr(storage_module, "_hash_descriptor", forbidden_hash)
    stats = storage.sweep_abandoned(
        datetime(2026, 8, 2), frozenset(),
        active_execution_ids=frozenset(), active_build_executions=frozenset(),
        budget=StorageSweepBudget(max_quarantines=1, max_bytes=4, max_seconds=1),
    )
    assert stats.budget_exhausted
    assert stats.quarantines_examined == 0
    assert reads == 0


def test_compensation_delete_prunes_empty_execution_and_task_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000031"
    execution = "10000000-0000-4000-8000-000000000031"
    artifact = "30000000-0000-4000-8000-000000000031"
    published = storage.publish_bytes(
        f"artifacts/{task}/{execution}/{artifact}.zip", b"artifact"
    )

    assert storage.delete_if_owned(published.object_key, published.sha256) is True

    assert not (root / f"artifacts/{task}/{execution}").exists()
    assert not (root / f"artifacts/{task}").exists()
    assert (root / "artifacts").is_dir()


def test_compensation_delete_preserves_sibling_execution_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000032"
    execution = "10000000-0000-4000-8000-000000000032"
    sibling_execution = "10000000-0000-4000-8000-000000000033"
    artifact = "30000000-0000-4000-8000-000000000032"
    sibling_artifact = "30000000-0000-4000-8000-000000000033"
    published = storage.publish_bytes(
        f"artifacts/{task}/{execution}/{artifact}.zip", b"artifact"
    )
    sibling = storage.publish_bytes(
        f"artifacts/{task}/{sibling_execution}/{sibling_artifact}.zip",
        b"sibling",
    )

    assert storage.delete_if_owned(published.object_key, published.sha256) is True

    assert not (root / f"artifacts/{task}/{execution}").exists()
    assert storage.read_bytes(sibling.object_key) == b"sibling"
    assert (root / f"artifacts/{task}/{sibling_execution}").is_dir()


def test_workspace_is_a_private_generated_child_and_is_not_reused(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    ids = iter((UUID(int=1), UUID(int=2)))
    manager = WorkspaceManager(root, uuid_factory=lambda: next(ids))

    first = manager.allocate()
    first_path = first.path
    assert first_path.parent == root
    assert first_path.name == "wf-execution-00000000-0000-0000-0000-000000000001"
    if os.name != "nt":
        assert first_path.stat().st_mode & 0o077 == 0
    first.cleanup()

    second = manager.allocate()
    assert second.path != first_path
    second.cleanup()


def test_workspace_allocation_persists_identity_bound_queue_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    execution = "10000000-0000-4000-8000-000000000615"
    manager = WorkspaceManager(root)

    owned = manager.allocate(execution)
    item = storage_module._queue_peek(
        manager._root_descriptor,
        storage_module._WORKSPACE_QUEUE_ROOT,
        "ready",
    )
    status = owned.path.stat()

    assert item is not None
    assert item.record["generation"] == 1
    assert item.record["sourceDev"] == status.st_dev
    assert item.record["sourceIno"] == status.st_ino


def test_workspace_normal_cleanup_completes_maintenance_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root)
    owned = manager.allocate("10000000-0000-4000-8000-000000000616")
    owned.capability.write_bytes("output.bin", b"output")

    result = owned.cleanup()
    counts = storage_module._queue_counts(
        manager._root_descriptor, storage_module._WORKSPACE_QUEUE_ROOT
    )

    assert result.backlog_entries == 0
    assert sum(counts[lane] for lane in ("ready", "deferred", "held")) == 0


def test_workspace_allocation_closes_child_fd_when_parent_duplication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root, uuid_factory=lambda: UUID(int=3))
    original_open = os.open
    original_dup = os.dup
    duplicate_calls = 0
    child_descriptor: int | None = None

    def tracking_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal child_descriptor
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if child_descriptor is None and str(path).startswith("wf-execution-"):
            child_descriptor = descriptor
        return descriptor

    def fail_second_dup(descriptor: int) -> int:
        nonlocal duplicate_calls
        duplicate_calls += 1
        if duplicate_calls == 2:
            raise OSError("injected parent descriptor duplication failure")
        return original_dup(descriptor)

    monkeypatch.setattr(storage_module.os, "open", tracking_open)
    monkeypatch.setattr(storage_module.os, "dup", fail_second_dup)

    with pytest.raises(OSError, match="injected parent descriptor"):
        manager.allocate()

    assert child_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(child_descriptor)


def test_workspace_cleanup_refuses_replaced_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root, uuid_factory=lambda: UUID(int=1))
    owned = manager.allocate()
    original = owned.path
    moved = root / "moved"
    original.rename(moved)
    original.mkdir()
    (original / "keep.txt").write_text("keep")

    with pytest.raises(RuntimeError, match="identity"):
        owned.cleanup()

    assert (original / "keep.txt").read_text() == "keep"
    item = storage_module._queue_peek(
        manager._root_descriptor,
        storage_module._WORKSPACE_QUEUE_ROOT,
        "ready",
    )
    assert item is not None
    assert item.record["sourceDev"] == moved.stat().st_dev
    assert item.record["sourceIno"] == moved.stat().st_ino


def test_workspace_cleanup_preserves_top_level_replacement_during_final_rmdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root, uuid_factory=lambda: UUID(int=101))
    owned = manager.allocate()
    owned.capability.write_bytes("nested/output.bin", b"generated")
    real_rename = storage_module._rename_no_replace
    injected = False

    def replace_after_isolation(
        source_parent: int,
        source: str,
        destination_parent: int,
        destination: str,
    ) -> None:
        nonlocal injected
        real_rename(source_parent, source, destination_parent, destination)
        if source == owned.path.name and not injected:
            injected = True
            replacement = root / owned.path.name
            replacement.mkdir()
            (replacement / "keep.txt").write_text("keep")

    monkeypatch.setattr(storage_module, "_rename_no_replace", replace_after_isolation)

    owned.cleanup()

    assert injected
    assert (root / owned.path.name / "keep.txt").read_text() == "keep"
    assert not (root / storage_module._retired_workspace_name(owned.path.name)).exists()


def test_workspace_cleanup_never_delegates_top_level_name_to_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    owned = WorkspaceManager(root, uuid_factory=lambda: UUID(int=102)).allocate()
    owned.capability.write_bytes("nested/output.bin", b"generated")

    def forbidden_rmtree(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("mutable top-level rmtree was used")

    monkeypatch.setattr(shutil, "rmtree", forbidden_rmtree)

    owned.cleanup()

    assert not owned.path.exists()


def test_workspace_mutations_share_bounded_gate_across_managers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    first = WorkspaceManager(root, uuid_factory=lambda: UUID(int=201))
    second = WorkspaceManager(root, uuid_factory=lambda: UUID(int=202))
    owned = first.allocate()
    entered = threading.Event()
    release = threading.Event()
    real_clear = storage_module._bounded_clear_workspace

    def pause(*args: object, **kwargs: object) -> bool:
        entered.set()
        assert release.wait(2)
        return real_clear(*args, **kwargs)

    monkeypatch.setattr(storage_module, "_bounded_clear_workspace", pause)
    cleanup = threading.Thread(target=owned.cleanup)
    cleanup.start()
    assert entered.wait(2)
    started = time.monotonic()
    result = second.sweep_abandoned(
        datetime(2026, 8, 2),
        active_execution_ids=frozenset(),
        budget=StorageSweepBudget(max_seconds=0.05),
    )
    elapsed = time.monotonic() - started
    release.set()
    cleanup.join(2)
    assert elapsed < 0.5
    assert result.lock_contended
    assert not owned.path.exists()


def _replace_root(root: Path) -> tuple[Path, Path]:
    moved = root.with_name(f"{root.name}-moved")
    root.rename(moved)
    root.mkdir(mode=0o700)
    return moved, root


def test_publish_stays_on_retained_root_during_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    duplicate_root = storage._duplicate_root
    moved: Path | None = None

    def replace_then_duplicate() -> int:
        nonlocal moved
        descriptor = duplicate_root()
        moved, _replacement = _replace_root(root)
        return descriptor

    monkeypatch.setattr(storage, "_duplicate_root", replace_then_duplicate)

    storage.publish_bytes("objects/probe.bin", b"owned")

    assert moved is not None
    assert (moved / "objects/probe.bin").read_bytes() == b"owned"
    assert not (root / "objects/probe.bin").exists()


def test_read_stays_on_retained_root_during_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    storage.publish_bytes("objects/probe.bin", b"owned")
    duplicate_root = storage._duplicate_root

    def replace_then_duplicate() -> int:
        descriptor = duplicate_root()
        moved, _replacement = _replace_root(root)
        (root / "objects").mkdir()
        (root / "objects/probe.bin").write_bytes(b"replacement")
        assert (moved / "objects/probe.bin").exists()
        return descriptor

    monkeypatch.setattr(storage, "_duplicate_root", replace_then_duplicate)

    assert storage.read_bytes("objects/probe.bin") == b"owned"


def test_delete_stays_on_retained_root_during_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    published = storage.publish_bytes("objects/probe.bin", b"owned")
    duplicate_root = storage._duplicate_root
    moved: Path | None = None

    def replace_then_duplicate() -> int:
        nonlocal moved
        descriptor = duplicate_root()
        moved, _replacement = _replace_root(root)
        (root / "objects").mkdir()
        (root / "objects/probe.bin").write_bytes(b"replacement")
        return descriptor

    monkeypatch.setattr(storage, "_duplicate_root", replace_then_duplicate)

    assert storage.delete_if_owned("objects/probe.bin", published.sha256) is True
    assert moved is not None
    assert not (moved / "objects/probe.bin").exists()
    assert (root / "objects/probe.bin").read_bytes() == b"replacement"


def test_workspace_allocate_fails_closed_if_root_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root, uuid_factory=lambda: UUID(int=11))
    duplicate_root = manager._duplicate_root
    moved: Path | None = None

    def replace_then_duplicate() -> int:
        nonlocal moved
        descriptor = duplicate_root()
        moved, _replacement = _replace_root(root)
        return descriptor

    monkeypatch.setattr(manager, "_duplicate_root", replace_then_duplicate)

    with pytest.raises(RuntimeError, match="root identity changed"):
        manager.allocate()

    assert moved is not None
    assert not list(moved.glob("wf-execution-*"))
    assert not list(root.iterdir())


def test_workspace_cleanup_uses_retained_root_after_path_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root, uuid_factory=lambda: UUID(int=12))
    owned = manager.allocate()
    moved, _replacement = _replace_root(root)
    replacement_workspace = root / owned.path.name
    replacement_workspace.mkdir()
    (replacement_workspace / "keep.txt").write_text("keep")

    owned.cleanup()

    assert not (moved / owned.path.name).exists()
    assert (replacement_workspace / "keep.txt").read_text() == "keep"


def test_storage_sweeper_uses_retained_root_after_path_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    storage = RootedLocalStorage(root)
    old_stage = root / ".wf-stage-00000000-0000-0000-0000-000000000013"
    old_stage.write_bytes(b"old")
    moved, _replacement = _replace_root(root)
    replacement_stage = root / old_stage.name
    replacement_stage.write_bytes(b"replacement")
    old = 1_000_000_000
    os.utime(moved / old_stage.name, (old, old))
    os.utime(replacement_stage, (old, old))

    storage.sweep_abandoned(
        datetime.fromtimestamp(old + 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert not (moved / old_stage.name).exists()
    assert replacement_stage.read_bytes() == b"replacement"


def test_workspace_sweeper_uses_retained_root_after_path_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    manager = WorkspaceManager(root, uuid_factory=lambda: UUID(int=14))
    owned = manager.allocate()
    owned.close()
    moved, _replacement = _replace_root(root)
    replacement_workspace = root / owned.path.name
    replacement_workspace.mkdir()
    old = 1_000_000_000
    os.utime(moved / owned.path.name, (old, old))
    os.utime(replacement_workspace, (old, old))

    manager.sweep_abandoned(
        datetime.fromtimestamp(old + 1), active_execution_ids=frozenset()
    )

    assert not (moved / owned.path.name).exists()
    assert replacement_workspace.exists()


def test_storage_and_workspace_manager_reject_operations_after_close(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir()
    workspace_root.mkdir()
    storage = RootedLocalStorage(data_root)
    workspaces = WorkspaceManager(workspace_root)

    storage.close()
    storage.close()
    workspaces.close()
    workspaces.close()

    with pytest.raises(RuntimeError, match="closed"):
        storage.publish_bytes("probe.bin", b"probe")
    with pytest.raises(RuntimeError, match="closed"):
        workspaces.allocate()


def test_artifact_sweeper_prunes_empty_execution_and_task_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000021"
    execution = "10000000-0000-4000-8000-000000000021"
    artifact = "30000000-0000-4000-8000-000000000021"
    key = f"artifacts/{task}/{execution}/{artifact}.zip"
    published = storage.publish_bytes(key, b"old")
    old = 1_000_000_000
    os.utime(root / published.object_key, (old, old))

    storage.sweep_abandoned(
        datetime.fromtimestamp(old + 1),
        frozenset(),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert not (root / f"artifacts/{task}/{execution}").exists()
    assert not (root / f"artifacts/{task}").exists()
    assert (root / "artifacts").is_dir()


def test_artifact_sweeper_preserves_nonempty_generated_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)
    task = "20000000-0000-4000-8000-000000000022"
    stale_execution = "10000000-0000-4000-8000-000000000022"
    live_execution = "10000000-0000-4000-8000-000000000023"
    stale_key = (
        f"artifacts/{task}/{stale_execution}/"
        "30000000-0000-4000-8000-000000000022.zip"
    )
    live_key = (
        f"artifacts/{task}/{live_execution}/"
        "30000000-0000-4000-8000-000000000023.zip"
    )
    stale = storage.publish_bytes(stale_key, b"stale")
    live = storage.publish_bytes(live_key, b"live")
    concurrent = root / f"artifacts/{task}/{stale_execution}/keep.txt"
    concurrent.write_bytes(b"concurrent")
    old = 1_000_000_000
    os.utime(root / stale.object_key, (old, old))
    os.utime(root / live.object_key, (old, old))

    storage.sweep_abandoned(
        datetime.fromtimestamp(old + 1),
        frozenset({live.object_key}),
        active_execution_ids=frozenset(),
        active_build_executions=frozenset(),
    )

    assert concurrent.read_bytes() == b"concurrent"
    assert (root / live.object_key).read_bytes() == b"live"
    assert (root / f"artifacts/{task}").is_dir()


def test_windows_worker_host_fails_closed_before_path_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(storage_module.os, "name", "nt")

    with pytest.raises(RuntimeError, match="Windows Worker hosts are not supported"):
        RootedLocalStorage(root)
    with pytest.raises(RuntimeError, match="Windows Worker hosts are not supported"):
        WorkspaceManager(root)


def test_data_and_workspace_roots_run_distinct_capability_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir()
    workspace_root.mkdir()
    calls: list[str] = []
    monkeypatch.setattr(
        storage_module,
        "_probe_root_capabilities",
        lambda _descriptor: calls.append("data"),
    )
    monkeypatch.setattr(
        storage_module,
        "_probe_workspace_capabilities",
        lambda _descriptor: calls.append("workspace"),
    )

    RootedLocalStorage(data_root).close()
    WorkspaceManager(workspace_root).close()

    assert calls == ["data", "workspace"]


def test_data_probe_requires_cross_directory_no_replace_and_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    real_rename = storage_module._rename_no_replace
    cross_directory = False
    missing_source = False

    def observe(
        source_parent: int,
        source: str,
        destination_parent: int,
        destination: str,
    ) -> None:
        nonlocal cross_directory, missing_source
        if source_parent != destination_parent:
            cross_directory = True
        if source == "missing":
            missing_source = True
        real_rename(source_parent, source, destination_parent, destination)

    monkeypatch.setattr(storage_module, "_rename_no_replace", observe)

    RootedLocalStorage(root).close()

    assert cross_directory
    assert missing_source


def test_external_stage_host_without_proc_fd_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module.sys, "platform", "darwin")

    with pytest.raises(RuntimeError, match="Linux /proc/self/fd"):
        storage_module.require_external_workspace_support()


def test_publish_cleanup_failure_does_not_replace_writer_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    storage = RootedLocalStorage(root)

    def fail_write(_handle: object, _content: bytes) -> tuple[int, str]:
        raise ValueError("primary writer failure")

    def fail_unlink(_parent: object, _name: str) -> None:
        raise OSError("secondary unlink failure")

    monkeypatch.setattr(storage_module, "_write_bytes", fail_write)
    monkeypatch.setattr(storage_module, "_unlink_named", fail_unlink)

    with pytest.raises(ValueError, match="primary writer failure"):
        storage.publish_bytes("generated/output.bin", b"content")
