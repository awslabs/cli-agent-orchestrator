"""Tool mapping from CAO vocabulary to provider-native tool names.

CAO defines a universal tool vocabulary (execute_bash, fs_read, fs_write, fs_list, fs_*,
@builtin, @cao-mcp-server) that is translated to each provider's native tool names.
This module provides the mapping and a function to compute which native tools to BLOCK
given a set of allowed CAO tools.
"""

from typing import Dict, List, Set

# All CAO tool categories and what they map to in each provider.
# Keys are provider names, values map CAO tool names to lists of native tool names.
TOOL_MAPPING: Dict[str, Dict[str, List[str]]] = {
    "claude_code": {
        "execute_bash": ["Bash"],
        "fs_read": ["Read"],
        "fs_write": ["Edit", "Write"],
        "fs_list": ["Glob", "Grep"],
        "fs_*": ["Read", "Edit", "Write", "Glob", "Grep"],
    },
    "copilot_cli": {
        "execute_bash": ["shell"],
        "fs_read": ["read"],
        "fs_write": ["write"],
        "fs_list": ["list", "grep"],
        "fs_*": ["read", "write", "list", "grep"],
    },
    "gemini_cli": {
        "execute_bash": ["run_shell_command"],
        "fs_read": ["read_file", "list_directory", "search_file_content", "glob"],
        "fs_write": ["write_file", "replace"],
        "fs_list": ["list_directory", "glob", "search_file_content"],
        "fs_*": [
            "read_file",
            "write_file",
            "replace",
            "list_directory",
            "search_file_content",
            "glob",
        ],
    },
}

# Complete set of all native tools per provider (used to compute disallowed set).
ALL_NATIVE_TOOLS: Dict[str, Set[str]] = {}
for _provider, _mapping in TOOL_MAPPING.items():
    tools: Set[str] = set()
    for _native_list in _mapping.values():
        tools.update(_native_list)
    ALL_NATIVE_TOOLS[_provider] = tools


def resolve_allowed_tools(
    profile_allowed_tools: List[str] | None,
    role: str | None,
    mcp_server_names: List[str] | None = None,
) -> List[str]:
    """Resolve the effective allowedTools for an agent.

    Resolution order:
    1. profile_allowed_tools (explicit in profile or --allowed-tools CLI)
    2. Role-based defaults from ROLE_TOOL_DEFAULTS
    3. Default to developer role

    MCP server names from the profile are appended as @server_name.
    """
    from cli_agent_orchestrator.constants import DEFAULT_ROLE, ROLE_TOOL_DEFAULTS

    if profile_allowed_tools is not None:
        allowed = list(profile_allowed_tools)
    else:
        effective_role = role or DEFAULT_ROLE
        allowed = list(ROLE_TOOL_DEFAULTS.get(effective_role, ROLE_TOOL_DEFAULTS[DEFAULT_ROLE]))

    # Append MCP server tools if not already present
    if mcp_server_names:
        for server_name in mcp_server_names:
            tool_ref = f"@{server_name}"
            if tool_ref not in allowed:
                allowed.append(tool_ref)

    return allowed


def get_disallowed_tools(provider: str, allowed: List[str]) -> List[str]:
    """Given CAO allowedTools, return provider-native tool names to BLOCK.

    Args:
        provider: Provider name (e.g., "claude_code", "copilot_cli", "gemini_cli")
        allowed: List of CAO tool names that are ALLOWED

    Returns:
        List of provider-native tool names that should be BLOCKED
    """
    if "*" in allowed:
        return []

    mapping = TOOL_MAPPING.get(provider)
    if not mapping:
        return []

    # Collect all native tools that are allowed
    allowed_native: Set[str] = set()
    for cao_tool in allowed:
        if cao_tool.startswith("@"):
            # MCP server references don't map to native tools
            continue
        if cao_tool in mapping:
            allowed_native.update(mapping[cao_tool])

    # Everything in ALL_NATIVE_TOOLS that is NOT allowed should be blocked
    all_tools = ALL_NATIVE_TOOLS.get(provider, set())
    disallowed = sorted(all_tools - allowed_native)
    return disallowed


def format_tool_summary(allowed: List[str]) -> str:
    """Format allowedTools into a human-readable summary for the confirmation prompt.

    Returns:
        A string like "execute_bash, fs_read, @cao-mcp-server"
    """
    if "*" in allowed:
        return "ALL TOOLS (unrestricted)"
    return ", ".join(allowed)
