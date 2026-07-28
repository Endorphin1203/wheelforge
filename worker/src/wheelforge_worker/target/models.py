from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


_PYTHON_VERSION = re.compile(r"^3\.(9|10|11|12|13)$")
_PYTHON_FULL_VERSION = re.compile(r"^3\.(9|10|11|12|13)\.\d+$")
_TARGET_MATRIX = {
    ("LINUX", "X86_64"): "manylinux2014_x86_64",
    ("LINUX", "AARCH64"): "manylinux2014_aarch64",
    ("WINDOWS", "AMD64"): "win_amd64",
    ("WINDOWS", "ARM64"): "win_arm64",
}


class _StrictTargetModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    @field_validator("*", check_fields=False)
    @classmethod
    def require_non_blank_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("target profile strings must not be blank")
        return value


class TargetProfile(_StrictTargetModel):
    profile_id: str = Field(alias="profileId", min_length=1)
    profile_code: str = Field(alias="profileCode", min_length=1)
    os: str = Field(min_length=1)
    architecture: str = Field(min_length=1)
    python_implementation: str = Field(alias="pythonImplementation", min_length=1)
    python_version: str = Field(alias="pythonVersion", min_length=1)
    python_full_version: str = Field(alias="pythonFullVersion", min_length=1)
    platform_tag: str = Field(alias="platformTag", min_length=1)
    abi_tags: tuple[str, str, str] = Field(alias="abiTags")
    validation_type: str = Field(alias="validationType", min_length=1)
    validation_policy_version: str = Field(
        alias="validationPolicyVersion", min_length=1
    )
    profile_version: StrictInt = Field(alias="profileVersion", ge=0)

    @field_validator("profile_id")
    @classmethod
    def require_canonical_profile_id(cls, profile_id: str) -> str:
        try:
            parsed = UUID(profile_id)
        except ValueError as error:
            raise ValueError("profileId must be a canonical UUID") from error
        if str(parsed).lower() != profile_id.lower():
            raise ValueError("profileId must be a canonical UUID")
        return profile_id

    @field_validator("python_implementation")
    @classmethod
    def require_cpython(cls, python_implementation: str) -> str:
        if python_implementation != "CPYTHON":
            raise ValueError("pythonImplementation must be CPYTHON")
        return python_implementation

    @field_validator("python_version")
    @classmethod
    def require_supported_python_version(cls, python_version: str) -> str:
        if not _PYTHON_VERSION.fullmatch(python_version):
            raise ValueError("pythonVersion must be CPython 3.9 through 3.13")
        return python_version

    @field_validator("python_full_version")
    @classmethod
    def require_supported_python_full_version(cls, python_full_version: str) -> str:
        if not _PYTHON_FULL_VERSION.fullmatch(python_full_version):
            raise ValueError("pythonFullVersion must be a CPython 3.9 through 3.13 patch version")
        return python_full_version

    @field_validator("validation_type")
    @classmethod
    def require_static_validation(cls, validation_type: str) -> str:
        if validation_type != "STATIC":
            raise ValueError("validationType must be STATIC")
        return validation_type

    @field_validator("validation_policy_version")
    @classmethod
    def require_policy_version(cls, validation_policy_version: str) -> str:
        if validation_policy_version != "wheel-tags-v1":
            raise ValueError("validationPolicyVersion must be wheel-tags-v1")
        return validation_policy_version

    @field_validator("abi_tags", mode="before")
    @classmethod
    def coerce_abi_tags_tuple(cls, abi_tags: Any) -> tuple[str, ...]:
        if not isinstance(abi_tags, (list, tuple)):
            raise ValueError("abiTags must be an array of strings")
        return tuple(abi_tags)

    @field_validator("abi_tags")
    @classmethod
    def normalize_abi_tags(
        cls, abi_tags: tuple[str, str, str], info: Any
    ) -> tuple[str, str, str]:
        python_version = info.data.get("python_version")
        if not isinstance(python_version, str) or not _PYTHON_VERSION.fullmatch(python_version):
            return abi_tags

        major, minor = python_version.split(".")
        expected = {f"cp{major}{minor}", "abi3", "none"}
        provided = set(abi_tags)
        if len(provided) != 3 or provided != expected:
            raise ValueError("abiTags must contain exactly cpXY, abi3, and none")
        return (f"cp{major}{minor}", "abi3", "none")

    @model_validator(mode="after")
    def validate_target_consistency(self) -> TargetProfile:
        expected_platform = _TARGET_MATRIX.get((self.os, self.architecture))
        if expected_platform is None:
            raise ValueError("os and architecture must match a supported target")
        if self.platform_tag != expected_platform:
            raise ValueError("platformTag must match os and architecture")
        if ".".join(self.python_full_version.split(".")[:2]) != self.python_version:
            raise ValueError("pythonFullVersion major/minor must match pythonVersion")
        return self

    @property
    def cpython_tag(self) -> str:
        major, minor = self.python_version.split(".")
        return f"cp{major}{minor}"

    @property
    def python_tag(self) -> str:
        major, minor = self.python_version.split(".")
        return f"py{major}{minor}"


def target_platform_tag(os: str, architecture: str) -> str | None:
    return _TARGET_MATRIX.get((os, architecture))
