# Control Planes

CAO has three inbound management surfaces and one outbound extension surface.
The right choice depends on who initiates the action and which transport that
caller can use.

## Surfaces at a glance

| Surface | Direction | Caller | Transport | Best for |
|---|---|---|---|---|
| [Web UI](web-ui.md) | Inbound | Human operator | HTTP and WebSocket | Interactive browser management |
| `cao session` and the [session-management skill](../skills/cao-session-management/SKILL.md) | Inbound | Human, script, CI job, or shell-capable agent | Shell to HTTP | Portable automation and one-off commands |
| `cao-ops-mcp` | Inbound | External MCP-capable agent | MCP stdio to HTTP | Typed fleet-management tools |
| [Plugins](plugins.md) | Outbound | `cao-server` | Python hooks to an external destination | Notifications, audit records, and observability |

These surfaces manage CAO from outside a session. The separate
`cao-mcp-server` is an orthogonal, in-session surface through which CAO agents
coordinate with tools such as `handoff`, `assign`, and `send_message`. See the
[core MCP tools](../skills/cao-supervisor-protocols/SKILL.md#core-mcp-tools)
and the guide to
[choosing between assign and handoff](../skills/cao-supervisor-protocols/SKILL.md#choosing-between-assign-and-handoff).

## Inbound and outbound traffic

The Web UI, shell CLI, and `cao-ops-mcp` send management requests into CAO.
Plugins receive events from CAO and send them outward. A bidirectional
integration therefore needs both an inbound command path and an outbound
plugin.

All inbound surfaces ultimately use the local HTTP API. See the
[API overview](api.md) for route families and generated OpenAPI for individual
HTTP operations.

## Web UI

The browser dashboard is bundled with `cao-server` and is the simplest surface
for interactively inspecting sessions and terminals. Its setup, remote-access,
and frontend-development details live in the [Web UI guide](web-ui.md).

## Shell CLI

Use `cao session` commands for scripts, CI, cron, or any caller that can execute
shell commands. `cao launch` creates sessions and `cao shutdown` removes them.
The canonical command reference and agent-facing procedure are in the
[session-management skill](../skills/cao-session-management/SKILL.md#commands).

## `cao-ops-mcp` server

`cao-ops-mcp` exposes operations such as profile installation, session launch,
and session inspection as typed MCP tools. Use it when a primary agent outside
CAO already speaks MCP and benefits from tool discovery and structured
arguments. The server forwards operations to a running `cao-server`; it does
not replace the in-session `cao-mcp-server`.

Start `cao-server` before the MCP server. By default, both use
`http://localhost:9889`; when CAO uses a custom endpoint, set `CAO_API_HOST` and
`CAO_API_PORT` in the MCP server environment to match it.

For Claude Code, add this stdio server to `.mcp.json`:

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

The current tools are grouped by purpose:

- Profiles: `list_profiles`, `get_profile_details`, `install_profile`
- Launch and messaging: `launch_session`, `send_session_message`
- Terminal inspection: `read_session_output`, `get_terminal_status`,
  `get_terminal_output`
- Session lifecycle: `list_sessions`, `get_session_info`, `shutdown_session`

MCP tool discovery is authoritative for clients. The declarations in
[`ops_mcp_server/server.py`](../src/cli_agent_orchestrator/ops_mcp_server/server.py)
are the source of truth when the server surface changes.

Choose the surface by caller:

| Caller | Preferred surface |
|---|---|
| Human in a browser | Web UI |
| Shell script, CI step, or cron job | `cao session` |
| External MCP-capable agent | `cao-ops-mcp` |
| Agent that can execute shell but not MCP | `cao session` through the skill |
| Custom application | [HTTP API](api.md) |

## Outbound plugins

Plugins are Python extensions loaded by `cao-server`. They subscribe to
lifecycle and message events and can forward those events to chat systems,
logs, metrics, or other destinations. They are event consumers, not an inbound
management protocol.

The [plugins guide](plugins.md) owns installation, supported events,
troubleshooting, and authoring. The
[`cao-plugin` skill](../skills/cao-plugin/SKILL.md) provides guided
scaffolding.

## Related reading

- [Web UI](web-ui.md)
- [HTTP API and PTY WebSocket](api.md)
- [Plugin guide](plugins.md)
- [Session-management commands](../skills/cao-session-management/SKILL.md#commands)
- [In-session MCP tools](../skills/cao-supervisor-protocols/SKILL.md#core-mcp-tools)
- [Assign and handoff selection](../skills/cao-supervisor-protocols/SKILL.md#choosing-between-assign-and-handoff)
