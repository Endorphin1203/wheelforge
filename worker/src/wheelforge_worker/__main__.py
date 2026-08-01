from __future__ import annotations

import signal
import threading
import os
from datetime import timedelta

from sqlalchemy import create_engine

from wheelforge_worker.jobs.consumer import JobConsumer
from wheelforge_worker.jobs.maintenance import MaintenanceService
from wheelforge_worker.jobs.pipeline import DefaultBuildStages, JobPipeline
from wheelforge_worker.jobs.repository import JobRepository
from wheelforge_worker.jobs.storage import RootedLocalStorage, WorkspaceManager
from wheelforge_worker.settings import Settings


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
    storage = RootedLocalStorage(settings.data_root)
    workspaces = WorkspaceManager(settings.workspace_root)
    if os.path.samefile(storage.root, workspaces.root):
        raise ValueError("WF_DATA_ROOT and WF_WORKSPACE_ROOT must be different")
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
    )


def main() -> int:
    settings = Settings.from_env()
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    create_consumer(settings, stop).run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
