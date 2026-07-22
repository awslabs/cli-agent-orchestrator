# submit_command Kind Catalog

Every state-changing UI gesture flows through the single `submit_command(kind, payload)` choke point. This reference documents each kind, its required/optional payload fields, scope requirements, and the Backplane endpoint it routes to.

## Scope Requirements

| Tier | Required Scope | Kinds |
|------|---------------|-------|
| Standard | `cao:write` | `send_message`, `assign`, `create_session` |
| Lifecycle | `cao:write` | `interrupt`, `pause`, `resume` |
| Destructive | `cao:admin` | `shutdown_session` |

When auth is disabled (default), all scopes are granted implicitly — every kind passes the pre-check.

## Size Guard

All payloads are subject to a 16,000-character cap (measured on JSON-serialized payload). Oversized payloads are rejected before routing with a structured error.

## Standard Kinds

### `create_session`

Creates a new CAO session with a supervisor terminal.

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `agent_profile` | **yes** | string | Agent profile name (e.g. `"code_supervisor"`) |
| `provider` | no | string | Provider override (e.g. `"claude_code"`) |
| `session_name` | no | string | Custom session name |
| `working_directory` | no | string | Working directory for the session |
| `allowed_tools` | no | string | Tool restriction list |

**Routes to:** `POST /sessions`

```json
{
  "kind": "create_session",
  "payload": {
    "agent_profile": "code_supervisor",
    "provider": "kiro_cli",
    "session_name": "my-review"
  }
}
```

### `send_message`

Delivers a message to an agent's inbox (queued; delivered when receiver is IDLE).

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `terminal_id` or `receiver_id` | **yes** | string | Target terminal ID |
| `message` | **yes** | string | Message content |
| `sender_id` | no | string | Sender label (defaults to `"operator"`) |

**Routes to:** `POST /terminals/{id}/inbox/messages`

```json
{
  "kind": "send_message",
  "payload": {
    "terminal_id": "abc123",
    "message": "Review the PR and report findings."
  }
}
```

### `assign`

Sends a direct input message to an existing terminal (assign semantics).

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `terminal_id` | **yes** | string | Target terminal ID |
| `message` | **yes** | string | Task message |
| `sender_id` | no | string | Sender label (defaults to `"operator"`) |

**Routes to:** `POST /terminals/{id}/input` with `orchestration_type=assign`

```json
{
  "kind": "assign",
  "payload": {
    "terminal_id": "def456",
    "message": "Implement the pagination feature."
  }
}
```

## Lifecycle Kinds

### `interrupt`

Sends SIGINT (Ctrl-C) to the terminal's foreground process.

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `terminal_id` | **yes** | string | Target terminal ID |

**Routes to:** `POST /terminals/{id}/key` with `key="C-c"`

```json
{
  "kind": "interrupt",
  "payload": { "terminal_id": "abc123" }
}
```

### `pause` / `resume`

**Not yet supported.** These kinds are classified and scope-checked but have no corresponding Backplane routes. They return a structured `{"success": false, "error": "unsupported"}` response.

## Destructive Kinds

### `shutdown_session`

Kills an entire CAO session and all its terminals.

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `session_name` | **yes** | string | Session name to shut down |

**Routes to:** `DELETE /sessions/{name}`

```json
{
  "kind": "shutdown_session",
  "payload": { "session_name": "cao-my-review" }
}
```

## Error Responses

All kinds return a structured result. On failure:

```json
{
  "success": false,
  "kind": "send_message",
  "error": "missing required field: terminal_id"
}
```

Common error conditions:
- Unknown kind → `"unknown command kind: X"`
- Missing scope → `"scope cao:admin required"`
- Missing field → `"missing required field: X"`
- Payload too large → `"payload too large: N characters exceeds the 16000 limit"`
- Auth misconfiguration → actionable message about `CAO_AUTH_LOCAL_TOKEN`
- HTTP error from Backplane → forwarded detail string
