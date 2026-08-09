# CLI Agent Orchestrator API Documentation

Base URL: `http://localhost:9889` (default)

## Health Check

### GET /health
Check if the server is running.

**Response:**
```json
{
  "status": "ok",
  "service": "cli-agent-orchestrator"
}
```

---

## Providers

### GET /agents/providers
List available providers with installation status.

**Response:** Array of provider objects
```json
[
  {
    "name": "kiro_cli",
    "binary": "kiro-cli",
    "installed": true
  },
  {
    "name": "claude_code",
    "binary": "claude",
    "installed": true
  },
  {
    "name": "codex",
    "binary": "codex",
    "installed": true
  },
  {
    "name": "kimi_cli",
    "binary": "kimi",
    "installed": false
  },
  {
    "name": "hermes",
    "binary": "hermes",
    "installed": true
  },
  {
    "name": "copilot_cli",
    "binary": "copilot",
    "installed": false
  }
]
```

**Note:** The `installed` field checks if the provider binary is available in the system PATH via `shutil.which()`.

---

## Sessions

### POST /sessions
Create a new session with one terminal.

**Parameters:**
- `provider` (string, required): Provider type ("kiro_cli", "claude_code", "codex", "antigravity_cli", "hermes", "kimi_cli", "copilot_cli", "opencode_cli", or "cursor_cli")
- `agent_profile` (string, required): Agent profile name
- `session_name` (string, optional): Custom session name
- `working_directory` (string, optional): Working directory for the agent session

**Response:** Terminal object (201 Created)

### GET /sessions
List all sessions.

**Response:** Array of session objects

### GET /sessions/{session_name}
Get details of a specific session.

**Response:** Session object with terminals list

### DELETE /sessions/{session_name}
Delete a session and all its terminals.

**Response:**
```json
{
  "success": true
}
```

---

## Terminals

**Note:** All `terminal_id` path parameters must be 8-character hexadecimal strings (e.g., "a1b2c3d4").

### POST /sessions/{session_name}/terminals
Create an additional terminal in an existing session.

**Parameters:**
- `provider` (string, required): Provider type
- `agent_profile` (string, required): Agent profile name
- `working_directory` (string, optional): Working directory for the terminal
- `allowed_tools` (string, optional): Comma-separated list of allowed CAO tools for the worker.
- `caller_id` (string, optional): Terminal ID of the creating terminal (8-character hexadecimal). Recorded so `send_message` can default replies to the caller (issue #284).
- `defer_init` (bool, optional, default `false`): When `true`, return as soon as the tmux window and DB record exist, without waiting for `provider.initialize()` to finish. The provider is still created and initialized — but on a background asyncio task on cao-server, so the HTTP round-trip stays under ~2s regardless of provider startup latency. Used by the MCP `assign` tool to keep tool-call latency well under kiro-cli 2.11's ~60s per-tool client timeout, and to allow multiple concurrent assigns to run their init phases in parallel.

**Request body (optional, JSON):** the deferred-init message payload is sent in the body — not query params — so prompt content is not exposed in HTTP access logs and is not subject to URL-length limits.
- `initial_message` (string, optional): When `defer_init=true`, this message is delivered to the newly created worker via `send_input` after `provider.initialize()` completes. Ignored if `defer_init=false`. Ordering: init runs first, then message delivery, both on the same background task.
- `initial_message_orchestration_type` (string, optional): One of `assign` or `handoff`. Passed through to `send_input` for plugin event emission when `initial_message` is delivered.

**Response:** Terminal object (201 Created). When `defer_init=true`, the returned status is `unknown` (the provider is still initializing on a background task); poll `GET /terminals/{id}` for the live status before sending further input.

### GET /sessions/{session_name}/terminals
List all terminals in a session.

**Response:** Array of terminal objects

### GET /terminals/{terminal_id}
Get terminal details.

**Response:** Terminal object
```json
{
  "id": "string",
  "name": "string",
  "provider": "kiro_cli|claude_code|codex|antigravity_cli|hermes|kimi_cli|copilot_cli|opencode_cli|cursor_cli",
  "session_name": "string",
  "agent_profile": "string",
  "caller_id": "string|null",
  "status": "idle|processing|completed|waiting_user_answer|error",
  "last_active": "timestamp"
}
```

### POST /terminals/{terminal_id}/input
Send input to a terminal.

**Parameters:**
- `message` (string, required): Message to send

**Response:**
```json
{
  "success": true
}
```

### POST /terminals/{terminal_id}/key
Send a tmux key sequence to a terminal. Use this for interactive prompts that
require non-text key presses, such as Hermes clarify picker navigation.

The endpoint is generic, but the only in-tree structured consumer today is the
Hermes path of `answer_user_prompt`. Other providers can use it in the future
when they expose equivalent prompt states or key-navigation flows.

**Parameters:**
- `key` (string, required): allowed tmux key name: `Up`, `Down`, `Left`,
  `Right`, `Enter`, `Tab`, `Escape`, `Space`, a single alphanumeric key, or a
  `C-`, `M-`, or `S-` modifier combo such as `C-c` or `M-x`

**Response:**
```json
{
  "success": true
}
```

### GET /terminals/{terminal_id}/output
Get terminal output.

**Parameters:**
- `mode` (string, optional): Output mode - "full" (default), "last", or "tail"
  - `"full"` returns the StatusMonitor rolling buffer (most recent ~8KB of streamed output), not unbounded scrollback. Long sessions are truncated to the tail; use the on-disk terminal log for complete history.

**Response:**
```json
{
  "output": "string",
  "mode": "string"
}
```

### GET /terminals/{terminal_id}/working-directory
Get the current working directory of a terminal's pane.

**Response:**
```json
{
  "working_directory": "/home/user/project"
}
```

**Note:** Returns `null` if working directory is unavailable.

### POST /terminals/{terminal_id}/exit
Send provider-specific exit command to terminal.

**Behavior:**
- Calls the provider's `exit_cli()` method to get the exit command
- Text commands (e.g., `/exit`, `quit`) are sent as literal text via `send_input()`
- Key sequences prefixed with `C-` or `M-` (e.g., `C-d` for Ctrl+D) are sent as tmux key sequences via `send_special_key()`, which tmux interprets as actual key presses

| Provider | Exit Command | Type |
|----------|-------------|------|
| kiro_cli | `/exit` | Text |
| claude_code | `/exit` | Text |
| codex | `/exit` | Text |
| antigravity_cli | `/exit` | Text |
| hermes | `/exit` | Text |
| kimi_cli | `/exit` | Text |
| copilot_cli | `/exit` | Text |

**Response:**
```json
{
  "success": true
}
```

### DELETE /terminals/{terminal_id}
Delete a terminal.

**Response:**
```json
{
  "success": true
}
```

---

## Inbox (Terminal-to-Terminal Messaging)

### POST /terminals/{receiver_id}/inbox/messages
Send a message to another terminal's inbox.

**Parameters:**
- `sender_id` (string, required): Sender terminal ID
- `message` (string, required): Message content

**Response:**
```json
{
  "success": true,
  "message_id": "string",
  "sender_id": "string",
  "receiver_id": "string",
  "created_at": "timestamp"
}
```

**Behavior:**
- Messages are queued and delivered when the receiver terminal is IDLE
- Messages are delivered in order (oldest first)
- Delivery is automatic via event-driven status detection

---

## Memory

REST mirror of the `cao memory` CLI. All `/memory` endpoints return `404` with
`"Memory system is disabled"` when `memory.enabled` is false in settings.json;
use `GET /settings/memory` to discover the enabled state (e.g. for hiding UI).

Keys must match `^[a-z0-9-]{1,60}$` and `scope_id` must match
`^[a-zA-Z0-9._-]{1,128}$`; malformed values return `422`.

Because the server's working directory is not the user's project, project scope
is addressed by an explicit `scope_id` query parameter (the resolved project
ID). This intentionally diverges from the MCP `memory_forget` tool, which
resolves context from the calling terminal.

Known inconsistency: the internal `GET /terminals/{id}/memory-context` endpoint
predates this contract and returns an empty `200` (not `404`) when memory is
disabled.

### GET /settings/memory
Return whether the memory subsystem is enabled.

**Response:**
```json
{
  "enabled": true
}
```

### GET /memory
List stored memories across all projects (the CLI's `cao memory list --all`).

**Parameters:**
- `scope` (string, optional): Filter by scope (`global`, `project`, `session`, `agent`)
- `type` (string, optional): Filter by memory type (`user`, `feedback`, `project`, `reference`)
- `scope_id` (string, optional): Filter to one project/session/agent
- `limit` (integer, optional): Max results, 1–100 (default: 50)

**Response:**
```json
[
  {
    "key": "string",
    "scope": "string",
    "scope_id": "string|null",
    "memory_type": "string",
    "tags": "string",
    "created_at": "timestamp",
    "updated_at": "timestamp"
  }
]
```

`scope_id` is the project ID for project memories, the session/agent ID for
those scopes, and `null` for global.

### GET /memory/export
Export one memory scope as an archive bundle (the CLI's `cao memory export`).
Streams a gzipped tarball of the OKF bundle (topic files plus `index.md` and
`manifest.md`).

**Parameters:**
- `scope` (string, required): Scope to export (`global`, `project`, or `federated`; `400` for the private `session`/`agent` scopes — there is no include-private escape hatch over HTTP)
- `format` (string, optional): Archive format (default: `okf`; `400` on unknown formats)
- `scope_id` (string): Required for `project` scope (`400` if missing)
- `include_history` (boolean, optional): Include `history/<key>.md` files (default: `false`)
- `redact` (boolean, optional): Redact secret matches instead of skipping the topic (default: `false`)

**Response:** `200` with `Content-Type: application/gzip` — the bundle tarball
as the response body.

When API auth is enabled, this endpoint requires a token carrying at least the
read scope (`cao:read`, `cao:write`, or `cao:admin`); requests without one are
`403`'d.

### GET /memory/{key}
Show a memory by key (first match wins when the same key exists in several
scopes; narrow with `scope`/`scope_id`).

**Parameters:**
- `scope` (string, optional): Scope to search in
- `scope_id` (string, optional): Project/session/agent to search in

**Response:** the list entry shape plus `"content"` (the latest wiki section).
`404` if no exact key match.

### DELETE /memory/{key}
Delete a memory by key.

**Parameters:**
- `scope` (string, optional): Scope of the memory (default: `project`)
- `scope_id` (string): Required for `project`, `session`, and `agent` scopes (`400` if missing)

**Response:**
```json
{
  "success": true
}
```

`404` if the key does not exist in the scope.

### DELETE /memory
Clear all memories in a scope. Best-effort: deletion continues past
per-item failures and reports how many were removed.

**Parameters:**
- `scope` (string, required): Scope to clear
- `scope_id` (string): Required for `project`, `session`, and `agent` scopes (`400` if missing)

**Response:**
```json
{
  "success": true,
  "deleted_count": 3
}
```

---

## Stable-Agent Roster (read-only, M3-A / cond-0377)

The roster is the fork-owned durable record of stable CAO agents, their
harness-native conversation lineages, and their disposable physical
incarnations.  All roster endpoints are **read-only**: they never mutate
roster state.  The durable records are written by the launch seams
(managed-v2 `bind_native`, unmanaged terminal creation, admission
completion, teardown retirement) and by the native-identity repair seam.

All four routes require at least the **read** authorization scope
(`cao:read`); write and admin scopes also satisfy them.  Legacy, missing,
corrupt, or unknown-version rows are reported truthfully and never crash
a read or audit.

### GET /roster/agents

Every stable agent, oldest first.

**Query parameters:**
- `session_name` (optional): scope the listing to one CAO session name.

**Response (200):**
```json
{
  "schema": "cao-m3-roster-list-v1",
  "agents": [
    {
      "agent_id": "…", "session_name": "…", "role": "worker",
      "profile_family": "developer", "disposition": "live",
      "disposition_known": true,
      "resume_contract_version": "cao-m3-resume-contract-v1",
      "current_lineage_id": "…", "current_incarnation_id": "…",
      "revision": 3, "created_at": "…", "updated_at": "…"
    }
  ]
}
```

`role` is `supervisor` or `worker`; `disposition` is one of `live`,
`dormant`, `identity_missing`, `retired` (`disposition_known` is `false`
for an unrecognized stored value).  An `identity_missing` agent has a
current lineage with no native session id — truthful, never fabricated,
and never a blocker for Stop.

### GET /roster/agents/{agent_id}

One stable agent with its full lineage and incarnation history.

**Response (200):**
```json
{
  "schema": "cao-m3-roster-agent-v1",
  "agent": { "…": "…", "current_lineage": { "…": "…" }, "current_incarnation": { "…": "…" } },
  "lineages": [ { "lineage_id": "…", "harness": "…", "native_session_id": "…",
                  "acquisition_method": "…", "route_provenance": { "…": "…" },
                  "predecessor_lineage_id": "…", "lineage_origin": "…" } ],
  "incarnations": [ { "incarnation_id": "…", "terminal_id": "…", "generation": "…",
                      "pane_id": "…", "disposition": "bound|admitted|retired" } ]
}
```

**Errors:** an unknown `agent_id` returns **404** with
`{"detail": "unknown stable agent: …"}`.

### GET /roster/terminals/{terminal_id}

The roster incarnation for one terminal, or an explicit null.

**Query parameters:**
- `generation` (optional): read exactly that generation's incarnation.

**Semantics:** with `generation`, the exact incarnation for that
(terminal, generation) pair, or `null`.  Without it, the **unique live**
incarnation of the terminal id — two live incarnations sharing a
terminal id (legacy/corrupt state) return **409** ("ambiguous") rather
than picking an arbitrary historical row.

**Response (200):**
```json
{ "schema": "cao-m3-roster-incarnation-v1", "incarnation": { "…": "…" } }
```

### GET /roster/audit

A truthful, **non-mutating** dry-run audit for later migration and
status repair.

**Response (200):**
```json
{
  "schema": "cao-m3-roster-audit-v1",
  "agents_total": 0, "lineages_total": 0, "incarnations_total": 0,
  "live_count": 0, "dormant_count": 0, "identity_missing_count": 0,
  "identity_missing_agents": [],
  "legacy_candidates_count": 0,
  "legacy_candidates": [ { "terminal_id": "…", "session_name": "…",
                           "provider": "…", "native_session_id": "…" } ],
  "problems": [],
  "problems_count": 0
}
```

`legacy_candidates` lists live terminals that already carry a native
session id but have no roster incarnation yet (migration candidates).
`problems` reports corrupt provenance JSON, unknown dispositions,
unknown resume-contract versions, dangling current pointers, and
agent/incarnation disposition inconsistencies — never fatal.  The audit
never mutates roster state.

## Error Responses

All endpoints return standard HTTP status codes:

- `200 OK`: Success
- `201 Created`: Resource created
- `400 Bad Request`: Invalid parameters
- `404 Not Found`: Resource not found
- `409 Conflict`: Immutable identity conflict, admission refused, or an
  ambiguous terminal-only roster lookup
- `500 Internal Server Error`: Server error
- `503 Service Unavailable`: The roster/attachment store could not be read

Error response format:
```json
{
  "detail": "Error message"
}
```

---
