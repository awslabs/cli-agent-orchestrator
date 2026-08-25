# API Overview

The default base URL is `http://localhost:9889`.

This page maps the public API by family and gives representative requests.
When `cao-server` is running, its generated FastAPI OpenAPI schema and schema
UI are the exhaustive contract for individual HTTP operations. OpenAPI does
not describe WebSocket behavior; the PTY WebSocket contract is documented
below.

## Representative HTTP usage

```bash
curl http://localhost:9889/health
curl http://localhost:9889/sessions
curl http://localhost:9889/agents/providers
```

HTTP errors use standard status codes and generally return a JSON `detail`
field. Authentication and network behavior depend on server configuration;
see [Configuration](configuration.md) and [Security](../SECURITY.md).

## HTTP route families

### Health and auth discovery

- `GET /health` reports service health.
- `GET /.well-known/oauth-protected-resource` publishes OAuth protected
  resource metadata when applicable.

### Events and AG-UI

- `/events` and `/events/history` expose server events.
- `/agui/v1/stream` and `/agui/v1/emit_ui` provide the AG-UI stream and
  generative UI input.

See [AG-UI](agui.md) for enablement, event shapes, and privacy boundaries.

### Profiles, providers, and settings

- `GET /agents/profiles` and `GET /agents/profiles/{name}` list and inspect
  installed profiles.
- `GET /agents/profiles/search` ranks installed, loadable profiles by capability
  using the same service as `cao profile find`.
- `GET /agents/profiles/templates` lists public template metadata (`name` and
  `description` only); internal template filesystem paths are never returned.
- `GET /agents/profiles/templates/{category}/{name}/schema` returns a template's
  JSON-Schema.
- `POST /agents/profiles/templates/validate` validates a config object against
  a template's JSON-Schema without writing a profile.
- `POST /agents/profiles/templates/preview` validates and renders a template to
  Markdown without writing a profile.
- `POST /agents/profiles/validate` validates a finished profile's frontmatter
  against the profile JSON-Schema plus CAO conventions, without writing
  anything. This is the HTTP equivalent of `cao profile validate`, and is
  distinct from `templates/validate`, which checks a template *config* against
  that template's own schema. Findings are severity-tagged (`error` or
  `warning`); only errors clear the `valid` flag, so warnings are advisory.
- `GET /agents/profiles/schema` returns the agent profile JSON-Schema, so a
  client can render create and edit forms from the server's definition instead
  of duplicating the field list.
- `POST /agents/profiles/install` installs a profile.
- `POST /agents/profiles` creates a profile in the local store from a supplied
  document. Named distinctly from `install`, which takes a bare profile name or
  an https:// URL rather than the document itself. The request carries `name`
  and `content`; the two identities of a profile, its storage key and its
  frontmatter `name`, must agree, so a mismatch is a 400 rather than a silent
  rename. A conflicting name returns 409. Requires `cao:write` or `cao:admin`.
- `PUT /agents/profiles/{name}` replaces an existing local-store profile and
  never creates one. A request naming a built-in or provider-managed profile
  returns 404 rather than writing a local file that would shadow the original.
  Requires `cao:write` or `cao:admin`.
- `DELETE /agents/profiles/{name}` removes a profile from the local store.
  Requires `cao:write` or `cao:admin`, the same guard as create and replace, so
  one credential covers the whole create/edit/delete cycle. Scopes are a flat
  set rather than a hierarchy, so requiring admin here would 403 a caller
  holding exactly `cao:write`. Built-ins are not deletable, for the same reason
  they are not replaceable.
- Both write routes run the profile validator on the exact submitted document
  before persisting anything, so an invalid profile never reaches disk. Errors
  reject the request with 400 and the findings attached; warnings do not block
  the write and are returned in the response so a client can surface them after
  a successful save.
- The validator rejects non-string mapping keys. A profile is written as YAML,
  which allows any scalar as a key, but the format is described by JSON Schema,
  where object keys are strings. Without this rule `mcpServers: {1: {...}}`
  validates clean and persists, then fails to load, since the model requires
  string keys. Note YAML also auto-types an unquoted date, so `2026-01-01:` is a
  date key rather than a string; quote such keys.
- A document is rejected up front if it cannot safely be handed to the steps that
  follow, on any of three grounds: how large it renders, how deeply it nests, or
  whether it contains a cycle. The reason is that YAML anchors decouple a
  document's rendered size from its byte count, and the schema step interpolates a
  rendering of an offending value into every error message it builds. Chained
  anchors multiply structure, and aliasing one large scalar multiplies content, so
  a request under the 256 KB `content` cap can render to gigabytes either way. The
  ceilings are therefore in *rendered bytes*, the unit that cost is paid in: at
  most 1 MB, about 3.8x the largest request that can arrive and ~2060x the largest
  bundled profile's 485 bytes, and at most 64 levels of nesting (~21x). Exceeding
  either is itself an error and nothing further runs, since the later steps are
  what such a document is expensive in. A cycle is rejected rather than measured:
  it has no finite rendering, and the providers that consume a profile cannot
  serialize one, so accepting it would persist a document the runtime cannot
  install. Individual schema findings are also length-capped before they reach a
  response, which bounds the case where several fields each render a subtree.
- Within those bounds, containers already visited are skipped, so each offending
  mapping key is reported once, at the first path that reaches it. Note that
  differs from the schema step, which does not memoize and so reports a shared
  invalid value once per referencing path.
- An `mcpServers` entry must define either `command`, for a server CAO launches,
  or `url`, for a remote one whose `type` names its transport. The schema
  previously required `command` unconditionally, which made the write routes
  reject url-based servers that the runtime accepts and passes through to the
  provider unchanged. An entry defining neither is still rejected. `url` is the
  spelling `resolve_mcp_server_config` documents; an entry naming its endpoint
  under any other key satisfies neither branch and is rejected.
- Every 400 from the profile write and source routes uses one `detail` shape,
  `{"message", "errors"}`, so a client never has to switch on the type of
  `detail`. `errors` is empty for a failure that is not attributable to a field,
  but the key is always present. This covers rejected names as well as schema
  findings. 404 and 409 keep FastAPI's conventional bare-string `detail`, since
  the status code already discriminates and there are no findings to attach.
- `GET /agents/profiles/{name}/source` returns a profile's document exactly as
  stored. Use this, not `GET /agents/profiles/{name}`, when the document is
  going to be edited and written back: that route returns the *resolved*
  profile, having applied `${VAR}` substitution from the managed environment
  file to the raw text before parsing. Round-tripping a resolved document
  through a write would persist substituted values into a plaintext profile.
  Requires `cao:read`, `cao:write`, or `cao:admin`, the same guard the profile
  reads beside it now carry. Gating matters at least as much here as on the parsed
  route, because this one returns the stored bytes verbatim from every configured
  store, including documents that fail to parse.
- Template validation and preview require the selected template to include a
  `schema.json` file.
- `/agents/providers` reports provider availability.
- `/settings/*` exposes supported agent-directory, skill-directory, and memory
  settings.

See [Agent Profiles](agent-profile.md) and
[Configuration](configuration.md).

### Skills

- `/skills/{name}` retrieves an installed skill.

See [Skills](skills.md) for discovery, installation, and catalog behavior.

### Sessions and terminals

- `/sessions*` creates, lists, inspects, and deletes sessions.
- `/sessions/{session_name}/terminals*` creates and lists session terminals.
- `/terminals/{terminal_id}*` inspects terminals, sends input or keys, reads
  output and working-directory state, exits providers, and deletes terminals.
- `GET /terminals/{terminal_id}/output?mode=full` returns the StatusMonitor
  rolling buffer (most recent `state_buffer_max` bytes of streamed output —
  server setting, 32KB by default, see [Configuration](configuration.md)),
  not unbounded scrollback. Long sessions are truncated to the tail; use the
  on-disk terminal log for complete history.
- Terminal creation accepts `use_worktree` (bool, default `false`, issue #100
  Phase 1): provisions an isolated `git worktree` on its own branch instead of
  sharing `working_directory` as given, requiring the resolved directory to be
  inside a git repository. At deletion, the worktree's working-tree contents
  are always discarded, but the branch is only deleted if it has no unmerged
  commits — commit and merge/push results before the terminal is deleted if
  they need to be kept. See the MCP `handoff`/`assign` tool descriptions for
  the full behavior.
- `POST /sessions` accepts optional `group`/`metadata` at creation, opting a
  session's initial terminal into peer discovery (a mid-session worker uses
  `PATCH /terminals/{terminal_id}/group`/`metadata` instead — see below).
  `group` is an ordered, general-to-specific array (e.g.
  `["tenant_1", "project_5"]`); `metadata` is a free-form JSON object the
  running agent updates via the `update_metadata` MCP tool. Both PATCH
  endpoints are whole-value replace, not merge, last-write-wins under
  concurrent calls, and reject an omitted field with `422` (an explicit
  `null`/`[]`/`{}` clears the value; omitting it does not).
- `GET /terminals/{terminal_id}/siblings` lists other terminals sharing a
  leading prefix of `terminal_id`'s own `group`, optionally narrowed by
  `depth`; a caller can never see a wider scope than its own group, and a
  terminal with no `group` set finds no siblings. Session-scoped by default
  — results are also filtered to the caller's own tmux session unless the
  explicit `cross_session=true` opt-in is passed. Sibling `metadata` is
  agent-authored, untrusted content — same trust domain as an inbound
  `send_message` body. The `list_siblings`/`update_metadata` MCP tools also
  require the `discovery` entry in `allowedTools` — a separate opt-in from
  orchestration tools, not bundled into `@cao-mcp-server` — see
  [Tool Restrictions](tool-restrictions.md) and
  [Discovery Tool Coexistence](discovery-tool-coexistence.md).

**`group` is an organizational label, not a security boundary.** On a
default install with auth disabled, a worker already has local shell access
to this API, so `group`/`discovery`/session-scoping provide no tenant
isolation or access-control guarantee even used together — do not build a
security boundary on top of them.

Terminal identifiers used in these routes are eight-character hexadecimal
strings. See [Control Planes](control-planes.md) for operator-facing choices.

### Inbox

- `/terminals/{terminal_id}/inbox/messages` sends and reads terminal inbox
  messages.

Agents normally use the in-session
[supervisor protocols](../skills/cao-supervisor-protocols/SKILL.md) rather
than calling these routes directly.

### Workflows

- `/workflows*` validates and inspects workflow specifications.
- `POST /workflows/runs` starts a run **inline** and holds the connection until it
  finishes, returning the complete result.
- `POST /workflows/runs:submit` starts a run **asynchronously**: it returns `202` with
  `{run_id, state, links}` as soon as the run is durably journaled, then drives the run in
  the background. The `links` map always carries `self`/`status`/`result`/`cancel`;
  `events` appears only on a build that serves the events route, so treat it as optional.
- `GET /workflows/runs` lists journaled runs newest-first (`?state=`, `?limit=`).
- `GET /workflows/runs/{run_id}` **inspects** a run: run metadata, current state, and
  each step's durable projection — including the step's full `output_json` and
  `error` text. ⚠️ This is the most payload-bearing read on the surface, and the
  output it returns is **not** gated by `workflow_journal_capture_output`; see the
  retention note in [Configuration](configuration.md#memory-memory). It is a
  superset of the older status-snapshot shape, so callers reading only
  `state`/`current_step_id`/`steps[].{id,state,attempts}` are unaffected.
- `GET /workflows/runs/{run_id}/result` returns the complete retained result. It is
  assembled from the journal, so it answers for a **detached, in-flight, or post-restart**
  run — not only a finished one. No run-level `output` field is returned (run-level output
  is not journaled); per-step outputs are on `steps[].output`.
- `POST /workflows/runs/{run_id}/cancel` cooperatively cancels a run;
  `POST /workflows/runs/{run_id}/resume` re-drives a crashed/failed one.
- `GET /workflows/runs/{run_id}/events` returns the run's ordered event timeline with
  any **declared** gaps. One content-negotiated path, two arms: send
  `Accept: text/event-stream` (or `?stream=true`) for a live SSE follow, otherwise a
  JSON page. `?after_seq=` is the replay cursor and must be `>= 0`; on the SSE arm it
  takes precedence over `Last-Event-ID`. A gap is data the server declares when an
  append was lost — never inferred by the client from seq numbering.
- `GET /workflows/runs/{run_id}/compare?against={other_run_id}` reports per-step
  differences between two runs. Outputs are compared at the reference level, never by
  diffing payloads. An unknown id on **either** side is a 404, not a partial compare.
- `GET /workflows/runs/{run_id}/diagnostics` returns a troubleshooting bundle: spec
  identifier + content hash, sanitized inputs, the event timeline with declared gaps,
  step outcomes, provider/agent/engine environment, and terminal/artifact references.
  Output excerpts appear only when `workflow_journal_capture_output` is on;
  `capture_enabled` in the body declares which posture produced the bundle.
- `DELETE /workflows/runs/{run_id}` removes a run and all its retained data (run row,
  steps, events, seq high-water) in one cascade. Requires a write or admin scope.
  A **running** run returns 409 — cancel it first; an already-absent run returns 204.
- `GET /terminals/{id}/output/range?start=&length=` reads a byte-exact window of a
  terminal's append-only log, for correlating a step with the terminal output it
  produced. `length` is capped server-side.

All five reads above (inspect, events, compare, diagnostics, and the run list)
require a `cao:read`, `cao:write`, or `cao:admin` scope **when authentication is
enabled**. With `CAO_AUTH_ENABLED` unset — the default — that check is inert.

See [Workflows](workflows.md).

### Memory and graph

- `/settings/memory` reports memory enablement (including `learning_enabled`).
- `/memory*` lists, reads, exports, and deletes memories.
- `/memory/relationships*` lists, creates, patches, promotes, rejects, and
  soft-deletes typed relationships between memories. `GET` is read-scoped and
  capped by `limit` (default 50, max 100); the mutating routes are write-scoped.
  `DELETE` is a soft-delete — the row is retained with `status=deleted`.
- `/graph/{provider}*` projects and exports graph views.
- `/outcomes` records (`POST`, write-scope) and lists (`GET`) workflow
  outcomes for the self-learning loop. Both return 404 while
  `memory.learning_enabled` is false.

See [Memory](memory.md), [Self-Learning](self-learning.md), and
[Knowledge Graph Viewing](knowledge-graph-viewing.md).

### Flows

- `/flows*` creates, lists, reads, deletes, enables, disables, and runs
  scheduled flows.

See [Flows](flows.md).

## PTY WebSocket

Connect to:

```text
/terminals/{terminal_id}/ws
```

The path must identify an existing terminal. This endpoint is unauthenticated
and grants full read/write access to that terminal's PTY.

### Client access boundary

By default, only loopback clients identified as `127.0.0.1`, `::1`, or
`localhost` are allowed. `CAO_WS_ALLOWED_CLIENTS` adds comma-separated client
IP addresses or hostnames to that allowlist. A literal `*` disables the
client-IP restriction.

Adding clients or using `*` gives those clients full PTY read/write access.
Treat either change as a security-boundary change and do not expose the
endpoint to untrusted networks. See the
[network configuration](configuration.md#network-network--env-var-only) for
related server settings.

### Frames and messages

The server sends binary WebSocket frames containing raw PTY bytes.

Clients send JSON in text frames:

```json
{"type":"input","data":"ls -la\n"}
```

The `input` message writes the UTF-8 string in `data` to the PTY.

```json
{"type":"resize","rows":24,"cols":80}
```

The `resize` message changes the PTY dimensions. Missing values default to 24
rows and 80 columns.

### Close outcomes

- `4003`: the client is restricted, or terminal/backend target metadata is
  invalid.
- `4004`: the terminal does not exist, or the backend cannot attach to it.
- A normal viewer disconnect detaches that viewer and preserves the session.

Malformed JSON, missing input data, unsupported message types, and other
forwarding errors do not currently have a documented stable application close
code.
