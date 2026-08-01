from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .storage import RootedLocalStorage, WorkspaceManager


@dataclass(frozen=True, slots=True)
class MaintenanceSnapshot:
    active_execution_ids: frozenset[str]
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
        clock: Callable[[], datetime] = datetime.utcnow,
    ) -> None:
        if minimum_age < timedelta(hours=1):
            raise ValueError("maintenance minimum age must be at least one hour")
        self._repository = repository
        self._storage = storage
        self._workspaces = workspaces
        self._minimum_age = minimum_age
        self._clock = clock

    def run(self) -> None:
        snapshot = self._repository.maintenance_snapshot()
        cutoff = self._clock() - self._minimum_age
        self._workspaces.sweep_abandoned(
            cutoff, snapshot.active_execution_ids
        )
        self._storage.sweep_abandoned(
            cutoff,
            snapshot.referenced_artifact_keys,
            active_execution_ids=snapshot.active_execution_ids,
        )
