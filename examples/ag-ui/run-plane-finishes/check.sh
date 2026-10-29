#!/usr/bin/env bash
#
# Checks that POST /agui/v1/run on a running cao-server ends: the body must
# carry RUN_FINISHED and close within 5 s. A stock AG-UI client (@ag-ui/client)
# resolves a run only when the response body ends, so a run that stays open
# after RUN_STARTED never completes for it.
#
# Usage: CAO_API_PORT=9889 ./examples/ag-ui/run-plane-finishes/check.sh
# The server needs CAO_AGUI_ENABLED=1. Talks to 127.0.0.1 only.

set -euo pipefail

PORT="${CAO_API_PORT:-9889}"
case "${PORT}" in
    '' | *[!0-9]*)
        echo "FAIL: CAO_API_PORT must be a number, got '${PORT}'"
        exit 2
        ;;
esac
URL="http://127.0.0.1:${PORT}/agui/v1/run"
LIMIT=5

rand() { od -An -N4 -tx4 /dev/urandom | tr -d ' \n'; }
BODY="{\"threadId\":\"thread-$(rand)\",\"runId\":\"run-$(rand)\",\"state\":{},\"messages\":[],\"tools\":[],\"context\":[],\"forwardedProps\":{}}"

echo "POST ${URL} (limit ${LIMIT}s)"
rc=0
out="$(curl -fsS -N --max-time "${LIMIT}" \
    -H 'Content-Type: application/json' \
    -H 'Accept: text/event-stream' \
    --data "${BODY}" "${URL}" 2>&1)" || rc=$?

frames="$(printf '%s\n' "${out}" | sed -n 's/^data: {"type": *"\([A-Z_]*\)".*/\1/p' | tr '\n' ' ')"
echo "frames: ${frames:-<none>}"

if [ "${rc}" -eq 28 ]; then
    echo "FAIL: body did not end within ${LIMIT}s"
    exit 1
fi
if [ "${rc}" -eq 22 ]; then
    echo "FAIL: HTTP error: $(printf '%s' "${out}" | tail -n 1)"
    echo "      404: AG-UI is off (CAO_AGUI_ENABLED=1); 501: the [agui] extra is missing; 401/403: auth"
    exit 1
fi
if [ "${rc}" -ne 0 ]; then
    echo "FAIL: curl exit ${rc}: $(printf '%s' "${out}" | tail -n 1)"
    exit 1
fi
case " ${frames}" in
    *" RUN_ERROR "*)
        echo "FAIL: run ended with RUN_ERROR"
        exit 1
        ;;
    *" RUN_FINISHED "*)
        outcome="$(printf '%s\n' "${out}" | sed -n 's/.*"RUN_FINISHED".*"outcome": *{"type": *"\([a-z]*\)".*/\1/p')"
        echo "PASS: RUN_FINISHED (outcome ${outcome:-unknown}), body closed"
        ;;
    *)
        echo "FAIL: body ended without RUN_FINISHED"
        exit 1
        ;;
esac
