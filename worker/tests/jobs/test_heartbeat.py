from __future__ import annotations

from datetime import timedelta
from threading import Event

import pytest

from wheelforge_worker.jobs.heartbeat import LeaseHeartbeat

from .test_repository import NOW, _repository


def test_long_stage_renews_lease_before_it_can_be_reclaimed() -> None:
    repository = _repository()
    lease = repository.claim_next("worker-a")
    assert lease is not None
    heartbeat_finished = Event()
    original_heartbeat = repository.heartbeat

    def observed_heartbeat(active_lease: object) -> bool:
        result = original_heartbeat(active_lease)  # type: ignore[arg-type]
        heartbeat_finished.set()
        return result

    repository.heartbeat = observed_heartbeat  # type: ignore[method-assign]
    wait_calls = 0

    def controlled_wait(stop: Event, _seconds: float) -> bool:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            repository._clock = lambda: NOW + timedelta(seconds=50)
            return False
        return stop.wait(1)

    with LeaseHeartbeat(
        repository,
        lease,
        interval_seconds=20,
        wait=controlled_wait,
    ):
        assert heartbeat_finished.wait(1)
        repository._clock = lambda: NOW + timedelta(seconds=61)
        assert repository.claim_next("worker-b") is None


def test_heartbeat_thread_exception_is_raised_in_stage_thread() -> None:
    repository = _repository()
    lease = repository.claim_next("worker-a")
    assert lease is not None
    failed = Event()

    def explode(_lease: object) -> bool:
        failed.set()
        raise RuntimeError("heartbeat database failure")

    repository.heartbeat = explode  # type: ignore[method-assign]

    def immediate_wait(_stop: Event, _seconds: float) -> bool:
        return False

    with pytest.raises(RuntimeError, match="heartbeat database failure"):
        with LeaseHeartbeat(
            repository,
            lease,
            interval_seconds=20,
            wait=immediate_wait,
        ) as heartbeat:
            assert failed.wait(1)
            heartbeat.check()
