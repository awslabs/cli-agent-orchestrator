"""Fresh operator settings snapshots and local setters."""

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from cli_agent_orchestrator.decisions.types import POINTS, PointState
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.model_tiers import TIERS, load_model_tiers, valid_model

logger = logging.getLogger(__name__)
STATE_ENV = {point: "CAO_DECISION_" + point.upper().replace(".", "_") for point in POINTS}
SAME_USER = "'Operator only' means not settable through MCP or any agent-facing API. It is a same-user local control, not a privilege boundary: settings.json can be edited by the same user, including an agent with shell access."


@dataclass(frozen=True)
class PointSettings:
    state: PointState = PointState.OFF
    decider: str = "fixed_table"
    exclude_profiles: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionSettings:
    points: Mapping[str, PointSettings] = field(
        default_factory=lambda: {p: PointSettings() for p in POINTS}
    )
    on_timeout_ms: int = 1000
    confidence_threshold: float = 0.70
    retention_days: int = 90
    max_concurrent: int = 4
    max_pending: int = 64
    shadow_timeout_ms: int = 10000
    deciders: Mapping[str, Any] = field(default_factory=dict)
    model_tiers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


def _block(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any, default: float, lower: float, upper: float) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = float(value)
        if not math.isfinite(number):
            raise ValueError()
        return min(upper, max(lower, number))
    except (ValueError, TypeError, OverflowError):
        logger.warning("Invalid decision numeric setting; using default")
        return default


def load_settings(
    *, flags: Mapping[str, str] | None = None, environment: Mapping[str, str] | None = None
) -> DecisionSettings:
    try:
        raw = settings_service._load_or_raise()
    except settings_service.SettingsUnreadableError:
        logger.warning("Decision settings unreadable; all points off")
        return DecisionSettings()
    env = os.environ if environment is None else environment
    block = _block(raw.get("decisions"))
    saved = _block(block.get("points"))
    points = {}
    for point in POINTS:
        item = _block(saved.get(point))
        state = (flags or {}).get(point, env.get(STATE_ENV[point], item.get("state", "off")))
        try:
            parsed = PointState(state)
        except (ValueError, TypeError):
            parsed = PointState.OFF
            logger.warning("Invalid decision state; point off")
        exclusions = item.get("exclude_profiles", []) if point == "model.route" else []
        names = (
            tuple(p for p in exclusions if isinstance(p, str))
            if isinstance(exclusions, list)
            else ()
        )
        name = item.get("decider", "fixed_table")
        if not isinstance(name, str):
            logger.warning("Invalid decider name; binding is unavailable")
            name = ""
        points[point] = PointSettings(parsed, name, names)
    shadow = _block(block.get("shadow"))
    return DecisionSettings(
        points=points,
        on_timeout_ms=int(
            _number(
                env.get("CAO_DECISION_ON_TIMEOUT_MS", block.get("on_timeout_ms", 1000)),
                1000,
                50,
                5000,
            )
        ),
        confidence_threshold=_number(
            env.get("CAO_DECISION_CONFIDENCE_THRESHOLD", block.get("confidence_threshold", 0.70)),
            0.70,
            0,
            1,
        ),
        retention_days=int(_number(block.get("retention_days", 90), 90, 1, 36500)),
        max_concurrent=int(_number(shadow.get("max_concurrent", 4), 4, 1, 1024)),
        max_pending=int(_number(shadow.get("max_pending", 64), 64, 0, 100000)),
        shadow_timeout_ms=int(_number(shadow.get("timeout_ms", 10000), 10000, 50, 60000)),
        deciders=_block(block.get("deciders")),
        model_tiers=load_model_tiers(raw),
    )


def _point(point: str) -> None:
    if point not in POINTS:
        raise ValueError("unknown decision point")


def _object(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    block = data
    for index, key in enumerate(keys):
        value = block.setdefault(key, {})
        if not isinstance(value, dict):
            path = ".".join(keys[: index + 1])
            raise ValueError(f'settings.json: "{path}" must be an object')
        block = value
    return block


def set_point(point: str, state: str, decider: str | None = None) -> None:
    _point(point)
    PointState(state)
    data = settings_service._load_or_raise()
    item = _object(data, "decisions", "points", point)
    item["state"] = state
    if decider is not None:
        item["decider"] = decider
    settings_service._save(data)


def set_tier(provider: str, tier: str, model: str | None) -> None:
    if (
        provider not in {p.value for p in ProviderType}
        or tier not in TIERS
        or (model is not None and not valid_model(model))
    ):
        raise ValueError("invalid provider, tier or model")
    data = settings_service._load_or_raise()
    entries = _object(data, "model_tiers", provider)
    if model is None:
        entries.pop(tier, None)
        if not entries:
            data["model_tiers"].pop(provider, None)
    else:
        entries[tier] = model
    settings_service._save(data)


def set_table(
    point: str, option: str, *, profile: str | None = None, role: str | None = None
) -> None:
    _point(point)
    if option not in POINTS[point].options + ("unsure",) or (profile is None) == (role is None):
        raise ValueError("provide one profile or role and a valid option")
    data = settings_service._load_or_raise()
    table = _object(
        data,
        "decisions",
        "deciders",
        "fixed_table",
        point,
        "profiles" if profile is not None else "roles",
    )
    key = profile if profile is not None else role
    assert key is not None
    table[key] = option
    settings_service._save(data)


def set_exclusions(*, add: str | None = None, remove: str | None = None) -> None:
    if (add is None) == (remove is None):
        raise ValueError("provide one exclusion to add or remove")
    data = settings_service._load_or_raise()
    item = _object(data, "decisions", "points", "model.route")
    names = item.setdefault("exclude_profiles", [])
    if not isinstance(names, list):
        raise ValueError(
            'settings.json: "decisions.points.model.route.exclude_profiles" must be an array'
        )
    if add is not None and add not in names:
        names.append(add)
    if remove in names:
        names.remove(remove)
    settings_service._save(data)


def tune(
    *,
    on_timeout_ms: int | None = None,
    threshold: float | None = None,
    retention_days: int | None = None,
) -> None:
    data = settings_service._load_or_raise()
    block = _object(data, "decisions")
    for key, value in (
        ("on_timeout_ms", on_timeout_ms),
        ("confidence_threshold", threshold),
        ("retention_days", retention_days),
    ):
        if value is not None:
            block[key] = value
    settings_service._save(data)


def apply_flags(values: list[str]) -> None:
    for value in values:
        point, sep, state = value.partition("=")
        if not sep or point not in STATE_ENV:
            raise ValueError("decision flag must be <point>=<state>")
        os.environ[STATE_ENV[point]] = state
