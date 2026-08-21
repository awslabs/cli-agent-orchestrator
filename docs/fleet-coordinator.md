# Fleet coordinator — cross-node CAO

Run one CAO node per machine and coordinate the whole fleet from a single place.
This guide explains the architecture, the execution flows, and how to operate it.
The runnable code lives in [`examples/fleet/`](../examples/fleet/); this document is
the "why and how." It is the reference for issue
[#349](https://github.com/awslabs/cli-agent-orchestrator/issues/349).

> **Part of the fleet example.** This guide documents [`examples/fleet/`](../examples/fleet/)
> — the coordinator foundation (merged in **#365**) and its web panel
> (`examples/fleet/panel/`, **#366**). Read it alongside that directory's own
> [`README.md`](../examples/fleet/README.md) and
> [`panel/README.md`](../examples/fleet/panel/README.md).

> **"Fleet" here means multiple _CAO nodes_ (machines).** CAO's
> [MCP Apps](mcp-apps.md) also uses "fleet" for the set of agents on **one**
> `cao-server` (its Fleet UI). This guide is about coordinating **across machines**
> and neither changes nor depends on that single-node Fleet UI.

- [What it is](#what-it-is)
- [Architecture](#architecture)
- [Execution flows](#execution-flows)
  - [1. Node bootstrap](#1-node-bootstrap)
  - [2. Web panel fan-out](#2-web-panel-fan-out)
  - [3. Live console screen mirror](#3-live-console-screen-mirror)
  - [4. AI conductor](#4-ai-conductor)
- [The node registry](#the-node-registry)
- [Transport and security](#transport-and-security)
- [Operate it](#operate-it)

## What it is

CAO already coordinates many agents on **one** machine (a supervisor delegating to
tmux-isolated workers). This layer coordinates many **CAO nodes**: each machine runs
its own `cao-server`, and a coordinator observes and commands all of them — node
health, installed providers, active sessions, and task delegation — without you
SSHing into every host.

Nothing about a node's local behavior changes. The coordinator is a thin, **stateless
client** of each node's existing HTTP API; there is no new database, no agent state on
the coordinator, and no change to how a node runs its own agents.

## Architecture

Two coordinator surfaces, one shared node registry, one shared per-node API:

- **Web panel** (`examples/fleet/panel/`) — a FastAPI app that fans out to every
  node's `cao-server` REST API and serves a browser SPA (a wall of live agent
  screens + a focused console).
- **AI conductor** (`examples/fleet/bin/fleet-conductor`) — a Claude Code agent
  wired to one `cao-ops-mcp` server per node, so one AI can drive the fleet in
  natural language.

Both read the same `fleet.json` (the node registry) and talk to the same
`cao-server` HTTP API on each node. Both are stateless: restart them any time.

```mermaid
flowchart TB
    subgraph coord["Coordinator"]
        direction TB
        UI["Browser — Fleet Console SPA"]
        Panel["fleet-panel<br/>FastAPI · stateless fan-out"]
        Cond["fleet-conductor<br/>AI agent · one MCP server per node"]
        Reg[("fleet.json<br/>node registry")]
    end

    UI -->|"HTTP :9888"| Panel
    Panel -. reads .-> Reg
    Cond -. reads .-> Reg
    Panel -->|"REST fan-out"| NET
    Cond -->|"cao-ops-mcp"| NET

    NET{{"Private network<br/>Tailscale · WireGuard · VPN · SSH · LAN"}}

    NET --> NA
    NET --> NB
    NET --> NC

    subgraph fleet["Fleet nodes — each runs cao-server + tmux + agents"]
        direction TB
        NA["node-a<br/>cao-server :9889"]
        NB["node-b<br/>cao-server :9889"]
        NC["node-c<br/>cao-server :9889"]
    end
```

## Execution flows

### 1. Node bootstrap

`deploy/bootstrap.sh` turns a fresh machine into a fleet node with one command. It is
transport-agnostic: it picks a bind address from `CAO_BIND_HOST`, then a Tailscale IP
if present, then the default-route IP — and binds `cao-server` there. On a
publicly-connected host (a cloud VM/VPS) that last fallback can resolve to a **public**
IP, which would expose an unauthenticated node; set `CAO_BIND_HOST` to a private/VPN
address before running bootstrap there. See [Transport and
security](#transport-and-security).

```mermaid
sequenceDiagram
    actor Op as Operator
    participant Node as New node
    participant CAO as cao-server (:9889)
    participant Reg as fleet.json (coordinator)

    Op->>Node: bash bootstrap.sh
    Node->>Node: pick bind address (CAO_BIND_HOST / Tailscale / default route)
    Node->>Node: install uv, tmux, CAO, agent profiles
    Node->>CAO: start persistent service, bind host:9889
    CAO-->>Node: GET /health → ok
    Node-->>Op: reachable at http://host:9889
    Op->>Reg: add { "name": ..., "host": ... }
```

### 2. Web panel fan-out

The panel is a **stateless proxy**. `GET /api/fleet` fans out to every node
concurrently and **isolates failures per node** — an offline or slow node is reported
`offline`, never a 500 for the whole fleet. Control actions (launch, message,
shutdown) proxy straight through to the target node's `cao-server`.

```mermaid
sequenceDiagram
    participant B as Browser (SPA)
    participant P as Panel (FastAPI)
    participant N1 as node-a cao-server
    participant N2 as node-b cao-server

    B->>P: GET /api/fleet
    par fan-out, isolated per node
        P->>N1: GET /health, /sessions
        N1-->>P: ok + sessions
    and
        P->>N2: GET /health, /sessions
        N2-->>P: timeout → marked offline
    end
    P-->>B: aggregated fleet (per-node online/offline + sessions)

    B->>P: POST /api/machines/node-a/launch
    P->>N1: POST /sessions (+ deliver task)
    N1-->>P: session + terminal id
    P-->>B: launched
```

### 3. Live console screen mirror

Click a tile and the console mirrors that agent's CLI, like glancing at a
`tmux attach`. The browser polls only visible tiles, at a cadence tied to their
state, through the stateless panel proxy — no SSE multiplexer.

Today this reads each terminal's existing **`GET /terminals/{id}/output?mode=full`**
tail (which can carry the CLI's own ANSI escapes, so the renderer still shows colors
and spinners). CAO's current server has no dedicated rendered-screen endpoint, so the
panel is designed to *prefer* a `GET /terminals/{id}/screen` primitive **if a future
server exposes one**, and otherwise fall back to the `/output` tail — so no tile is
ever blank on today's nodes. `/screen` is an optional/future server extension, not a
requirement of this example.

```mermaid
stateDiagram-v2
    [*] --> Idle: tile appears
    Idle --> Working: agent active
    Working --> Idle: agent quiet
    Idle --> Focused: tile opened
    Working --> Focused: tile opened
    Focused --> Working: tile closed
    Idle --> Offline: node unreachable
    Working --> Offline: node unreachable
    Offline --> Idle: node recovers

    note right of Focused: poll ~0.8 s
    note right of Working: poll ~1 s
    note right of Idle: poll ~3 s
    note right of Offline: polling stops
```

The screen poll itself is a two-hop proxy with graceful degradation. On today's
servers the `/screen` probe 404s and the panel serves the `/output` tail; the
`/screen` branch is there only for a future server that adds the endpoint:

```mermaid
sequenceDiagram
    participant B as Browser (visible tile)
    participant P as Panel proxy
    participant N as node cao-server

    B->>P: GET /api/machines/{node}/terminals/{id}/screen
    P->>N: GET /terminals/{id}/screen?ansi=1  (optional/future endpoint)
    alt future server exposes /screen
        N-->>P: { screen: <ANSI frame>, ansi: true }
    else today's server (404)
        P->>N: GET /terminals/{id}/output?mode=full
        N-->>P: text tail (may include ANSI)
    end
    P-->>B: frame (from /screen, or the /output tail)
```

### 4. AI conductor

The conductor is an AI agent given one MCP management surface per node.
`render-mcp-config.py` turns `fleet.json` into `conductor/.mcp.json`, where a node
named `node-b` becomes the MCP server `cao-node-b`. You then ask the conductor, in
plain language, to observe or act — and it calls the right node's tools.

```mermaid
sequenceDiagram
    actor H as Human
    participant C as Conductor (AI agent)
    participant M as cao-node-b (MCP server)
    participant S as node-b cao-server

    H->>C: "Launch a developer on node-b to do X"
    C->>M: launch_session(profile, provider, task, cwd)
    M->>S: POST /sessions
    S-->>M: session + terminal id
    M-->>C: launched
    C-->>H: "Running on node-b (session ...)"

    H->>C: "Status across the fleet"
    C->>M: list_sessions (repeated per cao-* server)
    M-->>C: sessions per node
    C-->>H: per-node summary (unreachable nodes noted, not fatal)
```

## The node registry

`fleet.json` is the single source of truth for both surfaces. Copy
`fleet.example.json` to `fleet.json` (git-ignored) and list your nodes:

```json
{
  "port": 9889,
  "machines": [
    { "name": "node-a", "host": "100.64.0.11", "label": "coordinator",  "role": "central" },
    { "name": "node-b", "host": "100.64.0.12", "label": "worker-linux",  "role": "agent" },
    { "name": "node-c", "host": "100.64.0.13", "label": "worker-macos",  "role": "agent" }
  ]
}
```

- **`host`** is any address the coordinator can reach the node at — a Tailscale or
  WireGuard IP, a VPN or LAN IP, or a DNS name. (The example values are placeholders
  in the reserved `100.64.0.0/10` CGNAT range; replace them with your own.)
- **`port`** defaults to `9889` (CAO's server port) and can be overridden per node.
- **`name`** is how you refer to the node in `fleet`, the panel, and the conductor.
- **`label`** / **`role`** are optional and used by the panel for display.

## Transport and security

> **⚠️ A node's `cao-server` is an unauthenticated command-execution surface.**
> Anyone who can reach `host:port` can launch and drive agents on that node — that is
> full command execution. The private network is the *only* thing protecting it.
> Keep every node — and the panel — on a private/VPN network, and **never expose a
> node's port or the panel to the public internet.**

- **Any private network works.** The coordinator only needs to reach each node at
  `host:port`. Tailscale, WireGuard, a VPN, an SSH tunnel, or a trusted LAN are all
  fine — the transport is your choice, not a requirement of this example.
- **The network is the trust boundary.** Each node's `cao-server` is bound to its
  private-network address, and there is **no per-request API auth on a node** in this
  example (see the callout above). Bind nodes to a private address only.
- **Bootstrap can bind a public address — set `CAO_BIND_HOST`.** `bootstrap.sh` picks
  its bind address as `CAO_BIND_HOST` → a Tailscale IP → the default-route IP. On a
  cloud host/VPS whose default route egresses a *public* interface (and where Tailscale
  isn't up), that last fallback would bind the unauthenticated `cao-server` to a public
  IP. Before bootstrapping any host with public connectivity, set `CAO_BIND_HOST` to a
  private/VPN address (or confirm Tailscale is up).
- **The panel adds opt-in auth; the nodes don't.** The web panel is also unauthenticated
  by default, so set **`CAO_PANEL_TOKEN`** whenever you bind it off loopback — every
  request then needs that shared secret (HTTP Basic password or `Bearer`). That guards
  the panel's own port; it does not add auth to the nodes it fans out to, which still
  rely on the private network. See
  [`panel/README.md`](../examples/fleet/panel/README.md#security).
- **`CAO_ALLOWED_HOSTS` is not authentication.** It is a `Host`-header allowlist
  enforced by `TrustedHostMiddleware` (a DNS-rebinding mitigation), not a
  network/port exposure control and not peer auth. For real authentication in front
  of a node, use CAO's OAuth layer (`AUTH0_DOMAIN` / `CAO_AUTH_JWKS_URI`).
- **Least privilege.** Run `cao-server` as a user that has only the agent access you
  intend. The raw PTY-attach WebSocket stays loopback-only by CAO's design; the
  coordinator uses the higher-level REST surface (message + a fixed set of control
  keys), not arbitrary keystroke injection.

## Operate it

Full setup is in [`examples/fleet/README.md`](../examples/fleet/README.md). In short:

```bash
# 1. On each machine — become a fleet node:
bash examples/fleet/deploy/bootstrap.sh

# 2. On the coordinator — register nodes:
cd examples/fleet && cp fleet.example.json fleet.json    # then edit

# 3a. Drive with the AI conductor:
python3 bin/render-mcp-config.py && bin/fleet-conductor

# 3b. …or the web panel:
cd panel && uv sync && uv run fleet-panel            # http://127.0.0.1:9888
```

Ad-hoc, from the shell:

```bash
examples/fleet/bin/fleet list
examples/fleet/bin/fleet exec node-b session list
```
