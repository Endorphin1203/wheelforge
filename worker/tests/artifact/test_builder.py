from __future__ import annotations

import hashlib
import csv
import errno
import io
import json
import os
import socket
import subprocess
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.tags import Tag
from packaging.version import Version

from wheelforge_worker.artifact import (
    ArtifactBuildContext,
    ArtifactBuildError,
    ArtifactBuilder,
    ValidatedWheel,
)
from wheelforge_worker.artifact import builder as builder_module
from wheelforge_worker.download import DownloadedWheel
from wheelforge_worker.resolver import (
    ArchiveHash,
    PackageSource,
    ResolutionResult,
    ResolvedPackage,
    VersionChange,
    VersionChangeKind,
)
from wheelforge_worker.target import TargetProfile
from wheelforge_worker.validation import (
    ArchiveValidationReport,
    StaticValidationReport,
    ValidationIssue,
    ValidationIssueCode,
    ValidatedWheelSnapshot,
)


BUILD_ID = "123e4567-e89b-12d3-a456-426614174000"
STATIC_MESSAGE = (
    "Static compatibility checks passed; target installation was not verified."
)


def target_profile(os_name: str = "LINUX") -> TargetProfile:
    windows = os_name == "WINDOWS"
    return TargetProfile.model_validate(
        {
            "profileId": "d9428888-122b-11e1-b85c-61cd3cbb3210",
            "profileCode": "windows-amd64-cp311" if windows else "linux-x64-cp311",
            "os": os_name,
            "architecture": "AMD64" if windows else "X86_64",
            "pythonImplementation": "CPYTHON",
            "pythonVersion": "3.11",
            "pythonFullVersion": "3.11.9",
            "platformTag": "win_amd64" if windows else "manylinux2014_x86_64",
            "abiTags": ["cp311", "abi3", "none"],
            "validationType": "STATIC",
            "validationPolicyVersion": "wheel-tags-v1",
            "profileVersion": 1,
        }
    )


def resolved_package(
    name: str = "demo", version: str = "1.2.3", *, digest: str | None = None
) -> ResolvedPackage:
    hashes = () if digest is None else (ArchiveHash("sha256", digest),)
    return ResolvedPackage(
        name=name,
        version=Version(version),
        requested=True,
        artifact_url=f"https://files.pythonhosted.org/{name}.whl",
        wheel_filename=f"{name}-{version}-py3-none-any.whl",
        requires_dist=(),
        requires_python=">=3.9",
        archive_hashes=hashes,
    )


def validated_wheel(
    root: Path,
    *,
    name: str = "demo",
    version: str = "1.2.3",
    content: bytes = b"validated wheel bytes",
) -> ValidatedWheel:
    filename = f"{name}-{version}-py3-none-any.whl"
    snapshot_dir = root / f"snapshot-{name}"
    snapshot_dir.mkdir()
    snapshot_path = snapshot_dir / filename
    snapshot_path.write_bytes(content)
    status = snapshot_path.stat()
    digest = hashlib.sha256(content).hexdigest()
    snapshot = ValidatedWheelSnapshot(
        path=snapshot_path,
        byte_size=len(content),
        sha256=digest,
        _directory=snapshot_dir,
        _device=status.st_dev,
        _inode=status.st_ino,
    )
    download = DownloadedWheel(
        package=name,
        version=Version(version),
        filename=filename,
        path=root / "original-download-must-not-be-read.whl",
        source=PackageSource.PYPI,
        byte_size=len(content),
        sha256=digest,
        tags=frozenset({Tag("py3", "none", "any")}),
    )
    report = ArchiveValidationReport(
        name=name,
        version=Version(version),
        requires_dist=(),
        requires_python=">=3.9",
        tags=download.tags,
        entry_count=3,
        total_uncompressed_bytes=100,
        snapshot=snapshot,
    )
    return ValidatedWheel(download, report)


def build_context(
    root: Path,
    *,
    os_name: str = "LINUX",
) -> ArtifactBuildContext:
    validated = validated_wheel(root)
    resolution = ResolutionResult(
        report_version="1",
        packages=(resolved_package(digest=validated.download.sha256),),
    )
    return ArtifactBuildContext(
        build_id=BUILD_ID,
        original_requirements="demo>=1\n",
        resolution=resolution,
        version_changes=(),
        target=target_profile(os_name),
        validation=StaticValidationReport((), True),
        wheels=(validated,),
    )


def partial_context(root: Path, *, malicious: bool = False) -> ArtifactBuildContext:
    validated = validated_wheel(root)
    missing_hash = "b" * 64
    issue_detail = '<missing & unsafe "detail">\nnext' if malicious else "missing Wheel"
    issue = ValidationIssue(
        ValidationIssueCode.WHEEL_MISSING,
        "missing<script>" if malicious else "missing",
        issue_detail,
    )
    change = VersionChange(
        package="missing<script>" if malicious else "missing",
        kind=VersionChangeKind.UPGRADE,
        original_constraint='>=1,"quoted"' if malicious else ">=1",
        original_version=Version("1"),
        resolved_version=Version("2"),
        reason='<reason & "quoted">\nnext' if malicious else "compatibility",
        source=PackageSource.PYPI,
    )
    return ArtifactBuildContext(
        build_id=BUILD_ID,
        original_requirements='demo>=1\r\nmissing>=1 # "quoted"\r\n',
        resolution=ResolutionResult(
            report_version="1",
            packages=(
                resolved_package("missing", "2", digest=missing_hash),
                resolved_package("demo", "1.2.3", digest=validated.download.sha256),
            ),
        ),
        version_changes=(change,),
        target=target_profile(),
        validation=StaticValidationReport((issue,), False),
        wheels=(validated,),
    )


def test_build_contains_auditable_linux_artifact_and_cleans_snapshot(
    tmp_path: Path,
) -> None:
    context = build_context(tmp_path)
    snapshot_path = context.wheels[0].report.snapshot.path  # type: ignore[union-attr]
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    artifact = ArtifactBuilder().build(context, output_dir)

    assert artifact.path == output_dir / f"wheelforge-{BUILD_ID}.zip"
    assert not snapshot_path.exists()
    assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
    assert artifact.manifest["validationLevel"] == "STATIC"
    assert artifact.manifest["installVerified"] is False
    assert artifact.manifest["validationMessage"] == STATIC_MESSAGE
    with ZipFile(artifact.path) as archive:
        names = set(archive.namelist())
        assert {
            "README.md",
            "requirements-original.txt",
            "requirements-resolved.txt",
            "version-comparison.csv",
            "build-report.html",
            "manifest.json",
            "checksums.sha256",
            "install.sh",
            "verify.sh",
            "packages/demo-1.2.3-py3-none-any.whl",
        } == names
        assert json.loads(archive.read("manifest.json")) == artifact.manifest
        assert archive.read("packages/demo-1.2.3-py3-none-any.whl") == (
            b"validated wheel bytes"
        )


def test_build_is_byte_deterministic_for_independent_snapshots(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_output = first_root / "output"
    second_output = second_root / "output"
    first_output.mkdir()
    second_output.mkdir()

    first = ArtifactBuilder().build(build_context(first_root), first_output)
    second = ArtifactBuilder().build(build_context(second_root), second_output)

    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == second.sha256


def test_windows_artifact_has_only_windows_target_scripts(tmp_path: Path) -> None:
    context = build_context(tmp_path, os_name="WINDOWS")
    output = tmp_path / "output"
    output.mkdir()

    artifact = ArtifactBuilder().build(context, output)

    with ZipFile(artifact.path) as archive:
        names = set(archive.namelist())
        assert {"install.bat", "verify.bat"} <= names
        assert {"install.sh", "verify.sh"}.isdisjoint(names)
        install = archive.read("install.bat").decode()
        assert "%OS%" in install
        assert "AMD64" in install
        assert "(3, 11)" in install
        assert "--no-index --find-links packages --require-hashes" in install
        assert "\r" not in install


def test_partial_artifact_retains_intended_hashes_and_disables_scripts(
    tmp_path: Path,
) -> None:
    context = partial_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()

    artifact = ArtifactBuilder().build(context, output)

    assert artifact.manifest["complete"] is False
    with ZipFile(artifact.path) as archive:
        requirements = archive.read("requirements-resolved.txt").decode()
        assert requirements == (
            f"demo==1.2.3 --hash=sha256:{context.wheels[0].download.sha256}\n"
            f"missing==2 --hash=sha256:{'b' * 64}\n"
        )
        assert "Partial artifact - not installable" in archive.read(
            "build-report.html"
        ).decode()
        for name in ("install.sh", "verify.sh"):
            script = archive.read(name).decode()
            assert "Incomplete artifact" in script
            assert "pip" not in script


def test_csv_html_json_and_text_are_encoded_safely(tmp_path: Path) -> None:
    context = partial_context(tmp_path, malicious=True)
    output = tmp_path / "output"
    output.mkdir()

    artifact = ArtifactBuilder().build(context, output)

    with ZipFile(artifact.path) as archive:
        csv_rows = list(
            csv.reader(io.StringIO(archive.read("version-comparison.csv").decode()))
        )
        assert csv_rows[1] == [
            "missing<script>",
            '>=1,"quoted"',
            "1",
            "2",
            "UPGRADE",
            "PYPI",
            '<reason & "quoted">\nnext',
        ]
        report = archive.read("build-report.html").decode()
        assert "<script>" not in report
        assert "&lt;script&gt;" in report
        assert "&lt;reason &amp; &quot;quoted&quot;&gt;" in report
        manifest_bytes = archive.read("manifest.json")
        assert b'\\nnext' in manifest_bytes
        assert json.loads(manifest_bytes)["validationIssues"][0]["detail"] == (
            '<missing & unsafe "detail">\nnext'
        )
        for name in archive.namelist():
            if name.endswith((".txt", ".csv", ".html", ".json", ".md", ".sh", ".bat")):
                assert b"\r" not in archive.read(name)


def test_checksums_cover_every_other_member_and_zip_metadata_is_fixed(
    tmp_path: Path,
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    artifact = ArtifactBuilder().build(context, output)

    with ZipFile(artifact.path) as archive:
        infos = archive.infolist()
        assert [item.filename for item in infos] == sorted(archive.namelist())
        assert {item.date_time for item in infos} == {(1980, 1, 1, 0, 0, 0)}
        assert all(item.create_system == 3 for item in infos)
        checksums = {}
        for line in archive.read("checksums.sha256").decode().splitlines():
            digest, name = line.split("  ", 1)
            checksums[name] = digest
        assert set(checksums) == set(archive.namelist()) - {"checksums.sha256"}
        for name, digest in checksums.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        modes = {item.filename: item.external_attr >> 16 for item in infos}
        assert modes["manifest.json"] == 0o100644
        assert modes["install.sh"] == 0o100755


@pytest.mark.parametrize("mismatch", ["name", "version", "filename", "size", "hash", "tags"])
def test_inconsistent_download_archive_pairs_are_rejected_and_cleaned(
    tmp_path: Path, mismatch: str
) -> None:
    context = build_context(tmp_path)
    pair = context.wheels[0]
    snapshot = pair.report.snapshot
    assert snapshot is not None
    download = pair.download
    report = pair.report
    if mismatch == "name":
        report = replace(report, name="other")
    elif mismatch == "version":
        report = replace(report, version=Version("9"))
    elif mismatch == "filename":
        download = replace(download, filename="other-1.2.3-py3-none-any.whl")
    elif mismatch == "size":
        download = replace(download, byte_size=download.byte_size + 1)
    elif mismatch == "hash":
        download = replace(download, sha256="f" * 64)
    else:
        report = replace(report, tags=frozenset({Tag("cp311", "none", "any")}))
    broken = replace(context, wheels=(ValidatedWheel(download, report),))
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ArtifactBuildError):
        ArtifactBuilder().build(broken, output)

    assert not snapshot.path.exists()
    assert list(output.iterdir()) == []


def test_missing_snapshot_is_rejected_without_reading_original_download(
    tmp_path: Path,
) -> None:
    context = build_context(tmp_path)
    pair = context.wheels[0]
    snapshot = pair.report.snapshot
    assert snapshot is not None
    snapshot.cleanup()
    broken = replace(
        context,
        wheels=(ValidatedWheel(pair.download, replace(pair.report, snapshot=None)),),
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ArtifactBuildError, match="snapshot"):
        ArtifactBuilder().build(broken, output)


def test_duplicate_resolved_identity_is_rejected_and_snapshot_cleaned(
    tmp_path: Path,
) -> None:
    context = build_context(tmp_path)
    duplicate = replace(
        resolved_package("Demo", "2", digest="c" * 64), name="Demo"
    )
    issue = ValidationIssue(
        ValidationIssueCode.RESOLUTION_DUPLICATE_PACKAGE,
        "demo",
        "duplicate",
    )
    broken = replace(
        context,
        resolution=replace(
            context.resolution,
            packages=context.resolution.packages + (duplicate,),
        ),
        validation=StaticValidationReport((issue,), False),
    )
    snapshot = context.wheels[0].report.snapshot
    assert snapshot is not None
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ArtifactBuildError, match="unique"):
        ArtifactBuilder().build(broken, output)

    assert not snapshot.path.exists()


def test_existing_artifact_is_never_overwritten_and_stage_is_removed(
    tmp_path: Path,
) -> None:
    context = build_context(tmp_path)
    snapshot = context.wheels[0].report.snapshot
    assert snapshot is not None
    output = tmp_path / "output"
    output.mkdir()
    final = output / f"wheelforge-{BUILD_ID}.zip"
    final.write_bytes(b"external artifact")

    with pytest.raises(FileExistsError):
        ArtifactBuilder().build(context, output)

    assert final.read_bytes() == b"external artifact"
    assert not snapshot.path.exists()
    assert [path.name for path in output.iterdir()] == [final.name]


def test_staged_verification_failure_removes_stage_and_cleans_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    snapshot = context.wheels[0].report.snapshot
    assert snapshot is not None
    output = tmp_path / "output"
    output.mkdir()

    def fail_verification(
        path: Path, expected_names: set[str], expected_hashes: dict[str, str]
    ) -> None:
        assert path.parent == output
        assert expected_names
        assert expected_hashes
        raise ArtifactBuildError("injected verification failure")

    monkeypatch.setattr(builder_module, "_verify_zip", fail_verification)
    with pytest.raises(ArtifactBuildError, match="injected"):
        ArtifactBuilder().build(context, output)

    assert list(output.iterdir()) == []
    assert not snapshot.path.exists()


def test_output_must_be_existing_real_directory_and_snapshot_is_cleaned(
    tmp_path: Path,
) -> None:
    context = build_context(tmp_path)
    snapshot = context.wheels[0].report.snapshot
    assert snapshot is not None

    with pytest.raises(ArtifactBuildError, match="already exist"):
        ArtifactBuilder().build(context, tmp_path / "missing")

    assert not snapshot.path.exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlink_output_directory_is_rejected(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    linked_output = tmp_path / "linked-output"
    linked_output.symlink_to(real_output, target_is_directory=True)

    with pytest.raises(ArtifactBuildError, match="real directory"):
        ArtifactBuilder().build(context, linked_output)

    assert list(real_output.iterdir()) == []


def test_staged_verification_streams_package_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    original_read = ZipFile.read

    def reject_whole_wheel_read(
        archive: ZipFile, name: str, pwd: bytes | None = None
    ) -> bytes:
        if str(name).startswith("packages/"):
            raise AssertionError("Wheel member was read into memory")
        return original_read(archive, name, pwd)

    monkeypatch.setattr(ZipFile, "read", reject_whole_wheel_read)

    artifact = ArtifactBuilder().build(context, output)

    assert artifact.path.exists()


def test_failure_cleanup_preserves_replacement_of_owned_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    replacement = b"external replacement"

    def replace_then_fail(
        path: Path, expected_names: set[str], expected_hashes: dict[str, str]
    ) -> None:
        assert expected_names
        assert expected_hashes
        path.unlink()
        path.write_bytes(replacement)
        raise ArtifactBuildError("injected replacement race")

    monkeypatch.setattr(builder_module, "_verify_zip", replace_then_fail)

    with pytest.raises(ArtifactBuildError, match="replacement race"):
        ArtifactBuilder().build(context, output)

    remaining = list(output.iterdir())
    assert len(remaining) == 1
    assert remaining[0].read_bytes() == replacement


def test_wheel_filename_identity_must_match_report_identity(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    pair = context.wheels[0]
    snapshot = pair.report.snapshot
    assert snapshot is not None
    renamed_path = snapshot.path.with_name("other-9.0-py3-none-any.whl")
    snapshot.path.rename(renamed_path)
    renamed_snapshot = replace(snapshot, path=renamed_path)
    broken = replace(
        context,
        wheels=(
            ValidatedWheel(
                replace(pair.download, filename=renamed_path.name),
                replace(pair.report, snapshot=renamed_snapshot),
            ),
        ),
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ArtifactBuildError, match="disagree"):
        ArtifactBuilder().build(broken, output)

    assert not renamed_path.exists()


def test_partial_missing_package_without_one_sha256_is_rejected(tmp_path: Path) -> None:
    context = partial_context(tmp_path)
    missing = replace(context.resolution.packages[0], archive_hashes=())
    broken = replace(
        context,
        resolution=replace(
            context.resolution,
            packages=(missing, context.resolution.packages[1]),
        ),
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ArtifactBuildError, match="unambiguous resolver SHA-256"):
        ArtifactBuilder().build(broken, output)


@pytest.mark.parametrize("failure", [False, True], ids=["success", "failure"])
def test_snapshot_cleanup_is_called_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    cleanup_calls = 0
    original_cleanup = ValidatedWheelSnapshot.cleanup

    def counted_cleanup(snapshot: ValidatedWheelSnapshot) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        original_cleanup(snapshot)

    monkeypatch.setattr(ValidatedWheelSnapshot, "cleanup", counted_cleanup)
    if failure:
        monkeypatch.setattr(
            builder_module,
            "_verify_zip",
            lambda path, expected, hashes: (_ for _ in ()).throw(
                ArtifactBuildError("injected failure")
            ),
        )
        with pytest.raises(ArtifactBuildError, match="injected"):
            ArtifactBuilder().build(context, output)
    else:
        ArtifactBuilder().build(context, output)

    assert cleanup_calls == 1


def test_original_download_path_is_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    original_path = context.wheels[0].download.path
    original_path.write_bytes(b"unvalidated original bytes")
    output = tmp_path / "output"
    output.mkdir()
    path_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object) -> object:
        if path == original_path:
            raise AssertionError("original download path was opened")
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    artifact = ArtifactBuilder().build(context, output)

    with ZipFile(artifact.path) as archive:
        assert archive.read("packages/demo-1.2.3-py3-none-any.whl") == (
            b"validated wheel bytes"
        )


def unordered_context(root: Path, *, reverse: bool) -> ArtifactBuildContext:
    alpha = validated_wheel(root, name="alpha", version="1")
    beta = validated_wheel(root, name="beta", version="2")
    packages = (
        resolved_package("alpha", "1", digest=alpha.download.sha256),
        resolved_package("beta", "2", digest=beta.download.sha256),
    )
    changes = (
        VersionChange(
            "alpha",
            VersionChangeKind.UPGRADE,
            ">=0",
            Version("0"),
            Version("1"),
            "alpha reason",
            PackageSource.PYPI,
        ),
        VersionChange(
            "alpha",
            VersionChangeKind.UNCHANGED,
            "==2",
            Version("2"),
            Version("2"),
            "beta reason",
            PackageSource.ALIYUN,
        ),
    )
    issues = (
        ValidationIssue(ValidationIssueCode.WHEEL_MISSING, "zeta", "z issue"),
        ValidationIssue(ValidationIssueCode.REQUIRES_DIST_INVALID, "alpha", "a issue"),
    )
    wheels = (alpha, beta)
    if reverse:
        packages = tuple(reversed(packages))
        changes = tuple(reversed(changes))
        issues = tuple(reversed(issues))
        wheels = tuple(reversed(wheels))
    return ArtifactBuildContext(
        BUILD_ID,
        "alpha\nbeta\n",
        ResolutionResult("1", packages),
        changes,
        target_profile(),
        StaticValidationReport(issues, False),
        wheels,
    )


def test_all_manifest_lists_and_zip_bytes_have_stable_ordering(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_output = first_root / "output"
    second_output = second_root / "output"
    first_output.mkdir()
    second_output.mkdir()

    first = ArtifactBuilder().build(unordered_context(first_root, reverse=False), first_output)
    second = ArtifactBuilder().build(unordered_context(second_root, reverse=True), second_output)

    assert first.manifest == second.manifest
    assert first.path.read_bytes() == second.path.read_bytes()


def test_linux_complete_scripts_check_exact_target_before_pip(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    artifact = ArtifactBuilder().build(context, output)

    with ZipFile(artifact.path) as archive:
        install = archive.read("install.sh").decode()
        verify = archive.read("verify.sh").decode()
        assert '"$(uname -s)" = "Linux"' in install
        assert '"$(uname -m)" = "x86_64"' in install
        assert "sys.version_info[:2] != (3, 11)" in install
        assert install.index("uname -s") < install.index("pip install")
        assert "sha256sum -c checksums.sha256" in verify
        assert "python -m pip check" in verify


def test_windows_partial_scripts_fail_without_invoking_pip(tmp_path: Path) -> None:
    context = replace(partial_context(tmp_path), target=target_profile("WINDOWS"))
    output = tmp_path / "output"
    output.mkdir()
    artifact = ArtifactBuilder().build(context, output)

    with ZipFile(artifact.path) as archive:
        for name in ("install.bat", "verify.bat"):
            script = archive.read(name).decode()
            assert "Incomplete artifact" in script
            assert "exit /b 1" in script
            assert "pip" not in script


def test_builder_never_starts_processes_or_network_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("external execution is forbidden")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    artifact = ArtifactBuilder().build(context, output)

    assert artifact.path.exists()


def test_publication_race_preserves_external_final_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    final = output / f"wheelforge-{BUILD_ID}.zip"

    def racing_rename(
        staged: Path,
        destination: Path,
        symbol: str,
        flag: int,
        *extra: object,
    ) -> None:
        assert staged.exists()
        assert destination == final
        assert symbol in {"renameatx_np", "renameat2"}
        assert flag in {1, 0x00000004}
        destination.write_bytes(b"external winner")
        raise OSError(errno.EEXIST, "injected race")

    monkeypatch.setattr(builder_module, "_rename_no_replace", racing_rename)

    with pytest.raises(FileExistsError):
        ArtifactBuilder().build(context, output)

    assert final.read_bytes() == b"external winner"
    assert [path.name for path in output.iterdir()] == [final.name]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_directory_swap_failure_removes_only_owned_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    moved_output = tmp_path / "moved-output"
    attacker = tmp_path / "attacker"
    output.mkdir()
    attacker.mkdir()
    replacement = b"attacker-owned replacement"

    def swap_directory_then_fail(
        path: Path, expected_names: set[str], expected_hashes: dict[str, str]
    ) -> None:
        assert expected_names
        assert expected_hashes
        output.rename(moved_output)
        output.symlink_to(attacker, target_is_directory=True)
        (attacker / path.name).write_bytes(replacement)
        raise ArtifactBuildError("injected directory swap")

    monkeypatch.setattr(builder_module, "_verify_zip", swap_directory_then_fail)

    with pytest.raises(ArtifactBuildError, match="directory swap"):
        ArtifactBuilder().build(context, output)

    assert list(moved_output.iterdir()) == []
    attacker_files = list(attacker.iterdir())
    assert len(attacker_files) == 1
    assert attacker_files[0].read_bytes() == replacement


def test_duplicate_snapshot_reference_is_cleaned_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    pair = context.wheels[0]
    issue = ValidationIssue(
        ValidationIssueCode.WHEEL_DUPLICATE,
        pair.download.package,
        "duplicate Wheel",
    )
    broken = replace(
        context,
        validation=StaticValidationReport((issue,), False),
        wheels=(pair, pair),
    )
    output = tmp_path / "output"
    output.mkdir()
    calls = 0
    cleanup = ValidatedWheelSnapshot.cleanup

    def count_cleanup(snapshot: ValidatedWheelSnapshot) -> None:
        nonlocal calls
        calls += 1
        cleanup(snapshot)

    monkeypatch.setattr(ValidatedWheelSnapshot, "cleanup", count_cleanup)

    with pytest.raises(ArtifactBuildError):
        ArtifactBuilder().build(broken, output)

    assert calls == 1


def test_valid_replacement_of_staged_path_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    verify = builder_module._verify_zip

    def replace_with_valid_copy(
        path: Path, expected_names: set[str], expected_hashes: dict[str, str]
    ) -> None:
        content = path.read_bytes()
        path.unlink()
        path.write_bytes(content)
        verify(path, expected_names, expected_hashes)

    monkeypatch.setattr(builder_module, "_verify_zip", replace_with_valid_copy)

    with pytest.raises(ArtifactBuildError, match="identity changed"):
        ArtifactBuilder().build(context, output)

    assert not (output / f"wheelforge-{BUILD_ID}.zip").exists()


def test_staged_zip_metadata_tampering_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    verify = builder_module._verify_zip

    def tamper_metadata(
        path: Path, expected_names: set[str], expected_hashes: dict[str, str]
    ) -> None:
        with ZipFile(path) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        with ZipFile(path, "w") as replacement:
            for name in sorted(entries):
                info = builder_module._zip_info(
                    name, script=name.endswith((".sh", ".bat"))
                )
                if name == "README.md":
                    info.date_time = (2025, 1, 2, 3, 4, 6)
                    info.external_attr = 0o100600 << 16
                replacement.writestr(info, entries[name])
        verify(path, expected_names, expected_hashes)

    monkeypatch.setattr(builder_module, "_verify_zip", tamper_metadata)

    with pytest.raises(ArtifactBuildError, match="metadata"):
        ArtifactBuilder().build(context, output)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_output_directory_swap_before_publication_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    moved_output = tmp_path / "moved-output"
    attacker = tmp_path / "attacker"
    output.mkdir()
    attacker.mkdir()
    verify = builder_module._verify_zip

    def verify_then_swap(
        path: Path, expected_names: set[str], expected_hashes: dict[str, str]
    ) -> None:
        verify(path, expected_names, expected_hashes)
        content = path.read_bytes()
        output.rename(moved_output)
        output.symlink_to(attacker, target_is_directory=True)
        (attacker / path.name).write_bytes(content)

    monkeypatch.setattr(builder_module, "_verify_zip", verify_then_swap)

    with pytest.raises(ArtifactBuildError, match="output directory identity changed"):
        ArtifactBuilder().build(context, output)

    assert list(moved_output.iterdir()) == []
    attacker_files = list(attacker.iterdir())
    assert len(attacker_files) == 1
    assert attacker_files[0].name.startswith(".wheelforge-artifact-")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_output_directory_swap_at_rename_boundary_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    moved_output = tmp_path / "moved-output"
    attacker = tmp_path / "attacker"
    output.mkdir()
    attacker.mkdir()
    rename = builder_module._rename_no_replace

    def swap_then_rename(
        staged: Path,
        final: Path,
        symbol: str,
        flag: int,
        *extra: object,
    ) -> None:
        content = staged.read_bytes()
        output.rename(moved_output)
        output.symlink_to(attacker, target_is_directory=True)
        (attacker / staged.name).write_bytes(content)
        rename(staged, final, symbol, flag, *extra)

    monkeypatch.setattr(builder_module, "_rename_no_replace", swap_then_rename)

    with pytest.raises(ArtifactBuildError, match="output directory identity changed"):
        ArtifactBuilder().build(context, output)

    assert list(moved_output.iterdir()) == []
    attacker_files = list(attacker.iterdir())
    assert len(attacker_files) == 1
    assert attacker_files[0].name.startswith(".wheelforge-artifact-")
    remaining = list(output.iterdir())
    assert len(remaining) == 1


def test_self_consistent_staged_content_tampering_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = build_context(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    verify = builder_module._verify_zip

    def tamper_with_valid_checksums(
        path: Path, expected_names: set[str], expected_hashes: dict[str, str]
    ) -> None:
        with ZipFile(path) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        entries["README.md"] = b"tampered but self-consistent\n"
        checksums = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in entries.items()
            if name != "checksums.sha256"
        }
        entries["checksums.sha256"] = "".join(
            f"{checksums[name]}  {name}\n" for name in sorted(checksums)
        ).encode()
        with ZipFile(path, "w") as replacement:
            for name in sorted(entries):
                replacement.writestr(
                    builder_module._zip_info(
                        name, script=name.endswith((".sh", ".bat"))
                    ),
                    entries[name],
                )
        verify(path, expected_names, expected_hashes)

    monkeypatch.setattr(builder_module, "_verify_zip", tamper_with_valid_checksums)

    with pytest.raises(ArtifactBuildError, match="expected content"):
        ArtifactBuilder().build(context, output)

    assert not (output / f"wheelforge-{BUILD_ID}.zip").exists()
