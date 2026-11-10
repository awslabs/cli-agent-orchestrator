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
# #1199 (provisional): four task-feature points, each a yes/no question about the task text.
# They're asked together, as one request with all four points, and only of a decider that
# declares all four; one settings switch, FEATURES_SWITCH, turns them off, to shadow or on.
TASK_LONG_CONTEXT = DecisionPoint(
    "task.long_context", "Must a very large amount of text be held at once?", ("yes", "no")
)
TASK_DEEP_REASONING = DecisionPoint(
    "task.deep_reasoning", "Does it need long chains of careful reasoning?", ("yes", "no")
)
TASK_READ_ONLY = DecisionPoint("task.read_only", "Will it only read and report?", ("yes", "no"))
TASK_NEEDS_NETWORK = DecisionPoint(
    "task.needs_network", "Does it need the internet?", ("yes", "no")
)
POINTS = {
    p.name: p
    for p in (
        MODEL_ROUTE,
        EFFORT_ROUTE,
        PROVIDER_ROUTE,
        TASK_LONG_CONTEXT,
        TASK_DEEP_REASONING,
        TASK_READ_ONLY,
        TASK_NEEDS_NETWORK,
    )
}
# The feature points in wire order.
FEATURE_POINTS = (
    TASK_LONG_CONTEXT.name,
    TASK_DEEP_REASONING.name,
    TASK_READ_ONLY.name,
    TASK_NEEDS_NETWORK.name,
)
# Each feature point's name in the closed feature vocabulary, as the records and matcher use it.
FEATURE_NAMES = {point: point.removeprefix("task.") for point in FEATURE_POINTS}
# The features the matcher may route on, in its fixed order of importance. needs_network is
# advisory only: it adds a hint to the delegation note and never routes.
ROUTING_FEATURES = ("long_context", "deep_reasoning", "read_only")
# The launch-routing points: the only ones a launch may declare as a field.
ROUTE_POINTS = (MODEL_ROUTE.name, EFFORT_ROUTE.name, PROVIDER_ROUTE.name)
FEATURES_SWITCH = "task.features"
# The settings switches: every key `decisions.points` and `cao decisions set` accept. The four
# feature points share FEATURES_SWITCH, so they are points but not switches.
SWITCHES = ROUTE_POINTS + (FEATURES_SWITCH,)


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
    # #1199 (provisional): advisory hints for the delegation note, from a closed vocabulary
    # ("needs_network"). They never change the launch.
    hints: tuple[str, ...] = ()
