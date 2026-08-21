#!/usr/bin/env python3
"""Render the conductor's .mcp.json from fleet.json — one cao-ops-mcp-server per node.

Each node in the registry becomes an MCP server named `cao-<node-name>`, pointed at
that node's cao-server via CAO_API_HOST / CAO_API_PORT. The conductor (an AI agent)
then has one management surface per node and can observe/command the whole fleet.

Usage:
    python3 bin/render-mcp-config.py [path/to/fleet.json]
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # examples/fleet
fleet_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "fleet.json")

if not os.path.exists(fleet_path):
    sys.exit(
        f"render-mcp-config: no fleet registry at '{fleet_path}'.\n"
        "Copy fleet.example.json -> fleet.json and edit it first."
    )

with open(fleet_path, encoding="utf-8") as f:
    fleet = json.load(f)

try:
    machines = fleet["machines"]
    default_port = fleet.get("port", 9889)
    servers = {}
    for node in machines:
        servers[f"cao-{node['name']}"] = {
            "command": "cao-ops-mcp-server",
            "args": [],
            "env": {
                "CAO_API_HOST": node["host"],
                "CAO_API_PORT": str(node.get("port", default_port)),
            },
        }
except (KeyError, TypeError) as e:
    sys.exit(
        f"render-mcp-config: malformed registry '{fleet_path}' (missing {e}).\n"
        "Each node needs a 'name' and 'host'; see fleet.example.json."
    )

# Default output is conductor/.mcp.json; override with CAO_MCP_CONFIG_OUT (used by the test).
dest = os.environ.get("CAO_MCP_CONFIG_OUT") or os.path.join(ROOT, "conductor", ".mcp.json")
os.makedirs(os.path.dirname(dest), exist_ok=True)
with open(dest, "w") as f:
    json.dump({"mcpServers": servers}, f, indent=2)
    f.write("\n")
print(f"wrote {dest} with {len(servers)} servers: {', '.join(servers)}")
