from __future__ import annotations

import pytest

from conftest import NativeStack, TARGET_CODE
from helpers.api import ApiError
from helpers.artifact import ArtifactReader


def _parsed(native_stack: NativeStack, project: str) -> str:
    file_id = native_stack.api.upload_requirements(native_stack.projects / project)
    native_stack.api.wait_for_parse(file_id)
    return file_id


def test_download_failure_produces_explainable_partial_artifact(
    native_stack: NativeStack, artifact_reader: type[ArtifactReader]
) -> None:
    api = native_stack.api
    task_id = api.create_build(_parsed(native_stack, "partial.txt"), TARGET_CODE)
    task = api.wait_for_terminal(task_id)

    assert task["status"] == "PARTIAL_SUCCESS"
    assert task["validationLevel"] == "STATIC"
    assert task["installVerified"] is False
    packages = {row["normalizedName"]: row for row in api.resolved_packages(task_id)}
    assert packages["partial-demo"]["wheelStatus"] == "MISSING"
    assert packages["demo-direct"]["wheelStatus"] == "STATIC_PASSED"

    record = api.artifact_for_task(task_id)
    assert record["buildStatus"] == "PARTIAL_SUCCESS"
    artifact = artifact_reader.open_bytes(api.download_artifact(record["id"]))
    assert artifact.verify_checksums()
    assert artifact.manifest["complete"] is False
    assert artifact.manifest["installVerified"] is False
    assert b"PARTIAL - NOT INSTALLABLE" in artifact.read("README.md")


@pytest.mark.parametrize("project", ["missing.txt", "conflict.txt"])
def test_unresolvable_build_fails_without_artifact_and_can_retry(
    native_stack: NativeStack, project: str
) -> None:
    api = native_stack.api
    original_id = api.create_build(_parsed(native_stack, project), TARGET_CODE)
    original = api.wait_for_terminal(original_id)

    assert original["status"] == "FAILED"
    assert original["targetSnapshot"]["pythonVersion"] == "3.11"
    with pytest.raises(ApiError, match="expected one artifact"):
        api.artifact_for_task(original_id)

    retried = api.retry_build(original_id)
    assert retried["id"] != original_id
    assert retried["sourceTaskId"] == original_id
    assert retried["targetSnapshot"] == original["targetSnapshot"]
    api.cancel_build(retried["id"])
    assert api.wait_for_terminal(retried["id"])["status"] == "CANCELLED"


def test_running_download_can_be_cancelled_without_artifact(
    native_stack: NativeStack,
) -> None:
    api = native_stack.api
    task_id = api.create_build(_parsed(native_stack, "slow.txt"), TARGET_CODE)
    downloading = api.wait_for_stage(task_id, "DOWNLOADING")
    assert downloading["targetSnapshot"]["pythonVersion"] == "3.11"

    api.cancel_build(task_id)
    terminal = api.wait_for_terminal(task_id)
    assert terminal["status"] == "CANCELLED"
    assert terminal["cancelRequested"] is True
    with pytest.raises(ApiError, match="expected one artifact"):
        api.artifact_for_task(task_id)
