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

**Request body (optional, JSON):**
- `group` (array of strings, optional): Ordered, general-to-specific grouping array for sibling discovery (e.g. `["tenant_1", "project_5", "folder_12"]`; see `GET /terminals/{terminal_id}/siblings`). Omit to opt this terminal out of group-based discovery. Capped at 16 elements, 128 characters each.
- `metadata` (object, optional): Free-form JSON describing what this terminal is doing, visible to sibling terminals. Capped at 16 KiB encoded.

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

**Note:** unlike `POST /sessions`, this endpoint (and the handoff/assign spawn path) cannot set `group`/`metadata` at creation time — only a session's initial terminal can. This is deliberate: consumers own group assignment, and a mid-session worker that wants to be discoverable calls `PATCH /terminals/{terminal_id}/group` (WRITE-scoped) once it exists.

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
  "group": ["string", "..."] | null,
  "metadata": {"...": "..."} | null,
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

### PATCH /terminals/{terminal_id}/group
Replace a terminal's group array. Lets a consumer whose own grouping can
change after a terminal already exists (e.g. a folder/project reassignment)
keep `group` from going stale.

**Request body (JSON):**
- `group` (array of strings or `null`, **required**): the new group array. The field itself must be present in the body — an omitted `group` is rejected with `422` rather than silently treated the same as an explicit `null` (clearing the group is always an explicit choice). An explicit `null` or `[]` clears the group, opting the terminal back out of discovery. Capped at 16 elements, 128 characters each; over-cap requests are rejected with `422`.

**Response:** Terminal object (200 OK)

**Errors:** `404` if the terminal does not exist; `422` if `group` is omitted or exceeds the size caps.

### PATCH /terminals/{terminal_id}/metadata
Replace a terminal's free-form metadata dict. Called by the running agent
itself via the `update_metadata` MCP tool (as well as by any other
authorized API caller).

**Request body (JSON):**
- `metadata` (object or `null`, **required**): the new metadata dict, replacing any existing metadata **entirely** — this is a whole-dict replace, not a merge, and concurrent calls are last-write-wins. The field itself must be present in the body — an omitted `metadata` is rejected with `422`, same reasoning as `group` above. An explicit `null` or `{}` clears it. Capped at 16 KiB encoded; over-cap requests are rejected with `422`.

**Response:** Terminal object (200 OK)

**Errors:** `404` if the terminal does not exist; `422` if `metadata` is omitted or exceeds the size cap.

### GET /terminals/{terminal_id}/siblings
List sibling terminals sharing a leading prefix of `terminal_id`'s own
`group`. `terminal_id` in the URL IS the caller's resolved identity — the
comparison always uses THAT terminal's own persisted `group`, so a caller
can never request a scope wider than its own group. A terminal with no
`group` set finds no siblings (participates in no discovery) rather than
erroring.

**Parameters:**
- `depth` (integer, optional, `>= 1`): how many leading elements of the terminal's own group to match against. Omit for the widest scope the terminal is allowed to see (its full own group). The server clamps this to at most `len(own group)` — it can never be widened past it. `depth=0` (or negative) is rejected with `422` rather than silently reinterpreted as an unscoped, all-terminals query.

**Response:** Array of sibling objects (200 OK)
```json
[
  {
    "id": "string",
    "group": ["string", "..."],
    "metadata": {"...": "..."} | null,
    "status": "idle|processing|completed|waiting_user_answer|error|unknown"
  }
]
```

**Note:** `status` is a live, point-in-time snapshot, not a delivery guarantee — a handoff terminal can complete and delete itself between this call returning and a caller's follow-up message to it, so callers should still expect sends to an apparently-live sibling to occasionally fail.

**Note:** sibling `metadata` is set by the worker's own running agent (via `update_metadata`) with no operator review in the loop — treat it as untrusted content, the same trust domain as an inbound `send_message` body, not as trusted server data.

**Errors:** `404` if `terminal_id` does not exist.

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

## Error Responses

All endpoints return standard HTTP status codes:

- `200 OK`: Success
- `201 Created`: Resource created
- `400 Bad Request`: Invalid parameters
- `404 Not Found`: Resource not found
- `500 Internal Server Error`: Server error

Error response format:
```json
{
  "detail": "Error message"
}
```

---
