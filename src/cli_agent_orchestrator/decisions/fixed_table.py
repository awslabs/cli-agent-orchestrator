"""In-process profile-first, role-second operator table."""

from typing import Any, ClassVar, Mapping

from cli_agent_orchestrator.decisions.types import (
    POINTS,
    DecisionAnswer,
    DecisionRequest,
    DecisionResponse,
)


class FixedTableDecider:
    name: ClassVar[str] = "fixed_table"
    version: ClassVar[str] = "1"
    points: ClassVar[frozenset[str]] = frozenset(POINTS)

    async def decide(self, request: DecisionRequest, config: Mapping[str, Any]) -> DecisionResponse:
        answers = {}
        for point in request.points:
            block = config.get(point, {})
            if not isinstance(block, Mapping):
                continue
            profiles, roles = block.get("profiles", {}), block.get("roles", {})
            option = profiles.get(request.facts.profile) if isinstance(profiles, Mapping) else None
            if option is None and isinstance(roles, Mapping):
                option = roles.get(request.facts.profile_role)
            if option in POINTS[point].options + ("unsure",):
                answers[point] = DecisionAnswer(option, {option: 1.0})
        return answers or None

    async def close(self) -> None:
        pass
