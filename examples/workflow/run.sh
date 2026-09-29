#!/usr/bin/env bash
#
# Non-interactive entry point for the workflow example.
#
# Copies workflow.py into the configured CAO workflows directory (idempotent —
# `cao workflow run` resolves a bare name against that directory, never the
# caller's cwd), validates it, then submits a run and follows it to a
# terminal state. No prompts; the exit code mirrors `cao workflow run`'s own
# contract (0 completed, 1 failed/cancelled).
#
# Usage:
#   ./run.sh [target] [run-id] [concurrency]
#   ./run.sh myapp demo-1 3
#
# Requires: cao-server already running, and claude_code available and
# authenticated (the example's steps use provider="claude_code", agent=
# "reviewer" — a built-in profile, no `cao install` needed).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-myapp}"
RUN_ID="${2:-workflow-demo-$$}"
CONCURRENCY="${3:-3}"
WORKFLOWS_DIR="${CAO_HOME_DIR:-$HOME/.aws/cli-agent-orchestrator}/workflows"

mkdir -p "${WORKFLOWS_DIR}"
cp "${HERE}/workflow.py" "${WORKFLOWS_DIR}/workflow.py"

echo "[workflow-example] validating ${WORKFLOWS_DIR}/workflow.py" >&2
cao workflow validate "${WORKFLOWS_DIR}/workflow.py"

echo "[workflow-example] running run-id=${RUN_ID} target=${TARGET} concurrency=${CONCURRENCY}" >&2
cao workflow run workflow \
    --run-id "${RUN_ID}" \
    --input "target=${TARGET}" \
    --input "concurrency=${CONCURRENCY}" \
    --json
