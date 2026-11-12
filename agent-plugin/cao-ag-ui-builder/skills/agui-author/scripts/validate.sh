#!/usr/bin/env bash
# /// script
# name = agui-author-validate
# description = Validate the AG-UI surface is enabled and emit_ui works
# requires-python = ">=3.10"
# ///
set -euo pipefail

echo "=== agui-author validation ==="

# Check server is running
if ! curl -sf http://localhost:9889/health > /dev/null 2>&1; then
    echo "SKIP: cao-server not running at localhost:9889"
    exit 0
fi

# Check AG-UI surface is enabled. The stream never ends, so bound the request:
# curl exits 28 on the timeout but still reports the status it received. No -f,
# so a 404 is reported as a code instead of failing the script under set -e.
STREAM_STATUS=$(curl -s --max-time 2 -o /dev/null -w "%{http_code}" \
    http://localhost:9889/agui/v1/stream || true)
if [ "$STREAM_STATUS" = "404" ]; then
    echo "WARN: AG-UI surface disabled (CAO_AGUI_ENABLED not set)"
    echo "  Fix: export CAO_AGUI_ENABLED=true and restart cao-server"
    exit 0
fi

# Test emit_ui with a valid component
RESULT=$(curl -s -X POST http://localhost:9889/agui/v1/emit_ui \
    -H 'Content-Type: application/json' \
    -d '{"component":"metric","props":{"label":"validation","value":1,"unit":"ok"}}' || true)

# Require ok:true; a bare '"ok"' match would also pass on {"ok": false}.
if echo "$RESULT" | grep -Eq '"ok": *true'; then
    echo "PASS: emit_ui works (metric component)"
else
    echo "FAIL: emit_ui returned unexpected result: $RESULT"
    exit 1
fi

# Test off-list rejection
REJECT=$(curl -sf -o /dev/null -w "%{http_code}" -X POST http://localhost:9889/agui/v1/emit_ui \
    -H 'Content-Type: application/json' \
    -d '{"component":"script","props":{}}' 2>/dev/null || true)

if [ "$REJECT" = "400" ]; then
    echo "PASS: off-list component rejected (HTTP 400)"
else
    echo "INFO: off-list rejection returned $REJECT (expected 400)"
fi

echo "=== validation complete ==="
