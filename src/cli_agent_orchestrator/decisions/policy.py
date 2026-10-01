"""Pure policy validation, explicit checks, fallback resolution and caps."""

from dataclasses import dataclass
from typing import Mapping

from cli_agent_orchestrator.decisions.types import POINTS, Effort, Fallback, MissingTier, Tier
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.provider import ProviderType

ModelTiers = Mapping[str, Mapping[str, str]]


class DecisionInputError(ValueError):
    """A launch violates a deterministic input constraint."""

    reason: str | None = None


class PolicyConfigError(DecisionInputError):
    """The operator envelope is invalid."""

    reason = "policy_invalid"


class PolicyViolation(DecisionInputError):
    reason = "above_ceiling"

    def __init__(self, point: str, value: str, ceiling: str) -> None:
        super().__init__(
            f"{'tier' if point == 'model.route' else 'effort'} '{value}' is above the policy ceiling '{ceiling}' for {point}"
        )


class EphemeralModelError(PolicyViolation):
    reason = "model_override_not_allowed"

    def __init__(self) -> None:
        DecisionInputError.__init__(self, "set the tier in the spec instead of a per-call model")


class UnmappedTierError(DecisionInputError):
    """Preserve the specific error while selecting its policy error category."""

    def __new__(cls, point: str, provider: str, tier: str, reason: str) -> "UnmappedTierError":
        if cls is UnmappedTierError:
            if reason == "explicit_unmapped":
                cls = ExplicitUnmappedTierError
            elif reason == "default_unmapped":
                cls = DefaultUnmappedTierError
            else:
                raise ValueError("unsupported unmapped-tier reason")
        return ValueError.__new__(cls)

    def __init__(self, point: str, provider: str, tier: str, reason: str) -> None:
        self.reason = reason
        detail = "explicit tier" if reason == "explicit_unmapped" else "policy default tier"
        DecisionInputError.__init__(
            self, f"model_tiers.{provider}.{tier} is not mapped ({detail} for {point})"
        )


class ExplicitUnmappedTierError(UnmappedTierError, PolicyViolation):
    reason = "explicit_unmapped"


class DefaultUnmappedTierError(UnmappedTierError, PolicyConfigError):
    reason = "default_unmapped"


class ProviderNotAllowedError(PolicyViolation):
    reason = "provider_not_allowed"

    def __init__(self, provider: str, allowed: frozenset[str]) -> None:
        DecisionInputError.__init__(
            self,
            f"provider '{provider}' is not allowed; allowed providers: {', '.join(sorted(allowed))}",
        )


@dataclass(frozen=True)
class PolicyBounds:
    default_tier: Tier | None = None
    max_tier: Tier | None = None
    default_effort: Effort | None = None
    max_effort: Effort | None = None
    allowed_providers: frozenset[str] | None = None

    def ceiling_for(self, point: str) -> str | None:
        return self.max_tier if point == "model.route" else self.max_effort

    def default_for(self, point: str) -> str | None:
        return self.default_tier if point == "model.route" else self.default_effort

    def validate(self, model_tiers: ModelTiers) -> None:
        for point, spec in POINTS.items():
            default, ceiling = self.default_for(point), self.ceiling_for(point)
            if any(value is not None and value not in spec.options for value in (default, ceiling)):
                raise PolicyConfigError(f"invalid policy value for {point}")
            if ceiling is not None and default is None:
                raise PolicyConfigError(f"{point} ceiling requires a default")
            if (
                default is not None
                and ceiling is not None
                and spec.options.index(default) > spec.options.index(ceiling)
            ):
                raise PolicyConfigError(f"{point} default exceeds ceiling")
        if self.allowed_providers is not None and not self.allowed_providers <= {
            p.value for p in ProviderType
        }:
            raise PolicyConfigError("invalid allowed provider")
        providers = (
            self.allowed_providers if self.allowed_providers is not None else model_tiers.keys()
        )
        for provider in sorted(providers):
            if not model_tiers.get(provider):
                continue
            required: list[str] = []
            if self.max_tier is not None:
                required.extend(
                    POINTS["model.route"].options[
                        : POINTS["model.route"].options.index(self.max_tier) + 1
                    ]
                )
            if self.default_tier is not None:
                required.append(self.default_tier)
            for tier in required:
                if not model_tiers.get(provider, {}).get(tier):
                    raise PolicyConfigError(
                        f"model_tiers.{provider}.{tier} is not mapped (required by policy)"
                    )

    def check_explicit(
        self, point: str, value: str, provider: str, model_tiers: ModelTiers
    ) -> None:
        options = POINTS[point].options
        if value not in options:
            raise DecisionInputError(f"invalid explicit value for {point}")
        ceiling = self.ceiling_for(point)
        if ceiling is not None and options.index(value) > options.index(ceiling):
            raise PolicyViolation(point, value, ceiling)
        if point == "model.route" and not model_tiers.get(provider, {}).get(value):
            raise UnmappedTierError(point, provider, value, "explicit_unmapped")

    def cap(self, point: str, option: str) -> tuple[str, bool]:
        ceiling = self.ceiling_for(point)
        options = POINTS[point].options
        if ceiling is not None and options.index(option) > options.index(ceiling):
            return ceiling, True
        return option, False


IDENTITY_POLICY = PolicyBounds()


def resolve_fallback(
    point: str,
    profile: AgentProfile | None,
    policy: PolicyBounds,
    tiers: Mapping[str, str],
    provider: str,
) -> Fallback | MissingTier:
    if point == "model.route" and profile is not None and profile.model:
        return Fallback(profile.model, "profile")
    default = policy.default_for(point)
    if default is None:
        return Fallback(None, "none")
    if point == "effort.route":
        return Fallback(default, "policy")
    model = tiers.get(default)
    return Fallback(model, "policy") if model else MissingTier(provider, default)
