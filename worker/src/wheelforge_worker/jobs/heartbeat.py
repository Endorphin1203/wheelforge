from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import Literal, Protocol

from .repository import JobLease, LostLeaseError


class _HeartbeatRepository(Protocol):
    def heartbeat(self, lease: JobLease) -> bool: ...


_Wait = Callable[[threading.Event, float], bool]


class LeaseHeartbeat:
    def __init__(
        self,
        repository: _HeartbeatRepository,
        lease: JobLease,
        *,
        interval_seconds: float,
        wait: _Wait | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._repository = repository
        self._lease = lease
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._wait = wait or _event_wait
        self._failure: BaseException | None = None
        self._failure_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name=f"wheelforge-heartbeat-{lease.id}",
            daemon=True,
        )
        self._started = False

    def __enter__(self) -> LeaseHeartbeat:
        self._thread.start()
        self._started = True
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self.stop()
        failure = self._get_failure()
        if failure is not None:
            if failure is exception:
                return False
            if exception is not None:
                raise BaseExceptionGroup(
                    "stage and lease heartbeat both failed",
                    [exception, failure],
                )
            raise failure
        return False

    def check(self) -> None:
        failure = self._get_failure()
        if failure is not None:
            raise failure

    def stop(self) -> None:
        self._stop.set()
        if not self._started:
            return
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            lifecycle_error = RuntimeError("lease heartbeat thread did not stop")
            with self._failure_lock:
                self._failure = self._failure or lifecycle_error

    def _run(self) -> None:
        try:
            while not self._wait(self._stop, self._interval_seconds):
                if not self._repository.heartbeat(self._lease):
                    raise LostLeaseError("job lease expired during stage execution")
        except BaseException as error:
            with self._failure_lock:
                self._failure = error
            self._stop.set()

    def _get_failure(self) -> BaseException | None:
        with self._failure_lock:
            return self._failure


def _event_wait(stop: threading.Event, seconds: float) -> bool:
    return stop.wait(seconds)
