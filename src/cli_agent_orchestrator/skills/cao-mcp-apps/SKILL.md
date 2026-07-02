---
name: cao-mcp-apps
description: Enable, operate, and extend CAO's MCP Apps surface — the sandboxed host-rendered fleet UI (SEP-1865) with the ui://cao/* views, the topology widget, the submit_command mutation choke point, SEP-2133 capability advertisement, and the default-off OAuth scope layer. Use whenever the user wants to turn on the MCP Apps UI, observe/steer a CAO fleet from inside an MCP App host (Claude / Claude Desktop, ChatGPT, VS Code GitHub Copilot, Microsoft 365 Copilot, Goose, Postman, MCPJam, Archestra.AI), debug why the views don't render, build the frontend bundles, or extend the surface (new view, tool, or command kind).
---

# CAO MCP Apps

Operator + developer playbook for CAO's host-rendered fleet UI. Reference docs:
[`docs/mcp-apps.md`](../../docs/mcp-apps.md); example: [`examples/mcp-apps/`](../../examples/mcp-apps/).

**Authoritative spec & sources of truth:**
[MCP Apps Overview](https://modelcontextprotocol.io/extensions/apps/overview) ·
[Build an MCP App](https://modelcontextprotocol.io/extensions/apps/build) ·
[capability negotiation](https://modelcontextprotocol.io/extensions/overview#negotiation) ·
[client matrix](https://modelcontextprotocol.io/extensions/client-matrix) ·
stable spec [`2026-01-26/apps.mdx`](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)
(SEP-1865, Status: Stable) ·
SDK [`@modelcontextprotocol/ext-apps`](https://www.npmjs.com/package/@modelcontextprotocol/ext-apps) v1.7.4
([API ref](https://apps.extensions.modelcontextprotocol.io/api/index.html) ·
[repo](https://github.com/modelcontextprotocol/ext-apps)) ·
provenance [PR #1865](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1865).

## Turn it on

The surface is **default-off**. Enable and run:

```bash
export CAO_MCP_APPS_ENABLED=true
uv run cao-server        # :9889 (REST + SSE /events)
uv run cao-mcp-server    # registers tools/resources via the mcp_apps plugin
```

It is packaged as the built-in `mcp_apps` plugin (`cao.plugins` entry-point). The
plugin's `on_mcp_server` hook registers the `ui://cao/*` resources, the five app
tools, the topology widget, and advertises the `io.modelcontextprotocol/ui`
capability — best-effort and default-off, so nothing changes when the flag is unset.

## What the operator gets

- `ui://cao/dashboard` — fleet overview + the mutation entry point.
- `ui://cao/agent` — one terminal's status, output tail, inbox, sub-agents.
- `ui://cao/event-stream` — live governance ticker (app-only).
- `cao://widget/topology` + `/widgets/topology/` — build-free live event view.

All mutations flow through `submit_command(kind, payload)` — kinds:
`send_message`, `assign`, `create_session` (standard); `interrupt`, `pause`,
`resume` (lifecycle); `shutdown_session` (destructive).

## Full capability scope (what the views use)

Beyond `tools/call`, the views exercise the spec's bidirectional channel:

- **Host-delegated open-link** (`ui/open-link`) — the dashboard shows
  "Open full Web UI ↗" → `http://127.0.0.1:9889` **only when** the host
  advertises `hostCapabilities.openLinks` (gate on `app.canOpenLinks()`; the
  sandbox forbids `window.open`).
- **Display modes** (`ui/request-display-mode`) — views declare
  `availableDisplayModes: ["inline","fullscreen"]` at `ui/initialize`.
- **Streamed tool input** (`ui/notifications/tool-input` / `-partial`) — render
  before the result lands.
- **Model-context notes** (`ui/update-model-context`) — body-free gesture
  summaries keep the agent aware without leaking message contents.

`preferredFrameSize` and `requiredScopes` are CAO additions, **not** spec
`_meta.ui` fields (the spec sizes via `containerDimensions` +
`ui/notifications/size-changed`); CAO requests **no** elevated `permissions`.

## Troubleshooting

- **Host doesn't offer the views** → confirm `CAO_MCP_APPS_ENABLED=true` and that
  `initialize` advertises `io.modelcontextprotocol/ui` (the host must speak
  SEP-1865). Non-SEP-1865 hosts still get text-only tool results.
- **Views are blank / fail to load** → the React bundles aren't built. Run
  `cd cao_mcp_apps && npm ci && npm run build:all`. The topology widget needs no
  build and is the quickest smoke test (`curl /widgets/topology/topology.html`).
- **Mutations rejected with 403** → the auth layer is enabled and the token lacks
  `cao:write`/`cao:admin` (`cao:admin` for `delete_session`). Unset
  `AUTH0_DOMAIN`/`CAO_AUTH_JWKS_URI` to disable enforcement.
- **Events don't stream** → check `GET /events` (SSE) directly; the bus is
  drop-on-slow, so a stalled consumer silently loses events — re-hydrate via
  `cao_fetch_history`.

## Extending the surface

- **Building or migrating an MCP App? Load the `mcp-apps-builder` skill first.**
  It equips the official ext-apps Agent Skills (`create-mcp-app`,
  `add-app-to-server`, `migrate-oai-app`, `convert-web-app`) and the build guide.
  Use `add-app-to-server` when adding a new `ui://cao/<name>` view.
- **New command kind** → add it to `submit_command`'s classifier + router in
  `mcp_server/app_tools.py` (map to a real Backplane HTTP endpoint; never bypass
  the HTTP-only boundary) and to the scope pre-check.
- **New view** → add a `ui://cao/<name>` resource in `ext_apps/apps.py` + an entry
  point under `cao_mcp_apps/`, build it, and tag the rendering tool with
  `ui_meta(...)`.
- **New host-delegated action** → add a thin method on the `McpApp` bridge
  (`cao_mcp_apps/src/shared/mcpApp.ts`) that issues the spec `ui/*` request
  (e.g. `openLink` → `ui/open-link`, `requestDisplayMode` →
  `ui/request-display-mode`); gate UI on the matching `hostCapabilities` flag and
  cover it with a `mockHost` test.
- **Keep the boundary** → `mcp_server/*` must reach state only over HTTP; the AST
  guard test (`test/test_http_only_boundary.py`) enforces it.
- **Keep bundles JIT-free** → no `eval`/`new Function` (host CSP forbids it); the
  CI scan fails the build otherwise.
