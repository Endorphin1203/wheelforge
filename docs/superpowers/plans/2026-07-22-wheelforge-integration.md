# WheelForge Integration and Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the API and Worker operate as one secure offline-build system and package a reproducible single-machine deployment.

**Architecture:** Black-box tests start MySQL, Redis, MinIO, API, Worker, a local PEP 503 fixture index, and controlled validator images. Tests drive only HTTP APIs and inspect downloadable artifacts, while failure injection verifies fallback, cancellation, ownership, and cleanup.

**Tech Stack:** Docker Compose, MySQL 8.4 LTS, Redis, MinIO, Spring Boot, Python Worker, pytest, REST Assured, local Wheel fixtures, QEMU/binfmt for ARM64 validation.

## Global Constraints

- Execute the foundation, API, and Worker plans in that order first.
- End-to-end tests use a local fixture index and never depend on public internet availability.
- Linux success requires offline install plus `pip check`; Windows success is labeled static validation.
- A partial artifact is never presented as a guaranteed-installable success artifact.
- All runtime images and target profiles use explicit immutable image references in release configuration.

---

## File Map

- `test-fixtures/index/`: local PEP 503 package index and deterministic Wheels.
- `test-fixtures/projects/`: Requirements inputs for success, downgrade, conflict, missing, and malicious cases.
- `integration-tests/`: API-driven end-to-end tests.
- `deploy/compose.yaml`: single-machine production topology.
- `deploy/validator-images/`: Linux x86_64 and ARM64 CPython validator definitions.
- `scripts/bootstrap-target-profiles.sh`: idempotent profile/source bootstrap through admin API.
- `docs/operations.md`: startup, QEMU, limits, backup, retention, and troubleshooting.
- `docs/v1-acceptance.md`: requirement-to-test traceability.

### Task 1: Deterministic Local Package Index

**Files:**
- Create: `test-fixtures/packages/demo_common/pyproject.toml`
- Create: `test-fixtures/packages/demo_native/pyproject.toml`
- Create: `test-fixtures/build-fixtures.sh`
- Create: `test-fixtures/projects/*.txt`
- Test: `integration-tests/test_fixture_index.py`

**Interfaces:**
- Produces: pure Wheels for direct/transitive dependency tests and renamed tag fixtures for rejection tests.
- Produces: local Simple API at `http://fixture-index:8080/simple`.

- [ ] **Step 1: Write a failing fixture-index test**

```python
def test_fixture_index_exposes_only_wheels(http_client) -> None:
    page = http_client.get("http://fixture-index:8080/simple/demo-common/")
    assert page.status_code == 200
    assert ".whl" in page.text
    assert ".tar.gz" not in page.text
```

- [ ] **Step 2: Run and verify connection failure**

Run: `pytest integration-tests/test_fixture_index.py -q`

Expected: FAIL because the fixture index is not running.

- [ ] **Step 3: Build deterministic fixtures**

Create package versions that exercise unchanged `1.2.3`, upgrade `1.2.4`, downgrade `1.2.2`, transitive dependencies, conflicting ranges, missing ARM64 Wheel, wrong `cp310`, valid `cp311`, `abi3`, and `py3-none-any`. Build with `python -m build --wheel`, generate static Simple API HTML with SHA-256 fragments, and serve it with an unprivileged Nginx container.

- [ ] **Step 4: Run fixture tests**

Run: `docker compose -f deploy/compose.test.yaml up -d fixture-index && pytest integration-tests/test_fixture_index.py -q`

Expected: fixture package pages and hashes pass without internet access.

- [ ] **Step 5: Commit**

```bash
git add test-fixtures integration-tests/test_fixture_index.py deploy/compose.test.yaml
git commit -m "test: add deterministic Wheel fixture index"
```

### Task 2: Upload, Parse, and Build Happy Path

**Files:**
- Create: `integration-tests/conftest.py`
- Create: `integration-tests/test_linux_arm64_build.py`
- Create: `integration-tests/helpers/artifact.py`

**Interfaces:**
- Verifies: login, upload, parse polling, target selection, build polling, log cursor, artifact download, manifest, version comparison, and checksums.

- [ ] **Step 1: Write the failing API-driven scenario**

```python
def test_linux_arm64_cp311_build(api, fixtures, artifact_reader) -> None:
    file_id = api.upload_requirements(fixtures / "success.txt")
    api.wait_for_parse(file_id, "PARSED")
    task_id = api.create_build(file_id, "linux-arm64-cp311-manylinux2014")
    result = api.wait_for_terminal(task_id)
    assert result["status"] == "SUCCESS"
    artifact = artifact_reader.open(api.download_artifact(result["artifactId"]))
    assert artifact.manifest["target"]["pythonVersion"] == "3.11"
    assert artifact.verify_checksums()
```

- [ ] **Step 2: Run and verify failure before stack wiring**

Run: `pytest integration-tests/test_linux_arm64_build.py -q`

Expected: FAIL because the complete test stack is not connected.

- [ ] **Step 3: Wire test Compose and seed profiles**

Add API and Worker services, health-based dependencies, isolated credentials, test source codes, and a CPython 3.11 Linux ARM64 profile. Ensure the validation container has no network and receives only the wheelhouse and hash-pinned Requirements.

- [ ] **Step 4: Run the happy path twice**

Run: `pytest integration-tests/test_linux_arm64_build.py -q --count=2`

Expected: both builds reach `SUCCESS`; each ZIP passes hashes and contains only Linux scripts.

- [ ] **Step 5: Commit**

```bash
git add integration-tests deploy/compose.test.yaml
git commit -m "test: verify Linux ARM64 offline build flow"
```

### Task 3: Compatibility, Failure, and Cancellation Scenarios

**Files:**
- Create: `integration-tests/test_compatibility_results.py`
- Create: `integration-tests/test_failure_and_cancel.py`

**Interfaces:**
- Verifies: unchanged/up/down/missing comparison rows, target Python immutability, fallback attribution, partial success, cancellation, and retry linkage.

- [ ] **Step 1: Write failing result assertions**

```python
def test_compatibility_table_is_explainable(api, parsed_file) -> None:
    task = api.build_and_wait(parsed_file, "linux-arm64-cp311-manylinux2014")
    rows = {row["packageName"]: row for row in api.version_comparison(task["id"])}
    assert rows["unchanged-demo"]["changeDirection"] == "UNCHANGED"
    assert rows["upgrade-demo"]["changeDirection"] == "UPGRADE"
    assert rows["downgrade-demo"]["changeDirection"] == "DOWNGRADE"
    assert rows["missing-demo"]["wheelStatus"] == "MISSING"
```

- [ ] **Step 2: Run and verify failures expose missing integration behavior**

Run: `pytest integration-tests/test_compatibility_results.py integration-tests/test_failure_and_cancel.py -q`

Expected: at least one scenario fails before failure injection and cancellation wiring are complete.

- [ ] **Step 3: Add deterministic failure injection**

Configure fixture-source responses for TUNA failure and Aliyun success, a delayed download for running cancellation, a hard conflict, and no `cp311` candidate. Assert the task never changes target Python, cancelled tasks publish no Artifact, retries have new IDs, and partial reports state they are not guaranteed installable.

- [ ] **Step 4: Run scenario tests**

Run: `pytest integration-tests/test_compatibility_results.py integration-tests/test_failure_and_cancel.py -q`

Expected: all compatibility, fallback, partial, failed, cancelled, and retry scenarios pass.

- [ ] **Step 5: Commit**

```bash
git add integration-tests/test_compatibility_results.py integration-tests/test_failure_and_cancel.py test-fixtures
git commit -m "test: cover compatibility and terminal build outcomes"
```

### Task 4: Security and Resource Boundary Tests

**Files:**
- Create: `integration-tests/test_security_boundaries.py`
- Create: `integration-tests/test_resource_limits.py`

**Interfaces:**
- Verifies: cross-user isolation, admin boundaries, upload grammar, path safety, source whitelist, size/count/time limits, and no-network validation.

- [ ] **Step 1: Write failing security matrix tests**

```python
@pytest.mark.parametrize("line", ["git+https://example/x.git", "-e .", "pkg @ https://example/x.whl", "-r other.txt", "../local"])
def test_unsafe_requirement_never_reaches_build_queue(api, line: str) -> None:
    file_id = api.upload_text(line + "\n")
    parsed = api.wait_for_parse(file_id, "FAILED")
    assert parsed["errorCode"] == "UNSUPPORTED_REQUIREMENT_SYNTAX"
    assert api.build_jobs_for(file_id) == []
```

- [ ] **Step 2: Run and identify unmet boundaries**

Run: `pytest integration-tests/test_security_boundaries.py integration-tests/test_resource_limits.py -q`

Expected: FAIL for every boundary not yet enforced end to end.

- [ ] **Step 3: Wire every named boundary to its owning component**

Set multipart 512 KiB limits in `application.yml`; enforce 2,000 lines in `parser/requirements.py`; read package/file/total byte and timeout limits from `system_config` in the Worker pipeline; add `user_id` predicates to artifact, task, file, and log repositories; reject `base_url` mutations in `AdminController`; generate object keys in `RequirementFileService` and `ArtifactService`; generate ZIP entry names in `ArtifactBuilder`; force validator `--network=none`; and add a log-scrubbing test for database, Redis, and MinIO credentials.

- [ ] **Step 4: Run security and full regression suites**

Run: `pytest integration-tests/test_security_boundaries.py integration-tests/test_resource_limits.py -q && make verify`

Expected: all boundary tests and repository tests pass.

- [ ] **Step 5: Commit**

```bash
git add integration-tests backend worker
git commit -m "test: enforce end-to-end security boundaries"
```

### Task 5: Single-Machine Deployment and Acceptance Matrix

**Files:**
- Create: `deploy/compose.yaml`
- Create: `deploy/.env.example`
- Create: `deploy/validator-images/linux/Dockerfile`
- Create: `scripts/bootstrap-target-profiles.sh`
- Create: `docs/operations.md`
- Create: `docs/v1-acceptance.md`

**Interfaces:**
- Produces: a documented single-machine deployment and traceable V1 acceptance evidence.

- [ ] **Step 1: Add a failing deployment smoke script**

```bash
#!/usr/bin/env bash
set -euo pipefail
curl --fail --silent http://localhost:8080/actuator/health/readiness | jq -e '.status == "UP"'
docker compose -f deploy/compose.yaml exec -T redis redis-cli ping | grep -qx PONG
docker compose -f deploy/compose.yaml exec -T mysql mysqladmin ping -uroot -p"${MYSQL_ROOT_PASSWORD}"
```

- [ ] **Step 2: Run and verify failure before deployment files exist**

Run: `bash deploy/smoke.sh`

Expected: FAIL because the production Compose stack is absent.

- [ ] **Step 3: Add pinned deployment topology and operations guide**

Run API and Worker as non-root, mount only dedicated data/work directories, configure health checks and restart policies, isolate the validator network, and document QEMU/binfmt installation, backup/restore, credential rotation, capacity limits, Artifact retention, cancellation cleanup, log inspection, and Windows static-verification wording. Map every design acceptance item to an automated test name or an explicit operator check in `docs/v1-acceptance.md`.

- [ ] **Step 4: Run final acceptance**

Run: `docker compose -f deploy/compose.yaml config -q && bash deploy/smoke.sh && make verify && pytest integration-tests -q`

Expected: Compose validates, services are healthy, all unit/integration tests pass, and the acceptance matrix has no uncovered V1 requirement.

- [ ] **Step 5: Commit**

```bash
git add deploy scripts/bootstrap-target-profiles.sh docs/operations.md docs/v1-acceptance.md
git commit -m "ops: add single-machine deployment and V1 acceptance"
```
