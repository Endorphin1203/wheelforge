from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from .errors import contains_exception, sanitize_error
from .pipeline import JobPipeline, PipelineResult
from .repository import JobLease, JobRepository, LostLeaseError


class _Repository(Protocol):
    def claim_next(self, worker_id: str) -> JobLease | None: ...

    def retry_or_fail(
        self, lease: JobLease, error: str, *, retryable: bool
    ) -> bool: ...


class _Pipeline(Protocol):
    def run(self, lease: JobLease) -> PipelineResult: ...


class JobConsumer:
    def __init__(
        self,
        repository: _Repository,
        pipeline: _Pipeline,
        worker_id: str,
        *,
        poll_seconds: int,
        wait: Callable[[float], bool | None] = time.sleep,
        maintenance: Callable[[], object] | None = None,
    ) -> None:
        if not worker_id or len(worker_id) > 100:
            raise ValueError("worker_id must contain at most 100 characters")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._repository = repository
        self._pipeline = pipeline
        self._worker_id = worker_id
        self._poll_seconds = poll_seconds
        self._wait = wait
        self._maintenance = maintenance or _no_maintenance

    def run_once(self) -> bool:
        lease = self._repository.claim_next(self._worker_id)
        if lease is None:
            return False
        try:
            self._pipeline.run(lease)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            if contains_exception(error, LostLeaseError):
                return True
            self._repository.retry_or_fail(
                lease, sanitize_error(error), retryable=True
            )
        return True

    def run_forever(self) -> None:
        while True:
            self._maintenance()
            if self.run_once():
                continue
            if self._wait(float(self._poll_seconds)):
                return


def build_consumer(
    repository: JobRepository,
    pipeline: JobPipeline,
    worker_id: str,
    poll_seconds: int,
    wait: Callable[[float], bool | None] = time.sleep,
) -> JobConsumer:
    return JobConsumer(
        repository,
        pipeline,
        worker_id,
        poll_seconds=poll_seconds,
        wait=wait,
    )


def _no_maintenance() -> None:
    pass
