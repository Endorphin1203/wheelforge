import json
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from wheelforge_worker.contracts import BuildStatus, JobPayload, JobStatus, JobType


FIXTURES = Path(__file__).parents[2] / "contracts" / "examples"
INVALID_FIXTURES = FIXTURES / "invalid"
VALID_FIXTURES = FIXTURES / "valid"
SCHEMA = Path(__file__).parents[2] / "contracts" / "job-payload-v1.schema.json"
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


def test_constructor_accepts_snake_case_fields() -> None:
    payload = JobPayload(
        schema_version=1,
        job_type=JobType.BUILD,
        subject_id="fe3b9a09-e696-4104-beb7-d8fd1fb85d24",
        created_at="2026-07-22T10:05:00Z",
        payload={},
    )

    assert payload.schema_version == 1
    assert payload.job_type is JobType.BUILD


def test_serializes_database_wire_names() -> None:
    payload = JobPayload.model_validate_json((FIXTURES / "build-v1.json").read_text())

    document = json.loads(payload.model_dump_json())

    assert document.keys() == {"schemaVersion", "jobType", "subjectId", "createdAt", "payload"}
    assert document["jobType"] == "BUILD"


def test_database_wire_parser_rejects_non_default_options() -> None:
    with pytest.raises(ValueError, match="fixed database wire settings"):
        JobPayload.model_validate_json(
            (FIXTURES / "build-v1.json").read_text(), strict=True
        )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_database_wire_parser_rejects_non_standard_json_constants(
    constant: str,
) -> None:
    with pytest.raises(ValueError, match="non-standard JSON constant"):
        JobPayload.model_validate_json(
            """
            {"schemaVersion":1,"jobType":"BUILD","subjectId":"fe3b9a09-e696-4104-beb7-d8fd1fb85d24","createdAt":"2026-07-22T10:05:00Z","payload":{"value":%s}}
            """
            % constant
        )


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
    with pytest.raises(ValidationError, match="RFC3339 date-time string with a timezone"):
        JobPayload.model_validate(
            {
                "schemaVersion": 1,
                "jobType": "BUILD",
                "subjectId": "fe3b9a09-e696-4104-beb7-d8fd1fb85d24",
                "createdAt": "2026-07-22T10:05:00",
                "payload": {},
            }
        )


@pytest.mark.parametrize("fixture", sorted(INVALID_FIXTURES.glob("*.json")))
def test_rejects_invalid_shared_fixture(fixture: Path) -> None:
    with pytest.raises(ValidationError):
        JobPayload.model_validate_json(fixture.read_text())


@pytest.mark.parametrize("fixture", sorted(VALID_FIXTURES.glob("*.json")))
def test_database_wire_parser_accepts_valid_shared_fixture(fixture: Path) -> None:
    assert JobPayload.model_validate_json(fixture.read_text()).schema_version == 1


def test_schema_accepts_valid_shared_fixtures() -> None:
    validator = schema_validator()

    for fixture in valid_shared_fixtures():
        assert list(validator.iter_errors(load_json_fixture(fixture))) == []


def test_schema_rejects_invalid_shared_fixtures() -> None:
    validator = schema_validator()

    for fixture in sorted(INVALID_FIXTURES.glob("*.json")):
        assert list(validator.iter_errors(load_json_fixture(fixture)))


def test_schema_date_time_format_checking_is_active() -> None:
    document = load_json_fixture(VALID_FIXTURES / "lowercase-t-z-v1.json")
    assert isinstance(document, dict)
    document["createdAt"] = "not-a-date-time"

    errors = list(schema_validator().iter_errors(document))

    assert any(list(error.path) == ["createdAt"] for error in errors)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_schema_fixture_parser_rejects_non_standard_json_constants(
    constant: str,
) -> None:
    with pytest.raises(ValueError, match="non-standard JSON constant"):
        load_json_text('{"payload":{"value":%s}}' % constant)


def test_fixture_payloads_exclude_mutable_database_row_fields() -> None:
    for fixture in valid_shared_fixtures():
        document = json.loads(fixture.read_text())

        assert MUTABLE_ROW_FIELDS.isdisjoint(document)


def valid_shared_fixtures() -> list[Path]:
    return sorted([*FIXTURES.glob("*.json"), *VALID_FIXTURES.glob("*.json")])


def load_json_fixture(fixture: Path) -> object:
    return load_json_text(fixture.read_text())


def load_json_text(value: str) -> object:
    return json.loads(
        value, parse_float=Decimal, parse_constant=reject_non_standard_json_constant
    )


def reject_non_standard_json_constant(constant: str) -> None:
    raise ValueError(f"non-standard JSON constant: {constant}")


def schema_validator() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads(SCHEMA.read_text()), format_checker=FormatChecker()
    )


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
