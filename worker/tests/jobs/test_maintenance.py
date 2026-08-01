from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from wheelforge_worker.jobs.maintenance import MaintenanceService, MaintenanceSnapshot
from wheelforge_worker.jobs.storage import RootedLocalStorage, WorkspaceManager


NOW = datetime(2026, 8, 1, 8, 0, 0)
ACTIVE = "10000000-0000-4000-8000-000000000001"
STALE = "10000000-0000-4000-8000-000000000002"
TASK = "20000000-0000-4000-8000-000000000001"
REFERENCED = f"artifacts/{TASK}/30000000-0000-4000-8000-000000000001.zip"
ORPHAN = f"artifacts/{TASK}/30000000-0000-4000-8000-000000000002.zip"


class SnapshotRepository:
    def maintenance_snapshot(self) -> MaintenanceSnapshot:
        return MaintenanceSnapshot(frozenset({ACTIVE}), frozenset({REFERENCED}))


def test_startup_maintenance_removes_only_old_proven_abandoned_entries(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir(mode=0o700)
    workspace_root.mkdir(mode=0o700)
    storage = RootedLocalStorage(data_root)
    workspaces = WorkspaceManager(workspace_root)
    active = workspaces.allocate(ACTIVE)
    stale = workspaces.allocate(STALE)
    referenced = storage.publish_bytes(REFERENCED, b"referenced")
    orphan = storage.publish_bytes(ORPHAN, b"orphan")
    stage = data_root / ".wf-stage-40000000-0000-4000-8000-000000000001"
    stage.write_bytes(b"stage")
    unrelated = data_root / "keep.txt"
    unrelated.write_text("keep")
    old = (NOW - timedelta(days=2)).timestamp()
    for path in (active.path, stale.path, data_root / referenced.object_key, data_root / orphan.object_key, stage):
        os.utime(path, (old, old))

    service = MaintenanceService(
        SnapshotRepository(),
        storage,
        workspaces,
        minimum_age=timedelta(days=1),
        clock=lambda: NOW,
    )
    service.run()

    assert active.path.exists()
    assert not stale.path.exists()
    assert (data_root / referenced.object_key).exists()
    # Artifact cleanup is deferred while any execution is live.
    assert (data_root / orphan.object_key).exists()
    # Stage files cannot be associated with one execution, so cleanup defers globally.
    assert stage.exists()
    assert unrelated.read_text() == "keep"


def test_orphan_artifact_is_removed_when_no_execution_is_live(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir(mode=0o700)
    workspace_root.mkdir(mode=0o700)
    storage = RootedLocalStorage(data_root)
    workspaces = WorkspaceManager(workspace_root)
    orphan = storage.publish_bytes(ORPHAN, b"orphan")
    path = data_root / orphan.object_key
    stage = data_root / ".wf-stage-40000000-0000-4000-8000-000000000001"
    stage.write_bytes(b"stage")
    old = (NOW - timedelta(days=2)).timestamp()
    os.utime(path, (old, old))
    os.utime(stage, (old, old))

    repository = SnapshotRepository()
    repository.maintenance_snapshot = lambda: MaintenanceSnapshot(
        frozenset(), frozenset({REFERENCED})
    )
    MaintenanceService(
        repository,
        storage,
        workspaces,
        minimum_age=timedelta(days=1),
        clock=lambda: NOW,
    ).run()

    assert not path.exists()
    assert not stage.exists()
