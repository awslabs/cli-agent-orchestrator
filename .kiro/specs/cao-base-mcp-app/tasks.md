# Implementation Plan: CAO Base MCP App

## Overview

This plan converts the approved design into incremental, code-only coding tasks for a
greenfield-additive build on **cli-agent-orchestrator v2.2.0**. The fork already provides the
load-bearing primitives (FastAPI HTTP-only boundary @ `127.0.0.1:9889`, in-process
`event_bus.py`, the `cao.plugins` entry-point plugin system, SQLite, providers); every layer of
the MCP App — history buffer, normalization, fan-out, snapshots, tools, endpoints, resources,
frontend, auth, and gates — is net-new and built here.

The sequence honors the design's build-order and shift-left philosophy:

- **Phase 0** front-loads the shift-left gates so every downstream change inherits them.
- **Phase I** builds the load-bearing services tier (the tools/views have nothing to read until
  the ring buffer is populated).
- **Phase II** builds the snapshot/tool/endpoint layer and the JIT-free single-file view layer.
- **Phase III** wires the AI-native model-context loop and completes the single mutation choke point.
- **Phase IV** is research-gated: a go/no-go verification task precedes the default-off auth layer.
- **Phase V** closes the remaining cells of the 16-point test matrix and raises the coverage floor.

Backend code is Python under `src/cli_agent_orchestrator/`; the frontend is React + Vite +
TypeScript under a new `cao_mcp_apps/` tree. Language is fixed by the design (no pseudocode), so
no implementation-language selection is required.

## Tasks

- [x] 1. Phase 0 — Shift-left quality gates (low-risk; everything downstream inherits them)
  - [x] 1.1 Add the HTTP-only MCP boundary guard test
    - Create a pytest (e.g. `test/test_http_only_boundary.py`) that AST/import-scans every module
      under `src/cli_agent_orchestrator/mcp_server/` and fails if any imports `clients.tmux` or
      `clients.database`; locks the invariant the fork already satisfies.
    - _Requirements: 7.1, 7.2, 7.3_
    - _Validates: Property 9 (HTTP-only boundary)_

  - [x] 1.2 Add the JIT-free deny-list scan script
    - Create `cao_mcp_apps/scripts/scan-jit.mjs` scanning built artifacts under `apps_static/` for
      the tokens `eval(`, `new Function(`, `Function("`, and WASM-JIT calls; exit non-zero on any hit.
    - Wire an `npm run scan:jit` script entry.
    - _Requirements: 8.2, 8.5, 20.2_
    - _Validates: Property 10 (JIT-free bundle)_

  - [x] 1.3 Establish the coverage ratchet baseline
    - Create a repo-root `.coverage-baseline.json` and a ratchet script that reads `pytest --cov`
      (already `--cov=src` in `addopts`) and `vitest --coverage` and fails on regression below the floor.
    - _Requirements: 20.1_

  - [x] 1.4 Add the bundle-size budget script
    - Create `cao_mcp_apps/scripts/check-bundle-size.mjs` enforcing gzip budgets
      (dashboard <= 250 KB gz, agent <= 250 KB gz, event-stream <= 150 KB gz); exit non-zero on overflow.
    - _Requirements: 20.4_

  - [x] 1.5 Scaffold local git hooks (husky + lint-staged)
    - Bootstrap a minimal `cao_mcp_apps/package.json` with `husky` + `lint-staged`: pre-commit runs
      fast lint + type checks on staged changes (< 15 s target); pre-push runs the full local sweep.
    - _Requirements: 21.1, 21.2, 21.3_

  - [x] 1.6 Wire the CI job and per-tier timing gates
    - Add a `cao_mcp_apps` job to `.github/workflows/ci.yml` (today only `web/` is built) running
      `tsc`, `vitest run`, `npm run scan:jit`, `check-bundle-size`, and `build:all`.
    - Enforce timing gates: Unit < 5 s, Component < 8 s, Integration < 15 s, E2E < 60 s.
    - _Requirements: 20.3, 20.5_
    - _Validates: Properties 9 (HTTP-only boundary), 10 (JIT-free bundle)_

- [x] 2. Phase I — Event services foundation (load-bearing)
  - [x] 2.1 Implement semantic event normalization
    - Create `src/cli_agent_orchestrator/services/event_primitives.py` with the `PRIMITIVES` tuple and
      a total `normalize_kind(event_type, detail=None)` mapping each lifecycle `event_type` to exactly
      one of `{launch, handoff, a2a_delegation, file_mod, completion, error}` or pass-through `other`;
      disambiguate `post_send_message` by `orchestration_type` (`a2a` -> `a2a_delegation`, else `handoff`).
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
    - _Validates: Properties 3 (primitive closure), 4 (normalization totality)_

  - [x]* 2.2 Write property test for `normalize_kind` totality (Hypothesis)
    - **Property 4: Normalization totality** — for any string input, `normalize_kind` returns exactly
      one value in the closed set and never raises/returns `None`.
    - **Validates: Requirements 1.1, 1.6, 1.7**

  - [x] 2.3 Implement the event-log ring buffer
    - Create `src/cli_agent_orchestrator/services/event_log_service.py`: thread-safe `EventLog` over
      `deque(maxlen=500)` with 24 h TTL, `append(kind, terminal_id, session_name, detail)` returning the
      stored row, and `history(limit, since, kinds)` (TTL filter + kinds filter + `since` + `[-limit:]`);
      store metadata only in `detail`. Expose `get_event_log()` singleton accessor.
    - _Requirements: 3.1, 3.2, 3.5, 3.6, 3.7, 18.1, 18.2_
    - _Validates: Properties 1 (ring-buffer bound), 2 (history order), 11 (privacy boundary)_

  - [x]* 2.4 Write property test for `history()` bound and order (Hypothesis)
    - **Property 1: Ring-buffer bound** — `len(EventLog) <= 500` and `history(limit)` returns at most
      `min(limit, 500)` events.
    - **Property 2: History order** — events returned in non-decreasing timestamp order.
    - **Validates: Requirements 3.1, 3.2, 3.5, 3.6**

  - [x] 2.5 Implement the SSE fan-out bus
    - Create `src/cli_agent_orchestrator/services/sse_bus.py`: `SseBus` with per-subscriber bounded
      `asyncio.Queue(maxsize=SSE_MAX_QUEUE_SIZE=256)`; non-blocking `publish()` that drops on full queue;
      async `subscribe()` generator that registers/removes the queue; `get_bus()` singleton accessor.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 18.3_

  - [x]* 2.6 Write unit tests for SSE drop-on-slow back-pressure
    - Verify a full subscriber queue drops events while other subscribers continue, and `publish()`
      never blocks the producer; disconnect removes the queue from the active set.
    - _Requirements: 4.2, 4.3, 4.4, 4.5_

  - [x] 2.7 Implement the observer-only `EventLogPublisher` plugin
    - Create `src/cli_agent_orchestrator/plugins/builtin/event_log_publisher.py` as a `CaoPlugin` with
      `@hook` methods for `post_create_terminal`, `post_create_session`, `post_send_message`,
      `post_kill_terminal`, `post_kill_session`; each normalizes via `normalize_kind`, appends to the
      Event_Log (metadata only — never message bodies), and relays to the SSE bus; mutate no Backplane state.
    - Register it via the `cao.plugins` entry point in `pyproject.toml`.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 18.1, 18.2_
    - _Validates: Properties 3 (primitive closure), 11 (privacy boundary)_

  - [x]* 2.8 Write unit tests for publisher observer-only behavior and privacy
    - Assert hooks append normalized rows + relay to SSE, store only metadata (no message body), and
      perform no state mutation.
    - _Requirements: 2.4, 2.5, 18.2_

- [ ] 3. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Phase II — Snapshot, tools, and event endpoints (backend)
  - [x] 4.1 Implement the snapshot + RFC-6902 diff service
    - Create `src/cli_agent_orchestrator/services/ui_state_service.py` with `build_dashboard_snapshot`,
      `build_agent_detail_snapshot`, and `diff_snapshot` (whole-key replace for `terminals`/`sessions`,
      per-key replace for scalars) producing the `DashboardSnapshot` shape; pure projection (no side effects).
    - _Requirements: 6.4, 9.1_
    - _Validates: Property 5 (RFC-6902 round-trip)_

  - [x]* 4.2 Write unit tests for `diff_snapshot`
    - Verify diffs cover scalar and collection changes and round-trip via patch application.
    - _Requirements: 9.1_
    - _Validates: Property 5 (RFC-6902 round-trip)_

  - [x] 4.3 Implement the MCP App tools and register them
    - Create `src/cli_agent_orchestrator/mcp_server/app_tools.py` implementing the read tools
      `render_dashboard`, `render_agent_view`, `cao_fetch_history`, `subscribe_events` (HTTP-only access to
      the FastAPI surface + process-local read services; correct `visibility` annotations and `_meta.ui`),
      plus a `submit_command` skeleton (full classification/routing completed in Phase III). Use a local
      default-off scopes helper for now (superseded by `security/auth.py` in Phase IV).
    - Add `register_app_tools(mcp)` and call it from `mcp_server/server.py` (thin one-line wiring), gated on
      `CAO_MCP_APPS_ENABLED`; return `False` + log info if registration is unavailable.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 13.1_
    - _Validates: Property 6 (single choke point)_

  - [x]* 4.4 Write unit tests for read tools + registration
    - Verify `render_dashboard` builds a snapshot from HTTP data, visibility annotations are correct,
      and disabled/unavailable registration returns `False`.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 4.5 Implement the FastAPI event endpoints
    - Add `/events` (`text/event-stream` streamed from `SseBus.subscribe()`) and `/events/history`
      (JSON replay from the ring buffer, normalized to the six primitives) to `src/cli_agent_orchestrator/api/main.py`.
    - _Requirements: 5.1, 5.2, 5.3, 5.4_
    - _Validates: Properties 2 (history order), 3 (primitive closure)_

  - [x]* 4.6 Write integration tests for the event endpoints
    - Verify `/events` streams `text/event-stream`, `/events/history` returns six-primitive events, and a
      full subscriber queue drops without blocking the producer.
    - _Requirements: 5.2, 5.3, 5.4_

  - [x] 4.7 Build the `ext_apps/` MCP App resource package
    - Create `src/cli_agent_orchestrator/ext_apps/` with `apps.py` (`DASHBOARD_RESOURCE_URI`,
      `AGENT_RESOURCE_URI`, `EVENT_STREAM_RESOURCE_URI`, `ui_meta(csp, required_scopes)`, `register_apps(mcp)`
      serving single-file HTML from `apps_static/`) and `sep2133.py` (`negotiate_capabilities`, no-op unless
      `CAO_MCP_APPS_ENABLED`); degrade gracefully (`False` + log) on older FastMCP without `@mcp.resource`.
    - _Requirements: 6.1, 6.2, 6.3, 8.1_

- [ ] 5. Phase II — Single-file view layer (frontend; file-disjoint from the backend)
  - [x] 5.1 Scaffold the `cao_mcp_apps/` single-file build
    - Extend the `cao_mcp_apps/package.json` with React 18 + Vite + `vite-plugin-singlefile`, per-view vite
      configs, and a `build:all` emitting `apps_static/{dashboard,agent,event-stream}.html`; ensure
      `allowUnsafeEval:false` is honored and no `localStorage`/`sessionStorage`/cookies are used.
    - Extend the wheel artifacts rule in `pyproject.toml` to ship `apps_static/`.
    - _Requirements: 8.1, 8.2, 8.4_
    - _Validates: Property 10 (JIT-free bundle)_

  - [x] 5.2 Implement shared types and the RFC-6902 sync client
    - Create `cao_mcp_apps/src/shared/types.ts` (`CaoEvent`, `DashboardSnapshot`, `SubmitCommandKind`,
      `SubmitCommandResult`) and `cao_mcp_apps/src/shared/patch.ts` (`applyPatch`, `clientDiff`) in pure JS
      (no JIT), with full RFC-6902 op support.
    - _Requirements: 9.1, 9.2_
    - _Validates: Property 5 (RFC-6902 round-trip)_

  - [x]* 5.3 Write property test for `patch.ts` round-trip (fast-check)
    - **Property 5: RFC-6902 round-trip** — for all snapshots `prev`, `curr`,
      `applyPatch(prev, clientDiff(prev, curr))` deep-equals `curr`; include pointer-escaping cases.
    - **Validates: Requirements 9.1**

  - [x] 5.4 Implement the MCP App lifecycle bridge
    - Create `cao_mcp_apps/src/shared/mcpApp.ts` (register handlers before `app.connect()`; `submitCommand`,
      `fetchHistory`, `startPolling`, and `silentlyNoteToModel`/`updateModelContext` stubs wired to the tool channel).
    - _Requirements: 8.3_

  - [x] 5.5 Implement shared view components
    - Create `cao_mcp_apps/src/shared/{TaskControl,EventStream,AgentStatus,HeaderBar}.tsx`; `TaskControl`
      buttons each map to exactly one `SubmitCommandKind`; render any markup/user input as escaped strings.
    - _Requirements: 12.1, 19.11_
    - _Validates: Property 3 (primitive closure)_

  - [x] 5.6 Implement the three view entry points
    - Create `cao_mcp_apps/src/dashboard/Dashboard.tsx` (poll `render_dashboard`, apply deltas),
      `cao_mcp_apps/src/agent/AgentView.tsx` (`render_agent_view` + xterm output tail), and
      `cao_mcp_apps/src/event-stream/EventStreamView.tsx` (`cao_fetch_history` + SSE subscribe); hydrate from
      the initial tool result and re-fetch history on re-mount.
    - _Requirements: 8.1, 8.3_
    - _Validates: Property 12 (re-mount idempotence)_

  - [x] 5.7 Implement the container-query layout
    - Create `cao_mcp_apps/src/shared/styles.css` with `container-type: inline-size`; single-column grid at
      `<= 350px` (no horizontal truncation) and multi-column at `>= 1280px`.
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

  - [x]* 5.8 Write component tests for container-query layouts (happy-dom)
    - Snapshot the dashboard grid at 350 px (single column, no truncation) and 1280 px (multi-column).
    - _Requirements: 10.2, 10.3, 19.15, 19.16_

- [ ] 6. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Phase III — AI-native model-context loop and choke point completion
  - [x] 7.1 Complete the `submit_command` choke point (server)
    - Finish `submit_command` in `mcp_server/app_tools.py`: classify each `kind` as standard/lifecycle/
      destructive, apply the scope pre-check (destructive -> `cao:admin`, standard/lifecycle -> `cao:write`),
      reject unknown kinds with `{success:false, error:...}`, and route authorized commands to the matching
      FastAPI mutation endpoint over HTTP, returning a structured result. Keep it the only mutating app tool.
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 14.1, 14.2, 14.3_
    - _Validates: Properties 6 (single choke point), 7 (scope monotonicity)_

    > **Route-mapping note (Task 7.1 — verified against `api/main.py` on this fork):**
    > `submit_command` stays HTTP-only (uses `requests` against `API_BASE_URL`; no `clients.*` imports).
    > Kind → real Backplane route:
    > - `create_session` → `POST /sessions` (query params `agent_profile` [required], `provider`,
    >   `session_name`, `working_directory`, `allowed_tools`; returns a `Terminal`).
    > - `send_message` → `POST /terminals/{id}/inbox/messages` (query params `sender_id`, `message`).
    >   This matches the **existing `send_message` MCP-tool semantics** (`_send_to_inbox` in `server.py`:
    >   queued inbox delivery), NOT `/input` (the direct-type path used by handoff/assign). `sender_id`
    >   defaults to `"operator"` since the operator surface has no terminal context.
    > - `assign` → **no single "create terminal + send" endpoint exists** on this fork. A
    >   reassignment-to-an-existing-agent gesture maps to the closest existing route:
    >   `POST /terminals/{id}/input` with `orchestration_type=assign` (mirrors `_send_direct_input_assign`).
    >   Documented deviation — we route to the closest existing endpoint rather than inventing an assign route.
    > - `interrupt` → `POST /terminals/{id}/key` with `key="C-c"` (SIGINT; `C-c` is accepted by the
    >   endpoint's `TMUX_KEY_PATTERN`). There is no separate interrupt/SIGINT route.
    > - `pause` / `resume` → **no corresponding terminal routes exist**, so these return a structured
    >   `{success:false, error:"unsupported"}` rather than inventing endpoints (per design guidance).
    > - `shutdown_session` → `DELETE /sessions/{name}`.
    > HTTP errors are caught and surfaced as `{success:false, error:<FastAPI detail>}`.

  - [x]* 7.2 Write unit tests for the choke point matrix
    - Cover kind classification, unknown-kind rejection, scope pre-check pass/deny per kind, and default-off
      (full scope set) passing every kind.
    - _Requirements: 13.2, 13.3, 13.4, 13.5, 13.6, 14.1, 14.2, 14.3_
    - _Validates: Properties 6 (single choke point), 7 (scope monotonicity)_

  - [x] 7.3 Implement gesture -> primitive mapping (client)
    - In `cao_mcp_apps/src/shared/{TaskControl.tsx,mcpApp.ts}`, map each mutation gesture to exactly one
      `SubmitCommandKind`; map drag-and-drop reassignment to `assign`; require `window.confirm()` for
      destructive kinds before calling `submitCommand`.
    - _Requirements: 12.1, 12.2_
    - _Validates: Properties 3 (primitive closure), 6 (single choke point)_

    > **Note (Task 7.3):** Added `buildGesturePayload(kind, target, extras)` + `DRAG_REASSIGN_KIND` to
    > `mcpApp.ts` so each gesture maps its `target` to the field the server route reads
    > (`shutdown_session`→`session_name`, everything else→`terminal_id`, `create_session`→extras only).
    > `TaskControl` now builds payloads via that helper, keeps each button → exactly one kind, gates
    > rendering by scope, requires `window.confirm()` for destructive kinds, and adds a drop zone that
    > maps a dropped task onto the target via the single `assign` kind.

  - [x] 7.4 Implement silent model-context ingestion (client)
    - Implement `silentlyNoteToModel`/`updateModelContext` in `cao_mcp_apps/src/shared/mcpApp.ts`: after a
      material action, post a single token-efficient summary note (described via a Semantic_Primitive) without
      triggering inference, excluding message bodies, and never blocking the iframe on failure.
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 12.3_
    - _Validates: Property 11 (privacy boundary)_

    > **Note (Task 7.4):** `silentlyNoteToModel` (uses `ui/update-model-context`, never `ui/message`, so it
    > does not trigger inference; swallows failures) is now wired into both view flows: `AgentView.handleSubmit`
    > and `Dashboard.handleSubmit` post exactly one body-free `describeGesture(kind, target)` note on each
    > successful material action.

  - [x]* 7.5 Write component tests for gesture mapping and model-context notes
    - Verify each gesture maps to one kind, destructive gestures require confirm, and notes are silent,
      body-free, and failure-tolerant.
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 12.2, 12.3_

- [ ] 8. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [x] 9. Phase IV — Research gate (GO / NO-GO for the rest of Phase IV)
  - [x] 9.1 Verify external auth claims and select the auth dependency (GATE)
    - Verify the upstream `@modelcontextprotocol/ext-apps` version + `allowUnsafeEval` API surface and the
      Auth0-for-MCP PRM / OBO (RFC 8693 / 8707) claims. Record findings in the spec notes.
    - **GO/NO-GO:** if the external claims verify, proceed with the Auth0-for-MCP shape; if any claim fails
      verification, fall back to generic OAuth 2.1 for all of Phase IV. Either way, add the JWT/JWKS dependency
      (`pyjwt[crypto]` or equivalent) to `pyproject.toml` pending the outcome.
    - _Requirements: 17.3_
    - _Validates: Property 8 (default-off equivalence)_

    > **Research note (ext-apps portion only — Phase II frontend gate; the Auth0/PRM/OBO portion
    > remains OPEN for Phase IV, so 9.1 stays unchecked):**
    >
    > **Package verified.** `@modelcontextprotocol/ext-apps` EXISTS on npm; latest published
    > version is **1.7.4** (`npm view @modelcontextprotocol/ext-apps version`). Spec **SEP-1865**
    > is **Stable (2026-01-26)**. Subpath exports: `.` (View `App` class), `/react` (`useApp`
    > hooks), `/app-bridge` (host side), `/server` (`getUiCapability`, `RESOURCE_MIME_TYPE`,
    > `registerTool`). peerDeps: `@modelcontextprotocol/sdk ^1.29`, `react 17–19`, `zod`.
    >
    > **API surface verified against the 2026-01-26 spec:**
    > - View acts as an MCP client over `postMessage` JSON-RPC. Handshake is
    >   `ui/initialize` → `ui/notifications/initialized`; **the host MUST NOT send to the View
    >   before `initialized`** — this confirms the design's "register handlers BEFORE `app.connect()`"
    >   (Req 8.3).
    > - Tool data arrives as **notifications**, not as a `connect()` return value:
    >   `ui/notifications/tool-input` (args) and `ui/notifications/tool-result`
    >   (`CallToolResult`). The design's "`onToolResult`" maps to handling the
    >   `ui/notifications/tool-result` notification.
    > - `updateModelContext` is REAL: method `ui/update-model-context` (View→Host request,
    >   `{content?, structuredContent?}`). Host SHOULD provide it to the model on **future**
    >   turns, MAY defer to the next user message, and overwrites prior context — i.e. silent,
    >   no immediate inference. This is exactly the design's silent model-context loop (Req 11),
    >   and is distinct from `ui/message` (which triggers a follow-up) and `notifications/message`
    >   (logging). `silentlyNoteToModel` MUST use `ui/update-model-context`, not `ui/message`.
    > - Tool visibility/linkage is `_meta.ui.visibility` (`["model","app"]` / `["app"]`) and
    >   `_meta.ui.resourceUri` — confirms the design, but the keys nest **under** `_meta.ui`.
    >   **Deviation fixed:** `ext_apps.ui_meta` + `app_tools` now emit `_meta.ui.{visibility,
    >   resourceUri,csp,requiredScopes}` (initial draft put `visibility`/`resource` at the top
    >   level). `requiredScopes` is retained as a documented CAO extension.
    > - CSP is a **structured** object `_meta.ui.csp.{connectDomains,resourceDomains,frameDomains,
    >   baseUriDomains}`, NOT a raw CSP string. **Deviation fixed:** `DEFAULT_CSP`/`ui_meta` now
    >   emit the structured shape; the host composes the header.
    > - Resource MIME type MUST be `text/html;profile=mcp-app`. **Deviation fixed:**
    >   `register_apps` now registers with that MIME type (`RESOURCE_MIME_TYPE`).
    >
    > **`allowUnsafeEval` does NOT exist as an ext-apps API.** The spec's mandatory default CSP is
    > `script-src 'self' 'unsafe-inline'` with **no** `'unsafe-eval'`; `allowUnsafeEval` is an
    > MCP-UI *host-renderer* prop, not part of ext-apps. **Adaptation:** we satisfy the design's
    > intent by building **JIT-free** bundles (no `eval`/`new Function`), enforced by the task 1.2
    > `scan-jit` gate, so the views run under the spec's no-`unsafe-eval` CSP. The frontend config
    > documents this rather than calling a non-existent API.
    >
    > **Frontend bridge decision (documented deviation).** The spec explicitly states "you don't
    > need an SDK to talk MCP with the host." To keep the single-file bundles dependency-free
    > (no `@modelcontextprotocol/ext-apps` / `@modelcontextprotocol/sdk` runtime dep — both pull
    > in code that complicates the JIT-free + single-file guarantee), `shared/mcpApp.ts` implements
    > the **spec's native `postMessage` JSON-RPC pattern directly** (ui/initialize handshake,
    > ui/notifications/tool-result + tool-input handling, tools/call for submit_command /
    > cao_fetch_history, ui/update-model-context for the silent note). The public interface
    > (`connect`/`submitCommand`/`fetchHistory`/`startPolling`/`updateModelContext`/
    > `silentlyNoteToModel`) is preserved, so swapping in the SDK later is a drop-in.
    >
    > **Auth0/PRM/OBO portion (Phase IV gate — RESOLVED, GO with generic OAuth 2.1):**
    >
    > Verified via web search against primary sources:
    > - **RFC 9728 "OAuth 2.0 Protected Resource Metadata"** — published IETF RFC (Aug 2024). Defines
    >   the `/.well-known/oauth-protected-resource` metadata document (resource, authorization_servers,
    >   scopes_supported, bearer_methods_supported) and the `WWW-Authenticate` discovery flow. REAL/CURRENT.
    > - **RFC 8693 "OAuth 2.0 Token Exchange"** — established IETF Standards-Track RFC (Jan 2020). Defines
    >   the STS token-exchange protocol used for OBO (on-behalf-of) impersonation/delegation. REAL/CURRENT.
    > - **RFC 8707 "Resource Indicators for OAuth 2.0"** — established IETF Standards-Track RFC (Feb 2020).
    >   Defines the `resource` request parameter. REAL/CURRENT.
    > - **"Auth0 Auth for MCP"** — VERIFIED REAL: Auth0 published "Auth0 Auth for MCP Is Now Generally
    >   Available" (GA, ~mid-2026), covering MCP-server authentication, client registration, and OBO token
    >   exchange, and brokering multiple IdPs (Okta, Entra ID, Ping, Google Workspace). It is, however, a
    >   **vendor product built on top of the standard OAuth 2.1 + RFC 9728/8693/8707 stack**, not a new
    >   protocol.
    > - **`pyjwt[crypto]`** — CONFIRMED the right library for RS256 + JWKS: the `cryptography` extra enables
    >   RSA signature verification and ships `jwt.PyJWKClient` for JWKS fetch/decode. Added to
    >   `pyproject.toml` as `pyjwt[crypto]>=2.8.0`.
    >
    > **GO/NO-GO decision: GO with a STANDARD generic OAuth 2.1 / RFC 9728 implementation.** All
    > load-bearing standards verified as established IETF RFCs, so Phase IV proceeds. We do **NOT**
    > hard-depend on any Auth0-specific behavior: Auth0 is treated as just one configurable IdP via
    > `CAO_AUTH_JWKS_URI` (generic JWKS), and `AUTH0_DOMAIN` is an optional convenience that derives the
    > JWKS URI. With `AUTH0_DOMAIN` unset the layer is fully **default-off** (full scope set, no
    > enforcement) — byte-for-byte identical to today's localhost-only posture. OBO (RFC 8693) is noted as
    > a future extension; Phase IV ships scope extraction + PRM + RBAC against the generic stack.

- [ ] 10. Phase IV — Default-off auth layer (proceeds only after task 9.1 GO)
  - [x] 10.1 Implement the auth core
    - Create `src/cli_agent_orchestrator/security/auth.py`: scope taxonomy (`cao:read/write/admin`),
      `extract_scopes_from_token` (RS256 + audience + expiry via JWKS, accepting `scope`/`permissions`/`scp`
      claims), a JWKS cache with 1 h TTL (reuse cached keys when the source is unreachable; fetch fresh when
      empty + reachable), `get_current_scopes` (FastAPI dependency), `get_scopes_for_local_token`, and the
      generic-IdP fallback (`CAO_AUTH_JWKS_URI`). Default-off: with `AUTH0_DOMAIN` unset, every path returns
      the full scope set. Refactor `app_tools.py` to import `get_scopes_for_local_token` from here.
    - _Requirements: 16.2, 16.3, 16.5, 16.6, 16.7, 17.1, 17.2, 17.3_
    - _Validates: Properties 7 (scope monotonicity), 8 (default-off equivalence)_

  - [x]* 10.2 Write unit tests for token/scope handling
    - Cover scope extraction across claim variants, JWKS cache TTL/reuse/empty-fetch, 401 on invalid/expired
      tokens, and default-off returning the full scope set.
    - _Requirements: 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 17.1, 17.2_
    - _Validates: Property 8 (default-off equivalence)_

  - [x] 10.3 Implement the `@requires_scopes` decorator
    - Create `src/cli_agent_orchestrator/security/decorators.py` with `requires_scopes(*scopes)` pre-checking
      local-token scopes before a tool impl runs (default-off -> full set -> always passes).
    - _Requirements: 16.1_
    - _Validates: Property 7 (scope monotonicity)_

  - [x] 10.4 Implement the Protected Resource Metadata endpoint
    - Add `/.well-known/oauth-protected-resource` to `api/main.py` returning the resource audience,
      authorization servers, supported scopes (`cao:read/write/admin`), and bearer methods when auth is enabled;
      return HTTP 404 when auth is disabled. Add `Depends(get_current_scopes)` to mutation endpoints.
    - _Requirements: 15.1, 15.2_
    - _Validates: Property 8 (default-off equivalence)_

    > **Note (Tasks 10.1/10.4):** `security/auth.py` implements generic OAuth 2.1 (RS256 via `jwt.PyJWKClient`),
    > default-off keyed on `AUTH0_DOMAIN`/`CAO_AUTH_JWKS_URI` being unset (full scope set, no enforcement).
    > `app_tools.py` now imports `get_scopes_for_local_token` + `SCOPE_*` from `security/auth.py` (the Phase II
    > local helper was removed); the HTTP-only guard still passes because `security/` imports only `constants`,
    > `fastapi`, and `pyjwt` (never `clients.tmux`/`clients.database`). `Depends(get_current_scopes)` was added
    > to exactly the five routed mutation endpoints submit_command targets: `POST /sessions`,
    > `DELETE /sessions/{name}`, `POST /terminals/{id}/input`, `POST /terminals/{id}/key`, and
    > `POST /terminals/{id}/inbox/messages`. Default-off, the dependency returns the full set without inspecting
    > the request, so all existing endpoint behavior is byte-for-byte unchanged (verified: full 396-test backend
    > suite green).

  - [x]* 10.5 Write integration tests for PRM and the RBAC matrix
    - Verify the PRM schema when enabled and 404 when disabled, the `@requires_scopes` RBAC matrix, and the
      enabled-auth 401/403 flow.
    - _Requirements: 15.1, 15.2, 16.1, 16.4_

- [ ] 11. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [x] 12. Phase V — Close the 16-point test matrix and raise the floor
  - [x]* 12.1 Complete the Unit tier cells
    - Add remaining unit tests across EventLog/primitives, choke point, view-layer (`applyPatch`/`clientDiff`),
      and auth (`extract_scopes_from_token`, JWKS cache TTL) per the matrix.
    - _Requirements: 19.10, 19.12_

    > **Note (Task 12.1):** Added a server-side oversized-payload backstop at the choke point
    > (`MAX_PAYLOAD_CHARS = 16000` + `_payload_too_large` in `mcp_server/app_tools.py`, measured on the
    > serialized payload) so an oversized task is rejected with a structured warning before any Backplane
    > call (Req 19.12) — defense-in-depth behind the View's 4000-char cap. New backend cells in
    > `test/mcp_server/test_app_tools.py`: empty agent name -> `create_session` validation error (Req 19.10),
    > oversized payload rejected + size-boundary accepted (Req 19.12). New frontend cells in
    > `cao_mcp_apps/src/shared/patchOps.test.ts` exercise the `applyPatch` ops `clientDiff` never emits
    > (add/remove on arrays, whole-doc replace, move/copy/test, error paths) plus the pointer-token helpers,
    > raising `patch.ts` to 94%. EventLog/primitives/auth cells were already complete from Phases I/IV.

  - [x]* 12.2 Complete the Component tier cells (happy-dom)
    - Zero-agents placeholder (no cards), active-agents cards with status badges, escaped-markup input,
      scope-gated button rendering.
    - _Requirements: 19.1, 19.2, 19.11_

    > **Note (Task 12.2):** `cao_mcp_apps/src/test/component.test.tsx` — zero-agents shows ONLY the
    > placeholder and renders no `agent-card` (19.1); active agents render one card each with the correct
    > status-badge text + `cao-status-*` modifier class, including the safe `unknown` fallback (19.2);
    > malicious `<img onerror>`/`<script>` markup in event metadata, agent profile/provider, and the task
    > textarea renders as an escaped string with no live node injected (19.11, no XSS); scope-gated rendering
    > hides every control under `cao:read`, hides only the admin-destructive control under `cao:write`, shows
    > it under `cao:admin`, and shows everything default-off.

  - [x]* 12.3 Complete the Integration tier cells (Mock Host)
    - Untrusted-origin postMessage ignored, no-UI-surface structured plain-text results, Backplane-unreachable
      retry control, and `oninitialized` replay / re-mount hydration.
    - _Requirements: 19.9, 19.13, 19.14_
    - _Validates: Property 12 (re-mount idempotence)_

    > **Note (Task 12.3):** Built a Mock Host harness (`cao_mcp_apps/src/test/mockHost.ts`) — a postMessage
    > JSON-RPC peer with two `FakeWindow` buses (iframe/host split) so the View never receives its own frames.
    > `cao_mcp_apps/src/test/integration.test.tsx` drives it: a forged frame from an untrusted origin is dropped
    > and the dashboard grid is unchanged (19.13); a no-UI-surface host (`uiSurface:false`) still returns a
    > structured plain-text `CallToolResult` whose text round-trips to the structured payload, while a UI-capable
    > host's `structuredContent` is unwrapped (19.9); an unreachable Backplane surfaces the retry control which
    > recovers once the tool succeeds (19.14, enabled by a small `startPolling(onError)` hook + Dashboard wiring);
    > `oninitialized` tool-result replay and EventStreamView re-mount both reproduce an identical timeline
    > (Property 12). Also added executable cells for iframe-teardown listener release (19.8, via a new
    > `ui/resource-teardown` -> `disconnect()` auto-handler in `mcpApp.connect`) and host-mediated AgentView
    > hydration + choke-point send.

  - [x]* 12.4 Complete the E2E tier cells (Playwright)
    - Launch agent -> new card, send task -> event-stream updates, open agent detail -> terminal sub-iframe,
      stop agent -> status `stopped`, oversized payload rejected, iframe teardown releases listeners,
      30 s auto-refresh cycle.
    - _Requirements: 19.3, 19.4, 19.5, 19.6, 19.7, 19.8, 19.12_

    > **Note (Task 12.4) — AUTHORED-BUT-NOT-EXECUTED-LOCALLY:** The sandbox network mode is
    > `COMMON_DEPENDENCIES` (allowlisted domains only), which blocks the Playwright browser binary download, so
    > these specs were fully authored and CI-wired but **not run locally** — no faked E2E results. They are
    > structured to pass in CI, where `npm run test:e2e:install` (`playwright install --with-deps chromium`)
    > succeeds. Deliverables: `cao_mcp_apps/playwright.config.ts` (E2E `timeout: 60_000` enforces the tier
    > budget; `webServer` builds the bundles then starts the harness), a self-contained harness
    > (`e2e/server.mjs` static+SSE server on 127.0.0.1:9889 — the event-stream bundle's default origin;
    > `e2e/host.html` + `e2e/host.js` MCP host JSON-RPC peer with canned mutable fleet state and a
    > `window.__host` control surface), and three specs: `e2e/dashboard.spec.ts` (launch->new card 19.3,
    > stop->`stopped` 19.6, 30s auto-refresh re-poll 19.7, teardown releases listeners 19.8), `e2e/agent.spec.ts`
    > (agent terminal-log detail renders 19.5, oversized payload rejected with submit-count 0 19.12), and
    > `e2e/event-stream.spec.ts` (send task -> live SSE event-stream update 19.4, via the combo harness page).
    > The harness server itself was verified locally at the HTTP layer (serves host page, host.js, built bundles,
    > and the `/emit` SSE relay). A `cao-mcp-apps-e2e` CI job runs them. Documented deviation: Req 19.7 names
    > `cao_list_sessions`; the implemented 30s refresh polls `render_dashboard` (which aggregates sessions), so
    > the spec asserts that refresh tool's re-poll on the 30s cycle.

  - [x] 12.5 Raise the coverage ratchet floor
    - Update `.coverage-baseline.json` to the new measured coverage so the ratchet locks in the gains.
    - _Requirements: 20.1_

    > **Note (Task 12.5):** Re-measured: backend `pytest --cov` = **87.21%** (2872 passed, 7 skipped),
    > frontend `vitest --coverage` = **90.92%** (51 passed). Raised `.coverage-baseline.json` floors from the
    > Phase 0 `null` placeholders to **python 87.0% / frontend 90.0%** (a small margin below measured to absorb
    > cross-Python-version jitter while still blocking regressions). The ratchet was previously inert in CI
    > (the `cao-mcp-apps` job ran vitest without `--coverage` and never produced `coverage.json`); wired the job
    > to emit both reports (`vitest run --coverage` + a backend `pytest --cov-report=json` step) before
    > `coverage:ratchet`, so the raised floors now actually enforce. Verified locally: ratchet reports
    > `OK: python 87.21% (floor 87.00%)`, `OK: frontend 90.92% (floor 90.00%)`.

- [ ] 13. Final checkpoint — Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core
  implementation tasks are never optional.
- Each task references specific requirement sub-clauses and the correctness properties it validates.
- The design has a Correctness Properties section, so property-based tests are included: Hypothesis
  (Python) for `normalize_kind` totality and `history()` bound/order; fast-check (TS) for the
  `patch.ts` RFC-6902 round-trip.
- Build-order seam: `app_tools.py` (Phase II/III) initially uses a local default-off scopes helper;
  task 10.1 (Phase IV) supersedes it by importing from `security/auth.py`, preserving byte-for-byte
  default-off behavior.
- Task 9.1 is a GO/NO-GO research gate: the rest of Phase IV proceeds only after it resolves, using
  generic OAuth 2.1 as the fallback if external claims fail verification.
- The frontend view layer (`cao_mcp_apps/`) is file-disjoint from the backend auth layer
  (`security/`), so those tracks can run in parallel (see the dependency graph).
- All tasks are code-only; no dogfooding, deployment, or multi-agent-orchestration meta-tasks.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6"] },
    { "id": 1, "tasks": ["2.1", "2.3", "2.5", "4.1", "4.7", "5.1", "9.1"] },
    { "id": 2, "tasks": ["2.2", "2.4", "2.6", "2.7", "4.2", "5.2", "5.4"] },
    { "id": 3, "tasks": ["2.8", "4.3", "4.5", "5.3", "5.5", "5.7"] },
    { "id": 4, "tasks": ["4.4", "4.6", "5.6"] },
    { "id": 5, "tasks": ["5.8", "7.1", "7.3", "7.4"] },
    { "id": 6, "tasks": ["7.2", "7.5", "10.1"] },
    { "id": 7, "tasks": ["10.2", "10.3", "10.4"] },
    { "id": 8, "tasks": ["10.5", "12.1", "12.2", "12.3", "12.4"] },
    { "id": 9, "tasks": ["12.5"] }
  ]
}
```
