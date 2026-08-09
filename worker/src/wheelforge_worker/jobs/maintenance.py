from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Protocol

from .storage import RootedLocalStorage, StorageSweepStats, WorkspaceManager


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MaintenanceSnapshot:
    active_execution_ids: frozenset[str]
    active_build_executions: frozenset[tuple[str, str]]
    referenced_artifact_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
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
        self._workspaces.sweep_abandoned(
            cutoff, snapshot.active_execution_ids
        )
        stats = self._storage.sweep_abandoned(
            cutoff,
            snapshot.referenced_artifact_keys,
            active_execution_ids=snapshot.active_execution_ids,
            active_build_executions=snapshot.active_build_executions,
        )
        summary = _summary(stats)
        self._observer(summary)
        if (
            summary.quarantine_conflicts + summary.invalid_quarantines > 0
            and (self._next_signal_at is None or now >= self._next_signal_at)
        ):
            self._operational_signal(summary)
            self._next_signal_at = now + self._signal_interval
        self._next_run_at = now + self._interval
        return summary


def _summary(stats: StorageSweepStats) -> MaintenanceSummary:
    return MaintenanceSummary(
        stats.quarantines_restored,
        stats.quarantines_protected,
        stats.quarantine_conflicts,
        stats.invalid_quarantines,
        stats.empty_quarantines_removed,
        stats.quarantines_examined,
        stats.bytes_hashed,
        stats.budget_exhausted,
        stats.directories_scanned,
        stats.entries_scanned,
        stats.mutations,
    )


def _log_operational_signal(summary: MaintenanceSummary) -> None:
    _LOGGER.warning(
        "storage quarantine maintenance requires attention: conflicts=%d invalid=%d",
        summary.quarantine_conflicts,
        summary.invalid_quarantines,
    )
