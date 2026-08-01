from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

import wheelforge_worker.download.wheels as wheels_module
import wheelforge_worker.resolver.pip_report as pip_report_module
from wheelforge_worker.artifact import ArtifactBuildContext
from wheelforge_worker.jobs.pipeline import DefaultBuildStages
from wheelforge_worker.jobs.storage import WorkspaceManager
from wheelforge_worker.parser import parse_requirements
from wheelforge_worker.process import ProcessRunner, ProcessValidationError
from wheelforge_worker.target import TargetProfile


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux release gate requires /proc/self/fd descriptor inheritance",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_INDEX = REPOSITORY_ROOT / "test-fixtures" / "index" / "simple"
EXECUTION_ID = "50000000-0000-4000-8000-000000000051"
BUILD_ID = "20000000-0000-4000-8000-000000000051"


def _target() -> TargetProfile:
    return TargetProfile.model_validate(
        {
            "profileId": "40000000-0000-4000-8000-000000000051",
            "profileCode": "linux-aarch64-cp312-fixture",
            "os": "LINUX",
            "architecture": "AARCH64",
            "pythonImplementation": "CPYTHON",
            "pythonVersion": "3.12",
            "pythonFullVersion": "3.12.0",
            "platformTag": "manylinux2014_aarch64",
            "abiTags": ["cp312", "abi3", "none"],
            "validationType": "STATIC",
            "validationPolicyVersion": "wheel-tags-v1",
            "profileVersion": 1,
        }
    )


def test_default_stages_remain_fd_bound_after_workspace_root_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert FIXTURE_INDEX.is_dir()
    fixture_url = FIXTURE_INDEX.as_uri()
    monkeypatch.setattr(
        pip_report_module, "resolver_source_url", lambda _source: fixture_url
    )
    monkeypatch.setattr(wheels_module, "source_url", lambda _source: fixture_url)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(mode=0o700)
    manager = WorkspaceManager(workspace_root)
    owned = manager.allocate(EXECUTION_ID)
    capability = owned.capability
    inherited = capability.external().inherited_fds
    retained_root = tmp_path / "retained-workspaces"
    workspace_root.rename(retained_root)
    workspace_root.mkdir(mode=0o700)
    parsed = parse_requirements(b"demo-direct==1.0.0\n")
    target = _target()
    stages = DefaultBuildStages()

    try:
        resolution = stages.resolve(parsed, target, capability)
        assert {item.name for item in resolution.packages} == {
            "demo-common",
            "demo-direct",
        }

        download = stages.download(
            resolution, target, capability, lambda: False
        )
        assert download.failures == ()
        assert {wheel.package for wheel in download.wheels} == {
            "demo-common",
            "demo-direct",
        }

        validation = stages.validate(resolution, download.wheels, target)
        assert validation.report.complete is True
        capability.mkdir("artifact")
        built = stages.package(
            ArtifactBuildContext(
                build_id=BUILD_ID,
                original_requirements=parsed.normalized_text,
                resolution=resolution,
                version_changes=resolution.changes,
                target=target,
                validation=validation.report,
                wheels=validation.wheels,
            ),
            capability,
        )
        artifact_descriptor = capability.open_regular(built.path)
        try:
            assert os.fstat(artifact_descriptor).st_size > 0
        finally:
            os.close(artifact_descriptor)

        retained_workspace = retained_root / owned.path.name
        assert (retained_workspace / built.path).is_file()
        assert list(workspace_root.iterdir()) == []

        owned.cleanup()
        assert not retained_workspace.exists()
        with pytest.raises(RuntimeError, match="closed"):
            capability.external()
        with pytest.raises(ProcessValidationError, match="descriptor is not open"):
            ProcessRunner().run(
                [sys.executable, "-c", "raise SystemExit(0)"],
                workspace_root,
                timedelta(seconds=5),
                {},
                inherited_fds=inherited,
            )
    finally:
        owned.cleanup()
        manager.close()
