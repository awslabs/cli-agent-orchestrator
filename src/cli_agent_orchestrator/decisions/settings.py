"""Fresh operator settings snapshots and local setters."""

import logging
import math
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from cli_agent_orchestrator.decisions.types import POINTS, PointState
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.model_tiers import TIERS, load_model_tiers, valid_model

logger = logging.getLogger(__name__)
STATE_ENV = {point: "CAO_DECISION_" + point.upper().replace(".", "_") for point in POINTS}
STATES = tuple(state.value for state in PointState)
# Accepted ranges: `tune` refuses values outside them; loading clamps hand edits and overrides.
ON_TIMEOUT_MS_RANGE = (50, 5000)
THRESHOLD_RANGE = (0.0, 1.0)
RETENTION_DAYS_RANGE = (1, 36500)
# Shadow limits have no setter; loading clamps them to these ranges.
MAX_CONCURRENT_RANGE = (1, 1024)
MAX_PENDING_RANGE = (0, 100000)
SHADOW_TIMEOUT_MS_RANGE = (50, 60000)
SAME_USER = "'Operator only' means an operator context: over the operations MCP server a bound CAO terminal needs the '@cao-ops-mcp-server' grant, and lifting a halt is refused there even with it. It is a same-user local control, not a privilege boundary: settings.json can be edited by the same user, including an agent with shell access."

#: The ``set_point`` states that CLEAR an active halt marker, named once so the
#: mutation below and the MCP gate that refuses it (#1062 item 3,
#: ``ops_mcp_server/gate.py``) cannot drift. Two hand-written ``("shadow", "on")``
#: literals is how a guard stops guarding: the mutation gains a state and the
#: gate keeps checking the old pair.
HALT_LIFTING_STATES = frozenset({PointState.SHADOW.value, PointState.ON.value})


class CallerPosture(Enum):
    """Who is asking for a decision-settings mutation.

    Only one rule depends on it -- a BOUND caller may not lift an active halt
    (#1062 item 3) -- but that rule has to be evaluated INSIDE the mutation,
    under the lock that protects the halt marker, so the mutation has to be told
    the posture rather than deriving it. Deriving it here is not an option: this
    module is also reached by ``cao decisions set`` from an agent's own shell,
    which runs WITH ``CAO_TERMINAL_ID`` set and is documented (see
    :data:`SAME_USER`) as a same-user local control, not a privilege boundary.

    ``BOUND`` is the DEFAULT, so a caller that says nothing cannot lift a halt.
    #671's lesson, applied here: a control that depends on every future call site
    remembering to opt in is a control that lapses. The operator paths -- which
    are two, ``cli/commands/decisions.py`` and whatever a human runs in process
    -- state ``OPERATOR`` explicitly, and ``cao decisions set`` lifting a halt is
    pinned by ``test_enabling_point_prints_effective_states``, so dropping that
    argument fails loudly rather than quietly costing an operator the only way
    to lift one.
    """

    OPERATOR = "operator"
    BOUND = "bound"


#: Refused to a bound caller that would lift an active halt, by the mutation and
#: (as a faster, friendlier refusal) by the ops MCP middleware. One string, so
#: the two layers cannot describe the same rule differently.
HALT_LIFT_DENIAL = (
    "'decisions_set_point' is not permitted: lifting an active decisions halt is "
    "operator-only and is not available over MCP to a bound CAO terminal. Run "
    "`cao decisions set <point> <state>` locally."
)

#: Refused to a bound caller when the halt state could not be read exclusively.
#: Fail closed: an unknown halt state must not be lifted.
HALT_LOCK_BUSY_DENIAL = (
    "'decisions_set_point' is not permitted: the decisions lock is held elsewhere, "
    "so an active halt could not be ruled out. Retry, or run "
    "`cao decisions set <point> <state>` locally."
)


class HaltLiftRefused(ValueError):
    """A bound caller's mutation would have lifted an active operator halt.

    A ``ValueError`` on purpose: both the ops MCP tools' ``_result`` and the
    CLI's ``_run`` turn a ``ValueError`` into the message the caller is shown,
    while any other exception becomes a generic "operation failed" -- and a
    refusal the caller cannot read is a refusal it will retry forever.
    """


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
        if not lower <= number <= upper:
            logger.warning("Decision numeric setting out of range; clamped")
        return min(upper, max(lower, number))
    except (ValueError, TypeError, OverflowError):
        logger.warning("Invalid decision numeric setting; using default")
        return default


def load_settings(*, environment: Mapping[str, str] | None = None) -> DecisionSettings:
    try:
        raw = settings_service._load_or_raise()
    except settings_service.SettingsUnreadableError:
        logger.warning("Decision settings unreadable; all points off")
        return DecisionSettings()
    from cli_agent_orchestrator.decisions.egress import is_halted

    halted = is_halted()
    env = os.environ if environment is None else environment
    block = _block(raw.get("decisions"))
    saved = _block(block.get("points"))
    points = {}
    for point in POINTS:
        item = _block(saved.get(point))
        state = env.get(STATE_ENV[point], item.get("state", "off"))
        try:
            parsed = PointState(state)
        except (ValueError, TypeError):
            parsed = PointState.OFF
            logger.warning("Invalid decision state; point off")
        if halted:
            parsed = PointState.OFF
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
                *ON_TIMEOUT_MS_RANGE,
            )
        ),
        confidence_threshold=_number(
            env.get("CAO_DECISION_CONFIDENCE_THRESHOLD", block.get("confidence_threshold", 0.70)),
            0.70,
            *THRESHOLD_RANGE,
        ),
        retention_days=int(_number(block.get("retention_days", 90), 90, *RETENTION_DAYS_RANGE)),
        max_concurrent=int(_number(shadow.get("max_concurrent", 4), 4, *MAX_CONCURRENT_RANGE)),
        max_pending=int(_number(shadow.get("max_pending", 64), 64, *MAX_PENDING_RANGE)),
        shadow_timeout_ms=int(
            _number(shadow.get("timeout_ms", 10000), 10000, *SHADOW_TIMEOUT_MS_RANGE)
        ),
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


def _write_point(point: str, state: str, decider: str | None) -> None:
    data = settings_service._load_or_raise()
    item = _object(data, "decisions", "points", point)
    item["state"] = state
    if decider is not None:
        item["decider"] = decider
    settings_service._save(data)


def set_point(
    point: str,
    state: str,
    decider: str | None = None,
    *,
    posture: CallerPosture = CallerPosture.BOUND,
) -> None:
    """Save a point's state, lifting an active halt when the state lifts one.

    "A bound caller may not lift a halt" (#1062 item 3) is enforced HERE, not
    only in the ops MCP middleware, because authorization has to be bound to the
    mutation it authorizes. Independent review of #1062 showed the middleware
    check alone was check-then-use: a granted bound caller whose
    ``decisions_set_point(on)`` passed the middleware while no marker existed
    raced ``operator.off()``, which published the marker -- and the mutation,
    already allowed, removed it (``marker_after_off=True marker_after_set=False
    final_state=on``).

    Two things close it, both under the exclusive egress lock, the same one
    ``egress_permit`` and ``operator.off()`` coordinate on:

    * the posture rule is re-evaluated against the marker as it is RIGHT NOW,
      and refuses before writing anything;
    * the marker is only unlinked if it was observed under this lock. A halt
      published after that read is not "the halt this call is lifting", so it
      survives -- which is also what makes ``off()``'s own marker creation safe
      without blocking on the lock (it must never be delayed by an outstanding
      egress permit).

    The lock is only taken for a state that lifts a halt; ``off`` touches no
    marker and keeps the plain, uncontended path. Failing to take it is a
    refusal for a bound caller (an unknown halt state must not be lifted) and a
    logged warning for an operator, who must not lose the only way to lift one.
    """
    _point(point)
    PointState(state)

    if state not in HALT_LIFTING_STATES:
        _write_point(point, state, decider)
        return

    from cli_agent_orchestrator.decisions import egress

    with egress.exclusive(egress.HALT_CONTROL_TIMEOUT_S) as held:
        if not held and posture is CallerPosture.BOUND:
            raise HaltLiftRefused(HALT_LOCK_BUSY_DENIAL)
        halted = egress.is_halted()
        if halted and posture is CallerPosture.BOUND:
            raise HaltLiftRefused(HALT_LIFT_DENIAL)
        if not held:
            logger.warning("Decisions lock unavailable; setting the point without it")
        _write_point(point, state, decider)
        if halted:
            egress.HALT_FILE.unlink(missing_ok=True)


def lifts_halt(state: Any) -> bool:
    """Whether ``set_point(..., state)`` would clear an operator's active halt.

    The predicate the ops MCP gate asks before letting a bound caller near
    ``decisions_set_point`` (#1062 item 3). It reads the same
    ``HALT_LIFTING_STATES`` that ``set_point`` acts on and the same live marker
    ``egress.is_halted`` reads, so "would this lift the halt?" is answered once
    rather than re-derived by the guard.

    ``state`` is whatever arrived over the wire, so a non-string is simply not a
    lifting state. With no halt in place there is nothing to lift and this is
    False -- the gate's remaining selector check still applies.
    """
    if not isinstance(state, str) or state not in HALT_LIFTING_STATES:
        return False

    from cli_agent_orchestrator.decisions import egress

    return egress.is_halted()


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


def _check_range(
    name: str, value: float | None, bounds: tuple[float, float], *, integer: bool
) -> None:
    lower, upper = bounds
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, int if integer else (int, float))
        or not math.isfinite(value)
        or not lower <= value <= upper
    ):
        raise ValueError(f"{name} must be between {lower:g} and {upper:g}")


def tune(
    *,
    on_timeout_ms: int | None = None,
    threshold: float | None = None,
    retention_days: int | None = None,
) -> None:
    _check_range("on_timeout_ms", on_timeout_ms, ON_TIMEOUT_MS_RANGE, integer=True)
    _check_range("threshold", threshold, THRESHOLD_RANGE, integer=False)
    _check_range("retention_days", retention_days, RETENTION_DAYS_RANGE, integer=True)
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
    pairs = [value.partition("=") for value in values]
    for point, sep, state in pairs:
        if not sep or point not in STATE_ENV or state not in STATES:
            raise ValueError("decision flag must be <point>=" + "|".join(STATES))
    for point, _, state in pairs:
        os.environ[STATE_ENV[point]] = state
