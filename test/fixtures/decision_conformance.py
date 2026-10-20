"""Reusable behavioral conformance cases and a direct engine adapter."""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Protocol

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import DecisionRecordModel
from cli_agent_orchestrator.decisions.engine import (
    DecisionEngine,
    DelegationRequest,
    prepare_launch,
)
from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyBounds
from cli_agent_orchestrator.decisions.registry import DeciderRegistry
from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
from cli_agent_orchestrator.decisions.shadow import ShadowRunner
from cli_agent_orchestrator.decisions.store import DecisionStore
from cli_agent_orchestrator.decisions.targets import TargetKind
from cli_agent_orchestrator.decisions.types import (
    POINTS,
    Decider,
    DecisionAnswer,
    DecisionRequest,
    DecisionResponse,
    PointState,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile

Behaviour = str


class ConfidentDecider:
    name: ClassVar[str] = "fixture"
    version: ClassVar[str] = "1"
    points: ClassVar[frozenset[str]] = frozenset(POINTS)

    def __init__(self, option: str = "small", p: float = 0.91) -> None:
        self.option, self.p = option, p
        self.calls = 0
        self.requests: list[DecisionRequest] = []

    async def decide(self, request: DecisionRequest, config: Mapping[str, Any]) -> DecisionResponse:
        self.calls += 1
        self.requests.append(request)
        return {
            point: DecisionAnswer(
                (
                    self.option
                    if point == "model.route" or self.option in ("medium", "unsure")
                    else "high"
                ),
                {
                    (
                        self.option
                        if point == "model.route" or self.option in ("medium", "unsure")
                        else "high"
                    ): self.p
                },
            )
            for point in request.points
        }

    async def close(self) -> None:
        pass


class UnsureDecider(ConfidentDecider):
    def __init__(self) -> None:
        super().__init__("unsure", 1)


class TimeoutDecider(ConfidentDecider):
    async def decide(self, request: DecisionRequest, config: Mapping[str, Any]) -> DecisionResponse:
        self.calls += 1
        self.requests.append(request)
        await asyncio.Event().wait()
        return None


class ErrorDecider(ConfidentDecider):
    async def decide(self, request: DecisionRequest, config: Mapping[str, Any]) -> DecisionResponse:
        self.calls += 1
        self.requests.append(request)
        raise RuntimeError("decider unavailable")


class AboveCeilingDecider(ConfidentDecider):
    def __init__(self, option: str = "large") -> None:
        super().__init__(option)


class UnmappedTierDecider(ConfidentDecider):
    def __init__(self) -> None:
        super().__init__("large")


class OutOfSetDecider(ConfidentDecider):
    def __init__(self) -> None:
        super().__init__("xlarge")


class BlockingDecider(ConfidentDecider):
    def __init__(self, event: asyncio.Event) -> None:
        super().__init__()
        self.event = event
        self.started = asyncio.Event()

    async def decide(self, request: DecisionRequest, config: Mapping[str, Any]) -> DecisionResponse:
        self.started.set()
        await self.event.wait()
        return await super().decide(request, config)


class NoAnswerDecider(ConfidentDecider):
    async def decide(self, request: DecisionRequest, config: Mapping[str, Any]) -> DecisionResponse:
        self.calls += 1
        self.requests.append(request)
        return None


def default_fakes(behaviour: Behaviour) -> Decider:
    factories: dict[str, Callable[[], Decider]] = {
        "confident": ConfidentDecider,
        "unsure": UnsureDecider,
        "timeout": TimeoutDecider,
        "error": ErrorDecider,
        "above_ceiling": AboveCeilingDecider,
        "unmapped_tier": UnmappedTierDecider,
        "out_of_set": OutOfSetDecider,
        "blocking": lambda: BlockingDecider(asyncio.Event()),
        "no_answer": NoAnswerDecider,
        "low_confidence": lambda: ConfidentDecider("small", 0.2),
    }
    return factories[behaviour]()


@dataclass
class Observation:
    launched_model: str | None
    launched: bool
    error: str | None
    records: list[dict[str, Any]]
    spans: list[dict[str, Any]]
    logs: list[str]


class DecisionPathAdapter(Protocol):
    supports: frozenset[str]

    def configure(
        self,
        *,
        states: Mapping[str, str],
        deciders: Mapping[str, Decider],
        model_tiers: Mapping[str, Mapping[str, str]],
        policy: PolicyBounds | None = None,
        on_timeout_ms: int = 1000,
        threshold: float = 0.70,
    ) -> None: ...

    async def delegate(
        self,
        *,
        message: str,
        model: str | None = None,
        field_states: Mapping[str, str] | None = None,
        purpose: str | None = None,
    ) -> Observation: ...


class EngineAdapter:
    supports = frozenset(("policy", "field_states"))

    def __init__(self, root: Path) -> None:
        root = root / uuid.uuid4().hex
        root.mkdir()
        db = create_engine("sqlite:///" + str(root / "engine.db"))
        DecisionRecordModel.__table__.create(db, checkfirst=True)
        self.spans: list[dict[str, Any]] = []
        self.store = DecisionStore(
            sessionmaker(bind=db),
            root / "decision-hash.key",
            emit=self.spans.append,
        )
        self.profile_model: str | None = "model-y"
        self.provider = "codex"
        self.honored = True
        self.owner = "delegation"
        self.target = TargetKind.INSTALLED
        self.policy: PolicyBounds | None = None
        self.engine: DecisionEngine

    def configure(
        self,
        *,
        states: Mapping[str, str],
        deciders: Mapping[str, Decider],
        model_tiers: Mapping[str, Mapping[str, str]],
        policy: PolicyBounds | None = None,
        on_timeout_ms: int = 1000,
        threshold: float = 0.70,
    ) -> None:
        self.policy = policy
        settings = DecisionSettings(
            points={
                p: PointSettings(
                    PointState(states.get(p, "off")),
                    deciders[p].name if p in deciders else "unknown",
                )
                for p in POINTS
            },
            model_tiers=model_tiers,
            on_timeout_ms=on_timeout_ms,
            confidence_threshold=threshold,
        )
        sources = {d.name: lambda value=d: value for d in deciders.values()}
        self.engine = DecisionEngine(
            DeciderRegistry(sources),
            self.store,
            ShadowRunner(self.store),
            settings_loader=lambda: settings,
            profile_loader=lambda _: AgentProfile(
                name="worker", description="worker description", model=self.profile_model
            ),
            honors_model=lambda *args: self.honored,
        )

    async def delegate(
        self,
        *,
        message: str,
        model: str | None = None,
        field_states: Mapping[str, str] | None = None,
        purpose: str | None = None,
    ) -> Observation:
        request = DelegationRequest(
            "assign",
            self.provider,
            "worker",
            model,
            message,
            field_states=field_states or {},
            policy=self.policy or PolicyBounds(),
            target_kind=self.target,
            owner=self.owner,
            purpose=purpose,
        )
        logs: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                logs.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger("cli_agent_orchestrator.decisions")
        logger.addHandler(handler)
        try:
            try:
                plan = await prepare_launch(request, engine=self.engine)
            except DecisionInputError as error:
                return Observation(None, False, str(error), self.store.list(), self.spans, logs)
            launched_model = model or (plan.model if plan else None) or self.profile_model
            self.engine.bind_launch(
                plan, terminal_id="worker", model=launched_model, honored=self.honored
            )
            return Observation(launched_model, True, None, self.store.list(), self.spans, logs)
        finally:
            logger.removeHandler(handler)


@dataclass(frozen=True)
class Case:
    name: str
    behaviour: Behaviour = "confident"
    requires: frozenset[str] = frozenset()


def conformance_cases() -> list[Case]:
    ordinary = [
        Case("off"),
        Case("explicit"),
        Case("shadow", "blocking"),
        Case("content_free"),
        Case("installed_purpose"),
        Case("ephemeral_purpose", requires=frozenset(("field_states",))),
    ]
    ordinary += [
        Case("fallback_" + reason, behaviour)
        for reason, behaviour in (
            ("unsure", "unsure"),
            ("timeout", "timeout"),
            ("error", "error"),
            ("no_answer", "no_answer"),
            ("low_confidence", "low_confidence"),
            ("invalid_answer", "out_of_set"),
            ("tier_unmapped", "unmapped_tier"),
        )
    ]
    ordinary += [
        Case("capped", "above_ceiling", frozenset(("policy",))),
        Case("explicit_ceiling", requires=frozenset(("policy", "field_states"))),
    ]
    names = (
        "missing_default",
        "missing_answer",
        "explicit_unmapped",
        "profile_control",
        "field_default",
        "unavailable_default",
        "capped_default",
        "two_point_default",
        "installed_off_control",
        "not_honored_default",
    )
    ordinary += [
        Case(
            name,
            (
                "unsure"
                if name == "missing_default"
                else "above_ceiling" if name == "capped_default" else "confident"
            ),
            (
                frozenset(("policy", "field_states"))
                if name in ("explicit_unmapped", "field_default", "two_point_default")
                else frozenset(("policy",))
            ),
        )
        for name in names
    ]
    return ordinary


async def _run_single_case(
    case: Case,
    adapter_factory: Callable[[], DecisionPathAdapter],
    decider_factory: Callable[[Behaviour], Decider] = default_fakes,
    state_override: str | None = None,
) -> None:
    adapter = adapter_factory()
    missing = case.requires - adapter.supports
    if missing:
        pytest.skip("adapter lacks " + ", ".join(sorted(missing)))
    decider = decider_factory(case.behaviour)
    tiers = {"codex": {"small": "model-x", "medium": "model-m"}}
    policy = None
    states = {"model.route": "on"}
    fields = None
    purpose = None
    missing_cases = {
        "missing_default",
        "missing_answer",
        "explicit_unmapped",
        "profile_control",
        "field_default",
        "unavailable_default",
        "capped_default",
        "two_point_default",
        "installed_off_control",
        "not_honored_default",
    }
    try:
        if case.name in ("installed_purpose", "ephemeral_purpose"):
            purpose = "purpose-sentinel ghp_" + "a" * 36
            if case.name == "ephemeral_purpose":
                fields = {"model.route": "auto"}
                if isinstance(adapter, EngineAdapter):
                    adapter.target = TargetKind.EPHEMERAL
        if case.name in missing_cases:
            policy = PolicyBounds(default_tier="small", max_tier="medium")
            if isinstance(adapter, EngineAdapter):
                adapter.provider = "claude_code"
                adapter.profile_model = "model-y" if case.name == "profile_control" else None
                adapter.honored = case.name != "not_honored_default"
            if case.name == "explicit_unmapped":
                fields = {"model.route": "small"}
            if case.name in ("field_default", "two_point_default"):
                if isinstance(adapter, EngineAdapter):
                    adapter.target = TargetKind.EPHEMERAL
                fields = (
                    {"effort.route": "auto", "model.route": "auto"}
                    if case.name == "two_point_default"
                    else {"model.route": "auto"}
                )
            if case.name == "installed_off_control":
                states = {"model.route": "off"}
        if case.name == "capped":
            policy = PolicyBounds(default_tier="small", max_tier="medium")
        if case.name == "explicit_ceiling":
            policy = PolicyBounds(default_tier="small", max_tier="medium")
            fields = {"model.route": "large"}
        if case.name == "off":
            states = {"model.route": "off"}
        if case.name == "shadow":
            states = {"model.route": "shadow"}
        deciders = {} if case.name == "unavailable_default" else {p: decider for p in POINTS}
        if case.name == "two_point_default":
            states["effort.route"] = "on"
        if state_override is not None:
            states = {point: state_override for point in states}
        adapter.configure(
            states=states,
            deciders=deciders,
            model_tiers=tiers,
            policy=policy,
            on_timeout_ms=50,
            threshold=0.70,
        )
        start = time.perf_counter()
        observation = await adapter.delegate(
            message="sentinel-conformance-body",
            model="explicit-model" if case.name == "explicit" else None,
            field_states=fields,
            purpose=purpose,
        )
        if case.name == "fallback_timeout":
            assert time.perf_counter() - start < 1
        if case.name in missing_cases - {"profile_control", "installed_off_control"}:
            assert not observation.launched
            assert "model_tiers.claude_code.small" in observation.error
            reason = "explicit_unmapped" if case.name == "explicit_unmapped" else "default_unmapped"
            if state_override == "off":
                assert observation.records == []
                assert getattr(decider, "calls", 0) == 0
                return
            assert len(observation.records) == 1
            assert observation.records[0]["reason"] == reason
            assert observation.records[0]["launch_status"] == "not_launched"
            assert observation.records[0]["decision_status"] == "done"
            assert observation.records[0]["answer"] is None
            assert getattr(decider, "calls", 0) == 0
        elif case.name == "explicit_ceiling":
            assert not observation.launched and "medium" in observation.error
        elif case.name in ("off", "explicit", "installed_off_control"):
            assert observation.launched and not observation.records
            assert getattr(decider, "calls", 0) == 0
        elif case.name == "profile_control":
            assert observation.launched and observation.launched_model == "model-y"
            assert observation.error is None and len(observation.records) == 1
            assert observation.records[0]["outcome"] != "rejected"
        elif case.name == "shadow":
            assert observation.launched and observation.launched_model == "model-y"
            assert isinstance(decider, BlockingDecider) and not decider.event.is_set()
            decider.event.set()
            if isinstance(adapter, EngineAdapter):
                await adapter.engine.runner.drain()
                assert adapter.store.list()[0]["candidate_model"] == "model-x"
        elif case.name.startswith("fallback_"):
            assert observation.launched and observation.launched_model == "model-y"
            row = observation.records[0]
            assert row["outcome"] == "fallback" and row["reason"] == case.name.removeprefix(
                "fallback_"
            )
            assert row["applied_value"] is None
            if case.name == "fallback_invalid_answer":
                assert (
                    row["answer"] is None
                    and row["probabilities"] is None
                    and row["candidate_value"] is None
                )
        elif case.name == "capped":
            assert observation.launched_model == "model-m"
            assert observation.records[0]["outcome"] == "capped"
            assert observation.records[0]["answer"] == "large"
        if case.name in ("installed_purpose", "ephemeral_purpose"):
            assert observation.launched
            assert "purpose-sentinel" not in str(
                observation.records + observation.spans + observation.logs
            )
            requests = getattr(decider, "requests", ())
            assert requests
            if case.name == "installed_purpose":
                assert requests[0].purpose is None
            else:
                assert (
                    "purpose-sentinel" in requests[0].purpose and "ghp_" not in requests[0].purpose
                )
        if case.name == "content_free":
            assert "sentinel-conformance-body" not in str(
                observation.records + observation.spans + observation.logs
            )
    finally:
        if isinstance(adapter, EngineAdapter) and hasattr(adapter, "engine"):
            await adapter.engine.runner.close()
            await adapter.engine.registry.close()


async def run_case(
    case: Case,
    adapter_factory: Callable[[], DecisionPathAdapter],
    decider_factory: Callable[[Behaviour], Decider] = default_fakes,
) -> None:
    variants = {
        "missing_default": ("shadow", "on"),
        "explicit_unmapped": ("shadow", "on"),
        "profile_control": ("shadow", "on"),
        "field_default": ("off", "shadow", "on"),
        "two_point_default": ("shadow", "on"),
    }
    for state in variants.get(case.name, (None,)):
        if case.name == "field_default":
            for behaviour in ("unsure", "confident"):
                await _run_single_case(
                    replace(case, behaviour=behaviour), adapter_factory, decider_factory, state
                )
        else:
            await _run_single_case(case, adapter_factory, decider_factory, state)
