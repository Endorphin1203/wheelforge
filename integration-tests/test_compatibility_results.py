from __future__ import annotations

from conftest import NativeStack, TARGET_CODE


def test_compatibility_table_explains_up_down_and_unchanged(
    native_stack: NativeStack,
) -> None:
    api = native_stack.api
    file_id = api.upload_requirements(native_stack.projects / "changes.txt")
    api.wait_for_parse(file_id)
    task_id = api.create_build(file_id, TARGET_CODE)
    task = api.wait_for_terminal(task_id)

    assert task["status"] == "SUCCESS"
    assert task["targetSnapshot"]["pythonVersion"] == "3.11"
    assert task["targetSnapshot"]["platformTag"] == "manylinux2014_aarch64"

    rows = {row["packageName"]: row for row in api.version_comparison(task_id)}
    assert rows["unchanged-demo"]["changeDirection"] == "UNCHANGED"
    assert rows["unchanged-demo"]["finalVersion"] == "1.2.3"
    assert rows["upgrade-demo"]["changeDirection"] == "UPGRADE"
    assert rows["upgrade-demo"]["strictVersion"] == "1.2.3"
    assert rows["upgrade-demo"]["finalVersion"] == "1.2.4"
    assert rows["downgrade-demo"]["changeDirection"] == "DOWNGRADE"
    assert rows["downgrade-demo"]["strictVersion"] == "1.2.3"
    assert rows["downgrade-demo"]["finalVersion"] == "1.2.2"
    assert rows["upgrade-demo"]["packageSource"] == "ALIYUN"
    assert rows["downgrade-demo"]["packageSource"] == "ALIYUN"

    resolved = {row["normalizedName"]: row for row in api.resolved_packages(task_id)}
    assert resolved["upgrade-demo"]["wheelFilename"].endswith(
        "-cp311-cp311-manylinux2014_aarch64.whl"
    )
    assert resolved["downgrade-demo"]["wheelFilename"].endswith(
        "-cp311-cp311-manylinux2014_aarch64.whl"
    )
