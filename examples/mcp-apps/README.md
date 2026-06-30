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

Point an MCP App-capable host (Claude Desktop, Cursor, VS Code Insiders, Goose)
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

## 4. (Optional) turn on authorization

```bash
export CAO_AUTH_JWKS_URI="https://your-idp/.well-known/jwks.json"
export CAO_AUTH_AUDIENCE="cao-api"
```

With an IdP configured, mutating endpoints require `cao:write`/`cao:admin`
(`delete_session` requires `cao:admin`); a read-only `cao:read` token gets `403`.
With no IdP set, the layer is off and the localhost posture is unchanged.
