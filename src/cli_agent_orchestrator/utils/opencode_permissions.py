"""CAO allowedTools → OpenCode ``permission:`` frontmatter translator.

Implements the two-step algorithm from §9 of docs/feat-opencode-provider-design.md:

1. Expand CAO shorthand (``*``, ``@builtin``, ``@<mcp>``) in the input list.
2. Map CAO categories to OpenCode native tool names; apply hardcoded non-vocabulary policy.

Returns a ``{tool: "allow"|"ask"|"deny"}`` dict suitable for the ``permission:``
frontmatter field of an OpenCode agent ``.md`` file.
"""

from typing import Dict, List

# ── OpenCode built-in tool vocabulary ────────────────────────────────────────

ALL_OPENCODE_TOOLS: List[str] = [
    "read",
    "write",
    "edit",
    "glob",
    "grep",
    "bash",
    "task",
    "question",
    "webfetch",
    "websearch",
    "codesearch",
    "skill",
    "todowrite",
]

# ── CAO category → OpenCode tools mapping ────────────────────────────────────

_CAO_CATEGORY_MAP: Dict[str, List[str]] = {
    "execute_bash": ["bash"],
    "fs_read": ["read"],
    "fs_write": ["edit", "write"],
    "fs_list": ["glob", "grep"],
    "fs_*": ["read", "edit", "write", "glob", "grep"],
}

# Tools that CAO categories can enable (the "CAO-vocabulary" set).
_CAO_VOCABULARY_TOOLS: frozenset = frozenset(
    tool for tools in _CAO_CATEGORY_MAP.values() for tool in tools
)

# ── Hardcoded non-vocabulary policies ────────────────────────────────────────
# These apply regardless of allowedTools (unless overridden by "*").

_HARDCODED_DENY: frozenset = frozenset(["task", "question", "webfetch", "websearch", "codesearch"])
_HARDCODED_ALLOW: frozenset = frozenset(["todowrite", "skill"])


def cao_tools_to_opencode_permission(
    allowed_tools: List[str],
    auto_approve: bool,
) -> Dict[str, str]:
    """Translate a CAO ``allowedTools`` list to an OpenCode ``permission:`` dict.

    Args:
        allowed_tools: CAO-vocabulary tool list, e.g. ``["@builtin", "execute_bash"]``.
        auto_approve: When ``True``, permitted tools get ``"allow"``; when
            ``False``, they get ``"ask"`` (OpenCode prompts the user).

    Returns:
        A ``{tool_name: "allow"|"ask"|"deny"}`` dict covering all 13 OpenCode
        built-in tools.  ``@<mcp-server>`` entries in ``allowed_tools`` are
        silently skipped — they are handled via ``opencode.json`` agent tool gating
        (see §6 of the design doc).
    """
    permit_value = "allow" if auto_approve else "ask"

    # ── Step 1: shorthand expansion ──────────────────────────────────────────
    if "*" in allowed_tools:
        # Unrestricted: every OpenCode tool → allow.
        return {tool: "allow" for tool in ALL_OPENCODE_TOOLS}

    expanded_categories: List[str] = []
    for entry in allowed_tools:
        if entry == "@builtin":
            expanded_categories.extend(["execute_bash", "fs_read", "fs_write", "fs_list"])
        elif entry.startswith("@"):
            # MCP server reference — handled in opencode.json, not frontmatter.
            continue
        else:
            expanded_categories.append(entry)

    # ── Step 2: build the permission dict ────────────────────────────────────
    # Collect all OpenCode tools that should be permitted.
    permitted_tools: set = set()
    for category in expanded_categories:
        if category in _CAO_CATEGORY_MAP:
            permitted_tools.update(_CAO_CATEGORY_MAP[category])
        # Unknown CAO categories are silently ignored.

    result: Dict[str, str] = {}
    for tool in ALL_OPENCODE_TOOLS:
        if tool in _HARDCODED_DENY:
            result[tool] = "deny"
        elif tool in _HARDCODED_ALLOW:
            result[tool] = "allow"
        elif tool in permitted_tools:
            result[tool] = permit_value
        elif tool in _CAO_VOCABULARY_TOOLS:
            # CAO-vocabulary tool that was not permitted → deny.
            result[tool] = "deny"
        else:
            # A tool was added to ALL_OPENCODE_TOOLS without a policy — fail loudly.
            raise AssertionError(
                f"unhandled tool '{tool}': add it to _HARDCODED_DENY, _HARDCODED_ALLOW, "
                "or _CAO_CATEGORY_MAP in opencode_permissions.py"
            )

    return result
