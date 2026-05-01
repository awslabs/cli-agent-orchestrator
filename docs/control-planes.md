# Control Planes

CAO exposes several different ways to drive sessions and to observe what they do. They are not alternatives to each other — they occupy different positions on two axes: who is in charge, and which direction the traffic flows.

This guide explains what each surface is for, when to use it, and how they fit together.

## The four surfaces at a glance

| Surface | Direction | Who calls it | Transport | Typical use |
|---|---|---|---|---|
| **Web UI** | Inbound (outside → CAO) | Human in a browser | HTTP + WebSocket | Interactive management from the browser |
| **`cao session` CLI + [`cao-session-management`](../skills/cao-session-management/SKILL.md) skill** | Inbound | Human in a terminal, OR an external agent that can run shell commands | Shell → HTTP | Scripts, CI pipelines, headless jobs, agents that cannot speak MCP |
| **`cao-ops-mcp` server** | Inbound | Any external agent that speaks MCP | MCP (stdio) → HTTP | A primary agent managing CAO from inside its own chat loop |
| **Plugins** (e.g. `cao-discord`) | **Outbound** (CAO → outside) | `cao-server` itself, fire-and-forget | Python hooks → whatever the plugin chooses (webhook, log, metric) | Forwarding events to chat apps, observability, audit logging |

Separately, the in-session MCP server (`cao-mcp-server`) handles agent-to-agent orchestration *within* a CAO session. That is orthogonal to this document — see [MCP Server Tools and Orchestration Modes](../README.md#mcp-server-tools-and-orchestration-modes) in the README for `handoff` / `assign` / `send_message`.

## Inbound vs outbound

The first thing to internalise is that **Web UI, `cao session`, and `cao-ops-mcp` are all inbound** — they are ways to tell CAO what to do. **Plugins are outbound** — they are how CAO tells the outside world what it is doing.

So "should I use plugins or `cao-ops-mcp`?" is not the right question. The right question is:

- Who is initiating? → inbound surface.
- Does something need to know when CAO did something? → plugin.

A bidirectional bridge (e.g. a Telegram bot that lets Telegram users drive CAO and also streams CAO events back to the channel) is two components: one plugin for outbound, one inbound call path for commands.

## Inbound surfaces

All three inbound surfaces ultimately hit the same HTTP API at `localhost:9889`. They differ only in *how* a caller gets there.

### Web UI

Browser-based dashboard bundled with `cao-server`. See [Web UI](../README.md#web-ui) in the README.

- **Strength:** interactive, visual, no configuration for a human operator.
- **Weakness:** human only — no scripting, no agent access.

### `cao session` CLI and the `cao-session-management` skill

A set of `cao session <verb>` commands (`list`, `status`, `send`, plus `cao launch` / `cao shutdown`) wrapped into a [skill](../skills/cao-session-management/SKILL.md) so any agent that follows the SKILL.md format can drive CAO by running shell commands.

- **Strength:** universal — works from bash, Python, `subprocess`, any agent framework, any external tool that can run a command. Zero protocol requirements on the caller.
- **When to use:**
  - Scripting, CI pipelines, headless jobs.
  - An external AI assistant that does not speak MCP — e.g. [OpenClaw](https://www.digitalocean.com/resources/articles/what-is-openclaw), a custom Strands agent, a shell function.
  - Quick one-shots where spinning up an MCP client is overkill.

See [Session Management](../README.md#session-management) in the README for the command reference.

### `cao-ops-mcp` server

An MCP server that exposes the same set of management operations as structured tool calls. Add it to a primary agent's MCP configuration and that agent can call `launch_session`, `list_sessions`, `install_profile`, etc. as typed tools.

- **Strength:** structured tool calls instead of shell parsing. Typed arguments, typed results, errors surface as tool-call errors.
- **When to use:**
  - A primary agent (Claude Code, Claude Desktop, etc.) that already uses MCP should prefer this over shell.
  - Multi-step workflows where an agent benefits from tool-level discoverability.
- **When *not* to use:** if your caller cannot speak MCP or you are writing a shell script — use `cao session` instead.

See [CAO Ops MCP Server](../README.md#cao-ops-mcp-server) in the README for setup and the tool catalog.

### Choosing between `cao session` and `cao-ops-mcp`

| If your caller is… | Prefer |
|---|---|
| A human in a browser | Web UI |
| A shell script, cron job, CI step | `cao session` |
| An MCP-capable agent (Claude Code, Claude Desktop, etc.) | `cao-ops-mcp` |
| An AI assistant that can only run shell commands | `cao session` via the skill |
| A custom Python service polling CAO | Call the HTTP API at `localhost:9889` directly (see [docs/api.md](api.md)) |

They are functionally equivalent — both end up calling the same HTTP endpoints. The choice is purely ergonomic.

## Outbound surface: plugins

Plugins are Python packages loaded into `cao-server` at startup. They subscribe to lifecycle and message events via `@hook("<event_type>")` and react — typically by forwarding the event somewhere else.

- **Strength:** zero polling. The event is dispatched the moment it happens, with typed payload and direct DB access.
- **Constraint:** plugins today are **observer only**. They cannot block, modify, or reject CAO operations. See `docs/plugins.md` and the Plugins section in the README.

### Why plugins instead of polling

You could build a Discord bridge by polling `/terminals/{id}/inbox/messages` or tailing logs. Plugins exist because they sidestep that:

1. Events are dispatched from inside `cao-server`, so there is no polling lag or wasted calls.
2. Events are pydantic models, not JSON blobs to parse.
3. Plugins run in-process and can query the DB directly via `get_terminal_metadata()` — no HTTP round-trip.
4. Hook exceptions are caught and logged, so a broken plugin cannot take the server down.
5. Lifecycle is tied to `cao-server`, so there is no extra daemon to babysit.

### What plugins are commonly used for

- Forwarding inter-agent messages to chat apps (Discord, Slack, Telegram, Teams).
- Audit logging of session and terminal lifecycle.
- Metrics export (Prometheus, CloudWatch).
- Alerting on specific events (errors, long-running sessions).

### Authoring a plugin

- **Reference implementation:** [`examples/plugins/cao-discord/`](../examples/plugins/cao-discord/) — ~75 lines, forwards `post_send_message` events to a Discord webhook. The pattern is directly reusable for Slack, Telegram, or any webhook-style integration — swap the URL format and JSON payload shape.
- **Guided scaffolding:** the [`cao-plugin`](../skills/cao-plugin/SKILL.md) skill. Point any skill-aware agent at it and ask "create a CAO plugin for Telegram"; it will scaffold the package layout, entry-point, and hook registration and show you which events are available.
- **Installing and configuring:** see [docs/plugins.md](plugins.md).

## Putting it all together

A practical example: "I want a Telegram channel where my team can type `/cao launch …` and see the agents talk back."

That is three parts:

1. **Outbound:** a `cao-telegram` plugin that subscribes to `post_send_message` (and possibly session lifecycle events) and posts them into the channel.
2. **Inbound:** a Telegram bot process that listens for chat commands and translates them into calls against `cao-ops-mcp` or `cao session` (either works).
3. **Glue:** whatever mapping layer you like between Telegram user IDs and CAO session names.

Each component is small. The surface split keeps each one focused on a single direction.

## Related reading

- [Session Management](../README.md#session-management) in the README — command reference for `cao session` / `cao launch` / `cao shutdown`.
- [CAO Ops MCP Server](../README.md#cao-ops-mcp-server) in the README — setup and tool catalog for `cao-ops-mcp`.
- [docs/plugins.md](plugins.md) — plugin installation, event catalog, troubleshooting.
- [docs/api.md](api.md) — the underlying HTTP API that every inbound surface calls.
- [skills/cao-session-management/SKILL.md](../skills/cao-session-management/SKILL.md) — teach an agent to drive CAO via shell.
- [skills/cao-plugin/SKILL.md](../skills/cao-plugin/SKILL.md) — scaffold a new plugin.
