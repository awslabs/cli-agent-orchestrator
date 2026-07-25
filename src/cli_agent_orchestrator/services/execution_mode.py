"""Closed, immutable execution-mode contract for managed Kimi launches.

Two execution modes exist and no third is representable:

``native_tui``
    The real provider TUI is the primary process of the worker's tmux
    pane — visible and directly usable by a human, while remaining
    controllable through CAO's provider-native control adapter.  This is
    the golden path for persistent, long-running, or human-monitored
    managed workers.

``acp``
    The bridge process speaks the provider's Agent Client Protocol on
    private stdio pipes.  There is no native TUI and no human-usable
    pane; this mode is an explicit opt-in for small or intentionally
    hands-off work.

Invariant: the mode is resolved exactly once, **before any provider
I/O**, from ``launch input > task input > profile default``; it is then
persisted immutably against the reservation, generation, native binding,
every delivery and session-operation receipt, public status, and the run
manifest.  The two modes are separate launch branches: CAO never
silently switches between them in either direction, never falls back
from one to the other, and never mutates the mode of an existing
generation.  A mode change is a new generation.

Failure mode prevented: a launch that resolves its mode lazily (or
falls back after a native failure) can attach an ACP bridge and a native
TUI to the same provider session, replay task bytes into a resumed
session, or satisfy a native admission gate with ACP evidence.  Each of
those corrupts a live session in a way no downstream receipt can detect,
because the receipt itself would carry the wrong mode.

Why this guard exists: mode is the discriminator every other native
guard keys on — attachment ownership, control-adapter selection, and
acceptance-receipt admission all branch on it.  If mode can drift, be
inferred, or be defaulted differently by two call sites, every one of
those guards silently checks the wrong thing.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

#: The closed set of execution modes.  Nothing outside this set is a
#: representable mode: an unrecognized value is a hard rejection, never
#: a silent coercion to a default.
NATIVE_TUI = "native_tui"
ACP = "acp"
EXECUTION_MODES = frozenset({NATIVE_TUI, ACP})

#: Resolution sources in **descending** precedence.  ``profile_default``
#: is deliberately last and deliberately named a *default* rather than an
#: *input*: an explicit launch/task input overriding it is ordinary
#: precedence, not a conflict.
SOURCE_LAUNCH = "launch"
SOURCE_TASK = "task"
SOURCE_PROFILE_DEFAULT = "profile_default"
SOURCE_CLASS_DEFAULT = "class_default"
#: Rows written before the execution-mode contract existed.  Every such
#: row is legacy ACP and can never be reinterpreted as native.
SOURCE_LEGACY = "legacy"

_PRECEDENCE = (SOURCE_LAUNCH, SOURCE_TASK, SOURCE_PROFILE_DEFAULT)

#: Worker classes whose default is the native TUI.  The spec makes the
#: native default *class-conditioned*: native TUI is the default for
#: persistent, long-running, and human-monitored workers, while ACP
#: remains the mode for small or intentionally hands-off work.  A caller
#: that names no class and no mode therefore keeps the historical ACP
#: behavior, so deploying this module cannot silently move an existing
#: fleet onto the native branch.
CLASS_PERSISTENT = "persistent"
CLASS_LONG_RUNNING = "long_running"
CLASS_HUMAN_MONITORED = "human_monitored"
CLASS_ONE_SHOT = "one_shot"
CLASS_HANDS_OFF = "hands_off"
CLASS_UNSPECIFIED = "unspecified"

WORKER_CLASSES = frozenset(
    {
        CLASS_PERSISTENT,
        CLASS_LONG_RUNNING,
        CLASS_HUMAN_MONITORED,
        CLASS_ONE_SHOT,
        CLASS_HANDS_OFF,
        CLASS_UNSPECIFIED,
    }
)

NATIVE_DEFAULT_CLASSES = frozenset({CLASS_PERSISTENT, CLASS_LONG_RUNNING, CLASS_HUMAN_MONITORED})


class ExecutionModeError(ValueError):
    """Base class for every execution-mode resolution or binding failure."""

    code = "execution-mode-error"


class ExecutionModeInvalid(ExecutionModeError):
    """A supplied value is not a member of the closed mode enum."""

    code = "execution-mode-invalid"


class ExecutionModeConflict(ExecutionModeError):
    """Two explicit inputs disagree, or a bound mode would change.

    Raised rather than resolved so a disagreement can never be silently
    settled by precedence.  Precedence fills in an *absent* input; it
    never overrules a *present, contradictory* one.
    """

    code = "execution-mode-conflict"


class ExecutionModeResolution:
    """The immutable outcome of one resolution.

    ``mode`` is the resolved member of the closed enum, ``source`` names
    which precedence level supplied it, and ``inputs`` records exactly
    what was offered so a receipt can prove the resolution rather than
    restate its conclusion.
    """

    __slots__ = ("mode", "source", "worker_class", "inputs")

    def __init__(
        self,
        mode: str,
        source: str,
        worker_class: str,
        inputs: Mapping[str, Optional[str]],
    ) -> None:
        self.mode = mode
        self.source = source
        self.worker_class = worker_class
        self.inputs = dict(inputs)

    @property
    def is_native(self) -> bool:
        return self.mode == NATIVE_TUI

    def as_dict(self) -> dict[str, Any]:
        """The projection persisted into reservations and receipts."""
        return {
            "execution_mode": self.mode,
            "execution_mode_source": self.source,
            "execution_mode_worker_class": self.worker_class,
            "execution_mode_inputs": dict(self.inputs),
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ExecutionModeResolution):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ExecutionModeResolution(mode={self.mode!r}, source={self.source!r}, "
            f"worker_class={self.worker_class!r})"
        )


def validate_mode(value: Any, *, field: str = "execution_mode") -> str:
    """Return ``value`` if it is a member of the closed enum, else reject.

    Accepts only exact lowercase members.  Case-folding, aliasing, and
    whitespace trimming are all deliberately absent: a caller that sends
    ``"Native_TUI"`` has a bug, and coercing it would hide the bug in a
    field every downstream guard trusts.
    """
    if not isinstance(value, str) or value not in EXECUTION_MODES:
        raise ExecutionModeInvalid(
            f"{field} must be one of {sorted(EXECUTION_MODES)}; got {value!r}"
        )
    return value


def validate_worker_class(value: Any) -> str:
    if value is None:
        return CLASS_UNSPECIFIED
    if not isinstance(value, str) or value not in WORKER_CLASSES:
        raise ExecutionModeInvalid(
            f"worker_class must be one of {sorted(WORKER_CLASSES)}; got {value!r}"
        )
    return value


def default_mode_for_class(worker_class: str) -> str:
    """The mode used when no input at any precedence level supplies one."""
    return NATIVE_TUI if worker_class in NATIVE_DEFAULT_CLASSES else ACP


def resolve(
    *,
    launch_input: Optional[str] = None,
    task_input: Optional[str] = None,
    profile_default: Optional[str] = None,
    worker_class: Optional[str] = None,
) -> ExecutionModeResolution:
    """Resolve the execution mode before any provider I/O.

    Precedence is ``launch input > task input > profile default``, then
    the class default.  Every supplied value is validated against the
    closed enum first, so an invalid value at *any* level rejects even
    when a higher-precedence level would have won — a caller must never
    learn that its malformed field was quietly ignored.

    Two explicitly supplied inputs that disagree raise
    ``ExecutionModeConflict``.  ``profile_default`` is a default, not an
    input, so a launch or task input overriding it is ordinary
    precedence and never a conflict.
    """
    resolved_class = validate_worker_class(worker_class)

    supplied: dict[str, Optional[str]] = {}
    for name, value in (
        (SOURCE_LAUNCH, launch_input),
        (SOURCE_TASK, task_input),
        (SOURCE_PROFILE_DEFAULT, profile_default),
    ):
        if value is None:
            supplied[name] = None
            continue
        supplied[name] = validate_mode(value, field=f"{name} execution_mode")

    explicit = [
        (name, supplied[name])
        for name in (SOURCE_LAUNCH, SOURCE_TASK)
        if supplied[name] is not None
    ]
    if len(explicit) == 2 and explicit[0][1] != explicit[1][1]:
        raise ExecutionModeConflict(
            "conflicting execution_mode inputs: "
            f"{explicit[0][0]}={explicit[0][1]!r} vs {explicit[1][0]}={explicit[1][1]!r}; "
            "a disagreement is never resolved by precedence"
        )

    for name in _PRECEDENCE:
        value = supplied[name]
        if value is not None:
            return ExecutionModeResolution(value, name, resolved_class, supplied)

    return ExecutionModeResolution(
        default_mode_for_class(resolved_class),
        SOURCE_CLASS_DEFAULT,
        resolved_class,
        supplied,
    )


def mode_of_record(record: Optional[Mapping[str, Any]]) -> str:
    """The execution mode of a persisted row, treating legacy rows as ACP.

    A row written before this contract existed carries no mode.  It is
    **legacy ACP** by construction and can never be reinterpreted as
    native: a native guard that accepted "mode absent" would treat every
    historical ACP generation as an attachable native session.
    """
    if not record:
        return ACP
    value = record.get("execution_mode")
    if value is None:
        return ACP
    return validate_mode(value, field="persisted execution_mode")


def source_of_record(record: Optional[Mapping[str, Any]]) -> str:
    """The resolution source of a persisted row; legacy when absent."""
    if not record or record.get("execution_mode") is None:
        return SOURCE_LEGACY
    source = record.get("execution_mode_source")
    return source if isinstance(source, str) and source else SOURCE_LEGACY


def is_legacy_row(record: Optional[Mapping[str, Any]]) -> bool:
    """True when the row predates the execution-mode contract."""
    return not record or record.get("execution_mode") is None


def assert_immutable(bound: Optional[str], incoming: Optional[str]) -> str:
    """Confirm an incoming mode matches the already-bound one.

    ``None`` incoming means "the caller did not restate the mode" and is
    accepted — silence is not a change.  A different, present value is a
    conflict: the mode of a live generation is immutable, and a mode
    change is a new generation.
    """
    bound_mode = validate_mode(bound, field="bound execution_mode")
    if incoming is None:
        return bound_mode
    incoming_mode = validate_mode(incoming, field="incoming execution_mode")
    if incoming_mode != bound_mode:
        raise ExecutionModeConflict(
            f"execution_mode is immutable: generation is bound to {bound_mode!r} "
            f"and cannot be changed to {incoming_mode!r}; a mode change is a new generation"
        )
    return bound_mode


def assert_same_mode(left: Optional[str], right: Optional[str], *, context: str) -> str:
    """Confirm two identities share an execution mode, else refuse.

    Used at every seam where an operation minted under one mode could
    otherwise reach a session running the other — the concrete
    ACP-vs-native crossing this contract exists to prevent.  Legacy
    (absent) values resolve to ACP rather than matching anything.
    """
    left_mode = ACP if left is None else validate_mode(left, field=f"{context} execution_mode")
    right_mode = ACP if right is None else validate_mode(right, field=f"{context} execution_mode")
    if left_mode != right_mode:
        raise ExecutionModeConflict(
            f"{context}: execution_mode mismatch ({left_mode!r} vs {right_mode!r}); "
            "modes are separate launch branches and never cross"
        )
    return left_mode
