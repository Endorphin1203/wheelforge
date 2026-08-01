from __future__ import annotations

from dataclasses import dataclass

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
