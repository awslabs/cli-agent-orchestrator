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

## Stable-Agent Roster

The roster is the fork-owned durable record of stable CAO agents, their
harness-native conversation lineages, and their disposable physical
incarnations.  The roster read surface is **read-only** — it never
mutates roster state — and the durable records are written by the launch
seams (managed-v2 `bind_native`, unmanaged terminal creation, admission
completion, teardown retirement) and by the **write-scoped**
native-identity repair route documented below.

The four read routes require at least the **read** authorization scope
(`cao:read`); write and admin scopes also satisfy them.  The repair
`POST` requires the **write** or **admin** scope.  Legacy, missing,
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

### POST /roster/terminals/{terminal_id}/native-identity-repair

**Dark/manual, write-scoped.**  Repair one currently live rostered
terminal's missing native session id from its provider's exact
panel-attested `/status`.  The operation proves the exact stored
pane/session/window/process identity is live and the provider composer
idle, types literal `/status` once under the canonical lifecycle claim
set and the per-pane input lease, parses only the pinned
provider/build identity fields, adopts an exclusive native-session
attachment owner, and commits the terminal row, the roster lineage, and
a bounded evidence digest atomically.  It is a manual health operation,
never an automatic migration, reincarnation, or Pause/Stop behavior.

Requires the **write** or **admin** authorization scope.

**Request body** (all fields are strings; `operation_id` is required and
must be a canonical lowercase UUID):

```json
{
  "operation_id": "00000000-0000-4000-8000-0000000000bb",
  "generation": "00000000-0000-4000-8000-000000000001",
  "provider_version": "2.1.226",
  "physical_occurrence": "00000000-0000-4000-8000-0000000000aa"
}
```

- `operation_id` — explicit canonical UUID bound to a server-derived
  digest of the immutable resolved request facts; an exact retry is
  idempotent, a changed request is a typed conflict.
- `generation` — expected model generation of a managed terminal; omit
  for legacy rows (which have none).
- `provider_version` — installed provider build that selects the
  interaction plan; the panel-attested build is what is recorded.
- `physical_occurrence` — the durable physical identity of the terminal
  (its callback-target generation for a legacy row, or its model
  generation for a managed row); required for legacy rows.

**Response:** the typed outcome is returned directly as the body
(not wrapped in the generic `{"detail": ...}` envelope — see the error
format note below):

```json
{
  "schema": "cao-native-status-repair-v1",
  "status": "repaired",
  "reason": null,
  "detail": null,
  "operation_id": "…",
  "request_digest": "…",
  "terminal_id": "…",
  "generation": "…",
  "native_session_id": "…",
  "evidence_sha256": "…",
  "parser_key": "…",
  "task_bytes_submitted": false
}
```

`status` is one of `repaired`, `already-known`, `identity-still-missing`
(Kimi, before its first session-creating action), `refused`, or
`errored`.  `repaired`, `already-known`, and `identity-still-missing`
map to **200**; `refused` maps by its `reason` (400 for invalid
input/unsupported build/missing generation or physical occurrence, 404
for a missing terminal or roster incarnation, 409 for identity/version/
attachment/binding conflicts, 503 for transient store or binding
failures); `errored` maps to **500**.  The body never contains raw pane
output, raw exceptions, or evidence from another operation.

The repair's `/status` observation is **at-most-once**: an exact
observation-attempt journal is written atomically at the byte seam, so
exactly one caller may ever type `/status` for a given operation id.
An exact retry adopts the committed evidence, adopts the journaled
`identity-still-missing` verdict, or returns a typed
`observation-attempt-ambiguous` refusal — it never sends `/status`
again.  A changed request under the same operation id is a typed
conflict before any pane I/O.

### GET /roster/legacy-audit

**Read-scoped** (requires `cao:read`; write/admin also satisfy it).
The truthful, strictly read-only live legacy audit (cond-0377D):
classifies every currently live terminal row as an eligible migration
candidate or a typed refusal.  It never types bytes, never initializes a
provider session, never reserves an attachment, never calls self-healing
metadata readers, and never persists an audit receipt.

**Response (200):**
```json
{
  "schema": "cao-m3-legacy-audit-v1",
  "occurrence_id": "…",
  "generated_at": "…",
  "terminals_total": 1,
  "eligible_count": 1,
  "refusals_count": 0,
  "candidates": [
    {
      "terminal_id": "…", "vintage": "legacy|v2", "managed": false,
      "generation": null, "physical_occurrence": "…",
      "provider": "claude_code", "session_name": "…",
      "pane_id": "…", "window_id": "…", "session_id": "…",
      "server_socket_path": "…", "pane_pid": 4242,
      "process_identity": { "pid": 4242, "start_marker": "…" },
      "pane_live": true, "server_live": true, "process_live": true,
      "agent_id": "…", "agent_role": "worker",
      "agent_profile_family": "developer",
      "lineage_id": "…", "incarnation_id": "…",
      "incarnation_disposition": "bound",
      "terminal_native_session_id": null,
      "lineage_native_session_id": null,
      "binding_native_session_id": null,
      "attachment_state": null,
      "attachment_owner_terminal_id": null,
      "attachment_owner_generation": null,
      "attachment_native_session_id": null,
      "session_probe_required": false,
      "build_provenance": {
        "source": "pinned-legacy-plan-fallback|managed-v2-binding",
        "observed": false, "provider_version": null, "plan_pin": "2.1.226"
      },
      "occurrence_id": "…",
      "classification": "eligible",
      "reason": null,
      "observed_at": "…",
      "evidence_digest": "…"
    }
  ]
}
```

- Liveness is never inferred from a DB row alone: an eligible candidate
  binds the DB lifecycle state, the exact stored/live tmux tuple, and PID
  start-marker equality.  Unreadable, dead, ambiguous, mismatched,
  corrupt, missing-occurrence, unsupported-provider, known-id, retired,
  conflicting-owner, and missing-agent shapes are explicit typed refusals
  (`classification` + closed `reason`).
- `session_probe_required` is `true` for a missing-ID Kimi candidate:
  its session existence is truthfully unknown before the bounded probe,
  and `/status` never creates a session.
- `build_provenance` states whether the provider build came from a
  durable managed-v2 binding (`observed: true`) or is an explicit
  unaudited pinned-legacy-plan fallback (`observed: false`) — a fallback
  is never described as observed build proof.
- `evidence_digest` is a canonical SHA-256 of the bounded candidate facts
  (including `build_provenance`); one later migration request binds it.

### POST /roster/legacy-migrations

**Write-scoped** (requires `cao:write` or `cao:admin`).  The explicit
opt-in one-candidate migration coordinator (cond-0377D): consumes
exactly one eligible audit candidate and invokes the exact
native-identity repair operation under a repair operation id derived
deterministically from the migration operation id.  The read-only audit
endpoint is not a migration switch; this route is the only producer, and
nothing runs automatically at launch.

**Request body:**
```json
{
  "operation_id": "00000000-0000-4000-8000-0000000000bb",
  "terminal_id": "a1b2c3d4",
  "provider": "claude_code",
  "generation": null,
  "physical_occurrence": "00000000-0000-4000-8000-0000000000aa",
  "provider_version": "2.1.226",
  "audit_occurrence_id": "00000000-0000-4000-8000-0000000000dd",
  "audit_candidate_digest": "d" * 64
}
```

- `operation_id` — the caller's stable canonical UUID; the durable
  intent row (with the audit digest and the deterministic repair
  operation id) is persisted before any repair interaction.
- `audit_occurrence_id` + `audit_candidate_digest` — the exact audit
  occurrence and candidate digest the caller observed; the candidate is
  revalidated from current facts (including the digest) before any repair
  interaction and again at the irreversible persistence seam.
- `provider_version` — plan selection only; the response's
  `build_provenance` carries the audit-derived build fact and is never
  presented as observed proof.

**Semantics — at-most-once and response loss:**
- The `pending -> attempt-started` transition is an atomic execution
  claim: exactly one caller may invoke the repair; every loser
  query-adopts or returns a typed `in-progress`/`unresolved` outcome with
  zero repair invocation.  Combined with the repair's observation-attempt
  journal, the total `/status` interaction count for one operation is
  exactly one.
- An exact duplicate retry query-adopts the same migration AND repair
  operations (identical `repair_operation_id`, identical evidence).
- A changed request under the same `operation_id` is a typed
  `operation-conflict` before any provider or roster effect.
- Response loss: committed repair evidence is the authoritative
  `migrated` truth (a pane that exits after the commit is a separate
  lifecycle fact and never downgrades it); a journaled
  `identity-still-missing` verdict is adopted without resending; a
  journaled observation without committed evidence is typed
  `repair-attempt-ambiguous`; a total absence is `repair-attempt-unresolved`.
- Never replays task input, never creates a fresh conversation, never
  reincarnates a pane, never changes task/role/profile, never
  auto-creates Kimi's lazy session, and never overwrites a known native
  id.  Additive history only; rollback
  (`CAO_LEGACY_MIGRATION_PRODUCER_ENABLED=0`) disables only new
  operations while prior rows remain queryable/adoptable.

**Response:** the typed outcome dict is returned as the body:
```json
{
  "schema": "cao-m3-legacy-migration-v1",
  "status": "migrated", "reason": null, "detail": null,
  "operation_id": "…", "request_digest": "…",
  "repair_operation_id": "…", "repair_status": "repaired",
  "repair_reason": null,
  "terminal_id": "…", "provider": "…",
  "generation": null, "physical_occurrence": "…",
  "provider_version": "2.1.226",
  "build_provenance": { "source": "pinned-legacy-plan-fallback",
                        "observed": false, "provider_version": null,
                        "plan_pin": "2.1.226" },
  "audit_occurrence_id": "…", "audit_candidate_digest": "…",
  "native_session_id": "…", "evidence_sha256": "…",
  "parser_key": "…", "attachment": { "…": "…" },
  "task_bytes_submitted": false
}
```

`status` is one of `migrated`, `already-known`, `identity-still-missing`
(Kimi pristine panel — no synthetic turn, no session fabricated), or
`refused`/`errored` with the same HTTP mapping rules as the repair route
(the refusal `reason` vocabulary adds `operation-conflict`,
`producer-disabled`, `candidate-drift`, `provider-drift`,
`generation-mismatch`, `occurrence-mismatch`, `seam-drift`,
`repair-attempt-ambiguous`, `repair-attempt-unresolved`, `in-progress`,
and `missing-agent`).

### GET /roster/provider-capabilities

**Read-scoped.**  The versioned truthful provider capability read
(cond-0377D): one cell per provider (`claude_code`, `codex`,
`kimi_cli`, `muse_cli`).  DeepSeek/Z.ai are Claude Code route provenance,
not separate harness identity domains.

**Response (200):**
```json
{
  "schema": "cao-m3-provider-capabilities-v1",
  "generated_at": "…",
  "route_provenance_note": "deepseek and zai run Claude Code with route provenance; …",
  "providers": [
    {
      "provider": "claude_code",
      "harness_domain": "claude_code",
      "route_provenance_domains": ["deepseek", "zai"],
      "build_identity": {
        "installed_build": {
          "banner": "Claude Code 2.1.226",
          "normalized": "2.1.226",
          "sha256": "…",
          "executable_path": "/opt/claude-code/claude"
        },
        "installed_build_source": "canary-receipt"
      },
      "parser_support": {
        "code_supported": true, "parser_key": "claude-modal-v1",
        "capability_schema": "cao-native-status-repair-v1",
        "supported_builds": ["2.1.226"], "escape": true
      },
      "status_observation_repair_code_supported": true,
      "canary": {
        "present": true, "state": "matching|stale|failed|absent",
        "build": "2.1.226", "operation_id": "…",
        "migration_operation_id": "…",
        "evidence_sha256": "…", "native_session_id": "…",
        "status_action_count": 1, "parser_key": "claude-modal-v1",
        "attachment_outcome": "attached", "recorded_at": "…"
      },
      "installed_live_repair_proven": true,
      "cell_state": "enabled",
      "reason": "…"
    }
  ]
}
```

- `cell_state` is `enabled`, `disabled`, `unsupported`, `unavailable`, or
  `unresolved` with a closed `reason`.  A cell is `enabled` ONLY with
  code support AND an installed live canary receipt DERIVED from the
  actual committed records (migration operation/request, deterministic
  repair operation, observation-attempt journal with exactly one status
  action, repair evidence/request/evidence digest, provider/parser/plan,
  native identity, attachment/adoption outcome) AND a **read-time
  revalidation**: on every read the current canonical executable is
  re-hashed and its bounded `--version` banner re-observed, and both must
  exactly match the receipt (path, full banner including Muse `R`
  revision, normalized build, and service-computed SHA-256).  A deleted,
  replaced, or drifted executable is a typed stale/disabled cell — never
  green, never an exception.  Static parser support appears only under
  `parser_support.supported_builds` (there is no static
  `build_identity.durable_builds` field); `build_identity.installed_build`
  is null without an observed canary and carries the canonical
  `executable_path` when present.
- `build_identity.installed_build` is OBSERVED at receipt time, never
  caller-asserted: the record seam accepts a canonical absolute existing
  executable path, computes the SHA-256 of that exact file itself, and
  runs the bounded provider `--version` probe against it in the bounded
  child environment, retaining the complete banner — Muse's
  `0.1.0-R708.1` revision is never normalized away, and a future
  same-semver build with a different digest never inherits the proven
  build's identity.  The canonical path, banner, and observed digest are
  bound into the receipt's request digest: a changed path/banner/digest
  under the same canary id conflicts, and a build or file change before
  persistence refuses and leaves the cell non-green.  If a truthful exact
  executable cannot be resolved or rechecked, the cell stays
  absent/disabled/unresolved.
- Kimi without a session stays `unresolved`/`disabled` and receives no
  synthetic turn.


### GET /roster/pane-identity

**Read-scoped** (requires `cao:read`; write/admin also satisfy it).  The
bounded exact-live-pane identity resolution (cond-0377D M3-A read seam):
resolves one exact live tmux pane to its registered CAO terminal, unique
LIVE stable-agent incarnation, and stable agent/lineage identity — the
fork primitive the conductor's `conduct whoami` will consume.

**Query parameters** (the only two accepted; anything else is ignored):
- `pane_id` — the immutable tmux pane id.
- `server_socket_path` — the canonical identity of the tmux server the
  caller observed the pane on.

The service **re-observes** the pane (`pane_control_identity`) and its
server (`observe_pane_server_identity`) through the bounded tmux seams
and binds the canonical server identity plus the immutable
`pane_id`/`window_id`/`session_id` and positive `pane_pid` — never
session/window names.  A caller-supplied `TMUX_PANE`, `TMUX`,
`CAO_TERMINAL_ID`, window name, or terminal id can never override the
pane mapping, because the request accepts none of them.

**Response (200):**
```json
{
  "schema": "cao-m3-pane-identity-resolution-v1",
  "status": "resolved",
  "reason": null,
  "observed_at": "…",
  "pane": {
    "pane_id": "%7", "window_id": "@7", "session_id": "$1",
    "pane_pid": 4242, "server_socket_path": "/private/tmp/cao-native.sock"
  },
  "terminal": {
    "terminal_id": "a1b2c3d4", "generation": null,
    "physical_occurrence": "00000000-0000-4000-8000-0000000000aa",
    "vintage": "legacy"
  },
  "incarnation": {
    "incarnation_id": "…", "disposition": "bound", "lineage_id": "…"
  },
  "agent": {
    "agent_id": "…", "lineage_id": "…", "harness": "claude_code",
    "native_session_id": "…", "disposition": "live"
  }
}
```

`status` is `resolved` or one of the typed non-identity reasons
(all 200 — absence and ambiguity are normal typed answers, never guessed
identities; `pane`/`terminal`/`incarnation`/`agent` are `null` for
non-identity):

- `pane-unreadable-or-dead` — the pane is not provably live on exactly
  the caller's canonical server (dead, unreadable, or on a different
  server: identical pane ids on two isolated servers never cross-resolve).
- `pane-unregistered` — live and on the right server, but no terminal row
  claims this pane id on this server.
- `terminal-pane-mismatch-or-superseded` — the registered row's exact
  tuple no longer matches (pane id reused with a changed
  window/session/pid), the row is superseded/dead, or multiple rows claim
  the exact tuple.
- `roster-incarnation-missing` — no roster incarnation, or the
  incarnation is retired/non-live: a stopped/dead or superseded
  incarnation cannot resolve even if historical records remain.
- `roster-incarnation-ambiguous-or-invalid` — two live incarnations share
  the terminal (ambiguous; never picks the first), the incarnation's
  pane/pid or the agent's current pointers disagree, or the roster/agent
  store is unreadable.

The lookup is **byte-for-byte read-only** (no terminal liveness, roster,
attachment, or journal mutation; no write lease is taken to perform a
read), every tmux subprocess is bounded through the existing timeout
mechanism, and the trust boundary is explicitly **cooperative-local
routing, not a security gate**: it does not authenticate the human or
process that asked the question, and it issues no credentials, tokens, or
approvals.


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

**Exception:** the write-scoped
`POST /roster/terminals/{terminal_id}/native-identity-repair` route
returns its **typed outcome dict as the body** for every outcome —
including refusals and errors — instead of the generic `{"detail": ...}`
envelope, so a caller branches on the typed `status`/`reason` fields
without re-deciding what happened.  It never leaks raw pane output, raw
exceptions, or evidence from another operation.

---
