from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

import wheelforge_worker.jobs.storage as storage_module
from wheelforge_worker.jobs.storage import (
    InvalidObjectKey,
    RootedLocalStorage,
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


def test_compensation_deletes_only_the_exact_owned_content(
    storage: RootedLocalStorage,
) -> None:
    first = storage.publish_bytes("artifacts/a.zip", b"first")

    assert storage.delete_if_owned("artifacts/a.zip", "0" * 64) is False
    assert storage.read_bytes("artifacts/a.zip") == b"first"
    assert storage.delete_if_owned("artifacts/a.zip", first.sha256) is True
    with pytest.raises(FileNotFoundError):
        storage.read_bytes("artifacts/a.zip")


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
    assert not list(moved.iterdir())
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
