"""CAO allowedTools → OpenCode ``permission:`` frontmatter translator.

CAO owns the permission decision, so the output is always ``allow`` or ``deny``
for every built-in OpenCode tool. ``ask`` is never emitted — OpenCode's native
runtime prompt is treated as a provider-internal UX that CAO's policy replaces.

Algorithm:

1. Expand CAO shorthand in the input list: ``*`` means every tool; ``@``-prefixed
   selectors (``@builtin``, ``@<mcp>``) name no OpenCode tool and are skipped,
   exactly as ``utils/tool_mapping.py`` skips them for the providers it translates.
2. Map CAO categories to OpenCode native tool names; apply hardcoded non-vocabulary
   policy.
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


def cao_tools_to_opencode_permission(allowed_tools: List[str]) -> Dict[str, str]:
    """Translate a CAO ``allowedTools`` list to an OpenCode ``permission:`` dict.

    Args:
        allowed_tools: CAO-vocabulary tool list, e.g. ``["@builtin", "execute_bash"]``.

    Returns:
        A ``{tool_name: "allow"|"deny"}`` dict covering all 13 OpenCode
        built-in tools.  ``@``-prefixed entries in ``allowed_tools`` grant no
        OpenCode tool: ``@builtin`` is the provider-chrome selector that maps to
        nothing in ``utils/tool_mapping.py`` for any provider it translates, and
        ``@<mcp-server>`` entries are handled via ``opencode.json`` agent tool
        gating.  Only named CAO categories (``fs_read``, ``execute_bash``, ...)
        enable tools, so a ``reviewer`` default of ``["@builtin", "fs_read",
        "fs_list", "@cao-mcp-server"]`` yields a read-only agent here as it does
        on Claude Code, Copilot and Grok.
    """
    # ── Step 1: shorthand expansion ──────────────────────────────────────────
    if "*" in allowed_tools:
        # Unrestricted: every OpenCode tool → allow.
        return {tool: "allow" for tool in ALL_OPENCODE_TOOLS}

    expanded_categories: List[str] = []
    for entry in allowed_tools:
        if entry.startswith("@"):
            # ``@builtin`` is a selector for provider chrome, not a tool grant —
            # tool_mapping.get_disallowed_tools skips every ``@`` entry, so it
            # enables nothing on Claude Code / Copilot / Grok / Antigravity and
            # must enable nothing here, or the shipped ``reviewer`` role (which
            # lists it) would be read-only there and not here. ``@<mcp-server>``
            # references are handled in opencode.json, not frontmatter.
            continue
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
            result[tool] = "allow"
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
