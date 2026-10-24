#!/usr/bin/env bash
#
# Memory & learning example (offline, CI-safe).
#
# Part 1 -- persistent memory + cross-session injection, against the REAL and
# unmodified production code:
#   1. Storing a durable fact with the `memory_store` MCP tool.
#   2. Recalling it explicitly with the `memory_recall` MCP tool.
#   3. Cross-session injection: a brand-new terminal's first message gets the
#      <cao-memory> block prepended by inject_memory_context() -- the exact
#      function services/terminal_service.py runs before every terminal's
#      first message -- proving the fact persisted from one session into a
#      second session that never stored anything itself.
#
# Part 2 -- self-learning loop, against the REAL and unmodified production code:
#   4. Outcome capture and retrospector reads are disabled until
#      `memory.learning_enabled` is turned on (opt-in, off by default).
#   5. Workers report outcomes for a small fixed task corpus (a recurring
#      failure + a success) via the real OutcomeService.
#   6. A retrospector distills ONE lesson from the pattern and stores it in
#      the `developer` profile's AGENT scope via the store_lesson MCP tool.
#   7. Ambient first-message injection excludes agent scope by design; a
#      later `developer` terminal recalls the lesson explicitly instead.
#   8. `cao memory promote developer` prints a reviewable dry-run plan --
#      `--apply` is never passed (see README.md "Non-goals").
#
# Runs against a throwaway CAO_HOME_DIR (created below, removed on exit), so
# it never touches your real memory or outcome store. No cao-server, tmux, or
# live provider CLI required -- see README.md "Run it live" for the full,
# real-terminal walkthrough.
#
# Usage:
#   ./examples/memory-learning/run.sh

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

echo "[memory-learning] sandboxed CAO_HOME_DIR: ${DEMO_HOME}" >&2

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

echo "[memory-learning] Part 1 cross-check via the cao CLI (list / show)..." >&2
uv run cao memory list --scope global
uv run cao memory show fast-test-runs --scope global

echo "[memory-learning] Part 1 PASS: store -> recall -> cross-session injection all verified." >&2

uv run python3 - <<'PYTHON'
"""Part 2: self-learning loop, against the REAL and unmodified production code.

Continues in the SAME sandboxed CAO_HOME_DIR as Part 1: workers report
outcomes for a small fixed task corpus, a retrospector distills one lesson
from a recurring failure pattern and stores it in the developer profile's
AGENT scope via the store_lesson MCP tool, and a later developer terminal
recalls it explicitly (ambient first-message injection deliberately excludes
agent scope -- see README.md "How it works" #6).

store_lesson and the agent-scope memory_recall calls resolve "who is calling"
via _get_terminal_context_from_env(), which normally asks a live cao-server
over HTTP for the calling terminal's registered profile. This offline demo
has no server, so -- exactly like test/services/test_learning_loop_e2e.py --
it supplies that context directly. Nothing about the memory/learning logic
itself is faked.
"""
import asyncio
import os
from contextlib import contextmanager
from unittest.mock import patch

from cli_agent_orchestrator.clients.database import (
    MemoryMetadataModel,
    SessionLocal,
    create_terminal,
    init_db,
)
from cli_agent_orchestrator.mcp_server import server as srv
from cli_agent_orchestrator.mcp_server.server import list_outcomes, memory_recall, store_lesson
from cli_agent_orchestrator.services.outcome_service import (
    LEARNING_DISABLED_MESSAGE,
    LearningDisabledError,
    OutcomeService,
)
from cli_agent_orchestrator.services.terminal_service import inject_memory_context

WORKER_PROFILE = "developer"
SESSION_NAME = "memory-learning-outcomes"
LESSON_KEY = "paginate-limit-boundary"

# Synthetic task labels/notes only -- never transcripts, logs, or credentials
# (see README.md "Privacy boundary").
TASK_CORPUS = [
    ("implement paginated /items list endpoint", False, 40,
     "Off-by-one: limit equal to page size returned one extra row."),
    ("implement paginated /orders list endpoint", False, 40,
     "Same off-by-one as /items: limit equal to page size returned one extra row."),
    ("implement paginated /users list endpoint", True, 95, ""),
]

LESSON_TEXT = (
    "Validate that `limit` equals the page size exactly before returning "
    "results; two endpoints silently returned one extra row on that "
    "boundary. Applies when: implementing paginated list endpoints."
)


def _ctx(terminal_id: str, agent_profile: str) -> dict:
    return {
        "terminal_id": terminal_id,
        "session_name": SESSION_NAME,
        "agent_profile": agent_profile,
        "provider": "claude_code",
        "cwd": None,
    }


@contextmanager
def _as_caller(terminal_id: str, agent_profile: str):
    """Stand in for the calling terminal's identity (see module docstring)."""
    with patch.object(
        srv, "_get_terminal_context_from_env", return_value=_ctx(terminal_id, agent_profile)
    ):
        yield


async def main():
    init_db()
    outcomes = OutcomeService()

    # ---- Gate check: outcome capture AND retrospector reads are disabled
    # until memory.learning_enabled is explicitly turned on.
    os.environ.pop("CAO_MEMORY_LEARNING_ENABLED", None)
    try:
        outcomes.record_outcome(session_name=SESSION_NAME, task_label="probe", success=True)
        raise AssertionError("expected LearningDisabledError while learning is disabled")
    except LearningDisabledError as e:
        print(f"[6] record_outcome while learning disabled -> rejected: {e}")

    disabled_read = await list_outcomes(
        session_name=SESSION_NAME, agent_profile=None, workflow_name=None, limit=50
    )
    print(f"[7] list_outcomes while learning disabled -> {disabled_read}")
    assert disabled_read == {
        "success": False,
        "disabled": True,
        "error": LEARNING_DISABLED_MESSAGE,
        "outcomes": [],
    }

    os.environ["CAO_MEMORY_LEARNING_ENABLED"] = "true"
    print("[8] memory.learning_enabled=true (CAO_MEMORY_LEARNING_ENABLED)")

    # ---- Record the fixed task corpus: a recurring failure + one success.
    for label, ok, score, notes in TASK_CORPUS:
        outcomes.record_outcome(
            session_name=SESSION_NAME,
            workflow_name="memory-learning-demo",
            task_label=label,
            agent_profile=WORKER_PROFILE,
            success=ok,
            score=score,
            friction_notes=notes,
        )
    listed = await list_outcomes(
        session_name=SESSION_NAME, agent_profile=None, workflow_name=None, limit=50
    )
    recorded = listed["outcomes"]
    failures = [o for o in recorded if not o["success"]]
    successes = [o for o in recorded if o["success"]]
    print(
        f"[9] list_outcomes -> {len(recorded)} recorded, {len(failures)} recurring "
        f"failures (score {failures[0]['score']}), {len(successes)} success "
        f"(score {successes[0]['score']})"
    )
    assert len(recorded) == 3 and len(failures) == 2 and len(successes) == 1

    # ---- BEFORE: the developer profile has no agent-scope lesson yet.
    with _as_caller("memory-learning-worker-1", WORKER_PROFILE):
        before = await memory_recall(
            query=LESSON_KEY,
            scope="agent",
            memory_type=None,
            limit=10,
            search_mode="hybrid",
            sort_by="recency",
            include_related=False,
        )
    print(f"[10] memory_recall(scope=agent) BEFORE the lesson -> {before['memories']}")
    assert before["success"] and before["memories"] == []

    # ---- Retrospector reads the pattern and stores ONE lesson. Called AS
    # the retrospector: the caller whose profile must declare the
    # store_lesson capability (see agent_store/retrospector.md).
    with _as_caller("memory-learning-retrospector", "retrospector"):
        lesson = await store_lesson(
            target_agent_profile=WORKER_PROFILE,
            content=LESSON_TEXT,
            key=LESSON_KEY,
            tags="api,pagination",
        )
    print(f"[11] store_lesson -> {lesson}")
    assert lesson["success"] and lesson["scope"] == "agent" and lesson["scope_id"] == WORKER_PROFILE

    # ---- A LATER developer terminal. Ambient injection still carries Part
    # 1's GLOBAL fact but excludes the AGENT-scope lesson by design (see
    # README.md "How it works" #6) -- explicit recall is what finds it.
    create_terminal(
        terminal_id="memory-learning-worker-2",
        tmux_session="cao-memory-learning-worker-2",
        tmux_window="win1",
        provider="claude_code",
        agent_profile=WORKER_PROFILE,
    )
    next_task = "Implement paginated /accounts list endpoint."
    injected = inject_memory_context(next_task, "memory-learning-worker-2")
    print("[12] a later developer terminal's FIRST message, after inject_memory_context():")
    print("----")
    print(injected)
    print("----")
    assert "fast-test-runs" in injected  # Part 1's global fact still flows in...
    assert LESSON_KEY not in injected and "page size" not in injected  # ...the lesson does not

    with _as_caller("memory-learning-worker-2", WORKER_PROFILE):
        after = await memory_recall(
            query=LESSON_KEY,
            scope="agent",
            memory_type=None,
            limit=10,
            search_mode="hybrid",
            sort_by="recency",
            include_related=False,
        )
    print(f"[13] memory_recall(scope=agent) AFTER the lesson -> {after['memories']}")
    assert after["success"] and len(after["memories"]) == 1
    assert after["memories"][0]["key"] == LESSON_KEY
    assert "Applies when:" in after["memories"][0]["content"]

    # ---- Reinforce (simulates the lesson being recalled across several
    # later runs) so it clears the promotion plan's --min-recalls default.
    with SessionLocal() as db:
        row = db.query(MemoryMetadataModel).filter_by(key=LESSON_KEY, scope="agent").one()
        row.access_count = 4
        db.commit()
    print("[14] reinforced: access_count=4 (simulates 4 later recalls)")


asyncio.run(main())
PYTHON

# Promotion needs a WRITABLE profile file; built-in package profiles are
# refused (see cli/commands/memory.py _reject_builtin_profile_path). Copy the
# real built-in developer profile into the sandboxed, isolated agent-store --
# nothing outside CAO_HOME_DIR is read or written.
mkdir -p "${DEMO_HOME}/agent-store"
cp "${REPO_ROOT}/src/cli_agent_orchestrator/agent_store/developer.md" "${DEMO_HOME}/agent-store/developer.md"

echo "[memory-learning] Part 2 promotion dry run (never --apply -- see README.md Non-goals)..." >&2
uv run cao memory promote developer

echo "[memory-learning] Part 2 PASS: gate check -> outcome capture -> retrospector lesson -> scope boundary + recall -> promotion dry-run all verified." >&2
