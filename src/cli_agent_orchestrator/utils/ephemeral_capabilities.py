"""What each provider can do for an ephemeral agent (#1074 phase 3).

One table, read by ``ephemeral_service`` at create, at claim and when it
validates ``ephemeral.allowed_providers``, by the MCP tool schema, and later by
``provider.route`` (#1099), so the providers that rule 8 accepts, the atoms a
provider can run and the enforcement label a create reports cannot drift apart.

This is the ephemeral counterpart of ``utils/enforcement.py``, which describes
installed launches. The two differ on purpose: Kiro enforces installed agents
natively but runs no ephemeral, and the contained Codex launch planned in #1074
gets a sandbox class that installed Codex launches do not.

The class names and the ``tool_unsupported_on_provider:<atom>`` rule built from
``unsupported_atoms`` are provisional public strings (#1074 open questions 4 and
6).
"""

from dataclasses import dataclass
from typing import Iterable, Optional

from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS

# The provider runtime refuses tools outside the recorded list.
HARD = "hard"
# The list is prompt text: the child is told, not prevented.
ADVISORY = "advisory"
# An OS sandbox bounds the child by class (read-only or workspace-write), and
# atoms are advisory within it. Reserved for the contained Codex launch (#1074).
SANDBOXED = "sandboxed"
# Not offered for ephemeral agents; create refuses with provider_unsupported.
REFUSED = "refused"
ENFORCEMENT_CLASSES = (HARD, ADVISORY, SANDBOXED, REFUSED)


@dataclass(frozen=True)
class EphemeralCapability:
    """One provider's ephemeral facts; a row that breaks an invariant fails at import."""

    enforcement: str
    tool_atoms: frozenset[str]
    honors_model: bool
    # The live-profile section and key that carry effort, e.g. Claude Code's
    # claudeConfig.effort; None when the provider has no effort mapping.
    effort_config: Optional[tuple[str, str]]

    def __post_init__(self) -> None:
        if self.enforcement not in ENFORCEMENT_CLASSES:
            raise ValueError(f"unknown enforcement class {self.enforcement!r}")
        if not self.tool_atoms <= frozenset(TOOL_ATOMS):
            raise ValueError("tool_atoms must be CAO tool atoms")
        if self.supports_ephemeral and not (self.honors_model and self.honors_effort):
            # Every create resolves a tier and an effort, so a provider that ignored
            # either would silently drop the operator's policy.
            raise ValueError("an ephemeral provider must honour model and effort")
        if not self.supports_ephemeral and self.tool_atoms:
            raise ValueError("a refused provider runs no tool atoms")

    @property
    def supports_ephemeral(self) -> bool:
        return self.enforcement != REFUSED

    @property
    def honors_effort(self) -> bool:
        return self.effort_config is not None


_REFUSED = EphemeralCapability(REFUSED, frozenset(), False, None)

CAPABILITIES: dict[str, EphemeralCapability] = {
    # --disallowedTools; the MCP config holds only cao-mcp-server.
    "claude_code": EphemeralCapability(
        HARD, frozenset(TOOL_ATOMS), True, ("claudeConfig", "effort")
    ),
    # An operator opt-in that runs --yolo with network on, so every atom runs,
    # advisorily. The contained launch moves it to SANDBOXED without web_fetch.
    "codex": EphemeralCapability(
        ADVISORY, frozenset(TOOL_ATOMS), True, ("codexConfig", "model_reasoning_effort")
    ),
    # Refused until the per-launch Kiro agent lands (#1074). An ephemeral
    # profile's model would not reach --model either: Kiro reads it with
    # load_agent_profile, which cannot read reserved names.
    "kiro_cli": _REFUSED,
}


def capability_for(provider: object) -> EphemeralCapability:
    """The provider's row; an unknown provider or a non-string is refused."""
    return CAPABILITIES.get(provider, _REFUSED) if isinstance(provider, str) else _REFUSED


def ephemeral_providers() -> tuple[str, ...]:
    """Providers that support ephemeral agents, in table order (rule 8's accepted set)."""
    return tuple(p for p, capability in CAPABILITIES.items() if capability.supports_ephemeral)


def unsupported_atoms(provider: str, tools: Iterable[str]) -> list[str]:
    """Requested atoms the provider cannot run, in TOOL_ATOMS order; non-atoms are ignored."""
    requested = set(tools)
    supported = capability_for(provider).tool_atoms
    return [atom for atom in TOOL_ATOMS if atom in requested and atom not in supported]
