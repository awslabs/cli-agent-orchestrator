# Control Planes

CAO has four inbound management surfaces and one outbound extension surface.
The right choice depends on who initiates the action and which transport that
caller can use — and, for one of them, on whether the sessions being managed are
on this machine at all.

## Surfaces at a glance

| Surface | Direction | Caller | Transport | Best for |
|---|---|---|---|---|
| [Web UI](web-ui.md) | Inbound | Human operator | HTTP and WebSocket | Interactive browser management |
| `cao session` and the [session-management skill](../skills/cao-session-management/SKILL.md) | Inbound | Human, script, CI job, or shell-capable agent | Shell to HTTP | Portable automation and one-off commands |
| `cao-ops-mcp` | Inbound | External MCP-capable agent | MCP stdio to HTTP | Typed fleet-management tools |
| `cao fleet` and `cao worker` | Inbound | Human or script, from outside the cluster | Shell to HTTP, through a worker broker | Managing agents that run somewhere else |
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

All inbound surfaces ultimately use the HTTP API. See the
[API overview](api.md) for route families and generated OpenAPI for individual
HTTP operations. For every surface but one that API is the `cao-server` on this
machine; `cao fleet` and `cao worker` reach the same routes on a *remote*
`cao-server`, one per agent, through a broker.

## Web UI

The browser dashboard is bundled with `cao-server` and is the simplest surface
for interactively inspecting sessions and terminals. Its setup, remote-access,
and frontend-development details live in the [Web UI guide](web-ui.md).

## Shell CLI

Use `cao session` commands for scripts, CI, cron, or any caller that can execute
shell commands. `cao launch` creates sessions and `cao shutdown` removes them.
The canonical command reference and agent-facing procedure are in the
[session-management skill](../skills/cao-session-management/SKILL.md#commands).

## Remote fleets

`cao fleet` and `cao worker` manage agents that are not on this machine. Every
other surface here addresses the `cao-server` beside it; these two address a
cluster, where each agent has a `cao-server` of its own inside its own pod.

The distinction is worth keeping straight because two commands are one word
apart and neither can do the other's job. `cao shutdown` removes sessions on
this machine. `cao fleet shutdown` releases workers in a cluster, and the
sessions inside them go with the workers.

Two variables are the whole configuration:

```bash
export CAO_ELASTIC_BROKER_URL=http://127.0.0.1:9890   # a port-forward is enough
export CAO_ELASTIC_BROKER_TOKEN=...
cao fleet status
```

`cao fleet status` summarises the fleet and `cao fleet shutdown` empties it.
`cao worker list` names the workers, and the per-worker verbs — `status`, `send`,
`sessions`, `attach`, `logs`, `release` — are `cao session`'s verbs pointed at
one of them. Note that the broker's lease record is what `list` reads, so it
also answers the question a deleted pod cannot: why a worker is gone.

The cluster side is a reference implementation rather than part of CAO: the
commands speak four HTTP routes and know nothing about Kubernetes, so any
scheduler that serves those routes can be driven by them. See
[`examples/cao-clusters/kubernetes/eks/`](../examples/cao-clusters/kubernetes/eks/)
for the broker, the manifests, and the contract itself.

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

For a runnable walkthrough of the full lifecycle — discover, launch, poll for
readiness, follow up, read output, shut down — see
[`examples/ops-mcp/`](../examples/ops-mcp/). It also documents the boundary
between this external plane and in-session orchestration, and the readiness
polling that `launch_session` requires because it returns before the provider is
up.

Choose the surface by caller:

| Caller | Preferred surface |
|---|---|
| Human in a browser | Web UI |
| Shell script, CI step, or cron job | `cao session` |
| External MCP-capable agent | `cao-ops-mcp` |
| Agent that can execute shell but not MCP | `cao session` through the skill |
| Operator of agents running in a cluster | `cao fleet` and `cao worker` |
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
