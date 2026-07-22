#!/usr/bin/env bash
# /// script
# name = mcp-apps-builder-validate
# description = Validate prerequisites for building MCP Apps (Node.js, ext-apps SDK)
# requires-python = ">=3.10"
# ///
set -euo pipefail

echo "=== mcp-apps-builder validation ==="

# Check Node.js
if command -v node > /dev/null 2>&1; then
    NODE_VER=$(node --version)
    echo "PASS: Node.js installed ($NODE_VER)"
else
    echo "FAIL: Node.js not found (required for building MCP Apps)"
    echo "  Fix: install Node.js 18+ via nvm, brew, or your package manager"
    exit 1
fi

# Check npm
if command -v npm > /dev/null 2>&1; then
    NPM_VER=$(npm --version)
    echo "PASS: npm installed ($NPM_VER)"
else
    echo "FAIL: npm not found"
    exit 1
fi

# Check if ext-apps SDK is available
if npm list @modelcontextprotocol/ext-apps 2>/dev/null | grep -q ext-apps; then
    echo "PASS: @modelcontextprotocol/ext-apps SDK installed"
else
    echo "INFO: @modelcontextprotocol/ext-apps not in current project"
    echo "  Install with: npm install @modelcontextprotocol/ext-apps"
fi

# Check CAO-specific build system (if in CAO repo)
if [ -d "cao_mcp_apps" ]; then
    echo "INFO: CAO MCP Apps build system detected"
    if [ -f "cao_mcp_apps/package.json" ]; then
        echo "PASS: cao_mcp_apps/package.json exists"
    fi
    if [ -d "cao_mcp_apps/node_modules" ]; then
        echo "PASS: dependencies installed"
    else
        echo "WARN: run 'cd cao_mcp_apps && npm ci' to install dependencies"
    fi
fi

echo "=== validation complete ==="
