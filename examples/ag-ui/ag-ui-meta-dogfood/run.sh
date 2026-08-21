#!/usr/bin/env bash
#
# Meta-dogfood capture (AC5 task 19.2): the supervisor->developer->reviewer
# audit fleet rendered on the live AG-UI stream. Keyless + deterministic.
#
# Usage:
#   ./examples/ag-ui/ag-ui-meta-dogfood/run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

export CAO_AGUI_ENABLED="${CAO_AGUI_ENABLED:-1}"
echo "[meta-dogfood] Capturing the audit fleet on the live AG-UI stream..." >&2
uv run python "${REPO_ROOT}/examples/ag-ui/ag-ui-meta-dogfood/capture.py"
