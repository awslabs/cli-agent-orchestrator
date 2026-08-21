#!/usr/bin/env bash
#
# Headless CI runner for a single CAO agent.
#
# Spawns one agent in async mode, polls its terminal status until it reaches
# a terminal state, prints the captured output, and exits with a code that
# CI can act on:
#
#   0 — agent reached IDLE/COMPLETED
#   1 — agent reached ERROR
# 124 — timed out (CAO_CI_TIMEOUT seconds, default 600)
#
# Usage:
#   ./run.sh "Your prompt here"
#   CAO_CI_TIMEOUT=120 ./run.sh "Quick task"
#
# Requires: cao-server already running, ci_developer profile installed.

set -euo pipefail

PROMPT="${1:-Print the current date and exit.}"
SESSION_NAME="ci-$$"
PREFIXED="cao-${SESSION_NAME}"
TIMEOUT="${CAO_CI_TIMEOUT:-600}"
POLL_INTERVAL="${CAO_CI_POLL_INTERVAL:-5}"

cleanup() {
    local code=$?
    cao shutdown --session "${PREFIXED}" >/dev/null 2>&1 || true
    exit "${code}"
}
trap cleanup EXIT INT TERM

echo "[ci] launching ci_developer in session ${PREFIXED} (timeout=${TIMEOUT}s)" >&2
cao launch --agents ci_developer \
    --headless --async --yolo \
    --session-name "${SESSION_NAME}" \
    --working-directory "${PWD}" \
    "${PROMPT}"

START=$(date +%s)
while true; do
    STATUS_JSON=$(cao session status "${PREFIXED}" --workers 2>/dev/null || echo "")
    STATUS=$(echo "${STATUS_JSON}" | grep -oE '"status":[[:space:]]*"[A-Z_]+"' | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/')

    case "${STATUS}" in
        IDLE|COMPLETED)
            echo "[ci] agent reached ${STATUS}" >&2
            cao session status "${PREFIXED}" --workers
            exit 0
            ;;
        ERROR)
            echo "[ci] agent reached ERROR" >&2
            cao session status "${PREFIXED}" --workers
            exit 1
            ;;
        WAITING_USER_ANSWER)
            echo "[ci] agent is waiting for input — fail fast (CI cannot answer)" >&2
            cao session status "${PREFIXED}" --workers
            exit 1
            ;;
    esac

    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    if [ "${ELAPSED}" -ge "${TIMEOUT}" ]; then
        echo "[ci] timeout after ${TIMEOUT}s (last status: ${STATUS:-unknown})" >&2
        cao session status "${PREFIXED}" --workers || true
        exit 124
    fi

    sleep "${POLL_INTERVAL}"
done
