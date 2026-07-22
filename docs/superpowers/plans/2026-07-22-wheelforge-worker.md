# WheelForge Python Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement deterministic, cancellable target-platform dependency parsing, resolution, Wheel download, validation, reporting, and packaging.

**Architecture:** A Redis consumer claims jobs through conditional MySQL updates, then runs focused pipeline stages. pip supplies supported cross-target resolution reports; `packaging` independently parses requirements and validates versions, markers, and Wheel tags; all commands use argument arrays through a restricted process runner.

**Tech Stack:** Python 3.12, pip 26.1.x, packaging, Pydantic 2, SQLAlchemy 2, PyMySQL, redis-py, MinIO SDK, charset-normalizer, Jinja2, pytest.

## Global Constraints

- Execute the foundation plan first; execute the API plan before full queue integration.
- Never run subprocesses with `shell=True`.
- Accept only the approved PEP 508 subset; reject URLs, local paths, includes, constraints, editable installs, and pip options.
- Resolve Marker values against the target profile, never the Worker host.
- Force `--only-binary=:all:` and never accept sdist.
- Dependency version compatibility may move up or down but never changes target Python.
- Stable `1.x` candidates do not cross major; `0.x` candidates do not cross minor.
- Server-side validation never creates a virtual environment or container, never installs dependencies, never runs `pip check`, and never imports third-party packages. pip `--dry-run` is permitted only for dependency resolution.

---

## File Map

- `worker/src/wheelforge_worker/parser/`: input decoding and RequirementItem parsing.
- `worker/src/wheelforge_worker/target/`: target marker environment and accepted tags.
- `worker/src/wheelforge_worker/resolver/`: pip report adapter and compatibility search.
- `worker/src/wheelforge_worker/download/`: source fallback and Wheel acquisition.
- `worker/src/wheelforge_worker/validation/`: target compatibility, dependency closure, Wheel ZIP, METADATA, RECORD, path, and hash validation.
- `worker/src/wheelforge_worker/artifact/`: scripts, manifests, reports, hashes, and ZIP.
- `worker/src/wheelforge_worker/jobs/`: consumer, claims, stage orchestration, cancellation.
- `worker/tests/`: unit, contract, and pipeline tests with local fixtures.

### Task 1: Safe Requirements Parser

**Files:**
- Create: `worker/src/wheelforge_worker/parser/models.py`
- Create: `worker/src/wheelforge_worker/parser/requirements.py`
- Create: `worker/tests/parser/test_requirements.py`

**Interfaces:**
- Produces: `parse_requirements(raw: bytes) -> ParsedRequirements`.
- Produces: `RequirementItem(line_no, name, extras, specifier, marker, original_text)` and typed parse errors.

- [ ] **Step 1: Write failing encoding and rejection tests**

```python
@pytest.mark.parametrize("raw", ["requests==2.32.4\r\n".encode(), "请求包==1.0\n".encode("gbk")])
def test_normalizes_supported_encodings(raw: bytes) -> None:
    result = parse_requirements(raw)
    assert result.normalized_text.endswith("\n")

@pytest.mark.parametrize("line", ["git+https://example/x.git", "-e .", "pkg @ https://example/x.whl", "-r other.txt", "../pkg"])
def test_rejects_unsafe_lines(line: str) -> None:
    with pytest.raises(UnsupportedRequirementSyntax):
        parse_requirements((line + "\n").encode())
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `cd worker && .venv/bin/pytest tests/parser/test_requirements.py -q`

Expected: FAIL because parser modules are missing.

- [ ] **Step 3: Implement bounded decoding and structured parsing**

Limit input to 512 KiB and 2,000 logical lines, reject NUL and line continuation, try UTF-8/UTF-8-SIG before GBK detection, normalize CRLF, strip comments safely, and parse accepted lines with `packaging.requirements.Requirement`. Merge duplicate canonical names only when their constraints are compatible.

- [ ] **Step 4: Run parser tests**

Run: `cd worker && .venv/bin/pytest tests/parser -q`

Expected: encoding, CRLF, extras, Marker, duplicate, conflict, and unsafe-syntax tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker/src/wheelforge_worker/parser worker/tests/parser
git commit -m "feat(worker): parse safe requirements files"
```

### Task 2: Target Environment and Wheel Tag Policy

**Files:**
- Create: `worker/src/wheelforge_worker/target/models.py`
- Create: `worker/src/wheelforge_worker/target/markers.py`
- Create: `worker/src/wheelforge_worker/target/tags.py`
- Test: `worker/tests/target/test_policy.py`

**Interfaces:**
- Produces: `TargetProfile` matching the frozen database snapshot.
- Produces: `marker_environment(profile) -> dict[str, str]`.
- Produces: `wheel_is_compatible(filename: str, profile: TargetProfile) -> bool`.

- [ ] **Step 1: Write failing target-policy tests**

```python
def test_cp311_linux_arm64_accepts_native_abi3_and_universal(profile_cp311_arm64) -> None:
    assert wheel_is_compatible("demo-1.0-cp311-cp311-manylinux2014_aarch64.whl", profile_cp311_arm64)
    assert wheel_is_compatible("demo-1.0-cp39-abi3-manylinux2014_aarch64.whl", profile_cp311_arm64)
    assert wheel_is_compatible("demo-1.0-py3-none-any.whl", profile_cp311_arm64)
    assert not wheel_is_compatible("demo-1.0-cp310-cp310-manylinux2014_aarch64.whl", profile_cp311_arm64)
    assert not wheel_is_compatible("demo-1.0-cp311-cp311-manylinux2014_x86_64.whl", profile_cp311_arm64)
```

- [ ] **Step 2: Verify failure**

Run: `cd worker && .venv/bin/pytest tests/target/test_policy.py -q`

Expected: FAIL because target policy is absent.

- [ ] **Step 3: Implement explicit Marker and tag sets**

Construct the Marker dictionary from OS, machine, CPython major/minor/full version, and implementation. Parse Wheel filenames with `packaging.utils.parse_wheel_filename`; compare every compressed tag against profile tags including valid older `abi3` baselines and `py3-none-any`.

- [ ] **Step 4: Run target tests**

Run: `cd worker && .venv/bin/pytest tests/target -q`

Expected: Linux x86_64/ARM64, Windows x64/ARM64, Python 3.9-3.13, Marker, ABI, and rejection matrices pass.

- [ ] **Step 5: Commit**

```bash
git add worker/src/wheelforge_worker/target worker/tests/target
git commit -m "feat(worker): model target Python and Wheel compatibility"
```

### Task 3: Restricted Process Runner and Strict Resolver

**Files:**
- Create: `worker/src/wheelforge_worker/process.py`
- Create: `worker/src/wheelforge_worker/resolver/pip_report.py`
- Create: `worker/src/wheelforge_worker/resolver/models.py`
- Test: `worker/tests/resolver/test_pip_report.py`

**Interfaces:**
- Produces: `ProcessRunner.run(argv: Sequence[str], cwd: Path, timeout: timedelta, env: Mapping[str, str]) -> ProcessResult`.
- Produces: `StrictResolver.resolve(parsed, profile, source) -> ResolutionResult`.

- [ ] **Step 1: Write failing argv and report tests**

```python
def test_pip_command_is_targeted_and_binary_only(profile_cp311_arm64) -> None:
    argv = build_resolve_argv(Path("requirements.txt"), Path("report.json"), profile_cp311_arm64, "https://pypi.org/simple")
    assert argv[:3] == [sys.executable, "-m", "pip"]
    assert "--only-binary=:all:" in argv
    assert ["--platform", "manylinux2014_aarch64"] == argv[argv.index("--platform"):argv.index("--platform") + 2]
    assert "--python-version" in argv and "3.11" in argv
```

- [ ] **Step 2: Run and verify failure**

Run: `cd worker && .venv/bin/pytest tests/resolver/test_pip_report.py -q`

Expected: FAIL because resolver functions are missing.

- [ ] **Step 3: Implement strict resolution with pip dry-run report**

For a Linux ARM64 CPython 3.11 profile, build this exact argument shape: `python -m pip install --dry-run --ignore-installed --report report.json --only-binary=:all: --platform manylinux2014_aarch64 --python-version 3.11 --implementation cp --abi cp311 --abi abi3 --abi none --index-url https://pypi.org/simple -r requirements.txt`. Substitute only values from the validated TargetProfile and whitelisted PackageSource; every token remains a separate argv item. Parse report version `1`, require every artifact URL to end in `.whl`, and map direct/transitive metadata without treating the report as the final lock file.

- [ ] **Step 4: Run resolver tests against a local fixture index**

Run: `cd worker && .venv/bin/pytest tests/resolver/test_pip_report.py -q`

Expected: report parsing, timeout, nonzero exit, sdist rejection, and argv-injection tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker/src/wheelforge_worker/process.py worker/src/wheelforge_worker/resolver worker/tests/resolver/test_pip_report.py
git commit -m "feat(worker): add strict target resolver"
```

### Task 4: Bidirectional Compatibility Resolver

**Files:**
- Create: `worker/src/wheelforge_worker/resolver/candidates.py`
- Create: `worker/src/wheelforge_worker/resolver/compatible.py`
- Test: `worker/tests/resolver/test_compatible.py`

**Interfaces:**
- Produces: `ordered_candidates(original: Version, available: Iterable[Version]) -> list[Version]`.
- Produces: `CompatibleResolver.resolve(parsed: ParsedRequirements, profile: TargetProfile, sources: Sequence[PackageSource], limits: ResolveLimits) -> ResolutionResult` with attempt reasons.

- [ ] **Step 1: Write failing ordering tests**

```python
def test_prefers_nearest_patch_up_then_down_before_other_minor() -> None:
    available = map(Version, ["1.25.9", "1.26.3", "1.26.5", "1.27.0", "2.0.0"])
    assert ordered_candidates(Version("1.26.4"), available) == list(map(Version, ["1.26.5", "1.26.3", "1.27.0", "1.25.9"]))

def test_zero_major_never_crosses_minor() -> None:
    available = map(Version, ["0.28.9", "0.29.3", "0.29.5", "0.30.0"])
    assert ordered_candidates(Version("0.29.4"), available) == list(map(Version, ["0.29.5", "0.29.3"]))
```

- [ ] **Step 2: Run and verify failure**

Run: `cd worker && .venv/bin/pytest tests/resolver/test_compatible.py -q`

Expected: FAIL because candidate ordering is missing.

- [ ] **Step 3: Implement bounded candidate retries**

Exclude prereleases and yanked releases unless explicitly requested, filter by `Requires-Python` and target Wheel presence, preserve explicit ranges and `~=`, and only relax exact pins. Try at most 20 candidates per direct requirement, 100 total resolution attempts, and 10 minutes. Record `UPGRADE`, `DOWNGRADE`, `UNCHANGED`, or `UNRESOLVED` with every rejection reason.

- [ ] **Step 4: Run compatibility tests**

Run: `cd worker && .venv/bin/pytest tests/resolver/test_compatible.py -q`

Expected: nearest-version ordering, full-graph conflict rejection, range preservation, Python immutability, and attempt-limit tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker/src/wheelforge_worker/resolver/candidates.py worker/src/wheelforge_worker/resolver/compatible.py worker/tests/resolver/test_compatible.py
git commit -m "feat(worker): add bounded compatibility resolution"
```

### Task 5: Source Fallback Downloader and Static Validation

**Files:**
- Create: `worker/src/wheelforge_worker/download/sources.py`
- Create: `worker/src/wheelforge_worker/download/wheels.py`
- Create: `worker/src/wheelforge_worker/validation/static.py`
- Test: `worker/tests/download/test_wheels.py`

**Interfaces:**
- Produces: `WheelDownloader.download(resolved, profile, destination, cancel) -> list[DownloadedWheel]`.
- Produces: `validate_closure(resolved, wheels, profile) -> StaticValidationReport`.

- [ ] **Step 1: Write failing fallback tests**

```python
def test_download_falls_back_per_pinned_package(fake_sources, tmp_path, profile_cp311_arm64) -> None:
    fake_sources.tuna.fail("demo", "1.2.3")
    fake_sources.ali.serve("demo-1.2.3-py3-none-any.whl")
    wheel = downloader.download_one(PinnedPackage("demo", Version("1.2.3")), profile_cp311_arm64, tmp_path)
    assert wheel.source_code == "ALIYUN"
    assert len(wheel.sha256) == 64
```

- [ ] **Step 2: Run and verify failure**

Run: `cd worker && .venv/bin/pytest tests/download/test_wheels.py -q`

Expected: FAIL because downloader is absent.

- [ ] **Step 3: Implement TUNA to Aliyun to PyPI fallback**

Download each exact pin with `--no-deps`, target selectors, and `--only-binary=:all:`. Accept only configured source codes mapped to server-owned URLs. Validate canonical name/version/tag, enforce per-file and total byte limits while streaming, calculate SHA-256, reject duplicate filenames and path components, and poll cancellation between attempts.

- [ ] **Step 4: Run download and static validation tests**

Run: `cd worker && .venv/bin/pytest tests/download tests/validation/test_static.py -q`

Expected: fallback, hash, size limit, path rejection, dependency closure, Requires-Python, and wrong-platform tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker/src/wheelforge_worker/download worker/src/wheelforge_worker/validation/static.py worker/tests/download worker/tests/validation/test_static.py
git commit -m "feat(worker): download and statically validate Wheel sets"
```

### Task 6: Wheel Archive Integrity Validator

**Files:**
- Create: `worker/src/wheelforge_worker/validation/archive.py`
- Create: `worker/tests/validation/test_archive.py`

**Interfaces:**
- Produces: `validate_wheel_archive(path: Path, expected: DownloadedWheel, limits: ArchiveLimits) -> ArchiveValidationReport`.

- [ ] **Step 1: Write failing archive-safety and RECORD tests**

```python
def test_rejects_parent_path_without_extracting(tmp_path: Path, downloaded_wheel) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("../../escape.py", b"raise SystemExit")
    with pytest.raises(UnsafeWheelArchive, match="parent path"):
        validate_wheel_archive(wheel, downloaded_wheel, ArchiveLimits.defaults())


def test_rejects_record_hash_mismatch(wheel_with_bad_record, downloaded_wheel) -> None:
    with pytest.raises(WheelRecordMismatch):
        validate_wheel_archive(wheel_with_bad_record, downloaded_wheel, ArchiveLimits.defaults())
```

- [ ] **Step 2: Run and verify failure**

Run: `cd worker && .venv/bin/pytest tests/validation/test_archive.py -q`

Expected: FAIL because `validation.archive` is missing.

- [ ] **Step 3: Implement read-only Wheel archive validation**

Open the Wheel with `zipfile.ZipFile` without extracting it. Limit archives to 20,000 entries, 2 GiB total uncompressed bytes, 512 MiB per entry, and compression ratio 200. Reject absolute paths, `..`, backslashes, NUL, duplicate normalized paths, encrypted entries, and Unix symlink mode bits. Require exactly one matching `.dist-info/METADATA`, `WHEEL`, and `RECORD`; parse METADATA as email headers; verify Name and Version against `DownloadedWheel`; parse every RECORD CSV row; require every non-RECORD file to be listed; decode `sha256=` URL-safe base64 digests and verify bytes by streaming from the ZIP. Return `validation_level="STATIC"` and `install_verified=False`. Never import modules, execute entry points, or invoke a subprocess.

- [ ] **Step 4: Run archive validator tests**

Run: `cd worker && .venv/bin/pytest tests/validation/test_archive.py -q`

Expected: valid Wheel, missing metadata, path traversal, duplicate path, symlink, ZIP bomb limits, missing RECORD row, bad digest, Name mismatch, and Version mismatch tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker/src/wheelforge_worker/validation/archive.py worker/tests/validation/test_archive.py
git commit -m "feat(worker): statically validate Wheel archives"
```

### Task 7: Artifact Builder

**Files:**
- Create: `worker/src/wheelforge_worker/artifact/models.py`
- Create: `worker/src/wheelforge_worker/artifact/builder.py`
- Create: `worker/src/wheelforge_worker/artifact/templates/`
- Test: `worker/tests/artifact/test_builder.py`

**Interfaces:**
- Produces: `ArtifactBuilder.build(context, output_dir) -> BuiltArtifact`.

- [ ] **Step 1: Write a failing deterministic ZIP test**

```python
def test_build_contains_manifest_report_scripts_and_hashes(success_context, tmp_path) -> None:
    artifact = ArtifactBuilder().build(success_context, tmp_path)
    with ZipFile(artifact.path) as archive:
        names = set(archive.namelist())
    assert {"README.md", "manifest.json", "build-report.html", "version-comparison.csv", "checksums.sha256", "install.sh", "verify.sh"} <= names
    assert artifact.manifest["validationLevel"] == "STATIC"
    assert artifact.manifest["installVerified"] is False
    assert artifact.sha256 == sha256(artifact.path.read_bytes()).hexdigest()
```

- [ ] **Step 2: Run and verify failure**

Run: `cd worker && .venv/bin/pytest tests/artifact/test_builder.py -q`

Expected: FAIL because ArtifactBuilder is absent.

- [ ] **Step 3: Implement deterministic target-specific artifacts**

Generate exact hash-pinned Requirements, comparison CSV, escaped offline HTML report, Manifest, checksums, README, and only the target OS scripts. Manifest and report always state `validationLevel: STATIC`, `installVerified: false`, and `Static compatibility checks passed; target installation was not verified.` Scripts check OS/architecture/Python major-minor before using `--no-index --find-links --require-hashes`. Sort entries, use generated safe paths, fixed ZIP timestamps, and atomic rename after verification.

- [ ] **Step 4: Run artifact tests**

Run: `cd worker && .venv/bin/pytest tests/artifact -q`

Expected: Linux/Windows layouts, escaping, hashes, deterministic bytes, partial-report labeling, and Zip Slip tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker/src/wheelforge_worker/artifact worker/tests/artifact
git commit -m "feat(worker): package auditable offline artifacts"
```

### Task 8: Job Consumer and Pipeline Orchestration

**Files:**
- Create: `worker/src/wheelforge_worker/jobs/consumer.py`
- Create: `worker/src/wheelforge_worker/jobs/repository.py`
- Create: `worker/src/wheelforge_worker/jobs/pipeline.py`
- Create: `worker/src/wheelforge_worker/__main__.py`
- Test: `worker/tests/jobs/test_pipeline.py`

**Interfaces:**
- Produces: `python -m wheelforge_worker` long-running consumer.
- Produces: `REQUIREMENT_PARSE` handling plus conditional build claim, monotonic logs, cancellation, terminal-state, and Artifact publication behavior.

- [ ] **Step 1: Write failing duplicate-claim and cancellation tests**

```python
def test_only_one_execution_claims_a_queued_task(repository, queued_task) -> None:
    first = repository.claim(queued_task.id, UUID(int=1))
    second = repository.claim(queued_task.id, UUID(int=2))
    assert first is True
    assert second is False

def test_cancelled_pipeline_never_publishes_artifact(pipeline, cancel_after_download) -> None:
    result = pipeline.run(cancel_after_download.task_id)
    assert result.status is BuildStatus.CANCELLED
    assert result.artifact_id is None
```

- [ ] **Step 2: Run and verify failure**

Run: `cd worker && .venv/bin/pytest tests/jobs/test_pipeline.py -q`

Expected: FAIL because job orchestration is absent.

- [ ] **Step 3: Implement stage orchestration and terminal mapping**

Consume version-1 envelopes and dispatch by `job_type`. For `REQUIREMENT_PARSE`, atomically change the owned RequirementFile from `PENDING` to `PARSING`, read the original object, call `parse_requirements`, write the UTF-8 normalized object and RequirementItem rows, then set `PARSED` or `FAILED`. For `BUILD`, conditionally claim `QUEUED` tasks with `execution_id` and `version_no`, append monotonic logs, and invoke resolution/download/static-validation/package stages. Poll Redis and MySQL cancellation flags at every boundary. Publish Artifact metadata only after object upload and hash confirmation. Map complete static validation to `SUCCESS`, useful incomplete output to `PARTIAL_SUCCESS`, unrecoverable errors to `FAILED`, and requested cancellation to `CANCELLED`. No build stage creates a target container or virtual environment or executes dependency code.

- [ ] **Step 4: Run all Worker verification**

Run: `cd worker && .venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest -q`

Expected: lint, typing, unit, contract, and pipeline tests all pass.

- [ ] **Step 5: Commit**

```bash
git add worker/src/wheelforge_worker/jobs worker/src/wheelforge_worker/__main__.py worker/tests/jobs
git commit -m "feat(worker): orchestrate cancellable build jobs"
```
