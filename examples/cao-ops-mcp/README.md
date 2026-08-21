# Example: the cao-ops-mcp external control plane

A walk-through of managing a CAO fleet from *outside* any CAO session —
discovering profiles, launching a worker, watching it work, following up, and
tearing it down — using the `cao-ops-mcp` server's typed tools instead of the
Web UI or the `cao session` CLI. Full reference:
[`docs/control-planes.md`](../../docs/control-planes.md). The HTTP surface
`cao-ops-mcp` forwards every call to is documented in
[`docs/api.md`](../../docs/api.md).

## 1. Start `cao-server` and configure the MCP client

```bash
uv run cao-server        # FastAPI HTTP API on http://127.0.0.1:9889
```

`cao-ops-mcp` is a separate stdio MCP server that forwards every tool call to
that HTTP API — it runs no agents itself, so `cao-server` must already be up.
For Claude Code, add it to `.mcp.json`:

```json
{
  "mcpServers": {
    "cao-ops-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/awslabs/cli-agent-orchestrator.git@main",
        "cao-ops-mcp-server"
      ]
    }
  }
}
```

For other MCP clients, configure the equivalent stdio command:

```bash
uvx --from git+https://github.com/awslabs/cli-agent-orchestrator.git@main cao-ops-mcp-server
```

If `cao-server` isn't reachable at the default `127.0.0.1:9889`, set
`CAO_API_HOST` and `CAO_API_PORT` in the MCP server's own environment —
`cao-ops-mcp` builds every request URL from those two variables.

The server also publishes its own quick-start as MCP `instructions` metadata,
which most MCP clients surface to the model automatically:

```text
# CAO Operations MCP Server

Manage CLI Agent Orchestrator profiles and sessions from outside a CAO session.
Requires the CAO API server running at localhost:9889.

## Typical Workflow
1. list_profiles to inspect available profiles
2. get_profile_details to review a profile's full prompt and metadata
3. install_profile to install a profile for a target provider
4. launch_session to start a new CAO session
5. send_session_message to deliver a prompt to a running terminal
6. get_terminal_status to poll a worker until it finishes a task
7. get_terminal_output to read a worker's result (or review its files/git diff)
8. read_session_output to read a terminal's captured output by session name
9. get_session_info or list_sessions to monitor overall progress
10. shutdown_session to clean up when done
```

`cao-ops-mcp` exposes eleven tools in total. This example walks nine of them
end to end — everything except `install_profile` and `list_sessions`, both
covered briefly in the closing notes below.

## 2. Discover profiles

Call `list_profiles` with no arguments (→ HTTP `GET /agents/profiles`):

**Call:**

```json
{}
```

**Result:**

```json
{
  "success": true,
  "profiles": [
    {
      "name": "developer",
      "source": "built-in",
      "loadable": true,
      "description": "Developer Agent in a multi-agent system",
      "capabilities": [],
      "tags": [],
      "role": "developer",
      "duplicated_in": []
    },
    {
      "name": "reviewer",
      "source": "built-in",
      "loadable": true,
      "description": "Code Reviewer Agent in a multi-agent system",
      "capabilities": [],
      "tags": [],
      "role": "reviewer",
      "duplicated_in": []
    }
  ]
}
```

(Plus any profiles installed locally or discovered under a provider's agent
directory — `source` reflects where each one was found, and `duplicated_in`
flags a name that's shadowed in more than one directory.)

Then inspect one in full with `get_profile_details` (→ HTTP
`GET /agents/profiles/{name}`):

**Call:**

```json
{ "name": "developer" }
```

**Result:**

```json
{
  "name": "developer",
  "description": "Developer Agent in a multi-agent system",
  "system_prompt": "# DEVELOPER AGENT\n\n## Role and Identity\nYou are the Developer Agent in a multi-agent system...",
  "role": "developer",
  "mcpServers": {
    "cao-mcp-server": {
      "type": "stdio",
      "command": "cao-mcp-server",
      "args": []
    }
  }
}
```

This is the full `AgentProfile` model with every unset field (`provider`,
`allowedTools`, `model`, ...) dropped; `system_prompt` is the profile's entire
markdown body, truncated above for brevity.

## 3. Launch a session

`launch_session` needs only `agent_profile` — `provider`, `session_name`,
`working_directory`, and `allowed_tools` are all optional. Passing `provider`
explicitly avoids relying on the profile's own default:

**Call:**

```json
{ "agent_profile": "developer", "provider": "claude_code" }
```

**Result** (→ HTTP `POST /sessions`):

```json
{
  "success": true,
  "message": "Session 'cao-a1c9e274' launched successfully",
  "session_name": "cao-a1c9e274",
  "terminal_id": "3f9a7b2c"
}
```

It returns as soon as the terminal is created — it does not wait for the
provider CLI to finish initializing. Save `session_name` and `terminal_id`;
every later call in this walkthrough uses one or the other.

## 4. Inspect the session until it's idle

Right after launch, `get_session_info` (→ HTTP
`GET /sessions/{session_name}`) shows the terminal still starting up:

**Call:**

```json
{ "session_name": "cao-a1c9e274" }
```

**Result:**

```json
{
  "session": {
    "id": "cao-a1c9e274",
    "name": "cao-a1c9e274",
    "status": "detached"
  },
  "terminals": [
    {
      "id": "3f9a7b2c",
      "tmux_session": "cao-a1c9e274",
      "tmux_window": "developer-4d2e",
      "provider": "claude_code",
      "agent_profile": "developer",
      "last_active": "2026-08-14T18:32:05.123456",
      "status": "processing"
    }
  ]
}
```

Watch the two `status` fields — they mean different things. `session.status`
is tmux client attachment (`active`/`detached`), not agent progress.
`terminals[].status` is the one to poll; it's the same `TerminalStatus` value
`get_terminal_status` returns: `unknown`, `idle`, `processing`, `completed`,
`waiting_user_answer`, or `error`.

Once you have `terminal_id`, `get_terminal_status` (→ HTTP
`GET /terminals/{terminal_id}`) is the lighter-weight poll. A few seconds
later, once the provider has finished initializing:

**Call:**

```json
{ "terminal_id": "3f9a7b2c" }
```

**Result:**

```json
{
  "id": "3f9a7b2c",
  "name": "developer-4d2e",
  "provider": "claude_code",
  "session_name": "cao-a1c9e274",
  "agent_profile": "developer",
  "caller_id": null,
  "allowed_tools": null,
  "shell_command": null,
  "status": "idle",
  "last_active": "2026-08-14T18:32:11.654321"
}
```

Poll on an interval until `status` reaches `"idle"` before sending it work.

## 5. Send a follow-up instruction

`send_session_message` (→ HTTP
`POST /terminals/{terminal_id}/inbox/messages`) queues a message for delivery
through the inbox service:

**Call:**

```json
{
  "terminal_id": "3f9a7b2c",
  "message": "Add unit tests for the new parser and re-run the suite."
}
```

**Result:**

```json
{
  "success": true,
  "message": "Message queued for terminal '3f9a7b2c'",
  "terminal_id": "3f9a7b2c"
}
```

Delivery isn't immediate: the inbox service delivers once the terminal is
next `idle`. Poll `get_terminal_status` the same way as step 4 — first back
to `processing` while the agent works the new instruction, then to
`completed` or `idle` again when it's done.

## 6. Read the output

Two ways to read it back, with different defaults. `read_session_output`
(→ HTTP `GET /terminals/{terminal_id}/output`, after resolving `session_name`
to a terminal via `GET /sessions/{session_name}`) defaults to the
deterministic raw scrollback buffer:

**Call:**

```json
{ "session_name": "cao-a1c9e274" }
```

**Result:**

```json
{
  "success": true,
  "terminal_id": "3f9a7b2c",
  "mode": "full",
  "output": "Starting developer agent...\nAdded tests in test_parser.py; suite green (42 passed).\n",
  "truncated": false,
  "total_chars": 84
}
```

`session_name` only resolves when the session has exactly one terminal; a
multi-terminal session gets back `{"success": false, ..., "terminals": [...]}`
and you supply `terminal_id` explicitly instead.

`get_terminal_output` (→ HTTP `GET /terminals/{terminal_id}/output`) defaults
instead to the provider-extracted final message — handy for a one-line status
but flakier on redraw-heavy TUIs:

**Call:**

```json
{ "terminal_id": "3f9a7b2c" }
```

**Result:**

```json
{
  "output": "Added tests in test_parser.py; suite green (42 passed).",
  "mode": "last"
}
```

For code review, prefer reading the worker's files or git diff directly over
parsing either form of terminal text.

## 7. Shut down

`shutdown_session` (→ HTTP `DELETE /sessions/{session_name}`) exits the
providers, kills the tmux session, and removes the database records:

**Call:**

```json
{ "session_name": "cao-a1c9e274" }
```

**Result:**

```json
{
  "success": true,
  "deleted": ["cao-a1c9e274"],
  "errors": []
}
```

## 8. Other tools

Two tools aren't part of this walkthrough. `install_profile` installs a
profile — by name or by an allow-listed `https://` URL — for a target
provider before you `launch_session` with it; see its source-resolution and
provider-config rules in
[`ops_mcp_server/server.py`](../../src/cli_agent_orchestrator/ops_mcp_server/server.py).
`list_sessions` lists every active session at once, for a fleet-wide view
instead of one session at a time.

**Related reading:** [Control Planes](../../docs/control-planes.md) ·
[API Overview](../../docs/api.md) ·
[`ops_mcp_server/server.py`](../../src/cli_agent_orchestrator/ops_mcp_server/server.py)
(source of truth for tool signatures) ·
[session-management skill](../../skills/cao-session-management/SKILL.md) (the
shell-CLI alternative to this MCP surface).
