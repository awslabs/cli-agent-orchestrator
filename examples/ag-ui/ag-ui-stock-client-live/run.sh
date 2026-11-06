#!/usr/bin/env bash
#
# AG-UI stock client live demo (POST /agui/v1/run).
#
# Boots cao-server with CAO_AGUI_ENABLED=1 and mock_cli on PATH, then runs the
# pinned upstream stock client (@ag-ui/client HttpAgent, zero CAO wire code)
# against /agui/v1/run and verifies at least one frame is received from
# post-connect activity (AC3 / spec Requirement 13.4). It then verifies the
# ambient stream (GET/POST /agui/v1/stream) with the same SDK's verifyEvents:
# the conformance tests' captured streams and one live HttpAgent run.
#
# This proves the run plane works with the official AG-UI SDK speaking the
# stock protocol (no CopilotKit page, no CAO-specific adapter).
#
# Requires Node.js (for the @ag-ui/client SDK). Usage:
#   ./examples/ag-ui/ag-ui-stock-client-live/run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

export CAO_AGUI_ENABLED="${CAO_AGUI_ENABLED:-1}"
export CAO_API_PORT="${CAO_API_PORT:-9889}"
export PATH="${REPO_ROOT}/test/providers/fixtures/bin:${PATH}"
BASE="http://localhost:${CAO_API_PORT}"
SERVER_PID=""
SERVER_LOG="$(mktemp -t agui-stock-client.XXXXXX.log)"
CAPTURE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agui-ambient-captures.XXXXXX")"

cleanup() {
    local code=$?
    [ -n "${SERVER_PID}" ] && kill "${SERVER_PID}" >/dev/null 2>&1 || true
    rm -f "${SERVER_LOG}"
    rm -rf "${CAPTURE_DIR}"
    exit "${code}"
}
trap cleanup EXIT INT TERM

CAO_SERVER_BIN="cao-server"
if [ -x "${REPO_ROOT}/.venv/bin/cao-server" ]; then
    CAO_SERVER_BIN="${REPO_ROOT}/.venv/bin/cao-server"
fi

echo "[stock-client] Starting cao-server (CAO_AGUI_ENABLED=${CAO_AGUI_ENABLED}) on ${BASE}" >&2
"${CAO_SERVER_BIN}" >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!

# Wait for the server to become healthy.
for _ in $(seq 1 40); do
    if curl -fsS "${BASE}/health" >/dev/null 2>&1; then break; fi
    sleep 0.5
done
if ! curl -fsS "${BASE}/health" >/dev/null 2>&1; then
    echo "[stock-client] Server did not become healthy; log follows:" >&2
    cat "${SERVER_LOG}" >&2 || true
    exit 1
fi
echo "[stock-client] Server healthy." >&2

# Run the pinned @ag-ui/client stock client against /agui/v1/run (AC3).
EXAMPLE_DIR="${REPO_ROOT}/examples/ag-ui/ag-ui-stock-client-live"
if ! command -v node >/dev/null 2>&1; then
    echo "[stock-client] Node.js is required for the @ag-ui/client SDK; not found on PATH." >&2
    exit 1
fi
if [ ! -d "${EXAMPLE_DIR}/node_modules" ]; then
    echo "[stock-client] Installing pinned @ag-ui/client..." >&2
    (cd "${EXAMPLE_DIR}" && npm install --no-audit --no-fund >/dev/null 2>&1)
fi
echo "[stock-client] Running stock @ag-ui/client against /agui/v1/run..." >&2
node "${EXAMPLE_DIR}/client.mjs" "${BASE}"

# The ambient stream (GET/POST /agui/v1/stream) through the same pinned SDK's
# verifyEvents: every stream the conformance tests produce, then a live run
# through the stock HttpAgent that must end RUN_FINISHED cancelled.
echo "[stock-client] Capturing the ambient-stream conformance tests' streams..." >&2
(cd "${REPO_ROOT}" && CAO_AGUI_CAPTURE_DIR="${CAPTURE_DIR}" uv run pytest \
    test/api/test_agui_stream_conformance.py -q --no-cov -p no:cacheprovider)
echo "[stock-client] Verifying the ambient stream with @ag-ui/client verifyEvents..." >&2
node "${EXAMPLE_DIR}/verify-ambient.mjs" --captures "${CAPTURE_DIR}" --live "${BASE}"

echo "[stock-client] Done." >&2
