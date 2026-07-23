#!/usr/bin/env bash
#
# CrossProviderStateSync example.
#
# Demonstrates the fold-based cross-provider state sync construct by feeding
# synthetic AG-UI frames directly (no live server required). This dogfoods
# the library's own classes: RecordingUiEmitter + CrossProviderStateSync.
#
# Usage:
#   ./examples/ag-ui/ag-ui-cross-provider-sync/run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

cleanup() {
    local code=$?
    exit "${code}"
}
trap cleanup EXIT INT TERM

echo "[cross-provider-sync] Running CrossProviderStateSync example..." >&2

uv run python3 - <<'PYTHON'
"""Demonstrate CrossProviderStateSync fold composition and convergence."""
import json
import sys

from cli_agent_orchestrator.services.agui import (
    RecordingUiEmitter,
    CrossProviderStateSync,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STATE_SNAPSHOT,
    AGUI_STATE_DELTA,
)

emitter = RecordingUiEmitter()
sync = CrossProviderStateSync(emitter)

# 1. Before any snapshot: shared_state is None, providers_seen is empty.
assert sync.shared_state() is None, "No state before snapshot"
assert sync.providers_seen() == [], "No providers before snapshot"
print("[1] Before snapshot: state=None, providers=[]")

# 2. Feed a STATE_SNAPSHOT with a multi-provider fleet.
fleet_state = {
    "sessions": [
        {"name": "s1", "status": "active"},
    ],
    "terminals": [
        {"id": "t1", "provider": "claude_code", "status": "working"},
        {"id": "t2", "provider": "kiro_cli", "status": "working"},
        {"id": "t3", "provider": "codex", "status": "idle"},
        {"id": "t4", "provider": "claude_code", "status": "working"},
    ],
    "counts": {"sessions": 1, "terminals": 4},
}
sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet_state}, event_id=None)
print("\n[2] After STATE_SNAPSHOT:")
print(f"    providers_seen = {sync.providers_seen()}")
print(f"    converges_with(fleet_state) = {sync.converges_with(fleet_state)}")

# 3. Apply a STATE_DELTA: one terminal finishes.
delta_ops = [
    {"op": "replace", "path": "/terminals/2/status", "value": "terminated"},
]
sync.handle_frame(AGUI_STATE_DELTA, {"delta": delta_ops}, event_id=None)
print("\n[3] After STATE_DELTA (t3 terminated):")
state = sync.shared_state()
print(f"    t3 status = {state['terminals'][2]['status']}")

# The local state should NO LONGER converge with the original snapshot.
assert not sync.converges_with(fleet_state), "Should NOT converge after delta"
print(f"    converges_with(original) = False (expected)")

# 4. Build the expected state after the delta and verify convergence.
expected = json.loads(json.dumps(fleet_state))
expected["terminals"][2]["status"] = "terminated"
assert sync.converges_with(expected), "Should converge with updated state"
print(f"    converges_with(updated)  = True (expected)")

# 5. Seen-Set Dedup: replay an id-bearing frame (no effect on state frames).
sync.handle_frame(AGUI_STATE_DELTA, {"delta": delta_ops}, event_id="ev-replay")
sync.handle_frame(AGUI_STATE_DELTA, {"delta": delta_ops}, event_id="ev-replay")
# State frames (event_id=None) are always folded, so the second delta above
# would be a different id. The id-bearing replay is skipped.
assert sync.converges_with(expected), "Still converges after dedup"
print("\n[4] Seen-Set Dedup: replayed id-bearing frame had no effect.")

# 6. Projection accessor.
proj = sync.projection()
assert proj == expected, "projection() == shared_state()"
print(f"\n[5] projection() matches shared_state()")

# 7. Providers seen remains stable (3 unique providers).
providers = sync.providers_seen()
assert providers == ["claude_code", "codex", "kiro_cli"], f"Unexpected: {providers}"
print(f"    providers_seen = {providers}")

print("\n[cross-provider-sync] PASS: all assertions passed, convergence verified.")
sys.exit(0)
PYTHON

echo "[cross-provider-sync] Done." >&2
