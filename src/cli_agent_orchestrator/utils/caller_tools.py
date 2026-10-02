"""Shared effective caller allowlist resolution for CAO delegation policy."""

from typing import Any, Dict, List, Optional


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
