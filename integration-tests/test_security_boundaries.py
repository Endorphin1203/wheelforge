from __future__ import annotations

import secrets
from pathlib import PurePosixPath

import pytest

from conftest import NativeStack, TARGET_CODE
from helpers.api import ApiClient, ApiError
from helpers.artifact import ArtifactReader


@pytest.mark.parametrize(
    "line",
    [
        "git+https://example.invalid/project.git",
        "-e .",
        "demo @ https://example.invalid/demo.whl",
        "-r other.txt",
        "../local-package",
    ],
)
def test_unsafe_requirement_never_reaches_build_queue(
    native_stack: NativeStack, line: str
) -> None:
    api = native_stack.api
    before = len(api.build_tasks())
    file_id = api.upload_text(line + "\n")
    parsed = api.wait_for_parse(file_id, "FAILED")

    assert parsed["parseStatus"] == "FAILED"
    assert isinstance(parsed["parseError"], str) and parsed["parseError"]
    with pytest.raises(ApiError):
        api.create_build(file_id, TARGET_CODE)
    assert len(api.build_tasks()) == before


def test_upload_larger_than_512_kib_is_rejected(native_stack: NativeStack) -> None:
    with pytest.raises(ApiError, match="UPLOAD_TOO_LARGE|HTTP_413"):
        native_stack.api.upload_text(b"x" * (512 * 1024 + 1))


def test_more_than_2000_requirement_lines_fail_parsing(
    native_stack: NativeStack,
) -> None:
    file_id = native_stack.api.upload_text("demo==1.0\n" * 2001)
    parsed = native_stack.api.wait_for_parse(file_id, "FAILED")

    assert parsed["parseStatus"] == "FAILED"
    assert "2000" in parsed["parseError"]


def test_non_admin_and_cross_user_resources_are_hidden(
    native_stack: NativeStack,
) -> None:
    admin = native_stack.api
    suffix = secrets.token_hex(6)
    username = f"integration-user-{suffix}"
    password = f"Strong-{secrets.token_urlsafe(18)}"
    admin.create_user(username, password)
    user = ApiClient(admin.base_url, timeout=admin.timeout, poll_interval=0.1)
    user.login(username, password)

    file_id = admin.upload_requirements(native_stack.projects / "happy.txt")
    admin.wait_for_parse(file_id)
    task_id = admin.create_build(file_id, TARGET_CODE)
    assert admin.wait_for_terminal(task_id)["status"] == "SUCCESS"
    artifact = admin.artifact_for_task(task_id)

    for operation in (
        lambda: user.requirement_file(file_id),
        lambda: user.build_task(task_id),
        lambda: user.logs(task_id),
        lambda: user.artifact(artifact["id"]),
        lambda: user.download_artifact(artifact["id"]),
        user.package_sources,
    ):
        with pytest.raises(ApiError):
            operation()


def test_builtin_package_source_url_is_immutable(native_stack: NativeStack) -> None:
    api = native_stack.api
    source = api.package_sources()[0]

    with pytest.raises(ApiError, match="IMMUTABLE_SOURCE_FIELD"):
        api.update_package_source(
            source["id"], baseUrl="https://attacker.example.invalid/simple"
        )


def test_generated_artifact_contains_only_safe_paths(
    native_stack: NativeStack, artifact_reader: type[ArtifactReader]
) -> None:
    api = native_stack.api
    file_id = api.upload_requirements(native_stack.projects / "happy.txt")
    api.wait_for_parse(file_id)
    task_id = api.create_build(file_id, TARGET_CODE)
    assert api.wait_for_terminal(task_id)["status"] == "SUCCESS"
    record = api.artifact_for_task(task_id)
    archive = artifact_reader.open_bytes(api.download_artifact(record["id"]))

    for name in archive.names:
        path = PurePosixPath(name)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert "\\" not in name
