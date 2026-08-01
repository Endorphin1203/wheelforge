from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .storage import RootedLocalStorage, WorkspaceManager


@dataclass(frozen=True, slots=True)
class MaintenanceSnapshot:
    active_execution_ids: frozenset[str]
    active_build_executions: frozenset[tuple[str, str]]
    referenced_artifact_keys: frozenset[str]


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
    ) -> None:
        if minimum_age < timedelta(hours=1):
            raise ValueError("maintenance minimum age must be at least one hour")
        if interval <= timedelta(0):
            raise ValueError("maintenance interval must be positive")
        if failure_backoff <= timedelta(0) or failure_backoff > interval:
            raise ValueError(
                "maintenance failure backoff must be positive and no greater than interval"
            )
        self._repository = repository
        self._storage = storage
        self._workspaces = workspaces
        self._minimum_age = minimum_age
        self._interval = interval
        self._failure_backoff = failure_backoff
        self._clock = clock
        self._next_run_at: datetime | None = None

    def run(self) -> None:
        self._run_at(self._clock())

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

    def _run_at(self, now: datetime) -> None:
        snapshot = self._repository.maintenance_snapshot()
        cutoff = now - self._minimum_age
        self._workspaces.sweep_abandoned(
            cutoff, snapshot.active_execution_ids
        )
        self._storage.sweep_abandoned(
            cutoff,
            snapshot.referenced_artifact_keys,
            active_execution_ids=snapshot.active_execution_ids,
            active_build_executions=snapshot.active_build_executions,
        )
        self._next_run_at = now + self._interval
