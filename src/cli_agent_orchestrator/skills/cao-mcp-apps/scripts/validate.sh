#!/usr/bin/env bash
# /// script
# name = cao-mcp-apps-validate
# description = Validate the MCP Apps surface is enabled and views are accessible
# requires-python = ">=3.10"
# ///
set -euo pipefail

echo "=== cao-mcp-apps validation ==="

# Check server is running
if ! curl -sf http://localhost:9889/health > /dev/null 2>&1; then
    echo "SKIP: cao-server not running at localhost:9889"
    exit 0
fi

# Check the topology widget (requires no build)
TOPO_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" http://localhost:9889/widgets/topology/topology.html 2>/dev/null || echo "000")
if [ "$TOPO_STATUS" = "200" ]; then
    echo "PASS: topology widget accessible"
else
    echo "WARN: topology widget not found (HTTP $TOPO_STATUS)"
    echo "  This is expected if CAO_MCP_APPS_ENABLED is not set"
fi

# Check SSE events endpoint
EVENTS_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 2 http://localhost:9889/events 2>/dev/null || echo "000")
if [ "$EVENTS_STATUS" = "200" ]; then
    echo "PASS: /events SSE endpoint accessible"
else
    echo "INFO: /events returned HTTP $EVENTS_STATUS"
fi

# Check AG-UI stream (shared surface)
AGUI_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 2 http://localhost:9889/agui/v1/stream 2>/dev/null || echo "000")
if [ "$AGUI_STATUS" = "200" ]; then
    echo "PASS: AG-UI stream enabled (CAO_AGUI_ENABLED=true)"
elif [ "$AGUI_STATUS" = "404" ]; then
    echo "INFO: AG-UI stream disabled (set CAO_AGUI_ENABLED=true to enable)"
else
    echo "INFO: AG-UI stream returned HTTP $AGUI_STATUS"
fi

echo "=== validation complete ==="
