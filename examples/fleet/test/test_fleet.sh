#!/usr/bin/env bash
# test/test_fleet.sh — assertions for the `fleet` helper's node->endpoint
# resolution and the exec passthrough. Hermetic: builds a temp registry and stubs
# `cao`, so it needs no real fleet.json, no cao install, and no network.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET_BIN="$HERE/../bin/fleet"
fail=0
assert_eq() { # $1=actual $2=expected $3=label
  if [ "$1" = "$2" ]; then echo "ok: $3"; else echo "FAIL: $3 — got '$1' want '$2'"; fail=1; fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/fleet.json" <<'JSON'
{
  "port": 9889,
  "machines": [
    { "name": "node-a", "host": "100.64.0.11", "role": "central" },
    { "name": "node-b", "host": "100.64.0.12", "role": "agent" },
    { "name": "node-c", "host": "100.64.0.13", "role": "agent" },
    { "name": "node-d", "host": "100.64.0.14", "port": 9999, "role": "agent" }
  ]
}
JSON
export CAO_FLEET_CONFIG="$TMP/fleet.json"

# --- show: resolution against the default and a per-node port override ---
assert_eq "$("$FLEET_BIN" show node-a)" "http://100.64.0.11:9889" "show node-a (default port)"
assert_eq "$("$FLEET_BIN" show node-b)" "http://100.64.0.12:9889" "show node-b (default port)"
assert_eq "$("$FLEET_BIN" show node-d)" "http://100.64.0.14:9999" "show node-d (per-node port override)"

# --- unknown node exits non-zero ---
"$FLEET_BIN" show nope >/dev/null 2>&1
assert_eq "$?" "1" "unknown node exits 1"

# --- list prints one row per node ---
count="$("$FLEET_BIN" list | wc -l | tr -d ' ')"
assert_eq "$count" "4" "list prints 4 rows"
assert_eq "$("$FLEET_BIN" list | grep -c '^node-d 100.64.0.14$')" "1" "list row content for node-d"

# --- exec: exports CAO_API_HOST/PORT and runs the real `cao` with the args ---
# Stub `cao` on PATH so the test needs no install and no network.
STUB="$TMP/bin"; mkdir -p "$STUB"
cat > "$STUB/cao" <<'STUBSH'
#!/usr/bin/env bash
echo "HOST=$CAO_API_HOST PORT=$CAO_API_PORT ARGS=$*"
STUBSH
chmod +x "$STUB/cao"
out="$(PATH="$STUB:$PATH" "$FLEET_BIN" exec node-d session list)"
assert_eq "$out" "HOST=100.64.0.14 PORT=9999 ARGS=session list" "exec exports host/port + forwards args"

# --- missing registry exits 2 ---
CAO_FLEET_CONFIG="$TMP/does-not-exist.json" "$FLEET_BIN" list >/dev/null 2>&1
assert_eq "$?" "2" "missing registry exits 2"

exit $fail
