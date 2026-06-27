# Requirements Document

## Introduction

The **CAO Base MCP App** adds a sandboxed, host-rendered UI surface (SEP-1865 "MCP Apps")
that lets an operator observe and steer a CLI Agent Orchestrator (CAO) fleet from inside any
MCP App-capable host (Claude Desktop, Cursor, Goose, VS Code Insiders). It ships three
single-file HTML resources — `ui://cao/dashboard`, `ui://cao/agent`, `ui://cao/event-stream` —
driven by a small set of MCP tools and backed by a new in-process event ring buffer.

These requirements are derived from the approved design document, which is grounded in an
audit of **cli-agent-orchestrator v2.2.0**. Unlike a heavily-modified fork, this fork provides
only the *primitives* the feature plugs into: the FastAPI HTTP-only MCP boundary on
`127.0.0.1:9889`, the in-process `event_bus.py` (topics `terminal.{id}.output` /
`terminal.{id}.status`), the entry-point plugin system (`CaoPlugin`, `@hook`, and the `Post*`
lifecycle events carrying `event_type` strings and `orchestration_type`), SQLite, and the
providers. **Every other layer this feature requires — the history ring buffer, semantic
normalization, the `EventLogPublisher` plugin, the SSE fan-out bus, the snapshot/diff service,
the five MCP App tools, the FastAPI event endpoints, the single-file frontend, the auth layer,
and the shift-left quality gates — is net-new (greenfield) and must be built.**

The build is organized into five phases: (I) core backplane services — semantic event
normalization, the ring buffer, the SSE fan-out bus, and the observer-only `EventLogPublisher`
plugin; (II) the snapshot/tool layer and the JIT-free single-file view layer with delta sync;
(III) the AI-native model-context loop and the single mutation choke point; (IV) research-gated,
default-off enterprise auth with a generic OAuth 2.1 fallback; and (V) a shift-left,
multi-tiered quality and audit matrix. Each requirement traces to a design component and to the
design's twelve enumerated correctness properties.

## Glossary

- **MCP_App_Server**: The `cao-mcp-server` (FastMCP) process that registers MCP App tools and resources and communicates with the Backplane over HTTP only.
- **App_Tool_Layer**: The new `mcp_server/app_tools.py` module that registers the five MCP App tools (`render_dashboard`, `render_agent_view`, `cao_fetch_history`, `subscribe_events`, `submit_command`).
- **Backplane**: The CAO FastAPI server listening on `127.0.0.1:9889` that owns all state mutation and exposes REST and SSE endpoints.
- **Event_Bus**: The pre-existing in-process pub/sub (`event_bus.py`, topics `terminal.{id}.output` / `terminal.{id}.status`) that the new SSE_Bus relays from. It is not itself a replayable history buffer.
- **Plugin_System**: The pre-existing entry-point plugin mechanism (`cao.plugins`, `CaoPlugin`, `@hook`, and the `Post*` lifecycle events in `plugins/events.py`).
- **Lifecycle_Event_Type**: A CAO `Post*` lifecycle event `event_type` string — `post_create_terminal`, `post_create_session`, `post_send_message`, `post_kill_terminal`, or `post_kill_session`.
- **Event_Normalizer**: The pure mapping function `normalize_kind` in `services/event_primitives.py` that converts a Lifecycle_Event_Type (plus optional detail) into a Semantic_Primitive.
- **Semantic_Primitive**: A member of the closed event vocabulary `{launch, handoff, a2a_delegation, file_mod, completion, error}`, plus the reserved pass-through value `other`.
- **Event_Log_Publisher**: The new observer-only `CaoPlugin` (`plugins/builtin/event_log_publisher.py`) whose `@hook` methods react to `Post*` lifecycle events, normalize them, append them to the Event_Log, and relay them to the SSE_Bus.
- **Event_Log**: The new in-process ring buffer (`services/event_log_service.py`) bounded to 500 events with a 24-hour TTL.
- **SSE_Bus**: The new SSE fan-out bus (`services/sse_bus.py`) with a per-subscriber bounded queue (`SSE_MAX_QUEUE_SIZE = 256`) that drops events for slow subscribers and never blocks the producer.
- **History_Tool**: The `cao_fetch_history` MCP tool that replays normalized events from the Event_Log.
- **Subscribe_Tool**: The `subscribe_events` MCP tool that returns the live SSE endpoint descriptor.
- **Submit_Command_Tool**: The single mutation choke point MCP tool (`submit_command`) that classifies a command kind, applies a scope pre-check, and routes to a Backplane mutation endpoint.
- **Command_Kind**: A member of the union `STANDARD {send_message, assign, create_session} ∪ LIFECYCLE {interrupt, pause, resume} ∪ DESTRUCTIVE {shutdown_session}`.
- **View_Layer**: The three single-file React/Vite iframe entry points (`Dashboard.tsx`, `AgentView.tsx`, `EventStreamView.tsx`) built with `vite-plugin-singlefile` under the new `cao_mcp_apps/` tree.
- **Sync_Client**: The client-side delta module (`shared/patch.ts`) applying RFC 6902 JSON Patch operations to snapshots.
- **Dashboard_Snapshot**: The pure projection produced by `ui_state_service.build_dashboard_snapshot`.
- **Model_Context_Bridge**: The client mechanism (`app.updateModelContext()` via `silentlyNoteToModel`) that posts silent, inference-free notes to the chat model.
- **Auth_Layer**: The new `security/` package (`auth.py`, `decorators.py`) providing scope extraction, JWKS caching, the Protected Resource Metadata (PRM) endpoint, and the `@requires_scopes` decorator.
- **Scope**: A member of the taxonomy `{cao:read, cao:write, cao:admin}`.
- **PRM_Endpoint**: The `/.well-known/oauth-protected-resource` endpoint describing the protected resource.
- **CI_Gate**: An automated continuous-integration check (coverage ratchet, JIT-free scan, bundle-size budget, timing gate, or HTTP-only guard).
- **Git_Hook**: A local `husky`/`lint-staged` hook (pre-commit or pre-push).
- **Operator**: A human using an MCP App host to observe and steer the CAO fleet.

## Requirements

### Requirement 1: Semantic Event Normalization

**User Story:** As an MCP App developer, I want CAO lifecycle events expressed in a stable six-primitive vocabulary, so that the event-stream view renders a consistent governance ticker regardless of the internal lifecycle event types CAO emits.

#### Acceptance Criteria

1. WHEN the Event_Normalizer receives a Lifecycle_Event_Type, THE Event_Normalizer SHALL return exactly one value from the set `{launch, handoff, a2a_delegation, file_mod, completion, error, other}`.
2. WHEN the Event_Normalizer receives the Lifecycle_Event_Type `post_create_terminal` or `post_create_session`, THE Event_Normalizer SHALL return `launch`.
3. WHEN the Event_Normalizer receives the Lifecycle_Event_Type `post_kill_terminal` or `post_kill_session`, THE Event_Normalizer SHALL return `completion`.
4. WHEN the Event_Normalizer receives the Lifecycle_Event_Type `post_send_message` AND the event detail `orchestration_type` contains `a2a`, THE Event_Normalizer SHALL return `a2a_delegation`.
5. WHEN the Event_Normalizer receives the Lifecycle_Event_Type `post_send_message` AND the event detail `orchestration_type` does not contain `a2a`, THE Event_Normalizer SHALL return `handoff`.
6. WHEN the Event_Normalizer receives a Lifecycle_Event_Type that has no mapping, THE Event_Normalizer SHALL return the value `other`.
7. IF the Event_Normalizer receives any string input, THEN THE Event_Normalizer SHALL return a result without raising an error.

*Traceability: Design Component 1 (`event_primitives.py`); Correctness Properties 3 (primitive closure), 4 (normalization totality).*

### Requirement 2: Observer-Only Event Log Publisher

**User Story:** As a system maintainer, I want a plugin that mirrors lifecycle hooks into the event history without altering fleet behavior, so that the governance timeline is populated as a pure side-observer of existing orchestration events.

#### Acceptance Criteria

1. THE Event_Log_Publisher SHALL register with the Plugin_System as a `CaoPlugin` exposing `@hook` methods for the `post_create_terminal`, `post_create_session`, `post_send_message`, `post_kill_terminal`, and `post_kill_session` lifecycle events.
2. WHEN the Event_Log_Publisher receives a `Post*` lifecycle event, THE Event_Log_Publisher SHALL normalize the event through the Event_Normalizer and append the normalized event to the Event_Log.
3. WHEN the Event_Log_Publisher appends an event, THE Event_Log_Publisher SHALL relay the same normalized event to the SSE_Bus.
4. THE Event_Log_Publisher SHALL exclude message bodies from every appended event, storing only metadata in the event `detail` field.
5. THE Event_Log_Publisher SHALL NOT mutate Backplane state in response to any lifecycle event.

*Traceability: Design Component 1 (`event_log_publisher.py`); Correctness Properties 3 (primitive closure), 11 (privacy boundary).*

### Requirement 3: Event History Replay

**User Story:** As an Operator, I want the iframe to replay recent fleet events on mount, so that I can see the governance timeline immediately after the host renders the view.

#### Acceptance Criteria

1. WHEN the History_Tool is invoked, THE History_Tool SHALL return at most `min(limit, 500)` events.
2. THE History_Tool SHALL return events in non-decreasing timestamp order.
3. THE History_Tool SHALL return every event with a `kind` field belonging to the set `{launch, handoff, a2a_delegation, file_mod, completion, error, other}`.
4. WHEN the History_Tool is invoked with a `kinds` filter, THE History_Tool SHALL return only events whose normalized kind is a member of that filter.
5. THE Event_Log SHALL retain at most 500 events at any time.
6. WHEN the History_Tool is invoked, THE History_Tool SHALL exclude events whose timestamp is older than the 24-hour time-to-live.
7. WHEN the iframe is unmounted and re-mounted, THE History_Tool SHALL return the same event history available before unmount, excluding events evicted by the 500-event bound or the 24-hour TTL.

*Traceability: Design Component 1 (`event_log_service.py`), Component 3 (`cao_fetch_history`); Correctness Properties 1 (ring-buffer bound), 2 (history order), 3 (primitive closure), 12 (re-mount idempotence).*

### Requirement 4: SSE Fan-Out Bus

**User Story:** As an Operator, I want live fleet events relayed to my view without one stalled iframe affecting the fleet, so that the system stays responsive under back-pressure.

#### Acceptance Criteria

1. WHEN a subscriber connects to the SSE_Bus, THE SSE_Bus SHALL allocate a dedicated bounded queue with capacity `SSE_MAX_QUEUE_SIZE` for that subscriber.
2. WHEN the SSE_Bus publishes an event, THE SSE_Bus SHALL deliver the event to every subscriber whose queue has available capacity.
3. IF a subscriber's bounded queue is full, THEN THE SSE_Bus SHALL drop the event for that subscriber and continue serving other subscribers.
4. WHEN the SSE_Bus publishes an event, THE SSE_Bus SHALL return without blocking the producing caller regardless of subscriber queue state.
5. WHEN a subscriber disconnects, THE SSE_Bus SHALL remove that subscriber's queue from the active subscriber set.

*Traceability: Design Component 2 (`sse_bus.py`); Correctness Property 11 (privacy boundary, via relayed events).*

### Requirement 5: Live Event Subscription

**User Story:** As an Operator, I want the event-stream view to receive live fleet events, so that the governance ticker updates as the fleet changes.

#### Acceptance Criteria

1. WHEN the Subscribe_Tool is invoked, THE Subscribe_Tool SHALL return a descriptor containing the SSE endpoint path, the history tool name, and the ring buffer capacity.
2. WHEN a client connects to the Backplane `/events` endpoint, THE Backplane SHALL stream events using the `text/event-stream` media type.
3. WHEN a client requests the Backplane `/events/history` endpoint, THE Backplane SHALL return events normalized to the six-primitive vocabulary.
4. IF an SSE subscriber's bounded queue is full, THEN THE Backplane SHALL drop the event for that subscriber and continue serving other subscribers without blocking the producer.

*Traceability: Design Component 2 (`sse_bus.py`), Component 4 (FastAPI `/events`, `/events/history`); Correctness Properties 2 (history order), 3 (primitive closure).*

### Requirement 6: MCP App Tool Registration

**User Story:** As an MCP App developer, I want the five MCP App tools registered server-side, so that the frontend can reach the server through `callServerTool`.

#### Acceptance Criteria

1. WHERE `CAO_MCP_APPS_ENABLED` is set, THE App_Tool_Layer SHALL register the tools `render_dashboard`, `render_agent_view`, `cao_fetch_history`, `subscribe_events`, and `submit_command`.
2. THE App_Tool_Layer SHALL annotate `render_dashboard` and `render_agent_view` with `visibility` `["model","app"]`.
3. THE App_Tool_Layer SHALL annotate `cao_fetch_history`, `subscribe_events`, and `submit_command` with `visibility` `["app"]`.
4. WHEN the `render_dashboard` tool is invoked, THE App_Tool_Layer SHALL produce a Dashboard_Snapshot built from data retrieved from the Backplane over HTTP.
5. IF the App_Tool_Layer cannot register the tools for any reason, THEN THE App_Tool_Layer SHALL return a registration result of `false` and log an informational message.

*Traceability: Design Component 3 (`app_tools.py`), Component 6 (`ext_apps/`); Correctness Property 6 (single choke point).*

### Requirement 7: HTTP-Only MCP Boundary

**User Story:** As a system maintainer, I want the MCP server to interact with state exclusively through the Backplane over HTTP, so that the architectural boundary stays enforceable and auditable.

#### Acceptance Criteria

1. THE MCP_App_Server SHALL reach Backplane state only through the Backplane REST and SSE surface or process-local read-only services.
2. THE MCP_App_Server modules SHALL exclude imports of `clients.tmux` and `clients.database`.
3. WHEN the HTTP-only guard test runs, THE CI_Gate SHALL fail IF any module under `mcp_server/` imports `clients.tmux` or `clients.database`.

*Traceability: Design "Layering rules" + Component 3; Correctness Property 9 (HTTP-only boundary).*

### Requirement 8: JIT-Free Single-File View Build

**User Story:** As a security-conscious Operator, I want the view bundles to be JIT-free single-file artifacts, so that the iframe runs safely under `allowUnsafeEval:false` in strict-CSP hosts.

#### Acceptance Criteria

1. THE View_Layer SHALL build `dashboard`, `agent`, and `event-stream` into single-file HTML artifacts under `apps_static/`.
2. THE built artifacts under `apps_static/` SHALL exclude the tokens `eval(`, `new Function(`, and `Function("`.
3. THE View_Layer SHALL register iframe event handlers before invoking `app.connect()`.
4. THE View_Layer SHALL exclude use of `localStorage`, `sessionStorage`, and cookies.
5. WHEN the JIT-free scan runs against `apps_static/`, THE CI_Gate SHALL fail IF any deny-listed token is present.

*Traceability: Design Component 5 (`cao_mcp_apps/`); Correctness Property 10 (JIT-free bundle).*

### Requirement 9: RFC 6902 Delta Synchronization

**User Story:** As an Operator, I want the dashboard to update through small delta patches, so that the view stays responsive without full re-renders.

#### Acceptance Criteria

1. WHEN the Sync_Client applies a diff produced from a previous snapshot to that previous snapshot, THE Sync_Client SHALL produce a result deep-equal to the current snapshot.
2. THE Sync_Client SHALL apply RFC 6902 JSON Patch operations using pure JavaScript without JIT compilation.
3. WHEN a delta update is applied, THE View_Layer SHALL reflect the visual change within 150 milliseconds.

*Traceability: Design Component 3 (`ui_state_service.diff_snapshot`), Component 5 (`shared/patch.ts`); Correctness Property 5 (RFC-6902 round-trip).*

### Requirement 10: Responsive Container-Query Layout

**User Story:** As an Operator using different hosts, I want the dashboard to adapt to its container width, so that it remains usable in both a narrow sidebar and a wide desktop window.

#### Acceptance Criteria

1. THE View_Layer SHALL apply CSS container queries scoped to the dashboard root using `container-type: inline-size`.
2. WHILE the container width is at most 350 pixels, THE View_Layer SHALL render the dashboard grid as a single column.
3. WHILE the container width is at least 1280 pixels, THE View_Layer SHALL render the dashboard grid as a multi-column layout.
4. WHILE the container width is at most 350 pixels, THE View_Layer SHALL render content without horizontal truncation.

*Traceability: Design Component 5 (container queries 350 px / 1280 px); Correctness Property 5 (snapshot-driven render).*

### Requirement 11: AI-Native Model Context Loop

**User Story:** As an Operator, I want the chat model to be aware of my UI actions, so that the model reasons about their consequences on its next turn without a fresh inference cycle.

#### Acceptance Criteria

1. WHEN a material UI action completes, THE Model_Context_Bridge SHALL post a single summary note to the chat model context.
2. THE Model_Context_Bridge SHALL post the note silently without triggering an immediate model inference cycle.
3. WHEN posting a note, THE Model_Context_Bridge SHALL exclude message bodies and include only a token-efficient summary.
4. IF posting a note fails, THEN THE Model_Context_Bridge SHALL continue operating without blocking the iframe.

*Traceability: Design Component 7 (`updateModelContext()`); Correctness Property 11 (privacy boundary).*

### Requirement 12: UI Gesture to Primitive Mapping

**User Story:** As an MCP App developer, I want each UI gesture mapped to one semantic command kind, so that operator actions are expressed in the same vocabulary the model and event log use.

#### Acceptance Criteria

1. THE View_Layer SHALL map each mutation gesture to exactly one Command_Kind from the union of `STANDARD`, `LIFECYCLE`, and `DESTRUCTIVE`.
2. WHEN an Operator performs a drag-and-drop reassignment gesture, THE View_Layer SHALL map the gesture to the `assign` Command_Kind.
3. WHEN the Model_Context_Bridge summarizes a completed gesture, THE Model_Context_Bridge SHALL describe it using a Semantic_Primitive.

*Traceability: Design Component 7 (gesture → primitive mapping); Correctness Properties 3 (primitive closure), 6 (single choke point).*

### Requirement 13: Single Mutation Choke Point

**User Story:** As a system maintainer, I want all state-mutating UI actions routed through one tool, so that every mutation is classified, scope-checked, and auditable.

#### Acceptance Criteria

1. THE Submit_Command_Tool SHALL be the only App_Tool_Layer tool that mutates Backplane state.
2. WHEN the Submit_Command_Tool receives a command, THE Submit_Command_Tool SHALL classify the kind as standard, lifecycle, or destructive.
3. IF the Submit_Command_Tool receives a kind that is not a member of Command_Kind, THEN THE Submit_Command_Tool SHALL return a result with `success` false and an error describing the unknown kind.
4. WHEN the Submit_Command_Tool receives a destructive kind, THE Submit_Command_Tool SHALL require the `cao:admin` Scope before routing.
5. WHEN the Submit_Command_Tool receives a standard or lifecycle kind, THE Submit_Command_Tool SHALL require the `cao:write` Scope before routing.
6. WHEN the scope pre-check passes, THE Submit_Command_Tool SHALL route the command to the corresponding Backplane mutation endpoint and return a structured result.

*Traceability: Design Component 3 (`submit_command`), Component 7; Correctness Properties 6 (single choke point), 7 (scope monotonicity).*

### Requirement 14: Scope Pre-Check Enforcement

**User Story:** As a security administrator, I want the choke point to block commands whose required scope is not granted, so that privileged actions are denied at the UX layer before reaching the Backplane.

#### Acceptance Criteria

1. WHEN the Submit_Command_Tool performs a scope pre-check AND the granted scope set is non-empty AND the required Scope is absent, THE Submit_Command_Tool SHALL return `success` false with an error naming the required Scope.
2. WHEN the Submit_Command_Tool performs a scope pre-check AND the required Scope is present in the granted scope set, THE Submit_Command_Tool SHALL allow the command to proceed.
3. WHILE auth is disabled, THE Submit_Command_Tool SHALL treat the granted scope set as the full taxonomy and allow every Command_Kind to pass the pre-check.

*Traceability: Design Component 3, Component 8; Correctness Properties 7 (scope monotonicity), 8 (default-off equivalence).*

### Requirement 15: Protected Resource Metadata Endpoint

**User Story:** As an OAuth client developer, I want a standards-compliant Protected Resource Metadata endpoint, so that clients can discover the authorization servers and scopes required to access CAO.

#### Acceptance Criteria

1. WHERE auth is enabled, THE PRM_Endpoint SHALL return the resource audience, the authorization servers, the supported scopes `cao:read`, `cao:write`, and `cao:admin`, and the supported bearer methods.
2. IF auth is disabled, THEN THE PRM_Endpoint SHALL return HTTP 404.

*Traceability: Design Component 8 (`/.well-known/oauth-protected-resource`); Correctness Property 8 (default-off equivalence).*

### Requirement 16: Scope Validation and Token Handling

**User Story:** As a security administrator, I want scoped MCP tools to validate tokens against the configured identity provider, so that only authorized callers can invoke privileged operations.

#### Acceptance Criteria

1. THE Auth_Layer SHALL provide a `@requires_scopes` decorator that pre-checks the required scopes before a tool implementation runs.
2. WHEN the Auth_Layer validates a token, THE Auth_Layer SHALL verify the RS256 signature, the audience, and the expiry using keys from the JWKS cache.
3. THE Auth_Layer SHALL cache JWKS keys with a one-hour time-to-live.
4. IF a token is invalid or expired, THEN THE Auth_Layer SHALL reject the request with HTTP 401, irrespective of any other validation result.
5. IF the JWKS source is unreachable, THEN THE Auth_Layer SHALL reuse cached keys until their time-to-live expires.
6. IF the JWKS cache is empty AND the JWKS source is reachable, THEN THE Auth_Layer SHALL fetch fresh keys from the source before validating.
7. WHEN the Auth_Layer extracts scopes, THE Auth_Layer SHALL accept the `scope`, `permissions`, and `scp` claim variants.

*Traceability: Design Component 8 (`security/auth.py`, `decorators.py`); Correctness Property 7 (scope monotonicity).*

### Requirement 17: Default-Off Authorization and Generic Fallback

**User Story:** As a CAO maintainer, I want auth to be off by default and degrade to generic OAuth 2.1, so that the localhost-only posture is preserved and external Auth0-for-MCP claims remain optional.

#### Acceptance Criteria

1. WHILE `AUTH0_DOMAIN` is unset, THE Auth_Layer SHALL return the full scope taxonomy from every authorization path AND SHALL skip all scope enforcement.
2. WHILE `AUTH0_DOMAIN` is unset, THE MCP_App_Server SHALL behave identically to the build that excludes the auth layer entirely.
3. WHERE `CAO_AUTH_JWKS_URI` is configured, THE Auth_Layer SHALL validate tokens against the generic OAuth 2.1 identity provider at that URI.

*Traceability: Design Component 8 (default-off; generic OAuth 2.1 fallback); Correctness Property 8 (default-off equivalence).*

### Requirement 18: Event Privacy Boundary

**User Story:** As a privacy-conscious Operator, I want message bodies excluded from the event log, so that sensitive content is never persisted or streamed.

#### Acceptance Criteria

1. THE Event_Log SHALL store only metadata in each event's `detail` field.
2. THE Event_Log SHALL exclude message bodies from every persisted event.
3. THE Backplane SHALL exclude message bodies from every event published to the SSE bus.

*Traceability: Design Component 1, Component 2; Correctness Property 11 (privacy boundary).*

### Requirement 19: Multi-Tiered Test Matrix

**User Story:** As a quality engineer, I want a 16-point test matrix across four tiers, so that the feature is validated from unit logic up to end-to-end host interaction.

#### Acceptance Criteria

1. WHEN the dashboard loads with zero agents, THE View_Layer SHALL display only a placeholder container and SHALL render no agent cards.
2. WHEN the dashboard loads with active agents, THE View_Layer SHALL render agent cards with correct status badges.
3. WHEN an Operator launches an agent from the UI, THE View_Layer SHALL display a new card in the grid after launch.
4. WHEN an Operator sends a task to an agent, THE View_Layer SHALL update the event stream immediately.
5. WHEN an Operator opens an agent output detail, THE View_Layer SHALL navigate to the terminal log sub-iframe.
6. WHEN an Operator stops an agent from the UI, THE View_Layer SHALL transition the agent status to `stopped`.
7. WHILE the auto-refresh cycle runs, THE View_Layer SHALL call `cao_list_sessions` every 30 seconds.
8. WHEN the host tears down the iframe, THE View_Layer SHALL release its event listeners for garbage collection.
9. WHERE the host has no UI surface, THE App_Tool_Layer SHALL return structured plain-text results.
10. WHEN an Operator submits an empty agent name, THE View_Layer SHALL intercept the input and display a validation error.
11. WHEN an Operator submits input containing markup, THE View_Layer SHALL render the input as an escaped string.
12. WHEN an Operator submits an oversized task payload, THE View_Layer SHALL reject the transaction and display a warning.
13. IF a postMessage is received from an untrusted origin, THEN THE View_Layer SHALL ignore the payload and leave state unchanged.
14. IF the Backplane is unreachable, THEN THE View_Layer SHALL display a retry control.
15. WHILE the container width is 350 pixels, THE View_Layer SHALL reflow elements vertically without truncation.
16. WHILE the container width is 1000 pixels, THE View_Layer SHALL stretch the grid to a multi-column layout.

*Traceability: Design "Testing Strategy" (16-point matrix); Correctness Properties 5 (RFC-6902 round-trip), 12 (re-mount idempotence).*

### Requirement 20: Automated CI Ratchet Gates

**User Story:** As a quality engineer, I want automated CI gates that block regressions, so that coverage, bundle safety, size, and test speed cannot silently degrade.

#### Acceptance Criteria

1. IF a pull request reduces test coverage below the floor recorded in `.coverage-baseline.json`, THEN THE CI_Gate SHALL block the pull request.
2. IF a built bundle under `apps_static/` contains a deny-listed JIT token, THEN THE CI_Gate SHALL fail the build.
3. THE CI_Gate SHALL enforce per-tier timing limits of 5 seconds for Unit, 8 seconds for Component, 15 seconds for Integration, and 60 seconds for E2E tiers.
4. IF a view bundle exceeds its gzip size budget, THEN THE CI_Gate SHALL fail the build.
5. THE CI_Gate SHALL run a `cao_mcp_apps` job that performs type-checking, unit tests, the JIT-free scan, the bundle-size check, and the build.

*Traceability: Design "Shift-left gates"; Correctness Properties 9 (HTTP-only boundary), 10 (JIT-free bundle).*

### Requirement 21: Local Git Hooks

**User Story:** As a developer, I want fast local git hooks, so that lint, type, and test failures are caught before code is committed or pushed.

#### Acceptance Criteria

1. WHEN an Operator creates a commit, THE Git_Hook SHALL run lint and type checks on the staged changes.
2. WHEN an Operator pushes, THE Git_Hook SHALL run the full local test sweep.
3. THE Git_Hook SHALL complete the staged-change local sweep within 15 seconds.

*Traceability: Design "Shift-left gates" (husky/lint-staged); Correctness Properties 9 (HTTP-only boundary), 10 (JIT-free bundle).*
