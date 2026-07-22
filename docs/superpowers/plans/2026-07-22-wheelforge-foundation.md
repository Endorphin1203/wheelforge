# WheelForge Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a bootable, tested monorepo foundation with MySQL, local filesystem storage, database migrations, and a versioned Java/Python database-job contract.

**Architecture:** A Java 21 Spring Boot API and a Python 3.12 Worker share a MySQL schema, a versioned `build_jobs` contract, and a configured local data root. MySQL provides the durable queue; the filesystem stores uploaded Requirements and generated Artifacts; Flyway is the only schema owner.

**Tech Stack:** Java 21, Spring Boot 4.1.0, Maven, Python 3.12, pytest, MySQL 8.4 LTS, Flyway, local filesystem storage.

## Global Constraints

- MySQL 8.4 LTS is the production baseline; native MySQL 8.4 or newer may be used for development and integration tests. Use InnoDB, `utf8mb4`, and UTC timestamps.
- CPython Worker runtime is 3.12; target CPython versions are 3.9 through 3.13.
- Business IDs are application-generated UUID strings stored as `char(36)`.
- Database job payloads contain identifiers and immutable snapshots, never file bytes or credentials.
- V1 must run without Docker, Redis, or MinIO.
- Persistent paths are system-generated object keys resolved beneath one absolute data root.
- Flyway migrations are forward-only; Hibernate uses `ddl-auto: validate`.
- No shell command concatenation and no arbitrary package-source URLs.
- Every task follows red-green-refactor and ends with a focused commit.

---

## File Map

- `pom.xml`: Maven parent and Java version/dependency management.
- `backend/pom.xml`: Spring API dependencies and test tooling.
- `backend/src/main/java/com/wheelforge/api/WheelForgeApplication.java`: API entry point.
- `backend/src/main/resources/application.yml`: shared runtime configuration.
- `backend/src/test/java/com/wheelforge/api/WheelForgeApplicationTest.java`: Java smoke test.
- `worker/pyproject.toml`: Python package, runtime dependencies, pytest, Ruff, and mypy settings.
- `worker/src/wheelforge_worker/`: Worker package.
- `worker/tests/`: Worker unit and contract tests.
- `.env.example`: non-secret local defaults.
- `backend/src/main/resources/db/migration/V1__baseline.sql`: complete initial schema.
- `contracts/job-payload-v1.schema.json`: language-neutral database-job payload contract.
- `contracts/examples/*.json`: cross-language contract fixtures.
- `Makefile`: repeatable bootstrap, test, lint, and service commands.

### Task 1: Bootable Java and Python Modules

**Files:**
- Create: `pom.xml`
- Create: `backend/pom.xml`
- Create: `backend/src/main/java/com/wheelforge/api/WheelForgeApplication.java`
- Create: `backend/src/test/java/com/wheelforge/api/WheelForgeApplicationTest.java`
- Create: `worker/pyproject.toml`
- Create: `worker/src/wheelforge_worker/__init__.py`
- Create: `worker/tests/test_package.py`
- Create: `.gitignore`
- Create: `Makefile`

**Interfaces:**
- Produces: Java base package `com.wheelforge.api` and Python package `wheelforge_worker`.
- Produces: `make test` as the repository-wide verification entry point.

- [ ] **Step 1: Write failing smoke tests**

```java
package com.wheelforge.api;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.ApplicationContext;
import org.springframework.boot.test.context.SpringBootTest;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
class WheelForgeApplicationTest {
    @Autowired
    private ApplicationContext context;

    @Test
    void contextLoads() {
        assertThat(context.getBean(WheelForgeApplication.class)).isNotNull();
    }
}
```

```python
from wheelforge_worker import __version__


def test_package_version() -> None:
    assert __version__ == "0.1.0"
```

- [ ] **Step 2: Run tests and verify the empty repository fails**

Run: `mvn -q -pl backend test && python3.12 -m pytest worker/tests -q`

Expected: Maven reports a missing `pom.xml`, proving no implementation exists.

- [ ] **Step 3: Add minimal build files and entry points**

Use Spring Boot `4.1.0`, Java `21`, and dependencies `spring-boot-starter-web`, `spring-boot-starter-validation`, `spring-boot-starter-actuator`, and `spring-boot-starter-test`. Configure `worker/pyproject.toml` with Python `>=3.12,<3.13`, package version `0.1.0`, and test dependencies `pytest`, `pytest-cov`, `ruff`, and `mypy`.

```java
package com.wheelforge.api;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class WheelForgeApplication {
    public static void main(String[] args) {
        SpringApplication.run(WheelForgeApplication.class, args);
    }
}
```

```python
__version__ = "0.1.0"
```

Set `Makefile` targets to:

```make
.PHONY: test lint
test:
	./mvnw -q -pl backend test
	cd worker && .venv/bin/python -m pytest -q

lint:
	cd worker && .venv/bin/ruff check src tests
	cd worker && .venv/bin/mypy src
```

- [ ] **Step 4: Run smoke tests**

Run: `./mvnw -q -pl backend test`

Expected: `BUILD SUCCESS`.

Run: `cd worker && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q`

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add pom.xml backend worker .gitignore Makefile mvnw mvnw.cmd .mvn
git commit -m "build: scaffold WheelForge API and worker"
```

### Task 2: Native MySQL and Local Storage Configuration

**Files:**
- Modify: `.gitignore`
- Create: `.env.example`
- Create: `backend/src/main/resources/application.yml`
- Create: `worker/src/wheelforge_worker/settings.py`
- Create: `worker/tests/test_settings.py`

**Interfaces:**
- Consumes: a natively installed MySQL service at `localhost:3306` by default.
- Produces: `Settings.from_env() -> Settings` with validated database, local data, workspace, polling, and lease values.

- [ ] **Step 1: Write a failing Worker settings test**

```python
from pathlib import Path

from wheelforge_worker.settings import Settings


def test_settings_require_absolute_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WF_DATABASE_URL", "mysql+pymysql://wf:wf@localhost:3306/wheelforge")
    monkeypatch.setenv("WF_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("WF_WORKSPACE_ROOT", str(tmp_path / "work"))

    settings = Settings.from_env()

    assert settings.data_root == tmp_path / "data"
    assert settings.workspace_root == tmp_path / "work"
    assert settings.database_url.startswith("mysql+pymysql://")
```

- [ ] **Step 2: Verify the settings test fails**

Run: `cd worker && .venv/bin/pytest tests/test_settings.py -q`

Expected: FAIL with `ModuleNotFoundError: wheelforge_worker.settings`.

- [ ] **Step 3: Implement typed native configuration**

Define an immutable `Settings` dataclass with `database_url`, absolute `data_root`, absolute `workspace_root`, `queue_poll_seconds`, `job_lease_seconds`, and `worker_id`. Reject relative roots, equal data/workspace roots, non-positive polling, and a lease shorter than 30 seconds. Do not create directories while parsing settings.

Set `.env.example` to:

```dotenv
WF_JDBC_URL=jdbc:mysql://localhost:3306/wheelforge?connectionTimeZone=UTC
WF_DATABASE_URL=mysql+pymysql://wheelforge:wheelforge_local@localhost:3306/wheelforge
WF_DATABASE_USER=wheelforge
WF_DATABASE_PASSWORD=wheelforge_local
WF_DATA_ROOT=/absolute/path/to/wheelforge-data
WF_WORKSPACE_ROOT=/absolute/path/to/wheelforge-work
WF_QUEUE_POLL_SECONDS=2
WF_JOB_LEASE_SECONDS=60
WF_WORKER_ID=wheelforge-worker-1
```

Set Spring datasource URL, username, password, and `wheelforge.storage.data-root` from those environment variables. Keep Hibernate `ddl-auto: validate` and Flyway enabled. Ignore `.env`, `/data/`, and `/work/` in Git. No Redis, S3, MinIO, Docker, or Compose configuration may appear.

- [ ] **Step 4: Run configuration checks**

Run: `cd worker && .venv/bin/pytest tests/test_settings.py -q`

Expected: absolute-root, relative-root, equal-root, polling, and lease tests pass.

Run: `rg -n "redis|minio|docker|compose|s3" .env.example backend/src/main/resources/application.yml worker/src/wheelforge_worker/settings.py`

Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add .gitignore .env.example backend/src/main/resources/application.yml worker/src/wheelforge_worker/settings.py worker/tests/test_settings.py
git commit -m "build: configure native MySQL and local storage"
```

### Task 3: Baseline MySQL Schema

**Files:**
- Modify: `backend/pom.xml`
- Create: `backend/src/main/resources/db/migration/V1__baseline.sql`
- Create: `backend/src/test/java/com/wheelforge/api/db/BaselineMigrationTest.java`
- Create: `backend/src/test/java/com/wheelforge/api/db/BaselineMigrationSqlTest.java`

**Interfaces:**
- Produces: tables `users`, `requirement_files`, `requirement_items`, `target_profiles`, `build_tasks`, `build_jobs`, `resolved_packages`, `build_logs`, `package_sources`, `artifacts`, `download_records`, and `system_config`.
- Produces: `version_no bigint not null default 0` on mutable aggregate tables.

Add `spring-boot-starter-data-jpa`, `flyway-core`, `flyway-mysql`, and the MySQL Connector/J runtime dependency. Do not add Testcontainers or an embedded database that silently changes MySQL semantics.

- [ ] **Step 1: Write a failing native-MySQL migration test**

```java
@EnabledIfEnvironmentVariable(named = "WF_TEST_JDBC_URL", matches = ".+")
class BaselineMigrationTest {
    @Test
    void createsCoreTables() throws Exception {
        var flyway = Flyway.configure()
            .dataSource(
                System.getenv("WF_TEST_JDBC_URL"),
                System.getenv("WF_TEST_DATABASE_USER"),
                System.getenv("WF_TEST_DATABASE_PASSWORD"))
            .locations("classpath:db/migration")
            .load();
        flyway.migrate();
        try (var connection = flyway.getConfiguration().getDataSource().getConnection()) {
            assertThat(tableExists(connection, "build_tasks")).isTrue();
            assertThat(tableExists(connection, "build_jobs")).isTrue();
        }
    }
}
```

The test connects only to a disposable native MySQL database supplied through environment variables. It never starts a container. Add a separate always-on `BaselineMigrationSqlTest` that reads the migration resource and asserts the required table declarations and `engine=InnoDB default charset=utf8mb4`, so `make verify` still catches accidental migration removal when the integration database is unavailable.

- [ ] **Step 2: Verify migration test fails**

Run: `WF_TEST_JDBC_URL='jdbc:mysql://localhost:3306/wheelforge_test?connectionTimeZone=UTC' WF_TEST_DATABASE_USER=wheelforge WF_TEST_DATABASE_PASSWORD=wheelforge_local ./mvnw -q -pl backend -Dtest=BaselineMigrationTest,BaselineMigrationSqlTest test`

Expected: FAIL because `V1__baseline.sql` does not exist.

- [ ] **Step 3: Add the complete baseline migration**

Use `char(36)` UUID primary keys, `datetime(6)` UTC timestamps, JSON only for bounded structured context, and foreign keys with restrictive deletes. Include these required columns:

```sql
create table users (
  id char(36) primary key,
  username varchar(100) not null unique,
  password_hash varchar(255) not null,
  role varchar(20) not null,
  status varchar(20) not null,
  created_at datetime(6) not null
) engine=InnoDB default charset=utf8mb4;

create table requirement_files (
  id char(36) primary key,
  user_id char(36) not null,
  original_name varchar(255) not null,
  detected_encoding varchar(20),
  size_bytes bigint not null,
  sha256 char(64) not null,
  original_object_key varchar(512) not null,
  normalized_object_key varchar(512),
  parse_status varchar(20) not null,
  parse_error varchar(2000),
  created_at datetime(6) not null,
  version_no bigint not null default 0,
  constraint fk_requirement_file_user foreign key (user_id) references users(id)
) engine=InnoDB default charset=utf8mb4;

create table target_profiles (
  id char(36) primary key,
  code varchar(100) not null unique,
  os varchar(20) not null,
  architecture varchar(20) not null,
  python_implementation varchar(20) not null,
  python_version varchar(10) not null,
  python_full_version varchar(20) not null,
  platform_tag varchar(100) not null,
  abi_tags json not null,
  validation_type varchar(20) not null,
  validation_policy_version varchar(50) not null,
  enabled boolean not null,
  version_no bigint not null default 0
) engine=InnoDB default charset=utf8mb4;

create table build_tasks (
  id char(36) primary key,
  user_id char(36) not null,
  requirement_file_id char(36) not null,
  target_profile_id char(36) not null,
  source_task_id char(36),
  execution_id char(36),
  status varchar(30) not null,
  progress int not null default 0,
  current_stage varchar(50),
  solve_mode varchar(20) not null,
  target_snapshot json not null,
  cancel_requested boolean not null default false,
  failure_code varchar(100),
  failure_message varchar(2000),
  created_at datetime(6) not null,
  started_at datetime(6),
  finished_at datetime(6),
  deleted_at datetime(6),
  version_no bigint not null default 0,
  constraint fk_build_user foreign key (user_id) references users(id),
  constraint fk_build_file foreign key (requirement_file_id) references requirement_files(id),
  constraint fk_build_profile foreign key (target_profile_id) references target_profiles(id)
) engine=InnoDB default charset=utf8mb4;

create table build_jobs (
  id char(36) primary key,
  job_type varchar(30) not null,
  payload_version int not null,
  subject_id char(36) not null,
  payload_json json not null,
  status varchar(20) not null,
  priority_no int not null default 100,
  available_at datetime(6) not null,
  attempts int not null default 0,
  max_attempts int not null default 3,
  lease_owner varchar(100),
  execution_id char(36),
  lease_expires_at datetime(6),
  heartbeat_at datetime(6),
  last_error varchar(2000),
  created_at datetime(6) not null,
  started_at datetime(6),
  finished_at datetime(6),
  version_no bigint not null default 0,
  index ix_job_ready (status, available_at, priority_no, created_at),
  index ix_job_lease (status, lease_expires_at)
) engine=InnoDB default charset=utf8mb4;
```

Create these seven additional tables and their indexes in the same migration:

```sql
create table requirement_items (
  id char(36) primary key,
  requirement_file_id char(36) not null,
  line_no int not null,
  normalized_name varchar(255) not null,
  extras_json json not null,
  specifier varchar(500) not null,
  marker_text varchar(1000),
  original_text varchar(2000) not null,
  supported boolean not null,
  error_code varchar(100),
  error_message varchar(2000),
  constraint fk_item_file foreign key (requirement_file_id) references requirement_files(id),
  index ix_item_file (requirement_file_id)
) engine=InnoDB default charset=utf8mb4;

create table resolved_packages (
  id char(36) primary key,
  build_task_id char(36) not null,
  normalized_name varchar(255) not null,
  final_version varchar(100),
  dependency_type varchar(20) not null,
  original_constraint varchar(500),
  strict_version varchar(100),
  change_direction varchar(30) not null,
  change_reason varchar(1000),
  attempts_json json not null,
  wheel_filename varchar(500),
  wheel_tags json,
  package_source_code varchar(50),
  sha256 char(64),
  wheel_status varchar(30) not null,
  error_message varchar(2000),
  constraint fk_resolved_task foreign key (build_task_id) references build_tasks(id),
  unique key uk_resolved_task_name (build_task_id, normalized_name),
  index ix_resolved_task (build_task_id)
) engine=InnoDB default charset=utf8mb4;

create table build_logs (
  id char(36) primary key,
  build_task_id char(36) not null,
  sequence_no bigint not null,
  stage varchar(50) not null,
  level varchar(20) not null,
  message varchar(4000) not null,
  context_json json,
  created_at datetime(6) not null,
  constraint fk_log_task foreign key (build_task_id) references build_tasks(id),
  unique key uk_log_sequence (build_task_id, sequence_no),
  index ix_log_cursor (build_task_id, sequence_no)
) engine=InnoDB default charset=utf8mb4;

create table package_sources (
  id char(36) primary key,
  code varchar(50) not null unique,
  display_name varchar(100) not null,
  base_url varchar(500) not null,
  priority_no int not null,
  enabled boolean not null,
  timeout_seconds int not null,
  failure_count bigint not null default 0,
  version_no bigint not null default 0,
  updated_at datetime(6) not null
) engine=InnoDB default charset=utf8mb4;

create table artifacts (
  id char(36) primary key,
  build_task_id char(36) not null,
  artifact_type varchar(30) not null,
  filename varchar(255) not null,
  object_key varchar(512) not null unique,
  size_bytes bigint not null,
  sha256 char(64) not null,
  build_status varchar(30) not null,
  validation_type varchar(30) not null,
  expires_at datetime(6) not null,
  download_count bigint not null default 0,
  cleaned_at datetime(6),
  created_at datetime(6) not null,
  version_no bigint not null default 0,
  constraint fk_artifact_task foreign key (build_task_id) references build_tasks(id),
  index ix_artifact_task (build_task_id),
  index ix_artifact_expiry (expires_at, cleaned_at)
) engine=InnoDB default charset=utf8mb4;

create table download_records (
  id char(36) primary key,
  artifact_id char(36) not null,
  user_id char(36) not null,
  ip_address varchar(45) not null,
  user_agent varchar(1000),
  completed boolean not null,
  downloaded_at datetime(6) not null,
  constraint fk_download_artifact foreign key (artifact_id) references artifacts(id),
  constraint fk_download_user foreign key (user_id) references users(id),
  index ix_download_artifact (artifact_id),
  index ix_download_user_time (user_id, downloaded_at)
) engine=InnoDB default charset=utf8mb4;

create table system_config (
  config_key varchar(100) primary key,
  config_value json not null,
  description varchar(500) not null,
  updated_by char(36),
  updated_at datetime(6) not null,
  version_no bigint not null default 0,
  constraint fk_config_user foreign key (updated_by) references users(id)
) engine=InnoDB default charset=utf8mb4;

create index ix_requirement_file_user on requirement_files(user_id, created_at);
create index ix_build_user_created on build_tasks(user_id, created_at);
create index ix_build_file on build_tasks(requirement_file_id);
create index ix_build_profile on build_tasks(target_profile_id);
```

- [ ] **Step 4: Run migration tests**

Run the always-on structural test, then run the native integration test with the `WF_TEST_*` variables shown above.

Expected: both pass and Flyway reports schema version `1`; no Docker process or Testcontainers dependency is involved.

- [ ] **Step 5: Commit**

```bash
git add backend/pom.xml backend/src/main/resources/db/migration backend/src/test/java/com/wheelforge/api/db
git commit -m "feat: add MySQL baseline schema"
```

### Task 4: Versioned Database Job and State Contracts

**Files:**
- Modify: `worker/pyproject.toml`
- Create: `contracts/job-payload-v1.schema.json`
- Create: `contracts/examples/requirement-parse-v1.json`
- Create: `contracts/examples/build-v1.json`
- Create: `backend/src/main/java/com/wheelforge/api/contracts/JobPayload.java`
- Create: `backend/src/main/java/com/wheelforge/api/build/BuildStatus.java`
- Create: `backend/src/test/java/com/wheelforge/api/contracts/JobPayloadContractTest.java`
- Create: `worker/src/wheelforge_worker/contracts.py`
- Create: `worker/tests/test_contracts.py`

**Interfaces:**
- Produces: `JobPayload(schema_version, job_type, subject_id, created_at, payload)` in both languages; the serialized value is stored in `build_jobs.payload_json`.
- Produces: `BuildStatus` values exactly matching the approved state machine.

Add Pydantic `>=2,<3` as a Worker runtime dependency; do not rely on a globally installed package.

- [ ] **Step 1: Add failing cross-language fixture tests**

```python
def test_build_fixture_round_trips() -> None:
    raw = Path("../contracts/examples/build-v1.json").read_text()
    payload = JobPayload.model_validate_json(raw)
    assert payload.schema_version == 1
    assert payload.job_type is JobType.BUILD
    assert payload.payload["targetSnapshot"]["pythonVersion"] == "3.11"
```

```java
@Test
void readsBuildFixture() throws Exception {
    var json = Files.readString(Path.of("../contracts/examples/build-v1.json"));
    var payload = objectMapper.readValue(json, JobPayload.class);
    assertThat(payload.schemaVersion()).isEqualTo(1);
    assertThat(payload.jobType()).isEqualTo("BUILD");
}
```

- [ ] **Step 2: Verify both tests fail**

Run: `./mvnw -q -pl backend -Dtest=JobPayloadContractTest test`

Expected: FAIL because `JobPayload` is missing.

Run: `cd worker && .venv/bin/pytest tests/test_contracts.py -q`

Expected: FAIL because `contracts.py` is missing.

- [ ] **Step 3: Implement the frozen V1 database payload**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["schemaVersion", "jobType", "subjectId", "createdAt", "payload"],
  "properties": {
    "schemaVersion": {"const": 1},
    "jobType": {"enum": ["REQUIREMENT_PARSE", "BUILD"]},
    "subjectId": {"type": "string", "format": "uuid"},
    "createdAt": {"type": "string", "format": "date-time"},
    "payload": {"type": "object"}
  },
  "additionalProperties": false
}
```

Define BuildTask states `CREATED`, `PARSING`, `QUEUED`, `RESOLVING`, `DOWNLOADING`, `VALIDATING`, `PACKAGING`, `SUCCESS`, `PARTIAL_SUCCESS`, `FAILED`, and `CANCELLED`; define database job states `READY`, `RUNNING`, `COMPLETED`, `FAILED`, and `CANCELLED`. Reject unknown schema versions and job types in both languages. The database row supplies job ID, lease, attempts, and execution ID; do not duplicate those mutable fields inside the immutable payload JSON.

- [ ] **Step 4: Run contract tests**

Run: `./mvnw -q -pl backend -Dtest=JobPayloadContractTest test && cd worker && .venv/bin/pytest tests/test_contracts.py -q`

Expected: all contract fixture tests pass.

- [ ] **Step 5: Commit**

```bash
git add contracts backend/src/main/java/com/wheelforge/api/contracts backend/src/main/java/com/wheelforge/api/build/BuildStatus.java backend/src/test/java/com/wheelforge/api/contracts worker/pyproject.toml worker/src/wheelforge_worker/contracts.py worker/tests/test_contracts.py
git commit -m "feat: define versioned database job contracts"
```

### Task 5: Foundation Verification

**Files:**
- Modify: `Makefile`
- Create: `docs/development.md`

**Interfaces:**
- Produces: `make verify`, `make verify-mysql`, and a documented native startup sequence.

- [ ] **Step 1: Add a failing repository verification target**

```make
.PHONY: verify
verify:
	./mvnw -q -pl backend test
	cd worker && .venv/bin/ruff check src tests
	cd worker && .venv/bin/mypy src
	cd worker && .venv/bin/pytest -q
```

- [ ] **Step 2: Run the full verification target**

Run: `make verify`

Expected: FAIL until formatting, typing, and all contract tests are clean.

- [ ] **Step 3: Apply deterministic formatters and document setup**

Run `./mvnw -q -pl backend spotless:apply` and `cd worker && .venv/bin/ruff format src tests`, then document exact prerequisites, native MySQL 8.4+ installation/startup, creation of `wheelforge` and disposable `wheelforge_test` databases, least-privilege users, `.env.example` usage, creation and permissions of the data/workspace roots, Java/Python dependency installation, `make verify`, and `make verify-mysql` in `docs/development.md`. Add Spotless to `backend/pom.xml` if Task 1 did not already include it.

`make verify-mysql` must require explicit `WF_TEST_JDBC_URL`, `WF_TEST_DATABASE_USER`, and `WF_TEST_DATABASE_PASSWORD` variables and run `BaselineMigrationTest`; it must fail with a clear message if they are absent. Do not add Docker, Redis, MinIO, or Testcontainers setup.

- [ ] **Step 4: Re-run verification**

Run: `make verify`

Expected: exit code `0` with Java tests, Python tests, lint, and type checking passing. With a disposable native MySQL database configured, `make verify-mysql` also passes.

- [ ] **Step 5: Commit**

```bash
git add Makefile docs/development.md
git commit -m "docs: add foundation verification workflow"
```
