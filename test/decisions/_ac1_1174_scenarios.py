"""#1199 AC1: launches with task.features off, using only #1174's API (base 086f52b4).

This module runs unchanged on #1174's head and on this branch. Its output on the base is the
golden file ``test/fixtures/decisions_1174_ac1_golden.json``; test_task_features_engine.py
checks that this branch produces the same plans, records and requests, apart from the new
columns, which must be NULL. Regenerate the golden only from the base commit:

    <base>/.venv/bin/python -B test/decisions/_ac1_1174_scenarios.py <scratch-dir>
"""

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

# Columns whose values differ per run (ids, clocks, keyed hashes, timings).
VOLATILE = ("id", "created_at", "updated_at", "message_hash", "hash_key_id", "latency_ms")
# The columns #1174 has; anything else is new and is compared separately.
COLUMNS_1174 = (
    "contract_version",
    "point",
    "state",
    "decider",
    "decider_version",
    "kind",
    "provider",
    "agent_profile",
    "target_kind",
    "fallback_value",
    "fallback_source",
    "answer",
    "probabilities",
    "confidence",
    "candidate_value",
    "candidate_model",
    "applied_value",
    "outcome",
    "reason",
    "launched_model",
    "model_honored",
    "decision_status",
    "launch_status",
    "terminal_id",
    "message_bytes",
    "options",
    "fallback_enforcement",
    "applied_enforcement",
)
TIERS = {
    provider: {t: f"{provider}-{t}" for t in ("small", "medium", "large")}
    for provider in ("claude_code", "codex")
}
LABELS = {"claude_code": "hard", "codex": "sandboxed"}
BRIEF = "Read the whole payments service and map every caller of the charge API. Report only."


class Decider:
    name = "route_test"
    version = "1"
    points = frozenset({"model.route", "effort.route", "provider.route"})

    def __init__(self) -> None:
        self.answers = {"provider.route": "codex", "model.route": "large", "effort.route": "high"}
        self.requests: list[Any] = []

    async def decide(self, request: Any, config: Any) -> Any:
        from cli_agent_orchestrator.decisions.types import DecisionAnswer

        self.requests.append(request)
        return {
            point: DecisionAnswer(self.answers[point], {self.answers[point]: 0.9})
            for point in request.points
        }


SCENARIOS = {
    # name: (point states or None for fresh settings, fields, options)
    "fresh_settings": (
        None,
        ("provider.route", "model.route", "effort.route"),
        ("claude_code", "codex"),
    ),
    "all_on_routes": (
        {"provider.route": "on", "model.route": "on", "effort.route": "on"},
        ("provider.route", "model.route", "effort.route"),
        ("claude_code", "codex"),
    ),
    "provider_shadow_model_on": (
        {"provider.route": "shadow", "model.route": "on", "effort.route": "off"},
        ("provider.route", "model.route", "effort.route"),
        ("claude_code", "codex"),
    ),
    "single_option": (
        {"provider.route": "on", "model.route": "on", "effort.route": "on"},
        ("provider.route", "model.route", "effort.route"),
        ("claude_code",),
    ),
    "model_and_effort_only": (
        {"provider.route": "off", "model.route": "on", "effort.route": "on"},
        ("model.route", "effort.route"),
        None,
    ),
    "provider_only": (
        {"provider.route": "on", "model.route": "off", "effort.route": "off"},
        ("provider.route",),
        ("claude_code", "codex"),
    ),
}


def run(directory: Path) -> dict[str, Any]:
    """Each scenario's plan, records and requests, in a canonical, comparable form."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions.engine import DecisionEngine, DelegationRequest
    from cli_agent_orchestrator.decisions.policy import PolicyBounds
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner
    from cli_agent_orchestrator.decisions.store import DecisionStore
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState, ProviderOptions

    result: dict[str, Any] = {}
    for name, (states, fields, options) in SCENARIOS.items():
        db = create_engine("sqlite:///" + str(directory / f"{name}.db"))
        DecisionRecordModel.__table__.create(db)
        store = DecisionStore(sessionmaker(bind=db), directory / f"{name}.key", emit=lambda _: None)
        decider = Decider()
        if states is None:
            settings = DecisionSettings(model_tiers=TIERS)
        else:
            settings = DecisionSettings(
                points={
                    point: PointSettings(PointState(state), decider.name)
                    for point, state in states.items()
                },
                model_tiers=TIERS,
            )
        engine = DecisionEngine(
            DeciderRegistry({decider.name: lambda: decider}),
            store,
            ShadowRunner(store),
            settings_loader=lambda: settings,
            honors_model=lambda *args: True,
            honors_effort=lambda *args: True,
        )
        req = DelegationRequest(
            "assign",
            "claude_code",
            "Ramones-code_reading-3f9a",
            None,
            BRIEF,
            target_kind=TargetKind.EPHEMERAL,
            field_states={point: "auto" for point in fields},
            policy=PolicyBounds(
                min_tier="small",
                default_tier="medium",
                max_tier="large",
                min_effort="low",
                default_effort="medium",
                max_effort="high",
                allowed_providers=frozenset(TIERS),
            ),
            purpose="code_reading",
            description="Reads a large codebase end to end and maps call paths",
            provider_options=(
                ProviderOptions(options, {p: LABELS[p] for p in options}) if options else None
            ),
        )

        async def launch() -> Any:
            plan = await engine.prepare_launch(req)
            await engine.runner.drain()
            return plan

        plan = asyncio.run(launch())
        rows = sorted(store.list(), key=lambda row: row["id"])
        result[name] = {
            "plan": (
                None
                if plan is None
                else {
                    "model": plan.model,
                    "effort": plan.effort,
                    "provider": plan.provider,
                    "records": len(plan.record_ids),
                }
            ),
            "rows": [{column: row[column] for column in COLUMNS_1174} for row in rows],
            "requests": [
                {
                    key: value
                    for key, value in dataclasses.asdict(request).items()
                    if key != "request_id"
                }
                for request in decider.requests
            ],
        }
    return result


def new_columns(directory: Path) -> set[str]:
    """Columns this checkout's model has beyond #1174's (empty on the base)."""
    from cli_agent_orchestrator.clients.database import DecisionRecordModel

    names = {column.name for column in DecisionRecordModel.__table__.columns}
    return names - set(COLUMNS_1174) - set(VOLATILE)


if __name__ == "__main__":
    scratch = Path(sys.argv[1])
    scratch.mkdir(parents=True, exist_ok=False)
    print(json.dumps(run(scratch), indent=1, sort_keys=True, default=list))
