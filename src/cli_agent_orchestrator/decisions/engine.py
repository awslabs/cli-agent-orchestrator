"""Three-pass launch decisions: policy checks, on answers, then shadow work."""

import asyncio
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, cast
from urllib.parse import quote

from cli_agent_orchestrator.constants import MODEL_ID_RE
from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
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
# Reasons that mean the decider gave no usable answer. Other reasons are answers, such as
# unsure or low_confidence, except not_honored, which never asked the decider.
DECIDER_FAILURES = frozenset(
    ("timeout", "error", "invalid_answer", "no_answer", "decider_unavailable")
)
_LOG_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_UNSEEN = object()


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


def _label(value: str) -> str:
    # Decider names come from settings; a malformed one must not write arbitrary log text.
    return value if _LOG_LABEL.fullmatch(value) else "<invalid>"


def _file_version(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.lstat()
    except OSError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _settings_version() -> object:
    """A cheap change marker for settings.json and the halt marker."""
    from cli_agent_orchestrator.decisions import egress
    from cli_agent_orchestrator.services import settings_service

    return _file_version(settings_service.SETTINGS_FILE), _file_version(egress.HALT_FILE)


@dataclass
class _FailureStreak:
    reason: str
    reason_warned: bool = False
    warned: bool = False


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
        clock: Callable[[], float] = time.monotonic,
        warning_interval_s: float = 60.0,
        settings_version: Callable[[], object] = _settings_version,
    ) -> None:
        self.registry, self.store, self.runner = registry, store, runner
        self.settings_loader = settings_loader
        self.profile_loader = profile_loader
        self.honors_model = honors_model
        self._clock = clock
        self._warning_interval_s = warning_interval_s
        self._settings_version = settings_version
        self._logged_settings: tuple[Any, ...] | None = None
        self._streaks: dict[tuple[str, str], _FailureStreak] = {}
        self._warned_at: dict[tuple[str, str, str], float] = {}

    def log_settings(self, settings: DecisionSettings) -> None:
        """Log the resolved settings, then again only when they change."""
        try:
            points = ",".join(
                f"{point}={value.state.value}:{_label(value.decider)}"
                for point, value in sorted(settings.points.items())
            )
            summary = (
                settings.confidence_threshold,
                settings.on_timeout_ms,
                settings.shadow_timeout_ms,
                points,
            )
            if summary != self._logged_settings:
                logger.info(
                    "Decision settings: threshold=%s on_timeout_ms=%s shadow_timeout_ms=%s "
                    "points=%s",
                    *summary,
                )
                self._logged_settings = summary
        except Exception:
            # Diagnostics never change a decision or a launch.
            pass

    async def watch_settings(self, *, interval_s: float = 2.0) -> None:
        """Log settings changes as they are saved, without waiting for a launch."""
        seen: object = _UNSEEN
        while True:
            try:
                version = await asyncio.to_thread(self._settings_version)
                if version != seen:
                    if seen is not _UNSEEN:
                        self.log_settings(await asyncio.to_thread(self.settings_loader))
                    seen = version
            except Exception:
                logger.debug("Decision settings check failed")
            await asyncio.sleep(interval_s)

    def _observe(
        self, decider: str, point: str, reason: str | None, asked: Decider | None = None
    ) -> None:
        try:
            self._log_health(decider, point, reason, asked)
        except Exception:
            # Diagnostics never change a decision or a launch.
            pass

    def _log_health(
        self, decider: str, point: str, reason: str | None, asked: Decider | None = None
    ) -> None:
        # Labels only: never the request, profile, purpose, brief or exception text.
        if reason == "not_honored":
            return
        if reason == "no_answer" and type(asked) is FixedTableDecider:
            # The built-in table returns nothing for a profile it has no entry for: an
            # abstention, like unsure, not a failure. External deciders that return no
            # answer still warn, since that reason also covers real outages. Keyed on the
            # built-in's identity, not the configured name, so a package that registers
            # itself under the name "fixed_table" still warns on its own outages.
            reason = None
        key = (decider, point)
        streak = self._streaks.get(key)
        if reason not in DECIDER_FAILURES:
            self._streaks.pop(key, None)
            if streak is not None and streak.warned:
                logger.info(
                    "Decision decider recovered: decider=%s point=%s", _label(decider), point
                )
            return
        if streak is None:
            streak = self._streaks[key] = _FailureStreak(reason)
        elif streak.reason != reason:
            streak.reason, streak.reason_warned = reason, False
        if streak.reason_warned:
            return
        # A suppressed transition is warned about later if the failure persists.
        now = self._clock()
        last = self._warned_at.get((decider, point, reason))
        if last is not None and now - last < self._warning_interval_s:
            return
        logger.warning(
            "Decision decider failing: decider=%s point=%s reason=%s",
            _label(decider),
            point,
            reason,
        )
        self._warned_at[(decider, point, reason)] = now
        streak.reason_warned = streak.warned = True

    async def _ask_observed(self, name: str, point: str, *args: Any) -> dict[str, Any]:
        values = await self._ask(point, *args)
        self._observe(name, point, values.get("reason"), args[0])
        return values

    async def prepare_launch(self, req: DelegationRequest) -> LaunchPlan | None:
        settings = self.settings_loader()
        self.log_settings(settings)
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
                target_kind="workflow" if req.owner == WORKFLOW_STEP else target.value,
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
                self._observe(active[point].decider, point, values["reason"], decider)
                # A missing default tier was already refused above, before any decider call.
                if point == "model.route" and values.get("candidate_model"):
                    plan_model = values["candidate_model"]
                insert({**base(point, decider), **values}, message())
            for point in eligible:
                if point not in active or active[point].state != PointState.SHADOW:
                    continue
                decider, reason = self._resolve(point, req, profile, active[point].decider)
                if reason:
                    self._observe(active[point].decider, point, reason)
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
                                self._ask_observed,
                                active[point].decider,
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
    if model:
        model = "".join(
            char if re.fullmatch(MODEL_ID_RE, char) else quote(char, safe="") for char in model
        )
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
