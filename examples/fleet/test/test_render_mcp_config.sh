#!/usr/bin/env bash
# test/test_render_mcp_config.sh — assertions for render-mcp-config.py.
# Hermetic: temp registry in, temp .mcp.json out (CAO_MCP_CONFIG_OUT), no network.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDER="$HERE/../bin/render-mcp-config.py"
fail=0
assert_eq() { if [ "$1" = "$2" ]; then echo "ok: $3"; else echo "FAIL: $3 — got '$1' want '$2'"; fail=1; fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/fleet.json" <<'JSON'
{
  "port": 9889,
  "machines": [
    { "name": "node-a", "host": "100.64.0.11" },
    { "name": "node-b", "host": "100.64.0.12", "port": 9999 }
  ]
}
JSON

CAO_MCP_CONFIG_OUT="$TMP/out.json" python3 "$RENDER" "$TMP/fleet.json" >/dev/null

# server naming (cao-<name>), server count, per-node port override, and str() port coercion
assert_eq "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len(d["mcpServers"]))' "$TMP/out.json")" "2" "two servers rendered"
assert_eq "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(",".join(sorted(d["mcpServers"])))' "$TMP/out.json")" "cao-node-a,cao-node-b" "servers named cao-<node>"
assert_eq "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["mcpServers"]["cao-node-a"]["env"]["CAO_API_PORT"])' "$TMP/out.json")" "9889" "node-a inherits default port"
assert_eq "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["mcpServers"]["cao-node-b"]["env"]["CAO_API_PORT"])' "$TMP/out.json")" "9999" "node-b per-node port override"
assert_eq "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["mcpServers"]["cao-node-b"]["command"])' "$TMP/out.json")" "cao-ops-mcp-server" "command is cao-ops-mcp-server"

# malformed registry (missing host) exits non-zero
cat > "$TMP/bad.json" <<'JSON'
{ "machines": [ { "name": "node-x" } ] }
JSON
CAO_MCP_CONFIG_OUT="$TMP/bad-out.json" python3 "$RENDER" "$TMP/bad.json" >/dev/null 2>&1
assert_eq "$?" "1" "malformed registry (missing host) exits 1"

exit $fail
