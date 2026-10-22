"""Guard test: every autonomous pane dispatch must confirm the submit (#78).

``terminal_service.send_input`` / ``dispatch_input`` deliver a message to a TUI
open-loop: paste-buffer, a fixed sleep, then ``Enter`` (``clients/tmux.py``).
The sleep only *guesses* when the TUI is input-ready; when it guesses wrong the
Enter is swallowed, the text sits unsubmitted in the composer, and **nothing
raises**. A caller that treats "the call returned" as "the work started" --
which is every caller that does not block on a reply -- then loses the message
silently. That is GH#78 on the inbox path, and it is a *class*: the recovery
helper (``redeliver_dropped_message_with_boundary``) is opt-in, so each dispatch
path must remember to call it and nothing enumerated the paths that had not.

This test enumerates every call site of a dispatch primitive and requires each
one to either reach a confirmation helper or carry an explicit allowlist entry
stating why an unconfirmed send is acceptable there. Adding a new fire-and-
forget dispatch fails this test until its author makes that choice on purpose.

What this guard is, and is not (PR #1004 review, P3-1). It is a static NAME
check over the AST: a path "reaches confirmation" when its enclosing function,
or a function it calls in the same module, mentions a confirmation symbol
outside a branch that can never run (``if False``, ``while 0``, a boolean
``and`` with a constant-false operand). It does NOT prove the confirmation
executes on every path, and it is blind to:

- aliases (``confirm = _confirm_submitted; confirm(...)`` is seen only because
  the name is mentioned, not because it is called);
- a mention that is dead for a reason the AST cannot see (a flag that is
  always False at runtime, an early ``return`` above it);
- confirmation delegated across modules (one level of intra-module call
  resolution only).

So the guard catches the class -- a new dispatch call site nobody wired -- and
the behavioral tests (``TestInboxSubmitConfirmation`` for the inbox path, the
deferred-init tests for its path) are what prove the wiring actually runs.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set, Tuple

import cli_agent_orchestrator

# The open-loop primitives. Both live in ``services/terminal_service`` and both
# end in paste -> sleep -> Enter with no proof the Enter landed.
DISPATCH_PRIMITIVES = ("send_input", "dispatch_input")

# Symbols that constitute submit confirmation: the shared recovery helper, its
# bool-contract wrapper, and the per-path confirmation entry points built on it.
CONFIRMATION_SYMBOLS = (
    "redeliver_dropped_message",
    "redeliver_dropped_message_with_boundary",
    "_confirm_worker_started_or_resubmit",
    "_confirm_submitted",
)

# (module relative to the package root, enclosing function) -> why this dispatch
# may stay unconfirmed. Keep each reason specific enough to re-audit; a path that
# becomes fire-and-forget must move out of this table, not have its reason edited.
ALLOWED_UNCONFIRMED: Dict[Tuple[str, str], str] = {
    ("services/terminal_service.py", "send_input"): (
        "The primitive's own bool-contract wrapper over dispatch_input."
    ),
    ("services/terminal_service.py", "exit_terminal_cli"): (
        "Teardown. A swallowed Enter costs nothing: the caller kills the pane next."
    ),
    ("api/main.py", "send_terminal_input"): (
        "KNOWN GAP, tracked separately: POST /terminals/{id}/input returns success "
        "when the paste was sent, with no proof the Enter landed. Nothing in CAO "
        "retries it; a caller that does not watch the pane loses the message (#78 "
        "shape). Confirming inline would block the API worker, so the fix is the "
        "inbox path's shape: answer after the paste, confirm in the background."
    ),
    ("services/agui/handoff_approval.py", "send_input"): (
        "KNOWN GAP, tracked separately: the AnswerDelivery adapter sends the "
        "approval answer fire-and-forget. If the Enter is swallowed the interrupt is "
        "marked delivered and nothing re-attempts it; the answer sits unsubmitted in "
        "the agent's prompt (#78 shape)."
    ),
    ("services/memory_service.py", "get_curated_memory_context"): (
        "Bounded wait with a safe fallback: the caller polls the curator for ~15s "
        "and returns the uncurated context if it never answers. Nothing is lost."
    ),
    ("services/flow_service.py", "execute_flow"): (
        "KNOWN GAP, tracked separately: a scheduled flow's initial prompt is sent "
        "fire-and-forget and the flow is logged as launched. Same shape as #78."
    ),
}

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent


def _dispatch_references(tree: ast.Module) -> List[ast.AST]:
    """Nodes that reference a dispatch primitive, excluding its own definition."""

    refs: List[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in DISPATCH_PRIMITIVES:
            # Only the terminal_service primitives, not same-named methods on
            # HTTP clients or protocols (``client.send_input``).
            value = node.value
            if isinstance(value, ast.Name) and value.id == "terminal_service":
                refs.append(node)
        elif isinstance(node, ast.Name) and node.id in DISPATCH_PRIMITIVES:
            refs.append(node)
    return refs


def _functions(tree: ast.Module) -> List[ast.AST]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _enclosing_chain(functions: List[ast.AST], line: int) -> List[ast.AST]:
    """Every function whose body spans ``line``, innermost first."""

    spanning = [f for f in functions if f.lineno <= line <= (f.end_lineno or f.lineno)]
    return sorted(spanning, key=lambda f: -f.lineno)


def _statically_false(test: ast.AST) -> bool:
    """True for a condition the AST alone proves can never hold.

    ``if False:``, ``if 0:``, ``if None:``, and an ``and`` chain with any
    constant-false operand (``if False and FLAG:``). A disabled confirmation
    hidden behind one of these is dead code, and a dead mention must not
    satisfy the guard (PR #1004 review, P3-1).
    """
    if isinstance(test, ast.Constant):
        return not test.value
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_statically_false(value) for value in test.values)
    return False


def _names_in(node: ast.AST) -> Set[str]:
    """Names and attributes mentioned in ``node``, skipping statically dead branches."""
    out: Set[str] = set()

    def visit(sub: ast.AST) -> None:
        if isinstance(sub, (ast.If, ast.While)) and _statically_false(sub.test):
            # The body can never run; only the test and the else branch are live.
            visit(sub.test)
            for child in sub.orelse:
                visit(child)
            return
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        for child in ast.iter_child_nodes(sub):
            visit(child)

    visit(node)
    return out


def _reaches_confirmation(chain: List[ast.AST], by_name: Dict[str, ast.AST]) -> bool:
    """True if the enclosing chain, or anything it calls in-module, confirms.

    One level of intra-module call resolution: ``run_agent_step`` dispatches and
    delegates the confirmation to ``_wait_for_completion``, which is the normal
    shape for a path whose wait loop owns the recovery.
    """

    for func in chain:
        # The recovery helper's own re-send is the confirmation, not a dispatch
        # that needs one.
        if func.name in CONFIRMATION_SYMBOLS:  # type: ignore[attr-defined]
            return True
        names = _names_in(func)
        if names & set(CONFIRMATION_SYMBOLS):
            return True
        for callee in names & set(by_name):
            if _names_in(by_name[callee]) & set(CONFIRMATION_SYMBOLS):
                return True
    return False


def test_every_pane_dispatch_confirms_the_submit_or_is_allowlisted() -> None:
    """No dispatch primitive may be called fire-and-forget without a reason."""

    unconfirmed: List[str] = []
    seen: Set[Tuple[str, str]] = set()

    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        refs = _dispatch_references(tree)
        if not refs:
            continue
        rel = path.relative_to(_PACKAGE_ROOT).as_posix()
        functions = _functions(tree)
        by_name = {f.name: f for f in functions}  # type: ignore[attr-defined]
        for ref in refs:
            chain = _enclosing_chain(functions, ref.lineno)
            # Module-level dispatch has no wait loop to own recovery at all.
            owner = chain[0].name if chain else "<module>"  # type: ignore[attr-defined]
            seen.add((rel, owner))
            if (rel, owner) in ALLOWED_UNCONFIRMED:
                continue
            if _reaches_confirmation(chain, by_name):
                continue
            unconfirmed.append(f"{rel}:{ref.lineno} in {owner}()")

    assert not unconfirmed, (
        "These call sites dispatch into a TUI pane without confirming the submit "
        "landed (GH#78 class). Wire them to "
        "terminal_service.redeliver_dropped_message_with_boundary, or add an entry "
        "to ALLOWED_UNCONFIRMED in this file explaining why an unsubmitted message "
        "is harmless there:\n  " + "\n  ".join(unconfirmed)
    )

    # The allowlist is a liability, not documentation: an entry that no longer
    # matches a real call site must be deleted so the table stays re-auditable.
    stale = sorted(set(ALLOWED_UNCONFIRMED) - seen)
    assert not stale, f"ALLOWED_UNCONFIRMED has entries with no matching call site: {stale}"


def _reaches(source: str, func: str = "deliver") -> bool:
    tree = ast.parse(source)
    functions = _functions(tree)
    by_name = {f.name: f for f in functions}  # type: ignore[attr-defined]
    return _reaches_confirmation([by_name[func]], by_name)


def test_guard_counts_a_live_confirmation_mention() -> None:
    assert _reaches(
        "def deliver(t):\n"
        "    dispatch_input(t, 'm')\n"
        "    if INBOX_SUBMIT_CONFIRMATION:\n"
        "        _confirm_submitted(t)\n"
    )


def test_guard_does_not_count_a_confirmation_behind_a_constant_false_branch() -> None:
    """PR #1004 review, P3-1: ``if False and FLAG: confirm()`` used to pass the guard."""
    assert not _reaches(
        "def deliver(t):\n"
        "    dispatch_input(t, 'm')\n"
        "    if False and INBOX_SUBMIT_CONFIRMATION:\n"
        "        _confirm_submitted(t)\n"
    )
    assert not _reaches(
        "def deliver(t):\n"
        "    dispatch_input(t, 'm')\n"
        "    if 0:\n"
        "        _confirm_submitted(t)\n"
    )
    assert not _reaches(
        "def deliver(t):\n"
        "    dispatch_input(t, 'm')\n"
        "    while False:\n"
        "        _confirm_submitted(t)\n"
    )


def test_guard_counts_the_else_branch_of_a_dead_if() -> None:
    assert _reaches(
        "def deliver(t):\n"
        "    dispatch_input(t, 'm')\n"
        "    if False:\n"
        "        pass\n"
        "    else:\n"
        "        _confirm_submitted(t)\n"
    )


def test_guard_resolves_one_level_of_intra_module_calls() -> None:
    assert _reaches(
        "def deliver(t):\n"
        "    dispatch_input(t, 'm')\n"
        "    _wait(t)\n"
        "def _wait(t):\n"
        "    redeliver_dropped_message_with_boundary(t, 'm', 1)\n"
    )
    assert not _reaches(
        "def deliver(t):\n"
        "    dispatch_input(t, 'm')\n"
        "    _wait(t)\n"
        "def _wait(t):\n"
        "    if False:\n"
        "        redeliver_dropped_message_with_boundary(t, 'm', 1)\n"
    )
