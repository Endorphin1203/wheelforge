import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from wheelforge_worker.contracts import BuildStatus, JobPayload, JobStatus, JobType


FIXTURES = Path(__file__).parents[2] / "contracts" / "examples"
MUTABLE_ROW_FIELDS = {
    "id",
    "jobId",
    "executionId",
    "attempts",
    "leaseOwner",
    "leaseExpiresAt",
    "heartbeatAt",
}


def test_build_fixture_round_trips() -> None:
    raw = (FIXTURES / "build-v1.json").read_text()

    payload = JobPayload.model_validate_json(raw)

    assert payload.schema_version == 1
    assert payload.job_type is JobType.BUILD
    assert payload.payload["solveMode"] == "COMPATIBLE"
    assert payload.payload["targetSnapshot"]["architecture"] == "AARCH64"
    assert payload.payload["targetSnapshot"]["platformTag"] == "manylinux2014_aarch64"
    assert payload.payload["targetSnapshot"]["abiTags"] == ["cp311", "abi3", "none"]
    assert payload.payload["targetSnapshot"]["pythonVersion"] == "3.11"


def test_requirement_parse_fixture_round_trips() -> None:
    payload = JobPayload.model_validate_json(
        (FIXTURES / "requirement-parse-v1.json").read_text()
    )

    assert payload.job_type is JobType.REQUIREMENT_PARSE
    assert str(payload.payload["originalObjectKey"]).endswith("/original.txt")


def test_serializes_database_wire_names() -> None:
    payload = JobPayload.model_validate_json((FIXTURES / "build-v1.json").read_text())

    document = json.loads(payload.model_dump_json())

    assert document.keys() == {"schemaVersion", "jobType", "subjectId", "createdAt", "payload"}
    assert document["jobType"] == "BUILD"


def test_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValidationError, match="schemaVersion"):
        JobPayload.model_validate(
            {
                "schemaVersion": 2,
                "jobType": "BUILD",
                "subjectId": "fe3b9a09-e696-4104-beb7-d8fd1fb85d24",
                "createdAt": "2026-07-22T10:05:00Z",
                "payload": {},
            }
        )


def test_rejects_unsupported_job_type() -> None:
    with pytest.raises(ValidationError, match="jobType"):
        JobPayload.model_validate(
            {
                "schemaVersion": 1,
                "jobType": "EXPORT",
                "subjectId": "fe3b9a09-e696-4104-beb7-d8fd1fb85d24",
                "createdAt": "2026-07-22T10:05:00Z",
                "payload": {},
            }
        )


def test_rejects_malformed_subject_id() -> None:
    with pytest.raises(ValidationError, match="subjectId"):
        JobPayload.model_validate(
            {
                "schemaVersion": 1,
                "jobType": "BUILD",
                "subjectId": "not-a-uuid",
                "createdAt": "2026-07-22T10:05:00Z",
                "payload": {},
            }
        )


def test_rejects_created_at_without_timezone() -> None:
    with pytest.raises(ValidationError, match="createdAt must include a timezone"):
        JobPayload.model_validate(
            {
                "schemaVersion": 1,
                "jobType": "BUILD",
                "subjectId": "fe3b9a09-e696-4104-beb7-d8fd1fb85d24",
                "createdAt": "2026-07-22T10:05:00",
                "payload": {},
            }
        )


def test_fixture_payloads_exclude_mutable_database_row_fields() -> None:
    for fixture in FIXTURES.glob("*.json"):
        document = json.loads(fixture.read_text())

        assert MUTABLE_ROW_FIELDS.isdisjoint(document)


def test_keeps_build_task_and_database_job_state_sets_separate() -> None:
    assert list(BuildStatus) == [
        BuildStatus.CREATED,
        BuildStatus.PARSING,
        BuildStatus.QUEUED,
        BuildStatus.RESOLVING,
        BuildStatus.DOWNLOADING,
        BuildStatus.VALIDATING,
        BuildStatus.PACKAGING,
        BuildStatus.SUCCESS,
        BuildStatus.PARTIAL_SUCCESS,
        BuildStatus.FAILED,
        BuildStatus.CANCELLED,
    ]
    assert list(JobStatus) == [
        JobStatus.READY,
        JobStatus.RUNNING,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    ]
