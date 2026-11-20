"""Shared caller identity and effective allowlist resolution for CAO policy."""

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def caller_terminal_context() -> Optional[Dict[str, Any]]:
    """The calling terminal's recorded row, or ``None`` when there is no identity.

    The one resolver behind every authorization decision taken next to an agent:
    ``mcp_server``'s gate (#671/#972) and ``ops_mcp_server``'s (#1062) both ask
    this, so the two surfaces cannot disagree about who is calling. It lives here
    rather than in either server's module for that reason.

    A 404 (the terminal is genuinely not registered) is the ONLY case that means
    "no context". Transport and auth failures PROPAGATE so the caller can say
    "cannot reach cao-server" rather than "could not resolve terminal context":
    reporting a down server as a missing identity is how the memory scope once
    silently collapsed to global with auth enabled.

    ``None`` is therefore not proof of an operator context -- it is also what a
    malformed ``CAO_TERMINAL_ID`` and the trailing ``except Exception`` return.
    Boundness is established from the environment instead, by
    ``security.mcp_tool_gate.caller_is_bound``.
    """
    # Imported here, not at module scope: ``mcp_server.utils`` reaches the API
    # over HTTP and pulls in ``utils.orchestration``, which this module's other
    # callers (``services.ephemeral_service``, the delegation ceiling) have no
    # reason to import. Resolving through the module object also keeps the
    # existing ``mcp_server.utils.get_json`` patch sites working.
    from cli_agent_orchestrator.mcp_server import utils as mcp_utils
    from cli_agent_orchestrator.utils.orchestration import _current_terminal_id, _mcp_timeout

    try:
        terminal_id = _current_terminal_id()
    except ValueError as e:
        logger.warning(f"Failed to get terminal context: {e}")
        return None

    if not terminal_id:
        return None

    try:
        # Via mcp_utils, which attaches the internal Authorization header. A bare
        # requests.get does not, and GET /terminals/{id} IS scope-gated.
        try:
            meta = mcp_utils.get_json(f"/terminals/{terminal_id}", timeout=_mcp_timeout())
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 404:
                return None
            raise
        return {
            "terminal_id": meta["id"],
            "session_name": meta["session_name"],
            "provider": meta["provider"],
            "agent_profile": meta.get("agent_profile"),
            "allowed_tools": meta.get("allowed_tools"),
            "ephemeral": meta.get("ephemeral", False),
        }
    except requests.RequestException:
        # Let the caller distinguish "unreachable / unauthorized" from "no
        # context"; the memory tools still degrade to None via their own
        # handlers, while the outcome tools report the real cause.
        raise
    except Exception as e:  # noqa: BLE001  (a malformed row is "no context", not a crash)
        logger.warning(f"Failed to get terminal context: {e}")
        return None


def caller_effective_allowed_tools(context: Dict[str, Any]) -> Optional[List[str]]:
    """Effective CAO allowlist for the calling terminal, or None if unresolvable.

    Mirrors ``create_terminal``: a recorded ``allowed_tools`` IS the effective
    list, while ``None`` means "resolve from the agent profile" rather than
    "unrestricted", so the profile goes through the same
    ``resolve_allowed_tools`` the launch path uses.
    """
    recorded = context.get("allowed_tools")
    if recorded is not None:
        return list(recorded)

    profile_name = context.get("agent_profile")
    if not profile_name:
        return None

    from cli_agent_orchestrator.agent_plugins.mcp_delivery import grantable_server_names
    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
    from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

    profile = load_agent_profile(profile_name)
    mcp_server_names = grantable_server_names(profile)
    return resolve_allowed_tools(profile.allowedTools, profile.role, mcp_server_names)
