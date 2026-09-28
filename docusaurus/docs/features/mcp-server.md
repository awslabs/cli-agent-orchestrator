---
sidebar_position: 2
---

# MCP Servers

CAO ships two MCP servers for different use cases, plus an MCP Apps surface for host-rendered fleet UI.

## Two Servers, Two Roles

| Server | Command | Who uses it | Purpose |
|--------|---------|-------------|---------|
| **cao-mcp-server** | `cao-mcp-server` | Agents **inside** a CAO session | Inter-agent orchestration (`handoff`, `assign`, `send_message`, `memory_store`, `memory_recall`, `memory_forget`, `load_skill`, `workflow_run`, `workflow_resume`, `workflow_return`) |
| **cao-ops-mcp** | `cao-ops-mcp-server` | A primary agent **outside** CAO | Meta-management (install profiles, launch/monitor/shutdown sessions) |

### cao-mcp-server (orchestration tools)

This is the MCP server that CAO injects into each agent's session. It provides the orchestration primitives agents use to coordinate.

**Tools exposed:**

- `handoff` -- synchronous delegation to a worker
- `assign` -- asynchronous fire-and-forget delegation
- `send_message` -- direct inbox delivery to another terminal
- `delete_terminal` -- clean up a finished worker
- `memory_store` / `memory_recall` / `memory_forget` -- persistent agent memory
- `load_skill` -- retrieve a skill's full markdown body at runtime
- `answer_user_prompt` -- respond to structured prompts (Hermes)
- `workflow_run` -- start a workflow execution
- `workflow_resume` -- resume a paused workflow
- `workflow_return` -- return a result from a workflow step
- `workflow_cancel` -- cancel a running workflow
- `emit_ui` -- emit a UI event to connected dashboard clients
- `find_profiles` -- search available agent profiles

Agents inside a CAO session receive this server automatically. The server identifies the caller by the `CAO_TERMINAL_ID` environment variable.

### cao-ops-mcp (management tools)

This server lets an external primary agent (running in Claude Code, Claude Desktop, or any MCP client) manage CAO sessions without being inside one.

**Tools exposed:**

- `list_profiles` -- browse available agent profiles
- `get_profile_details` -- inspect a profile's full prompt and metadata
- `install_profile` -- install a profile for a target provider
- `launch_session` -- start a new CAO session
- `send_session_message` -- deliver a prompt to a running terminal
- `get_terminal_status` -- poll a worker until it finishes
- `get_terminal_output` -- read a worker's result
- `get_session_info` / `list_sessions` -- monitor progress
- `shutdown_session` -- clean up when done

**Setup** -- add to your primary agent's MCP configuration (requires `cao-server` running at `localhost:9889`).

For Claude Code (`.mcp.json`):
```json
{
  "mcpServers": {
    "cao-ops-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/awslabs/cli-agent-orchestrator.git@main", "cao-ops-mcp-server"]
    }
  }
}
```

For other MCP clients, use the equivalent stdio command:
```
uvx --from git+https://github.com/awslabs/cli-agent-orchestrator.git@main cao-ops-mcp-server
```

**Typical workflow:**

```
list_profiles → install_profile → launch_session → send_session_message
→ get_terminal_status → get_terminal_output → shutdown_session
```

## MCP Apps -- Host-Rendered Fleet UI

Beyond the browser dashboard, CAO can render its fleet UI **inside any MCP App-capable host** (Claude Desktop, VS Code GitHub Copilot, ChatGPT, Goose, Postman, etc.) using the MCP Apps extension (SEP-1865).

### Views

| Surface | URI | Description |
|---------|-----|-------------|
| Dashboard | `ui://cao/dashboard` | Sessions, terminals, provider status, fleet overview |
| Agent detail | `ui://cao/agent` | One terminal: status, output tail, inbox depth, sub-agents |
| Event stream | `ui://cao/event-stream` | Compact governance ticker of normalized fleet events |
| Topology widget | `cao://widget/topology` | Vanilla-JS live event view (build-free fallback) |

### Enabling MCP Apps

MCP Apps is **default-off** and behavior-preserving. Enable it:

```bash
export CAO_MCP_APPS_ENABLED=true
cao-server                  # REST + SSE /events on :9889
cao-mcp-server              # registers MCP App tools/resources for your host
```

Or persist in settings.json:
```bash
cao config set apps.enabled true
```

### MCP App Tools

- `render_dashboard` / `render_agent_view` -- return snapshot HTML the host renders
- `cao_fetch_history` -- replay normalized events from the ring buffer
- `subscribe_events` -- returns the live SSE endpoint descriptor
- `submit_command` -- single mutation choke point (classifies, scope-checks, routes)

### Security

Optional OAuth 2.1 scopes (`cao:read` / `cao:write` / `cao:admin`) gate mutations when an IdP is configured. Without auth, the surface is localhost-only by default.
