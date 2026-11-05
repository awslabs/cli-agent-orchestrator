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
from cli_agent_orchestrator.decisions.match import (
    MATCH_DECIDER,
    MatchResult,
    band,
    match_decider,
    match_launch,
    match_none,
    record_columns,
)
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
    FEATURE_NAMES,
    FEATURE_POINTS,
    FEATURES_SWITCH,
    POINTS,
    ROUTE_POINTS,
    Decider,
    DecisionAnswer,
    DecisionFacts,
    DecisionRequest,
    Effort,
    Fallback,
    LaunchPlan,
    MissingTier,
    PointState,
    ProviderOptions,
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
        # #1099 (provisional): provider.route had one option, so nothing was asked.
        "single_option",
        # #1199 (provisional): the local matcher picked nothing; match_rule says why.
        "no_match",
    )
)
# Reasons that mean the decider gave no usable answer. Other reasons are answers, such as
# unsure or low_confidence, except not_honored, single_option and no_match, which never asked
# it.
DECIDER_FAILURES = frozenset(
    ("timeout", "error", "invalid_answer", "no_answer", "decider_unavailable")
)
_LOG_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_UNSEEN = object()


def _unanswered() -> dict[str, Any]:
    return dict(
        outcome="fallback",
        reason=None,
        candidate_value=None,
        candidate_model=None,
        applied_value=None,
        answer=None,
        probabilities=None,
    )


def _group_reason(group: Mapping[str, Mapping[str, Any]]) -> str | None:
    """The feature group's one failure reason, for health logging, or None if it answered."""
    reasons = {values.get("reason") for values in group.values()}
    if len(reasons) == 1:
        (reason,) = reasons
        return reason if reason in DECIDER_FAILURES else None
    return None


def _usable_yes(group: Mapping[str, Mapping[str, Any]]) -> frozenset[str] | None:
    """The features answered a usable yes, or None when no feature got a usable reply."""
    if all(values.get("reason") in DECIDER_FAILURES for values in group.values()):
        return None
    return frozenset(
        FEATURE_NAMES[point]
        for point, values in group.items()
        if values.get("outcome") == "applied" and values.get("answer") == "yes"
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
    # #1068 Q4 (provisional): the creator-written description, sent only for ephemeral targets.
    description: str | None = None
    # #1099 (provisional): provider.route's per-launch options, set only by decision_inputs.
    provider_options: ProviderOptions | None = None
    # #1199 (provisional): set only by decision_inputs for a claimed ephemeral launch. The
    # stored tier the launch runs without a raise, and whether its grant has web_fetch (for
    # the advisory needs_network hint). Neither is ever sent to a decider.
    launch_tier: str | None = None
    network_granted: bool | None = None


def _honors(provider: str, name: str | None, profile: AgentProfile | None) -> bool:
    return bool(ProviderManager.provider_class(provider).honors_model(name, profile, None))


def _honors_effort(provider: str, name: str | None, profile: AgentProfile | None) -> bool:
    return bool(ProviderManager.provider_class(provider).honors_effort(name, profile))


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
        honors_effort: Callable[[str, str | None, AgentProfile | None], bool] = _honors_effort,
        clock: Callable[[], float] = time.monotonic,
        warning_interval_s: float = 60.0,
        settings_version: Callable[[], object] = _settings_version,
    ) -> None:
        self.registry, self.store, self.runner = registry, store, runner
        self.settings_loader = settings_loader
        self.profile_loader = profile_loader
        self.honors_model = honors_model
        self.honors_effort = honors_effort
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
        if reason in ("not_honored", "single_option"):
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
        # #1199: the task-feature points are never launch fields; only the engine asks them.
        if any(point not in ROUTE_POINTS for point in fields):
            raise DecisionInputError("unknown decision point")
        for point, value in fields.items():
            if value != "auto" and value not in POINTS[point].options:
                raise DecisionInputError(
                    f"{point} must be one of {', '.join(POINTS[point].options)} (got {value!r})"
                )
        # #1099: only decision_inputs asks provider.route, for a claimed ephemeral launch and
        # with options CAO computed that contain the fallback. Anything else is a bug.
        route_options = req.provider_options
        if "provider.route" in fields and (
            fields["provider.route"] != "auto"
            or target != TargetKind.EPHEMERAL
            or route_options is None
            or req.provider not in route_options.options
            or len(set(route_options.options)) != len(route_options.options)
            or set(route_options.labels) != set(route_options.options)
        ):
            raise DecisionInputError("provider.route needs CAO's per-launch options")
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
        plan_effort: Effort | None = None
        plan_provider: str | None = None
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
                    (
                        redact_secrets(profile.description)[0]
                        if profile
                        else (
                            redact_secrets(req.description)[0]
                            if target == TargetKind.EPHEMERAL and req.description is not None
                            else None
                        )
                    ),
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

        def request_for(point: str) -> DecisionRequest:
            # #1099 Q4 (provisional): every ask of one launch shares one request and one
            # request_id. provider.route sees the fallback and this launch's options; the
            # points asked after it see the provider it resolved to.
            shared = decision_request()
            if point == "provider.route":
                assert route_options is not None  # The invariant check above.
                return replace(shared, options=route_options.options)
            if shared.facts.provider == req.provider:
                return shared
            return replace(shared, facts=replace(shared.facts, provider=req.provider))

        def feature_request() -> DecisionRequest:
            # #1199: the same request as this launch's model.route ask, with the four feature
            # points: the same message, description, purpose, facts and request_id (AC2).
            return replace(request_for("model.route"), points=FEATURE_POINTS)

        def insert(values: dict[str, Any], body: str | None) -> int | None:
            record_id = self.store.insert(values, body)
            if record_id is not None:
                ids.append(record_id)
            return record_id

        def shadow(point: str, ask_tiers: Mapping[str, str], extra: Mapping[str, Any]) -> None:
            decider, reason = self._resolve(point, req, profile, active[point].decider)
            if reason:
                self._observe(active[point].decider, point, reason)
                insert(
                    {**base(point, decider), **extra, "outcome": "shadow", "reason": reason},
                    message(),
                )
                return
            ask_request = request_for(point)
            record_id = insert(
                {
                    **base(point, decider),
                    **extra,
                    "outcome": "shadow",
                    "decision_status": "pending",
                },
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
                            ask_tiers,
                            settings.shadow_timeout_ms,
                        ),
                    )
                except Exception:
                    self.store.update_decision(
                        record_id,
                        dict(outcome="shadow", reason="error", decision_status="done"),
                        emit=False,
                    )

        # #1199 (provisional): the task features are asked for a claimed ephemeral launch whose
        # spec declared provider or model_tier auto, when the task.features switch isn't off.
        switch = settings.points.get(FEATURES_SWITCH)
        features = (
            switch
            if switch is not None
            and switch.state != PointState.OFF
            and target == TargetKind.EPHEMERAL
            and req.owner != WORKFLOW_STEP
            and ("provider.route" in fields or fields.get("model.route") == "auto")
            else None
        )
        plan_hints: tuple[str, ...] = ()

        def feature_base(point: str, decider: Decider | None) -> dict[str, Any]:
            assert features is not None
            return dict(
                contract_version=CONTRACT_VERSION,
                point=point,
                state=features.state.value,
                decider=decider.name if decider else features.decider,
                decider_version=decider.version if decider else None,
                kind=req.kind,
                provider=req.provider,
                agent_profile=req.agent_profile,
                target_kind=target.value,
                fallback_source="none",
                fallback_value=None,
                outcome="fallback",
                decision_status="done",
            )

        def switch_provider(decided: str) -> None:
            """Run the rest of this plan on a decided provider (#1099), whoever decided it."""
            nonlocal req, tiers, plan_provider
            req = replace(req, provider=decided)
            tiers = settings.model_tiers.get(decided, {})
            for point in fields:
                if point != "provider.route":
                    fallbacks[point] = resolve_fallback(point, profile, req.policy, tiers, decided)
            # Defensive: the options already required both for the decided one.
            others = tuple(point for point in active if point != "provider.route")
            if (
                req.policy.allowed_providers is not None
                and decided not in req.policy.allowed_providers
            ):
                reject(ProviderNotAllowedError(decided, req.policy.allowed_providers), others)
            for point, fb in fallbacks.items():
                if isinstance(fb, MissingTier):
                    reject(
                        DefaultUnmappedTierError(point, fb.provider, fb.tier, "default_unmapped"),
                        (point,),
                    )
            plan_provider = decided

        try:
            # #1099: provider.route is asked first, so every provider-dependent value below
            # (the tier table, honors_model and honors_effort, the fallbacks, facts.provider)
            # is the resolved provider's. From here every record binds on any failure.
            # #1199: with the built-in matcher as its answerer and the features asked, the
            # provider.route row waits for the features and the size instead (deferred).
            route = active.get("provider.route")
            route_extra: dict[str, Any] = {}
            deferred_route = False
            if route is not None:
                assert route_options is not None  # The invariant check above.
                labels = route_options.labels
                # #1099 (provisional): the record carries the options and both labels.
                route_extra = dict(
                    options=list(route_options.options),
                    fallback_enforcement=labels[req.provider],
                )
                if req.owner == WORKFLOW_STEP:
                    insert(
                        {**base("provider.route"), **route_extra, "reason": "out_of_scope"}, None
                    )
                elif len(route_options.options) == 1:
                    # Nothing to choose between: no decider is resolved or asked.
                    self._observe(route.decider, "provider.route", "single_option")
                    insert(
                        {
                            **base("provider.route"),
                            **route_extra,
                            "outcome": "shadow" if route.state == PointState.SHADOW else "fallback",
                            "reason": "single_option",
                        },
                        message(),
                    )
                elif route.decider == MATCH_DECIDER:
                    if features is None:
                        # No features, so nothing to match: the fallback runs (no_features).
                        result = match_none(settings, req.provider, "no_features")
                        insert(
                            {
                                **base("provider.route"),
                                **route_extra,
                                **match_decider(),
                                **record_columns(result, settings),
                                "outcome": (
                                    "shadow" if route.state == PointState.SHADOW else "fallback"
                                ),
                                "reason": "no_match",
                            },
                            message(),
                        )
                    else:
                        deferred_route = True
                elif route.state == PointState.SHADOW:
                    shadow("provider.route", {}, route_extra)
                else:
                    decider, reason = self._resolve("provider.route", req, profile, route.decider)
                    values = (
                        {"outcome": "fallback", "reason": reason}
                        if reason
                        else await self._ask(
                            "provider.route",
                            decider,
                            request_for("provider.route"),
                            settings,
                            req.policy,
                            {},
                            settings.on_timeout_ms,
                        )
                    )
                    self._observe(route.decider, "provider.route", values["reason"], decider)
                    decided = values["applied_value"] if values["outcome"] == "applied" else None
                    insert(
                        {
                            **base("provider.route", decider),
                            **route_extra,
                            **values,
                            # The provider this plan launches on; fallback_value stays the
                            # creator's.
                            "provider": decided or req.provider,
                            "applied_enforcement": labels[decided] if decided else None,
                        },
                        message(),
                    )
                    if decided is not None and decided != req.provider:
                        switch_provider(decided)
            resolved: dict[str, tuple[Decider | None, str | None]] = {}
            for point in eligible:
                if (
                    point != "provider.route"
                    and point in active
                    and active[point].state == PointState.ON
                ):
                    resolved[point] = self._resolve(point, req, profile, active[point].decider)
            # Scope selects only the question and record; it never bypasses policy.
            out_of_scope = tuple(
                point
                for point, value in fields.items()
                if value == "auto"
                and point in active
                and req.owner == WORKFLOW_STEP
                and not excluded(point)
                and point != "provider.route"
            )
            plan_model: str | None = None
            fb_model = fallbacks.get("model.route")
            if isinstance(fb_model, Fallback) and fb_model.source == "policy":
                plan_model = fb_model.value
            if fields.get("model.route") not in (None, "auto"):
                plan_model = tiers.get(fields["model.route"])
            # #1199: an on-mode feature ask runs beside model.route, never after it.
            feature_decider: Decider | None = None
            feature_task: (
                asyncio.Future[tuple[dict[str, dict[str, Any]] | None, BaseException | None]] | None
            ) = None
            if features is not None:
                feature_decider = self._resolve_features(features.decider)
                if features.state == PointState.ON and feature_decider is not None:
                    asked_decider = feature_decider

                    async def ask_features() -> (
                        tuple[dict[str, dict[str, Any]] | None, BaseException | None]
                    ):
                        try:
                            return (
                                await self._ask_features(
                                    asked_decider,
                                    feature_request(),
                                    settings,
                                    settings.on_timeout_ms,
                                ),
                                None,
                            )
                        except (SystemExit, KeyboardInterrupt) as exc:
                            # Raised in this owner below, never in a child task, where it
                            # would stop the server's event loop (as _call does).
                            return None, exc

                    feature_task = asyncio.ensure_future(ask_features())
            model_ask: tuple[Decider | None, dict[str, Any]] | None = None
            feature_values: dict[str, dict[str, Any]] | None = None
            try:
                for point in eligible:
                    if (
                        point == "provider.route"
                        or point not in active
                        or active[point].state != PointState.ON
                    ):
                        continue
                    # #1199: with features asked, effort.route waits for the match's CLI.
                    if features is not None and point == "effort.route":
                        continue
                    decider, reason = resolved[point]
                    values = (
                        {"outcome": "fallback", "reason": reason}
                        if reason
                        else await self._ask(
                            point,
                            decider,
                            request_for(point),
                            settings,
                            req.policy,
                            tiers,
                            settings.on_timeout_ms,
                        )
                    )
                    self._observe(active[point].decider, point, values["reason"], decider)
                    if features is not None and point == "model.route":
                        # #1199: recorded after the match, which may raise its tier.
                        model_ask = (decider, values)
                        continue
                    # A missing default tier was already refused above, before any decider call.
                    if point == "model.route" and values.get("candidate_model"):
                        plan_model = values["candidate_model"]
                    # #1083 Q1 (provisional): only an on-mode answer within the bounds is carried.
                    if point == "effort.route" and values.get("outcome") in ("applied", "capped"):
                        plan_effort = cast(Effort, values["applied_value"])
                    insert({**base(point, decider), **values}, message())
                if feature_task is not None:
                    feature_values, escaped = await feature_task
                    if escaped is not None:
                        raise escaped
            finally:
                if feature_task is not None:
                    try:
                        if not feature_task.done():
                            feature_task.cancel()
                            await asyncio.gather(feature_task, return_exceptions=True)
                    finally:
                        # A fatal exception the ask transported must not vanish, even if this
                        # owner was cancelled first.
                        if (
                            feature_task.done()
                            and not feature_task.cancelled()
                            and feature_task.exception() is None
                        ):
                            _, escaped = feature_task.result()
                            if escaped is not None:
                                raise escaped
            match_targets: list[tuple[int, str]] = []
            if features is not None:
                # #1199: record the features, match, then record provider.route and model.route.
                assert route is not None or not deferred_route
                fallback_provider = req.provider
                model_usable = model_ask is not None and model_ask[1]["outcome"] in (
                    "applied",
                    "capped",
                )
                size = (
                    model_ask[1]["candidate_value"]
                    if model_ask is not None and model_usable
                    else req.launch_tier
                )
                bounds = (req.policy.min_tier, req.policy.max_tier)
                route_options_now = (
                    (route_options.options, route_options.labels)
                    if deferred_route and route_options is not None
                    else None
                )
                applied_route = (
                    deferred_route and route is not None and route.state == PointState.ON
                )
                yes: frozenset[str] | None = None
                if features.state == PointState.ON:
                    group = feature_values or {
                        point: dict(_unanswered(), reason="decider_unavailable")
                        for point in FEATURE_POINTS
                    }
                    self._observe(
                        features.decider, FEATURES_SWITCH, _group_reason(group), feature_decider
                    )
                    for point in FEATURE_POINTS:
                        insert({**feature_base(point, feature_decider), **group[point]}, message())
                    yes = _usable_yes(group)
                    if "needs_network" in (yes or ()) and req.network_granted is False:
                        plan_hints = ("needs_network",)

                def matches(
                    answered: frozenset[str] | None,
                ) -> tuple[MatchResult | None, MatchResult | None]:
                    """The route match (all options) and the applied match, for these answers."""
                    route_result = (
                        match_launch(
                            settings,
                            route_options_now[0],
                            route_options_now[1],
                            fallback_provider,
                            band(size, *bounds, raise_allowed=model_usable),
                            size,
                            answered,
                        )
                        if route_options_now is not None
                        else None
                    )
                    if applied_route:
                        return route_result, route_result
                    applied = (
                        match_launch(
                            settings,
                            (fallback_provider,),
                            None,
                            fallback_provider,
                            band(size, *bounds, raise_allowed=True),
                            size,
                            answered,
                        )
                        if model_usable
                        else None
                    )
                    return route_result, applied

                on = features.state == PointState.ON
                route_result, applied = matches(yes) if on else (None, None)
                if deferred_route:
                    assert route is not None and route_options is not None
                    picked = route_result if on and route_result is not None else None
                    if picked is not None and picked.picked and applied_route:
                        values = dict(
                            outcome="applied",
                            reason=None,
                            answer=picked.provider,
                            probabilities={picked.provider: 1.0},
                            confidence=1.0,
                            candidate_value=picked.provider,
                            applied_value=picked.provider,
                            provider=picked.provider,
                            applied_enforcement=route_options.labels[picked.provider],
                        )
                    elif picked is not None and picked.picked:
                        # provider.route in shadow: the would-be CLI, never applied.
                        values = dict(
                            outcome="shadow",
                            reason=None,
                            answer=picked.provider,
                            probabilities={picked.provider: 1.0},
                            confidence=1.0,
                            candidate_value=picked.provider,
                        )
                    else:
                        values = dict(
                            outcome="shadow" if route.state == PointState.SHADOW else "fallback",
                            reason="no_match",
                        )
                    record_id = insert(
                        {
                            **base("provider.route"),
                            **route_extra,
                            **match_decider(),
                            **values,
                            **(record_columns(picked, settings) if picked is not None else {}),
                        },
                        message(),
                    )
                    if record_id is not None:
                        match_targets.append((record_id, "route"))
                # As #1099 does once its row is recorded: the rest runs on the matched CLI.
                if applied is not None and applied.picked and applied.provider != req.provider:
                    switch_provider(applied.provider)
                # The plan's model, as #1099 computes it after a decided provider.
                plan_model = None
                fb_model = fallbacks.get("model.route")
                if isinstance(fb_model, Fallback) and fb_model.source == "policy":
                    plan_model = fb_model.value
                if model_ask is not None:
                    decider, values = model_ask
                    values = dict(values)
                    if applied is not None:
                        if applied.picked and model_usable and applied.tier is not None:
                            # The raise (#1199): the size stays in candidate_value and
                            # match_from; the tier the match picked is what applies.
                            values.update(
                                candidate_model=settings.model_tiers[applied.provider][
                                    applied.tier
                                ],
                                applied_value=applied.tier,
                            )
                        values.update(record_columns(applied, settings, size=size, model_row=True))
                    # A missing default tier was already refused above, before any decider call.
                    if values.get("candidate_model") and values.get("outcome") in (
                        "applied",
                        "capped",
                    ):
                        plan_model = values["candidate_model"]
                    record_id = insert({**base("model.route", decider), **values}, message())
                    if record_id is not None and (applied_route or model_usable):
                        match_targets.append((record_id, "model"))
                for point in eligible:
                    if (
                        point != "effort.route"
                        or point not in active
                        or active[point].state != PointState.ON
                    ):
                        continue
                    decider, reason = self._resolve(point, req, profile, active[point].decider)
                    values = (
                        {"outcome": "fallback", "reason": reason}
                        if reason
                        else await self._ask(
                            point,
                            decider,
                            request_for(point),
                            settings,
                            req.policy,
                            tiers,
                            settings.on_timeout_ms,
                        )
                    )
                    self._observe(active[point].decider, point, values["reason"], decider)
                    if values.get("outcome") in ("applied", "capped"):
                        plan_effort = cast(Effort, values["applied_value"])
                    insert({**base(point, decider), **values}, message())
            for point in eligible:
                if (
                    point == "provider.route"
                    or point not in active
                    or active[point].state != PointState.SHADOW
                ):
                    continue
                shadow(point, tiers, {})
            if features is not None and features.state == PointState.SHADOW:
                # #1199: four pending rows and one background ask; when the answers arrive,
                # the would-be match is written onto the rows `on` would have matched.
                feature_ids = [
                    insert(
                        {
                            **feature_base(point, feature_decider),
                            "outcome": "shadow",
                            **(
                                {"decision_status": "pending"}
                                if feature_decider is not None
                                else {"reason": "decider_unavailable"}
                            ),
                        },
                        message(),
                    )
                    for point in FEATURE_POINTS
                ]
                self._submit_features(
                    tuple(feature_ids),
                    feature_decider,
                    features.decider,
                    feature_request(),
                    settings,
                    matches,
                    tuple(match_targets),
                    size,
                )
            for point in out_of_scope:
                insert({**base(point), "reason": "out_of_scope"}, None)
        except BaseException:
            self.store.bind(tuple(ids), launch_status="not_launched")
            raise
        if (
            ids
            or plan_model is not None
            or plan_effort is not None
            or plan_provider is not None
            or plan_hints
        ):
            return LaunchPlan(plan_model, tuple(ids), plan_effort, plan_provider, plan_hints)
        return None

    def _resolve(
        self, point: str, req: DelegationRequest, profile: AgentProfile | None, name: str
    ) -> tuple[Decider | None, str | None]:
        if point == "model.route" and not self.honors_model(
            req.provider, req.agent_profile, profile
        ):
            return None, "not_honored"
        # #1083 (provisional): a provider that cannot apply an effort is never asked for one.
        if point == "effort.route" and not self.honors_effort(
            req.provider, req.agent_profile, profile
        ):
            return None, "not_honored"
        # #1199: the built-in matcher's name is reserved; no package registered under it is
        # ever asked.
        if name == MATCH_DECIDER:
            return None, "decider_unavailable"
        decider = self.registry.get(name, point)
        return (decider, None) if decider else (None, "decider_unavailable")

    def _resolve_features(self, name: str) -> Decider | None:
        """#1199: only a decider declaring all four feature points gets the group."""
        if name == MATCH_DECIDER:
            return None
        return self.registry.get_all(name, FEATURE_POINTS)

    async def _call(
        self,
        decider: Decider,
        request: DecisionRequest,
        settings: DecisionSettings,
        timeout_ms: int,
    ) -> tuple[Any, str | None]:
        """One contained decide() within the timeout: (response, None), or (None, reason).

        Raises asyncio.TimeoutError on a timeout; any other decider error propagates to the
        caller, which records it. SystemExit and KeyboardInterrupt reach the owner.
        """

        async def contained() -> tuple[Any, BaseException | None]:
            try:
                return (
                    await decider.decide(request, settings.deciders.get(decider.name, {})),
                    None,
                )
            except (SystemExit, KeyboardInterrupt) as exc:
                # These must propagate through the owner, not escape a child
                # task callback and stop the server's event loop.
                return None, exc

        task = asyncio.ensure_future(contained())
        try:
            # wait does not raise the decider's own cancellation. Cancellation
            # of this launch still propagates from the await, including on 3.10.
            done, _ = await asyncio.wait({task}, timeout=timeout_ms / 1000)
            if not done:
                # Match wait_for: wait for cancellation cleanup, including a
                # decider that returns an answer or raises another error there.
                task.cancel()
                await asyncio.wait({task})
                if task.cancelled():
                    raise asyncio.TimeoutError()
            try:
                response, _ = task.result()
            except asyncio.CancelledError:
                return None, "error"
            return response, None
        finally:
            try:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            finally:
                # Inspect completed cleanup even if another owner cancellation
                # interrupts gather; transported fatal exceptions must not vanish.
                if task.done() and not task.cancelled() and task.exception() is None:
                    _, escaped = task.result()
                    if escaped is not None:
                        raise escaped

    def _evaluate(
        self,
        point: str,
        response: Any,
        request: DecisionRequest,
        settings: DecisionSettings,
        policy: PolicyBounds,
        tiers: Mapping[str, str],
        values: dict[str, Any],
    ) -> None:
        """Validate one point's answer from a response, then threshold and cap it."""
        answer = response.get(point) if isinstance(response, Mapping) else None
        if answer is None:
            values["reason"] = "no_answer"
            return
        if not self._valid(point, answer, request):
            values["reason"] = "invalid_answer"
            return
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
        values: dict[str, Any] = _unanswered()
        try:
            if decider is None:
                values["reason"] = "decider_unavailable"
                return values
            response, failure = await self._call(
                decider, replace(request, points=(point,)), settings, timeout_ms
            )
            if failure is not None:
                values["reason"] = failure
                return values
            self._evaluate(point, response, request, settings, policy, tiers, values)
        except asyncio.TimeoutError:
            values["reason"] = "timeout"
        except Exception:
            values["reason"] = "error"
        finally:
            values["latency_ms"] = (time.perf_counter() - start) * 1000
        return values

    async def _ask_features(
        self,
        decider: Decider,
        request: DecisionRequest,
        settings: DecisionSettings,
        timeout_ms: int,
    ) -> dict[str, dict[str, Any]]:
        """#1199: one request with all four feature points; each answer is checked on its own.

        A failed call gives every feature that reason. A missing or invalid answer affects
        only its own feature. The answers are thresholded like any point and never capped.
        """
        start = time.perf_counter()
        group = {point: _unanswered() for point in FEATURE_POINTS}
        try:
            response, failure = await self._call(
                decider, replace(request, points=FEATURE_POINTS), settings, timeout_ms
            )
            if failure is not None:
                for values in group.values():
                    values["reason"] = failure
            else:
                for point, values in group.items():
                    self._evaluate(point, response, request, settings, IDENTITY_POLICY, {}, values)
        except asyncio.TimeoutError:
            for values in group.values():
                values["reason"] = "timeout"
        except Exception:
            for values in group.values():
                values.update(_unanswered(), reason="error")
        finally:
            latency = (time.perf_counter() - start) * 1000
            for values in group.values():
                values["latency_ms"] = latency
        return group

    def _submit_features(
        self,
        record_ids: tuple[int | None, ...],
        decider: Decider | None,
        name: str,
        request: DecisionRequest,
        settings: DecisionSettings,
        matches: Callable[[frozenset[str] | None], tuple[MatchResult | None, MatchResult | None]],
        targets: tuple[tuple[int, str], ...],
        size: str | None,
    ) -> None:
        """#1199: ask the features in the background, then write the would-be match.

        ``targets`` are the provider.route ("route") and model.route ("model") rows that `on`
        would have matched; each gets its would-be match columns once the answers arrive, or
        no_features when they never do.
        """

        def write(answers: tuple[MatchResult | None, MatchResult | None]) -> None:
            route_result, applied = answers
            for record_id, kind in targets:
                result = route_result if kind == "route" else applied
                if result is not None:
                    self.store.update_match(
                        (record_id,),
                        record_columns(result, settings, size=size, model_row=kind == "model"),
                    )

        if decider is None:
            self._observe(name, FEATURES_SWITCH, "decider_unavailable")
            write(matches(None))
            return
        selected = decider
        pairs = tuple(
            (record_id, point)
            for record_id, point in zip(record_ids, FEATURE_POINTS)
            if record_id is not None
        )

        async def work() -> dict[int, dict[str, Any]]:
            try:
                group = await self._ask_features(
                    selected, request, settings, settings.shadow_timeout_ms
                )
            except BaseException:
                write(matches(None))
                raise
            self._observe(name, FEATURES_SWITCH, _group_reason(group), selected)
            try:
                write(matches(_usable_yes(group)))
            except Exception:
                logger.warning("Decision shadow match failed")
            return {record_id: group[point] for record_id, point in pairs}

        try:
            submitted = self.runner.submit_group(tuple(r for r, _ in pairs), work)
        except Exception:
            submitted = False
            for record_id, _ in pairs:
                self.store.update_decision(
                    record_id,
                    dict(outcome="shadow", reason="error", decision_status="done"),
                    emit=False,
                )
        if not submitted:
            write(matches(None))

    @staticmethod
    def _valid(point: str, answer: Any, request: DecisionRequest) -> bool:
        # #1099: a provider.route answer must be one of this launch's options, never just a
        # known provider id; a provider.route request without options accepts nothing.
        if point == "provider.route":
            if request.options is None:
                return False
            options = request.options + ("unsure",)
        else:
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
        note: str | None = None
        try:
            rows = [self.store.get(record_id) for record_id in (plan.record_ids if plan else ())]
            # #1199: a match applies only with the task features on. In shadow, the background
            # ask later writes the would-be match onto these same rows; the note never shows it.
            applied = any(
                row is not None and row.get("point") in FEATURE_POINTS and row.get("state") == "on"
                for row in rows
            )
            # #1199: whether a match moved this launch to another CLI, for the note.
            moved = any(
                row is not None
                and row.get("point") == "provider.route"
                and row.get("decider") == MATCH_DECIDER
                and row.get("outcome") == "applied"
                and row.get("answer") != row.get("fallback_value")
                for row in rows
            )
            for row in rows:
                note = render_note(row, moved=moved, matched=applied) if row else None
                if note:
                    break
        except Exception:
            logger.warning("Decision note read failed")
            note = None
        # #1199: an advisory hint never changes the launch; it only reaches the creator.
        hints = [HINTS[hint] for hint in (plan.hints if plan else ()) if hint in HINTS]
        return "; ".join([note] + hints if note else hints) or None


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


# #1199: the delegation note's advisory hints, by closed-vocabulary name. ASCII only: the note
# travels in an HTTP header.
HINTS = {
    "needs_network": "the task may need network access, and this helper has no web_fetch",
}


def _matched(row: Mapping[str, Any], moved: bool) -> str:
    """#1199: ", matched <needs>[ -> [<cli> ]<tier>]" for a model.route row a match picked."""
    if row.get("match_rule") != "covered" or not isinstance(row.get("match_pick"), str):
        return ""
    covered = row.get("match_covered") or []
    provider, _, tier = row["match_pick"].partition("/")
    text = ", matched " + "+".join(name for name in covered if isinstance(name, str))
    if moved:
        return text + f" -> {provider} {tier}"
    if tier != row.get("match_from"):
        return text + f" -> {tier}"
    return text


def render_note(
    row: Mapping[str, Any], *, moved: bool = False, matched: bool = False
) -> str | None:
    """The delegation note for a model.route row; ``matched`` when this launch applied a match.

    #1199: the match text needs ``matched``, because a shadow launch's rows also get the
    would-be match, written after the launch.
    """
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
        return text + (_matched(row, moved) if matched else "") + ")"
    return None
