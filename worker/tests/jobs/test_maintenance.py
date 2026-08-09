from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wheelforge_worker.jobs.maintenance import MaintenanceService, MaintenanceSnapshot
from wheelforge_worker.jobs.storage import RootedLocalStorage, WorkspaceManager
from wheelforge_worker.jobs.storage import StorageSweepStats


NOW = datetime(2026, 8, 1, 8, 0, 0)
ACTIVE = "10000000-0000-4000-8000-000000000001"
STALE = "10000000-0000-4000-8000-000000000002"
TASK = "20000000-0000-4000-8000-000000000001"
OTHER_TASK = "20000000-0000-4000-8000-000000000002"
REFERENCED = f"artifacts/{TASK}/{STALE}/30000000-0000-4000-8000-000000000001.zip"
ACTIVE_ARTIFACT = f"artifacts/{TASK}/{ACTIVE}/30000000-0000-4000-8000-000000000002.zip"
ORPHAN = f"artifacts/{OTHER_TASK}/{STALE}/30000000-0000-4000-8000-000000000003.zip"
STALE_SAME_TASK = (
    f"artifacts/{TASK}/{STALE}/30000000-0000-4000-8000-000000000004.zip"
)


class SnapshotRepository:
    def maintenance_snapshot(self) -> MaintenanceSnapshot:
        return MaintenanceSnapshot(
            frozenset({ACTIVE}),
            frozenset({(TASK, ACTIVE)}),
            frozenset({REFERENCED}),
        )


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
    active_artifact = storage.publish_bytes(ACTIVE_ARTIFACT, b"active")
    orphan = storage.publish_bytes(ORPHAN, b"orphan")
    active_stage = data_root / f".wf-stage-{ACTIVE}-40000000-0000-4000-8000-000000000001"
    active_stage.write_bytes(b"active stage")
    stale_stage = data_root / f".wf-stage-{STALE}-40000000-0000-4000-8000-000000000002"
    stale_stage.write_bytes(b"stale stage")
    unrelated = data_root / "keep.txt"
    unrelated.write_text("keep")
    old = (NOW - timedelta(days=2)).timestamp()
    for path in (
        active.path,
        stale.path,
        data_root / referenced.object_key,
        data_root / active_artifact.object_key,
        data_root / orphan.object_key,
        active_stage,
        stale_stage,
    ):
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
    assert (data_root / active_artifact.object_key).exists()
    assert not (data_root / orphan.object_key).exists()
    assert active_stage.exists()
    assert not stale_stage.exists()
    assert unrelated.read_text() == "keep"


def test_active_successor_does_not_protect_prior_execution_orphan(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir(mode=0o700)
    workspace_root.mkdir(mode=0o700)
    storage = RootedLocalStorage(data_root)
    active = storage.publish_bytes(ACTIVE_ARTIFACT, b"active")
    stale = storage.publish_bytes(STALE_SAME_TASK, b"stale")
    old = (NOW - timedelta(days=2)).timestamp()
    os.utime(data_root / active.object_key, (old, old))
    os.utime(data_root / stale.object_key, (old, old))

    MaintenanceService(
        SnapshotRepository(),
        storage,
        WorkspaceManager(workspace_root),
        minimum_age=timedelta(days=1),
        clock=lambda: NOW,
    ).run()

    assert (data_root / active.object_key).read_bytes() == b"active"
    assert not (data_root / stale.object_key).exists()


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
        frozenset(), frozenset(), frozenset({REFERENCED})
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


def test_periodic_maintenance_reconsiders_files_that_age_after_startup(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir(mode=0o700)
    workspace_root.mkdir(mode=0o700)
    storage = RootedLocalStorage(data_root)
    workspaces = WorkspaceManager(workspace_root)
    orphan = storage.publish_bytes(ORPHAN, b"orphan")
    path = data_root / orphan.object_key
    created = (NOW - timedelta(minutes=30)).timestamp()
    os.utime(path, (created, created))
    clock = [NOW]
    repository = SnapshotRepository()
    repository.maintenance_snapshot = lambda: MaintenanceSnapshot(
        frozenset({ACTIVE}), frozenset({(TASK, ACTIVE)}), frozenset()
    )
    service = MaintenanceService(
        repository,
        storage,
        workspaces,
        minimum_age=timedelta(hours=1),
        interval=timedelta(minutes=10),
        clock=lambda: clock[0],
    )

    assert service.run_if_due() is True
    assert path.exists()
    clock[0] = NOW + timedelta(minutes=5)
    assert service.run_if_due() is False
    assert path.exists()
    clock[0] = NOW + timedelta(minutes=40)
    assert service.run_if_due() is True
    assert not path.exists()


def test_failed_periodic_maintenance_is_backed_off_before_retry(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir(mode=0o700)
    workspace_root.mkdir(mode=0o700)
    calls = 0

    class FlakyRepository:
        def maintenance_snapshot(self) -> MaintenanceSnapshot:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary metadata failure")
            return MaintenanceSnapshot(frozenset(), frozenset(), frozenset())

    clock = [NOW]
    service = MaintenanceService(
        FlakyRepository(),
        RootedLocalStorage(data_root),
        WorkspaceManager(workspace_root),
        minimum_age=timedelta(hours=1),
        interval=timedelta(hours=1),
        failure_backoff=timedelta(minutes=10),
        clock=lambda: clock[0],
    )

    with pytest.raises(OSError, match="temporary metadata failure"):
        service.run_if_due()
    clock[0] = NOW + timedelta(minutes=9)
    assert service.run_if_due() is False
    assert calls == 1
    clock[0] = NOW + timedelta(minutes=10)
    assert service.run_if_due() is True
    assert calls == 2


def test_maintenance_publishes_bounded_stats_and_rate_limits_operational_signal(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    data_root.mkdir()
    workspace_root.mkdir()
    storage = RootedLocalStorage(data_root)
    summaries: list[object] = []
    signals: list[object] = []
    clock = [NOW]

    def sweep(*_args: object, **_kwargs: object) -> StorageSweepStats:
        return StorageSweepStats(
            quarantine_conflicts=2,
            invalid_quarantines=3,
            quarantines_examined=5,
            bytes_hashed=128,
            budget_exhausted=True,
        )

    storage.sweep_abandoned = sweep  # type: ignore[method-assign]
    service = MaintenanceService(
        SnapshotRepository(), storage, WorkspaceManager(workspace_root),
        minimum_age=timedelta(hours=1), interval=timedelta(minutes=1),
        failure_backoff=timedelta(seconds=30),
        clock=lambda: clock[0], observer=summaries.append,
        operational_signal=signals.append,
        signal_interval=timedelta(hours=1),
    )
    service.run()
    clock[0] += timedelta(minutes=2)
    service.run_if_due()

    assert len(summaries) == 2
    assert len(signals) == 1
    summary = summaries[0]
    assert vars(summary) if not hasattr(summary, "__slots__") else True
    assert summary.quarantine_conflicts == 2  # type: ignore[attr-defined]
    assert summary.invalid_quarantines == 3  # type: ignore[attr-defined]
    assert len(repr(summary)) <= 512
