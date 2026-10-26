"""One MCP tool authorization gate, shared by every CAO MCP surface (#671, #1062).

#972 put a single ``on_call_tool`` middleware in front of ``cao-mcp-server`` so a
tool registered later is gated by DEFAULT instead of by someone remembering a
per-site guard. It recorded ``cao-ops-mcp-server`` as knowingly ungated (in
``UNGATED_SURFACES``) and left it for #1062 -- which is this module. The gate and
its policy primitives live here rather than inside one server's module, so the
second surface reuses the first's decision instead of growing a parallel one: two
independently written matchers is exactly the drift ``granted_mcp_servers`` exists
to prevent one layer down.

What is shared, and why each piece has to be:

* :func:`install_allowlist_middleware` -- the middleware itself, including the
  fail-closed check for a FastMCP build with no ``add_middleware``. A surface that
  cannot install its gate must not start ungated and silent.
* :func:`caller_is_bound`, :func:`caller_context_or_denial` and
  :func:`caller_allowed_or_denial` -- "who is calling, and what may they use",
  with the refusal wording. An unset ``CAO_TERMINAL_ID`` is the supported operator
  context and allows; once a caller claims an identity, every failure to resolve
  it denies.
* :func:`allowlist_grants_mcp_server` -- the one spelling rule for an ``@server``
  grant, delegating to ``tool_mapping.mcp_reference_covers`` so what a profile's
  ``allowedTools`` grants at the provider layer is what it grants here.

Each surface keeps its OWN policy function: the selector it requires, its exempt
set and any argument-level rule are surface facts, and collapsing them into one
function is how an exemption meant for one surface leaks onto the other.
"""

import asyncio
import logging
import os
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from cli_agent_orchestrator.utils.caller_tools import caller_effective_allowed_tools
from cli_agent_orchestrator.utils.tool_mapping import mcp_reference_covers

logger = logging.getLogger(__name__)

#: The grant that reaches the agent-facing CAO MCP tool surface (#671/#972).
CAO_MCP_SERVER_SELECTOR = "@cao-mcp-server"

#: The grant that reaches the operations control plane (#1062). Separate from
#: ``@cao-mcp-server`` on purpose: the ops surface launches sessions, installs
#: profiles and holds the decision controls, so a profile that needs delegation
#: does not get the control plane as a side effect.
CAO_OPS_MCP_SERVER_SELECTOR = "@cao-ops-mcp-server"

#: A surface's policy: the reason ``tool_name`` is refused, or ``None`` to allow.
#: Takes the call's arguments too, because a refusal can depend on them -- #1062
#: item 3 refuses ``decisions_set_point`` only for the states that lift a halt.
DeniedReason = Callable[[str, Mapping[str, Any]], Optional[str]]

#: Resolves the calling terminal's recorded row, or ``None`` when there is none.
ContextResolver = Callable[[], Optional[Dict[str, Any]]]


def caller_is_bound() -> bool:
    """Whether the caller claims a CAO terminal identity.

    Boundness is established from the environment rather than inferred from a
    resolver returning a context, because a resolver also returns ``None`` for a
    malformed ``CAO_TERMINAL_ID`` and from its own trailing ``except Exception``.
    An unbound caller is the supported operator context -- ``cao assign`` and an
    operator's own MCP host run with no caller identity -- and is never gated.
    """
    return bool(os.environ.get("CAO_TERMINAL_ID"))


def caller_context_or_denial(
    tool_name: str, resolve: ContextResolver
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The bound caller's terminal row, or the reason it cannot be authorized.

    Exactly one half is ``None``. Call only when :func:`caller_is_bound` is true:
    an unbound caller has nothing to resolve and must not be refused for it.

    Fails closed. A malformed ID, an unreachable or unauthorized cao-server, a
    terminal that is not registered -- each denies, because an unknown result must
    not dispatch.
    """
    try:
        context = resolve()
    except Exception as exc:  # noqa: BLE001  (an unknown result must not dispatch)
        logger.warning(f"authorization lookup failed for '{tool_name}': {exc}")
        return None, (
            f"cannot authorize '{tool_name}': the calling terminal could not be resolved ({exc})"
        )

    if context is None:
        return None, (
            f"cannot authorize '{tool_name}': CAO_TERMINAL_ID is set but the calling "
            "terminal could not be resolved"
        )

    return context, None


def caller_allowed_or_denial(
    tool_name: str, context: Mapping[str, Any]
) -> Tuple[Optional[List[str]], Optional[str]]:
    """The caller's effective CAO allowlist, or the reason it cannot be resolved.

    Exactly one half is ``None``. The effective list is the terminal's recorded
    ``allowed_tools``, or the profile resolution that a recorded ``None`` stands
    for -- ``caller_effective_allowed_tools`` is the launch path's own rule, so
    the gate cannot disagree with what the terminal was launched under.

    An empty list is a real answer (an operator may grant nothing) and is returned
    as such; only an UNRESOLVABLE list denies.
    """
    try:
        allowed = caller_effective_allowed_tools(dict(context))
    except Exception as exc:  # noqa: BLE001  (an unknown result must not dispatch)
        logger.warning(f"allowlist resolution failed for '{tool_name}': {exc}")
        return None, (
            f"cannot authorize '{tool_name}': the caller's allowed tools could not "
            f"be resolved ({exc})"
        )

    if allowed is None:
        return None, (
            f"cannot authorize '{tool_name}': the caller's allowed tools could not be resolved"
        )

    return allowed, None


def allowlist_grants_mcp_server(allowed_tools: Optional[Iterable[str]], selector: str) -> bool:
    """Whether a resolved CAO allowlist grants the whole MCP server ``selector``.

    ``*`` means unrestricted, as it does everywhere in ``resolve_allowed_tools``.
    Every other entry is compared with
    :func:`~cli_agent_orchestrator.utils.tool_mapping.mcp_reference_covers`, the
    same case-sensitive rule ``granted_mcp_servers`` applies when it turns
    ``allowedTools`` into provider MCP policy -- so the documented glob forms
    (``@cao-*``, ``@*``) authorize here exactly what they authorize there. #972
    compared by exact membership, which refused a profile the provider layer had
    already granted.

    ``None`` is not read as unrestricted: an unresolvable effective list is a
    refusal taken earlier, by :func:`caller_allowed_or_denial`. A bare tool name
    never names an MCP tool, on any provider (``get_disallowed_tools`` skips every
    ``@``-prefixed entry), which is why this axis needs its own enforcement at all.
    """
    entries = [entry for entry in (allowed_tools or ()) if isinstance(entry, str)]
    if "*" in entries:
        return True
    return any(mcp_reference_covers(entry, selector) for entry in entries)


def missing_grant_denial(tool_name: str, selector: str) -> str:
    """The refusal every surface returns for "the caller does not hold the grant"."""
    return (
        f"'{tool_name}' is not permitted: the calling terminal's allowed tools do not "
        f"include '{selector}'"
    )


def install_allowlist_middleware(
    mcp: Any,
    *,
    denied_reason: DeniedReason,
    exempt_tools: FrozenSet[str],
    surface: str,
) -> None:
    """Gate every tool on ``mcp`` through ``denied_reason``, once.

    One rule applied here rather than at every registration, because per-site
    guards are how both surfaces drifted in the first place and a tool added
    tomorrow -- by a later PR or by a plugin's ``on_mcp_server`` hook -- would be
    ungated by default. Register this AFTER every registration hook, or
    plugin-registered tools sit outside the gate.

    ``denied_reason`` is the single source of the surface's policy; this function
    holds no policy of its own beyond ``exempt_tools``, which skips the policy
    entirely rather than passing it a tool it would have to special-case.

    Raises ``RuntimeError`` on a FastMCP build with no ``add_middleware``. Call
    sites are module-level, so that aborts import -- the loudest failure
    available, and the only acceptable one: a surface that cannot install its gate
    must not start ungated and silent.
    """
    add_middleware = getattr(mcp, "add_middleware", None)
    if not callable(add_middleware):
        raise RuntimeError(
            f"FastMCP build has no add_middleware; cannot install the {surface} "
            "tool allowlist gate (#671)"
        )

    from fastmcp.exceptions import ToolError
    from fastmcp.server.middleware import Middleware

    class _AllowlistGate(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            tool_name = context.message.name
            if tool_name in exempt_tools:
                return await call_next(context)
            arguments = getattr(context.message, "arguments", None) or {}
            denied = await asyncio.to_thread(denied_reason, tool_name, arguments)
            if denied:
                # The audit record of the refusal itself. The policy logs why it
                # could not resolve a caller; nothing logged that a resolved
                # caller was turned away, so a refused agent left no trace an
                # operator could find afterwards.
                logger.warning(f"{surface}: refused MCP tool call: {denied}")
                raise ToolError(denied)
            return await call_next(context)

    add_middleware(_AllowlistGate())
