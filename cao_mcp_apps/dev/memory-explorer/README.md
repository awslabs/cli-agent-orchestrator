# CAO Memory Graph Explorer (dev tool)

A **standalone, read-only** web page that loads your **real** memory graph
from a running `cao-server`, renders it with Sigma.js, and — when you click a
node — fetches that topic's content and shows it in a side panel.

This is a **dev/local tool**. It is NOT shipped product code and NOT the
MCP-Apps `ui://cao/graph` renderer (which only works inside an MCP host and
whose node-click is a silent no-op standalone). This page gives you an actual
"click a node to READ that memory" browser experience.

- File: `index.html` (single self-contained page; deps via esm.sh CDN)
- Visual rules mirror `cao_mcp_apps/src/graph/GraphView.tsx` (node size/color,
  edge color, `circular.assign` layout).

## Endpoints it uses

1. `GET /graph/memory?scope=<scope>&scope_id=<id>` → GraphView JSON
   (`{nodes, edges, meta}`).
2. `GET /memory/{key}?scope=<scope>&scope_id=<id>` → MemoryDetail JSON
   (`{content, key, scope, tags, ...}`). `key` is the node id from the graph.
3. `POST /graph/memory/export?scope=<scope>&scope_id=<id>` → export sink result
   (`{written_files, sink, dest}`). Body: `{"sink":"obsidian","dest":"<name>","options":{}}`.
   Used by the **Export to Obsidian** button (see below).

Default scope is **global**. `session`/`agent` scopes are private tiers; if the
server rejects them the page shows a friendly message. `project` needs a
`scope_id`.

> **Note (slow graph fetch):** the graph projection runs `wiki_lint` (ripgrep
> detectors) server-side and can take **~60s** on a populated scope. The page
> uses a 120s timeout and a spinner; be patient on first load.

## Assumptions

- **Auth is OFF** (no IdP configured — the user's setup). The `GET` graph and
  memory routes need no token. If auth were enabled the fetches would need a
  bearer token, which this dev tool does not send.

## CORS — the likely failure, handled explicitly

The page is served from `http://127.0.0.1:8900`; it fetches `cao-server` on a
different port → **cross-origin**. `cao-server`'s `CORSMiddleware` only allows
origins in `CORS_ORIGINS` (`constants.py`), extendable via the
`CAO_CORS_ORIGINS` env var (comma-separated). So the server **must** be started
with the explorer's origin allowed.

**Approach (a): serve the page from a fixed local port and allow that origin on
the server.**

### 1. Start cao-server with the explorer origin allowed

From the worktree root (`/Users/fanhongy/Project/cao-graph-layer-b4`):

```sh
CAO_API_PORT=9894 \
CAO_CORS_ORIGINS="http://127.0.0.1:8900,http://localhost:8900" \
CAO_MCP_APPS_ENABLED=1 \
uv run cao-server
```

(Ports used during verification: server on **9894**, page on **8900**. The
page's `server` field defaults to `http://127.0.0.1:9894` and is editable in
the header, so any port works — just match `CAO_API_PORT` and the field.)

### 2. Serve the page

```sh
cd cao_mcp_apps/dev/memory-explorer
python3 -m http.server 8900
```

### 3. Open it

<http://127.0.0.1:8900/index.html>

Pick a scope (default `global`), click **Load graph**, then click any node to
read its memory in the right-hand panel.

## Export to Obsidian

Next to **Load graph** is an **Export to Obsidian** button. It is **disabled
until a graph is loaded** (a successful load with >0 nodes); it exports the
*currently-loaded* scope/`scope_id` — not whatever is typed in the header.

On click it `POST`s to
`POST /graph/memory/export?scope=<scope>&scope_id=<id>` with body
`{"sink":"obsidian","dest":"<vault name>","options":{}}` and shows a toast with
the result. `dest` defaults to `<scope>-vault`; the optional **vault** field in
the header overrides it (leave blank for the default).

### Where the vault lands

`dest` is a **relative** vault name. The server confines it **under**
`CAO_GRAPH_EXPORT_ROOT` (default `<CAO_HOME_DIR>/graph-exports`) via
`safe_join_under_base`, so the vault ends up at:

```
<CAO_HOME_DIR>/graph-exports/<dest>
```

The success toast shows the actual server-returned path(s) (`written_files`) so
you can find it. **Do not** type an absolute path or `../` traversal — anything
resolving outside the root is rejected with **400**. Open the vault folder in
**Obsidian → Graph view** to see the exported notes.

### Write-scoped (unlike the read routes)

The read routes the explorer already uses (`GET /graph/memory`,
`GET /memory/{key}`) are ungated (and refuse private `session`/`agent` tiers).
The **export** route is additionally **WRITE-scoped**: it requires
`cao:write`/`cao:admin` via `require_any_scope`. With **auth OFF** (the user's
setup) it works tokenless. This dev tool **sends no token**, so if you enabled
an IdP the export would `401`/`403` — the toast surfaces that instead of
crashing.

### Secret gate (422)

Before writing anything, the server **secret-scans the serialized graph**. If a
memory value matches a secret pattern the export is rejected with **422** and
**no file is written**. The toast surfaces `body.detail`, which names the
matched **pattern only** — never the secret bytes. (This tool never dumps raw
secret content anywhere.)

### Export toast states

- **Success (200)** → "Exported N notes" + the vault path from `written_files`
  + "Open that folder in Obsidian → Graph view."
- **401/403** → export is scope-gated; note that this dev tool sends no token.
- **422** → blocked by the secret gate; shows the pattern name from `detail`.
- **400** → bad destination (traversal/outside-root) or private scope; shows
  `detail`.
- **timeout / unreachable** → same friendly "is cao-server running?" message as
  a failed load.

> **Note:** export re-projects the graph server-side (same `wiki_lint` pass as a
> load), so it can take up to ~60s; the button uses a 60s timeout and shows an
> "Exporting…" toast meanwhile.

## Empty / error states

- Empty graph → "No memory nodes for this scope".
- Server unreachable / timeout → "Server unreachable — is cao-server running?"
  with the exact start command.
- Private tier (400) → "session/agent scopes are private…".
- Missing memory on click (404) → "Memory not found" in the side panel.
