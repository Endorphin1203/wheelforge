from __future__ import annotations

from dataclasses import dataclass, replace

from wheelforge_worker.contracts import BuildStatus
from wheelforge_worker.jobs.consumer import JobConsumer
from wheelforge_worker.jobs.pipeline import PipelineResult
from wheelforge_worker.jobs.repository import JobLease, LostLeaseError


LEASE = JobLease(
    id="10000000-0000-4000-8000-000000000001",
    job_type="BUILD",
    subject_id="20000000-0000-4000-8000-000000000001",
    payload_version=1,
    payload_json="{}",
    worker_id="worker-a",
    execution_id="30000000-0000-4000-8000-000000000001",
    attempts=1,
    max_attempts=3,
)


@dataclass
class FakeRepository:
    claimed: JobLease | None
    failures: list[tuple[JobLease, str, bool]]

    def claim_next(self, worker_id: str) -> JobLease | None:
        assert worker_id == "worker-a"
        result = self.claimed
        self.claimed = None
        return result

    def retry_or_fail(self, lease: JobLease, error: str, *, retryable: bool) -> bool:
        self.failures.append((lease, error, retryable))
        return True


class RecordingPipeline:
    def __init__(self, error: BaseException | None = None) -> None:
        self.leases: list[JobLease] = []
        self.error = error

    def run(self, lease: JobLease) -> PipelineResult:
        self.leases.append(lease)
        if self.error is not None:
            raise self.error
        return PipelineResult(BuildStatus.SUCCESS, "artifact-id")


def test_run_once_claims_and_dispatches_exact_lease() -> None:
    repository = FakeRepository(LEASE, [])
    pipeline = RecordingPipeline()
    consumer = JobConsumer(repository, pipeline, "worker-a", poll_seconds=2)

    assert consumer.run_once() is True
    assert pipeline.leases == [LEASE]
    assert repository.failures == []


def test_run_once_returns_false_for_empty_queue() -> None:
    repository = FakeRepository(None, [])
    consumer = JobConsumer(repository, RecordingPipeline(), "worker-a", poll_seconds=2)

    assert consumer.run_once() is False


def test_unexpected_pipeline_error_returns_owned_job_for_retry() -> None:
    repository = FakeRepository(LEASE, [])
    consumer = JobConsumer(
        repository,
        RecordingPipeline(RuntimeError("database connection interrupted")),
        "worker-a",
        poll_seconds=2,
    )

    assert consumer.run_once() is True
    assert repository.failures == [
        (LEASE, "RuntimeError: database connection interrupted", True)
    ]


def test_unexpected_pipeline_error_is_sanitized_before_persistence() -> None:
    repository = FakeRepository(LEASE, [])
    consumer = JobConsumer(
        repository,
        RecordingPipeline(
            ExceptionGroup(
                "consumer",
                [RuntimeError("mysql://user:secret@db/wf?token=hidden")],
            )
        ),
        "worker-a",
        poll_seconds=2,
    )

    assert consumer.run_once() is True
    persisted = repository.failures[0][1]
    assert "secret" not in persisted
    assert "hidden" not in persisted
    assert "RuntimeError" in persisted


def test_consumer_does_not_mutate_state_for_nested_lost_lease() -> None:
    repository = FakeRepository(LEASE, [])
    consumer = JobConsumer(
        repository,
        RecordingPipeline(
            ExceptionGroup(
                "lost ownership",
                [OSError("network"), LostLeaseError("lease expired")],
            )
        ),
        "worker-a",
        poll_seconds=2,
    )

    assert consumer.run_once() is True
    assert repository.failures == []


def test_run_forever_waits_only_when_queue_is_empty() -> None:
    repository = FakeRepository(None, [])
    waits: list[float] = []

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return True

    consumer = JobConsumer(
        repository,
        RecordingPipeline(),
        "worker-a",
        poll_seconds=3,
        wait=wait,
    )

    consumer.run_forever()

    assert waits == [3]


def test_run_forever_runs_periodic_maintenance_while_processing_jobs() -> None:
    repository = FakeRepository(LEASE, [])
    maintenance_calls: list[None] = []

    consumer = JobConsumer(
        repository,
        RecordingPipeline(),
        "worker-a",
        poll_seconds=3,
        wait=lambda _seconds: True,
        maintenance=lambda: maintenance_calls.append(None),
    )

    consumer.run_forever()

    assert len(maintenance_calls) == 2


def test_periodic_maintenance_failure_is_sanitized_backed_off_and_does_not_stop_jobs() -> None:
    repository = FakeRepository(LEASE, [])
    pipeline = RecordingPipeline()
    maintenance_calls = 0
    errors: list[str] = []
    waits: list[float] = []

    def maintenance() -> None:
        nonlocal maintenance_calls
        maintenance_calls += 1
        if maintenance_calls == 1:
            raise OSError(
                "mysql://worker:secret@db/wf?token=hidden " + "x" * 10_000
            )

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return len(waits) == 2

    consumer = JobConsumer(
        repository,
        pipeline,
        "worker-a",
        poll_seconds=3,
        wait=wait,
        maintenance=maintenance,
        maintenance_error=errors.append,
    )

    consumer.run_forever()

    assert pipeline.leases == [LEASE]
    assert waits == [3, 3]
    assert len(errors) == 1
    assert len(errors[0]) <= 2048
    assert "secret" not in errors[0]
    assert "hidden" not in errors[0]


def test_maintenance_failure_after_processed_job_does_not_sleep_before_next_claim() -> None:
    second = replace(LEASE, id="00000000-0000-4000-8000-000000000099")
    leases = [LEASE, second]

    class QueueRepository:
        def claim_next(self, worker_id: str) -> JobLease | None:
            assert worker_id == "worker-a"
            return leases.pop(0) if leases else None

        def retry_or_fail(
            self, lease: JobLease, error: str, *, retryable: bool
        ) -> bool:
            raise AssertionError((lease, error, retryable))

    repository = QueueRepository()
    pipeline = RecordingPipeline()
    maintenance_calls = 0
    waits: list[float] = []

    def maintenance() -> None:
        nonlocal maintenance_calls
        maintenance_calls += 1
        if maintenance_calls == 1:
            raise OSError("temporary maintenance failure")

    consumer = JobConsumer(
        repository,
        pipeline,
        "worker-a",
        poll_seconds=3,
        wait=lambda seconds: waits.append(seconds) or True,
        maintenance=maintenance,
    )

    consumer.run_forever()

    assert pipeline.leases == [LEASE, second]
    assert waits == [3]


def test_consumer_close_releases_owned_resources_once() -> None:
    closes: list[None] = []
    consumer = JobConsumer(
        FakeRepository(None, []),
        RecordingPipeline(),
        "worker-a",
        poll_seconds=3,
        close=lambda: closes.append(None),
    )

    consumer.close()
    consumer.close()

    assert closes == [None]
