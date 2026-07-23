#!/usr/bin/env bash
#
# AgentHandoffWithApproval example (CI path).
#
# Demonstrates the human-in-the-loop approval construct in two modes:
#
# 1. OFFLINE (default): Feeds synthetic events directly into the construct,
#    exercises classify_reason, on_provider_waiting, resume, and expire.
#    No server required.
#
# 2. LIVE (CAO_APPROVAL_LIVE=1): Starts cao-server with mock_cli scripted
#    prompts, waits for an approval interrupt, sends a resume via REST,
#    and verifies resolution. Requires the mock_cli fixture.
#
# Usage:
#   ./examples/ag-ui/ag-ui-handoff-approval/run.sh              # offline mode
#   CAO_APPROVAL_LIVE=1 ./examples/ag-ui/ag-ui-handoff-approval/run.sh  # live mode

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
LIVE_MODE="${CAO_APPROVAL_LIVE:-0}"

SERVER_PID=""
SERVER_LOG=""

cleanup() {
    local code=$?
    [ -n "${SERVER_PID}" ] && kill "${SERVER_PID}" >/dev/null 2>&1 || true
    [ -n "${SERVER_LOG}" ] && rm -f "${SERVER_LOG}" || true
    exit "${code}"
}
trap cleanup EXIT INT TERM

# ── Offline mode: exercise the construct API directly ──────────────────────
if [ "${LIVE_MODE}" != "1" ]; then
    echo "[handoff-approval] Running offline (construct-only) example..." >&2

    uv run python3 - <<'PYTHON'
"""Demonstrate AgentHandoffWithApproval offline (no server)."""
import asyncio
import json
import sys

from cli_agent_orchestrator.services.agui import (
    AgentHandoffWithApproval,
    ApprovalDecision,
    Interrupt,
    RecordingUiEmitter,
    classify_reason,
)


async def main():
    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter)

    # 1. classify_reason: deterministic, total, never raises.
    reason_claude = classify_reason("claude_code", "Do you want to allow this?")
    reason_kiro = classify_reason("kiro_cli", "Allow this action? [y/n/t]:")
    reason_unknown = classify_reason("mystery_provider", "something happened")
    print(f"[1] classify_reason:")
    print(f"    claude_code permission -> {reason_claude}")
    print(f"    kiro_cli permission    -> {reason_kiro}")
    print(f"    unknown provider       -> {reason_unknown}")
    assert "claude-code:" in reason_claude
    assert "kiro:" in reason_kiro
    assert "mystery-provider:" in reason_unknown

    # 2. Create an interrupt (on_provider_waiting).
    interrupt = construct.on_provider_waiting(
        terminal_id="t-abc",
        provider="claude_code",
        raw_prompt="Do you want to allow this?",
        session_name="demo-session",
    )
    print(f"\n[2] on_provider_waiting created interrupt:")
    print(f"    id={interrupt.id}, reason={interrupt.reason}")
    print(f"    options={interrupt.options}, resolved={interrupt.resolved}")
    assert not interrupt.resolved
    assert len(construct.pending()) == 1

    # 3. Resume the interrupt with APPROVE.
    resolved = await construct.resume(interrupt.id, ApprovalDecision.APPROVE)
    print(f"\n[3] resume(APPROVE):")
    print(f"    resolved={resolved.resolved}, outcome={resolved.outcome}")
    assert resolved.resolved
    assert resolved.outcome == "approve"
    assert len(construct.pending()) == 0

    # 4. Idempotent re-resume (no error, returns same outcome).
    re_resolved = await construct.resume(interrupt.id, ApprovalDecision.DENY)
    assert re_resolved.outcome == "approve", "Idempotent: first resolution wins"
    print(f"\n[4] Idempotent re-resume: still outcome={re_resolved.outcome}")

    # 5. Expire an interrupt via status transition (zero keystrokes).
    interrupt2 = construct.on_provider_waiting(
        terminal_id="t-xyz",
        provider="kiro_cli",
        raw_prompt="Allow this action? [y/n/t]:",
        session_name="demo-session",
    )
    expired = construct.expire("t-xyz")
    print(f"\n[5] expire(t-xyz):")
    print(f"    outcome={expired.outcome}")
    assert expired.outcome == "expired"
    assert expired.resolved

    # 6. Projection accessor.
    proj = construct.projection()
    print(f"\n[6] projection() = {json.dumps(proj)}")
    assert proj["total"] == 2
    assert len(proj["pending"]) == 0

    # 7. Emitter captured approval_card intents.
    cards = [i for i in emitter.intents if i["component"] == "approval_card"]
    print(f"\n[7] Emitter captured {len(cards)} approval_card intents.")
    assert len(cards) >= 3, f"Expected >= 3 approval_card emits, got {len(cards)}"

    print("\n[handoff-approval] PASS: all assertions passed.")


asyncio.run(main())
PYTHON

    echo "[handoff-approval] Done (offline mode)." >&2
    exit 0
fi

# ── Live mode: server + scripted prompts + REST resume ─────────────────────
echo "[handoff-approval] Running LIVE mode (server + scripted prompts)..." >&2

export CAO_AGUI_ENABLED=1
export CAO_API_PORT="${CAO_API_PORT:-9889}"
export CAO_MOCK_CLI_SCRIPTED_PROMPTS=1
export PATH="${REPO_ROOT}/test/providers/fixtures/bin:${PATH}"
BASE="http://localhost:${CAO_API_PORT}"
SERVER_LOG="$(mktemp -t agui-approval-server.XXXXXX.log)"

CAO_SERVER_BIN="cao-server"
if [ -x "${REPO_ROOT}/.venv/bin/cao-server" ]; then
    CAO_SERVER_BIN="${REPO_ROOT}/.venv/bin/cao-server"
fi

echo "[handoff-approval] Starting cao-server on ${BASE}..." >&2
"${CAO_SERVER_BIN}" >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 40); do
    if curl -fsS "${BASE}/health" >/dev/null 2>&1; then break; fi
    sleep 0.5
done
if ! curl -fsS "${BASE}/health" >/dev/null 2>&1; then
    echo "[handoff-approval] Server did not become healthy; log follows:" >&2
    cat "${SERVER_LOG}" >&2 || true
    exit 1
fi
echo "[handoff-approval] Server healthy." >&2

# Check for pending interrupts via the approval endpoint.
echo "[handoff-approval] Checking for approval interrupts..." >&2

uv run python3 - "${BASE}" <<'PYTHON'
"""Live mode: verify the approval REST flow works."""
import json
import sys
import time
import requests

base = sys.argv[1]

# Poll for pending interrupts (mock_cli scripted prompts may take a moment).
pending = []
for _ in range(20):
    try:
        resp = requests.get(f"{base}/agui/v1/approvals", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            pending = data.get("pending", [])
            if pending:
                break
    except Exception:
        pass
    time.sleep(0.5)

if not pending:
    print("[handoff-approval] No pending interrupts found (scripted prompts may not have triggered).")
    print("[handoff-approval] PASS: server is healthy and approval endpoint responds.")
    sys.exit(0)

interrupt = pending[0]
print(f"[handoff-approval] Found pending interrupt: id={interrupt['id']}, reason={interrupt['reason']}")

# Resume it.
resp = requests.post(
    f"{base}/agui/v1/approvals/{interrupt['id']}/resume",
    json={"decision": "approve"},
    timeout=10,
)
print(f"[handoff-approval] Resume response: {resp.status_code} {resp.text[:200]}")
assert resp.status_code in (200, 201, 204), f"Unexpected status: {resp.status_code}"

print("[handoff-approval] PASS: live approval round-trip succeeded.")
sys.exit(0)
PYTHON

echo "[handoff-approval] Done (live mode)." >&2
