# WheelForge Frontend Design

## Product Scope

The frontend is a Vue 3 single-page operational console for the existing
WheelForge API. It serves two roles in one application:

- Users upload one `requirements.txt`, select one enabled OS/CPU/Python target,
  create a compatibility build, inspect progress and results, and download the
  offline ZIP.
- Administrators additionally manage users, the three built-in package sources,
  and bounded system configuration.

The first authenticated screen is the build workspace, not a dashboard or
marketing landing page. V1 exposes only the backend's `COMPATIBLE` solve mode.

## Architecture

- `frontend/` uses Vue 3, TypeScript, Vite, Vue Router, Pinia, Element Plus,
  Vitest, and Testing Library.
- A typed `fetch` client owns JSON validation, bearer authorization, API errors,
  multipart upload, Blob downloads, and 401 logout behavior.
- Authentication is stored in `sessionStorage`. The client reads the signed
  token payload only to choose navigation; the API remains the authorization
  authority.
- Hash history keeps all application routes under `/` without a server-side SPA
  fallback.
- Development uses the Vite server with `/api` and `/actuator` proxied to port
  8080.
- Production builds to `frontend/dist`. Spring Boot reads that external,
  root-owned directory through `WF_FRONTEND_ROOT`; no Nginx, Docker, Redis, or
  MinIO process is introduced.

## Information Architecture

Public route:

- `/login`: username/password login with explicit invalid-credential and expired-
  session states.

Authenticated user routes:

- `/build/new`: upload, parse result, target selection, compatibility-mode
  disclosure, and build creation in one continuous workspace.
- `/tasks`: dense task history table with status filter and client-side search.
- `/tasks/:id`: task header and actions plus Overview, Dependencies, Version
  comparison, Logs, and Artifact tabs. Active tasks poll; terminal tasks stop.
- `/artifacts`: downloadable Artifact history with expiry, size, checksum,
  validation label, and download count.

Administrator routes:

- `/admin/users`: create users, reveal a generated initial password once, and
  enable/disable accounts.
- `/admin/sources`: enable, prioritize, and set timeout for the immutable
  Tsinghua, Aliyun, and PyPI sources.
- `/admin/config`: edit typed bounded configuration values with optimistic
  versions and conflict refresh.

Unknown or unauthorized routes resolve to a compact not-found/forbidden state.

## Build Workflow

1. The user drops or selects one `.txt` file up to 512 KiB.
2. The frontend uploads immediately and polls the file until `PARSED` or
   `FAILED`.
3. Parsed direct requirements appear in a compact table. Parse failures remain
   on the page with an actionable message.
4. Enabled target profiles are fetched from the API and grouped into OS,
   architecture, and Python controls. Only combinations represented by a real
   profile can be selected.
5. The page states that compatibility solving may upgrade or downgrade pinned
   versions when the original target Wheel is unavailable.
6. Build creation routes to the task detail page. The page polls every two
   seconds while non-terminal, with a single in-flight request and cleanup on
   navigation.
7. Terminal results show status, dependency and version tables, static validation
   evidence, and the Artifact download action when one exists.

All Linux and Windows targets display: "静态兼容性校验，不执行目标环境安装，不能证明目标机器一定安装成功。"

## Visual System

The interface is a restrained build console rather than a card-based SaaS
landing page:

- A 216 px graphite navigation rail, white work surface, cool-gray dividers,
  cyan selection, green success, amber partial/caution, and red failure.
- Compact typography using bundled system sans and monospace stacks; no remote
  fonts or image dependencies are required in an offline deployment.
- Page sections are unframed bands. Cards are reserved for upload drop zones,
  repeated artifacts, and dialogs, with radius no greater than 8 px.
- Status uses both icon/text and color. Version direction uses arrows plus
  `升级`, `降级`, or `未变化`, never color alone.
- Desktop prioritizes tables and scanning. Below 768 px, navigation becomes a
  drawer, table-critical columns remain visible, and secondary columns move into
  row details.
- Motion is limited to progress transitions, polling indicators, drawers, and
  dialogs. Reduced-motion preferences are honored.

## State And Errors

- Pinia stores authentication and the short-lived current upload/build draft.
  Server data is refreshed from the API rather than treated as client truth.
- API error codes map to concise Chinese actions. Unknown errors display a trace
  ID when supplied, without rendering raw server exception text.
- A 401 clears the session and returns to login with the intended route saved.
- A 403 shows a role boundary; a 409 conflict refreshes current server state;
  network failures preserve page data and expose a retry command.
- Buttons have stable dimensions and disable only while their own command is in
  flight. Polling never overlaps requests.

## Security

- The token is never placed in URLs, logs, localStorage, or download filenames.
- Artifact downloads use authenticated `fetch`, validate the response type, and
  create a short-lived object URL with a server-provided safe filename fallback.
- Requirement content is rendered as text only. No HTML from API values is used.
- Admin navigation is a convenience boundary only; every admin API remains
  server-authorized.
- Package source URLs are read-only in the UI.

## Testing And Acceptance

- Vitest covers auth expiry, API error handling, target grouping, polling stop,
  version direction, download cleanup, and admin optimistic updates.
- Vue component tests cover login, build workflow, task terminal/partial/failure
  states, and responsive navigation semantics.
- Backend tests verify public static assets while `/api/**` remains protected.
- Playwright verifies desktop and mobile login, build workspace, task detail,
  version comparison, artifacts, and admin screens against a deterministic mock
  API. Screenshots are checked for blank output, overflow, overlap, and unstable
  layout.
- `npm run typecheck`, `npm run test`, `npm run build`, `make verify`, and the
  browser acceptance suite are required before completion.

## Deployment Changes

- Add `WF_FRONTEND_ROOT=/opt/wheelforge/frontend/dist` to the deployment
  environment.
- Install the Vite build as root-owned, read-only files under `/opt/wheelforge`.
- Permit unauthenticated GET access only to `/`, `/index.html`, favicon/manifest,
  and fingerprinted `/assets/**`; all `/api/**` endpoints retain current security.
- Extend the smoke script to fetch `/` and verify the WheelForge HTML marker.
