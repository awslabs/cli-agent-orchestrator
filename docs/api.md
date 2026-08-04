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
- `POST /agents/profiles/install` installs a profile.
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
- `/workflows/runs*` starts, inspects, cancels, and resumes runs and retrieves
  run output.

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
