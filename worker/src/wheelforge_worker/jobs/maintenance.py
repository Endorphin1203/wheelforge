from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Protocol

from .storage import (
    RootedLocalStorage,
    StorageSweepStats,
    WorkspaceManager,
    WorkspaceSweepStats,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MaintenanceSnapshot:
    active_execution_ids: frozenset[str]
    active_build_executions: frozenset[tuple[str, str]]
    referenced_artifact_keys: frozenset[str]


@dataclass(frozen=True, slots=True, repr=False)
class MaintenanceSummary:
    quarantines_restored: int
    quarantines_protected: int
    quarantine_conflicts: int
    invalid_quarantines: int
    empty_quarantines_removed: int
    quarantines_examined: int
    bytes_hashed: int
    budget_exhausted: bool
    directories_scanned: int
    entries_scanned: int
    mutations: int
    data_lock_contended: bool
    workspace_lock_contended: bool
    data_budget_exhausted: bool
    workspace_budget_exhausted: bool
    progress_made: bool
    data_backlog_entries: int
    workspace_backlog_entries: int
    oversized_quarantines: int
    quarantines_held: int
    workspaces_held: int
    invalid_workspaces: int
    workspaces_examined: int
    workspaces_removed: int
    workspace_entries_scanned: int
    workspace_mutations: int
    data_queue_record_bytes: int
    workspace_queue_record_bytes: int
    data_queue_segments: int
    workspace_queue_segments: int
    data_queue_capacity_events: int
    workspace_queue_capacity_events: int

    def __repr__(self) -> str:
        return (
            "MaintenanceSummary("
            f"restored={self.quarantines_restored},"
            f"protected={self.quarantines_protected},"
            f"conflicts={self.quarantine_conflicts},"
            f"invalid={self.invalid_quarantines},"
            f"held={self.quarantines_held},"
            f"oversized={self.oversized_quarantines},"
            f"data_backlog={self.data_backlog_entries},"
            f"workspace_backlog={self.workspace_backlog_entries},"
            f"workspace_held={self.workspaces_held},"
            f"workspace_invalid={self.invalid_workspaces},"
            f"capacity_events={self.data_queue_capacity_events + self.workspace_queue_capacity_events},"
            f"lock_contended={self.data_lock_contended or self.workspace_lock_contended},"
            f"budget_exhausted={self.budget_exhausted},"
            f"progress={self.progress_made})"
        )


class _Repository(Protocol):
    def maintenance_snapshot(self) -> MaintenanceSnapshot: ...


class MaintenanceService:
    def __init__(
        self,
        repository: _Repository,
        storage: RootedLocalStorage,
        workspaces: WorkspaceManager,
        *,
        minimum_age: timedelta,
        interval: timedelta = timedelta(hours=1),
        failure_backoff: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = datetime.utcnow,
        observer: Callable[[MaintenanceSummary], object] | None = None,
        operational_signal: Callable[[MaintenanceSummary], object] | None = None,
        signal_interval: timedelta = timedelta(hours=1),
    ) -> None:
        if minimum_age < timedelta(hours=1):
            raise ValueError("maintenance minimum age must be at least one hour")
        if interval <= timedelta(0):
            raise ValueError("maintenance interval must be positive")
        if failure_backoff <= timedelta(0) or failure_backoff > interval:
            raise ValueError(
                "maintenance failure backoff must be positive and no greater than interval"
            )
        if signal_interval <= timedelta(0):
            raise ValueError("maintenance signal interval must be positive")
        self._repository = repository
        self._storage = storage
        self._workspaces = workspaces
        self._minimum_age = minimum_age
        self._interval = interval
        self._failure_backoff = failure_backoff
        self._clock = clock
        self._observer = observer or (lambda _summary: None)
        self._operational_signal = operational_signal or _log_operational_signal
        self._signal_interval = signal_interval
        self._next_signal_at: datetime | None = None
        self._next_run_at: datetime | None = None
        self._stalled_runs = 0

    def run(self) -> MaintenanceSummary:
        return self._run_at(self._clock())

    def run_if_due(self) -> bool:
        now = self._clock()
        if self._next_run_at is not None and now < self._next_run_at:
            return False
        try:
            self._run_at(now)
        except Exception:
            self._next_run_at = now + self._failure_backoff
            raise
        return True

    def _run_at(self, now: datetime) -> MaintenanceSummary:
        snapshot = self._repository.maintenance_snapshot()
        cutoff = now - self._minimum_age
        workspace_stats = self._workspaces.sweep_abandoned(
            cutoff, snapshot.active_execution_ids
        )
        stats = self._storage.sweep_abandoned(
            cutoff,
            snapshot.referenced_artifact_keys,
            active_execution_ids=snapshot.active_execution_ids,
            active_build_executions=snapshot.active_build_executions,
        )
        summary = _summary(stats, workspace_stats)
        self._observer(summary)
        if (
            summary.data_backlog_entries + summary.workspace_backlog_entries > 0
            and not summary.progress_made
        ):
            self._stalled_runs += 1
        else:
            self._stalled_runs = 0
        if (
            _requires_operational_signal(summary, self._stalled_runs)
            and (self._next_signal_at is None or now >= self._next_signal_at)
        ):
            self._operational_signal(summary)
            self._next_signal_at = now + self._signal_interval
        self._next_run_at = now + self._interval
        return summary


def _summary(
    stats: StorageSweepStats, workspace: WorkspaceSweepStats
) -> MaintenanceSummary:
    return MaintenanceSummary(
        stats.quarantines_restored,
        stats.quarantines_protected,
        stats.quarantine_conflicts,
        stats.invalid_quarantines,
        stats.empty_quarantines_removed,
        stats.quarantines_examined,
        stats.bytes_hashed,
        stats.budget_exhausted or workspace.budget_exhausted,
        stats.directories_scanned,
        stats.entries_scanned,
        stats.mutations,
        stats.lock_contended,
        workspace.lock_contended,
        stats.budget_exhausted,
        workspace.budget_exhausted,
        stats.progress_made or workspace.progress_made,
        stats.backlog_entries,
        workspace.backlog_entries,
        stats.oversized_quarantines,
        stats.quarantines_held,
        workspace.workspaces_held,
        workspace.invalid_workspaces,
        workspace.workspaces_examined,
        workspace.workspaces_removed,
        workspace.entries_scanned,
        workspace.mutations,
        stats.queue_record_bytes,
        workspace.queue_record_bytes,
        stats.queue_segments,
        workspace.queue_segments,
        stats.queue_capacity_events,
        workspace.queue_capacity_events,
    )


def _requires_operational_signal(
    summary: MaintenanceSummary, stalled_runs: int
) -> bool:
    return any(
        (
            summary.quarantine_conflicts + summary.invalid_quarantines > 0,
            summary.invalid_workspaces > 0,
            summary.data_lock_contended,
            summary.workspace_lock_contended,
            summary.oversized_quarantines > 0,
            summary.quarantines_held > 0,
            summary.workspaces_held > 0,
            summary.data_queue_capacity_events > 0,
            summary.workspace_queue_capacity_events > 0,
            stalled_runs >= 2,
        )
    )


def _log_operational_signal(summary: MaintenanceSummary) -> None:
    _LOGGER.warning(
        "storage maintenance requires attention: conflicts=%d invalid=%d "
        "held=%d workspace_invalid=%d workspace_held=%d data_backlog=%d "
        "workspace_backlog=%d capacity_events=%d",
        summary.quarantine_conflicts,
        summary.invalid_quarantines,
        summary.quarantines_held,
        summary.invalid_workspaces,
        summary.workspaces_held,
        summary.data_backlog_entries,
        summary.workspace_backlog_entries,
        summary.data_queue_capacity_events
        + summary.workspace_queue_capacity_events,
    )
