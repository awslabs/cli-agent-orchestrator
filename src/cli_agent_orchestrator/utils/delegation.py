"""Delegation ceilings: what a terminal may grant to the children it creates.

A terminal's ``allowed_tools`` bounds what *it* can call. Its **delegation
ceiling** (``delegate_tools`` on the terminals row) bounds what it can *create*:
every child launched through ``assign``, ``handoff``, a workflow step or the
HTTP create endpoints must hold a subset of the creator's ceiling, and the
child's own ceiling is capped by it. The rule, in one sentence::

    child.allowed_tools ⊆ caller.ceiling  and
    child.ceiling = (child.allowed_tools ∪ child.declared_ceiling) ∩ caller.ceiling

where ``caller.ceiling`` is read from the CALLER'S STORED TERMINAL ROW, never
re-derived from a profile. That is what stops a restricted agent laundering
privilege through an intermediate ``role: supervisor`` profile: the
intermediate's row carries the restricted ceiling it was created under, and
its children are bounded by that row, not by what its profile claims.

A profile declares its ceiling with ``delegateTools``. Without it the role
default applies (``ROLE_DELEGATE_DEFAULTS``: ``supervisor`` -> ``*``), and a
role with no entry there -- every other built-in and every custom role --
defaults to the agent's own allowedTools. The ceiling always includes the
agent's own effective allowlist: an agent may hand down what it holds.

This module is the ONE comparator for "does this list exceed that one". The
ephemeral path (``ephemeral_service.create_ephemeral_agent``, which bounds a
new ephemeral profile by its creator's row) and the installed-profile path
(``terminal_service.create_terminal``, Step 0c) both call
:func:`tools_exceeding`, and the MCP-side precheck in ``utils/orchestration``
reuses :func:`describe_rejection` so the early error reads the same as the
server's authoritative one.

Vocabulary handled here (see docs/tool-restrictions.md "Tool Vocabulary"):

- ``*`` grants everything and is only satisfied by ``*``.
- ``fs_*`` expands to ``fs_read``, ``fs_list``, ``fs_write``; a child ``fs_*``
  is satisfied by a parent ``fs_*`` or by all three atoms.
- ``@builtin`` grants nothing on any provider and is ignored on the child
  side, otherwise ``supervisor`` (no ``@builtin``) could never launch
  ``reviewer`` (which lists it).
- ``@cao-mcp-server``, ``@<server>`` and other MCP references use the selector
  rule of :func:`cli_agent_orchestrator.utils.tool_mapping.mcp_reference_covers`:
  a whole-server grant covers a per-tool reference on that server (``@server``
  covers ``@server/tool``), not exact-name matching.
- Every other entry (``execute_bash``, ``web_fetch``, ``discovery``) matches by
  exact name.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from cli_agent_orchestrator.constants import ROLE_DELEGATE_DEFAULTS
from cli_agent_orchestrator.utils.tool_mapping import mcp_reference_covers, split_mcp_reference

UNRESTRICTED = "*"
FS_GLOB = "fs_*"
FS_ATOMS = ("fs_read", "fs_list", "fs_write")
#: Selector that grants nothing by itself (docs/tool-restrictions.md). Ignored
#: when asking whether a child exceeds its parent.
BUILTIN_SELECTOR = "@builtin"


class DelegationCeilingError(Exception):
    """A child would hold tools its creator may not delegate.

    Deliberately NOT a ``ValueError`` subclass: the create endpoints map
    ``ValueError`` to 404/400, while this is an authorization refusal and must
    surface as 403 with a message the operator can act on. ``status_code`` is
    read by the API layer; ``excess`` lists the offending tools.
    """

    status_code = 403

    def __init__(
        self,
        message: str,
        *,
        excess: Optional[Sequence[str]] = None,
        parent_id: Optional[str] = None,
        child_profile: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.excess = list(excess or [])
        self.parent_id = parent_id
        self.child_profile = child_profile


def _dedupe(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _grants(tools: Iterable[str]) -> set[str]:
    """Everything ``tools`` grants by exact name, with ``fs_*`` expanded. ``*`` stays literal."""
    granted: set[str] = set()
    for tool in tools:
        granted.add(tool)
        if tool == FS_GLOB:
            granted.update(FS_ATOMS)
    if set(FS_ATOMS) <= granted:
        granted.add(FS_GLOB)
    return granted


def covers(ceiling: Sequence[str], tool: str) -> bool:
    """Whether ``ceiling`` authorizes the single CAO-vocabulary entry ``tool``.

    The one predicate behind :func:`tools_exceeding` and :func:`intersect_ceiling`:

    - ``*`` in the ceiling covers everything; ``*`` as the tool needs ``*``.
    - ``@builtin`` grants nothing and is always covered (ignored).
    - an MCP reference (``@server``, ``@server/tool``, globs) is covered when some
      ceiling reference covers it under :func:`mcp_reference_covers`, the same
      selector semantics the provider resolvers use to grant servers at launch.
    - ``fs_*`` needs ``fs_*`` or all three filesystem atoms; an atom needs itself
      or ``fs_*``.
    - anything else matches by exact name. Unknown, odd-case or whitespace names
      are therefore excess (fail closed).
    """
    if UNRESTRICTED in ceiling:
        return True
    if tool == BUILTIN_SELECTOR:
        return True
    if tool == UNRESTRICTED:
        return False
    if split_mcp_reference(tool) is not None:
        return any(mcp_reference_covers(grant, tool) for grant in ceiling)
    return tool in _grants(ceiling)


def tools_exceeding(child: Optional[Sequence[str]], ceiling: Sequence[str]) -> List[str]:
    """Entries of ``child`` that ``ceiling`` does not cover, in ``child`` order.

    ``child=None`` means an unrestricted launch and is treated as ``["*"]``.
    An empty result means the child fits. ``@builtin`` never exceeds.
    """
    requested = [UNRESTRICTED] if child is None else list(child)
    return [tool for tool in _dedupe(requested) if not covers(ceiling, tool)]


def intersect_ceiling(declared: Sequence[str], ceiling: Sequence[str]) -> List[str]:
    """``declared ∩ ceiling`` in CAO vocabulary, preserving ``declared`` order.

    ``*`` on either side yields the other side verbatim. A declared ``fs_*``
    against a ceiling holding only some filesystem atoms narrows to those
    atoms. ``@builtin`` is kept when declared (it grants nothing, so keeping
    it widens nothing and keeps role lists readable).
    """
    if UNRESTRICTED in ceiling:
        return _dedupe(declared)
    if UNRESTRICTED in declared:
        return _dedupe(ceiling)
    granted = _grants(ceiling)
    out: List[str] = []
    for tool in declared:
        if tool == FS_GLOB and FS_GLOB not in granted:
            out.extend(atom for atom in FS_ATOMS if atom in granted)
        elif covers(ceiling, tool):
            out.append(tool)
    return _dedupe(out)


def union_tools(first: Sequence[str], second: Sequence[str]) -> List[str]:
    """``first ∪ second``; ``*`` on either side collapses to ``["*"]``."""
    if UNRESTRICTED in first or UNRESTRICTED in second:
        return [UNRESTRICTED]
    return _dedupe([*first, *second])


def resolve_delegate_tools(
    declared: Optional[Sequence[str]],
    role: Optional[str],
    effective_allowed_tools: Optional[Sequence[str]],
) -> List[str]:
    """The ceiling a profile declares, before any parent caps it.

    Resolution order mirrors ``resolve_allowed_tools``: an explicit
    ``delegateTools`` wins; else the role's entry in ``ROLE_DELEGATE_DEFAULTS``
    (``supervisor`` -> ``*``, keyed by NAME so a custom role of that name keeps
    it); else the agent's own list. The result is unioned with the agent's own
    effective allowlist, because an agent may always hand down what it holds
    (so a ``--yolo`` launch, whose allowlist is ``*``, delegates ``*``). A
    ``None`` allowlist is the unrestricted launch and resolves to ``*``.
    """
    own = [UNRESTRICTED] if effective_allowed_tools is None else list(effective_allowed_tools)
    if declared is not None:
        ceiling: List[str] = list(declared)
    elif role and role in ROLE_DELEGATE_DEFAULTS:
        ceiling = list(ROLE_DELEGATE_DEFAULTS[role])
    else:
        ceiling = list(own)
    return union_tools(own, ceiling)


def ceiling_from_row(row: Dict[str, Any]) -> Optional[List[str]]:
    """The ceiling recorded on a terminal row, with the pre-migration fallback.

    Rows written before the ``delegate_tools`` column existed have ``None``
    there. They are bounded by their OWN RECORDED allowlist until relaunched --
    the strict reading, chosen over re-deriving from the profile, because such a
    row may itself have been created by a restricted parent under the old rule
    and the whole point is to stop trusting the profile for that. A row with
    NEITHER column recorded has no immutable authority at all and returns
    ``None``: the profile on disk is mutable, so resolving it here would let an
    edit to the profile widen an existing terminal's ceiling after the fact.
    Callers fail closed on ``None`` and tell the operator to relaunch.
    """
    recorded = row.get("delegate_tools")
    if recorded is not None:
        return list(recorded)
    own = row.get("allowed_tools")
    return list(own) if own is not None else None


def describe_rejection(
    *,
    parent_id: Optional[str],
    parent_profile: Optional[str],
    child_profile: Optional[str],
    excess: Sequence[str],
    ceiling: Sequence[str],
) -> str:
    """The one message both the server and the MCP precheck use for a refusal.

    Names the parent, the child, the exceeding tools and the remedy, so the
    agent that sees it (and the operator reading the log) knows which profile
    to edit. ``parent_id`` may be ``None`` for a ceiling asserted by a remote
    caller whose row lives on another node.
    """
    parent = (f"terminal {parent_id}" if parent_id else "the calling terminal") + (
        f" (profile '{parent_profile}')" if parent_profile else ""
    )
    child = f"'{child_profile}'" if child_profile else "the requested child"
    return (
        f"Delegation refused: {parent} may not launch {child} with tools it cannot "
        f"delegate: {', '.join(excess)}. The parent's delegation ceiling is "
        f"[{', '.join(ceiling)}]. Remedy: declare `delegateTools` on the parent's "
        f"profile listing the tools its children may hold (for example "
        f'`delegateTools: ["*"]` or the specific tools), reinstall if the '
        f"provider enforces at install time, and relaunch the parent. The ceiling "
        f"is recorded when a terminal is created and can only narrow down the tree."
    )


def bound_child(
    *,
    child_allowed_tools: Optional[Sequence[str]],
    child_declared_ceiling: List[str],
    caller_ceiling: Sequence[str],
    parent_id: Optional[str],
    parent_profile: Optional[str],
    child_profile: Optional[str],
) -> List[str]:
    """Apply the rule: refuse an over-wide child, return the child's capped ceiling."""
    excess = tools_exceeding(child_allowed_tools, caller_ceiling)
    if excess:
        raise DelegationCeilingError(
            describe_rejection(
                parent_id=parent_id,
                parent_profile=parent_profile,
                child_profile=child_profile,
                excess=excess,
                ceiling=caller_ceiling,
            ),
            excess=excess,
            parent_id=parent_id,
            child_profile=child_profile,
        )
    return intersect_ceiling(child_declared_ceiling, caller_ceiling)


class InstalledPolicyExceedsAllowlistError(DelegationCeilingError):
    """An install-time provider's materialised policy is wider than the bounded row.

    Kiro (and any provider that enforces from a file ``cao install`` wrote) runs
    the INSTALLED policy, not the allowlist the create request resolved. For a
    delegated create the row must be the effective policy, so a launch whose
    installed file grants more than the row is refused before allocation with
    the remedy (reinstall the profile with the narrower policy, or do not
    override the allowlist). Operator launches keep the existing warning.
    """


#: ``/health`` feature flag a node advertises once it evaluates ceilings. A
#: remote placement (``target_host``) carries the caller's ceiling in the
#: request; the client checks this flag first and refuses when it is absent,
#: because an older node would silently ignore the field (fail closed on
#: version skew).
REMOTE_FEATURE = "delegation_ceiling"
