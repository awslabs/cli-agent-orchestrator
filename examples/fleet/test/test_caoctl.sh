#!/usr/bin/env bash
# test/test_caoctl.sh — assertions for caoctl node->endpoint resolution.
# Hermetic: builds a temp registry, so it needs no real fleet.json and no network.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAOCTL="$HERE/../bin/caoctl"
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
    { "name": "node-c", "host": "100.64.0.13", "role": "agent" }
  ]
}
JSON
export CAO_FLEET_CONFIG="$TMP/fleet.json"

assert_eq "$("$CAOCTL" --show node-a)" "http://100.64.0.11:9889" "resolve node-a"
assert_eq "$("$CAOCTL" --show node-b)" "http://100.64.0.12:9889" "resolve node-b"
assert_eq "$("$CAOCTL" --show node-c)" "http://100.64.0.13:9889" "resolve node-c"

# unknown node exits non-zero
"$CAOCTL" --show nope >/dev/null 2>&1
assert_eq "$?" "1" "unknown node exits 1"

# --list prints all three rows
count="$("$CAOCTL" --list | wc -l | tr -d ' ')"
assert_eq "$count" "3" "--list prints 3 rows"

exit $fail
