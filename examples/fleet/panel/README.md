# CAO Fleet Panel

A web control panel + live console for a CAO fleet. It is the `panel/` surface of the
[`examples/fleet`](../README.md) coordinator: a **stateless** FastAPI app that fans out
to every node's `cao-server` REST API and serves a browser SPA — a wall of live agent
screens, with a click-to-focus console that sends messages and control keys.

It adds no state and no core changes: each node stays a plain `cao-server`, and the
panel is just a client. Background and the execution-flow diagrams are in the
[fleet coordinator guide](../../../docs/fleet-coordinator.md).

## Requirements

- [`uv`](https://docs.astral.sh/uv/) (Python 3.10+).
- One or more CAO nodes reachable at `host:9889` — bring them up with
  [`../deploy/bootstrap.sh`](../deploy/bootstrap.sh).
- A registry (`../fleet.json`) listing those nodes (see below).

## Quickstart

```bash
cd examples/fleet
cp fleet.example.json fleet.json      # then edit: one entry per node
cd panel
uv sync
uv run fleet-panel                     # serves http://127.0.0.1:9888
```

The panel binds `127.0.0.1` by default. To reach it from another device, set
`CAO_PANEL_HOST` to the coordinator's private-network address **and** set a token
(see [Security](#security)):

```bash
CAO_PANEL_HOST=100.64.0.11 CAO_PANEL_TOKEN=$(openssl rand -hex 16) uv run fleet-panel
```

## The node registry

The panel reads the same `fleet.json` as the rest of `examples/fleet` — one registry
for the whole fleet. It resolves, in order: `$CAO_FLEET_CONFIG`, then `../fleet.json`,
then the packaged `../fleet.example.json` (placeholder nodes, so the panel still
starts on a fresh checkout). Format:

```json
{
  "port": 9889,
  "machines": [
    { "name": "node-a", "host": "100.64.0.11", "label": "coordinator",  "role": "central" },
    { "name": "node-b", "host": "100.64.0.12", "label": "worker-linux",  "role": "agent" }
  ]
}
```

`host` is any address the coordinator can reach the node at (Tailscale/WireGuard/VPN/LAN
IP or DNS name); `port` defaults to `9889`. `fleet.json` is git-ignored so your node
addresses stay local.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CAO_FLEET_CONFIG` | `../fleet.json` → `../fleet.example.json` | Path to the node registry. |
| `CAO_PANEL_HOST` | `127.0.0.1` | Address to bind the panel to. |
| `CAO_PANEL_PORT` | `9888` | Port to bind the panel to. |
| `CAO_PANEL_TOKEN` | _(unset)_ | Shared secret required on every request. Unset = no auth. |

## Security

The panel is a **control plane**: its routes launch agents, inject keystrokes
(including `^C`), and shut down sessions across every node. It reaches the nodes over
their `cao-server` REST API, which itself has **no per-request auth** — the private
network is the trust boundary (same posture as the [coordinator
guide](../../../docs/fleet-coordinator.md#transport-and-security)).

- **Loopback is safe; off-loopback is not, unless you add auth.** The moment you set
  `CAO_PANEL_HOST` to a network address, anyone who can reach the port has full
  agent/command execution across the fleet.
- **Set `CAO_PANEL_TOKEN` whenever the panel is not on loopback.** When set, every
  request (page, static assets, and API) must present it — as an HTTP Basic password
  (any username) so a browser prompts once, or as `Authorization: Bearer <token>` for
  scripts. Use a long random value.
- **Keep it on a private/VPN network regardless.** A token is a second layer, not a
  substitute for network isolation. Do not expose the panel — or a node's port — to
  the public internet. For real per-request auth in front of a node, use CAO's OAuth
  layer (`AUTH0_DOMAIN` / `CAO_AUTH_JWKS_URI`).

## Run as a service

[`systemd/fleet-panel.service.example`](systemd/fleet-panel.service.example) is a
ready-to-edit unit for running the panel on the coordinator.

## Tests

Hermetic — a fixture registry, mocked `cao-server` calls, no network or tmux:

```bash
# Backend (FastAPI + httpx MockTransport):
uv run python -m pytest

# Frontend (pure logic units, Node's built-in runner):
cd static && node --test test/*.test.js
```

These live under `panel/` with their own `pyproject.toml`, so the main repo's test run
does not pick them up; run them from here.
