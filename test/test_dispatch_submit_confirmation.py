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
        "POST /terminals/{id}/input is request/response -- the HTTP caller observes "
        "the pane and retries. Confirming here would block the API worker."
    ),
    ("services/agui/handoff_approval.py", "send_input"): (
        "AnswerDelivery adapter, not a dispatch path: the approval flow owns the "
        "retry (the interrupt stays UNRESOLVED and a later resume re-attempts)."
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


def _names_in(node: ast.AST) -> Set[str]:
    out: Set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
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
