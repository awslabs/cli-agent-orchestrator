#!/usr/bin/env bash
#
# Drive the LIVE AG-UI generative-UI path against a running cao-server.
#
# Emits all six allow-listed components via POST /agui/v1/emit_ui (each -> a
# GENERATIVE_UI frame on the SSE stream) plus one OFF-LIST component (iframe)
# which the server-side allow-list refuses with HTTP 400 — proving an untrusted
# agent cannot inject arbitrary markup. Meanwhile it tails GET /agui/v1/stream
# and the PASS gate requires the frames to actually arrive: 6x HTTP 200,
# 1x HTTP 400, and >=6 GENERATIVE_UI frames captured off the live stream.
# Doubles as a deployment smoke test (non-zero exit on any miss).
#
# Requires: ./run.sh already running (or any cao-server with CAO_AGUI_ENABLED).
#
# Usage:
#   ./examples/agui-dashboard/showcase.sh
#   CAO_AGUI_BASE=http://localhost:9889 ./examples/agui-dashboard/showcase.sh
#   CAO_TOKEN=<jwt> ./examples/agui-dashboard/showcase.sh   # auth-enabled server

set -euo pipefail

BASE="${CAO_AGUI_BASE:-http://localhost:9889}"
STREAM="${BASE}/agui/v1/stream"
EMIT="${BASE}/agui/v1/emit_ui"

# Optional bearer for auth-enabled servers. EventSource-style stream auth uses
# ?access_token= (browsers can't set headers); the POST uses the header.
AUTH_ARGS=()
STREAM_URL="${STREAM}"
if [ -n "${CAO_TOKEN:-}" ]; then
    AUTH_ARGS=(-H "Authorization: Bearer ${CAO_TOKEN}")
    STREAM_URL="${STREAM}?access_token=${CAO_TOKEN}"
fi

command -v curl >/dev/null 2>&1 || {
    echo "curl is required" >&2
    exit 1
}
if ! curl -fsS "${BASE}/health" >/dev/null 2>&1; then
    echo "cao-server not reachable at ${BASE} — start it first:" >&2
    echo "  ./examples/agui-dashboard/run.sh" >&2
    exit 1
fi

FRAMES="$(mktemp -t agui-frames.XXXXXX)"
EMIT_OUT="$(mktemp -t agui-emit.XXXXXX)"
TAIL_PID=""
cleanup() {
    [ -n "${TAIL_PID}" ] && kill "${TAIL_PID}" >/dev/null 2>&1 || true
    rm -f "${FRAMES}" "${EMIT_OUT}"
}
trap cleanup EXIT INT TERM

# Tail the SSE stream in the background for the duration of the showcase.
curl -N -fsS "${STREAM_URL}" >"${FRAMES}" 2>/dev/null &
TAIL_PID=$!
sleep 1 # let the STATE_SNAPSHOT + subscription establish

fail=0

emit() {
    local name="$1" props="$2" expected="$3"
    local code
    code=$(curl -s -o "${EMIT_OUT}" -w '%{http_code}' -X POST "${EMIT}" \
        "${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"}" \
        -H 'content-type: application/json' \
        -d "{\"component\":\"${name}\",\"props\":${props}}")
    printf '  %-14s -> HTTP %s  %s\n' "${name}" "${code}" "$(cat "${EMIT_OUT}")"
    if [ "${code}" != "${expected}" ]; then
        echo "  ^ UNEXPECTED: wanted HTTP ${expected} for ${name}" >&2
        fail=1
    fi
}

echo "[showcase] emitting the six allow-listed components:"
emit approval_card '{"title":"Deploy to prod?","detail":"3 files, 1 migration","risk":"high"}' 200
emit choice_prompt '{"question":"Pick a branch","choices":[{"label":"main","value":"main"},{"label":"release","value":"release"}]}' 200
emit diff_summary '{"title":"Refactor rpc handler","files":[{"path":"a2a/rpc.py","additions":74,"deletions":3}]}' 200
emit progress '{"label":"Indexing repo","value":0.42}' 200
emit metric '{"label":"tokens","value":12840,"unit":"tok"}' 200
emit agent_card '{"name":"worker-1","provider":"kiro_cli","status":"working"}' 200

echo "[showcase] emitting an OFF-LIST component (must be refused 400):"
emit iframe '{"src":"https://evil.example"}' 400

sleep 1
echo
echo "[showcase] AG-UI frames captured from ${STREAM}:"
grep -aE '^event:|rejected_component' "${FRAMES}" | head -40 || true

# The frames are part of the gate, not just display: the six accepted intents
# must actually traverse the live SSE stream as GENERATIVE_UI frames.
FRAME_COUNT=$(grep -ac '^event: GENERATIVE_UI' "${FRAMES}" || true)

echo
if [ "${fail}" -eq 0 ] && [ "${FRAME_COUNT}" -ge 6 ]; then
    echo "[showcase] PASS: 6 components accepted (HTTP 200), iframe refused (HTTP 400), ${FRAME_COUNT} GENERATIVE_UI frames on the live stream."
else
    echo "[showcase] FAIL: emit_mismatch=${fail}, generative_ui_frames=${FRAME_COUNT} (need 0 mismatches and >=6 frames)." >&2
    exit 1
fi
