# CAO Fleet — cross-node coordinator

Run one CAO node per machine (VPS, VM, container, or laptop) and drive the **whole
fleet from a single coordinator**: see every node's health and sessions, launch and
message agents on any node, and watch each remote agent's CLI screen live — without
SSH-ing into every host.

This extends CAO from *"one machine coordinating many agents"* to *"one coordinator
managing many CAO nodes,"* while every node keeps its normal localhost-first
behavior. It is the reference implementation for issue
[#349](https://github.com/awslabs/cli-agent-orchestrator/issues/349).

> **Full walkthrough, architecture, and diagrams:** [`docs/fleet_instructions.md`](../../docs/fleet_instructions.md).

## Two ways to coordinate

| Surface | What it is | Best for |
|---|---|---|
| **Conductor** (`bin/fleet-conductor`) | An AI agent (Claude Code) wired to one `cao-ops-mcp` server per node. You ask it, in plain language, to observe or command the fleet. | Natural-language, agent-driven fleet ops. |
| **Web panel** (`panel/`) | A FastAPI app that fans out to every node's `cao-server` REST API. A browser SPA shows a wall of live agent screens; click a tile for a focused console (send messages + control keys). | A visual, terminal-feeling control panel. |

Both are **stateless proxies** over the same node registry (`fleet.json`) and the same
`cao-server` HTTP API. Use either or both.

## Transport-agnostic by design

Nodes are addressed by `host:port` — a node's `host` may be a **Tailscale or
WireGuard IP, a VPN or LAN IP, or a DNS name**. Anything the coordinator can reach
works. `bootstrap.sh` does not require any specific mesh; it auto-detects a bind
address and you point the coordinator at it. The private network is the trust
boundary — **do not expose a node's port to the public internet.**

## Layout

```
examples/fleet/
├── fleet.example.json              # node registry — copy to fleet.json and edit
├── bin/
│   ├── caoctl                      # run cao commands against one node
│   ├── fleet-conductor             # start the AI conductor
│   └── render-mcp-config.py        # fleet.json -> conductor/.mcp.json
├── conductor/CONDUCTOR.md          # the conductor's operating guide
├── deploy/
│   ├── bootstrap.sh                # one-command node setup (Linux/macOS)
│   └── cao-server.service.example  # hand-install systemd unit template
├── panel/                          # FastAPI web panel + live console SPA
└── test/test_caoctl.sh
```

## Requirements

- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/) on the coordinator (for the panel).
- A CAO node reachable at `host:9889` for each machine (see step 1).
- A private network connecting them (Tailscale, WireGuard, VPN, SSH tunnel, or LAN).

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

### 3a. Drive it with the AI conductor

```bash
python3 bin/render-mcp-config.py     # build conductor/.mcp.json from fleet.json
bin/fleet-conductor                  # interactive; or: bin/fleet-conductor "status across the fleet"
```

### 3b. …or with the web panel

```bash
cd panel
uv sync
CAO_PANEL_HOST=127.0.0.1 uv run cao-fleet-panel   # then open http://127.0.0.1:9888
```

Set `CAO_PANEL_HOST` to your coordinator's private-network IP to reach the panel
from other devices. See [`panel/systemd/cao-fleet-panel.service.example`](panel/systemd/cao-fleet-panel.service.example)
to run it as a service.

### Ad-hoc control from the shell

```bash
bin/caoctl --list                    # list nodes
bin/caoctl node-b session list       # run any cao command against a node
bin/caoctl --show node-b             # print the resolved API base URL
```

## Tests

```bash
bash examples/fleet/test/test_caoctl.sh              # node resolution
cd examples/fleet/panel && uv run pytest -q          # panel API + client + config
cd examples/fleet/panel/static && node --test test/*.test.js   # UI units (ANSI, debounce, …)
```

## Contribution note

This example lands as a short PR series against #349: (1) this coordinator
foundation, (2) the web panel + live console (`panel/`), (3) the guide
(`docs/fleet_instructions.md`). Provider adapters (Qwen/MiniMax) mentioned in the
issue are tracked separately.
