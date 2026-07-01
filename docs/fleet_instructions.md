# Fleet coordinator — cross-node CAO

Run one CAO node per machine and coordinate the whole fleet from a single place.
This guide explains the architecture, the execution flows, and how to operate it.
The runnable code lives in [`examples/fleet/`](../examples/fleet/); this document is
the "why and how." It is the reference for issue
[#349](https://github.com/awslabs/cli-agent-orchestrator/issues/349).

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
SSH-ing into every host.

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
        Panel["cao-fleet-panel<br/>FastAPI · stateless fan-out"]
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
if present, then the default-route IP — and binds `cao-server` there.

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

Click a tile and the console mirrors that agent's **rendered CLI screen** (colors,
spinners, boxes), like glancing at a `tmux attach`. The browser polls only visible
tiles, at a cadence tied to their state, through the stateless panel proxy — no SSE
multiplexer. Nodes that expose the `/screen` primitive return a colored ANSI frame;
older nodes fall back to the plain-text `/output` tail, so no tile is ever blank.

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

The screen poll itself is a two-hop proxy with graceful degradation:

```mermaid
sequenceDiagram
    participant B as Browser (visible tile)
    participant P as Panel proxy
    participant N as node cao-server

    B->>P: GET /api/machines/{node}/terminals/{id}/screen
    P->>N: GET /terminals/{id}/screen?ansi=1
    alt node exposes /screen
        N-->>P: { screen: <ANSI frame>, ansi: true }
    else older node (404)
        P->>N: GET /terminals/{id}/output?mode=full
        N-->>P: plain-text tail
    end
    P-->>B: frame (colored, or plain-text fallback)
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
- **`name`** is how you refer to the node in `caoctl`, the panel, and the conductor.

## Transport and security

- **Any private network works.** The coordinator only needs to reach each node at
  `host:port`. Tailscale, WireGuard, a VPN, an SSH tunnel, or a trusted LAN are all
  fine — the transport is your choice, not a requirement of this example.
- **The network is the trust boundary.** Each node's `cao-server` is bound to its
  private-network address and its `CAO_ALLOWED_HOSTS`. **Do not expose a node's port
  to the public internet.** There is no per-request auth in this example; anyone who
  can reach the port can command the node.
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
cd panel && uv sync && uv run cao-fleet-panel            # http://127.0.0.1:9888
```

Ad-hoc, from the shell:

```bash
examples/fleet/bin/caoctl --list
examples/fleet/bin/caoctl node-b session list
```
