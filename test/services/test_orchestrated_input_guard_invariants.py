"""Structural guard for #896's defect class: the WAITING_USER_ANSWER input seam.

#896's root cause was not "the inbox path had a bug". It was that
``terminal_service.dispatch_input``'s dialog guard decides *is this input
machine-originated?* by testing membership in a hand-written subset of
``OrchestrationType`` -- ``{ASSIGN, HANDOFF}`` -- instead of deriving it from
the property it actually cares about. Anything carrying a value outside that
literal set, or carrying no value at all, walked straight past the one check
designed to stop it. The inbox delivery carried ``SEND_MESSAGE``, so it was
pasted into an open Claude Code AskUserQuestion dialog, which ate the text and
submitted whichever option was highlighted.

Nothing in the tree linked the literal set to the enum, and no test enumerated
either the enum or the call sites, so both halves of the hole were invisible to
CI: the suite was green with the defect present (a unit test asserted the buggy
delivery), ``mypy`` cannot see an exhaustiveness obligation on ``in {...}`` --
and the guard compares ``str`` values, erasing the enum type entirely -- and
``.github/workflows/ci.yml`` runs mypy with ``continue-on-error: true`` anyway.

Two invariants close the class:

1. **Enum completeness** -- every ``OrchestrationType`` member, present and
   future, is refused while the terminal is WAITING_USER_ANSWER on a provider
   that declares the hazard, and refused with the reason that re-queues rather
   than strands the message. Adding a member to the enum fails this test until
   its author decides, rather than silently granting it a bypass.
2. **Call-site census** -- every call into ``send_input``/``dispatch_input``
   from ``src/`` either tags its ``orchestration_type`` (so invariant 1 governs
   it) or appears in the explicit allowlist below with a reason. A new untagged
   machine-originated call site fails this test instead of quietly acquiring a
   bypass, which is the sibling shape of #896.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import patch

import pytest

import cli_agent_orchestrator
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import (
    TerminalInputBlockedError,
    TerminalInputBlockedReason,
    TerminalStatus,
)
from cli_agent_orchestrator.services.terminal_service import send_input

_TS = "cli_agent_orchestrator.services.terminal_service"


# --------------------------------------------------------------------------
# Invariant 1: the guard is keyed on the whole enum, not a literal subset.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("orchestration_type", list(OrchestrationType), ids=lambda t: t.value)
def test_every_orchestration_type_is_refused_while_waiting_user_answer(
    orchestration_type: OrchestrationType,
) -> None:
    """No ``OrchestrationType`` may be typed into an open dialog (#896).

    Parametrised over ``list(OrchestrationType)`` on purpose: a member added
    later (say ``BROADCAST``) is machine-originated input by construction and
    inherits the exact hazard, so it must be covered the day it is defined.
    """
    with (
        patch(f"{_TS}.get_terminal_metadata") as mock_meta,
        patch("cli_agent_orchestrator.backends.registry._backend") as mock_backend,
        patch(f"{_TS}.provider_manager") as mock_pm,
        patch(f"{_TS}.update_last_active") as mock_update,
        patch(f"{_TS}.status_monitor") as mock_status_monitor,
    ):
        mock_meta.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        provider = mock_pm.get_provider.return_value
        provider.blocks_orchestrated_input_while_waiting_user_answer = True
        mock_status_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER

        with pytest.raises(TerminalInputBlockedError) as exc:
            send_input("test1234", "machine text", orchestration_type=orchestration_type)

        # The reason must classify as transient, or the catcher in
        # inbox_service marks the batch FAILED and the message is lost for
        # good -- the very outcome #896 reported.
        assert exc.value.reason is TerminalInputBlockedReason.WAITING_USER_ANSWER
        mock_backend.send_keys.assert_not_called()
        mock_update.assert_not_called()


# --------------------------------------------------------------------------
# Invariant 2: no new untagged machine-input call site.
# --------------------------------------------------------------------------

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent
_INPUT_FUNCS = {"send_input", "dispatch_input"}

# Call sites that reach send_input/dispatch_input WITHOUT an orchestration_type
# and therefore cannot be governed by invariant 1. Each needs a reason. Keyed by
# module so the entries do not rot as line numbers move.
_ALLOWED_UNTAGGED: Dict[str, str] = {
    # The exit command on teardown: the terminal is being killed, so answering
    # a dialog with it is moot, and dispatch_input's own recursion is tagged.
    "services/terminal_service.py": "teardown exit command; internal dispatch is tagged",
    # Workflow step prompt (api/main.py's run_step comment names this gap) --
    # LIVE #896 SIBLING, flagged for its own issue, not fixed here.
    "services/agent_step.py": "run_step prompt is untagged; live sibling, separate issue",
    # Memory curator task dispatch -- LIVE #896 SIBLING, separate issue.
    "services/memory_service.py": "curator dispatch is untagged; live sibling, separate issue",
    # Approved handoff text delivery -- LIVE #896 SIBLING, separate issue.
    "services/agui/handoff_approval.py": "approved handoff text is untagged; live sibling",
    # Flow launch prompt -- LIVE #896 SIBLING, separate issue.
    "services/flow_service.py": "flow launch prompt is untagged; live sibling",
}


def _is_seam_reference(node: ast.expr) -> bool:
    """True for an expression naming terminal_service's input entry points."""
    if isinstance(node, ast.Name):
        return node.id in _INPUT_FUNCS
    if isinstance(node, ast.Attribute) and node.attr in _INPUT_FUNCS:
        # Only the module-qualified form; ``client.send_input`` is the HTTP
        # client and ``self.send_input`` is a wrapper, not the seam itself.
        return isinstance(node.value, ast.Name) and node.value.id == "terminal_service"
    return False


def _input_call_sites() -> List[Tuple[str, int, bool]]:
    """Every src/ call into the input seam, with whether it tags itself.

    Covers the direct call AND ``asyncio.to_thread(terminal_service.send_input,
    ...)``, which forwards its own keywords to the seam -- agent_step.py reaches
    it only that way, so a matcher that saw direct calls alone would have
    reported a clean census while a real untagged path existed.
    """
    sites: List[Tuple[str, int, bool]] = []
    seen: set = set()
    unanalysable: List[Tuple[str, int]] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        module = path.relative_to(_PACKAGE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            target = None
            if _is_seam_reference(func):
                target = func
            elif (
                isinstance(func, (ast.Name, ast.Attribute))
                and (func.id if isinstance(func, ast.Name) else func.attr) == "to_thread"
                and node.args
                and _is_seam_reference(node.args[0])
            ):
                target = node.args[0]
            if target is None:
                continue
            seen.add(id(target))
            tagged = any(kw.arg == "orchestration_type" for kw in node.keywords)
            sites.append((module, node.lineno, tagged))
        # Any other mention of the seam launders it past this census.
        for node in ast.walk(tree):
            if isinstance(node, (ast.Name, ast.Attribute)) and _is_seam_reference(node):
                if id(node) not in seen and not isinstance(
                    getattr(node, "ctx", None), (ast.Store, ast.Del)
                ):
                    unanalysable.append((module, node.lineno))
    assert not unanalysable, (
        "send_input/dispatch_input referenced without being called directly or via "
        f"asyncio.to_thread: {unanalysable}. Teach _input_call_sites about this form "
        "rather than letting it escape the #896 census."
    )
    return sites


def test_input_seam_census_still_sees_the_seam() -> None:
    """Sanity: the matcher finds both a tagged and an untagged real call site.

    Without this, a refactor that renames the seam would make the census test
    below pass vacuously.
    """
    sites = _input_call_sites()
    assert any(
        module == "services/inbox_service.py" and tagged for module, _, tagged in sites
    ), "matcher no longer recognises inbox_service's tagged send_input calls"
    assert any(not tagged for _, _, tagged in sites), "matcher found no untagged call at all"


def test_no_new_untagged_machine_input_call_site() -> None:
    """A machine-originated send must tag itself or be allowlisted with a reason."""
    untagged = [(module, line) for module, line, tagged in _input_call_sites() if not tagged]
    found = {module for module, _ in untagged}
    new = found - set(_ALLOWED_UNTAGGED)
    assert not new, (
        "New untagged send_input/dispatch_input call site(s): "
        + ", ".join(f"{m}:{ln}" for m, ln in sorted(untagged) if m in new)
        + ". Pass orchestration_type= so the WAITING_USER_ANSWER guard governs this "
        "path (#896), or add the module to _ALLOWED_UNTAGGED with a reason."
    )
    stale = sorted(set(_ALLOWED_UNTAGGED) - found)
    assert not stale, f"_ALLOWED_UNTAGGED entries no longer needed, drop them: {stale}"
