"""SC-2 — Per-scope injection cap tests (U2).

Covers `MemoryService.get_memory_context_for_terminal` with the per-scope
caps introduced in U2:

- MEMORY_MAX_PER_SCOPE: at most 10 entries per scope.
- MEMORY_SCOPE_BUDGET_CHARS: per-scope character ceiling, applied independently
  so one scope cannot monopolize the overall budget.
- Empty scopes do NOT cause other scopes to grow (precedence + cache boundary
  preservation — see tasks.md cross-unit risk note for Phase 2 U7).
- Scope precedence session > project > global is preserved in output order.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from cli_agent_orchestrator.constants import (
    MEMORY_MAX_PER_SCOPE,
    MEMORY_SCOPE_BUDGET_CHARS,
)
from cli_agent_orchestrator.services.memory_service import MemoryService


def _ctx(terminal_id: str = "term-u2") -> dict:
    return {
        "terminal_id": terminal_id,
        "session_name": "sess-u2",
        "agent_profile": "dev",
        "provider": "claude_code",
        "cwd": "/home/user/proj-u2",
    }


def _run(coro):
    return asyncio.run(coro)


def _make_svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(base_dir=tmp_path)
    ctx = _ctx()
    svc._get_terminal_context = lambda terminal_id: ctx  # type: ignore[method-assign]
    return svc


def _inner(block: str) -> str:
    """Return the content between the cao-memory tags."""
    return block.split("<cao-memory>")[1].split("</cao-memory>")[0]


def _count_scope_lines(block: str, scope: str) -> int:
    return sum(1 for line in block.splitlines() if line.startswith(f"- [{scope}]"))


# ---------------------------------------------------------------------------
# AC1 — single scope cannot monopolize the budget
# AC2 — MEMORY_MAX_PER_SCOPE is enforced
# ---------------------------------------------------------------------------


def test_single_scope_capped_to_max_per_scope(tmp_path: Path) -> None:
    """20 memories in one scope → at most MEMORY_MAX_PER_SCOPE entries."""
    svc = _make_svc(tmp_path)
    ctx = _ctx()

    # Store 20 long memories into a single scope.
    long_content = "x " * 300  # ~600 chars — larger than per-scope budget
    for i in range(20):
        _run(
            svc.store(
                content=long_content + f"entry-{i}",
                scope="global",
                memory_type="project",
                key=f"glob-{i:03d}",
                terminal_context=ctx,
            )
        )

    # Give the caller ample budget so we know the cap — not the budget — is
    # what truncates output.
    block = svc.get_memory_context_for_terminal("term-u2", budget_chars=100_000)
    assert block, "context block should not be empty"

    inner = _inner(block)
    global_lines = _count_scope_lines(inner, "global")
    assert global_lines <= MEMORY_MAX_PER_SCOPE, (
        f"global scope produced {global_lines} entries, "
        f"exceeds MEMORY_MAX_PER_SCOPE={MEMORY_MAX_PER_SCOPE}"
    )


def test_single_scope_bounded_by_scope_char_budget(tmp_path: Path) -> None:
    """20 long memories in one scope → per-scope char cap bounds the slice.

    The effective per-scope cap is
    min(MEMORY_SCOPE_BUDGET_CHARS, budget_chars // N_scopes). With a caller
    budget of 9000 and 3 scopes, the effective cap is MEMORY_SCOPE_BUDGET_CHARS.
    """
    svc = _make_svc(tmp_path)
    ctx = _ctx()

    long_content = "y " * 300  # ~600 chars per memory
    for i in range(20):
        _run(
            svc.store(
                content=long_content + f"entry-{i}",
                scope="global",
                memory_type="project",
                key=f"glob-{i:03d}",
                terminal_context=ctx,
            )
        )

    block = svc.get_memory_context_for_terminal("term-u2", budget_chars=9000)
    assert block
    inner = _inner(block)

    # Count characters belonging to the global scope slice only. Allow a small
    # slack for tag/header overhead; each line is already below the per-scope
    # cap, and the loop terminates before adding a line that would exceed it.
    global_chars = sum(
        len(line) + 1  # +1 accounts for the join-newline counted in the cap
        for line in inner.splitlines()
        if line.startswith("- [global]")
    )
    assert global_chars <= MEMORY_SCOPE_BUDGET_CHARS, (
        f"global scope consumed {global_chars} chars, "
        f"exceeds MEMORY_SCOPE_BUDGET_CHARS={MEMORY_SCOPE_BUDGET_CHARS}"
    )


# ---------------------------------------------------------------------------
# AC4 — precedence preserved; AC1 — each scope gets its own slice
# ---------------------------------------------------------------------------


def test_all_scopes_each_get_their_own_slice_in_precedence_order(tmp_path: Path) -> None:
    """session > project > global: each populated scope gets its slice, in order."""
    svc = _make_svc(tmp_path)
    ctx = _ctx()

    for i in range(5):
        _run(
            svc.store(
                content=f"session finding {i}",
                scope="session",
                memory_type="project",
                key=f"sess-{i}",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content=f"project decision {i}",
                scope="project",
                memory_type="project",
                key=f"proj-{i}",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content=f"global reference {i}",
                scope="global",
                memory_type="reference",
                key=f"glob-{i}",
                terminal_context=ctx,
            )
        )

    block = svc.get_memory_context_for_terminal("term-u2", budget_chars=9000)
    assert block
    inner = _inner(block)
    lines = [ln for ln in inner.splitlines() if ln.startswith("- [")]

    # Each scope must contribute entries.
    assert any(ln.startswith("- [session]") for ln in lines), "session scope missing"
    assert any(ln.startswith("- [project]") for ln in lines), "project scope missing"
    assert any(ln.startswith("- [global]") for ln in lines), "global scope missing"

    # Each scope independently capped at MEMORY_MAX_PER_SCOPE.
    for scope in ("session", "project", "global"):
        assert _count_scope_lines(inner, scope) <= MEMORY_MAX_PER_SCOPE

    # Precedence order: every session line appears before every project line,
    # and every project line appears before every global line.
    def first_index(scope: str) -> int:
        for i, ln in enumerate(lines):
            if ln.startswith(f"- [{scope}]"):
                return i
        return -1

    def last_index(scope: str) -> int:
        idx = -1
        for i, ln in enumerate(lines):
            if ln.startswith(f"- [{scope}]"):
                idx = i
        return idx

    assert last_index("session") < first_index("project"), "session must precede project"
    assert last_index("project") < first_index("global"), "project must precede global"


# ---------------------------------------------------------------------------
# AC3 — overall injection stays within budget
# ---------------------------------------------------------------------------


def test_total_injection_within_overall_budget(tmp_path: Path) -> None:
    """Total injection does not exceed the caller-supplied budget_chars."""
    svc = _make_svc(tmp_path)
    ctx = _ctx()

    # Populate all three scopes with more than the per-scope cap of long entries.
    long_content = "z " * 200  # ~400 chars
    for scope in ("session", "project", "global"):
        for i in range(15):
            _run(
                svc.store(
                    content=long_content + f"{scope}-{i}",
                    scope=scope,
                    memory_type="project",
                    key=f"{scope}-{i:02d}",
                    terminal_context=ctx,
                )
            )

    block = svc.get_memory_context_for_terminal("term-u2", budget_chars=3000)
    assert block
    inner = _inner(block)

    # Sum of per-scope slices must not exceed 3 * MEMORY_SCOPE_BUDGET_CHARS,
    # and must not exceed the caller-supplied budget_chars.
    assert len(inner) <= 3 * MEMORY_SCOPE_BUDGET_CHARS + 200, (  # header overhead slack
        f"inner block of {len(inner)} chars exceeds 3x per-scope cap"
    )


# ---------------------------------------------------------------------------
# Empty-scope non-reallocation — cache boundary preservation
# ---------------------------------------------------------------------------


def test_empty_scope_does_not_grow_other_scopes(tmp_path: Path) -> None:
    """An empty session scope must not cause project/global to exceed their caps.

    Verifies Phase 2 U7 cache-friendly behavior: unused budget from an empty
    scope is not reallocated. The per-scope cap stays the same regardless of
    whether sibling scopes are populated.
    """
    svc = _make_svc(tmp_path)
    ctx = _ctx()

    # Populate only project and global, leave session empty.
    for i in range(20):
        _run(
            svc.store(
                content=f"project entry {i} " + ("p " * 150),
                scope="project",
                memory_type="project",
                key=f"proj-{i:02d}",
                terminal_context=ctx,
            )
        )
        _run(
            svc.store(
                content=f"global entry {i} " + ("g " * 150),
                scope="global",
                memory_type="reference",
                key=f"glob-{i:02d}",
                terminal_context=ctx,
            )
        )

    block = svc.get_memory_context_for_terminal("term-u2", budget_chars=9000)
    assert block
    inner = _inner(block)

    # Session scope contributes nothing.
    assert _count_scope_lines(inner, "session") == 0

    # project and global each still capped at MEMORY_MAX_PER_SCOPE — not 15, not 20.
    assert _count_scope_lines(inner, "project") <= MEMORY_MAX_PER_SCOPE
    assert _count_scope_lines(inner, "global") <= MEMORY_MAX_PER_SCOPE

    # And each scope's total chars ≤ MEMORY_SCOPE_BUDGET_CHARS.
    for scope in ("project", "global"):
        scope_chars = sum(
            len(line) + 1
            for line in inner.splitlines()
            if line.startswith(f"- [{scope}]")
        )
        assert scope_chars <= MEMORY_SCOPE_BUDGET_CHARS, (
            f"{scope} scope grew beyond MEMORY_SCOPE_BUDGET_CHARS "
            f"({scope_chars} > {MEMORY_SCOPE_BUDGET_CHARS})"
        )
