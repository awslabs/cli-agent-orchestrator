"""The operations control plane's allowlist gate (#1062, #671's recorded follow-up).

#972 gated every ``cao-mcp-server`` tool on the caller's ``tools:`` allowlist and
recorded this surface as knowingly ungated, in ``UNGATED_SURFACES``, for its own
issue. The gap it recorded: ``CAO_TERMINAL_ID`` is forwarded into EVERY server in
a profile's ``mcpServers`` map, so a bound terminal whose profile lists
``cao-ops-mcp-server`` reached ``launch_session``, ``install_profile``,
``shutdown_session``, ``send_session_message`` and the seven ``decisions_*`` tools
with its allowlist unconsulted -- on the one surface that can mint a session and
holds the operator's decision controls.

Three decisions, each a proposal maintainers can flip:

1. **The grant is ``@cao-ops-mcp-server``**, separate from ``@cao-mcp-server``.
   A profile that needs delegation should not acquire the control plane as a side
   effect, and the two surfaces answer to different authorities: one acts within
   the caller's own assign/handoff tree, this one acts on the node.
2. **``decisions_off`` stays reachable without the grant**, the way
   ``mcp_server.server.CALLBACK_EXEMPT_TOOLS`` are. See
   :data:`FAIL_SAFE_EXEMPT_TOOLS`.
3. **Lifting a halt is operator-only**, refused for any bound caller even WITH
   the grant. #955 made ``decisions_off`` the operator's kill switch, and
   ``settings.set_point(point, "shadow"|"on")`` unlinks the halt marker -- so
   without this rule a caller holding the grant could undo an operator's
   ``cao decisions off`` through the same surface. The CLI path is untouched.
   The refusal here is the FAST one; the rule is enforced inside the mutation,
   under the lock that protects the marker, because a check taken before
   dispatch cannot bind a halt published after it (see
   ``decisions/settings.set_point``).

The gate itself is ``security.mcp_tool_gate``'s, not a second one written here:
boundness, caller resolution, allowlist resolution, the ``@server`` spelling rule
and the refusal wording are all that module's, so this surface and
``cao-mcp-server`` cannot drift apart on any of them.
"""

import logging
from typing import Any, Mapping, Optional

from cli_agent_orchestrator.decisions import settings as decision_settings
from cli_agent_orchestrator.security.mcp_tool_gate import (
    CAO_OPS_MCP_SERVER_SELECTOR,
    allowlist_grants_mcp_server,
    caller_allowed_or_denial,
    caller_context_or_denial,
    caller_is_bound,
    install_allowlist_middleware,
    missing_grant_denial,
)
from cli_agent_orchestrator.utils import caller_tools

logger = logging.getLogger(__name__)

#: Ops tools a bound terminal reaches whatever its allowlist says.
#:
#: ``decisions_off`` only ever turns decision points OFF: it is argument-free,
#: has no state parameter, and halts and drains (#955 added it here as the
#: operator's kill switch). A fail-safe that needs a grant is not a fail-safe --
#: the caller most likely to need it is the restricted one noticing something is
#: wrong, and refusing it would mean the surface could be *entered* to start
#: decisions but not to stop them.
#:
#: The exemption rests on the tool staying argument-free; a ``state`` or ``point``
#: parameter added later would widen an ungated tool, which is why its input
#: schema is pinned by
#: ``test_mcp_tool_surface_gate_coverage.test_the_fail_safe_decisions_off_stays_argument_free``.
#: Keep this set this small: a tool belongs here only if it can do nothing but
#: reduce what the node is doing.
FAIL_SAFE_EXEMPT_TOOLS = frozenset({"decisions_off"})

#: Refused for a bound caller even with the grant: see decision 3 above. The
#: string is ``decisions.settings``', because the MUTATION owns this rule and
#: raises the same refusal under the lock (see below); one wording, so the fast
#: path and the real control cannot describe the rule differently.
HALT_LIFT_DENIAL = decision_settings.HALT_LIFT_DENIAL


def ops_tool_denied_reason(tool_name: str, arguments: Mapping[str, Any]) -> Optional[str]:
    """Reason the calling terminal's allowlist bars ``tool_name``, or None to allow.

    The single source of this surface's policy, the way ``_tool_denied_reason`` is
    for ``cao-mcp-server``.

    Fails closed. An unset ``CAO_TERMINAL_ID`` is the supported operator context
    -- this server is normally run by a human's MCP host, and ``cao-ops`` in the
    operator agent-plugin -- and is unchanged by this gate. Once a caller claims
    an identity, every failure to resolve it denies.
    """
    if not caller_is_bound():
        return None

    context, denial = caller_context_or_denial(tool_name, caller_tools.caller_terminal_context)
    if context is None:
        return denial

    allowed, denial = caller_allowed_or_denial(tool_name, context)
    if allowed is None:
        return denial

    if not allowlist_grants_mcp_server(allowed, CAO_OPS_MCP_SERVER_SELECTOR):
        return missing_grant_denial(tool_name, CAO_OPS_MCP_SERVER_SELECTOR)

    # Decision 3, as a FAST refusal only -- the control is in the mutation.
    # Checked here after the grant, so an ungranted caller is told the one thing
    # it needs to hear, and asked of ``decision_settings`` rather than restated,
    # so the gate and the mutation read one predicate. This check cannot be the
    # control: it runs before dispatch, and an ``operator.off()`` landing between
    # it and the write would be erased by a call it had already allowed
    # (independent review of #1062). ``settings.set_point`` re-decides it under
    # the exclusive egress lock, with the posture
    # ``ops_mcp_server/decision_tools`` passes it.
    if tool_name == "decisions_set_point" and decision_settings.lifts_halt(arguments.get("state")):
        return HALT_LIFT_DENIAL

    return None


def install_ops_allowlist_middleware(mcp: Any) -> None:
    """Gate every ops MCP tool on the caller's allowlist (#1062).

    Install AFTER ``register_decision_tools`` and ``register_mcp_server_surfaces``,
    so a plugin-registered operator tool lands INSIDE the gate rather than beside
    it. The decision is taken before dispatch and keyed on the tool name, so an
    ops tool a later PR adds is gated by default -- which is the half of #671 that
    per-registration guards cannot deliver.
    """
    install_allowlist_middleware(
        mcp,
        denied_reason=ops_tool_denied_reason,
        exempt_tools=FAIL_SAFE_EXEMPT_TOOLS,
        surface="cao-ops-mcp-server",
    )
