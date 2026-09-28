# CLI Agent Orchestrator Codebase

This guide maps CAO's principal runtime surfaces and implementation areas for
contributors. First-time setup belongs in [README.md](README.md); focused
operator contracts remain in their owning documents.

## Runtime surfaces

CAO is a local client-server application with several entry points:

- `cao` is the Click CLI in `src/cli_agent_orchestrator/cli/`.
- `cao-server` is the FastAPI application in
  `src/cli_agent_orchestrator/api/main.py`.
- `cao-mcp-server` exposes in-session supervisor/worker tools from
  `src/cli_agent_orchestrator/mcp_server/`.
- `cao-bridge` is the execution-only runtime entrypoint in
  `src/cli_agent_orchestrator/runtime_channel/bridge.py`, launched with
  `CAO_NODE_MODE=bridge`. It has no HTTP API, scheduler, or MCP host of its own;
  it dials one persistent outbound WebSocket to the central server's
  `WS /runtime/channel` and runs providers beside its own tmux.
- `cao-mcp-stdio-bridge` in
  `src/cli_agent_orchestrator/mcp_server/stdio_bridge.py` forwards a stdio-only
  provider's MCP calls to the shared HTTP MCP endpoint, carrying the caller
  identity in a header without letting the agent choose it.
- `cao-ops-mcp-server` exposes external fleet-management tools from
  `src/cli_agent_orchestrator/ops_mcp_server/`.
- The bundled browser client is built from `web/`; MCP App views are built
  from `cao_mcp_apps/` and served through `ext_apps/` and the built-in plugin.

Console-script and plugin entry points are declared in `pyproject.toml`.
[Control Planes](docs/control-planes.md) explains which operator-facing surface
to use.

## Package map

| Path | Ownership |
|---|---|
| `src/cli_agent_orchestrator/api/` | FastAPI HTTP, SSE, AG-UI, and PTY WebSocket endpoints |
| `src/cli_agent_orchestrator/runtime_channel/` | Remote execution boundary (#745/#776): the `WS /runtime/channel` endpoint and `/runtimes*` routes (`api.py`), the `cao-bridge` execution-runtime entrypoint (`bridge.py`), the terminal→runtime routing registry (`registry.py`), the frame protocol and codec (`protocol.py`), and the bounded per-stream output replay buffer (`replay_buffer.py`) |
| `src/cli_agent_orchestrator/cli/` | `cao` commands and command-line validation |
| `src/cli_agent_orchestrator/mcp_server/` | In-session orchestration MCP tools |
| `src/cli_agent_orchestrator/ops_mcp_server/` | External operations MCP tools |
| `src/cli_agent_orchestrator/services/` | Session, terminal, inbox, workflow, memory, event, configuration, and plugin services |
| `src/cli_agent_orchestrator/backends/` | Terminal-backend abstraction and tmux/herdr implementations |
| `src/cli_agent_orchestrator/clients/` | SQLite and tmux clients used by services and backends |
| `src/cli_agent_orchestrator/providers/` | Provider adapters for interactive agent CLIs |
| `src/cli_agent_orchestrator/models/` | Pydantic domain and API models |
| `src/cli_agent_orchestrator/schemas/` | Shipped schemas, including agent-profile validation |
| `src/cli_agent_orchestrator/utils/` | Profile, skill, path, tool, and terminal helpers |
| `src/cli_agent_orchestrator/agent_store/` | Packaged agent profiles |
| `src/cli_agent_orchestrator/skills/` | Skills packaged with the Python distribution |
| `src/cli_agent_orchestrator/plugins/` | Event-plugin API, event definitions, discovery, and built-ins |
| `src/cli_agent_orchestrator/agent_plugins/` | Agent Plugins 1.0.0 support: manifest validation, containment, source resolution, install store, skill projection, and MCP mapping/delivery |
| `src/cli_agent_orchestrator/security/` | Authentication and authorization helpers |
| `src/cli_agent_orchestrator/telemetry/` | OpenTelemetry spans, metrics, context, and semantic conventions |
| `src/cli_agent_orchestrator/graph/` | Graph providers, cache, models, and export sinks |
| `src/cli_agent_orchestrator/ext_apps/` | MCP App resources and built-in topology assets |
| `src/cli_agent_orchestrator/templates/` | Agent-scaffolding templates and schemas |
| `src/cao_workflow/` | Standalone workflow authoring/runtime package shipped by the project |
| `web/` | React browser UI source and build configuration |
| `cao_mcp_apps/` | MCP App React views and build tooling |
| `agent-plugin/` | CAO's own Agent Plugins 1.0.0 packages (`cao`, `cao-contributor`), generated and checked by `make check-agent-plugin` |
| `test/` | Unit, API, CLI, documentation, and integration tests |

## Request and service flow

The CLI, Web UI, and both MCP servers adapt user or agent actions into requests
handled by `api/main.py`. Route handlers validate transport-level input and
delegate stateful work to modules under `services/`.

Session and terminal creation generally follows this path:

```text
CLI, Web UI, or MCP client
  -> FastAPI route
  -> session/terminal service
  -> terminal backend
  -> provider adapter
  -> provider CLI process
```

The terminal backend owns terminal lifecycle and PTY interaction. The provider
adapter owns provider-specific launch commands, initialization, status
detection, input handling, and exit behavior. Keep those responsibilities
separate when adding a backend or provider.

## Providers and terminal backends

Provider classes under `providers/` adapt supported coding CLIs to a common
interface in `providers/base.py`; `providers/manager.py` creates and tracks
instances. Public provider selection is modeled in
`models/provider.py`. The mock adapter is credentials-free test infrastructure,
not a public provider recommendation.

Terminal mechanics are abstracted by `backends/base.py` and selected through
`backends/factory.py` and `backends/registry.py`. The tmux implementation uses
`clients/tmux.py`; the herdr implementation integrates its own terminal
backend. Provider logic should not assume a concrete backend when the base
contract supplies the operation.

## Persistence and event flow

`clients/database.py` owns SQLite access for sessions, terminals, inbox
messages, and related state. Paths and defaults are centralized in
`constants.py`; unified runtime configuration is resolved by
`services/config_service.py` and `services/settings_service.py`.

Terminal output and status changes flow through services such as
`fifo_reader.py`, `status_monitor.py`, `event_bus.py`, `sse_bus.py`, and
`inbox_service.py`. Inbox delivery combines immediate delivery with
event-driven and reconciliation paths. Focused behavior is documented in
[Event-Driven Architecture](docs/event-driven-architecture.md) and
[Inbox Delivery](docs/inbox-delivery.md).

Workflow specifications and runs are handled by the workflow services and the
separate `src/cao_workflow/` package. Scheduled flows are handled by
`services/flow_service.py`. Memory, archive, wiki, and graph modules own their
respective persistence and projection behavior.

## Remote execution boundary

CAO runs in two topologies from the same image. In the default local topology
every operation is co-located: the API, scheduler, state, and tmux all live in
one `cao-server`. In the remote topology one central `cao-server` (API,
scheduler, SQLite state) coordinates N execution runtimes running `cao-bridge`,
each holding one persistent **outbound** WebSocket to `WS /runtime/channel`.
Anything that touches the tmux socket, the provider process, its PTY, or its
config files runs in the runtime; the server relays interactive traffic,
keeps routing and state, and republishes runtime events onto the existing bus
for consumers that only depend on the bus topic contract.

`runtime_channel/registry.py` maps each terminal to its live channel and
persists the association so routing survives a server restart — rebuilt from
each runtime's reconnect (`hello`) snapshot. A request for one terminal can
never be served by another runtime: inbound channel frames go through an
ownership-checked claim rather than an unconditional bind.

`clients/database.py` gains a **dispatch journal** (`dispatch_journal` table)
that records each remote command with the owner it was made for, so a `LAUNCH`
or `RUN_SCRIPT` result redelivered after a reconnect is reconciled with its
original owner instead of orphaned. It settles operations whose outcome is
known and leaves unknown-outcome operations unsettled for redelivery. The
`flows.owner` and `terminals.owner` columns record the principal that deferred
work belongs to (see [design.md](docs/issues/745-remote-execution-boundary/design.md)).

Shared MCP hosting (`mcp_server/http_hosting.py`, selected by
`CAO_MCP_TRANSPORT=http`) serves the in-session tools over HTTP and resolves the
caller's terminal identity per authenticated request, replacing the
process-global `CAO_TERMINAL_ID`. A stdio-only provider reaches the same
endpoint through the `cao-mcp-stdio-bridge` forwarding shim
(`mcp_server/stdio_bridge.py`), which registers no tools and holds no state.

The full contract lives in
[design.md](docs/issues/745-remote-execution-boundary/design.md); the route
families are in [docs/api.md](docs/api.md); the deployed topology is the
[EKS example](examples/cao-clusters/kubernetes/eks/README.md).

## Plugins, security, and telemetry

`plugins/base.py` and `plugins/events.py` define the extension contract;
`plugins/registry.py` discovers entry points from the `cao.plugins` group.
Built-in integrations live under `plugins/builtin/`. See
[Event Plugins](docs/plugins.md) before changing hook behavior.

Authentication and authorization helpers live under `security/`, while
network allowlists and server defaults are defined through configuration and
constants consumed by the API. Public deployment guidance belongs in
[SECURITY.md](SECURITY.md) and [Configuration](docs/configuration.md).

Telemetry is optional and isolated under `telemetry/`. The base package remains
usable without OpenTelemetry extras; exporters and deployment guidance are in
[OpenTelemetry Collector Deployment](docs/otel-deployment.md).

## Frontend and build components

`web/` contains the browser UI source, tests, and Vite build. Built assets are
packaged for `cao-server`; operators do not need a frontend toolchain for a
normal installation. See [web/README.md](web/README.md) for development.

`cao_mcp_apps/` contains the MCP App views and their build. Python resources
under `ext_apps/` and `plugins/builtin/mcp_apps.py` connect those assets to the
MCP surface. See [MCP Apps](docs/mcp-apps.md) and
[cao_mcp_apps/README.md](cao_mcp_apps/README.md).

Packaging, console scripts, optional dependencies, package inclusion, and
plugin entry points are controlled by `pyproject.toml`.

## Documentation maintenance

Focused documents are canonical for their subject; summaries should link to
them instead of duplicating detailed contracts. In particular:

- First-run sequence and top-level navigation belong in `README.md`.
- Architecture, package ownership, and service flow belong in `CODEBASE.md`.
- Control-plane selection belongs in `docs/control-planes.md`.
- Profile fields and provider precedence belong in `docs/agent-profile.md`.
- HTTP route-family orientation and the PTY WebSocket contract belong in
  `docs/api.md`.

Any change to public commands, providers, profile fields, API route families,
or Markdown headings must update the relevant canonical document and every
affected link in the same change. Verify technical claims against CLI help,
schemas, source, or package metadata, and validate local paths and fragments
before merging.

Use [DEVELOPMENT.md](DEVELOPMENT.md) for repository setup and required test
commands.
