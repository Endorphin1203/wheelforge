from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class JobType(StrEnum):
    REQUIREMENT_PARSE = "REQUIREMENT_PARSE"
    BUILD = "BUILD"


class JobStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BuildStatus(StrEnum):
    CREATED = "CREATED"
    PARSING = "PARSING"
    QUEUED = "QUEUED"
    RESOLVING = "RESOLVING"
    DOWNLOADING = "DOWNLOADING"
    VALIDATING = "VALIDATING"
    PACKAGING = "PACKAGING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    schema_version: Literal[1] = Field(alias="schemaVersion")
    job_type: JobType = Field(alias="jobType")
    subject_id: UUID = Field(alias="subjectId")
    created_at: datetime = Field(alias="createdAt")
    payload: dict[str, Any]

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        wire_payload = _DatabaseWirePayload.model_validate_json(json_data)
        return cls.model_validate(
            {
                "schema_version": wire_payload.schema_version,
                "job_type": wire_payload.job_type,
                "subject_id": wire_payload.subject_id,
                "created_at": wire_payload.created_at,
                "payload": wire_payload.payload,
            }
        )

    @field_validator("payload", mode="before")
    @classmethod
    def require_object_payload(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        return payload

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, created_at: datetime) -> datetime:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("createdAt must include a timezone")
        return created_at


class _DatabaseWirePayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )

    schema_version: StrictInt = Field(alias="schemaVersion")
    job_type: JobType = Field(alias="jobType")
    subject_id: UUID = Field(alias="subjectId")
    created_at: datetime = Field(alias="createdAt")
    payload: dict[str, Any]

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_numeric_schema_version(cls, schema_version: Any) -> int:
        if isinstance(schema_version, bool):
            raise ValueError("schemaVersion must be a number equal to 1")
        if isinstance(schema_version, int):
            return schema_version
        if isinstance(schema_version, float) and schema_version == 1.0:
            return 1
        raise ValueError("schemaVersion must be a number equal to 1")

    @field_validator("schema_version")
    @classmethod
    def require_supported_schema_version(cls, schema_version: int) -> int:
        if schema_version != 1:
            raise ValueError("schemaVersion must be 1")
        return schema_version

    @field_validator("job_type", mode="before")
    @classmethod
    def require_text_job_type(cls, job_type: Any) -> str:
        if not isinstance(job_type, str):
            raise ValueError("jobType must be a string")
        return job_type

    @field_validator("subject_id", mode="before")
    @classmethod
    def require_canonical_subject_id(cls, subject_id: Any) -> str:
        if not isinstance(subject_id, str):
            raise ValueError("subjectId must be a canonical UUID")
        try:
            parsed_subject_id = UUID(subject_id)
        except ValueError as error:
            raise ValueError("subjectId must be a canonical UUID") from error
        if str(parsed_subject_id).lower() != subject_id.lower():
            raise ValueError("subjectId must be a canonical UUID")
        return subject_id

    @field_validator("created_at", mode="before")
    @classmethod
    def require_text_created_at(cls, created_at: Any) -> str:
        if not isinstance(created_at, str):
            raise ValueError("createdAt must be an RFC3339 date-time string")
        return created_at

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, created_at: datetime) -> datetime:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("createdAt must include a timezone")
        return created_at

    @field_validator("payload", mode="before")
    @classmethod
    def require_object_payload(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        return payload
