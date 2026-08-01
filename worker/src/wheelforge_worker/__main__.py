from __future__ import annotations

import signal
import threading
import logging
from datetime import timedelta

from sqlalchemy import create_engine

from wheelforge_worker.jobs.consumer import JobConsumer
from wheelforge_worker.jobs.maintenance import MaintenanceService
from wheelforge_worker.jobs.pipeline import DefaultBuildStages, JobPipeline
from wheelforge_worker.jobs.repository import JobRepository
from wheelforge_worker.jobs.storage import (
    RootedLocalStorage,
    WorkspaceManager,
    require_external_workspace_support,
)
from wheelforge_worker.settings import Settings


_LOGGER = logging.getLogger(__name__)


def create_consumer(settings: Settings, stop: threading.Event) -> JobConsumer:
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=300,
    )
    repository = JobRepository(
        engine,
        lease_seconds=settings.job_lease_seconds,
    )
    storage: RootedLocalStorage | None = None
    workspaces: WorkspaceManager | None = None
    try:
        storage = RootedLocalStorage(settings.data_root)
        workspaces = WorkspaceManager(settings.workspace_root)
        if storage.root_identity == workspaces.root_identity:
            raise ValueError("WF_DATA_ROOT and WF_WORKSPACE_ROOT must be different")
        require_external_workspace_support()
        maintenance = MaintenanceService(
            repository,
            storage,
            workspaces,
            minimum_age=timedelta(seconds=settings.maintenance_age_seconds),
        )
        maintenance.run()
        pipeline = JobPipeline(
            repository,
            storage,
            workspaces,
            DefaultBuildStages(),
        )
        return JobConsumer(
            repository,
            pipeline,
            settings.worker_id,
            poll_seconds=settings.queue_poll_seconds,
            wait=stop.wait,
            maintenance=maintenance.run_if_due,
            close=lambda: _close_resources(workspaces, storage, engine),
        )
    except BaseException:
        _close_resources_quietly(workspaces, storage, engine)
        raise


def main() -> int:
    settings = Settings.from_env()
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    consumer = create_consumer(settings, stop)
    try:
        consumer.run_forever()
    finally:
        try:
            consumer.close()
        except Exception:
            _LOGGER.exception("worker resource cleanup failed")
    return 0


def _close_resources(
    workspaces: WorkspaceManager | None,
    storage: RootedLocalStorage | None,
    engine: object,
) -> None:
    errors: list[Exception] = []
    for resource in (workspaces, storage, engine):
        if resource is None:
            continue
        operation = getattr(resource, "dispose", None) or getattr(resource, "close")
        try:
            operation()
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("worker resource cleanup failed", errors)


def _close_resources_quietly(
    workspaces: WorkspaceManager | None,
    storage: RootedLocalStorage | None,
    engine: object,
) -> None:
    try:
        _close_resources(workspaces, storage, engine)
    except Exception:
        _LOGGER.exception("startup resource cleanup failed")


if __name__ == "__main__":
    raise SystemExit(main())
