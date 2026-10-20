"""How each provider enforces a CAO ``allowedTools`` policy.

One table, consumed by the launch confirmation gate, the server-side warning
in ``terminal_service`` and the documentation tables (a test keeps
``SECURITY.md`` and ``docs/tool-restrictions.md`` in step with it), so the
answer to "does the Blocked list mean anything on this provider?" cannot
drift between the code and what the operator is shown.

Three levels:

* ``NATIVE``: the provider runtime refuses denied tools itself (deny flags,
  an install-time permission block, or an allowlist in the agent file).
* ``PROMPT``: CAO can only tell the agent not to use the tools; the agent may
  still call them.
* ``NONE``: CAO passes no restriction at all, and the provider auto-approves
  tool calls. A restricted profile on such a provider runs unrestricted.
"""

from typing import Dict, Optional, Sequence

NATIVE = "native"
PROMPT = "prompt"
NONE = "none"

PROVIDER_ENFORCEMENT: Dict[str, str] = {
    "claude_code": NATIVE,  # --disallowedTools
    # kiro-cli is launched with --trust-all-tools, which only suppresses the
    # approval prompts; what the agent CAN use is the agent JSON's ``tools``
    # list, and `cao install` writes the resolved CAO policy into it
    # (``tool_mapping.kiro_agent_tools``), so a restricted role has no shell,
    # write or network tool to call. Install-time like OpenCode: the policy is
    # the INSTALLED agent's, and a launch override does not change it. A
    # profile installed before this change still carries ``tools: ["*"]`` until
    # it is reinstalled; the launch gate warns when it finds one.
    "kiro_cli": NATIVE,
    "copilot_cli": NATIVE,  # --deny-tool
    # The permission block is written at `cao install` time from the profile's
    # allowedTools and enforced by opencode itself. The runtime policy CAO
    # resolves at launch is NOT applied: --allowed-tools and a role override
    # select the installed agent and change nothing in it (see
    # INSTALL_TIME_PROVIDERS and describe_enforcement).
    "opencode_cli": NATIVE,
    "grok_cli": NATIVE,  # --permission-mode dontAsk with --allow/--deny
    "kimi_cli": PROMPT,
    "codex": PROMPT,
    "antigravity_cli": PROMPT,
    "omp": PROMPT,
    "mcode": PROMPT,
    "hermes": NONE,  # launches --yolo --accept-hooks; restrict inside the Hermes profile
    "cursor_cli": NONE,  # launches --force; allowedTools is not applied
    "mock_cli": NONE,
}

# NATIVE providers whose enforcement comes from the INSTALLED agent, not from
# the policy resolved at launch. A launch-time override (--allowed-tools, a
# role change) does not reach the provider; what runs is what `cao install`
# wrote. The launch gate must say so instead of attaching the native promise
# to the requested list.
INSTALL_TIME_PROVIDERS = frozenset({"opencode_cli", "kiro_cli"})


def enforcement_for(provider: str) -> str:
    """Enforcement level for ``provider``; an unknown provider is ``NONE``."""
    return PROVIDER_ENFORCEMENT.get(provider, NONE)


def native_providers() -> Sequence[str]:
    return tuple(p for p, level in PROVIDER_ENFORCEMENT.items() if level == NATIVE)


def is_restricted(allowed_tools: Optional[Sequence[str]]) -> bool:
    """True when the policy is anything other than unrestricted."""
    return allowed_tools is not None and "*" not in allowed_tools


def is_install_time(provider: str) -> bool:
    """True when the native policy is the installed agent's, not the launch request's."""
    return provider in INSTALL_TIME_PROVIDERS


def describe_enforcement(provider: str, allowed_tools: Optional[Sequence[str]]) -> str:
    """One line for the launch gate saying what the Blocked list is worth here."""
    level = enforcement_for(provider)
    if level == NATIVE and is_install_time(provider):
        return (
            "native at install time (the installed agent's policy applies; "
            "launch overrides do not change it)"
        )
    if level == NATIVE:
        return "native (the provider refuses blocked tools)"
    if not is_restricted(allowed_tools):
        return f"{level} (not applicable: the policy is unrestricted)"
    if level == PROMPT:
        return "prompt-only (the agent is told, not prevented; treat as unrestricted)"
    return "none (the provider applies no restriction; the agent runs unrestricted)"
