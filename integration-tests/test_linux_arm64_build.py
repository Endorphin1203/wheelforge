from __future__ import annotations

import hashlib

import pytest

from conftest import NativeStack, TARGET_CODE
from helpers.artifact import ArtifactReader


@pytest.mark.parametrize("repeat", [1, 2])
def test_linux_arm64_cp311_build(
    native_stack: NativeStack, artifact_reader: type[ArtifactReader], repeat: int
) -> None:
    del repeat
    api = native_stack.api
    file_id = api.upload_requirements(native_stack.projects / "success.txt")
    parsed = api.wait_for_parse(file_id)
    assert parsed["parseStatus"] == "PARSED"
    assert {item["normalizedName"] for item in api.requirement_items(file_id)} == {
        "demo-direct",
        "demo-native",
        "unchanged-demo",
    }

    task_id = api.create_build(file_id, TARGET_CODE)
    task = api.wait_for_terminal(task_id)
    assert task["status"] == "SUCCESS"
    assert task["validationLevel"] == "STATIC"
    assert task["installVerified"] is False
    assert task["targetSnapshot"]["pythonVersion"] == "3.11"
    assert task["targetSnapshot"]["architecture"] == "AARCH64"

    logs = api.logs(task_id)
    assert logs
    assert [row["sequence"] for row in logs] == sorted(row["sequence"] for row in logs)
    assert api.logs(task_id, logs[-1]["sequence"]) == []

    resolved = {row["normalizedName"]: row for row in api.resolved_packages(task_id)}
    assert set(resolved) == {
        "demo-common",
        "demo-direct",
        "demo-native",
        "unchanged-demo",
    }
    assert all(row["wheelStatus"] == "STATIC_PASSED" for row in resolved.values())
    assert resolved["demo-common"]["dependencyType"] == "TRANSITIVE"
    assert resolved["demo-native"]["wheelFilename"].endswith(
        "-cp311-cp311-manylinux2014_aarch64.whl"
    )

    comparison = {row["packageName"]: row for row in api.version_comparison(task_id)}
    assert set(comparison) == set(resolved)
    assert comparison["demo-common"]["dependencyType"] == "TRANSITIVE"
    assert comparison["unchanged-demo"]["finalVersion"] == "1.2.3"

    artifact_record = api.artifact_for_task(task_id)
    content = api.download_artifact(artifact_record["id"])
    assert hashlib.sha256(content).hexdigest() == artifact_record["sha256"]
    artifact = artifact_reader.open_bytes(content)
    assert artifact.verify_checksums()
    assert artifact.manifest["buildId"] == task_id
    assert artifact.manifest["validationLevel"] == "STATIC"
    assert artifact.manifest["installVerified"] is False
    assert artifact.manifest["complete"] is True
    assert artifact.manifest["target"] == {
        "os": "LINUX",
        "architecture": "AARCH64",
        "pythonVersion": "3.11",
    }
    assert set(artifact.scripts) == {"install.sh", "verify.sh"}
    assert {"install.bat", "verify.bat"}.isdisjoint(artifact.filenames)
    assert {name.rsplit("/", 1)[-1] for name in artifact.wheels} == {
        row["wheelFilename"] for row in resolved.values()
    }
