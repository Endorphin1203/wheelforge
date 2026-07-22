# WheelForge Spring API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the authenticated Spring Boot API for uploads, target profiles, build lifecycle, logs, version comparisons, artifacts, and administration.

**Architecture:** Controllers expose versioned REST resources, application services enforce ownership and state transitions, JPA repositories persist both business data and durable jobs to the Flyway-owned MySQL schema, and a rooted local-file adapter stores files behind authorization checks.

**Tech Stack:** Java 21, Spring Boot 4.1.0, Spring Security, Spring Data JPA, Flyway, MySQL 8.4 LTS, Java NIO local storage, JUnit 5, AssertJ.

## Global Constraints

- Execute `2026-07-22-wheelforge-foundation.md` first.
- Every ordinary-user query includes `user_id`; only administrators may query global resources.
- Build target fields are immutable after creation.
- Deletes are soft deletes; retention jobs perform physical object cleanup.
- API errors use one JSON shape: `code`, `message`, `fieldErrors`, `traceId`.
- Business records and corresponding `build_jobs` rows are inserted in the same MySQL transaction.
- The API never exposes absolute host paths and never accepts a client-supplied storage key.
- V1 has no Docker, Redis, MinIO, S3, presigned URL, or Testcontainers dependency.
- Passwords use Argon2id; access tokens expire after 30 minutes.
- Every build result reports `validationLevel: STATIC` and `installVerified: false`; no API copy claims that installation was verified.

---

## File Map

- `backend/src/main/java/com/wheelforge/api/security/`: authentication and authorization.
- `backend/src/main/java/com/wheelforge/api/target/`: target capability queries.
- `backend/src/main/java/com/wheelforge/api/requirements/`: upload and parse-result resources.
- `backend/src/main/java/com/wheelforge/api/build/`: task state machine and commands.
- `backend/src/main/java/com/wheelforge/api/artifact/`: authorized artifact downloads.
- `backend/src/main/java/com/wheelforge/api/admin/`: built-in source and system configuration.
- `backend/src/main/java/com/wheelforge/api/common/`: errors, paging, clock, durable jobs, and rooted local storage.
- `backend/src/test/java/com/wheelforge/api/`: controller, service, repository, and integration tests.

### Task 1: Authentication and Ownership Context

**Files:**
- Modify: `backend/pom.xml`
- Modify: `backend/src/main/resources/application.yml`
- Create: `backend/src/main/java/com/wheelforge/api/security/SecurityConfig.java`
- Create: `backend/src/main/java/com/wheelforge/api/security/AuthController.java`
- Create: `backend/src/main/java/com/wheelforge/api/security/AuthService.java`
- Create: `backend/src/main/java/com/wheelforge/api/security/TokenService.java`
- Create: `backend/src/main/java/com/wheelforge/api/security/UserAccount.java`
- Create: `backend/src/main/java/com/wheelforge/api/security/UserAccountRepository.java`
- Create: `backend/src/main/java/com/wheelforge/api/security/CurrentUser.java`
- Create: `backend/src/main/java/com/wheelforge/api/security/AdminBootstrap.java`
- Create: `backend/src/main/java/com/wheelforge/api/common/ApiExceptionHandler.java`
- Test: `backend/src/test/java/com/wheelforge/api/security/AuthControllerTest.java`
- Test: `backend/src/test/java/com/wheelforge/api/security/AuthServiceTest.java`
- Test: `backend/src/test/java/com/wheelforge/api/security/TokenServiceTest.java`

**Interfaces:**
- Produces: `POST /api/auth/login` with `LoginRequest(username, password)` and `TokenResponse(accessToken, expiresAt)`.
- Produces: `CurrentUser.requireUserId()` and `CurrentUser.isAdmin()`.

- [ ] **Step 1: Write failing login tests**

```java
@Test
void returnsTokenForValidCredentials() throws Exception {
    mvc.perform(post("/api/auth/login")
            .contentType(APPLICATION_JSON)
            .content("{\"username\":\"alice\",\"password\":\"correct horse battery staple\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.accessToken").isString())
        .andExpect(jsonPath("$.expiresAt").isString());
}

@Test
void rejectsInvalidCredentialsWithoutRevealingUsernameExistence() throws Exception {
    mvc.perform(post("/api/auth/login").contentType(APPLICATION_JSON)
            .content("{\"username\":\"alice\",\"password\":\"wrong\"}"))
        .andExpect(status().isUnauthorized())
        .andExpect(jsonPath("$.code").value("AUTHENTICATION_FAILED"));
}
```

- [ ] **Step 2: Run the tests and verify 404 failures**

Run: `./mvnw -q -pl backend -Dtest=AuthControllerTest test`

Expected: FAIL because `/api/auth/login` does not exist.

- [ ] **Step 3: Implement Argon2id authentication and signed access tokens**

Configure stateless security, permit only login and health endpoints anonymously, and map token claims `sub`, `role`, `iat`, and `exp` into `CurrentUser`. Return the same error for unknown users and bad passwords. Use an HMAC SHA-256 JWT signed with `WF_AUTH_TOKEN_SECRET`; require at least 32 UTF-8 bytes and provide no built-in default. Tests supply their own deterministic secret. The application must refuse startup when the secret is absent or too short in a production-configured context.

On startup, `AdminBootstrap` creates the first administrator only when the users table is empty and `WF_BOOTSTRAP_ADMIN_USERNAME` plus `WF_BOOTSTRAP_ADMIN_PASSWORD` are present; hash the password immediately and never log either value. Refuse startup when only one bootstrap variable is present. Implement `ApiExceptionHandler` with the global `code`, `message`, `fieldErrors`, and `traceId` shape.

- [ ] **Step 4: Run security tests**

Run: `./mvnw -q -pl backend -Dtest=AuthControllerTest test`

Expected: all login and unauthorized-resource tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/pom.xml backend/src/main/resources/application.yml backend/src/main/java/com/wheelforge/api/security backend/src/test/java/com/wheelforge/api/security
git commit -m "feat(api): add token authentication"
```

### Task 2: Target Profiles and Requirement Upload

**Files:**
- Create: `backend/src/main/java/com/wheelforge/api/target/TargetProfileController.java`
- Create: `backend/src/main/java/com/wheelforge/api/target/TargetProfileService.java`
- Create: `backend/src/main/java/com/wheelforge/api/requirements/RequirementFileController.java`
- Create: `backend/src/main/java/com/wheelforge/api/requirements/RequirementFileService.java`
- Create: `backend/src/main/java/com/wheelforge/api/common/storage/LocalFileStorage.java`
- Create: `backend/src/main/java/com/wheelforge/api/common/jobs/BuildJobService.java`
- Test: `backend/src/test/java/com/wheelforge/api/requirements/RequirementFileControllerTest.java`

**Interfaces:**
- Produces: `GET /api/target-profiles` returning enabled OS/architecture/CPython combinations.
- Produces: `POST /api/requirement-files` accepting one file up to 512 KiB.
- Produces: `GET /api/requirement-files/{id}` and `GET /api/requirement-files/{id}/items` with user ownership checks.
- Produces: `LocalFileStorage.putAtomically(key, InputStream, size)` and `open(key)` with root-constrained generated keys.
- Produces: `BuildJobService.enqueue(jobType, subjectId, payload)` inside the caller's transaction.

- [ ] **Step 1: Write failing upload boundary tests**

```java
@Test
@WithMockUser(username = "alice", authorities = "USER")
void uploadsRequirementsAndQueuesParsing() throws Exception {
    var file = new MockMultipartFile("file", "requirements.txt", "text/plain", "requests==2.32.4\n".getBytes(UTF_8));
    mvc.perform(multipart("/api/requirement-files").file(file))
        .andExpect(status().isAccepted())
        .andExpect(jsonPath("$.parseStatus").value("PENDING"));
}

@Test
void rejectsFilesLargerThanLimit() throws Exception {
    var file = new MockMultipartFile("file", "requirements.txt", "text/plain", new byte[524_289]);
    mvc.perform(multipart("/api/requirement-files").file(file))
        .andExpect(status().isPayloadTooLarge());
}
```

- [ ] **Step 2: Run tests and verify failure**

Run: `./mvnw -q -pl backend -Dtest=RequirementFileControllerTest test`

Expected: FAIL because the resource is absent.

- [ ] **Step 3: Implement upload, hashing, ownership, and transactional parse enqueue**

Stream the upload once while computing SHA-256, atomically write to generated object key `users/{userId}/requirements/{fileId}/original.txt`, then persist `PENDING` and a `READY` `REQUIREMENT_PARSE` row in one MySQL transaction. If persistence fails, delete the newly written unreferenced file. Resolve normalized paths and reject any key that escapes the configured data root. Reject empty files, names other than `.txt`, NUL bytes, and oversized content.

- [ ] **Step 4: Run controller, local-storage, and job tests**

Run: `./mvnw -q -pl backend -Dtest='RequirementFile*Test,TargetProfile*Test' test`

Expected: upload, limit, root confinement, atomic publication, transactional job creation, target-profile filtering, and cross-user 404 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/src/main/java/com/wheelforge/api/target backend/src/main/java/com/wheelforge/api/requirements backend/src/main/java/com/wheelforge/api/common/storage backend/src/test/java/com/wheelforge/api/requirements
git commit -m "feat(api): add target profiles and requirement uploads"
```

### Task 3: Build Task Lifecycle

**Files:**
- Create: `backend/src/main/java/com/wheelforge/api/build/BuildTaskController.java`
- Create: `backend/src/main/java/com/wheelforge/api/build/BuildTaskService.java`
- Create: `backend/src/main/java/com/wheelforge/api/build/BuildStateMachine.java`
- Create: `backend/src/main/java/com/wheelforge/api/build/BuildTaskRepository.java`
- Test: `backend/src/test/java/com/wheelforge/api/build/BuildTaskServiceTest.java`

**Interfaces:**
- Produces: create/list/detail/cancel/retry/soft-delete endpoints from the approved design.
- Produces: `BuildStateMachine.requireTransition(BuildStatus from, BuildStatus to)`.

- [ ] **Step 1: Write failing transition and ownership tests**

```java
@ParameterizedTest
@CsvSource({"CREATED,QUEUED", "QUEUED,RESOLVING", "RESOLVING,DOWNLOADING", "PACKAGING,SUCCESS"})
void acceptsDeclaredTransitions(BuildStatus from, BuildStatus to) {
    assertThatCode(() -> machine.requireTransition(from, to)).doesNotThrowAnyException();
}

@Test
void rejectsTerminalTransition() {
    assertThatThrownBy(() -> machine.requireTransition(BuildStatus.SUCCESS, BuildStatus.QUEUED))
        .isInstanceOf(InvalidBuildTransition.class);
}
```

- [ ] **Step 2: Run and verify failure**

Run: `./mvnw -q -pl backend -Dtest=BuildTaskServiceTest test`

Expected: FAIL because the state machine is missing.

- [ ] **Step 3: Implement immutable target snapshots and commands**

Allow creation only for a parsed, user-owned RequirementFile and enabled TargetProfile. Snapshot all target fields and persist the BuildTask plus one `READY` `BUILD` job payload in the same transaction, leaving the task in `QUEUED`. Cancellation sets `cancel_requested`; queued tasks and their unclaimed jobs transition immediately to `CANCELLED`. Retry creates a new task UUID, `source_task_id`, and job row in one transaction. Delete sets `deleted_at`.

- [ ] **Step 4: Run lifecycle tests**

Run: `./mvnw -q -pl backend -Dtest='BuildTask*Test' test`

Expected: valid transitions, cancellation, retry linkage, immutable target, and ownership tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/src/main/java/com/wheelforge/api/build backend/src/test/java/com/wheelforge/api/build
git commit -m "feat(api): add build task lifecycle"
```

### Task 4: Logs, Resolved Packages, and Version Comparison

**Files:**
- Create: `backend/src/main/java/com/wheelforge/api/build/BuildResultController.java`
- Create: `backend/src/main/java/com/wheelforge/api/build/VersionComparisonRow.java`
- Test: `backend/src/test/java/com/wheelforge/api/build/BuildResultControllerTest.java`

**Interfaces:**
- Produces: `GET /api/build-tasks/{id}/logs?afterSequence=N`.
- Produces: resolved-package and version-comparison endpoints plus the fixed static-validation result fields.

- [ ] **Step 1: Write a failing comparison test**

```java
@Test
void showsUpgradeDowngradeAndMissingRows() throws Exception {
    mvc.perform(get("/api/build-tasks/{id}/version-comparison", taskId).with(user("alice")))
        .andExpect(status().isOk())
        .andExpect(header().string("X-WheelForge-Validation", "STATIC"))
        .andExpect(jsonPath("$[0].changeDirection").value("UNCHANGED"))
        .andExpect(jsonPath("$[1].changeDirection").value("DOWNGRADE"))
        .andExpect(jsonPath("$[2].wheelStatus").value("MISSING"));
}
```

- [ ] **Step 2: Run and verify the endpoint is absent**

Run: `./mvnw -q -pl backend -Dtest=BuildResultControllerTest test`

Expected: FAIL with 404.

- [ ] **Step 3: Implement cursor logs and structured comparison queries**

Build rows from `requirement_items` plus `resolved_packages`, never by parsing log text. Return fields `packageName`, `dependencyType`, `originalConstraint`, `strictVersion`, `finalVersion`, `changeDirection`, `changeReason`, `wheelStatus`, and `packageSource`. Build detail and Artifact responses include `validationLevel: STATIC`, `installVerified: false`, and the user-facing message `Static compatibility checks passed; target installation was not verified.`

- [ ] **Step 4: Run result tests**

Run: `./mvnw -q -pl backend -Dtest=BuildResultControllerTest test`

Expected: comparison ordering, missing rows, cursor logs, and cross-user 404 tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/src/main/java/com/wheelforge/api/build/BuildResultController.java backend/src/main/java/com/wheelforge/api/build/VersionComparisonRow.java backend/src/test/java/com/wheelforge/api/build/BuildResultControllerTest.java
git commit -m "feat(api): expose build logs and version comparison"
```

### Task 5: Artifact Download and Administration

**Files:**
- Create: `backend/src/main/java/com/wheelforge/api/artifact/ArtifactController.java`
- Create: `backend/src/main/java/com/wheelforge/api/artifact/ArtifactService.java`
- Create: `backend/src/main/java/com/wheelforge/api/admin/AdminController.java`
- Create: `backend/src/main/java/com/wheelforge/api/artifact/ArtifactRetentionJob.java`
- Test: `backend/src/test/java/com/wheelforge/api/artifact/ArtifactControllerTest.java`

**Interfaces:**
- Produces: artifact list/detail/download, administrator user management, built-in source configuration, system configuration, and retention cleanup.

- [ ] **Step 1: Write failing authorization and expiry tests**

```java
@Test
void ownerCanDownloadAndOtherUserCannot() throws Exception {
    mvc.perform(get("/api/artifacts/{id}/download", artifactId).with(user("alice")))
        .andExpect(status().isOk());
    mvc.perform(get("/api/artifacts/{id}/download", artifactId).with(user("bob")))
        .andExpect(status().isNotFound());
}

@Test
void expiredArtifactReturnsGone() throws Exception {
    mvc.perform(get("/api/artifacts/{id}/download", expiredId).with(user("alice")))
        .andExpect(status().isGone());
}
```

- [ ] **Step 2: Run and verify failure**

Run: `./mvnw -q -pl backend -Dtest=ArtifactControllerTest test`

Expected: FAIL because download is not implemented.

- [ ] **Step 3: Implement authorized streaming and admin-only configuration**

After ownership checks, stream the Artifact from `LocalFileStorage` through the API with a fixed attachment filename and content length; never return an absolute path or redirect URL. Insert `download_records` before returning content and increment counts transactionally. Permit administrators to enable, disable, and reorder only the three built-in sources; reject URL changes. Retention claims expired rows, deletes the rooted local object idempotently, then records cleanup completion.

Add administrator-only `POST /api/admin/users`, `PUT /api/admin/users/{id}/status`, and `GET /api/admin/users`. Require unique usernames, hash generated or submitted initial passwords with Argon2id, forbid an administrator from disabling their own account, and never return password hashes.

- [ ] **Step 4: Run API verification**

Run: `./mvnw -q -pl backend test`

Expected: all API tests pass, including admin 403, source URL immutability, download audit, and retention idempotency.

- [ ] **Step 5: Commit**

```bash
git add backend/src/main/java/com/wheelforge/api/artifact backend/src/main/java/com/wheelforge/api/admin backend/src/test/java/com/wheelforge/api/artifact
git commit -m "feat(api): add artifact downloads and administration"
```
