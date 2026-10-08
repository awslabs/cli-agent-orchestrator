"""Three-pass launch decisions: policy checks, on answers, then shadow work."""

import asyncio
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Callable, Literal, Mapping, cast

from cli_agent_orchestrator.decisions.owner import DELEGATION, WORKFLOW_STEP
from cli_agent_orchestrator.decisions.policy import (
    IDENTITY_POLICY,
    DecisionInputError,
    DefaultUnmappedTierError,
    EphemeralModelError,
    PolicyBounds,
    PolicyConfigError,
    PolicyViolation,
    ProviderNotAllowedError,
    resolve_fallback,
)
from cli_agent_orchestrator.decisions.registry import DeciderRegistry
from cli_agent_orchestrator.decisions.settings import DecisionSettings, load_settings
from cli_agent_orchestrator.decisions.shadow import ShadowRunner
from cli_agent_orchestrator.decisions.store import DecisionStore
from cli_agent_orchestrator.decisions.targets import TargetKind, profile_source
from cli_agent_orchestrator.decisions.types import (
    CONTRACT_VERSION,
    POINTS,
    Decider,
    DecisionAnswer,
    DecisionFacts,
    DecisionRequest,
    Fallback,
    LaunchPlan,
    MissingTier,
    PointState,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.services.secret_gate import redact_secrets
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

logger = logging.getLogger(__name__)
FALLBACK_REASONS = frozenset(
    (
        "unsure",
        "low_confidence",
        "timeout",
        "error",
        "invalid_answer",
        "no_answer",
        "tier_unmapped",
        "not_honored",
        "decider_unavailable",
    )
)


@dataclass(frozen=True)
class DelegationRequest:
    kind: Literal["assign", "handoff"]
    provider: str
    agent_profile: str | None
    model: str | None
    message: str
    use_worktree: bool = False
    owner: str = DELEGATION
    field_states: Mapping[str, str] = field(default_factory=dict)
    policy: PolicyBounds = IDENTITY_POLICY
    target_kind: TargetKind | None = None
    purpose: str | None = None


def _honors(provider: str, name: str | None, profile: AgentProfile | None) -> bool:
    return bool(ProviderManager.provider_class(provider).honors_model(name, profile, None))


class DecisionEngine:
    def __init__(
        self,
        registry: DeciderRegistry,
        store: DecisionStore,
        runner: ShadowRunner,
        *,
        settings_loader: Callable[[], DecisionSettings] = load_settings,
        profile_loader: Callable[[str], AgentProfile | None] = load_agent_profile,
        honors_model: Callable[[str, str | None, AgentProfile | None], bool] = _honors,
    ) -> None:
        self.registry, self.store, self.runner = registry, store, runner
        self.settings_loader = settings_loader
        self.profile_loader = profile_loader
        self.honors_model = honors_model

    async def prepare_launch(self, req: DelegationRequest) -> LaunchPlan | None:
        settings = self.settings_loader()
        target = req.target_kind or profile_source(req.agent_profile)
        fields = (
            dict(req.field_states)
            if req.field_states
            else (
                {"model.route": "auto"}
                if target == TargetKind.INSTALLED and req.model is None
                else {}
            )
        )
        active = {
            point: settings.points[point]
            for point in fields
            if point in settings.points and settings.points[point].state != PointState.OFF
        }
        if target == TargetKind.INSTALLED and not active:
            return None
        if any(point not in POINTS for point in fields):
            raise DecisionInputError("unknown decision point")
        for point, value in fields.items():
            if value != "auto" and value not in POINTS[point].options:
                raise DecisionInputError(
                    f"{point} must be one of {', '.join(POINTS[point].options)} (got {value!r})"
                )
        try:
            profile = (
                self.profile_loader(req.agent_profile)
                if target == TargetKind.INSTALLED and req.agent_profile
                else None
            )
        except FileNotFoundError:
            profile = None
        tiers = settings.model_tiers.get(req.provider, {})
        fallbacks = {
            point: resolve_fallback(point, profile, req.policy, tiers, req.provider)
            for point in fields
        }
        ids: list[int] = []
        redacted: str | None = None

        def message() -> str:
            nonlocal redacted
            if redacted is None:
                redacted = redact_secrets(req.message)[0]
            return redacted

        def base(point: str, decider: Decider | None = None) -> dict[str, Any]:
            fb = fallbacks[point]
            source = "policy" if isinstance(fb, MissingTier) else fb.source
            value = (
                fb.tier
                if isinstance(fb, MissingTier)
                else (req.policy.default_for(point) if fb.source == "policy" else fb.value)
            )
            return dict(
                contract_version=CONTRACT_VERSION,
                point=point,
                state=active[point].state.value,
                decider=decider.name if decider else None,
                decider_version=decider.version if decider else None,
                kind="workflow_step" if req.owner == WORKFLOW_STEP else req.kind,
                provider=req.provider,
                agent_profile=req.agent_profile,
                fallback_source=source,
                fallback_value=value,
                outcome="fallback",
                decision_status="done",
            )

        def reject(error: PolicyViolation | PolicyConfigError, points: tuple[str, ...]) -> None:
            for point in points:
                if point in active:
                    self.store.insert(
                        {
                            **base(point),
                            "outcome": "rejected",
                            "reason": error.reason,
                            "launch_status": "not_launched",
                        },
                        message(),
                    )
            raise error

        # Validate the whole snapshot before target-specific and field checks.
        try:
            req.policy.validate(settings.model_tiers)
        except (PolicyViolation, PolicyConfigError) as error:
            reject(error, tuple(active))
        if (
            req.policy.allowed_providers is not None
            and req.provider not in req.policy.allowed_providers
        ):
            reject(
                ProviderNotAllowedError(req.provider, req.policy.allowed_providers), tuple(active)
            )
        for point, value in fields.items():
            try:
                if target == TargetKind.EPHEMERAL and req.model is not None:
                    raise EphemeralModelError()
                if value != "auto":
                    req.policy.check_explicit(point, value, req.provider, settings.model_tiers)
            except (PolicyViolation, PolicyConfigError) as error:
                reject(error, (point,))
        if target == TargetKind.EPHEMERAL and req.model is not None and not fields:
            reject(EphemeralModelError(), ())
        for point, fb in fallbacks.items():
            if isinstance(fb, MissingTier):
                reject(
                    DefaultUnmappedTierError(point, fb.provider, fb.tier, "default_unmapped"),
                    (point,),
                )
        model_settings = settings.points.get("model.route")
        model_excluded = (
            target == TargetKind.INSTALLED
            and model_settings is not None
            and req.agent_profile in model_settings.exclude_profiles
        )

        def excluded(point: str) -> bool:
            # An exclusion pins only the model; other points are still asked.
            return model_excluded and point == "model.route"

        eligible = tuple(
            point
            for point, value in fields.items()
            if value == "auto" and not excluded(point) and req.owner != WORKFLOW_STEP
        )
        resolved: dict[str, tuple[Decider | None, str | None]] = {}
        for point in eligible:
            if point in active and active[point].state == PointState.ON:
                resolved[point] = self._resolve(point, req, profile, active[point].decider)
        # Scope selects only the question and record; it never bypasses policy.
        out_of_scope = tuple(
            point
            for point, value in fields.items()
            if value == "auto"
            and point in active
            and req.owner == WORKFLOW_STEP
            and not excluded(point)
        )
        plan_model: str | None = None
        fb_model = fallbacks.get("model.route")
        if isinstance(fb_model, Fallback) and fb_model.source == "policy":
            plan_model = fb_model.value
        if fields.get("model.route") not in (None, "auto"):
            plan_model = tiers.get(fields["model.route"])
        request: DecisionRequest | None = None

        def decision_request() -> DecisionRequest:
            nonlocal request
            if request is None:

                def fact(value: str | None) -> str | None:
                    return (
                        redact_secrets(value)[0]
                        if value is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
                        else None
                    )

                request = DecisionRequest(
                    CONTRACT_VERSION,
                    str(uuid.uuid4()),
                    eligible,
                    message(),
                    redact_secrets(profile.description)[0] if profile else None,
                    DecisionFacts(
                        req.provider,
                        req.kind,
                        fact(req.agent_profile) if target == TargetKind.INSTALLED else None,
                        fact(profile.role) if profile and target == TargetKind.INSTALLED else None,
                        len(message().encode("utf-8")),
                        req.use_worktree,
                    ),
                    purpose=(
                        redact_secrets(req.purpose)[0]
                        if target == TargetKind.EPHEMERAL and req.purpose is not None
                        else None
                    ),
                )
            return request

        def insert(values: dict[str, Any], body: str | None) -> int | None:
            record_id = self.store.insert(values, body)
            if record_id is not None:
                ids.append(record_id)
            return record_id

        try:
            for point in eligible:
                if point not in active or active[point].state != PointState.ON:
                    continue
                decider, reason = resolved[point]
                values = (
                    {"outcome": "fallback", "reason": reason}
                    if reason
                    else await self._ask(
                        point,
                        decider,
                        decision_request(),
                        settings,
                        req.policy,
                        tiers,
                        settings.on_timeout_ms,
                    )
                )
                # A missing default tier was already refused above, before any decider call.
                if point == "model.route" and values.get("candidate_model"):
                    plan_model = values["candidate_model"]
                insert({**base(point, decider), **values}, message())
            for point in eligible:
                if point not in active or active[point].state != PointState.SHADOW:
                    continue
                decider, reason = self._resolve(point, req, profile, active[point].decider)
                if reason:
                    insert(
                        {**base(point, decider), "outcome": "shadow", "reason": reason}, message()
                    )
                    continue
                ask_request = decision_request()
                record_id = insert(
                    {**base(point, decider), "outcome": "shadow", "decision_status": "pending"},
                    message(),
                )
                if record_id is not None:
                    try:
                        self.runner.submit(
                            record_id,
                            partial(
                                self._ask,
                                point,
                                decider,
                                ask_request,
                                settings,
                                req.policy,
                                tiers,
                                settings.shadow_timeout_ms,
                            ),
                        )
                    except Exception:
                        self.store.update_decision(
                            record_id,
                            dict(outcome="shadow", reason="error", decision_status="done"),
                            emit=False,
                        )
            for point in out_of_scope:
                insert({**base(point), "reason": "out_of_scope"}, None)
        except BaseException:
            self.store.bind(tuple(ids), launch_status="not_launched")
            raise
        return LaunchPlan(plan_model, tuple(ids)) if ids or plan_model is not None else None

    def _resolve(
        self, point: str, req: DelegationRequest, profile: AgentProfile | None, name: str
    ) -> tuple[Decider | None, str | None]:
        if point == "model.route" and not self.honors_model(
            req.provider, req.agent_profile, profile
        ):
            return None, "not_honored"
        decider = self.registry.get(name, point)
        return (decider, None) if decider else (None, "decider_unavailable")

    async def _ask(
        self,
        point: str,
        decider: Decider | None,
        request: DecisionRequest,
        settings: DecisionSettings,
        policy: PolicyBounds,
        tiers: Mapping[str, str],
        timeout_ms: int,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        values: dict[str, Any] = dict(
            outcome="fallback",
            reason=None,
            candidate_value=None,
            candidate_model=None,
            applied_value=None,
            answer=None,
            probabilities=None,
        )
        try:
            if decider is None:
                values["reason"] = "decider_unavailable"
                return values
            response = await asyncio.wait_for(
                decider.decide(
                    replace(request, points=(point,)), settings.deciders.get(decider.name, {})
                ),
                timeout_ms / 1000,
            )
            answer = response.get(point) if isinstance(response, Mapping) else None
            if answer is None:
                values["reason"] = "no_answer"
                return values
            if not self._valid(point, answer):
                values["reason"] = "invalid_answer"
                return values
            confidence = answer.probabilities[answer.option]
            values.update(
                answer=answer.option,
                probabilities=dict(answer.probabilities),
                confidence=confidence,
            )
            if answer.option == "unsure":
                values["reason"] = "unsure"
            elif confidence < settings.confidence_threshold:
                values["reason"] = "low_confidence"
            else:
                candidate, capped = policy.cap(point, answer.option)
                model = tiers.get(candidate) if point == "model.route" else None
                if point == "model.route" and model is None:
                    values["reason"] = "tier_unmapped"
                else:
                    values.update(
                        outcome="capped" if capped else "applied",
                        candidate_value=candidate,
                        candidate_model=model,
                        applied_value=candidate,
                    )
        except asyncio.TimeoutError:
            values["reason"] = "timeout"
        except Exception:
            values["reason"] = "error"
        finally:
            values["latency_ms"] = (time.perf_counter() - start) * 1000
        return values

    @staticmethod
    def _valid(point: str, answer: Any) -> bool:
        options = POINTS[point].options + ("unsure",)
        return (
            isinstance(answer, DecisionAnswer)
            and answer.option in options
            and isinstance(answer.probabilities, Mapping)
            and answer.option in answer.probabilities
            and all(
                key in options
                and isinstance(value, (float, int))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and 0 <= value <= 1
                for key, value in answer.probabilities.items()
            )
        )

    def bind_launch(
        self,
        plan: LaunchPlan | None,
        *,
        terminal_id: str | None = None,
        model: str | None = None,
        honored: bool | None = None,
        launch_status: str = "launched",
        not_honored: bool = False,
    ) -> None:
        if plan is not None:
            self.store.bind(
                plan.record_ids,
                terminal_id=terminal_id,
                launched_model=model,
                model_honored=honored,
                launch_status=launch_status,
                not_honored=not_honored,
            )

    def launch_note(self, plan: LaunchPlan | None) -> str | None:
        try:
            for record_id in plan.record_ids if plan else ():
                row = self.store.get(record_id)
                note = render_note(row) if row else None
                if note:
                    return note
        except Exception:
            logger.warning("Decision note read failed")
        return None


def _runtime(engine: DecisionEngine | None) -> DecisionEngine:
    if engine is not None:
        return engine
    from cli_agent_orchestrator.api.main import app

    return cast(DecisionEngine, app.state.decision_engine)


async def prepare_launch(
    req: DelegationRequest, *, engine: DecisionEngine | None = None
) -> LaunchPlan | None:
    return await _runtime(engine).prepare_launch(req)


def bind_launch(
    plan: LaunchPlan | None, *, engine: DecisionEngine | None = None, **kwargs: Any
) -> None:
    _runtime(engine).bind_launch(plan, **kwargs)


def launch_note(plan: LaunchPlan | None, *, engine: DecisionEngine | None = None) -> str | None:
    return _runtime(engine).launch_note(plan)


def render_note(row: Mapping[str, Any]) -> str | None:
    if (
        row.get("point") != "model.route"
        or row.get("state") != "on"
        or row.get("launch_status") != "launched"
    ):
        return None
    model = row.get("launched_model") if row.get("model_honored") else None
    prefix = f"ran on {model}" if model else "ran on provider default"
    outcome = row.get("outcome")
    if outcome == "fallback" and row.get("reason") in FALLBACK_REASONS:
        return f"{prefix} (auto: fallback, {row['reason']})"
    if outcome in ("applied", "capped"):
        text = f"{prefix} (auto: {row['answer']} {row['confidence']:.2f}"
        if outcome == "capped":
            text += f", capped to {row['candidate_value']}"
        return text + ")"
    return None
