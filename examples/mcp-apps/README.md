# Example: the MCP Apps fleet UI

A minimal walk-through of enabling CAO's host-rendered MCP Apps surface and
driving it. Full reference: [`docs/mcp-apps.md`](../../docs/mcp-apps.md).

## 1. Start CAO with the surface enabled

```bash
export CAO_MCP_APPS_ENABLED=true
uv run cao-server        # FastAPI + SSE /events on http://127.0.0.1:9889
uv run cao-mcp-server    # registers the MCP App tools/resources/widget
```

## 2. Confirm the surface is live

```bash
# Topology widget (build-free; works without the React bundles):
curl -s http://127.0.0.1:9889/widgets/topology/topology.html | grep -i topology

# Live event stream (Server-Sent Events):
curl -N http://127.0.0.1:9889/events

# Replay recent normalized events:
curl -s "http://127.0.0.1:9889/events/history?limit=20"
```

## 3. Use it from an MCP App host

Point an MCP App-capable host (Claude / Claude Desktop, ChatGPT, VS Code GitHub
Copilot, Microsoft 365 Copilot, Goose, Postman, MCPJam, Archestra.AI — see the
[client matrix](https://modelcontextprotocol.io/extensions/client-matrix))
at `cao-mcp-server`. The host discovers the `io.modelcontextprotocol/ui`
capability during `initialize` and offers the views:

- **Dashboard** (`ui://cao/dashboard`) — call `render_dashboard` to see sessions,
  terminals, and provider status, then act on the fleet.
- **Agent detail** (`ui://cao/agent`) — call `render_agent_view` for one terminal.
- **Event stream** (`ui://cao/event-stream`) — a live governance ticker.

All state changes go through the single `submit_command` choke point, e.g.:

```jsonc
// send a message to a terminal's inbox
{ "kind": "send_message", "payload": { "terminal_id": "<id>", "message": "re-run with -v" } }

// shut a session down (requires cao:admin when auth is enabled)
{ "kind": "shutdown_session", "payload": { "session_name": "cao-demo" } }
```

The views also use the spec's bidirectional channel beyond tool calls:

- **Host-delegated open-link** — when the host advertises the `openLinks`
  capability (returned in `McpUiInitializeResult.hostCapabilities`), the
  dashboard shows an **"Open full Web UI ↗"** button that calls `ui/open-link`
  to pop CAO's bundled browser UI (`http://127.0.0.1:9889`) out of the chat
  host. The sandbox forbids `window.open`, so this is delegated to the host.
- **Display modes** — the views declare `availableDisplayModes: ["inline",
  "fullscreen"]` in `ui/initialize` and can request a change via
  `ui/request-display-mode` (the host returns the actual resulting mode).
- **Streamed tool input** — the views register `ui/notifications/tool-input`
  (and tolerate `tool-input-partial`) so they can render as soon as the host
  streams the tool arguments, before the result arrives.
- **Silent model-context notes** — after a material gesture the view posts a
  body-free `ui/update-model-context` summary so the agent stays aware of
  operator actions without leaking message contents.

## 4. (Optional) turn on authorization

```bash
export CAO_AUTH_JWKS_URI="https://your-idp/.well-known/jwks.json"
export CAO_AUTH_AUDIENCE="cao-api"
```

With an IdP configured, mutating endpoints require `cao:write`/`cao:admin`
(`delete_session` requires `cao:admin`); a read-only `cao:read` token gets `403`.
With no IdP set, the layer is off and the localhost posture is unchanged.

## 5. How CAO maps onto the MCP Apps capability surface

This example *dogfoods* the extension end-to-end. CAO's surface against the
stable [2026-01-26 spec](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx):

| MCP Apps capability (spec) | CAO surface |
|---|---|
| `ui://` UI resources + `text/html;profile=mcp-app` | `ui://cao/dashboard`, `ui://cao/agent`, `ui://cao/event-stream` |
| Tool→UI linkage (`_meta.ui.resourceUri` + `visibility`) | `render_dashboard` / `render_agent_view` carry `_meta.ui` |
| Resource `_meta.ui` `csp` / `permissions` / `domain` / `prefersBorder` | structured loopback `csp`; **no elevated `permissions`** by design; per-view `domain`; `prefersBorder` |
| `ui/initialize` handshake + `appCapabilities.availableDisplayModes` | bridge handshake, declares `inline` + `fullscreen` |
| `tools/call` from the app | every read/poll + the `submit_command` choke point |
| `ui/open-link` (host-delegated) | "Open full Web UI ↗" → `http://127.0.0.1:9889` |
| `ui/request-display-mode` | `requestDisplayMode(...)` |
| `ui/update-model-context` | body-free gesture notes |
| `ui/notifications/tool-input` (+ `-partial`) / `tool-result` | live hydration + streamed-input handling |
| `ui/notifications/host-context-changed` / `resource-teardown` | theme/size merge + listener teardown |
| Capability negotiation (`io.modelcontextprotocol/ui`) | advertised on `initialize` (see below) |

CAO-specific (not spec) additions: `preferredFrameSize` (an additive sizing
hint — the spec sizes via `containerDimensions` + `ui/notifications/size-changed`)
and `requiredScopes` (read by the `submit_command` scope pre-check).

## 6. Capability negotiation

The surface is offered only after the host advertises support during
`initialize` via the standard
[extensions mechanism](https://modelcontextprotocol.io/extensions/overview#negotiation):

```jsonc
{ "method": "initialize", "params": { "capabilities": { "extensions": {
  "io.modelcontextprotocol/ui": { "mimeTypes": ["text/html;profile=mcp-app"] }
} } } }
```

Non-MCP-Apps hosts get text-only tool results (graceful degradation).

## 7. Build your own MCP App

CAO's views are hand-rolled (single-file, JIT-free) but follow the same spec.
To scaffold your own, see the [build guide](https://modelcontextprotocol.io/extensions/apps/build)
and the official [ext-apps Agent Skills](https://github.com/modelcontextprotocol/ext-apps)
(`create-mcp-app`, `migrate-oai-app`, `add-app-to-server`, `convert-web-app`),
or the [`@modelcontextprotocol/ext-apps`](https://www.npmjs.com/package/@modelcontextprotocol/ext-apps)
SDK (v1.7.4) with its [API reference](https://apps.extensions.modelcontextprotocol.io/api/index.html).

**Sources of truth:** [MCP Apps Overview](https://modelcontextprotocol.io/extensions/apps/overview) ·
[Build](https://modelcontextprotocol.io/extensions/apps/build) ·
[client matrix](https://modelcontextprotocol.io/extensions/client-matrix) ·
[stable spec 2026-01-26](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx) ·
[SEP-1865 PR #1865](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1865).
