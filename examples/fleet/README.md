# CAO Fleet — cross-node coordinator

Run one CAO node per machine (VPS, VM, container, or laptop) and drive the **whole
fleet from a single coordinator**: see every node's health and sessions, launch and
message agents on any node, and watch each remote agent's CLI screen live — without
SSHing into every host.

This extends CAO from *"one machine coordinating many agents"* to *"one coordinator
managing many CAO nodes,"* while every node keeps its normal localhost-first
behavior. It is the reference implementation for issue
[#349](https://github.com/awslabs/cli-agent-orchestrator/issues/349).

> **"Fleet" here means multiple _CAO nodes_ (machines), not the single-node agent
> fleet.** CAO's [MCP Apps](../../docs/mcp-apps.md) also uses the word "fleet" for the
> set of agents running on **one** `cao-server`. This example is about coordinating
> **across machines**; it does not change or depend on the MCP Apps Fleet UI.

> **This is PR 1 of a 3-PR series for #349.** It ships the coordinator foundation
> (bootstrap + AI conductor + the `fleet` helper + registry). The **web panel**
> (`panel/`) lands in **PR #366** and the **full guide** (`docs/fleet-coordinator.md`)
> in **PR #367**. Sections below tagged _(PR #366)_ / _(PR #367)_ are not runnable from
> this PR alone.

## Two ways to coordinate

| Surface | What it is | Best for |
|---|---|---|
| **Conductor** (`bin/fleet-conductor`) | An AI agent (Claude Code) wired to one `cao-ops-mcp-server` per node. You ask it, in plain language, to observe or command the fleet. | Natural-language, agent-driven fleet ops. |
| **Web panel** (`panel/`, _PR #366_) | A FastAPI app that fans out to every node's `cao-server` REST API. A browser SPA shows a wall of live agent screens; click a tile for a focused console (send messages + control keys). | A visual, terminal-feeling control panel. |

Both are **stateless proxies** over the same node registry (`fleet.json`) and the same
`cao-server` HTTP API. Use either or both.

## Transport-agnostic by design

Nodes are addressed by `host:port` — a node's `host` may be a **Tailscale or
WireGuard IP, a VPN or LAN IP, or a DNS name**. Anything the coordinator can reach
works. `bootstrap.sh` does not require any specific mesh; it auto-detects a bind
address and you point the coordinator at it. **The private network is the trust
boundary: there is no per-request API auth in this setup, so anyone who can reach a
node's port has full agent-launch/PTY control. Do not expose a node's port to the
public internet.** (`CAO_ALLOWED_HOSTS` is a Host-header allowlist — DNS-rebinding
protection — not authentication.)

## Layout

```
examples/fleet/
├── fleet.example.json              # node registry — copy to fleet.json and edit
├── bin/
│   ├── fleet                       # run cao commands against one node (list/show/exec)
│   ├── fleet-conductor             # start the AI conductor
│   └── render-mcp-config.py        # fleet.json -> conductor/.mcp.json
├── conductor/CONDUCTOR.md          # the conductor's operating guide
├── deploy/
│   ├── bootstrap.sh                # one-command node setup (Linux/macOS)
│   └── cao-server.service.example  # hand-install systemd unit template
├── panel/                          # FastAPI web panel + live console SPA   (PR #366)
└── test/
    ├── test_fleet.sh
    └── test_render_mcp_config.sh
```

## Requirements

- **Python 3.10+** on the coordinator (for `fleet` and `render-mcp-config.py`).
- A CAO node reachable at `host:9889` for each machine (see step 1).
- A private network connecting them (Tailscale, WireGuard, VPN, SSH tunnel, or LAN).
- The web panel _(PR #366)_ additionally needs [`uv`](https://docs.astral.sh/uv/).

## Quickstart

### 1. Bootstrap each node

On every machine you want in the fleet:

```bash
bash examples/fleet/deploy/bootstrap.sh
# force a specific address with:  CAO_BIND_HOST=<ip-or-hostname> bash .../bootstrap.sh
```

It installs `uv`, `tmux`, CAO, and agent profiles, then starts a persistent
`cao-server` bound to the node's private-network address. It prints the node's
address and the `fleet.json` entry to add.

### 2. Register your nodes

```bash
cd examples/fleet
cp fleet.example.json fleet.json
# edit fleet.json: one entry per node, with its real host/label/role
```

`fleet.json` is git-ignored so your node addresses stay local.

### 3. Drive it with the AI conductor

```bash
python3 bin/render-mcp-config.py     # build conductor/.mcp.json from fleet.json
bin/fleet-conductor                  # interactive; or: bin/fleet-conductor "status across the fleet"
```

### …or with the web panel _(PR #366)_

The `panel/` app lands in PR #366; once it's present:

```bash
cd panel
uv sync
CAO_PANEL_HOST=127.0.0.1 uv run fleet-panel   # then open http://127.0.0.1:9888
```

Set `CAO_PANEL_HOST` to your coordinator's private-network IP to reach the panel
from other devices.

### Ad-hoc control from the shell

```bash
bin/fleet list                   # list nodes
bin/fleet exec node-b session list   # run any cao command against a node
bin/fleet show node-b            # print the resolved API base URL
```

## Tests

This PR ships hermetic shell tests (temp registry, stubbed `cao`, no network):

```bash
bash examples/fleet/test/test_fleet.sh           # node resolution + exec passthrough
bash examples/fleet/test/test_render_mcp_config.sh   # .mcp.json rendering
```

The panel's Python + JS suites land with the panel in _PR #366_.

## Contribution note

This example lands as a short PR series against #349: (1) this coordinator
foundation, (2) the web panel + live console (`panel/`, #366), (3) the guide
(`docs/fleet-coordinator.md`, #367). Provider adapters (Qwen/MiniMax) mentioned in
the issue are tracked separately.
