# Knowledge graph — the graph layer & how to view it

CAO can project a subsystem's state (memory first) into a **standard, typed
graph** and hand it to whatever engine you want to render or store it. The
thesis is deliberately small:

> **CAO emits a standard `GraphView`; you bring the engine.**

The **primary way to see your graph is the web UI**: the `web/` single-page app
has a **Memory tab** with a List⇄Graph toggle that renders your real memory
graph, lets you drag and click nodes to read memories, and exports to Obsidian —
all against the same `GraphView` contract. Beyond that first-party viewer, the
same shape fans out to a sink you point the graph at: a file you open in
Obsidian/Gephi, the built-in Sigma renderer inside an MCP-Apps host, or a
dependency-light standalone dev page. This doc explains the design (for the "how
is this built" reader) and then gives copy-pasteable steps to actually see your
graph (for the "just show me" reader).

> **Ports in this doc are examples.** `cao-server` defaults to `127.0.0.1:9889`
> (`CAO_API_PORT`); the web dev server runs on `5173` and proxies to it. The
> standalone-explorer examples further down use server port **9894** and serve
> the page on **8900** — match your own `CAO_API_PORT` and the page's editable
> server field.

## Design in brief

Two pluggable axes sit on either side of the `GraphView` contract:

- **Providers** — project *some subsystem* → a `GraphView`. `memory` is the
  first (and today only real) provider; `stub` exists to prove the seam is
  heterogeneous. Register more with `@register_provider("name")`.
- **Sinks** — consume a `GraphView` → *some target*. Built in: the in-host
  Sigma **renderer** (`ui://cao/graph`), plus three file exporters —
  **OKF**, **Obsidian**, and **GraphML**. Register more with
  `@register_sink("name")`.

```
                         ┌──────────────────────────┐
   subsystem state  ───▶ │  GraphProvider.project()  │ ───▶  GraphView
   (memory wiki, …)      └──────────────────────────┘        {nodes, edges, meta}
                                                                    │
             ┌──────────────────┬──────────────────────────────────┼─────────────────────┐
             ▼                  ▼                     ▼              ▼                     ▼
     web/ Memory tab    render_graph_view       OKF sink      Obsidian sink         GraphML sink
     (Sigma in the      (ui://cao/graph,        (md bundle)   (wiki-linked vault)   (.graphml XML)
      React SPA)         Sigma in MCP host)          │              │                     │
                                                     └──── files under CAO_GRAPH_EXPORT_ROOT ┘
```

*Text description:* a provider projects a subsystem into a single `GraphView`
(nodes + edges + metadata). That one shape then feeds any consumer: the
first-party **web/ Memory tab** (Sigma in the React SPA), the built-in Sigma
renderer (rendered inside an MCP-Apps host), or the OKF / Obsidian / GraphML
file exporters (which write under the graph-export root). There is **zero
branching over provider or sink name** in the API route — names resolve through
registries.

### The `GraphView` contract

Source: `src/cli_agent_orchestrator/graph/models.py`.

| Field | Type | Notes |
|---|---|---|
| `Node.id` | `str` | Unique within a view (duplicate ids are rejected). For memory, this is the topic key. |
| `Node.kind` | `str` | Lowercase snake-case (`^[a-z][a-z0-9_]*$`). `"topic"` for memory, `"stub"` for the stub provider. |
| `Node.label` | `str` | Display text. **Untrusted** — may be an LLM summary; sinks and the web UI escape/render as plain text on output. |
| `Node.status` | `NodeStatus` | `active` (default), `proposal`, `observation`, `superseded`. |
| `Node.attrs` | `dict` | Free-form. Memory sets `is_hub` and/or `is_orphan` (see below). |
| `Edge.source` / `Edge.target` | `str` | Must reference known node ids (validated). |
| `Edge.type` | `EdgeType` | Closed taxonomy — see below. |
| `Edge.attrs` | `dict` | Free-form (e.g. `{"source": "related_keys"}`). |
| `GraphView.meta` | `dict` | Provider metadata (`provider`, `scope`, `scope_id`, …), plus cache provenance (`cached`, `as_of`) — see [Caching](#caching--staleness). |

The **edge-type taxonomy** (`EdgeType`) is a closed enum organized by family:

| Value | Family | Populated by |
|---|---|---|
| `relates_to` | topical | memory provider (from `related_keys`) |
| `contradiction` | lint-derived | memory provider (from `wiki_lint`) |
| `supersedes` | lifecycle | **reserved** — no provider emits it in this deliverable |

> `GraphView` enforces referential integrity (every edge endpoint is a known
> node) but imposes **no size cap** on node/edge counts or payloads — providers
> are trusted local code today.

## What the memory provider projects

Source: `src/cli_agent_orchestrator/graph/providers/memory.py`.

`MemoryGraphProvider` projects **one memory scope's wiki** into a graph, keyed
by `(scope, scope_id)`:

- **Nodes** — one `kind="topic"` node per key in that scope's index, plus one
  extra node per `orphan_page` lint finding (orphans are absent from the index
  by definition, so they need a synthesized node to carry the attribute).
- **Edges** —
  - `relates_to` edges from each topic's `related_keys` (a target outside this
    scope's key set is dropped — never a cross-scope edge);
  - `contradiction` edges from `wiki_lint` contradiction findings, carrying a
    `summary` of the finding in `attrs`.
- **Node attributes** —
  - `is_hub: true` — from a `graph_density` lint finding (highly-connected topic);
  - `is_orphan: true` — from an `orphan_page` lint finding (unreferenced topic).
- **Scope-bounded** — edges never cross the `(scope, scope_id)` boundary.
  `stale_claim`, `poison_frequency`, and `lint_error` findings are dropped.

A scope with no wiki on disk (or an unresolvable scope) is an **empty graph, not
an error**. If the lint pass itself fails, the provider degrades to a lint-free
graph (topics + `relates_to` only) and records `lint_error` in `meta` rather
than returning a 500.

> Graphs can be **sparse**. A scope with topics but no `related_keys` and no
> contradiction findings yields disconnected nodes; `global` today often has
> few or no edges. That's data, not a bug.

## Caching & staleness

Building the memory graph runs `wiki_lint` (ripgrep-backed detectors + an LLM
contradiction check) **in-request**. Profiling `scope=global` measured this at
**~30s typical and up to ~148s under load** — the dominant cost is the
ripgrep-based `stale_claim` detector (~95 `rg` subprocess spawns), not the LLM
detector. Because that can exceed the frontend's 120s fetch budget, the
projection is now **cached** (`src/cli_agent_orchestrator/graph/cache.py`).

- The **projected `GraphView` is cached per `(provider, scope, scope_id)`** with
  a **300-second TTL** (`DEFAULT_TTL_S = 300.0`).
- The first (cold) request in a window pays the full projection cost; every
  request within the TTL returns the cached view **near-instantly**. Concurrent
  cold requests for the same key collapse onto a single build (single-flight —
  no thundering herd).
- `meta.cached` (bool) and `meta.as_of` (ISO-8601 UTC timestamp of the build)
  tell you whether a response was served from cache and when the underlying data
  was projected.

> **Staleness caveat — read this.** Invalidation is **TTL-only**; there is no
> write-invalidation hook wired up today. So after you **store or forget** a
> memory, the graph can be **up to 300s stale**. The **Refresh** button in the
> web UI does **not** bypass the cache — within the TTL it serves the same
> cached view. To force a fresh projection sooner, wait out the TTL. (An
> `invalidate()` method exists in `cache.py` for a future write-path hook to
> call, but nothing calls it yet — this is a tracked follow-up to wire Refresh
> to bypass the cache.)

> **First cold load may still time out on a large scope.** If the cold
> projection runs past the UI's 120s budget, the fetch aborts — but thanks to
> single-flight the server keeps building and caches the result, so a **Refresh
> a moment later** returns it near-instantly.

## The API

Two routes, in `src/cli_agent_orchestrator/api/main.py`. Both take the provider
name as a path segment and forward all query params to the provider as filters.
These are still the right tool for **scripting / no-UI** use.

### `GET /graph/{provider}` — project and return the wire shape

| Param | In | Notes |
|---|---|---|
| `provider` | path | `memory` or `stub`. Unregistered → **404**. |
| `scope` | query | `global` (default) or `project`. `session` / `agent` → **400** (private, refused). |
| `scope_id` | query | Required for `project` (the canonical project id). Omit for `global`. |

Returns the `GraphView` wire shape: `{"nodes": [...], "edges": [...], "meta": {...}}`.

```bash
# Global scope (no scope_id)
curl -s "http://127.0.0.1:9889/graph/memory?scope=global" | jq

# A specific project scope
curl -s "http://127.0.0.1:9889/graph/memory?scope=project&scope_id=github-com-awslabs-cli-agent-orchestrator" | jq
```

#### Finding your `scope_id`

`scope_id` is the **canonical project id** resolved by
`resolve_project_id` (`services/memory_service.py`): the git-remote-derived,
auth-stripped identity — e.g. `github-com-awslabs-cli-agent-orchestrator` — or a
`sha256(realpath(cwd))[:12]` fallback for a repo with no remote. In practice the
scope_ids on your machine are the **directory names** under the memory root:

```bash
ls ~/.aws/cli-agent-orchestrator/memory/
# → global  github-com-awslabs-cli-agent-orchestrator  <12-hex-hash>  …
```

`global` has **no** `scope_id`. `project` scopes use the canonical id / dir
name. `session` and `agent` are private tiers and are **not** exposed through
this API at all (see [Secure access](#secure-access)).

### `POST /graph/{provider}/export` — project, then write through a sink

Request body (`GraphExportRequest`):

| Field | Type | Notes |
|---|---|---|
| `sink` | `str` | `okf`, `obsidian`, or `graphml`. Unregistered → **404**. |
| `dest` | `str` | Destination **relative to** `CAO_GRAPH_EXPORT_ROOT` (a directory for `okf`/`obsidian`, a filename for `graphml`). Traversal/escape → **400**. |
| `options` | `dict` | Opaque, forwarded per-sink; the route never inspects it. |

Query params (`scope`, `scope_id`) are still forwarded to the provider, exactly
as for `GET`. Response: `{"written_files": [...], "sink": "...", "dest": "..."}`.

```bash
# Export the global memory graph as an Obsidian vault named "global-vault"
curl -s -X POST \
  "http://127.0.0.1:9889/graph/memory/export?scope=global" \
  -H 'Content-Type: application/json' \
  -d '{"sink":"obsidian","dest":"global-vault","options":{}}' | jq

# Export a project scope to a single GraphML file
curl -s -X POST \
  "http://127.0.0.1:9889/graph/memory/export?scope=project&scope_id=github-com-awslabs-cli-agent-orchestrator" \
  -H 'Content-Type: application/json' \
  -d '{"sink":"graphml","dest":"cao.graphml","options":{}}' | jq
```

The built sinks:

| Sink | `dest` is | Output |
|---|---|---|
| `okf` | a directory | Markdown bundle: one `.md` per node + a generated `index.md` + a `manifest.md` provenance note. |
| `obsidian` | a directory | Obsidian vault: one wiki-linked note per node (`[[target]]` per outgoing edge; contradiction edges suffixed). No `.obsidian/` config written. |
| `graphml` | a filename | A single `.graphml` XML file (stdlib only, deterministic key order). |

## Secure access

The graph carries **summaries of memory content** (notably contradiction-edge
summaries), so access is gated. The behaviors below are enforced in code, not
aspirational.

- **Reads are scope-gated (D5).** `GET /graph/{provider}` requires any of
  `cao:read` / `cao:write` / `cao:admin` (read is the floor), identical to
  `/events`. This **supersedes** the earlier "ungated by design" (FR-12)
  wording — an unauthenticated caller must not read the graph.
- **Private tiers are refused outright.** `scope=session` or `scope=agent` is
  rejected with **400** even for an authed `cao:read` caller (case-insensitive
  check). The graph API never exposes private tiers — mirrors `/memory/export`.
- **Exports are write-scoped.** `POST /graph/{provider}/export` requires
  `cao:write` / `cao:admin`.
- **Secret gate runs before any write.** The serialized view is scanned by
  `secret_gate` **before** the sink is invoked. On a hit the export is rejected
  with **422**, the sink's `export()` is never called, and **nothing is
  written**. The 422 detail names only the matched **pattern**, never the
  matched bytes.
- **Exports are confined under a root.** `dest` is confined **under**
  `CAO_GRAPH_EXPORT_ROOT` (default `<CAO_HOME_DIR>/graph-exports`, i.e.
  `~/.aws/cli-agent-orchestrator/graph-exports`) via `safe_join_under_base` —
  per-segment validation + realpath containment. An absolute `dest` is accepted
  **only** if it already resolves under the root; `..` traversal, absolute-path
  escape, and symlink escape are all rejected with **400**. There is no
  arbitrary server-side write; **each sink owns confinement** before its first
  write.
- **Auth-off default: localhost trust.** With no IdP configured
  (`AUTH0_DOMAIN` / `CAO_AUTH_JWKS_URI` unset), every request is granted the
  full scope set and nothing is enforced — this is CAO's unauthenticated,
  **localhost-only** trust model. Keep the server on a trusted loopback host in
  this state. To enforce scopes, configure an IdP (`AUTH0_DOMAIN` /
  `CAO_AUTH_JWKS_URI`, plus `CAO_AUTH_AUDIENCE` / `CAO_AUTH_ISSUER`); see
  [`mcp-apps.md`](mcp-apps.md#security) for the full auth layer.

## Viewing option A — the web UI Memory tab (recommended)

**This is the primary way to view the graph.** The `web/` single-page app has a
**Memory** tab (the Brain icon) with a **List ⇄ Graph toggle**. Flip it to
**Graph** and CAO renders your real memory graph with Sigma.js — no MCP host, no
export step, no separate page.

What the Graph view gives you (`web/src/components/MemoryGraphView.tsx`,
`web/src/components/MemoryPanel.tsx`):

- **Sigma render** with the same visual constants as the MCP renderer
  (`GraphView.tsx`): a **hub** node is larger, an **orphan** node is dimmed grey,
  a **contradiction** edge is red, ordinary topics/edges are blue/slate.
- **Node dragging** — grab a node and drag to reposition it. A drag does **not**
  trigger click-to-read (the handler distinguishes a moved pointer from a click).
- **Click a node → read that memory** — clicking (without dragging) opens a side
  panel showing that topic's content as **plain text** (memory bodies are
  untrusted agent output, so they are never rendered as HTML/markdown).
- **Export to Obsidian** button — exports the currently-loaded scope to an
  Obsidian vault named `<scope>-vault` via `POST /graph/memory/export`, and
  toasts the resulting path under `CAO_GRAPH_EXPORT_ROOT`. Enabled once a graph
  with ≥1 node is loaded.
- **Shared scope selector** — the scope dropdown is shared with the List view.
  Pick **global** (sends no `scope_id`) or **project** (a `scope_id` text input
  appears; it's pre-filled from the listed memories when discoverable). The
  **All scopes / session / agent** tiers can't be projected as a single graph
  and show a friendly "pick global or project" guard instead of firing a doomed
  request.

### Run recipe (dev)

The web app is **same-origin** with the API (`api.ts` uses `BASE = ''`). In dev,
the Vite dev server proxies `/graph`, `/memory`, `/sessions`, `/terminals`,
`/agents`, `/settings`, `/flows`, and `/health` to `cao-server` on `:9889`
(`web/vite.config.ts`). So there is **no CORS setup for this path** — do **not**
set `CAO_CORS_ORIGINS` for the web UI (that's only for the standalone explorer,
option D).

```bash
# Terminal 1 — from the worktree root, start the API server (default :9889).
uv run cao-server

# Terminal 2 — start the web dev server (proxies to :9889).
cd web
npm install
npm run dev
# → open http://localhost:5173
```

Then in the browser: **Memory** tab → **Graph** toggle → pick **global** (or
**project** + a `scope_id`). Expect a slow first (cold) load — the server runs
`wiki_lint`; see [Caching & staleness](#caching--staleness). Subsequent views
within the 300s TTL are near-instant.

> **If `/graph` 404s**, the running server is stale or not this worktree's code.
> Stop any other server occupying `:9889` first, then from the worktree root run
> `uv sync` and `uv run cao-server` again. Confirm the OpenAPI schema lists the
> route: `curl -s http://127.0.0.1:9889/openapi.json | jq '.paths | keys'`
> should include `/graph/{provider}`.

### Run recipe (production)

Build the SPA into the package and let `cao-server` serve it directly — still
same-origin, no proxy:

```bash
cd web
npm run build          # emits static files into src/cli_agent_orchestrator/web_ui/
```

`cao-server` then serves the bundled UI at `http://localhost:9889`. Open that,
go to the Memory tab, and use the Graph toggle exactly as in dev.

## Viewing option B — file exports (Obsidian / GraphML)

If you'd rather see the graph in a dedicated graph tool, export it and open the
file. No web build, no MCP host.

**Obsidian** — export the `obsidian` sink, then open the folder as a vault:

```bash
curl -s -X POST "http://127.0.0.1:9889/graph/memory/export?scope=global" \
  -H 'Content-Type: application/json' \
  -d '{"sink":"obsidian","dest":"global-vault","options":{}}' | jq -r '.written_files[0]'
# → ~/.aws/cli-agent-orchestrator/graph-exports/global-vault/<topic>.md
```

Then in Obsidian: **Open folder as vault** → point at
`~/.aws/cli-agent-orchestrator/graph-exports/global-vault` → open the **Graph
view**. Each node is a note; each `relates_to` / `contradiction` edge is a
`[[wikilink]]`. (This is exactly what the web UI's **Export to Obsidian** button
produces.)

**GraphML** — export the `graphml` sink and open the `.graphml` in **Gephi**,
**yEd**, **Cytoscape**, or load it with **networkx**:

```bash
curl -s -X POST "http://127.0.0.1:9889/graph/memory/export?scope=global" \
  -H 'Content-Type: application/json' \
  -d '{"sink":"graphml","dest":"global.graphml","options":{}}' | jq -r '.written_files[0]'
# → ~/.aws/cli-agent-orchestrator/graph-exports/global.graphml
```

```python
import networkx as nx
g = nx.read_graphml("~/.aws/cli-agent-orchestrator/graph-exports/global.graphml")
print(g.number_of_nodes(), g.number_of_edges())
```

Everything lands **under** `CAO_GRAPH_EXPORT_ROOT`; the response's
`written_files` gives you the exact paths.

## Viewing option C — the built-in Sigma renderer (`ui://cao/graph`)

CAO ships a Sigma.js renderer as an **MCP App** (SEP-1865). It polls
`render_graph_view` every 30s and mounts a Sigma canvas over a graphology graph.
Node styling mirrors the contract: hubs are larger, orphans are grey,
contradiction edges are red.

**This needs a real MCP-Apps UI host** — it does not run as a standalone page.
Hosts that implement the SEP-1865 UI capability include the Claude Desktop
consumer app, Cursor, VS Code Insiders, and Goose (see the
[client support matrix](https://modelcontextprotocol.io/extensions/client-matrix)).

Steps:

```bash
# 1. Build the view bundles (emits graph.html into apps_static/)
cd cao_mcp_apps && npm ci && npm run build:all

# 2. Enable the surface and run both servers
export CAO_MCP_APPS_ENABLED=true
uv run cao-server        # FastAPI + /events on :9889
uv run cao-mcp-server    # registers render_graph_view + ui://cao/graph
```

Then, from an MCP-Apps-capable host connected to `cao-mcp-server`, ask in
**Agent-mode chat** to render the graph — the host renders the `ui://cao/graph`
view. See [`mcp-apps.md`](mcp-apps.md#enabling) for the full enable-and-drive
flow.

> **Caveat — not every host renders the canvas.** Enterprise/managed hosts and
> some third-party builds (e.g. Claude "cowork"-style deployments) may **not**
> implement the MCP-Apps UI capability. In that case you get the tool's JSON
> output (or a mermaid fallback), **not** the Sigma canvas. That's a host
> limitation, not a CAO bug — use option A, B, or D instead.

> **Node clicks are host-mediated.** Clicking a node calls `onOpenTopic`, which
> in this build calls `app.silentlyNoteToModel(...)` — it *tells the model* a
> node was opened. It is **not** a standalone "open this memory" action; without
> a host driving the model, a click does nothing visible. (The web UI's Graph
> view, option A, gives you a real click-to-read side panel instead.)

## Viewing option D — the standalone explorer (secondary / headless aid)

Lives at **`cao_mcp_apps/dev/memory-explorer/`**. It's a single self-contained
HTML page (deps via CDN) that loads your real memory graph from a running
`cao-server`, renders it with Sigma.js, and lets you click a node to read that
memory — the same experience as the web UI's Graph view, but with **no build
step and no npm**.

Use it **if you're not running the web UI** (option A) or want a dependency-light
page — e.g. a quick headless check. It is a **read-only local dev aid** (under
`cao_mcp_apps/dev/`, sanctioned but dev-tier, not shipped product code) and hits
the same public routes, so the same security behaviors apply (private-tier
**400**, secret-gate **422** on export).

Unlike option A, this page is served from a **different origin** than the API,
so it **does** need CORS: `cao-server` must be started with the page's origin in
`CAO_CORS_ORIGINS`, or the browser blocks every fetch. Full run steps and CORS
troubleshooting are in the tool's own
[`README.md`](../cao_mcp_apps/dev/memory-explorer/README.md); briefly:

```bash
# 1. Start cao-server with the page's origin allowed (CORS required here).
CAO_API_PORT=9894 \
CAO_CORS_ORIGINS="http://127.0.0.1:8900,http://localhost:8900" \
uv run cao-server

# 2. Serve the page on the matching origin
cd cao_mcp_apps/dev/memory-explorer
python3 -m http.server 8900
```

Then open <http://127.0.0.1:8900/index.html>, pick a scope (default `global`;
`project` needs a `scope_id`), click **Load graph**, and click any node to read
its memory. Same cold-load cost and 300s cache behavior as option A (it calls
the same routes). Assumes **auth off** — it sends no bearer token, so an
IdP-enabled server would `401`/`403` the fetches (the page surfaces that rather
than crashing).

## Choosing a path

| I want to… | Use |
|---|---|
| Just view / explore the graph, click a node to read a memory | **The web UI Memory tab** (option A) — the recommended default |
| Explore + click-to-read with **no build step / no npm** | The standalone **explorer** (option D) |
| A portable artifact for Gephi / yEd / networkx | **GraphML** export (option B) |
| The graph in Obsidian's own graph view | **Obsidian** export (option B) |
| Script against the graph data | `GET /graph/memory` via `curl` (the API) |
| The graph rendered **inside my agent host** | The built-in **Sigma renderer** (option C) — needs a UI-capable host |

## See also

- [`mcp-apps.md`](mcp-apps.md) — the MCP Apps surface, enabling
  `CAO_MCP_APPS_ENABLED`, the auth layer, and which hosts render MCP UI.
- [`web/README.md`](../web/README.md) — the web UI's architecture, the canonical
  dev-vs-production run story, and the Vite proxy config.
- [`cao_mcp_apps/dev/memory-explorer/README.md`](../cao_mcp_apps/dev/memory-explorer/README.md)
  — full explorer run guide, CORS troubleshooting, and export toast states.
- **Source of truth:**
  - Contract: `src/cli_agent_orchestrator/graph/models.py`
  - Providers: `src/cli_agent_orchestrator/graph/providers/`
  - Cache: `src/cli_agent_orchestrator/graph/cache.py`
  - Sinks: `src/cli_agent_orchestrator/graph/sinks/`
  - API routes: `src/cli_agent_orchestrator/api/main.py` (`/graph/{provider}`)
  - Web UI Graph view: `web/src/components/MemoryGraphView.tsx`,
    `web/src/components/MemoryPanel.tsx`
  - MCP renderer: `cao_mcp_apps/src/graph/GraphView.tsx`,
    `mcp_server/app_tools.py` (`render_graph_view`), `ext_apps/apps.py`
    (`ui://cao/graph`)
</content>
</invoke>
