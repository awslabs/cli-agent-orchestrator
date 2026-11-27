#!/usr/bin/env bash
#
# Exercises the full `cao schedule` lifecycle against the local-task-demo
# flow defined in this directory:
#
#   add -> list -> run (skip) -> disable -> list -> enable -> run (allow) -> remove
#
# Isolated from any real CAO state via CAO_HOME_DIR (see constants.py) so
# this is safe to run repeatedly without touching ~/.aws/cli-agent-orchestrator.
# `cao schedule add/list/disable/enable/remove` only touch this isolated
# database -- no `cao-server` is required for any step, including `run`:
# that command bootstraps its own event pipeline in-process (see
# `_run_flow_with_pipeline` in cli/commands/schedule.py). `cao-server` (and a
# durable CAO_HOME_DIR) only matter for unattended, schedule-driven runs.
#
# Requires: `cao` on PATH, tmux. The "run (allow)" step launches a real
# terminal session for the flow's agent_profile/provider (default:
# developer/kiro_cli) -- install the corresponding CLI to see that step
# complete; the rest of the lifecycle does not need it.
#
# Usage: ./run-lifecycle.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CREATED_HOME_DIR=0
if [ -z "${CAO_HOME_DIR:-}" ]; then
    CAO_HOME_DIR="$(mktemp -d -t cao-flow-example.XXXXXX)"
    export CAO_HOME_DIR
    CREATED_HOME_DIR=1
fi

FLOW_NAME="local-task-demo"
SESSION_NAME="cao-flow-${FLOW_NAME}"
SKIP_FLAG="${CAO_EXAMPLE_SKIP_FLAG:-/tmp/cao-examples-flow/skip}"

cleanup() {
    local code=$?
    rm -f "${SKIP_FLAG}"
    cao schedule remove "${FLOW_NAME}" >/dev/null 2>&1 || true
    # `cao shutdown --session` calls cao-server's HTTP API, which this script
    # deliberately never starts -- it would silently no-op here. Kill the
    # tmux session directly instead (safe: the isolated CAO_HOME_DIR has no
    # config.json, so the backend that created it was tmux by default).
    tmux kill-session -t "${SESSION_NAME}" >/dev/null 2>&1 || true
    if [ "${CREATED_HOME_DIR}" = "1" ]; then
        rm -rf "${CAO_HOME_DIR}"
    fi
    exit "${code}"
}
trap cleanup EXIT INT TERM

echo "[flow] CAO_HOME_DIR=${CAO_HOME_DIR} (isolated)"

echo "[flow] 1/8 add"
cao schedule add local-task.md

echo "[flow] 2/8 list -- cron schedule, next run, enabled=yes"
cao schedule list

echo "[flow] 3/8 run -- skip path (gate.sh sees the flag file)"
mkdir -p "$(dirname "${SKIP_FLAG}")"
touch "${SKIP_FLAG}"
cao schedule run "${FLOW_NAME}"
rm -f "${SKIP_FLAG}"

echo "[flow] 4/8 disable"
cao schedule disable "${FLOW_NAME}"

echo "[flow] 5/8 list -- enabled=no"
cao schedule list

echo "[flow] 6/8 enable -- next run recalculated from now"
cao schedule enable "${FLOW_NAME}"

echo "[flow] 7/8 run -- allow path (launches session ${SESSION_NAME})"
cao schedule run "${FLOW_NAME}"

echo "[flow] 8/8 remove"
cao schedule remove "${FLOW_NAME}"

echo "[flow] lifecycle complete"
