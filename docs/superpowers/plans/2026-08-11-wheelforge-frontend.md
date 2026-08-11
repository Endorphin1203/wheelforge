# WheelForge Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a responsive Vue 3 operations console for creating cross-platform Wheel builds, inspecting results, downloading artifacts, and administering WheelForge.

**Architecture:** A hash-routed Vue 3 SPA uses a typed native-fetch client and Pinia session state to consume the existing Spring Boot API. Vite serves development traffic through an API proxy; production assets are built into `frontend/dist` and served read-only by Spring Boot from `WF_FRONTEND_ROOT`.

**Tech Stack:** Vue 3, TypeScript, Vite, Vue Router, Pinia, Element Plus, Vitest, Vue Testing Library, Playwright, Spring Boot Security, MySQL-backed existing API

## Global Constraints

- V1 exposes only `solveMode: "COMPATIBLE"`; the UI must not present a strict-mode switch.
- Every target selection must resolve to an enabled server-provided target profile covering OS, CPU architecture, and Python version.
- The upload accepts one `.txt` file no larger than 512 KiB and renders requirement content as text only.
- Authentication uses `sessionStorage`; tokens never appear in URLs, logs, `localStorage`, or filenames.
- Linux and Windows results display exactly: `静态兼容性校验，不执行目标环境安装，不能证明目标机器一定安装成功。`
- Active tasks poll every two seconds with one request in flight; polling stops on navigation and terminal status.
- Artifact downloads use authenticated `fetch`, validate a ZIP response, and revoke the temporary object URL.
- Admin source URLs are immutable; config and source writes include optimistic `version` values where supplied by the API.
- The navigation rail is 216 px on desktop and a drawer below 768 px; cards use at most 8 px radius.
- Production uses Spring Boot static serving with no Docker, Nginx, Redis, or MinIO process.

---

### Task 1: Frontend Foundation And Spring Static Hosting

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/index.html`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.app.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/vitest.setup.ts`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/styles/base.css`
- Create: `frontend/src/App.test.ts`
- Create: `backend/src/test/java/com/wheelforge/api/security/StaticAssetSecurityTest.java`
- Modify: `backend/src/main/java/com/wheelforge/api/security/SecurityConfig.java`
- Modify: `backend/src/main/resources/application.yml`
- Modify: `Makefile`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: Spring Security's existing `SecurityFilterChain` and Vite proxy targets `/api` and `/actuator` at `http://localhost:8080`.
- Produces: `npm run dev`, `npm run typecheck`, `npm run test`, and `npm run build`; a Vue mount point `#app`; public GET access to `/`, `/index.html`, `/assets/**`, `/favicon.svg`, and `/manifest.webmanifest` while `/api/**` remains authenticated.

- [ ] **Step 1: Add failing static-security and application-shell tests**

```java
@Test
void staticEntryPointIsPublicButApiRemainsProtected() throws Exception {
    mvc.perform(get("/index.html")).andExpect(status().isOk());
    mvc.perform(get("/api/build-tasks")).andExpect(status().isUnauthorized());
}
```

```ts
it('renders the WheelForge application marker', () => {
  render(App)
  expect(screen.getByText('WheelForge')).toBeInTheDocument()
})
```

- [ ] **Step 2: Run the focused tests and confirm the missing frontend/static policy fails**

Run: `./mvnw -pl backend -Dtest=StaticAssetSecurityTest test`
Expected: FAIL because `/index.html` is not yet a public static resource.

Run: `npm test -- --run src/App.test.ts`
Expected: FAIL before the frontend package and test script exist.

- [ ] **Step 3: Scaffold Vite and configure Spring external static resources**

Use these scripts in `frontend/package.json`:

```json
{
  "scripts": {
    "dev": "vite --host 0.0.0.0",
    "typecheck": "vue-tsc --noEmit",
    "test": "vitest",
    "test:run": "vitest run",
    "build": "npm run typecheck && vite build"
  }
}
```

Configure Vite aliases and proxies, mount `App`, import Element Plus and the global stylesheet, and add this Spring resource location:

```yaml
spring:
  web:
    resources:
      static-locations:
        - classpath:/static/
        - file:${WF_FRONTEND_ROOT:/opt/wheelforge/frontend/dist/}
```

Permit only static entry assets before `.anyRequest().authenticated()`:

```java
.requestMatchers(HttpMethod.GET, "/", "/index.html", "/favicon.svg", "/manifest.webmanifest", "/assets/**").permitAll()
```

- [ ] **Step 4: Install locked dependencies and run foundation verification**

Run: `npm install`
Expected: `package-lock.json` is generated without audit errors blocking installation.

Run: `npm run typecheck && npm run test:run && npm run build`
Expected: all commands exit 0 and `frontend/dist/index.html` contains `WheelForge`.

Run: `./mvnw -pl backend -Dtest=StaticAssetSecurityTest test`
Expected: PASS.

- [ ] **Step 5: Commit the foundation**

```bash
git add frontend backend/src/main/java/com/wheelforge/api/security/SecurityConfig.java backend/src/main/resources/application.yml backend/src/test/java/com/wheelforge/api/security/StaticAssetSecurityTest.java Makefile .gitignore
git commit -m "feat: add WheelForge frontend foundation"
```

### Task 2: Typed API, Authentication, And Responsive Application Shell

**Files:**
- Create: `frontend/src/api/contracts.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/client.test.ts`
- Create: `frontend/src/stores/auth.ts`
- Create: `frontend/src/stores/auth.test.ts`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/layouts/AppShell.vue`
- Create: `frontend/src/components/StatusBadge.vue`
- Create: `frontend/src/components/StaticValidationNotice.vue`
- Create: `frontend/src/views/LoginView.vue`
- Create: `frontend/src/views/NotFoundView.vue`
- Create: `frontend/src/views/ForbiddenView.vue`
- Create: `frontend/src/views/LoginView.test.ts`
- Modify: `frontend/src/main.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles/base.css`

**Interfaces:**
- Consumes: `POST /api/auth/login` with `{ username, password }`, returning `{ accessToken, expiresAt }`; JWT claims `{ sub, role, exp }`.
- Produces: `ApiClient.request<T>(path, init)`, `ApiClient.download(path)`, `ApiError { status, code, message, traceId }`, `useAuthStore()` actions `login`, `logout`, `restore`, and route metadata `{ requiresAuth, adminOnly }`.

- [ ] **Step 1: Write failing API/auth/component tests**

```ts
it('clears an expired session during restore', () => {
  sessionStorage.setItem('wf.session', expiredTokenFixture)
  const auth = useAuthStore()
  auth.restore()
  expect(auth.isAuthenticated).toBe(false)
})

it('turns a JSON API failure into ApiError', async () => {
  server.use(http.get('/api/build-tasks', () => HttpResponse.json(
    { code: 'TASK_NOT_FOUND', message: '任务不存在', traceId: 'trace-1' },
    { status: 404 },
  )))
  await expect(api.request('/api/build-tasks')).rejects.toMatchObject({ status: 404, code: 'TASK_NOT_FOUND' })
})
```

- [ ] **Step 2: Run focused tests and verify failures**

Run: `npm run test:run -- src/api/client.test.ts src/stores/auth.test.ts src/views/LoginView.test.ts`
Expected: FAIL because the client, store, and login view do not exist.

- [ ] **Step 3: Implement typed contracts, fetch behavior, and auth lifecycle**

Define API contracts using exact camel-case response fields from the Spring controllers. Implement request headers, JSON parsing, multipart passthrough, 401 callback, and Blob download metadata. Decode only JWT payload metadata; never use decoded claims as API authorization.

```ts
export interface DownloadResult {
  blob: Blob
  filename: string
  contentType: string
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly traceId?: string,
  ) { super(message) }
}
```

- [ ] **Step 4: Implement login, route guards, and adaptive shell**

Create a 216 px desktop rail with Build, Tasks, and Artifacts navigation; append Users, Sources, and Config only for `ADMIN`. Below 768 px, expose the same links in an Element Plus drawer opened by an icon button with an accessible label and tooltip. Preserve the intended route on 401 and redirect successful login there.

- [ ] **Step 5: Verify auth and shell behavior**

Run: `npm run typecheck && npm run test:run -- src/api/client.test.ts src/stores/auth.test.ts src/views/LoginView.test.ts`
Expected: PASS, including expired-token cleanup, 401 callback, invalid credentials, password visibility control, and admin navigation visibility.

- [ ] **Step 6: Commit auth and shell**

```bash
git add frontend/src
git commit -m "feat: add frontend authentication and app shell"
```

### Task 3: Requirement Upload And Target Build Workspace

**Files:**
- Create: `frontend/src/api/requirements.ts`
- Create: `frontend/src/api/targets.ts`
- Create: `frontend/src/api/tasks.ts`
- Create: `frontend/src/composables/usePolling.ts`
- Create: `frontend/src/composables/usePolling.test.ts`
- Create: `frontend/src/features/build/targetProfiles.ts`
- Create: `frontend/src/features/build/targetProfiles.test.ts`
- Create: `frontend/src/stores/buildDraft.ts`
- Create: `frontend/src/views/BuildView.vue`
- Create: `frontend/src/views/BuildView.test.ts`
- Modify: `frontend/src/router/index.ts`

**Interfaces:**
- Consumes: requirement upload/detail/items endpoints, `GET /api/target-profiles`, and `POST /api/build-tasks` with `{ requirementFileId, targetProfileId, solveMode: "COMPATIBLE" }`.
- Produces: `uploadRequirement(file): Promise<RequirementFileView>`, `listRequirementItems(id)`, `listTargetProfiles()`, `createBuildTask(input)`, `groupTargetProfiles(profiles)`, and `usePolling(load, options)` with explicit `start()` and `stop()`.

- [ ] **Step 1: Write failing target grouping, polling, and workflow tests**

```ts
it('offers only target combinations present in profiles', () => {
  const grouped = groupTargetProfiles([linuxArmPy311, windowsX64Py312])
  expect(grouped.architecturesFor('LINUX')).toEqual(['ARM64'])
  expect(grouped.pythonVersionsFor('LINUX', 'ARM64')).toEqual(['3.11'])
})

it('stops polling after a terminal response', async () => {
  const load = vi.fn().mockResolvedValueOnce({ status: 'PARSING' }).mockResolvedValueOnce({ status: 'PARSED' })
  const polling = usePolling(load, { intervalMs: 10, isTerminal: value => value.status !== 'PARSING' })
  polling.start()
  await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2))
  await new Promise(resolve => setTimeout(resolve, 30))
  expect(load).toHaveBeenCalledTimes(2)
})
```

- [ ] **Step 2: Run focused tests and verify failures**

Run: `npm run test:run -- src/features/build/targetProfiles.test.ts src/composables/usePolling.test.ts src/views/BuildView.test.ts`
Expected: FAIL because target grouping, polling, and build workspace are absent.

- [ ] **Step 3: Implement upload validation and parse result flow**

Reject non-`.txt`, empty, or larger-than-524288-byte files before upload. Upload immediately, poll nonterminal parse status without overlap, render each `RequirementItemView` in a compact table, and retain parse error code/message in an action row.

- [ ] **Step 4: Implement valid target selectors and compatible build creation**

Render OS and architecture as segmented controls and Python as a select. Derive each option from the profiles returned by the API, store the selected profile ID, show compatibility upgrade/downgrade disclosure and the exact static-validation notice, and route a successful create response to `/tasks/{id}`.

- [ ] **Step 5: Verify workspace behavior**

Run: `npm run typecheck && npm run test:run -- src/features/build/targetProfiles.test.ts src/composables/usePolling.test.ts src/views/BuildView.test.ts`
Expected: PASS for size/type validation, parse success/failure, impossible target prevention, compatible payload, duplicate click prevention, and poll cleanup.

- [ ] **Step 6: Commit the build workspace**

```bash
git add frontend/src/api frontend/src/composables frontend/src/features frontend/src/stores/buildDraft.ts frontend/src/views/BuildView.vue frontend/src/views/BuildView.test.ts frontend/src/router/index.ts
git commit -m "feat: add requirement build workspace"
```

### Task 4: Task History, Build Evidence, Version Comparison, And Artifacts

**Files:**
- Create: `frontend/src/api/artifacts.ts`
- Create: `frontend/src/features/tasks/taskPresentation.ts`
- Create: `frontend/src/features/tasks/taskPresentation.test.ts`
- Create: `frontend/src/features/artifacts/downloadArtifact.ts`
- Create: `frontend/src/features/artifacts/downloadArtifact.test.ts`
- Create: `frontend/src/views/TasksView.vue`
- Create: `frontend/src/views/TaskDetailView.vue`
- Create: `frontend/src/views/ArtifactsView.vue`
- Create: `frontend/src/views/TaskDetailView.test.ts`
- Create: `frontend/src/views/ArtifactsView.test.ts`
- Modify: `frontend/src/router/index.ts`

**Interfaces:**
- Consumes: task list/detail/cancel/retry/delete, logs, resolved packages, version comparison, artifact list/detail/download endpoints.
- Produces: `isTerminalStatus(status)`, `presentVersionDirection(row)`, `downloadArtifact(id, client, browser)`, and authenticated user routes `/tasks`, `/tasks/:id`, `/artifacts`.

- [ ] **Step 1: Write failing presentation, terminal-state, and download tests**

```ts
it.each([
  ['UPGRADE', '升级'],
  ['DOWNGRADE', '降级'],
  ['UNCHANGED', '未变化'],
])('presents %s with text as well as an icon', (direction, label) => {
  expect(presentVersionDirection({ changeDirection: direction } as VersionComparisonRow).label).toBe(label)
})

it('revokes the object URL after starting a ZIP download', async () => {
  await downloadArtifact(7, clientFixture, browserFixture)
  expect(browserFixture.revokeObjectURL).toHaveBeenCalledWith('blob:test')
})
```

- [ ] **Step 2: Run focused tests and verify failures**

Run: `npm run test:run -- src/features/tasks/taskPresentation.test.ts src/features/artifacts/downloadArtifact.test.ts src/views/TaskDetailView.test.ts src/views/ArtifactsView.test.ts`
Expected: FAIL because the result and download modules do not exist.

- [ ] **Step 3: Implement task history and task-detail polling**

Build a compact searchable/filterable history table. The detail view loads task, dependency, comparison, log, and artifact data into Overview, Dependencies, Version Comparison, Logs, and Artifact tabs. Poll task and incremental logs every two seconds only for nonterminal statuses. Wire cancel for active tasks, retry for failed/cancelled tasks, and delete behind a confirmation dialog.

- [ ] **Step 4: Implement visual comparison and static evidence**

Show original constraint, strict version, final version, direction icon plus Chinese label, reason, Wheel status, and source. Render success, partial success, failure, and cancellation with distinct icon/text states. Display `validationLevel`, `installVerified`, `validationMessage`, and the exact static-validation warning without claiming installation success.

- [ ] **Step 5: Implement artifact history and secure ZIP download**

Render expiry, byte size, SHA-256, build status, validation label, and download count. Require `application/zip` or `application/octet-stream`, parse a safe filename from `Content-Disposition`, fall back to `wheelforge-artifact-{id}.zip`, click a temporary anchor, and always revoke the object URL.

- [ ] **Step 6: Verify task and artifact behavior**

Run: `npm run typecheck && npm run test:run -- src/features/tasks/taskPresentation.test.ts src/features/artifacts/downloadArtifact.test.ts src/views/TaskDetailView.test.ts src/views/ArtifactsView.test.ts`
Expected: PASS for all terminal states, partial results, version directions, incremental log polling, action visibility, ZIP validation, filename fallback, and URL cleanup.

- [ ] **Step 7: Commit task and artifact views**

```bash
git add frontend/src
git commit -m "feat: add build results and artifact downloads"
```

### Task 5: Administrator Console

**Files:**
- Create: `frontend/src/api/admin.ts`
- Create: `frontend/src/features/admin/configFields.ts`
- Create: `frontend/src/features/admin/configFields.test.ts`
- Create: `frontend/src/views/admin/UsersView.vue`
- Create: `frontend/src/views/admin/SourcesView.vue`
- Create: `frontend/src/views/admin/ConfigView.vue`
- Create: `frontend/src/views/admin/AdminViews.test.ts`
- Modify: `frontend/src/router/index.ts`

**Interfaces:**
- Consumes: admin users, package sources, and system config endpoints and the API client's typed 403/409 errors.
- Produces: admin-only routes `/admin/users`, `/admin/sources`, `/admin/config`; typed methods `listUsers`, `createUser`, `setUserStatus`, `listSources`, `updateSource`, `listConfig`, and `updateConfig`.

- [ ] **Step 1: Write failing admin behavior tests**

```ts
it('shows a generated password only in the creation result dialog', async () => {
  render(UsersView)
  await user.click(screen.getByRole('button', { name: '创建用户' }))
  await user.type(screen.getByLabelText('用户名'), 'builder')
  await user.click(screen.getByRole('button', { name: '确认创建' }))
  expect(await screen.findByText('Temp-Only-123')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '关闭' }))
  expect(screen.queryByText('Temp-Only-123')).not.toBeInTheDocument()
})

it('refreshes source data after an optimistic conflict', async () => {
  server.use(http.put('/api/admin/package-sources/1', () => HttpResponse.json(
    { code: 'OPTIMISTIC_LOCK_CONFLICT', message: '配置已更新' },
    { status: 409 },
  )))
  render(SourcesView)
  await user.click(await screen.findByRole('switch', { name: '启用清华镜像' }))
  expect(await screen.findByText('配置已被其他管理员更新，已刷新')).toBeInTheDocument()
})
```

- [ ] **Step 2: Run admin tests and verify failures**

Run: `npm run test:run -- src/features/admin/configFields.test.ts src/views/admin/AdminViews.test.ts`
Expected: FAIL because admin modules do not exist.

- [ ] **Step 3: Implement users and one-time password handling**

Create users with role and optional initial password, show a server-generated password only in the immediate modal state, and activate/disable accounts with row-scoped loading. Never persist or log the returned password.

- [ ] **Step 4: Implement source and typed config editors**

Keep source `code`, `displayName`, and `baseUrl` read-only. Edit enabled, priority, and timeout with numeric bounds and submit the current version. Derive config controls from known keys using select, switch, or bounded number input; send `{ value, version }`. On 409, refetch and tell the user the server value replaced the stale edit.

- [ ] **Step 5: Verify admin authorization and updates**

Run: `npm run typecheck && npm run test:run -- src/features/admin/configFields.test.ts src/views/admin/AdminViews.test.ts`
Expected: PASS for role navigation, route denial, one-time password disposal, immutable URLs, row loading, typed config controls, successful writes, and conflict refresh.

- [ ] **Step 6: Commit administrator features**

```bash
git add frontend/src
git commit -m "feat: add WheelForge administrator console"
```

### Task 6: Browser Acceptance, Deployment Integration, And Release Verification

**Files:**
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/fixtures/mockApi.ts`
- Create: `frontend/e2e/wheelforge.spec.ts`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `deploy/.env.example`
- Modify: `deploy/smoke.sh`
- Modify: `integration-tests/test_deployment_files.py`
- Modify: `docs/operations.md`
- Modify: `docs/acceptance.md`
- Modify: `README.md`
- Modify: `Makefile`

**Interfaces:**
- Consumes: the completed SPA, Vite preview server, deterministic Playwright route mocks, and existing single-machine deployment scripts.
- Produces: `npm run test:e2e`, root `make frontend-verify`, root `make verify` including frontend checks, documented frontend installation under `/opt/wheelforge/frontend/dist`, and a smoke assertion for the WheelForge HTML marker.

- [ ] **Step 1: Add failing deployment-policy assertions**

```python
def test_frontend_root_and_smoke_marker_are_deployed():
    env = (ROOT / "deploy/.env.example").read_text()
    smoke = (ROOT / "deploy/smoke.sh").read_text()
    assert "WF_FRONTEND_ROOT=/opt/wheelforge/frontend/dist" in env
    assert "WheelForge" in smoke
```

- [ ] **Step 2: Run deployment test and confirm failure**

Run: `python -m pytest integration-tests/test_deployment_files.py -q`
Expected: FAIL because the frontend root and smoke marker are not yet deployed.

- [ ] **Step 3: Build deterministic browser fixtures and end-to-end flows**

Mock login, requirement parsing, target profiles, task progression, logs, version comparison, artifacts, users, sources, and config through Playwright `page.route`. Cover desktop 1440x900 and mobile 390x844. Assert login, build creation, terminal partial-success evidence, upgrade/downgrade table, ZIP action, admin navigation, mobile drawer, no horizontal document overflow, and no page-level error.

- [ ] **Step 4: Update deployment, smoke, and operator documentation**

Add `WF_FRONTEND_ROOT=/opt/wheelforge/frontend/dist`, document `npm ci && npm run build` followed by root-owned read-only installation, and extend `deploy/smoke.sh` to fetch `/` and require the stable `WheelForge` marker. Document that validation is static and list the full frontend verification commands.

- [ ] **Step 5: Run browser and responsive visual verification**

Run: `npm run test:e2e`
Expected: desktop and mobile scenarios pass, screenshots are nonblank, and `document.documentElement.scrollWidth <= document.documentElement.clientWidth`.

Inspect the generated login, build, task-detail, artifacts, admin, and mobile screenshots for overlap, clipped text, accidental nested cards, unstable controls, and unreadable status contrast. Correct styles and rerun until all screenshots pass inspection.

- [ ] **Step 6: Run complete project verification**

Run: `npm run typecheck && npm run test:run && npm run build && npm run test:e2e`
Expected: all frontend checks exit 0.

Run: `make verify`
Expected: backend, worker, integration, deployment-policy, and frontend checks all exit 0; MySQL-gated tests remain explicitly gated when credentials are absent.

- [ ] **Step 7: Commit acceptance and deployment integration**

```bash
git add frontend deploy integration-tests/test_deployment_files.py docs/operations.md docs/acceptance.md README.md Makefile
git commit -m "test: complete frontend deployment acceptance"
```

- [ ] **Step 8: Start the development server for user inspection**

Run: `npm run dev -- --port 5173`
Expected: Vite reports a local URL, keeps running, and `/` renders the login screen while `/api` proxies to port 8080.
