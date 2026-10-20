"""Closed decision contract and launch values."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Literal, Mapping, Optional, Protocol

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
POINTS = {p.name: p for p in (MODEL_ROUTE, EFFORT_ROUTE)}


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
class Fallback:
    value: str | None
    source: Literal["profile", "policy", "none"]


@dataclass(frozen=True)
class MissingTier:
    provider: str
    tier: str


@dataclass(frozen=True)
class LaunchPlan:
    model: str | None = None
    record_ids: tuple[int, ...] = ()
