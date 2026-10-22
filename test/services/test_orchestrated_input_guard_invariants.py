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

Two invariants guard the class:

1. **Enum completeness** -- every ``OrchestrationType`` member, present and
   future, is refused while the terminal is WAITING_USER_ANSWER on a provider
   that declares the hazard, and refused with the reason that re-queues rather
   than strands the message. Adding a member to the enum fails this test until
   its author decides, rather than silently granting it a bypass.
2. **Call-site census** -- every call into ``send_input``/``dispatch_input``
   from ``src/`` either tags its ``orchestration_type`` (so invariant 1 governs
   it) or appears in the explicit allowlist below with a reason and a reviewed
   occurrence count. A new untagged machine-originated call site fails this
   test instead of quietly acquiring a bypass, which is the sibling shape of
   #896. The census is built on ``test/_support/callsite_lint.py`` (#1011), so
   it ratchets the same way the other call-site rules do: a site beyond its
   row's count fails, and a row that matches nothing fails.

Known limit, stated on purpose: the census credits a call site on keyword
*presence*. ``api/main.py``'s ``/terminals/{id}/input`` route passes
``orchestration_type=orchestration_type`` and so reads as tagged even when the
request body carried none, which is how ``cao worker send`` reaches the seam
unclassified. That is operator input by design today and belongs with the
sibling issue, not here.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from test._support import callsite_lint as cl
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

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

_INPUT_FUNCS = frozenset({"send_input", "dispatch_input"})
_SEAM_MODULE = "terminal_service"

# Call sites that reach send_input/dispatch_input WITHOUT an orchestration_type
# and therefore cannot be governed by invariant 1. Keyed by (module, function)
# with a reviewed occurrence count, exactly like the #1011 rules in
# test/test_callsite_lint_rules.py: a plain string means one reviewed site, a
# (count, reason) pair means that many. A second untagged call in a module that
# already has a one-occurrence row is a new obligation, not a free pass, and a
# row that no longer matches anything fails so the table can only shrink.
_ALLOWED_UNTAGGED: Dict[Tuple[str, ...], cl.AllowlistRow] = {
    ("cli_agent_orchestrator/services/terminal_service.py", "send_input"): (
        "teardown exit command: the terminal is being killed, so answering a "
        "dialog with it is moot; dispatch_input's own recursion is tagged"
    ),
    ("cli_agent_orchestrator/services/agent_step.py", "send_input"): (
        2,
        "run_step prompt, once without and once with a frozen-memory block "
        "(api/main.py's run_step comment names this gap) -- LIVE #896 SIBLING, "
        "flagged for its own issue, not fixed here",
    ),
    ("cli_agent_orchestrator/services/memory_service.py", "send_input"): (
        "curator task dispatch -- LIVE #896 SIBLING, separate issue"
    ),
    ("cli_agent_orchestrator/services/flow_service.py", "send_input"): (
        "flow launch prompt -- LIVE #896 SIBLING, separate issue"
    ),
    ("cli_agent_orchestrator/services/agui/handoff_approval.py", "send_input"): (
        "ANSWER PATH, not a #896 sibling: TerminalServiceAnswerDelivery types the "
        "operator's approval decision into the waiting prompt (y/n text for codex, "
        "kiro_cli and the fallback, or the edited text). Like answer_user_prompt it "
        "must reach a WAITING_USER_ANSWER terminal, so tagging it would make the "
        "guard refuse every codex/kiro_cli approval. Must stay untagged."
    ),
}

_CENSUS_REMEDY = (
    "Pass orchestration_type= so the WAITING_USER_ANSWER guard governs this path "
    "(#896), or add a (module, function) row to _ALLOWED_UNTAGGED in this file "
    "with a reason -- and a (count, reason) pair if the module already has a row."
)


class _SeamNames:
    """The names under which one module can reach the input seam.

    Built from the module's own import statements so an alias cannot launder a
    call past the census: ``from ... import terminal_service as ts`` followed by
    ``ts.send_input(...)``, ``import ...terminal_service as tsmod``, and
    ``from ...terminal_service import send_input as deliver`` followed by
    ``deliver(...)`` all resolve to the seam. The bare names ``send_input`` and
    ``dispatch_input`` always count, as does the fully dotted
    ``cli_agent_orchestrator.services.terminal_service.send_input``.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.module_names = {_SEAM_MODULE}
        self.func_names: Dict[str, str] = {name: name for name in _INPUT_FUNCS}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name.split(".")[-1] == _SEAM_MODULE:
                        self.module_names.add(alias.asname)
            elif isinstance(node, ast.ImportFrom):
                from_seam = (node.module or "").split(".")[-1] == _SEAM_MODULE
                for alias in node.names:
                    if alias.name == _SEAM_MODULE:
                        self.module_names.add(alias.asname or alias.name)
                    elif from_seam and alias.name in _INPUT_FUNCS:
                        self.func_names[alias.asname or alias.name] = alias.name

    def seam_function(self, node: ast.AST) -> Optional[str]:
        """The seam function ``node`` refers to, or ``None`` if it is not the seam."""
        if isinstance(node, ast.Name):
            return self.func_names.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr in _INPUT_FUNCS:
            value = node.value
            # ``client.send_input`` is the HTTP client and ``self.send_input`` is
            # a wrapper, not the seam; only a module reference qualifies.
            if isinstance(value, ast.Name) and value.id in self.module_names:
                return node.attr
            if isinstance(value, ast.Attribute) and value.attr == _SEAM_MODULE:
                return node.attr
        return None


def _is_to_thread(func: ast.expr) -> bool:
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name == "to_thread"


def _input_call_sites(root: Optional[Path] = None) -> List[Tuple[cl.Site, bool]]:
    """Every call into the input seam under ``root`` (default ``src/``), with a tagged flag.

    Covers the direct call AND ``asyncio.to_thread(terminal_service.send_input,
    ...)``, which forwards its own keywords to the seam -- agent_step.py reaches
    it only that way, so a matcher that saw direct calls alone would have
    reported a clean census while a real untagged path existed. Any other
    mention of the seam (bound to a local, passed to ``functools.partial``)
    fails loudly rather than escaping.
    """
    sites: List[Tuple[cl.Site, bool]] = []
    unanalysable: List[str] = []
    for module in cl.parse_sources(root):
        names = _SeamNames(module.tree)
        seen: set = set()
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Call):
                continue
            target: ast.AST = node.func
            seam = names.seam_function(node.func)
            if seam is None and _is_to_thread(node.func) and node.args:
                target = node.args[0]
                seam = names.seam_function(target)
            if seam is None:
                continue
            seen.add(id(target))
            tagged = any(kw.arg == "orchestration_type" for kw in node.keywords)
            sites.append(
                (
                    cl.Site(
                        relpath=module.relpath,
                        line=node.lineno,
                        key=(module.relpath, seam),
                        detail=f"{seam}(...) {'tagged' if tagged else 'UNTAGGED'}",
                    ),
                    tagged,
                )
            )
        for node in ast.walk(module.tree):
            if not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            if isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
                continue
            if names.seam_function(node) is not None and id(node) not in seen:
                unanalysable.append(f"{module.relpath}:{node.lineno}")
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
        site.relpath == "cli_agent_orchestrator/services/inbox_service.py" and tagged
        for site, tagged in sites
    ), "matcher no longer recognises inbox_service's tagged send_input calls"
    assert any(not tagged for _, tagged in sites), "matcher found no untagged call at all"


def test_no_new_untagged_machine_input_call_site() -> None:
    """A machine-originated send must tag itself or hold a (module, function) row with a count."""
    cl.check_against_allowlist(
        rule="untagged-input-seam",
        sites=[site for site, tagged in _input_call_sites() if not tagged],
        allowlist=_ALLOWED_UNTAGGED,
        remedy=_CENSUS_REMEDY,
    )


# --------------------------------------------------------------------------
# Guard the guard: the census must see the forms a reviewer showed it missing.
# --------------------------------------------------------------------------


def _package(tmp_path: Path, **modules: str) -> Path:
    root = tmp_path / "pkg"
    root.mkdir()
    for name, source in modules.items():
        (root / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def test_census_resolves_module_and_function_aliases(tmp_path: Path) -> None:
    """An import alias must not launder a call past the census (#896 review, P3-2).

    The review added ``_ts.send_input(...)`` through a module alias and the
    census reported clean. Every aliased spelling below is one untagged site.
    """
    root = _package(
        tmp_path,
        aliased="""
        import asyncio
        import cli_agent_orchestrator.services.terminal_service
        import cli_agent_orchestrator.services.terminal_service as tsmod
        from cli_agent_orchestrator.services import terminal_service as ts
        from cli_agent_orchestrator.services.terminal_service import send_input as deliver


        def a(t):
            ts.send_input(t, "x")


        def b(t):
            deliver(t, "x")


        def c(t):
            tsmod.dispatch_input(t, "x")


        def d(t):
            cli_agent_orchestrator.services.terminal_service.send_input(t, "x")


        async def e(t):
            await asyncio.to_thread(ts.send_input, t, "x")


        def f(t):
            ts.send_input(t, "x", orchestration_type="assign")
        """,
    )
    sites = _input_call_sites(root)
    assert len(sites) == 6, [site.detail for site, _ in sites]
    assert sum(1 for _, tagged in sites if not tagged) == 5
    assert {site.key for site, _ in sites} == {
        ("pkg/aliased.py", "send_input"),
        ("pkg/aliased.py", "dispatch_input"),
    }


def test_census_flags_a_laundered_alias(tmp_path: Path) -> None:
    """Binding an aliased seam to something the walker cannot follow fails loudly."""
    root = _package(
        tmp_path,
        laundered="""
        import functools
        from cli_agent_orchestrator.services.terminal_service import send_input as deliver


        def a(t):
            return functools.partial(deliver, t)
        """,
    )
    with pytest.raises(AssertionError, match="without being called directly"):
        _input_call_sites(root)


def test_census_row_count_is_a_ratchet(tmp_path: Path) -> None:
    """A second untagged call in an allowlisted module is a new obligation (#896 review, P3-2).

    The old allowlist was keyed by module alone, so a module with one reviewed
    untagged call could grow any number of them unseen. The review added a
    second ``send_input`` to ``terminal_service.py`` and the census passed.
    """
    root = _package(
        tmp_path,
        twice="""
        from cli_agent_orchestrator.services import terminal_service


        def a(t):
            terminal_service.send_input(t, "x")


        def b(t):
            terminal_service.send_input(t, "y")
        """,
    )
    untagged = [site for site, tagged in _input_call_sites(root) if not tagged]
    assert len(untagged) == 2
    key = ("pkg/twice.py", "send_input")
    with pytest.raises(AssertionError, match="exceed their allowlist row"):
        cl.check_against_allowlist(
            rule="probe", sites=untagged, allowlist={key: "one reviewed"}, remedy="r"
        )
    cl.check_against_allowlist(
        rule="probe", sites=untagged, allowlist={key: (2, "two reviewed")}, remedy="r"
    )
