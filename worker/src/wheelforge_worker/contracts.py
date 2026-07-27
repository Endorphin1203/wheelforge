import json
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


_RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:[0-5]\d(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


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


class _StrictPayloadModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    @field_validator("*", check_fields=False)
    @classmethod
    def require_non_blank_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("payload strings must not be blank")
        return value


class RequirementParsePayload(_StrictPayloadModel):
    original_object_key: str = Field(alias="originalObjectKey", min_length=1)
    normalized_object_key: str = Field(alias="normalizedObjectKey", min_length=1)


class TargetSnapshot(_StrictPayloadModel):
    profile_id: str = Field(alias="profileId", min_length=1)
    profile_code: str = Field(alias="profileCode", min_length=1)
    os: str = Field(min_length=1)
    architecture: str = Field(min_length=1)
    python_implementation: str = Field(alias="pythonImplementation", min_length=1)
    python_version: str = Field(alias="pythonVersion", min_length=1)
    python_full_version: str = Field(alias="pythonFullVersion", min_length=1)
    platform_tag: str = Field(alias="platformTag", min_length=1)
    abi_tags: list[str] = Field(alias="abiTags", min_length=1)
    validation_type: str = Field(alias="validationType", min_length=1)
    validation_policy_version: str = Field(
        alias="validationPolicyVersion", min_length=1
    )
    profile_version: StrictInt = Field(alias="profileVersion", ge=0)

    @field_validator("profile_version", mode="before")
    @classmethod
    def require_mathematical_integer(cls, profile_version: Any) -> int:
        if isinstance(profile_version, bool):
            raise ValueError("profileVersion must be a non-negative integer")
        if isinstance(profile_version, int):
            return profile_version
        if isinstance(profile_version, Decimal) and profile_version == profile_version.to_integral():
            return int(profile_version)
        raise ValueError("profileVersion must be a non-negative integer")

    @field_validator("profile_id")
    @classmethod
    def require_canonical_profile_id(cls, profile_id: str) -> str:
        return validate_canonical_uuid(profile_id, "profileId")

    @field_validator("abi_tags")
    @classmethod
    def require_non_empty_abi_tags(cls, abi_tags: list[str]) -> list[str]:
        if any(not value.strip() for value in abi_tags):
            raise ValueError("abiTags must contain non-empty strings")
        return abi_tags


class BuildPayload(_StrictPayloadModel):
    requirement_file_id: str = Field(alias="requirementFileId", min_length=1)
    normalized_object_key: str = Field(alias="normalizedObjectKey", min_length=1)
    solve_mode: Literal["COMPATIBLE"] = Field(alias="solveMode")
    target_snapshot: TargetSnapshot = Field(alias="targetSnapshot")

    @field_validator("requirement_file_id")
    @classmethod
    def require_canonical_requirement_file_id(cls, requirement_file_id: str) -> str:
        return validate_canonical_uuid(requirement_file_id, "requirementFileId")


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
    created_at: str = Field(alias="createdAt")
    payload: dict[str, Any]

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        if any(value is not None for value in kwargs.values()):
            raise ValueError("model_validate_json uses fixed database wire settings")
        wire_payload = _DatabaseWirePayload.model_validate(
            json.loads(
                json_data,
                parse_float=Decimal,
                parse_constant=reject_non_standard_json_constant,
            )
        )
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

    @model_validator(mode="after")
    def require_job_type_payload(self) -> Self:
        payload_type: type[RequirementParsePayload] | type[BuildPayload]
        if self.job_type is JobType.REQUIREMENT_PARSE:
            payload_type = RequirementParsePayload
        else:
            payload_type = BuildPayload
        validated = payload_type.model_validate(self.payload)
        self.payload = validated.model_dump(by_alias=True)
        return self

    @field_validator("created_at", mode="before")
    @classmethod
    def require_rfc3339_created_at(cls, created_at: Any) -> str:
        return validate_rfc3339_created_at(created_at)


class _DatabaseWirePayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )

    schema_version: StrictInt = Field(alias="schemaVersion")
    job_type: JobType = Field(alias="jobType")
    subject_id: UUID = Field(alias="subjectId")
    created_at: str = Field(alias="createdAt")
    payload: dict[str, Any]

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_numeric_schema_version(cls, schema_version: Any) -> int:
        if isinstance(schema_version, bool):
            raise ValueError("schemaVersion must be a number equal to 1")
        if isinstance(schema_version, int):
            return schema_version
        if isinstance(schema_version, Decimal) and schema_version == Decimal(1):
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
    def require_rfc3339_created_at(cls, created_at: Any) -> str:
        return validate_rfc3339_created_at(created_at)

    @field_validator("payload", mode="before")
    @classmethod
    def require_object_payload(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        return payload


def validate_rfc3339_created_at(created_at: Any) -> str:
    if not isinstance(created_at, str) or not _RFC3339_DATE_TIME.fullmatch(created_at):
        raise ValueError(
            "createdAt must be an RFC3339 date-time string with a timezone"
        )

    normalized = f"{created_at[:10]}T{created_at[11:]}"
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    datetime.fromisoformat(normalized)
    return created_at


def validate_canonical_uuid(value: str, field: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"{field} must be a canonical UUID") from error
    if str(parsed).lower() != value.lower():
        raise ValueError(f"{field} must be a canonical UUID")
    return value


def reject_non_standard_json_constant(constant: str) -> None:
    raise ValueError(f"non-standard JSON constant: {constant}")
