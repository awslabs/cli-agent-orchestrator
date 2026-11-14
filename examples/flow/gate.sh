#!/usr/bin/env bash
#
# Deterministic gating script for local-task.md.
#
# Schedule gating contract (services/flow_service.py execute_flow, ~lines
# 226-257): stdout must be exactly one JSON object shaped
# {"execute": <bool>, "output": <dict>}. "execute": false skips the flow (no
# session is launched); "output" is merged into the flow file's
# [[placeholder]] template when "execute" is true.
#
# Allow vs skip is a plain file-presence check, so both paths are demonstrable
# without editing this script -- see run-lifecycle.sh steps 3 and 7:
#   default:                       execute=true  (allow)
#   CAO_EXAMPLE_SKIP_FLAG exists:  execute=false (skip)
set -euo pipefail

SKIP_FLAG="${CAO_EXAMPLE_SKIP_FLAG:-/tmp/cao-examples-flow/skip}"
LOG_FILE="${CAO_EXAMPLE_LOG_FILE:-/tmp/cao-examples-flow/local-task.log}"

if [ -e "${SKIP_FLAG}" ]; then
    echo '{"execute": false, "output": {}}'
    exit 0
fi

mkdir -p "$(dirname "${LOG_FILE}")"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"execute": true, "output": {"timestamp": "%s", "log_file": "%s"}}\n' \
    "${TIMESTAMP}" "${LOG_FILE}"
