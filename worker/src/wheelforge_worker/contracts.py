from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )

    schema_version: Literal[1] = Field(alias="schemaVersion")
    job_type: JobType = Field(alias="jobType")
    subject_id: UUID = Field(alias="subjectId")
    created_at: datetime = Field(alias="createdAt")
    payload: dict[str, Any]

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, created_at: datetime) -> datetime:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("createdAt must include a timezone")
        return created_at
