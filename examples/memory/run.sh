#!/usr/bin/env bash
#
# Persistent memory + cross-session injection example (offline, CI-safe).
#
# Demonstrates, against the REAL and unmodified production code:
#   1. Storing a durable fact with the `memory_store` MCP tool.
#   2. Recalling it explicitly with the `memory_recall` MCP tool.
#   3. Cross-session injection: a brand-new terminal's first message gets the
#      <cao-memory> block prepended by inject_memory_context() -- the exact
#      function services/terminal_service.py runs before every terminal's
#      first message -- proving the fact persisted from one session into a
#      second session that never stored anything itself.
#
# Runs against a throwaway CAO_HOME_DIR (created below, removed on exit), so
# it never touches your real memory store. No cao-server, tmux, or live
# provider CLI required -- see README.md "Run it live" for the full,
# real-terminal walkthrough.
#
# Usage:
#   ./examples/memory/run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO_HOME="$(mktemp -d)"
export CAO_HOME_DIR="${DEMO_HOME}"

cleanup() {
    local code=$?
    rm -rf "${DEMO_HOME}"
    exit "${code}"
}
trap cleanup EXIT INT TERM

echo "[memory] sandboxed CAO_HOME_DIR: ${DEMO_HOME}" >&2

cd "${REPO_ROOT}"

uv run python3 - <<'PYTHON'
"""Persistent memory + cross-session injection, against the REAL production code.

No cao-server, tmux, or provider CLI needed: memory_store/memory_recall are called
directly (see README "Note on CLI vs. MCP tool"), and inject_memory_context() only
needs a terminal registered in the database -- not a live pty. Runs against the
throwaway CAO_HOME_DIR that run.sh exported, so nothing here touches a real store.
"""
import asyncio

from cli_agent_orchestrator.clients.database import create_terminal, init_db
from cli_agent_orchestrator.mcp_server.server import memory_recall, memory_store
from cli_agent_orchestrator.services.terminal_service import inject_memory_context

FACT = (
    "Run tests with `pytest --no-cov` in this repo -- the default coverage "
    "flag adds 30-50% overhead per test."
)
KEY = "fast-test-runs"


async def main():
    init_db()

    # Session 1: an agent learns something mid-session and stores it.
    create_terminal(
        terminal_id="memory-demo-session-1",
        tmux_session="cao-memory-demo-1",
        tmux_window="win1",
        provider="claude_code",
        agent_profile="developer",
    )

    # NOTE: memory_store/memory_recall declare their defaults as pydantic
    # Field(...) objects -- resolved by FastMCP's own calling convention when
    # invoked over MCP. Called directly like this, every argument must be
    # passed explicitly, or the parameter binds to the raw FieldInfo object
    # instead of the value it describes.
    store_result = await memory_store(
        content=FACT,
        scope="global",
        memory_type="feedback",
        key=KEY,
        tags="testing,pytest",
    )
    print(f"[1] memory_store -> {store_result}")
    assert store_result["success"], store_result

    recall_result = await memory_recall(
        query=KEY,
        scope="global",
        memory_type=None,
        limit=10,
        search_mode="hybrid",
        sort_by="recency",
        include_related=False,
    )
    print(f"[2] memory_recall -> {recall_result}")
    assert recall_result["success"] and recall_result["memories"], recall_result
    assert recall_result["memories"][0]["content"] == FACT

    # Session 2: a BRAND NEW terminal that never called memory_store itself.
    create_terminal(
        terminal_id="memory-demo-session-2",
        tmux_session="cao-memory-demo-2",
        tmux_window="win1",
        provider="claude_code",
        agent_profile="developer",
    )
    print("[3] registered session 2 -- a brand-new terminal that stored nothing")

    first_message = (
        "I need to add a test for the new memory example. What's the "
        "fastest way to run just that test file?"
    )
    injected = inject_memory_context(first_message, "memory-demo-session-2")
    print("[4] session 2's FIRST message, after inject_memory_context():")
    print("----")
    print(injected)
    print("----")
    assert "<cao-memory>" in injected and "</cao-memory>" in injected
    assert KEY in injected and "pytest --no-cov" in injected
    assert injected.endswith(first_message)

    second_message = "Great, thanks!"
    not_injected = inject_memory_context(second_message, "memory-demo-session-2")
    print(f"[5] session 2's SECOND message, unchanged: {not_injected!r}")
    assert not_injected == second_message
    assert "<cao-memory>" not in not_injected


asyncio.run(main())
PYTHON

echo "[memory] cross-checking via the cao CLI (list / show)..." >&2
uv run cao memory list --scope global
uv run cao memory show fast-test-runs --scope global

echo "[memory] PASS: store -> recall -> cross-session injection all verified." >&2
