"""Closed decision contract and launch values."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Literal, Mapping, Optional, Protocol

from cli_agent_orchestrator.models.provider import ProviderType

CONTRACT_VERSION = 1
UNSURE = "unsure"
Tier = Literal["small", "medium", "large"]
Effort = Literal["low", "medium", "high"]


class PointState(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


@dataclass(frozen=True)
class DecisionPoint:
    name: str
    question: str
    options: tuple[str, ...]


MODEL_ROUTE = DecisionPoint(
    "model.route", "What model size does this task need?", ("small", "medium", "large")
)
EFFORT_ROUTE = DecisionPoint(
    "effort.route", "How much reasoning effort does this task need?", ("low", "medium", "high")
)
# #1099 (provisional): the static options are every CAO provider id. Each launch asks within
# a narrower set that CAO computes and sends as DecisionRequest.options.
PROVIDER_ROUTE = DecisionPoint(
    "provider.route", "Which CLI should run this task?", tuple(p.value for p in ProviderType)
)
POINTS = {p.name: p for p in (MODEL_ROUTE, EFFORT_ROUTE, PROVIDER_ROUTE)}


@dataclass(frozen=True)
class DecisionFacts:
    provider: str
    kind: Literal["assign", "handoff"]
    profile: str | None
    profile_role: str | None
    message_bytes: int
    use_worktree: bool


@dataclass(frozen=True)
class DecisionRequest:
    contract_version: int
    request_id: str
    points: tuple[str, ...]
    message: str
    profile_description: str | None
    purpose: str | None = field(default=None, kw_only=True)
    facts: DecisionFacts
    # #1099 Q3 (provisional): the per-launch options of a provider.route ask; None on every
    # other point's request, so no CONTRACT_VERSION bump.
    options: tuple[str, ...] | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class DecisionAnswer:
    option: str
    probabilities: Mapping[str, float]


DecisionResponse = Optional[Mapping[str, DecisionAnswer]]


class Decider(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    points: ClassVar[frozenset[str]]

    async def decide(
        self, request: DecisionRequest, config: Mapping[str, Any]
    ) -> DecisionResponse: ...


@dataclass(frozen=True)
class ProviderOptions:
    """The providers one launch may route to, with their enforcement labels (#1099).

    CAO computes them for each launch; the decider never does. They always contain the
    fallback provider, in capability-table order.
    """

    options: tuple[str, ...]
    labels: Mapping[str, str]


@dataclass(frozen=True)
class Fallback:
    value: str | None
    # #1099 (provisional): "creator" is provider.route's fallback, the creator's provider.
    source: Literal["profile", "policy", "none", "creator"]


@dataclass(frozen=True)
class MissingTier:
    provider: str
    tier: str


@dataclass(frozen=True)
class LaunchPlan:
    model: str | None = None
    record_ids: tuple[int, ...] = ()
    # #1083 Q1 (provisional): an on-mode effort.route answer, applied or capped. Only the
    # ephemeral site sets it; terminal_service.resolve_launch_effort passes it to the
    # provider, and installed launches never set it.
    effort: Effort | None = None
    # Only the engine sets `provider`, and only to a decided provider that differs from the
    # fallback; terminal_service.resolve_launch_provider runs that launch on it, and nothing
    # shared records it (#1099, as for the model and effort).
    provider: str | None = None
