"""Terminal service with workflow functions.

This module provides high-level terminal management operations that orchestrate
multiple components (database, tmux, providers) to create a unified terminal
abstraction for CLI agents.

Key Responsibilities:
- Terminal lifecycle management (create, get, delete)
- Provider initialization and cleanup
- Tmux session/window management
- Terminal output capture and message extraction

Terminal Workflow:
1. create_terminal() → Creates tmux window, initializes provider, starts logging
2. send_input() → Sends user message to the agent via tmux
3. get_output() → Retrieves agent response from terminal history
4. delete_terminal() → Cleans up provider, database record, and logging
"""

import asyncio
import contextlib
import logging
import os
import re
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional

from sqlalchemy.exc import OperationalError

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    REGISTRATION_OK,
    backfill_terminal_identity_if_missing,
    create_inbox_message,
)
from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import create_terminal_v2 as db_create_terminal_v2
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (
    delete_terminal_if_generation as db_delete_terminal_if_generation,
)
from cli_agent_orchestrator.clients.database import (
    delete_terminal_v2_if_generation as db_delete_terminal_v2_if_generation,
)
from cli_agent_orchestrator.clients.database import (
    find_terminal_by_pane_identity,
    get_terminal_metadata,
    get_terminal_metadata_v2,
    record_terminal_lifecycle,
    refresh_terminal_window_names,
    register_terminal_incarnation_outcome,
    register_v2_terminal_incarnation_outcome,
    report_terminal_missing_from_every_store,
    set_terminal_native_session_id,
    update_last_active,
    update_terminal_shell_command,
    upgrade_terminal_identity_from_observation,
    upgrade_v2_terminal_identity_from_observation,
)
from cli_agent_orchestrator.constants import (
    FIFO_DIR,
    PIPE_LIVENESS_TAIL_LINES,
    SESSION_PREFIX,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateTerminalEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.herdr_inbox_registry import get_herdr_inbox_service
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import (
    clear_session_env,
    get_session_env,
    set_session_env,
)
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
    managed_window_name,
    wait_until_status,
)

logger = logging.getLogger(__name__)

# Track terminals that have already received memory injection (first message only).
_memory_injected_terminals: set = set()
_memory_injected_lock = threading.Lock()

# Strong references to in-flight deferred-init background tasks. asyncio keeps
# only a WEAK reference to tasks from loop.create_task, so without this a
# deferred provider.initialize() + input-send task could be GC'd mid-run,
# silently leaving a worker uninitialized. Tasks drop themselves on completion.
_deferred_init_tasks: set = set()


class TerminalInputBlockedError(Exception):
    """Raised when orchestrated input would answer an active interactive prompt."""


class TerminalIdentityMismatchError(TerminalInputBlockedError):
    """The terminal's recorded pane no longer resolves to what was recorded.

    Deliberately a subclass of the input-blocked error rather than a new
    exception family: to every caller this is the same kind of answer —
    "this terminal is not a lawful target right now, and nothing was
    delivered" — and it inherits the refusal mapping the API boundary
    already applies. Read paths raise it too, which reads oddly by name but
    is the honest outcome: a read resolved by a stale name returns another
    worker's screen, so refusing is the same decision as refusing a write.
    """


class TerminalInputRefusedError(TerminalInputBlockedError):
    """A v1 pane write was refused before any payload byte, with a typed reason.

    Raised only on the zero-byte side of the copy-mode-safe write boundary
    (cond-0178): the pane lease was busy, the identity re-proof under the
    lease failed, or the copy-mode guard could not prove the exact pane out
    of copy mode.  ``reason_code`` is the control-input contract's closed
    vocabulary (``copy-mode-active`` for the guard's own refusals), so the
    API boundary keeps its existing ``TerminalInputBlockedError`` → HTTP 409
    mapping and an inbox caller can tell "proven nothing was written,
    reattemptable" apart from a payload write whose ending is ambiguous —
    which keeps the ordinary failure mapping and is never re-typed.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


LIFECYCLE_LIVE = "live"
LIFECYCLE_SUPERSEDED = "superseded"
LIFECYCLE_DEAD = "dead"
LIFECYCLE_UNKNOWN_LIVENESS = "unknown-liveness"

#: The canonical identity of one live incarnation. All five or none —
#: there is no useful subset.
#:
#: Each field closes a way the others can be wrong. ``server_socket_path``
#: is not optional context: a pane id is unique only *within* one tmux
#: server, several run on one host, and an id with no server named is an
#: id on whichever server the reading process happens to reach.
#: ``pane_pid`` closes the remaining gap, which is not hypothetical — a
#: tmux server restart hands out ``%0``/``%1`` again, so the old ids
#: resolve to different live panes on a socket with the same path.
IDENTITY_FIELDS = ("server_socket_path", "session_id", "window_id", "pane_id", "pane_pid")

IDENTITY_ABSENT = "absent"
IDENTITY_PARTIAL = "partial"
IDENTITY_COMPLETE = "complete"


def identity_completeness(metadata: Dict[str, Any]) -> str:
    """Whether a row carries a whole canonical identity, some of one, or none.

    The three answers get three different treatments, and the middle one
    is the point:

    ``complete``  every field present — the row may be verified and
                  addressed by id.
    ``partial``   some fields present — **fail closed**. The missing
                  fields cannot be inferred, and inferring them is the
                  specific mistake: a pane id without its server silently
                  means "this pane on the default server", which after a
                  tmux restart is a different live pane wearing the same
                  id. Refusing costs an operator a re-registration;
                  guessing costs somebody else's session.
    ``absent``    no fields at all — a row predating identity
                  persistence. Unmanaged, and left on the name-resolved
                  path it has always used.
    """
    present = [field for field in IDENTITY_FIELDS if metadata.get(field)]
    if not present:
        return IDENTITY_ABSENT
    if len(present) == len(IDENTITY_FIELDS):
        return IDENTITY_COMPLETE
    return IDENTITY_PARTIAL


class VerifiedPaneTarget(NamedTuple):
    """One pane, proven at a single instant to still be the registered one.

    ``pane_id`` is what a write targets: it is immutable, so nothing can
    slip between the proof and the write. ``session_name``/``window_name``
    are the names that pane answers to *now* — not the names recorded on
    the row, which a rename or a later unrelated window may since have
    taken. Read paths that can only address a name use these, so they read
    the pane that was verified rather than whatever currently occupies the
    recorded name.

    ``window_id``/``pane_pid``/``server_socket_path`` are the rest of the
    proven canonical identity, carried so a writer that re-proves the pane
    under the pane-input lease (the copy-mode-safe write boundary,
    cond-0178) binds to the identity this proof actually compared — which
    may come from a just-upgraded row the caller's own metadata snapshot
    does not yet reflect.  They default to empty only so older positional
    constructions keep compiling; a target produced by the proof always
    carries them.
    """

    pane_id: str
    session_name: str
    window_name: str
    window_id: str = ""
    pane_pid: int = 0
    server_socket_path: str = ""


class IncarnationRegistrationRefused(Exception):
    """A launch could not register its live incarnation, and why.

    Carries the exact typed cause so a caller can tell an absent row from
    an unreadable pane from a handle something else already holds. Raised
    only where registration is a launch precondition; see
    :func:`_register_incarnation`.
    """

    def __init__(self, terminal_id: str, cause: str, identity: Dict[str, Any]) -> None:
        self.terminal_id = terminal_id
        self.cause = cause
        self.identity = {field: identity.get(field) for field in IDENTITY_FIELDS}
        super().__init__(
            f"terminal {terminal_id} could not be registered as a live "
            f"incarnation ({cause}); observed identity {self.identity}"
        )


def _register_incarnation(
    terminal_id: str,
    generation: Optional[str],
    identity: Dict[str, Any],
    *,
    native_session_id: Optional[str] = None,
    protocol_vintage: str = "v1",
) -> bool:
    """Register the live incarnation this create just produced.

    The insert above already wrote the tuple, so on a first launch this
    call finds every field equal and writes nothing. That is the point: it
    makes the registration path the one both a new launch and a later
    re-drive go through, so idempotency by ``(terminal_id, generation)``
    and the refusal to re-point are exercised by ordinary use rather than
    being guarantees nothing ever tests.

    Dispatches on vintage, because a v2 managed terminal lives only in the
    v2 store. Sending it to the legacy writer asked ``terminals`` for a row
    only ``managed_launch_v2_terminals`` holds, which reported an absent
    row for *every* native launch — a false negative from a table the
    launch never wrote.

    The two vintages differ in what a failure means, and so in what this
    does about it:

    v1 logs and continues, unchanged. The row is already correct there, so
    this call can only confirm it, and tearing down a working terminal over
    a redundant write would be the worse error.

    v2 raises. For a native launch the registration is a *precondition*,
    not bookkeeping: the identity registered here is what every later
    identity-addressed operation resolves against, so a launch that
    proceeds without it produces exactly what was observed in production —
    a live pane whose terminal nothing can find, status stuck unknown, and
    a lookup failure repeating for as long as the pane lives.

    Raising does not tear that pane down. A managed v2 launch runs under
    ``preserve_on_init_failure``, so this refusal preserves the generation
    and exposes it as a zero-byte failure -- one that can then be finalized
    negative, because no binding, bind intent or admission was ever
    published. So the choice is not between a live pane and a dead one: it
    is between a preserved generation somebody can close and a live pane
    that can be neither addressed nor cleaned up. This happens before any
    task byte can be delivered, and is the only reason this function is
    allowed to raise at all.
    """
    register = (
        register_v2_terminal_incarnation_outcome
        if protocol_vintage == "v2"
        else register_terminal_incarnation_outcome
    )
    try:
        outcome = register(
            terminal_id,
            generation=generation,
            server_socket_path=identity.get("server_socket_path") or "",
            session_id=identity.get("session_id") or "",
            window_id=identity.get("window_id") or "",
            pane_id=identity.get("pane_id") or "",
            pane_pid=int(identity["pane_pid"]) if identity.get("pane_pid") else 0,
            native_session_id=native_session_id,
        )
    except Exception as exc:
        if protocol_vintage == "v2":
            raise
        logger.warning("Could not register incarnation for %s: %s", terminal_id, exc)
        return False
    if outcome != REGISTRATION_OK:
        if protocol_vintage == "v2":
            raise IncarnationRegistrationRefused(terminal_id, outcome, identity)
        # v1, unchanged: the cause is named rather than reduced to a flag,
        # because these need different fixes and the log is where somebody
        # finds out which one happened.
        logger.info(
            "Terminal %s was not registered as a live incarnation (%s, identity %s)",
            terminal_id,
            outcome,
            {field: identity.get(field) for field in IDENTITY_FIELDS},
        )
    return outcome == REGISTRATION_OK


def record_native_session(terminal_id: str, native_session_id: str) -> bool:
    """Bind a proven provider-native session to this terminal's row.

    Called once the provider has answered — the SessionStart hook for
    Claude, the ACP bootstrap for Kimi — rather than when the launcher
    chose the id, so the row records what was observed rather than what was
    intended. Without it the field stays NULL on every managed row, which
    costs the native-session half of the supersession test: a pane running
    a *different* session than the one registered would otherwise look
    identical to the right one.
    """
    try:
        recorded = set_terminal_native_session_id(terminal_id, native_session_id)
    except Exception as exc:  # pragma: no cover - a missing label is not fatal
        logger.warning("Could not record native session for %s: %s", terminal_id, exc)
        return False
    # M3-A repair seam: bind the observed identity onto the terminal's
    # roster lineage when that lineage is still truthfully
    # ``identity_missing``.  Best-effort and never fatal: a conflicting
    # recorded identity is a real conflict and is logged, and the truthful
    # missing state remains visible to the roster audit.
    try:
        from cli_agent_orchestrator.services import stable_agent_roster

        metadata = get_terminal_metadata(terminal_id)
        harness = (metadata or {}).get("provider", "unknown")
        # Pass the exact generation when the row carries one so the repair
        # resolves the exact incarnation; a terminal-only fallback resolves
        # the unique live incarnation deterministically.
        stable_agent_roster.record_native_identity(
            terminal_id=terminal_id,
            generation=(metadata or {}).get("generation"),
            native_session_id=native_session_id,
            harness=harness,
        )
    except Exception as exc:  # noqa: BLE001 - the terminal fact is already recorded
        logger.warning("stable-agent roster repair failed for terminal %s: %s", terminal_id, exc)
    return recorded


def upgrade_observed_identity(
    terminal_id: str, metadata: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Complete a pre-upgrade row from an observation of its own pane.

    Returns the updated metadata when the row was completed, or ``None``
    when it was not — in which case the caller refuses exactly as before.

    The build deployed before this one recorded ``pane_id``, ``window_id``
    and ``server_socket_path``. Adding ``session_id`` and ``pane_pid`` to
    the canonical tuple makes every one of those rows partial, and partial
    fails closed, so without this step installing over an existing database
    would leave the running fleet unable to take control input, unreadable,
    and unattachable — fail-closed, so nothing is misdelivered, but dead.

    The distinction that makes this lawful is *observed* versus *invented*.
    The migration declines to backfill because it runs against whichever
    tmux server the upgrading process reaches, so what it wrote would be a
    guess that the later identity check then confirms — the check agreeing
    with itself. Here the row already names a pane and a server; the pane
    is looked up on that server, and the two missing fields are read from
    that same record. If the pane is not there, or the enumeration cannot
    be made, nothing is written and the refusal stands.

    A row missing ``pane_id`` or ``server_socket_path`` is never upgraded:
    there is nothing to observe it against, and that is precisely the case
    where a guess would put a live handle on somebody else's pane.

    The write is dispatched by protocol vintage, exactly as the
    native-session writers are. The deployed build created three-of-five
    rows in *both* stores, and the managed store is isolated with its own
    prefixed columns, so a v2 row handed to the shared-table writer
    matches nothing: the upgrade silently reports failure and the whole
    preserved managed fleet stays demoted forever.
    """
    pane_id = metadata.get("pane_id")
    socket_path = metadata.get("server_socket_path")
    if not pane_id or not socket_path:
        return None
    if metadata.get("session_id") and metadata.get("pane_pid"):
        return None

    backend = get_backend()
    if getattr(backend, "supports_pane_identity", False) is not True:
        return None
    try:
        observed = backend.observe_pane_identity(pane_id)
    except Exception as exc:  # pragma: no cover - an unreadable server is a refusal
        logger.debug("Identity upgrade could not observe pane %s: %s", pane_id, exc)
        return None
    if not isinstance(observed, dict) or observed.get("outcome") != "observed":
        return None
    # The observation has to be of the pane on *this row's* server. A pane
    # id read from a different server is a different pane wearing the same
    # id, which is the whole reason the socket is part of the identity.
    if str(observed.get("server_socket_path")) != str(socket_path):
        return None
    session_id = observed.get("session_id")
    pane_pid = observed.get("pane_pid")
    if not session_id or not pane_pid:
        return None

    writer = (
        upgrade_v2_terminal_identity_from_observation
        if metadata.get("protocol_vintage") == "v2"
        else upgrade_terminal_identity_from_observation
    )
    try:
        completed = writer(
            terminal_id,
            pane_id=pane_id,
            server_socket_path=socket_path,
            session_id=str(session_id),
            pane_pid=int(pane_pid),
        )
    except Exception as exc:  # pragma: no cover - a failed upgrade is a refusal
        logger.warning("Could not persist observed identity for %s: %s", terminal_id, exc)
        return None
    if not completed:
        return None

    logger.info(
        "Completed pre-upgrade identity for terminal %s from its own pane %s",
        terminal_id,
        pane_id,
    )
    upgraded = dict(metadata)
    upgraded["session_id"] = str(session_id)
    upgraded["pane_pid"] = int(pane_pid)
    return upgraded


def verified_pane_target(
    terminal_id: str,
    metadata: Dict[str, Any],
    *,
    operation: str,
) -> Optional[VerifiedPaneTarget]:
    """Prove the terminal's recorded pane is still the pane it registered.

    Returns the verified target, or ``None`` when this row is outside the
    identity boundary and the caller should keep resolving by name. Raises
    :class:`TerminalIdentityMismatchError` when the row *is* inside the
    boundary and the proof fails — before anything is written or read.

    A row is inside the boundary only when it records the *whole*
    canonical identity (see :func:`identity_completeness`). A partial
    identity is refused rather than partially trusted, and a row with no
    identity at all keeps resolving by name exactly as before — the
    documented boundary of this change rather than an oversight.

    The check is read-only. It spends no provider turn, delivers no byte,
    and its whole cost is one enumeration of the panes tmux already knows
    about — so a refused operation costs nothing and a demotion is never
    something the system did to a worker, only something it noticed.
    """
    completeness = identity_completeness(metadata)
    if completeness == IDENTITY_ABSENT:
        return None
    if completeness == IDENTITY_PARTIAL:
        # A row created by the previously deployed build carries exactly
        # three of the five fields, so every one of them lands here on the
        # first read after an upgrade. Try to complete it from an
        # observation of its own pane before refusing — see
        # :func:`upgrade_observed_identity` for why an observation may do
        # what the migration must not.
        upgraded = upgrade_observed_identity(terminal_id, metadata)
        if upgraded is not None:
            metadata = upgraded
            completeness = identity_completeness(metadata)
    if completeness == IDENTITY_PARTIAL:
        recorded = {field: metadata.get(field) for field in IDENTITY_FIELDS}
        missing = ", ".join(field for field in IDENTITY_FIELDS if not metadata.get(field))
        _demote(terminal_id, LIFECYCLE_UNKNOWN_LIVENESS, f"identity incomplete: missing {missing}")
        raise TerminalIdentityMismatchError(
            f"Terminal {terminal_id} cannot be used for {operation}: its recorded identity "
            f"is incomplete (missing {missing}; have {recorded}). The missing fields are not "
            "inferred — a pane id resolved against an unnamed server can be a different live "
            "pane. Nothing was delivered."
        )
    recorded_pane = metadata["pane_id"]

    backend = get_backend()
    # ``is True`` rather than a truthiness test: a backend double answers
    # every attribute with a stand-in object, and treating that as a
    # declared capability would start enforcing a boundary against evidence
    # the double never gathered.
    if getattr(backend, "supports_pane_identity", False) is not True:
        return None

    observed = backend.observe_pane_identity(recorded_pane)
    if not isinstance(observed, dict):
        return None

    outcome = observed.get("outcome")
    if outcome == "absent":
        # The server answered and the pane is not on it. That is evidence,
        # and the row can say so.
        _demote(terminal_id, LIFECYCLE_DEAD, f"pane {recorded_pane} is absent from its server")
        raise TerminalIdentityMismatchError(
            f"Terminal {terminal_id} cannot be used for {operation}: its recorded pane "
            f"{recorded_pane} no longer exists. Nothing was delivered."
        )
    if outcome != "observed":
        # Nothing was learned. The operation is refused all the same — an
        # unverifiable target is not a lawful one — but the row is recorded
        # as unknown rather than reaped, because "we could not look" is not
        # evidence that the worker is gone.
        _demote(
            terminal_id,
            LIFECYCLE_UNKNOWN_LIVENESS,
            f"pane {recorded_pane} could not be observed",
        )
        raise TerminalIdentityMismatchError(
            f"Terminal {terminal_id} cannot be used for {operation}: its recorded pane "
            f"{recorded_pane} could not be observed. Nothing was delivered."
        )

    if observed.get("dead") == "1":
        _demote(terminal_id, LIFECYCLE_DEAD, f"pane {recorded_pane} is dead")
        raise TerminalIdentityMismatchError(
            f"Terminal {terminal_id} cannot be used for {operation}: its pane "
            f"{recorded_pane} is dead. Nothing was delivered."
        )

    # Every field is compared, because the identity was required to be
    # complete to get here. A conditional comparison would quietly skip
    # whichever field happened to be missing, which is the partial-trust
    # this boundary refuses.
    mismatches = []
    for field, label in (
        ("server_socket_path", "tmux server"),
        ("window_id", "window"),
        ("session_id", "session"),
        ("pane_pid", "pane process"),
    ):
        recorded = metadata[field]
        seen = observed.get(field)
        if str(seen) != str(recorded):
            mismatches.append(f"{label} {seen!r} != {recorded!r}")

    if mismatches:
        detail = "; ".join(mismatches)
        # Name the successor if a row claims the pane as it is *now*. A
        # superseded row that can only say it lost its pane leaves an
        # operator and the conductor with nothing to act on; the point of
        # the pointer is that the next action ("read the superseding
        # terminal's identity, then replace") is answerable from the row.
        successor = _successor_of(terminal_id, observed)
        _demote(terminal_id, LIFECYCLE_SUPERSEDED, detail, successor=successor)
        named = (
            f" It now belongs to terminal {successor['terminal_id']} "
            f"(generation {successor['generation']})."
            if successor
            else ""
        )
        raise TerminalIdentityMismatchError(
            f"Terminal {terminal_id} cannot be used for {operation}: the pane at "
            f"{recorded_pane} is a different incarnation ({detail}).{named} "
            "Nothing was delivered."
        )

    # Names are read from the observation, never from the row. A worker
    # that renamed its own window is still the right worker — a name is a
    # label, and demoting on one would reap live terminals for relabelling
    # themselves.
    session_name = observed.get("session_name") or metadata["tmux_session"]
    window_name = observed.get("window_name") or metadata["tmux_window"]
    _note_live(terminal_id)
    if session_name != metadata["tmux_session"] or window_name != metadata["tmux_window"]:
        # Refresh the row's cached labels to the ones the *proven* pane now
        # answers to. This is not re-pointing: the identity — server, pane,
        # window, session, pid — was just verified unchanged, and only the
        # mutable labels moved. Leaving them stale would keep every
        # name-addressed reader (status detection, history capture, the
        # dashboard) pointed at a name this pane no longer has.
        try:
            refresh_terminal_window_names(
                terminal_id,
                tmux_session=session_name,
                tmux_window=window_name,
                pane_id=recorded_pane,
            )
        except Exception as exc:  # pragma: no cover - a stale label is not fatal
            logger.warning("Could not refresh window labels for %s: %s", terminal_id, exc)
    return VerifiedPaneTarget(
        recorded_pane,
        session_name,
        window_name,
        # The identity fields as just proven — read from the (possibly
        # upgrade-completed) metadata this function verified, so a caller
        # holding a pre-upgrade snapshot still binds to the proven values.
        window_id=str(metadata["window_id"]),
        pane_pid=int(metadata["pane_pid"]),
        server_socket_path=str(metadata["server_socket_path"]),
    )


def _successor_of(terminal_id: str, observed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Which terminal, if any, is registered to the pane as it now stands.

    Answered from the observation rather than by searching for something
    that looks similar: the full observed tuple has to match a row, so a
    pane id reissued by a restarted server cannot name an unrelated
    terminal as the successor. No match is a normal answer — the pane may
    belong to nothing this system registered — and it means the demotion
    records the loss without inventing a destination for it.
    """
    socket_path = observed.get("server_socket_path")
    pane_id = observed.get("pane_id")
    if not socket_path or not pane_id:
        return None
    try:
        return find_terminal_by_pane_identity(
            server_socket_path=str(socket_path),
            pane_id=str(pane_id),
            session_id=str(observed["session_id"]) if observed.get("session_id") else None,
            pane_pid=int(observed["pane_pid"]) if observed.get("pane_pid") else None,
            exclude_terminal_id=terminal_id,
        )
    except Exception as exc:  # pragma: no cover - an unanswerable lookup is not fatal
        logger.debug("Could not resolve a successor for %s: %s", terminal_id, exc)
        return None


def _demote(
    terminal_id: str,
    state: str,
    reason: str,
    *,
    successor: Optional[Dict[str, Any]] = None,
) -> None:
    """Record an observed demotion, never letting the bookkeeping decide the
    outcome: if the row cannot be updated the caller still refuses."""
    try:
        record_terminal_lifecycle(
            terminal_id,
            state=state,
            reason=reason,
            superseded_by_terminal_id=(successor or {}).get("terminal_id"),
            superseded_by_generation=(successor or {}).get("generation"),
        )
    except Exception as exc:  # pragma: no cover - bookkeeping must not mask the refusal
        logger.warning("Could not record %s lifecycle for %s: %s", state, terminal_id, exc)


def _note_live(terminal_id: str) -> None:
    try:
        record_terminal_lifecycle(terminal_id, state=LIFECYCLE_LIVE)
    except Exception as exc:  # pragma: no cover - see _demote
        logger.debug("Could not record live lifecycle for %s: %s", terminal_id, exc)


class TerminalGenerationMismatchError(ValueError):
    """The terminal id now names a different incarnation than the caller's
    bound generation/session. Destructive cleanup must preserve every resource
    and report the ambiguity — never delete the replacement. Mapped to HTTP
    409 at the API boundary (distinct from a plain 404 not-found)."""


class DestructiveEndpointRequiredError(TerminalGenerationMismatchError):
    """A v2 terminal row was targeted by a legacy destructive path.

    A caller may retire one exact generation by supplying its generation
    and session, or use the stronger conditional destructive endpoint when
    heartbeat, fencing, dual-exit, and containment proofs are required.  A
    bare legacy caller carries neither identity nor endpoint-issued intent,
    so teardown refuses with zero mutation. Mapped to HTTP 409 like every
    other conditional-identity refusal."""


_SHELL_COMMANDS = frozenset({"sh", "bash", "zsh", "dash", "fish", "csh", "tcsh", "ksh", "login"})


def _get_terminal_metadata_any(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Resolve legacy or v2 metadata; an uninstalled v2 surface is absent.

    The v1 probe is silent: for a healthy v2-only terminal its miss is the
    expected first-tier outcome, and warning there reported a live terminal as
    missing on every hot call (COND-0242). The miss is worth saying only when
    the v2 tier comes up empty too, which is what actually means "gone".
    """
    metadata = get_terminal_metadata(terminal_id, warn_if_missing=False)
    if metadata is not None:
        return metadata
    try:
        v2_metadata = get_terminal_metadata_v2(terminal_id)
    except OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        v2_metadata = None
    if v2_metadata is None:
        report_terminal_missing_from_every_store(terminal_id)
    return v2_metadata


def _verify_managed_pane_process(session_name: str, window_name: str) -> None:
    """Prove the managed window is NOT running a shell (fail-closed check).

    The zero-keystroke contract: the pane's process must be the bridge argv
    itself. If the pane reports a shell — or its process cannot be resolved —
    the launch is unsafe: kill the window and raise so the reservation records
    a preflight block instead of degrading to shell typing."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            command = get_backend().get_pane_current_command(session_name, window_name)
        except Exception:
            command = None
        if command:
            break
        if time.monotonic() >= deadline:
            command = None
            break
        time.sleep(0.05)
    if command and command.strip().lower() not in _SHELL_COMMANDS:
        return
    try:
        get_backend().kill_window(session_name, window_name)
    finally:
        raise RuntimeError(
            f"managed window {session_name}:{window_name} did not start the "
            f"bridge as its own process (pane command: {command!r}); refusing "
            "to fall back to shell-typed startup"
        )


def inject_memory_context(first_message: str, terminal_id: str) -> str:
    """Prepend <cao-memory> context block to the first user message.

    Tracks which terminals have already been injected so that only the very
    first user message after init receives the memory block.

    Calls MemoryService.get_memory_context_for_terminal() which returns
    a formatted <cao-memory>...</cao-memory> block (or empty string if
    no memories exist). Stateless — no file mutation, no backup/restore.
    """
    with _memory_injected_lock:
        if terminal_id in _memory_injected_terminals:
            return first_message
        _memory_injected_terminals.add(terminal_id)

    try:
        svc = MemoryService()
        context = svc.get_curated_memory_context(terminal_id, task_description=first_message[:200])
        if context:
            return context + "\n\n" + first_message
    except Exception as e:
        logger.warning(f"Failed to inject memory context for terminal {terminal_id}: {e}")
    return first_message


class OutputMode(str, Enum):
    """Output mode for terminal history retrieval.

    FULL: Returns complete terminal output (scrollback buffer)
    LAST: Returns only the last agent response (extracted by provider)
    """

    FULL = "full"
    LAST = "last"


# Providers that accept a runtime skill_prompt kwarg and append it to the
# system prompt at launch time.  Other providers deliver skills differently:
# Kiro (skill:// resources) and OpenCode (OPENCODE_CONFIG_DIR/skills symlink)
# discover skills natively; Copilot receives a baked catalog at install
# time.
RUNTIME_SKILL_PROMPT_PROVIDERS = {
    ProviderType.CLAUDE_CODE.value,
    ProviderType.CODEX.value,
    ProviderType.KIMI_CLI.value,
    ProviderType.ANTIGRAVITY_CLI.value,
}

# Providers whose tool restrictions are prompt-level text only (no native
# blocking mechanism) — a restricted policy on these is advisory, not enforced.
SOFT_ENFORCEMENT_PROVIDERS = {
    ProviderType.KIMI_CLI.value,
    ProviderType.CODEX.value,
    ProviderType.ANTIGRAVITY_CLI.value,
}


def _register_v2_terminal_resources(
    terminal_id: str, generation: str, window_name: str, session_name: str
) -> None:
    """Journal-first DECLARATION of a v2 generation's runtime resources.

    The code-owned resource registry is the authority for the v2/managed
    lifecycle: every resource the runtime constructs for a v2 generation
    is declared here — durably, BEFORE any physical window or DB row is
    constructed — so a crash can never orphan an undeclared resource.
    This function only declares intent (and attaches the shared
    companion-dir monitor); it NEVER marks anything created.  Each entry
    is transitioned to ``created`` by ``_mark_v2_resource_created`` only
    after the exact physical/DB/memory identity is observed to exist.
    Legacy v1 surfaces predate the registry and stay outside its
    authority boundary.  Owned identities embed their entry_id so the
    create-to-capture crash window stays discoverable (registry rule);
    the per-generation companion dir is shared among the heartbeat/fence/
    broker/destructive writers and is registered monitor-only.
    """
    from cli_agent_orchestrator.constants import COMPANION_DIR
    from cli_agent_orchestrator.services import resource_registry as rr

    registry = rr.get_resource_registry()
    constructor = "terminal_service.create_terminal"
    resources: list[tuple[str, str, str, dict[str, Any]]] = [
        (
            "fifo",
            f"{terminal_id}.fifo",
            "owned",
            {"desired_fs_path": str(FIFO_DIR / f"{terminal_id}.fifo")},
        ),
        (
            "log",
            f"{terminal_id}.log",
            "owned",
            {"desired_fs_path": str(TERMINAL_LOG_DIR / f"{terminal_id}.log")},
        ),
        (
            "scrollback",
            f"{terminal_id}.scrollback",
            "owned",
            {"desired_fs_path": str(TERMINAL_LOG_DIR / f"{terminal_id}.scrollback")},
        ),
        (
            "snapshot",
            f"{terminal_id}.snapshot.json",
            "owned",
            {"desired_fs_path": str(TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json")},
        ),
        ("tmux_window", window_name, "owned", {"desired_tmux_name": window_name}),
        (
            "provider_instance",
            f"{terminal_id}.provider",
            "owned",
            {"desired_db_key": f"provider:{terminal_id}.provider"},
        ),
        (
            "session_env",
            f"{terminal_id}.session-env",
            "owned",
            {"desired_db_key": f"session_env:{session_name}:{terminal_id}.session-env"},
        ),
        (
            "herdr",
            f"{terminal_id}.herdr",
            "owned",
            {"desired_db_key": f"herdr:{terminal_id}.herdr"},
        ),
        (
            "pipe_pane",
            f"{terminal_id}.pipe-pane",
            "owned",
            {"desired_db_key": f"pipe_pane:{terminal_id}.pipe-pane"},
        ),
        (
            "watchdog",
            f"{terminal_id}.watchdog",
            "owned",
            {"desired_db_key": f"watchdog:{terminal_id}.watchdog"},
        ),
        (
            "status_map",
            f"{terminal_id}.status-map",
            "owned",
            {"desired_memory_key": f"status:{terminal_id}.status-map"},
        ),
        (
            "memory_injection",
            f"{terminal_id}.memory-injection",
            "owned",
            {"desired_memory_key": f"memory-injection:{terminal_id}.memory-injection"},
        ),
        (
            "curator_lock",
            f"{terminal_id}.curator-lock",
            "owned",
            {"desired_memory_key": f"curator-lock:{terminal_id}.curator-lock"},
        ),
        (
            "db_row_set",
            f"{terminal_id}.db-row",
            "owned",
            {"desired_db_key": f"managed_launch_v2_terminals:{terminal_id}.db-row"},
        ),
        (
            # The per-generation companion dir (heartbeat/fence/broker/
            # destructive state and their lock sidecars) is written by
            # several subsystems; registered monitor-only as shared.
            "other",
            f"{terminal_id}.companion",
            "shared",
            {"desired_fs_path": str(COMPANION_DIR / terminal_id / generation)},
        ),
    ]
    for kind, entry_id, ownership, identity in resources:
        registry.declare(
            entry_id=entry_id,
            kind=kind,
            protocol_vintage="v2",
            terminal_id=terminal_id,
            generation=generation,
            owner="fork",
            ownership=ownership,
            constructor_id=constructor,
            deleter_id="terminal_service.delete_terminal",
            monitor_id=("heartbeat_store.issue_fencing_token" if kind == "other" else None),
            rollback_rule="generation-isolated",
            actor_id=constructor,
            **identity,
        )
        if ownership != "owned":
            registry.monitor(
                entry_id,
                monitor_id="heartbeat_store.issue_fencing_token",
                actor_id=constructor,
            )


def _mark_v2_resource_created(
    entry_id: str,
    *,
    actor_id: str,
    observed: Optional[dict[str, Any]] = None,
    receipt_subject: Optional[dict[str, Any]] = None,
) -> None:
    """Transition a declared v2 entry to ``created`` after observed creation.

    Callers invoke this ONLY at the point the exact physical/DB/memory
    identity was observed to exist (the window creation returned, the DB
    insert committed, the FIFO/path is present); the observed identity and
    the existence receipt digest are journaled with the transition.  An
    undeclared or already-created entry is left untouched — creation is
    never manufactured and re-drives converge.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    registry = rr.get_resource_registry()
    try:
        entry = registry.resolve(entry_id)
    except rr.RegistryError:
        return
    if entry["lifecycle_state"] != "declared":
        return
    registry.register_created(
        entry_id,
        actor_id=actor_id,
        observed=observed,
        existence_receipt_digest=rr.receipt_digest(
            {"entry_id": entry_id, "observed": receipt_subject or observed or {}}
        ),
    )


def _retire_reused_tmux_observation(
    entry_id: str,
    identity: dict[str, Any],
) -> None:
    """Free a tmux id whose previous registered window is provably absent.

    Tmux assigns ``@N`` ids only within one server lifetime.  After the
    server restarts it can reuse an id while the durable resource registry
    still remembers the old generation.  The uniqueness index must remain
    authoritative, so reuse is accepted only after one readable pane
    inventory proves both sides of the handoff: the newly created window
    owns the observed id and every previous registered owner name is absent.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    observed_id = identity.get("window_id")
    pane_id = identity.get("pane_id")
    if not observed_id or not pane_id:
        return

    registry = rr.get_resource_registry()
    conflicts = [
        resource
        for resource in registry.enumerate(
            lifecycle_states=("created", "active", "draining", "closed")
        )
        if resource["kind"] == "tmux_window"
        and resource.get("observed_tmux_id") == observed_id
        and resource["entry_id"] != entry_id
    ]
    if not conflicts:
        return

    panes = get_backend().observe_pane_identities()
    if panes is None:
        raise rr.RegistryConflict(
            f"tmux id {observed_id} is already registered and the live server "
            "inventory is unreadable"
        )
    current = panes.get(str(pane_id))
    expected_fields = {
        "pane_id": str(pane_id),
        "window_id": str(observed_id),
        "window_name": entry_id,
    }
    for field in ("session_id", "server_socket_path", "pane_pid"):
        if identity.get(field) is not None:
            expected_fields[field] = str(identity[field])
    if current is None or any(
        str(current.get(field)) != value for field, value in expected_fields.items()
    ):
        raise rr.RegistryConflict(
            f"tmux id {observed_id} is already registered and does not resolve "
            "to the newly created exact window identity"
        )

    live_names = {
        str(record.get("window_name"))
        for record in panes.values()
        if record.get("window_name") is not None
    }
    stale_names = {str(resource["desired_tmux_name"]) for resource in conflicts}
    still_live = sorted(stale_names & live_names)
    if still_live:
        raise rr.RegistryConflict(
            "tmux id reuse cannot retire registered windows still present in "
            f"the readable server inventory: {', '.join(still_live)}"
        )

    absence = rr.receipt_digest(
        {
            "domain": "tmux-observed-id-reuse-v1",
            "observed_tmux_id": observed_id,
            "new_entry_id": entry_id,
            "new_identity": expected_fields,
            "absent_entry_ids": sorted(resource["entry_id"] for resource in conflicts),
            "inventory_pane_ids": sorted(panes),
        }
    )
    actor = "terminal_service.create_terminal.reconcile_tmux_id_reuse"
    for resource in conflicts:
        state = resource["lifecycle_state"]
        if state in ("created", "active"):
            registry.drain(resource["entry_id"], actor_id=actor)
            state = "draining"
        if state == "draining":
            registry.close(resource["entry_id"], actor_id=actor)
        registry.delete(
            resource["entry_id"],
            actor_id=actor,
            verified_absence_digest=absence,
        )


def _mark_existing_v2_fs_artifacts(terminal_id: str) -> None:
    """Mark fs-backed v2 entries created iff the artifact is really present."""
    actor = "terminal_service.create_terminal"
    for entry_id, path in (
        (f"{terminal_id}.log", TERMINAL_LOG_DIR / f"{terminal_id}.log"),
        (f"{terminal_id}.scrollback", TERMINAL_LOG_DIR / f"{terminal_id}.scrollback"),
        (f"{terminal_id}.snapshot.json", TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json"),
    ):
        if path.exists():
            _mark_v2_resource_created(
                entry_id,
                actor_id=actor,
                observed={"observed_fs_path": str(path)},
                receipt_subject={"fs_exists": str(path)},
            )


def _v2_resource_presence(
    entry: dict[str, Any], terminal_id: str, session_name: Optional[str]
) -> Optional[bool]:
    """Identity-specific existence probe: True/False, None when unprovable.

    Every teardown verdict derives from a REAL check of the underlying
    identity — a filesystem stat, a DB read, a backend window query, or an
    in-process membership probe — never from a synthesized claim.
    """
    kind = entry["kind"]
    fs_path = entry.get("desired_fs_path")
    if kind == "socket" and entry.get("binding_identity") is not None:
        from cli_agent_orchestrator.services.managed_provider_bridge import (
            rendezvous_resource_presence,
        )

        return rendezvous_resource_presence(entry)
    if fs_path:
        return Path(fs_path).exists()
    if kind in ("tmux_window", "provider_instance", "pipe_pane"):
        # The provider process and its pipe die with the window.  Only the
        # tmux_window entry carries the window identity; for the others the
        # managed v2 window name is deterministic from the generation.
        session = session_name
        window = entry.get("desired_tmux_name")
        metadata = None
        if window is None and entry.get("generation"):
            window = managed_window_name(terminal_id, entry["generation"])
        if session is None:
            metadata = get_terminal_metadata_v2(terminal_id) or {}
            session = metadata.get("tmux_session")
        if not session or not window:
            return None
        try:
            return bool(get_backend().window_exists(session, window))
        except Exception:  # noqa: BLE001 - an unanswerable probe is unknown
            return None
    if kind == "db_row_set":
        return get_terminal_metadata_v2(terminal_id) is not None
    if kind == "session_env":
        # The underlying row is session-scoped: present while the SESSION's
        # forwarded-env row exists (it is never this terminal's to remove).
        key = entry.get("desired_db_key") or ""
        parts = key.split(":", 2)
        if len(parts) < 3:
            return None
        try:
            return bool(get_session_env(parts[1]))
        except Exception:  # noqa: BLE001
            return None
    if kind == "watchdog":
        return (
            terminal_id in fifo_manager._readers
            or terminal_id in fifo_manager._threads
            or terminal_id in fifo_manager._pane_probe
        )
    if kind == "status_map":
        return (
            terminal_id in status_monitor._buffers
            or terminal_id in status_monitor._last_status
            or terminal_id in status_monitor._screens
        )
    if kind == "memory_injection":
        return terminal_id in _memory_injected_terminals
    if kind == "curator_lock":
        from cli_agent_orchestrator.services.memory_service import _curator_locks

        return terminal_id in _curator_locks
    if kind == "herdr":
        svc = get_herdr_inbox_service()
        if svc is None:
            return False
        try:
            return terminal_id in svc._terminal_to_pane
        except Exception:  # noqa: BLE001
            return None
    return None


def _remove_v2_resource(entry: dict[str, Any], terminal_id: str) -> None:
    """Physically remove the generation-owned artifacts this teardown owns.

    FS artifacts (FIFO/log/scrollback/snapshot) are unlinked; in-process
    maps (status/watchdog/memory-injection/curator lock) are cleared.
    The tmux window, provider process, v2 DB row, and session-scoped env
    row are removed by the caller's ordered teardown (or shared with the
    session) — here they are only ever probed, never removed.
    """
    fs_path = entry.get("desired_fs_path")
    if entry["kind"] == "socket" and entry.get("binding_identity") is not None:
        from cli_agent_orchestrator.services.managed_provider_bridge import (
            cleanup_stale_rendezvous,
        )

        cleanup_stale_rendezvous(
            entry,
            terminal_id=terminal_id,
            generation=entry["generation"],
        )
        return
    if fs_path:
        with contextlib.suppress(OSError):
            Path(fs_path).unlink()
        return
    kind = entry["kind"]
    if kind == "watchdog":
        with contextlib.suppress(Exception):
            fifo_manager.stop_reader(terminal_id)
    elif kind == "status_map":
        with contextlib.suppress(Exception):
            status_monitor.clear_terminal(terminal_id)
    elif kind == "memory_injection":
        with _memory_injected_lock:
            _memory_injected_terminals.discard(terminal_id)
    elif kind == "curator_lock":
        with contextlib.suppress(Exception):
            from cli_agent_orchestrator.services.memory_service import _curator_locks

            _curator_locks.pop(terminal_id, None)


def _absence_receipt(entry: dict[str, Any]) -> str:
    """Digest of the actual absence probe result for one entry."""
    from cli_agent_orchestrator.services import resource_registry as rr

    return rr.receipt_digest(
        {
            "entry_id": entry["entry_id"],
            "absent": True,
            "probe": {
                "kind": entry["kind"],
                "desired_fs_path": entry.get("desired_fs_path"),
                "desired_db_key": entry.get("desired_db_key"),
                "desired_tmux_name": entry.get("desired_tmux_name"),
                "desired_memory_key": entry.get("desired_memory_key"),
            },
        }
    )


def _deregister_v2_terminal_resources(
    terminal_id: str, generation: str, session_name: Optional[str] = None
) -> None:
    """Drain/close/delete a v2 generation's registry entries, truthfully.

    An entry still only ``declared`` is aborted ONLY on a verified-absence
    probe (a probe that finds a trace first records the observed creation
    — the create-to-capture crash window — and never aborts).  An entry
    marked created is drained and closed, its generation-owned physical
    artifact is actually removed, and it is marked ``deleted`` ONLY after
    an identity-specific absence probe confirms the removal — a resource
    that is still present keeps its row (retained, never a synthesized
    absence claim).  Shared monitor-only entries are abandoned on a
    verified-empty probe; a present shared identity is retained.
    """
    from cli_agent_orchestrator.services import resource_registry as rr

    deleter = "terminal_service.delete_terminal"
    try:
        registry = rr.get_resource_registry()
    except Exception:  # noqa: BLE001 - teardown stays best-effort
        logger.warning("resource registry unavailable during v2 teardown", exc_info=True)
        return
    try:
        entries = registry.enumerate(terminal_id=terminal_id, generation=generation)
    except Exception:  # noqa: BLE001
        logger.warning("resource registry enumeration failed during v2 teardown", exc_info=True)
        return
    for entry in entries:
        entry_id = entry["entry_id"]
        state = entry["lifecycle_state"]
        if state in ("deleted", "aborted"):
            continue
        presence = _v2_resource_presence(entry, terminal_id, session_name)
        try:
            if entry["ownership"] != "owned":
                # Monitor-only: abandon the declaration on a verified-empty
                # probe; a present shared identity retains its monitor row.
                if presence is False:
                    registry.abort(
                        entry_id,
                        actor_id=deleter,
                        verified_absence_digest=_absence_receipt(entry),
                    )
                continue
            if state == "declared":
                if presence is True:
                    # Created but never receipt-marked (e.g. the teardown
                    # scrollback/snapshot capture): discover first, never abort.
                    _mark_v2_resource_created(
                        entry_id,
                        actor_id=deleter,
                        observed=_observed_identity(entry),
                        receipt_subject={"discovered_at_teardown": True},
                    )
                    state = "created"
                elif presence is False:
                    registry.abort(
                        entry_id,
                        actor_id=deleter,
                        verified_absence_digest=_absence_receipt(entry),
                    )
                    continue
                else:
                    logger.warning(
                        "v2 registry entry %s: absence unprovable; row retained", entry_id
                    )
                    continue
            if state in ("created", "active"):
                registry.drain(entry_id, actor_id=deleter)
                state = "draining"
            if state == "draining":
                registry.close(entry_id, actor_id=deleter)
                entry = registry.resolve(entry_id)
            _remove_v2_resource(entry, terminal_id)
            if _v2_resource_presence(entry, terminal_id, session_name) is False:
                registry.delete(
                    entry_id,
                    actor_id=deleter,
                    verified_absence_digest=_absence_receipt(entry),
                )
            else:
                logger.warning(
                    "v2 resource %s still present after teardown; registry row retained",
                    entry_id,
                )
        except Exception:  # noqa: BLE001 - one entry's failure must not strand the rest
            logger.warning("registry deregistration failed for %s", entry_id, exc_info=True)


def _observed_identity(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The observed-identity capture for a discovered (declared→created) entry."""
    if entry.get("desired_fs_path"):
        return {"observed_fs_path": entry["desired_fs_path"]}
    if entry.get("desired_db_key"):
        return {"observed_db_key": entry["desired_db_key"]}
    if entry.get("desired_memory_key"):
        return {"observed_memory_key": entry["desired_memory_key"]}
    return None


def _admit_session_creation(session_name: str) -> str:
    """One lifecycle admission policy for both creation modes.

    Applied under the physical session claim, before any resource declaration
    or backend effect: canonicalize the target, fail closed on an unreadable
    lifecycle store, and reject a stopped name. Returns the canonical name.

    ``describe`` fails open to ``working`` + ``unreadable`` for observational
    marshal callers; creation admission must not, since an unreadable store can
    hide a stopped row and admit a stale-env deletion or name reuse.
    """
    from cli_agent_orchestrator.services import session_lifecycle

    canonical = session_lifecycle.normalise_session_name(session_name)
    declared = session_lifecycle.describe(canonical)
    if declared.get("unreadable"):
        raise session_lifecycle.SessionLifecycleUnavailable(
            f"cannot admit session {canonical!r}: its declared lifecycle is "
            f"unreadable ({declared['unreadable']})"
        )
    if declared["lifecycle"] == session_lifecycle.STOPPED:
        raise ValueError(
            f"session {canonical!r} is stopped and still holds what a resume "
            f"would restore ({declared['restore_to']!r}); delete the session "
            "to release the name, or pick another"
        )
    return canonical


def _roster_retire_incarnation_best_effort(terminal_id: str, generation: Optional[str]) -> None:
    """M3-A teardown retirement: best-effort and never raised.

    The physical disposable is already being torn down; a roster failure
    here (missing record, unreadable store) must not block cleanup — Stop
    is best-effort for every roster.  The stable agent and its history
    survive regardless; the audit reports anything left un-retired.
    """
    try:
        from cli_agent_orchestrator.services import stable_agent_roster

        stable_agent_roster.retire_incarnation(
            terminal_id=terminal_id,
            generation=generation,
            reason="terminal teardown",
        )
    except Exception as e:  # noqa: BLE001 - teardown must never be blocked
        logger.warning(f"Failed to retire roster incarnation for {terminal_id}: {e}")


def _roster_bind_unmanaged(
    *,
    terminal_id: str,
    session_name: str,
    stable_agent_role: Optional[str],
    agent_profile: Optional[str],
    provider: str,
    terminal_generation: Optional[str],
    pane_id: Optional[str],
    pane_pid: Optional[int],
    native_status_source: bool,
) -> None:
    """M3-A / cond-0377: durably bind the stable CAO agent for an
    UNMANAGED terminal (the session supervisor and legacy workers).  The
    stable-agent record exists before the launch returns and before any
    message the caller sends at creation; managed reservations bind at
    their own canonical choke point (``bind_native``), so this seam
    excludes them.  Role is launch truth: the caller passes the role its
    owning operation decided; ``None`` means worker.

    DARK-FOUNDATION TRUTH, NOT FULL ADMISSION ENFORCEMENT: the native
    identity is not yet known on this path, so the lineage starts in the
    truthful ``identity_missing`` state and the repair seam
    (``record_native_identity``) upgrades it when the provider answers.
    The roster records identity here, but the legacy unmanaged input
    lanes are NOT yet gated on native-ID-ready admission — deterministic
    native capture and status repair are the follow-up cond-0377
    sub-slices.  The managed-v2 seam's ``assert_admission_ready`` gate IS
    enforced; this unmanaged path is a dark foundation, not the full gate.

    Fail-closed by contract: a newly created terminal whose stable-agent
    row cannot be durably bound is NOT a successfully rostered launch —
    this raises (the caller unwinds with zero task input) rather than
    reporting success with a swallowed bind failure.  A terminal row that
    did not durably persist (unit fakes) has nothing to bind and is
    skipped.
    """
    from cli_agent_orchestrator.services import stable_agent_roster

    if get_terminal_metadata(terminal_id) is None:
        return
    stable_agent_roster.bind_generation(
        stable_agent_roster.BindingContract(
            agent_id=stable_agent_roster.derive_initial_agent_id(terminal_id),
            session_name=session_name,
            role=stable_agent_role or stable_agent_roster.ROLE_WORKER,
            profile_family=agent_profile or "default",
            harness=provider,
            native_session_id=None,
            terminal_id=terminal_id,
            generation=terminal_generation,
            pane_id=pane_id,
            pane_pid=pane_pid,
            execution_mode="native_tui" if native_status_source else None,
        )
    )


async def create_terminal(
    provider: str,
    agent_profile: str,
    session_name: Optional[str] = None,
    new_session: bool = False,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    registry: PluginRegistry | None = None,
    env_vars: Optional[dict[str, str]] = None,
    caller_id: Optional[str] = None,
    defer_init: bool = False,
    initial_message: Optional[str] = None,
    initial_message_orchestration_type: Optional[OrchestrationType] = None,
    reserved_terminal_id: Optional[str] = None,
    terminal_generation: Optional[str] = None,
    trusted_project_root: Optional[str] = None,
    expected_model: Optional[str] = None,
    expected_effort: Optional[str] = None,
    preserve_on_init_failure: bool = False,
    managed_native_command: Optional[list[str]] = None,
    protocol_vintage: str = "v1",
    # Whether this terminal's status comes from the native provider
    # observer instead of the FIFO status monitor. Stated by the caller
    # rather than inferred: ``managed_native_command`` cannot decide it,
    # because the v2 ACP bridge is also launched by argv and *does* need
    # the FIFO -- it is a line-oriented subprocess. Only the launch verb
    # knows whether the pane it is about to create is a full-screen TUI.
    native_status_source: bool = False,
    #: The stable-agent role of THIS terminal (M3-A / cond-0377).  Role is
    #: launch truth, never a profile-name heuristic: session creation
    #: passes ``supervisor`` for the session's initial terminal, and every
    #: other terminal is a ``worker`` unless its owning operation
    #: explicitly says otherwise.  ``None`` means worker.
    stable_agent_role: Optional[str] = None,
) -> Terminal:
    """Create a new terminal with an initialized CLI agent.

    This function orchestrates the complete terminal creation workflow:
    0. (new sessions only) Preflight: strictly pre-clear any stale persisted
       session env BEFORE any resource exists — a failure aborts with zero
       cleanup actions
    1. Generate unique terminal ID and window name
    2. Create tmux session/window (new or existing)
    3. Save terminal metadata to database
    4. Initialize the CLI provider (starts the agent)
    5. Set up terminal logging via tmux pipe-pane

    Args:
        provider: Provider type string (e.g., "kiro_cli", "claude_code")
        agent_profile: Name of the agent profile to use
        session_name: Optional custom session name. If not provided, auto-generated.
        new_session: If True, creates a new tmux session. If False, adds to existing.
        working_directory: Optional working directory for the terminal shell
        env_vars: Operator-forwarded env vars (``cao launch --env``). On
            ``new_session=True``, these are stored on the session record and
            inherited by every worker spawned later in the same session. On
            ``new_session=False``, the persisted session vars are merged in
            automatically and the explicit ``env_vars`` argument is merged on
            top, winning on key conflict — per-step vars (e.g. workflow
            routing ids) must reach the window even inside an existing
            session. See issues #248 and #408.
        caller_id: Terminal ID of the supervisor that created this terminal
            via handoff/assign. Recorded so send_message can route callbacks
            structurally instead of parsing IDs out of message text (issue #284).
            None for operator-launched terminals.

    Returns:
        Terminal object with all metadata populated

    Raises:
        ValueError: If session already exists (new_session=True) or not found (new_session=False)
        TimeoutError: If provider initialization times out
    """
    # cond-0067: the no-env/new-session strict pre-clear is a TRUE PREFLIGHT.
    # It runs BEFORE terminal-ID generation and OUTSIDE the resource-owning
    # try/except below, so if it fails, creation aborts with ZERO cleanup
    # actions: at this point the invocation has acquired nothing, and cleanup
    # may only ever touch resources this invocation itself acquired. (When
    # this pre-clear sat inside the broad cleanup try, a failure still drove
    # FIFO stop/unlink, status clear, provider cleanup, and terminal-row
    # delete against the freshly generated — possibly 32-bit-colliding —
    # terminal ID, destroying an unrelated live terminal's state.)
    session_claim = None
    # M3-A: whether the unmanaged roster bind committed during this
    # creation.  If a later step fails, the exact incarnation must be
    # retired during unwind so a dead terminal is never left live.
    # Initialized at function scope so every cleanup path (new and
    # add-to-existing sessions alike) sees it bound.
    roster_bound = False
    if new_session:
        if not session_name:
            session_name = generate_session_name()

        # Ensure session name has the CAO prefix for identification
        if not session_name.startswith(SESSION_PREFIX):
            session_name = f"{SESSION_PREFIX}{session_name}"

        # cond-0221: acquire the physical session claim BEFORE the admission
        # check and the stale-env pre-clear, so a racing stop_session (or
        # delete) cannot write ``stopped``/collect panes between this check
        # and the physical creation below. cond-0067's zero-cleanup preflight
        # is preserved: the checks below still run before terminal-ID
        # generation and outside the resource-owning try below, so a failure
        # releases the claim and aborts with zero resource effects.
        from cli_agent_orchestrator.services import callback_recovery, session_lifecycle

        session_claim = callback_recovery.async_session_lifecycle_claim(
            type(get_backend()).__name__, session_name
        )
        await session_claim.__aenter__()
        try:
            # Admission under the claim: a stopped name still holds what a
            # resume would restore to (and a collected fleet) and must not be
            # silently recreated; an unreadable store cannot be trusted. The
            # same policy is applied to the add-to-existing path below.
            session_name = _admit_session_creation(session_name)
            # Prevent duplicate sessions (re-checked under the claim).
            if get_backend().session_exists(session_name):
                raise ValueError(f"Session '{session_name}' already exists")
            # Wipe any stale mapping a prior aborted lifecycle for this name
            # may have left behind, so a no-env relaunch can't inherit them.
            # Strict (cond-0050): if the durable delete cannot complete, this
            # raises and creation aborts BEFORE any tmux session/provider/
            # window/terminal side effect — a session name may never be
            # reused over an unconfirmed stale row.
            clear_session_env(session_name)
        except BaseException:
            await session_claim.__aexit__(None, None, None)
            session_claim = None
            raise

    session_created = False  # tracks whether THIS call created the tmux session
    # harness-control#186: tracks whether THIS call created a new WINDOW in an
    # already-existing session (the `new_session=False` branch below — what
    # every MCP spawn/assign-into-existing-session call does). Independent of
    # `session_created` above: on failure, the cleanup path already tears
    # down the whole session (window included) when THIS call created a brand
    # new one, but had no equivalent for a window added to a session that
    # already existed — see the `except` block.
    window_created = False
    try:
        # Step 1: Generate unique identifiers.  Managed launches allocate the
        # terminal id durably before provider I/O and pass it back here; the
        # ordinary path retains its existing local allocation.
        if reserved_terminal_id is None:
            terminal_id = generate_terminal_id()
        else:
            terminal_id = reserved_terminal_id
        if reserved_terminal_id is not None and not re.fullmatch(r"[a-f0-9]{8}", terminal_id):
            raise ValueError("reserved_terminal_id must be exactly 8 lowercase hex characters")
        if reserved_terminal_id is not None and terminal_generation is None:
            raise ValueError("managed reserved terminals require terminal_generation")
        if reserved_terminal_id is None and terminal_generation is not None:
            raise ValueError("terminal_generation is valid only with reserved_terminal_id")
        if trusted_project_root is not None and provider != ProviderType.CODEX.value:
            raise ValueError("trusted_project_root is supported only by the Codex provider")
        if managed_native_command is not None:
            if reserved_terminal_id is None or terminal_generation is None:
                raise ValueError("managed_native_command requires a reserved terminal generation")
            if not managed_native_command or not all(
                isinstance(item, str) and "\x00" not in item for item in managed_native_command
            ):
                raise ValueError("managed_native_command must be a non-empty argv list")
            if not os.path.isabs(managed_native_command[0]):
                raise ValueError("managed_native_command executable must be absolute")
            if new_session:
                raise ValueError(
                    "managed native launches require an existing session; atomic "
                    "process creation is only defined for window creation"
                )
        if protocol_vintage not in ("v1", "v2"):
            raise ValueError("protocol_vintage must be v1 or v2")
        if protocol_vintage == "v2" and (
            managed_native_command is None
            or reserved_terminal_id is None
            or terminal_generation is None
        ):
            raise ValueError("v2 terminal persistence requires a managed native reserved launch")

        if not session_name:
            session_name = generate_session_name()

        # Canonicalize the join target so the physical claim, the admission
        # check, and every backend effect share the canonical name (and
        # serialize against stop/delete, which take the same claim). A new
        # session already canonicalized and admitted in the preflight above.
        from cli_agent_orchestrator.services import callback_recovery, session_lifecycle

        session_name = session_lifecycle.normalise_session_name(session_name)

        # Creation and teardown share this exact backend/session claim.  It
        # covers the final existence check, physical pane/session effect, DB
        # persistence, and rollback so a stale delete cannot kill a new window
        # in a reused session name. The add-to-existing-session path acquires
        # it here (a new session already holds it from the preflight).
        if session_claim is None:
            session_claim = callback_recovery.async_session_lifecycle_claim(
                type(get_backend()).__name__, session_name
            )
            await session_claim.__aenter__()
            # Admission under the claim for the add-to-existing path: reject a
            # stopped/unreadable row before any resource declaration (v2 below)
            # or backend window effect. On refusal the resource-try finally
            # releases the claim; nothing is owned yet, so cleanup is a no-op.
            session_name = _admit_session_creation(session_name)

        window_name = (
            managed_window_name(terminal_id, terminal_generation)
            if managed_native_command is not None and terminal_generation is not None
            else generate_window_name(agent_profile)
        )

        # v2 journal-first: every intended resource is declared durably in
        # the registry BEFORE any physical window or DB row is constructed,
        # so a crash can never orphan an undeclared v2 resource and nothing
        # is ever registered as created before it is observed to exist
        # (fail-closed: the v2 plane is the registry's authority scope).
        if protocol_vintage == "v2":
            if terminal_generation is None:
                # Redundant with the v2 validation above; mypy cannot
                # narrow through it — fail closed rather than declare
                # without the exact generation.
                raise ValueError("v2 terminal persistence requires the exact generation")
            _register_v2_terminal_resources(
                terminal_id, terminal_generation, window_name, session_name
            )

        # Step 2: Create tmux session or window
        if new_session:
            # Create new tmux session with initial window
            get_backend().create_session(
                session_name,
                window_name,
                terminal_id,
                working_directory,
                extra_env=env_vars,
            )
            session_created = True  # only set after successful creation

            # Persist forwarded env only after the tmux session actually
            # exists; the failure path below clears it if a later step
            # tears the session back down.
            if env_vars:
                set_session_env(session_name, env_vars)
        else:
            # Add window to existing session
            if not get_backend().session_exists(session_name):
                raise ValueError(f"Session '{session_name}' not found")
            if managed_native_command is not None:
                # ZERO-KEYSTROKE managed creation (spec §20.2d(3)/§20.2e P1-4):
                # the bridge is the pane's OWN process/argv at window creation.
                # Nothing is typed into a shell — no send_keys, no special key,
                # no generic input — and the window env is the minimal managed
                # identity, never ambient operator session env (P1-9).
                window_name = get_backend().create_window_with_argv(
                    session_name,
                    window_name,
                    terminal_id,
                    managed_native_command,
                    working_directory,
                    extra_env=env_vars or {},
                )
            else:
                # Merge explicit per-step env_vars over the persisted session env
                # (per-step wins on conflict): workflow routing ids like
                # CAO_WORKFLOW_RUN_ID must reach the window even when it joins an
                # existing session (issue #408).
                window_name = get_backend().create_window(
                    session_name,
                    window_name,
                    terminal_id,
                    working_directory,
                    extra_env={**get_session_env(session_name), **(env_vars or {})},
                )
            window_created = True  # only set after successful creation

        # Step 3: Load the profile once for allowed tool resolution before
        # provider initialization. The skill catalog is computed only for
        # providers that consume it at launch time (see RUNTIME_SKILL_PROMPT_PROVIDERS).
        try:
            profile = load_agent_profile(agent_profile)
        except FileNotFoundError:
            profile = None
        skill_prompt = (
            build_skill_catalog(profile.skills if profile else None)
            if provider in RUNTIME_SKILL_PROMPT_PROVIDERS
            else None
        )

        # Step 3b: Resolve allowed_tools from profile if not explicitly provided
        if allowed_tools is None and profile is not None:
            from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

            mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
            allowed_tools = resolve_allowed_tools(
                profile.allowedTools, profile.role, mcp_server_names
            )

        # Soft-enforcement guard: kimi_cli/codex have NO native tool-blocking
        # mechanism (kimi runs --yolo; restrictions are prompt-level text
        # only), so a restricted policy on them is advisory, not enforced.
        # Surface that loudly at launch so operators route restricted or
        # write-capable roles to hard-enforcement providers instead.
        if provider in SOFT_ENFORCEMENT_PROVIDERS and allowed_tools and "*" not in allowed_tools:
            logger.warning(
                f"Terminal {terminal_id}: provider '{provider}' cannot enforce tool "
                f"restrictions (soft/prompt-level only) but profile '{agent_profile}' "
                f"requests {allowed_tools}. Treat this worker as unrestricted; for "
                f"enforced restrictions use claude_code, kiro_cli, or "
                f"copilot_cli."
            )

        # Step 3c: Persist terminal metadata to database after restrictions
        # are resolved so API reads and snapshots report the actual launch policy.
        # Bind the server-owned immutable pane/window identity at creation:
        # attestation binds supervisors by pane_id, never by mutable window name.
        # v2 managed terminals persist ONLY to the isolated v2 surface so
        # old-binary query/list/cleanup paths have zero visibility into them.
        identity = get_backend().window_identity(session_name, window_name) or {}
        if protocol_vintage == "v2":
            if terminal_generation is None:
                # Redundant with the v2 validation above; mypy cannot
                # narrow through it, and the v2 surface requires the exact
                # generation — fail closed rather than persist without it.
                raise ValueError("v2 terminal persistence requires the exact generation")
            # The window now physically exists: record the OBSERVED creation
            # (never before, never for an absent identity).
            _retire_reused_tmux_observation(window_name, identity)
            _mark_v2_resource_created(
                window_name,
                actor_id="terminal_service.create_terminal",
                observed=(
                    {"observed_tmux_id": identity["window_id"]}
                    if identity.get("window_id")
                    else None
                ),
                receipt_subject={"tmux_window": window_name, "identity": identity},
            )
            db_create_terminal_v2(
                terminal_id,
                session_name,
                window_name,
                provider,
                agent_profile,
                allowed_tools,
                caller_id=caller_id,
                generation=terminal_generation,
                pane_id=identity.get("pane_id"),
                window_id=identity.get("window_id"),
                server_socket_path=identity.get("server_socket_path"),
                # The rest of the canonical tuple, from the same single
                # observation. Recorded now rather than repaired later:
                # every consumer of this row treats a partial identity as
                # unusable, so a row born incomplete is a row that has to
                # be replaced rather than one that merely reads oddly.
                session_id=identity.get("session_id"),
                pane_pid=int(identity["pane_pid"]) if identity.get("pane_pid") else None,
            )
            _register_incarnation(terminal_id, terminal_generation, identity, protocol_vintage="v2")
            # The v2 DB row is durably committed: observed creation.
            _mark_v2_resource_created(
                f"{terminal_id}.db-row",
                actor_id="terminal_service.create_terminal",
                observed={"observed_db_key": f"managed_launch_v2_terminals:{terminal_id}.db-row"},
                receipt_subject={"db_row": "managed_launch_v2_terminals", "id": terminal_id},
            )
            # The forwarded-env row is session-scoped: mark it created only
            # when it is really present; otherwise the entry stays declared.
            try:
                if get_session_env(session_name):
                    _mark_v2_resource_created(
                        f"{terminal_id}.session-env",
                        actor_id="terminal_service.create_terminal",
                        observed={
                            "observed_db_key": (
                                f"session_env:{session_name}:{terminal_id}.session-env"
                            )
                        },
                        receipt_subject={"session_env_row": session_name},
                    )
            except Exception:  # noqa: BLE001 - an unanswerable probe stays declared
                logger.warning(
                    "session-env observation failed for v2 terminal %s",
                    terminal_id,
                    exc_info=True,
                )
        else:
            db_create_terminal(
                terminal_id,
                session_name,
                window_name,
                provider,
                agent_profile,
                allowed_tools,
                caller_id=caller_id,
                generation=terminal_generation,
                pane_id=identity.get("pane_id"),
                window_id=identity.get("window_id"),
                server_socket_path=identity.get("server_socket_path"),
                # The rest of the canonical tuple, from the same single
                # observation. Recorded now rather than repaired later:
                # every consumer of this row treats a partial identity as
                # unusable, so a row born incomplete is a row that has to
                # be replaced rather than one that merely reads oddly.
                session_id=identity.get("session_id"),
                pane_pid=int(identity["pane_pid"]) if identity.get("pane_pid") else None,
            )
            _register_incarnation(terminal_id, terminal_generation, identity)

        # M3-A / cond-0377: bind the stable CAO agent for every UNMANAGED
        # terminal before any real task input can be delivered to it.
        # Fail-closed: a roster bind failure unwinds the launch (typed
        # failure, zero task input) instead of returning a successfully
        # rostered terminal.  The standalone synchronous bind runs OFF the
        # asyncio event loop (``to_thread``) while still being awaited
        # before any task input or a successful return, so SQLite
        # contention and retry backoff never stall unrelated requests.
        #
        # Cancellation safety (repair-3 P1-1): cancellation does not stop
        # the worker thread.  On cancellation we shield-await the worker to
        # a KNOWN outcome before allowing cleanup/lock ownership to end; if
        # it committed, the cleanup fact is set so the cancellation teardown
        # retires the exact incarnation.  A late commit can therefore never
        # cross the teardown boundary.
        if reserved_terminal_id is None:
            worker = asyncio.create_task(
                asyncio.to_thread(
                    _roster_bind_unmanaged,
                    terminal_id=terminal_id,
                    session_name=session_name,
                    stable_agent_role=stable_agent_role,
                    agent_profile=agent_profile,
                    provider=provider,
                    terminal_generation=terminal_generation,
                    pane_id=identity.get("pane_id"),
                    pane_pid=(int(identity["pane_pid"]) if identity.get("pane_pid") else None),
                    native_status_source=native_status_source,
                )
            )
            # The worker is SHIELDED from the very first await: Python
            # delivers an outer-task cancellation to an awaited task, which
            # would mark the to_thread future cancelled while the thread
            # keeps running.  Shield keeps ``worker`` alive so its done()
            # truthfully reflects the thread's completion.
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Hold ownership: await the worker to completion (shielded,
                # tolerating repeated cancellation), then record whether it
                # committed so the teardown below retires the incarnation.
                # A late thread commit can never cross this boundary.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                try:
                    worker.result()
                except Exception:
                    roster_bound = False
                else:
                    roster_bound = True
                raise
            roster_bound = True

        # Step 4/5: Set up the FIFO event-driven output pipeline for pipe-pane
        # backends (tmux). Event-inbox backends (herdr) deliver via their own
        # socket events and their pipe_pane is a no-op, so skip the FIFO there and
        # rely on the herdr inbox registration below.
        #
        # A native TUI is skipped here too, where the monitor is *scheduled*.
        # It owns its pane's argv and renders a full-screen interface; there
        # is no line-oriented stream for a FIFO to carry and no legacy
        # provider to parse one, so scheduling the monitor is a category
        # error. Left scheduled it does not degrade quietly: every output
        # chunk reaches a provider lookup that raises for a terminal the
        # legacy table does not hold, which is the log storm observed in
        # production. Deciding it here rather than catching that exception
        # is the difference between not asking a question and asking it and
        # ignoring the answer -- only the first leaves the status source
        # free to be the observer that can actually see the pane.
        if not get_backend().supports_event_inbox() and not native_status_source:
            fifo_path = FIFO_DIR / f"{terminal_id}.fifo"

            # Reader must exist BEFORE pipe-pane starts so it captures from the
            # start. Enroll it in the pipe-pane liveness watchdog (issue #388):
            # supply a probe for tmux's live pane content and a re-arm that
            # re-attaches a stalled forwarder. The re-arm does stop-then-start,
            # NOT a bare pipe_pane() — a stalled pane still reports pane_pipe=1,
            # so the backend's ``pipe-pane -o`` toggle would just switch the
            # dead pipe OFF instead of restarting it.
            def _probe_pane(s=session_name, w=window_name) -> str:
                return get_backend().get_history(s, w, tail_lines=PIPE_LIVENESS_TAIL_LINES)

            def _rearm_pipe(s=session_name, w=window_name, p=str(fifo_path)) -> None:
                get_backend().stop_pipe_pane(s, w)
                get_backend().pipe_pane(s, w, p)

            fifo_manager.create_reader(terminal_id, pane_probe=_probe_pane, rearm=_rearm_pipe)

            # Configure pipe-pane to stream output to the FIFO. This enables
            # real-time event-driven processing via StatusMonitor and LogWriter
            # (LogWriter writes TERMINAL_LOG_DIR/{id}.log from the FIFO). A pane
            # has a single pipe-pane target, so we pipe ONLY to the FIFO.
            get_backend().pipe_pane(session_name, window_name, str(fifo_path))

            if protocol_vintage == "v2":
                # Observed creation only: the FIFO is on disk, the pane pipe
                # is attached, and the liveness watchdog is enrolled.
                if fifo_path.exists():
                    _mark_v2_resource_created(
                        f"{terminal_id}.fifo",
                        actor_id="terminal_service.create_terminal",
                        observed={"observed_fs_path": str(fifo_path)},
                        receipt_subject={"fs_exists": str(fifo_path)},
                    )
                _mark_v2_resource_created(
                    f"{terminal_id}.pipe-pane",
                    actor_id="terminal_service.create_terminal",
                    observed={"observed_db_key": f"pipe_pane:{terminal_id}.pipe-pane"},
                    receipt_subject={"pipe_pane": str(fifo_path)},
                )
                _mark_v2_resource_created(
                    f"{terminal_id}.watchdog",
                    actor_id="terminal_service.create_terminal",
                    observed={"observed_db_key": f"watchdog:{terminal_id}.watchdog"},
                    receipt_subject={"watchdog_enrolled": terminal_id},
                )

            # Nudge the shell so it re-renders its prompt AFTER pipe-pane attaches.
            # pipe-pane only captures output produced after it starts; on a fast
            # shell the initial prompt is drawn before the pipe attaches, leaving
            # the StatusMonitor buffer empty so wait_for_shell() times out. A bare
            # Enter produces a fresh prompt line that flows through the pipe.
            # Managed native windows run the bridge as their own process and
            # receive ZERO keystrokes/special keys (spec §20.2d(3)).
            if managed_native_command is None:
                get_backend().send_special_key(session_name, window_name, "Enter")

        # Managed native sessions run a structured provider bridge in the
        # reserved pane. The bridge process was created ATOMICALLY as the
        # window's own argv above — nothing is typed into a shell. The bridge,
        # not pane scraping or tmux paste, owns readiness and task admission
        # receipts for this exact generation.
        if managed_native_command is not None:
            _verify_managed_pane_process(session_name, window_name)
            if protocol_vintage == "v2":
                # The bridge process was verified as the pane's own process:
                # observed provider-instance creation.
                _mark_v2_resource_created(
                    f"{terminal_id}.provider",
                    actor_id="terminal_service.create_terminal",
                    observed={"observed_db_key": f"provider:{terminal_id}.provider"},
                    receipt_subject={"provider_process": "pane-verified"},
                )
                # Log artifacts appear lazily; mark only what really exists.
                _mark_existing_v2_fs_artifacts(terminal_id)
            terminal = Terminal(
                id=terminal_id,
                name=window_name,
                provider=ProviderType(provider),
                session_name=session_name,
                agent_profile=agent_profile,
                caller_id=caller_id,
                allowed_tools=allowed_tools,
                shell_command=None,
                status=TerminalStatus.UNKNOWN,
                last_active=datetime.now(),
            )
            dispatch_plugin_event(
                registry,
                "post_create_terminal",
                PostCreateTerminalEvent(
                    session_id=terminal.session_name,
                    terminal_id=terminal.id,
                    generation=terminal_generation,
                    agent_name=terminal.agent_profile,
                    provider=provider,
                ),
            )
            return terminal

        # Step 6: Create and initialize the CLI provider
        # This starts the agent (e.g., runs "kiro-cli chat --agent developer").
        # Only runtime-prompt providers (Claude Code, Codex, Kimi) receive
        # the skill catalog here; Kiro (skill:// resources) and OpenCode
        # (OPENCODE_CONFIG_DIR/skills symlink) discover skills natively;
        # Copilot gets the catalog baked at install time.
        provider_instance = provider_manager.create_provider(
            provider,
            terminal_id,
            session_name,
            window_name,
            agent_profile,
            allowed_tools,
            skill_prompt=skill_prompt,
            model=profile.model if profile else None,
            trusted_project_root=trusted_project_root,
            expected_model=expected_model,
            expected_effort=expected_effort,
        )

        # Deferred-init path: return fast so callers (e.g. MCP assign) do not
        # block on `provider.initialize()`. The remaining initialize + input
        # send runs as a background task, so two concurrent assigns can each
        # kick off their init in parallel. Kiro-cli 2.11's per-tool client
        # timeout (~120s observed) previously cancelled assign RPCs when init
        # took long enough to push the round-trip past that cap; deferring init
        # keeps the tool call under 2s.
        if defer_init:
            shell_command = None  # unknown until initialize() runs
            _schedule_deferred_init(
                provider_instance,
                terminal_id,
                initial_message,
                initial_message_orchestration_type,
                registry,
            )
        else:
            await provider_instance.initialize()

            # Persist shell_command baseline if the provider captured one
            shell_command = provider_instance.shell_baseline
            if not isinstance(shell_command, str):
                shell_command = None
            if shell_command:
                update_terminal_shell_command(terminal_id, shell_command)

        # Build and return the Terminal object. In the deferred-init path the
        # provider is still initializing on a background task, so the terminal
        # is NOT ready for input yet — report UNKNOWN (not IDLE) so a client
        # can't mistake it for ready and send input early. Callers poll
        # GET /terminals/{id} for the live status once init completes. The
        # synchronous path has already reached IDLE by here.
        initial_status = TerminalStatus.UNKNOWN if defer_init else TerminalStatus.IDLE
        terminal = Terminal(
            id=terminal_id,
            name=window_name,
            provider=ProviderType(provider),
            session_name=session_name,
            agent_profile=agent_profile,
            caller_id=caller_id,
            allowed_tools=allowed_tools,
            shell_command=shell_command,
            status=initial_status,
            last_active=datetime.now(),
        )

        logger.info(
            f"Created terminal: {terminal_id} in session: {session_name} (new_session={new_session})"
        )
        dispatch_plugin_event(
            registry,
            "post_create_terminal",
            PostCreateTerminalEvent(
                session_id=terminal.session_name,
                terminal_id=terminal.id,
                agent_name=terminal.agent_profile,
                provider=provider,
            ),
        )

        # Register with herdr inbox service for message delivery
        svc = get_herdr_inbox_service()
        if svc:
            try:
                pane_id = get_backend().get_pane_id(terminal_id, session_name, window_name)
                is_kiro = provider == ProviderType.KIRO_CLI.value
                svc.register_terminal(terminal_id, pane_id, is_kiro)
            except Exception as e:
                logger.warning(f"Failed to register terminal {terminal_id} with herdr inbox: {e}")
        return terminal

    except (asyncio.CancelledError, Exception) as e:
        # Cleanup on failure OR cancellation: a cancelled launch is not a
        # successfully created terminal, so the same resource cleanup runs
        # (FIFO, status, provider, roster retirement, terminal row).  This
        # catches CancelledError without broadly swallowing BaseException
        # (KeyboardInterrupt/SystemExit still propagate) and re-raises
        # below, preserving normal cancellation semantics and the primary
        # cancellation/error.
        logger.error(f"Failed to create terminal: {e}")
        # A managed no-task launch deliberately preserves a generation once
        # its durable terminal row exists.  The reservation service records the
        # structured preflight/negative evidence and owns later reconciliation;
        # deleting here would turn response loss into an unfindable generation.
        if preserve_on_init_failure:
            try:
                persisted_terminal = get_terminal_metadata(terminal_id)
                if persisted_terminal is None:
                    # v2 managed terminals persist to the isolated v2 surface.
                    persisted_terminal = get_terminal_metadata_v2(terminal_id)
            except Exception:
                # Metadata lookup itself failed.  Fail closed by leaving any
                # resources untouched; the reservation remains queryable and
                # explicit recovery must reconcile it.
                logger.warning(
                    "Could not prove whether managed terminal %s was persisted; "
                    "preserving resources for reconciliation",
                    terminal_id,
                    exc_info=True,
                )
                raise e
            if persisted_terminal is not None:
                logger.warning(
                    "Preserving managed terminal %s after initialization failure",
                    terminal_id,
                )
                raise
        try:
            fifo_manager.stop_reader(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        try:
            status_monitor.clear_terminal(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        try:
            provider_manager.cleanup_provider(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        # M3-A (i-0003): if the unmanaged roster bind committed before this
        # failure, retire the exact incarnation BEFORE removing the terminal
        # row — a dead terminal must never leave a live roster incarnation
        # that blocks identity reuse and lies to the audit.  Idempotent
        # (retiring a retired/absent incarnation is a no-op), best-effort,
        # and preserves the primary exception below.
        if roster_bound:
            _roster_retire_incarnation_best_effort(terminal_id, terminal_generation)
        # Roll back the DB terminal row so a failed create does not leave an
        # orphan record: the stale row would still be listed for the session
        # and report UNKNOWN status even though nothing is running. Idempotent
        # (DELETE ... WHERE id = ?), so it is a no-op when the failure happened
        # before the row was written. Runs regardless of session_created so a
        # pre-existing session keeps its live terminals but loses the dead row.
        try:
            db_delete_terminal(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        if session_created and session_name:
            try:
                get_backend().kill_session(session_name)
            except:
                pass  # Ignore cleanup errors
            # Session is gone, drop any forwarded env we stashed for it so
            # secrets don't linger in memory or bleed into a future reuse
            # of the same name. The store's clear is strict (cond-0050);
            # this exception-teardown path must preserve the primary failure
            # being handled above, so a failed delete is caught and logged
            # HERE — the one sanctioned softening call site — and the
            # retained row is left for the startup reconcile to retry and
            # report truthfully.
            try:
                clear_session_env(session_name)
            except Exception:
                logger.warning(
                    "could not clear session env for %s during create-terminal teardown",
                    session_name,
                    exc_info=True,
                )
        elif window_created and session_name and window_name:
            # harness-control#186: a window added to an ALREADY-EXISTING session
            # (new_session=False -- every MCP spawn/assign-into-existing-session
            # call) has no session-level teardown to fall back on above, since
            # `session_created` is False and the pre-existing session must stay
            # up. Live-reproduced without this: a provider init timeout here
            # (e.g. "Claude Code initialization timed out after 60s") rolls back
            # the DB row and stops the FIFO/provider/status-monitor above, but
            # the tmux WINDOW itself — the actual pane, still running whatever
            # shell/process the provider left behind — was never torn down.
            # Result: the caller (the spawning agent's MCP tool call) gets a
            # hard error back, AND a permanently orphaned window is left behind:
            # invisible to this terminal's own list/tree (the DB row is gone),
            # never cleaned up, sitting there indefinitely.
            try:
                get_backend().kill_window(session_name, window_name)
            except Exception:
                pass  # Ignore cleanup errors
        if protocol_vintage == "v2" and terminal_generation is not None:
            # Roll back the journal-first declarations truthfully: entries
            # still declared are aborted ONLY on a verified-absence probe
            # (physical teardown above ran first); anything already observed
            # created is drained/closed/deleted only against the same real
            # absence checks. Never a synthesized absence claim.
            try:
                _deregister_v2_terminal_resources(
                    terminal_id, terminal_generation, session_name=session_name
                )
            except Exception:
                logger.warning("v2 registry rollback failed for %s", terminal_id, exc_info=True)
        raise
    finally:
        if session_claim is not None:
            await session_claim.__aexit__(None, None, None)


def _notify_caller_of_deferred_failure(
    terminal_id: str,
    message: str,
    registry: "PluginRegistry | None",
    delete_worker: bool,
) -> None:
    """Make a deferred-init failure observable to the supervisor that assigned
    the worker, then optionally tear the worker down.

    Runs in a worker thread (blocking DB + tmux I/O). The supervisor is the
    worker's ``caller_id``; we enqueue a PENDING inbox message to it so the
    failure surfaces as the supervisor's next input instead of leaving it to
    wait forever on a callback that will never come. Every step is best-effort
    and independently guarded — a failure to notify must not prevent teardown,
    and a failure to tear down must not crash the background task.
    """
    caller_id = None
    try:
        metadata = get_terminal_metadata(terminal_id)
        if metadata:
            caller_id = metadata.get("caller_id")
    except Exception as exc:  # noqa: BLE001 — notification is best-effort
        logger.warning(
            "Deferred-init failure notify: could not read metadata for %s: %s",
            terminal_id,
            exc,
        )

    if caller_id:
        try:
            create_inbox_message(sender_id=terminal_id, receiver_id=caller_id, message=message)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "Deferred-init failure notify: could not enqueue inbox message to "
                "caller %s for worker %s: %s",
                caller_id,
                terminal_id,
                exc,
            )
    else:
        logger.warning(
            "Deferred-init failure for %s has no caller_id to notify; failure is " "log-only.",
            terminal_id,
        )

    if delete_worker:
        try:
            # Pass registry so post_kill_terminal hooks fire — parity with the
            # DELETE endpoint and agent_step teardown.
            delete_terminal(terminal_id, registry=registry)
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            logger.warning(
                "Deferred-init failure: teardown of worker %s failed (zombie "
                "window may remain): %s",
                terminal_id,
                exc,
            )


# --- deferred-init submit verification ----------------------------------------
# send_input delivers via paste-buffer → fixed sleep → Enter (clients/tmux.py).
# That fixed sleep only guesses when the TUI is input-ready; when it guesses
# wrong the Enter (or the whole paste) is dropped and the message sits
# unsubmitted in the prompt box. In the deferred-init path nobody blocks on
# completion, so a dropped submit leaves the worker IDLE forever with the task
# never started and NO exception raised — the supervisor then waits on a
# callback that can never arrive. Confirm the worker actually began processing
# and re-submit if it did not.
_DEFERRED_SUBMIT_CONFIRM_TIMEOUT = 8.0  # per-attempt wait for the PROCESSING edge
_DEFERRED_SUBMIT_MAX_RESUBMITS = 3
# Statuses proving the worker accepted the task (left the ready IDLE state).
# WAITING_USER_ANSWER counts: the worker consumed the input and is now asking.
_DEFERRED_STARTED_STATUSES = {
    TerminalStatus.PROCESSING,
    TerminalStatus.COMPLETED,
    TerminalStatus.WAITING_USER_ANSWER,
}


def _message_visible_in_box(terminal_id: str, message: str) -> bool:
    """True when the delivered message is still sitting in the input box.

    Decides the resubmit action: if our text is there the paste landed and only
    the Enter was dropped (send a bare Enter); if it is absent the paste itself
    was dropped (re-deliver the full message). Guessing wrong the other way must
    be avoided — a bare Enter into an EMPTY box would submit a blank prompt and
    the real task would be lost. Collapse to [a-z0-9] so wrapping / whitespace /
    unicode punctuation in the rendered box can't defeat the match.
    """
    probe = re.sub(r"[^a-z0-9]", "", message.lower())[:24]
    if len(probe) < 8:
        # Too short to match reliably — treat as "not shown" so we re-deliver
        # in full rather than risk a blank submit.
        return False
    try:
        rendered = get_output(terminal_id)
    except Exception:
        return False
    return probe in re.sub(r"[^a-z0-9]", "", rendered.lower())


async def _confirm_worker_started_or_resubmit(
    terminal_id: str,
    message: str,
    registry: "PluginRegistry | None",
    sender_id: Optional[str],
    orchestration_type: Optional[OrchestrationType],
) -> bool:
    """Confirm a deferred-init worker began processing; re-submit if not.

    Returns True once the terminal reaches a started status, False if it is
    still stuck at IDLE after all resubmit attempts. Blocking tmux/DB I/O runs
    off the loop via to_thread so concurrent deferred inits aren't frozen.
    """
    if await wait_until_status(
        terminal_id,
        _DEFERRED_STARTED_STATUSES,
        timeout=_DEFERRED_SUBMIT_CONFIRM_TIMEOUT,
        polling_interval=0.5,
    ):
        return True

    for attempt in range(1, _DEFERRED_SUBMIT_MAX_RESUBMITS + 1):
        if await asyncio.to_thread(_message_visible_in_box, terminal_id, message):
            logger.warning(
                "Deferred assign to %s unsubmitted (Enter swallowed); "
                "re-submitting via Enter (attempt %d)",
                terminal_id,
                attempt,
            )
            await asyncio.to_thread(send_special_key, terminal_id, "Enter")
        else:
            logger.warning(
                "Deferred assign to %s not accepted (paste dropped); "
                "re-delivering message (attempt %d)",
                terminal_id,
                attempt,
            )
            await asyncio.to_thread(
                send_input,
                terminal_id,
                message,
                registry=registry,
                sender_id=sender_id,
                orchestration_type=orchestration_type,
            )
        if await wait_until_status(
            terminal_id,
            _DEFERRED_STARTED_STATUSES,
            timeout=_DEFERRED_SUBMIT_CONFIRM_TIMEOUT,
            polling_interval=0.5,
        ):
            return True

    return False


def _schedule_deferred_init(
    provider_instance,
    terminal_id: str,
    initial_message: Optional[str],
    orchestration_type: Optional[OrchestrationType],
    registry: PluginRegistry | None,
) -> None:
    """Kick off provider.initialize() in the background and, on success,
    deliver the initial message via send_input.

    Runs as an asyncio task on the running event loop so it doesn't block
    the caller. Because assign() has already returned success=True by the
    time this runs, a failure here must be made OBSERVABLE to the supervisor
    rather than silently swallowed — otherwise the supervisor waits forever
    on a callback that can never arrive and a later inspect 404s. On failure
    we notify the caller's inbox (best-effort) and then tear the worker down.

    ``TerminalInputBlockedError`` (the worker is parked on a WAITING_USER_ANSWER
    prompt right after init) is NOT a teardown case: the worker is alive and
    answerable via answer_user_prompt, so we leave it in place and only log.
    """

    async def _run() -> None:
        caller_id: Optional[str] = None
        try:
            await provider_instance.initialize()
            shell_command = provider_instance.shell_baseline
            if isinstance(shell_command, str) and shell_command:
                update_terminal_shell_command(terminal_id, shell_command)
            if initial_message:
                # For assign/handoff the sender is the CALLER (the supervisor),
                # not this MCP server. But the deferred path is used only via
                # /assign, and _assign_impl on the MCP-server side already
                # embedded the callback instructions into initial_message.
                # We still pass sender_id=caller_id if present in DB metadata
                # so plugin events see it.
                metadata = await asyncio.to_thread(get_terminal_metadata, terminal_id)
                if metadata:
                    caller_id = metadata.get("caller_id")
                # send_input is blocking tmux I/O — off the loop so it can't
                # freeze the server for concurrent requests.
                await asyncio.to_thread(
                    send_input,
                    terminal_id,
                    initial_message,
                    registry=registry,
                    sender_id=caller_id,
                    orchestration_type=orchestration_type,
                )
                # Delivery can be silently dropped (Enter swallowed / paste lost)
                # when the TUI isn't input-ready. Confirm the worker actually
                # started and re-submit if not; if it never starts, surface the
                # failure so the supervisor re-routes instead of waiting forever.
                started = await _confirm_worker_started_or_resubmit(
                    terminal_id,
                    initial_message,
                    registry,
                    caller_id,
                    orchestration_type,
                )
                if not started:
                    logger.error(
                        "Deferred init for %s: worker never started after "
                        "resubmits; task not delivered — notifying caller and "
                        "tearing down.",
                        terminal_id,
                    )
                    await asyncio.to_thread(
                        _notify_caller_of_deferred_failure,
                        terminal_id,
                        (
                            f"Worker {terminal_id} received the assigned task but "
                            f"never started processing (input not accepted after "
                            f"retries). It has been deleted — re-assign the task."
                        ),
                        registry,
                        True,  # delete_worker
                    )
                    return
        except TerminalInputBlockedError as e:
            # The worker initialized but is parked on an interactive prompt
            # (WAITING_USER_ANSWER). It is alive and can be driven via
            # answer_user_prompt — do NOT delete it. Just surface the state to
            # the supervisor so it knows delivery is pending on a prompt.
            logger.warning(
                "Deferred init for terminal %s: worker is waiting on a user "
                "prompt; task not yet delivered. Leaving worker alive for "
                "answer_user_prompt. (%s)",
                terminal_id,
                e,
            )
            await asyncio.to_thread(
                _notify_caller_of_deferred_failure,
                terminal_id,
                f"Worker {terminal_id} is waiting on an interactive prompt; the "
                f"assigned task has not been delivered yet. Use answer_user_prompt "
                f"to unblock it, then it will receive the task.",
                registry,
                delete_worker=False,
            )
        except Exception as e:
            # exc_info=True preserves the traceback for debugging; {e!r} avoids
            # newline/control-character injection into logs and the inbox message
            # (the exception text can contain provider-supplied content).
            logger.error(
                "Deferred init for terminal %s failed: %r. "
                "Notifying caller and tearing down worker.",
                terminal_id,
                e,
                exc_info=True,
            )
            await asyncio.to_thread(
                _notify_caller_of_deferred_failure,
                terminal_id,
                f"Worker {terminal_id} failed to initialize: {e!r}. It has been "
                f"deleted — re-assign the task or report the failure.",
                registry,
                delete_worker=True,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error(f"Deferred init for {terminal_id}: no running event loop; init skipped")
        return
    task = loop.create_task(_run())
    _deferred_init_tasks.add(task)
    task.add_done_callback(_deferred_init_tasks.discard)


def get_terminal(terminal_id: str) -> Dict:
    """Get terminal data."""
    try:
        metadata = _get_terminal_metadata_any(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # A pre-identity-persistence terminal is eligible only when BOTH
        # fields are absent.  The backend must prove the candidate pane's
        # process lineage still carries this exact terminal id; the mutable
        # window name merely locates a candidate and is never sufficient.
        if (
            "pane_id" in metadata
            and "window_id" in metadata
            and metadata.get("pane_id") is None
            and metadata.get("window_id") is None
            and metadata.get("protocol_vintage") != "v2"
        ):
            identity = get_backend().terminal_bound_window_identity(
                terminal_id,
                metadata["tmux_session"],
                metadata["tmux_window"],
            )
            if (
                isinstance(identity, dict)
                and isinstance(identity.get("pane_id"), str)
                and identity["pane_id"]
                and isinstance(identity.get("window_id"), str)
                and identity["window_id"]
                and backfill_terminal_identity_if_missing(
                    terminal_id, identity["pane_id"], identity["window_id"]
                )
            ):
                refreshed = get_terminal_metadata(terminal_id)
                if refreshed is not None:
                    metadata = refreshed

        status = status_monitor.get_status(terminal_id).value

        return {
            "id": metadata["id"],
            "name": metadata["tmux_window"],
            "provider": metadata["provider"],
            "session_name": metadata["tmux_session"],
            "agent_profile": metadata["agent_profile"],
            "caller_id": metadata.get("caller_id"),
            "allowed_tools": metadata.get("allowed_tools"),
            "pane_id": metadata.get("pane_id"),
            "window_id": metadata.get("window_id"),
            "status": status,
            "last_active": metadata["last_active"],
        }

    except Exception as e:
        logger.error(f"Failed to get terminal {terminal_id}: {e}")
        raise


def get_working_directory(terminal_id: str) -> Optional[str]:
    """Get the current working directory of a terminal's pane.

    Args:
        terminal_id: The terminal identifier

    Returns:
        Working directory path, or None if pane has no directory

    Raises:
        ValueError: If terminal not found
        Exception: If unable to query working directory
    """
    try:
        metadata = _get_terminal_metadata_any(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        verified = verified_pane_target(terminal_id, metadata, operation="working-directory")
        session_name = verified.session_name if verified else metadata["tmux_session"]
        window_name = verified.window_name if verified else metadata["tmux_window"]
        working_dir = get_backend().get_pane_working_directory(session_name, window_name)
        return working_dir

    except Exception as e:
        logger.error(f"Failed to get working directory for terminal {terminal_id}: {e}")
        raise


def _send_keys_copy_mode_guarded(
    terminal_id: str,
    metadata: Dict[str, Any],
    verified: VerifiedPaneTarget,
    message: str,
    *,
    enter_count: int,
    submit_delay: float,
) -> None:
    """Write one v1 payload to a proven pane through the pane-arbiter boundary.

    The legacy/v1 ordinary delivery write, held to the same boundary the
    control-input path established (cond-0178).  Everything from the
    under-lease identity re-proof to the payload's trailing Enter happens
    while holding the exact pane's input lease, so no other writer can
    interleave between the copy-mode guard's proof and the payload:

    1. re-prove the bound identity live (the guard's first read is also the
       pane-dead / identity-mismatch / server-identity re-proof),
    2. read ``pane_in_mode`` on that exact pane,
    3. only on a proven ``1``, send the one non-payload keystroke
       (``send-keys -X cancel``) to that exact pane,
    4. re-prove the identity and ``pane_in_mode=0``,
    5. only then arm status detection, clear the buffer, and write the
       original payload exactly once, inside the same lease.

    A dashboard wheel-scroll can leave the pane in copy mode, where the
    payload's Enter is consumed by the mode and the text rests unsubmitted
    while every transport fact reads success.  Detection that cannot be
    proven is a zero-byte refusal, never a speculative cancel and never a
    delivery claim; the cancel is never sent speculatively either.

    Raises:
        TerminalInputRefusedError: the pane lease was busy or the copy-mode
            guard refused.  Both are proven zero-payload-byte outcomes
            raised BEFORE the status arm and buffer clear, so a refused
            write leaves detection exactly as it found it and may be
            re-attempted.  ``reason_code`` uses the control-input
            contract's closed vocabulary (``copy-mode-active`` for the
            guard's own refusals, ``pane-busy`` for the lease).
        Exception: anything the payload write itself raises propagates
            unchanged — bytes may have landed, so the write is ambiguous
            and is never silently re-typed.
    """
    # Call-site-local imports, matching this file's managed_launch pattern:
    # the v2 control-input module's import graph stays untouched, and the
    # guard itself is reused rather than duplicated.
    import hashlib

    from cli_agent_orchestrator.services import control_input_service
    from cli_agent_orchestrator.services.control_input_contract import REASON_PANE_BUSY
    from cli_agent_orchestrator.services.control_input_journal import ControlInputBinding
    from cli_agent_orchestrator.services.pane_input_arbiter import (
        PaneBusyError,
        pane_input_lease,
    )

    # A verified target exists only when the backend declared pane-identity
    # support, which only the tmux backend does, so under it this client is
    # never None.  A non-tmux backend has no tmux copy mode to guard
    # against; the lease still serializes the write there.
    client = control_input_service._tmux_client()
    binding = ControlInputBinding(
        request_id=f"send-input:{terminal_id}",
        terminal_id=terminal_id,
        pane_id=verified.pane_id,
        window_id=verified.window_id,
        pane_pid=verified.pane_pid,
        request_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        generation=metadata.get("generation") or None,
        server_socket_path=verified.server_socket_path or None,
    )
    try:
        with pane_input_lease(verified.pane_id, holder=f"send-input:{terminal_id}", timeout=0.0):
            if client is not None:
                refusal = control_input_service._copy_mode_guard_refusal(
                    client,
                    binding,
                    deadline_monotonic=time.monotonic()
                    + control_input_service.WRITE_DEADLINE_SECONDS,
                )
                if refusal is not None:
                    raise TerminalInputRefusedError(refusal[0], refusal[1])

            # Arm the StatusMonitor stickiness gate so that the next provider-
            # detected PROCESSING transition is honored (overriding the latched
            # IDLE/COMPLETED). Without this, sticky ready-status would block
            # the genuine PROCESSING signal that arrives once the agent starts
            # working on the new message.
            status_monitor.notify_input_sent(terminal_id)

            # Clear ONLY the rolling byte buffer BEFORE sending keys, so stale idle
            # prompts from BEFORE the input can't trigger a false COMPLETED
            # (kiro-cli 2.11's TUI keeps the "ask a question" placeholder in the raw
            # buffer, which combined with input_received=True would return COMPLETED
            # within seconds of send_input). Clearing here — not after send_keys —
            # avoids a race: send_keys includes a submit-delay sleep during which
            # the agent can begin emitting output; a post-send_keys clear would wipe
            # that newly-emitted first chunk of the turn (lost from
            # GET /terminals/{id}/output?mode=full and from early detection). This
            # uses clear_rolling_buffer (byte-only), which preserves the sticky-latch
            # arm set by notify_input_sent above; reset_buffer would wipe the arm and
            # latch-block the IDLE→PROCESSING transition for the whole turn.
            status_monitor.clear_rolling_buffer(terminal_id)

            # Target the verified pane id, not the recorded names. The proof
            # above and the write below are then about the same pane with no
            # name resolution in between for a later window to answer.
            get_backend().send_keys(
                verified.session_name,
                verified.window_name,
                message,
                enter_count=enter_count,
                force_bracketed_paste=True,
                submit_delay=submit_delay,
                pane_id=verified.pane_id,
            )
    except PaneBusyError as exc:
        raise TerminalInputRefusedError(
            REASON_PANE_BUSY,
            f"another writer holds pane {verified.pane_id}: {exc}; nothing was typed",
        ) from exc


def send_input(
    terminal_id: str,
    message: str,
    registry: PluginRegistry | None = None,
    sender_id: str | None = None,
    orchestration_type: OrchestrationType | None = None,
) -> bool:
    """Send input to terminal via tmux paste buffer.

    Uses bracketed paste mode (-p) to bypass TUI hotkey handling. The number
    of Enter keys sent after pasting is determined by the provider's
    ``paste_enter_count`` property (e.g., some TUIs need 2 Enters because
    bracketed paste triggers multi-line mode).
    """
    try:
        # Managed panes are human-readable renderers over a private native-RPC
        # bridge.  Raw tmux input would target the bridge console/transport, not
        # the provider's semantic input API.  Enforce this at the shared sink so
        # a forgotten caller cannot reintroduce paste-based steering.
        from cli_agent_orchestrator.services import managed_launch

        if managed_launch.managed_control_identity(terminal_id) is not None:
            raise TerminalInputBlockedError(
                f"Terminal {terminal_id} is managed; use a generation-bound "
                "managed session operation instead of tmux input"
            )
        metadata = _get_terminal_metadata_any(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Prove the recorded pane is still the registered one before any of
        # the work below — the status arming and buffer clear mutate state,
        # and doing them for a write that is about to be refused would leave
        # the wrong terminal's detection disturbed.
        verified = verified_pane_target(terminal_id, metadata, operation="send-input")

        provider = provider_manager.get_provider(terminal_id)
        orchestration_value = (
            orchestration_type.value
            if isinstance(orchestration_type, OrchestrationType)
            else str(orchestration_type or "")
        )

        if provider:
            current_status = status_monitor.get_status(terminal_id)

            # Guard: refuse to type into a terminal whose provider process has
            # exited. Without this check, queued messages would be pasted into
            # a bare shell and executed as arbitrary commands.
            if current_status == TerminalStatus.ERROR:
                raise TerminalInputBlockedError(
                    f"Terminal {terminal_id} provider is in ERROR state "
                    "(provider process may have exited). Refusing to deliver input."
                )

            if (
                provider.blocks_orchestrated_input_while_waiting_user_answer is True
                and orchestration_value
                in {OrchestrationType.ASSIGN.value, OrchestrationType.HANDOFF.value}
                and current_status == TerminalStatus.WAITING_USER_ANSWER
            ):
                raise TerminalInputBlockedError(
                    f"Terminal {terminal_id} is waiting for a user answer. "
                    "Use answer_user_prompt to submit a selection or approval before "
                    f"sending {orchestration_value} input."
                )

        # Inject memory context into the very first user message after init.
        # Phase 1 wires injection inline for every provider. The Kiro
        # AgentSpawn hook will replace this path once the plugin
        # migration PR lands; until then, inline injection is the only
        # delivery path.
        # Keep the original message for the PostSendMessageEvent so
        # plugins/webhooks see what the caller sent — not the
        # internal <cao-memory> block that we paste into the TUI.
        original_message = message
        message = inject_memory_context(message, terminal_id)

        # Check how many Enter keys the provider needs after paste
        enter_count = provider.paste_enter_count if provider else 1
        submit_delay = provider.paste_submit_delay if provider else 0.3

        if verified is not None:
            # The identity-bound path writes through the same pane-arbiter
            # boundary the control-input write uses (cond-0178): the pane
            # lease, the under-lease identity re-proof, and the copy-mode
            # guard run BEFORE the status arm and buffer clear below, so a
            # refused write leaves detection exactly as it found it — and
            # the payload write itself stays inside the same lease.
            _send_keys_copy_mode_guarded(
                terminal_id,
                metadata,
                verified,
                message,
                enter_count=enter_count,
                submit_delay=submit_delay,
            )
        else:
            # Arm the StatusMonitor stickiness gate so that the next provider-
            # detected PROCESSING transition is honored (overriding the latched
            # IDLE/COMPLETED). Without this, sticky ready-status would block
            # the genuine PROCESSING signal that arrives once the agent starts
            # working on the new message.
            status_monitor.notify_input_sent(terminal_id)

            # Clear ONLY the rolling byte buffer BEFORE sending keys, so stale idle
            # prompts from BEFORE the input can't trigger a false COMPLETED
            # (kiro-cli 2.11's TUI keeps the "ask a question" placeholder in the raw
            # buffer, which combined with input_received=True would return COMPLETED
            # within seconds of send_input). Clearing here — not after send_keys —
            # avoids a race: send_keys includes a submit-delay sleep during which
            # the agent can begin emitting output; a post-send_keys clear would wipe
            # that newly-emitted first chunk of the turn (lost from
            # GET /terminals/{id}/output?mode=full and from early detection). This
            # uses clear_rolling_buffer (byte-only), which preserves the sticky-latch
            # arm set by notify_input_sent above; reset_buffer would wipe the arm and
            # latch-block the IDLE→PROCESSING transition for the whole turn.
            status_monitor.clear_rolling_buffer(terminal_id)

            get_backend().send_keys(
                metadata["tmux_session"],
                metadata["tmux_window"],
                message,
                enter_count=enter_count,
                force_bracketed_paste=True,
                submit_delay=submit_delay,
            )

        # Notify the provider that external input was received.
        # This allows providers to adjust status
        # detection — specifically to stop reporting IDLE for the post-init
        # state and resume normal COMPLETED detection after a real task.
        if provider:
            provider.mark_input_received()

        update_last_active(terminal_id)
        logger.info(f"Sent input to terminal: {terminal_id}")
        if registry is not None and sender_id is not None and orchestration_type is not None:
            # Telemetry (opt-in; no-ops without the [otel] extra or when the SDK
            # is disabled): record a GenAI ``execute_tool`` span for the dispatch,
            # count it, and propagate the active trace context into the plugin
            # event so downstream consumers can continue the trace.
            from cli_agent_orchestrator.telemetry import (
                execute_tool_span,
                inject_traceparent,
                record_orchestration_dispatch,
            )

            with execute_tool_span(
                f"send_message:{orchestration_value}",
                conversation_id=metadata["tmux_session"],
            ):
                record_orchestration_dispatch(orchestration_value)
                dispatch_plugin_event(
                    registry,
                    "post_send_message",
                    PostSendMessageEvent(
                        session_id=metadata["tmux_session"],
                        sender=sender_id,
                        receiver=terminal_id,
                        message=original_message,
                        orchestration_type=orchestration_type,
                        traceparent=inject_traceparent(),
                    ),
                )
        return True

    except Exception as e:
        logger.error(f"Failed to send input to terminal {terminal_id}: {e}")
        raise


def send_special_key(terminal_id: str, key: str) -> bool:
    """Send a tmux special key sequence (e.g., C-d, C-c) to terminal.

    Unlike send_input(), this sends the key as a tmux key name (not literal text)
    and does not append a carriage return. Used for control signals like Ctrl+D (EOF).

    Held to the same identity boundary as ``send_input``, and for a sharper
    reason. A control key is not a smaller write than a message: ``Enter``
    submits whatever is sitting in a composer, and ``C-c`` interrupts
    whatever is running. Delivered to a window resolved by *name*, after
    that name has been reused, both act on a stranger's session — and
    neither leaves the trace a mistyped message would.

    Args:
        terminal_id: Target terminal identifier
        key: Tmux key name (e.g., "C-d", "C-c", "Escape")

    Returns:
        True if the key was sent successfully

    Raises:
        ValueError: If terminal not found
        TerminalIdentityMismatchError: If the recorded pane is no longer the
            registered one. Nothing is delivered.
    """
    try:
        from cli_agent_orchestrator.services import managed_launch

        if managed_launch.managed_control_identity(terminal_id) is not None:
            raise TerminalInputBlockedError(
                f"Terminal {terminal_id} is managed; raw tmux keys are disabled"
            )
        metadata = _get_terminal_metadata_any(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Proven before the key is armed, let alone sent: a refusal here
        # must leave the status monitor untouched as well as the pane, or a
        # terminal nobody wrote to would still be marked as having received
        # input.
        target = verified_pane_target(terminal_id, metadata, operation=f"special key {key!r}")

        # Arm StatusMonitor stickiness: special keys (Enter on a permission
        # prompt, C-c interrupting work, C-d sending EOF) all initiate a new
        # processing cycle that must be allowed to push past any latched
        # ready status.
        status_monitor.notify_input_sent(terminal_id)
        if target is not None:
            get_backend().send_special_key(
                target.session_name, target.window_name, key, pane_id=target.pane_id
            )
        else:
            get_backend().send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)

        update_last_active(terminal_id)
        logger.info(f"Sent special key '{key}' to terminal: {terminal_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send special key to terminal {terminal_id}: {e}")
        raise


def exit_terminal_cli(terminal_id: str) -> None:
    """Send the provider-specific exit command to gracefully shut down the CLI.

    Mirrors the ``POST /terminals/{id}/exit`` endpoint: resolve the provider,
    send ``provider.exit_cli()`` — as a tmux key sequence when it is one (e.g.
    ``C-d``), else as literal input (e.g. ``/exit``). This is the graceful CLI
    shutdown that should precede ``delete_terminal`` (which goes straight to
    ``kill_window``). Both the endpoint and ``run_agent_step`` call this so the
    exit-then-delete lifecycle is implemented once.

    Raises:
        ValueError: if no provider is registered for ``terminal_id``.
    """
    provider = provider_manager.get_provider(terminal_id)
    if provider is None:
        raise ValueError(f"Provider not found for terminal {terminal_id}")
    exit_command = provider.exit_cli()
    # Some providers use tmux key sequences (e.g., "C-d" for Ctrl+D) instead of
    # text commands (e.g., "/exit"). Key sequences must be sent via
    # send_special_key() to be interpreted by tmux, not as literal text.
    if exit_command.startswith(("C-", "M-")):
        send_special_key(terminal_id, exit_command)
    else:
        send_input(terminal_id, exit_command)


def get_output(terminal_id: str, mode: OutputMode = OutputMode.FULL) -> str:
    """Get terminal output.

    ``FULL`` mode returns the StatusMonitor rolling buffer (the streamed output
    accumulated from the FIFO pipeline), which is bounded to the most recent
    ``STATE_BUFFER_MAX`` bytes (8KB); it falls back to a tmux history capture
    only when that buffer is empty. This is a deliberate trade-off in the
    event-driven architecture (instant, no tmux call) — it is *not* unbounded
    scrollback, so very long sessions are truncated to the tail. Use the
    on-disk ``{id}.log`` (LogWriter) or the delete-time ``{id}.scrollback``
    snapshot when complete history is required.

    For ``LAST`` mode, if the provider declares ``extraction_retries > 0``,
    retries extraction with 10 s delays between attempts.  This handles
    TUI-based providers (e.g. Antigravity CLI's renderer) whose notification
    spinners can temporarily obscure response text in the tmux capture buffer.

    If the provider exposes an ``extraction_tail_lines`` attribute, that
    fixed value is used for the history capture and the escalating-fetch
    logic below is skipped.

    Otherwise, extraction uses an escalating fetch strategy: start with a
    small capture window and widen until the response marker is found.
    Steps: 200 -> 500 -> 1000 -> 5000.  If no marker is found at 5000 lines,
    the raw tail is returned with a [PARTIAL RESPONSE] prefix so the caller
    knows the output may be incomplete.
    """
    # Escalation steps used when the provider does not declare extraction_tail_lines.
    _ESCALATION_STEPS = [200, 500, 1000, 5000]

    try:
        metadata = _get_terminal_metadata_any(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Every history capture below reads the names the verified pane
        # answers to now. Reading the recorded names instead would return a
        # later, unrelated window's screen as this terminal's output — the
        # same misdelivery as a write, just in the other direction.
        verified = verified_pane_target(terminal_id, metadata, operation="get-output")
        capture_session = verified.session_name if verified else metadata["tmux_session"]
        capture_window = verified.window_name if verified else metadata["tmux_window"]

        # Get output from StatusMonitor buffer (instant, no tmux call)
        full_output = status_monitor.get_buffer(terminal_id)
        if not full_output:
            # Fallback to backend history only if buffer not available (edge case)
            full_output = get_backend().get_history(capture_session, capture_window)

        if mode == OutputMode.FULL:
            return full_output
        elif mode == OutputMode.LAST:
            provider = provider_manager.get_provider(terminal_id)
            if provider is None:
                raise ValueError(f"Provider not found for terminal {terminal_id}")

            # If the provider pins a fixed scrollback depth, honour it and skip
            # escalation — the provider knows what it needs.
            fixed_extract_lines = getattr(provider, "extraction_tail_lines", None)
            if fixed_extract_lines is not None:
                full_output = get_backend().get_history(
                    capture_session,
                    capture_window,
                    tail_lines=fixed_extract_lines,
                )
                retries = provider.extraction_retries
                last_err: Exception | None = None
                for attempt in range(1 + retries):
                    try:
                        if attempt > 0:
                            time.sleep(10.0)
                            full_output = get_backend().get_history(
                                capture_session,
                                capture_window,
                                tail_lines=fixed_extract_lines,
                            )
                        return provider.extract_last_message_from_script(full_output)
                    except ValueError as exc:
                        last_err = exc
                        logger.debug(
                            "Output extraction attempt %d/%d for %s failed: %s",
                            attempt + 1,
                            1 + retries,
                            terminal_id,
                            exc,
                        )
                raise last_err  # type: ignore[misc]

            # Escalating fetch: try progressively larger capture windows until
            # the response marker is found or we hit the cap.
            last_err = None
            full_output = ""
            for step_lines in _ESCALATION_STEPS:
                full_output = get_backend().get_history(
                    capture_session,
                    capture_window,
                    tail_lines=step_lines,
                )
                try:
                    result = provider.extract_last_message_from_script(full_output)
                    if step_lines > _ESCALATION_STEPS[0]:
                        logger.debug(
                            "get_output: %s marker found at %d lines",
                            terminal_id,
                            step_lines,
                        )
                    return result
                except ValueError as exc:
                    last_err = exc
                    logger.debug(
                        "get_output: %s no marker at %d lines, escalating",
                        terminal_id,
                        step_lines,
                    )

            # All tail-based steps failed — try full scrollback before giving up.
            logger.debug(
                "get_output: %s escalation exhausted, trying full_history",
                terminal_id,
            )
            full_output = get_backend().get_history(
                metadata["tmux_session"],
                metadata["tmux_window"],
                full_history=True,
            )
            try:
                result = provider.extract_last_message_from_script(full_output)
                logger.debug("get_output: %s marker found in full_history", terminal_id)
                return result
            except ValueError:
                pass

            # Full scrollback also failed — distinguish overflow from no response.
            # If the buffer is close to full (>=90% of last escalation cap), the
            # response marker was likely produced but pushed past the scrollback
            # limit (overflow).  If the buffer is mostly empty, the agent never
            # produced a text response (e.g. only tool calls, crash, or timeout).
            actual_lines = full_output.count("\n") + 1
            overflow_threshold = int(_ESCALATION_STEPS[-1] * 0.9)
            if actual_lines >= overflow_threshold:
                logger.warning(
                    "get_output: %s response marker not found, buffer near-full "
                    "(%d lines >= %d threshold) — likely overflow",
                    terminal_id,
                    actual_lines,
                    overflow_threshold,
                )
                return (
                    f"[PARTIAL RESPONSE - response marker not found, buffer overflow likely "
                    f"({actual_lines} lines retrieved)]\n{full_output}"
                )
            else:
                logger.warning(
                    "get_output: %s response marker not found, buffer sparse "
                    "(%d lines < %d threshold) — agent likely produced no text response",
                    terminal_id,
                    actual_lines,
                    overflow_threshold,
                )
                return (
                    f"[NO RESPONSE - agent completed without producing a text response "
                    f"({actual_lines} lines in buffer)]\n{full_output}"
                )

    except Exception as e:
        logger.error(f"Failed to get output from terminal {terminal_id}: {e}")
        raise


def retire_closed_workspace_session(
    session_name: str,
    registry: PluginRegistry | None = None,
    *,
    unregister_inbox: bool = True,
) -> list[str]:
    """Retire DB state after Herdr authoritatively reports a closed workspace.

    Every terminal incarnation is claimed and revalidated before any row is
    removed.  An open callback recovery on either the source or its bound
    supervisor holds the entire session so a workspace lifecycle event cannot
    bypass managed teardown.
    """
    from cli_agent_orchestrator.clients.database import list_terminals_by_session
    from cli_agent_orchestrator.services import callback_recovery

    observed: list[dict[str, Any]] = []
    for row in list_terminals_by_session(session_name):
        metadata = _get_terminal_metadata_any(row["id"])
        if metadata is not None:
            observed.append(metadata)

    claim_keys = callback_recovery.terminal_lifecycle_claim_set(*observed)
    # A workspace-close event is a destructor too.  Acquire the identical
    # session claim as creation and explicit deletion before every terminal
    # claim, so a stale closed-workspace decision cannot win over a new pane.
    claim_keys.add(("", "session-workspace", f"{type(get_backend()).__name__}:{session_name}"))

    with callback_recovery.generation_lifecycle_claims(claim_keys):

        current_rows: list[dict[str, Any]] = []
        for item in observed:
            current = _get_terminal_metadata_any(item["id"])
            if current is None:
                continue
            if (
                current.get("tmux_session") != session_name
                or current.get("generation") != item.get("generation")
                or current.get("pane_id") != item.get("pane_id")
            ):
                raise TerminalGenerationMismatchError(
                    f"terminal {item['id']} changed incarnation while retiring "
                    f"closed workspace {session_name}"
                )
            if callback_recovery.terminal_has_open_recovery(item["id"], item.get("generation")):
                raise TerminalGenerationMismatchError(
                    f"terminal {item['id']} has an open callback-recovery "
                    "operation; closed-workspace cleanup is held"
                )
            current_rows.append(current)

        retired: list[str] = []
        for item in current_rows:
            generation = item.get("generation")
            kwargs: dict[str, Any] = {
                "backend_already_closed": True,
                "unregister_inbox": unregister_inbox,
            }
            if generation:
                kwargs.update(
                    expected_generation=generation,
                    expected_session=session_name,
                )
            delete_terminal(item["id"], registry=registry, **kwargs)
            retired.append(item["id"])
        return retired


def retire_observed_terminal(
    terminal_id: str,
    registry: PluginRegistry | None = None,
    *,
    expected_session: str | None = None,
    expected_pane_id: str | None = None,
    backend_already_closed: bool = True,
    unregister_inbox: bool = True,
) -> bool:
    """Retire one lifecycle-observed terminal without bypassing recovery.

    Herdr startup, reconcile, and lifecycle events all converge here. The
    durable model generation is the teardown authority; pane identity is a
    separate secondary claim used to detect compact-pane reuse.
    """
    from cli_agent_orchestrator.services import callback_recovery

    observed = _get_terminal_metadata_any(terminal_id)
    if observed is None:
        return True
    if expected_pane_id is not None and observed.get("pane_id") != expected_pane_id:
        raise TerminalGenerationMismatchError(
            f"terminal {terminal_id} pane identity changed before lifecycle retirement"
        )
    claim_keys = callback_recovery.terminal_lifecycle_claim_set(observed)
    session_name = str(observed.get("tmux_session") or "")
    if session_name:
        claim_keys.add(("", "session-workspace", f"{type(get_backend()).__name__}:{session_name}"))

    with callback_recovery.generation_lifecycle_claims(claim_keys):
        current = _get_terminal_metadata_any(terminal_id)
        if current is None:
            return True
        if (
            current.get("generation") != observed.get("generation")
            or current.get("pane_id") != observed.get("pane_id")
            or current.get("tmux_session") != observed.get("tmux_session")
            or (expected_session is not None and current.get("tmux_session") != expected_session)
            or (expected_pane_id is not None and current.get("pane_id") != expected_pane_id)
        ):
            raise TerminalGenerationMismatchError(
                f"terminal {terminal_id} changed incarnation before lifecycle retirement"
            )
        if callback_recovery.terminal_has_open_recovery(terminal_id, observed.get("generation")):
            raise TerminalGenerationMismatchError(
                f"terminal {terminal_id} has an open callback-recovery operation"
            )
        return _delete_terminal_claimed(
            terminal_id,
            registry=registry,
            expected_generation=observed.get("generation"),
            expected_session=expected_session or observed.get("tmux_session"),
            backend_already_closed=backend_already_closed,
            unregister_inbox=unregister_inbox,
        )


def delete_terminal(
    terminal_id: str,
    registry: PluginRegistry | None = None,
    *,
    expected_generation: str | None = None,
    expected_session: str | None = None,
    via_destructive_endpoint: bool = False,
    backend_already_closed: bool = False,
    unregister_inbox: bool = True,
) -> bool:
    """Delete under the same exact-generation claim recovery admission uses."""
    from cli_agent_orchestrator.services import callback_recovery

    if expected_generation is not None:
        expected_claim = (terminal_id, "model-generation", expected_generation)
        held = getattr(callback_recovery._LIFECYCLE_CLAIMS, "held", set())
        if expected_claim not in held:
            # Preserve the first durable row observation for the claimed
            # teardown itself.  An eager metadata probe changes the meaning of
            # the row-absent and replacement-race paths before they can record
            # their required specialized refusal.
            with callback_recovery.generation_lifecycle_claim(terminal_id, expected_generation):
                return _delete_terminal_claimed(
                    terminal_id,
                    registry=registry,
                    expected_generation=expected_generation,
                    expected_session=expected_session,
                    via_destructive_endpoint=via_destructive_endpoint,
                    backend_already_closed=backend_already_closed,
                    unregister_inbox=unregister_inbox,
                )

    metadata = get_terminal_metadata(terminal_id)
    if metadata is None:
        metadata = get_terminal_metadata_v2(terminal_id)
    claim_keys = callback_recovery.terminal_lifecycle_claim_set(metadata)
    if expected_generation and not claim_keys:
        claim_keys.add((terminal_id, "model-generation", expected_generation))
    with callback_recovery.generation_lifecycle_claims(claim_keys):
        return _delete_terminal_claimed(
            terminal_id,
            registry=registry,
            expected_generation=expected_generation,
            expected_session=expected_session,
            via_destructive_endpoint=via_destructive_endpoint,
            backend_already_closed=backend_already_closed,
            unregister_inbox=unregister_inbox,
        )


def _delete_terminal_claimed(
    terminal_id: str,
    registry: PluginRegistry | None = None,
    *,
    expected_generation: str | None = None,
    expected_session: str | None = None,
    via_destructive_endpoint: bool = False,
    backend_already_closed: bool = False,
    unregister_inbox: bool = True,
) -> bool:
    """Delete terminal and kill its tmux window.

    A bare legacy delete can never tear down a v2 terminal row or a
    generation with v2 companion binding state.  Ordinary conductor
    retirement is allowed only when it supplies the exact generation and
    session, preserving the existing compare-and-delete protection against a
    reused terminal id.  The stronger destructive endpoint may also call this
    function after performing its additional heartbeat/fence checks."""
    from cli_agent_orchestrator.services import callback_recovery

    try:
        if callback_recovery.terminal_has_open_recovery(terminal_id, expected_generation):
            raise TerminalGenerationMismatchError(
                f"terminal {terminal_id} has an open callback-recovery "
                "operation; deletion is held until callback completion or "
                "a terminal refusal/manual disposition"
            )
        # P1-1 (final conformance §20.2f): expected_session without the exact
        # generation NEVER degrades to ID-only destruction — a session name is
        # not an incarnation identity.
        if expected_generation is None and expected_session is not None:
            raise TerminalGenerationMismatchError(
                f"terminal {terminal_id} cleanup supplied a session identity "
                "without the exact generation; refusing ID-only destruction"
            )
        if not via_destructive_endpoint:
            v2_row = get_terminal_metadata_v2(terminal_id)
            if v2_row is not None and (expected_generation is None or expected_session is None):
                raise DestructiveEndpointRequiredError(
                    f"terminal {terminal_id} is a v2 managed row (generation "
                    f"{v2_row.get('generation')!r}); bare deletion is refused "
                    "with zero mutation — supply its exact generation and "
                    "session for compare-and-delete retirement"
                )
        # Managed cleanup claims the exact DB incarnation before any external
        # destructive action. If the id now names a replacement generation,
        # preserve every resource and report the mismatch.  v2 managed
        # terminals live in the isolated v2 surface; the legacy table is
        # consulted first, then the v2 table.
        metadata = get_terminal_metadata(terminal_id)
        v2_record = False
        if metadata is None and expected_generation is not None:
            metadata = get_terminal_metadata_v2(terminal_id)
            v2_record = metadata is not None
        terminal_record_absent = False
        if expected_generation is not None:
            if metadata is None:
                if not expected_session:
                    raise TerminalGenerationMismatchError(
                        "managed cleanup of a missing terminal record requires "
                        "the reserved session identity"
                    )
                terminal_record_absent = True
            elif metadata.get("generation") != expected_generation:
                raise TerminalGenerationMismatchError(
                    f"terminal {terminal_id} generation mismatch; expected "
                    f"{expected_generation!r}"
                )
            expected_window = None
            if metadata is not None:
                if metadata.get("tmux_session") != expected_session:
                    raise TerminalGenerationMismatchError(
                        f"terminal {terminal_id} session identity mismatch; expected "
                        f"{expected_session!r}"
                    )
                if not backend_already_closed:
                    expected_window = managed_window_name(terminal_id, expected_generation)
                    if metadata.get("tmux_window") != expected_window:
                        raise TerminalGenerationMismatchError(
                            f"terminal {terminal_id} route identity mismatch; expected "
                            f"{expected_session!r}:{expected_window!r}"
                        )
            if terminal_record_absent and not backend_already_closed:
                expected_window = managed_window_name(terminal_id, expected_generation)
                # The window name embeds the immutable generation, so recovery
                # can finish a crash that occurred before the terminal row was
                # persisted or after another cleanup removed it. A different
                # generation has a different window identity.
                get_backend().kill_window(expected_session, expected_window)
                if get_backend().window_exists(expected_session, expected_window):
                    raise RuntimeError(
                        f"managed terminal window survived cleanup: "
                        f"{expected_session}:{expected_window}"
                    )

        def _recheck_teardown_claim() -> None:
            """Generation-owned teardown claim (P1-1, final conformance
            §20.2f): immediately before EVERY destructive subsystem step,
            immutably recheck that the live row still names the exact claimed
            generation. A replacement swapped in mid-teardown (or a row
            appearing on the row-absent recovery path — only a replacement
            can appear there) stops the teardown with zero further
            destructive action."""
            if expected_generation is None:
                return
            current = get_terminal_metadata(terminal_id)
            if current is None and v2_record:
                current = get_terminal_metadata_v2(terminal_id)
            if terminal_record_absent:
                if current is not None:
                    raise TerminalGenerationMismatchError(
                        f"terminal {terminal_id} row appeared during "
                        "row-absent managed cleanup; preserving the "
                        "replacement incarnation"
                    )
                return
            if current is None or current.get("generation") != expected_generation:
                raise TerminalGenerationMismatchError(
                    f"terminal {terminal_id} changed generation during cleanup"
                )

        # Unregister from herdr inbox service
        _recheck_teardown_claim()
        svc = get_herdr_inbox_service() if unregister_inbox else None
        if svc:
            try:
                svc.unregister_terminal(
                    terminal_id,
                    expected_pane_id=(metadata or {}).get("pane_id"),
                )
            except Exception as e:
                logger.warning(f"Failed to unregister terminal {terminal_id} from herdr inbox: {e}")

        if metadata:
            _recheck_teardown_claim()
            if not backend_already_closed:
                # Snapshot scrollback + metadata before killing (for debugging/restore)
                try:
                    # Capture plain text full scrollback (no -e, no line cap)
                    scrollback = get_backend().get_history(
                        metadata["tmux_session"],
                        metadata["tmux_window"],
                        strip_escapes=True,
                        full_history=True,
                    )
                    scrollback_path = TERMINAL_LOG_DIR / f"{terminal_id}.scrollback"
                    scrollback_path.write_text(scrollback, encoding="utf-8")

                    import json as _json

                    snapshot = {
                        "terminal_id": terminal_id,
                        "session_name": metadata["tmux_session"],
                        "window_name": metadata["tmux_window"],
                        "agent_profile": metadata.get("agent_profile"),
                        "provider": metadata["provider"],
                        "working_directory": get_backend().get_pane_working_directory(
                            metadata["tmux_session"], metadata["tmux_window"]
                        ),
                        "allowed_tools": metadata.get("allowed_tools"),
                        "caller_id": metadata.get("caller_id"),
                    }
                    snapshot_path = TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json"
                    snapshot_path.write_text(_json.dumps(snapshot, indent=2), encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Failed to snapshot terminal {terminal_id}: {e}")

                # Stop pipe-pane logging
                _recheck_teardown_claim()
                try:
                    get_backend().stop_pipe_pane(metadata["tmux_session"], metadata["tmux_window"])
                except Exception as e:
                    logger.warning(f"Failed to stop pipe-pane for {terminal_id}: {e}")

            # Stop FIFO reader and cleanup FIFO file. Must run BEFORE kill_window
            # so the reader thread (which reopens the FIFO on EOF) unblocks and
            # joins before the pane disappears.
            _recheck_teardown_claim()
            try:
                fifo_manager.stop_reader(terminal_id)
            except Exception as e:
                logger.warning(f"Failed to stop FIFO reader for {terminal_id}: {e}")

            # Clear state detector buffers for this terminal
            _recheck_teardown_claim()
            try:
                status_monitor.clear_terminal(terminal_id)
            except Exception as e:
                logger.warning(f"Failed to clear state detector for {terminal_id}: {e}")

            if not backend_already_closed:
                # Kill the tmux window (this terminates the agent process)
                _recheck_teardown_claim()
                if expected_generation is None:
                    try:
                        get_backend().kill_window(metadata["tmux_session"], metadata["tmux_window"])
                    except Exception as e:
                        logger.warning(f"Failed to kill tmux window for {terminal_id}: {e}")
                else:
                    get_backend().kill_window(metadata["tmux_session"], metadata["tmux_window"])
                    if get_backend().window_exists(
                        metadata["tmux_session"], metadata["tmux_window"]
                    ):
                        raise RuntimeError(
                            f"managed terminal window survived cleanup: "
                            f"{metadata['tmux_session']}:{metadata['tmux_window']}"
                        )

        # Cleanup provider state and database record
        _recheck_teardown_claim()
        provider_manager.cleanup_provider(terminal_id)
        # Close this terminal's claim on its provider-native session.  The
        # claim is exclusive and keyed by the provider's own session id, so
        # one that outlives its terminal makes that session permanently
        # unresumable — a later attach is refused by an owner that no longer
        # exists.  This runs after the window kill because that is the first
        # moment the owning process can be observed absent, and before the
        # row delete so it is still inside the generation claim.
        #
        # Never raised.  The window is already killed and the row is about to
        # go; failing here would abort a teardown part-way and leave worse
        # state than the claim it was trying to resolve.
        _recheck_teardown_claim()
        try:
            from cli_agent_orchestrator.services import native_attachment_recovery

            for outcome in native_attachment_recovery.release_owned_by_terminal(
                terminal_id, generation=expected_generation
            ):
                if outcome["action"] == "released":
                    logger.info(
                        "Released the %s session claim held by terminal %s",
                        outcome["provider"],
                        terminal_id,
                    )
                else:
                    logger.warning(
                        "Terminal %s was torn down while still holding %s session %s (%s): %s",
                        terminal_id,
                        outcome["provider"],
                        outcome["native_session_id"],
                        outcome["reason"],
                        outcome.get("detail", ""),
                    )
        except Exception as e:
            logger.warning(f"Failed to resolve native session claims for {terminal_id}: {e}")
        # M3-A: retire the roster incarnation so the physical history is
        # truthful while the stable agent survives teardown.  Best-effort
        # and never raised: missing roster records or an unreadable store
        # must not block cleanup (Stop is best-effort for every roster).
        _recheck_teardown_claim()
        _roster_retire_incarnation_best_effort(terminal_id, expected_generation)
        _recheck_teardown_claim()
        with _memory_injected_lock:
            _memory_injected_terminals.discard(terminal_id)
        # Drop any per-curator dispatch lock so the registry doesn't grow
        # forever as memory_manager terminals come and go.
        from cli_agent_orchestrator.services.memory_service import _curator_locks

        _curator_locks.pop(terminal_id, None)
        if expected_generation is not None:
            if terminal_record_absent:
                deleted = True
                # The row is gone (a crash after teardown removed it, or a
                # prior cleanup) but the generation's resource-registry
                # entries can outlive the row. Deregister the exact
                # generation's entries with the reserved session too, so a
                # cleaned row-absent generation leaves no live registry
                # entries -- the same effect the v2-row branch applies after
                # its delete. This runs after the absence/replacement
                # rechecks, and enumeration is keyed by exact terminal +
                # generation, so a replacement incarnation is untouched.
                _deregister_v2_terminal_resources(
                    terminal_id,
                    expected_generation,
                    session_name=expected_session,
                )
            elif v2_record:
                deleted = db_delete_terminal_v2_if_generation(terminal_id, expected_generation)
                if not deleted:
                    current = get_terminal_metadata_v2(terminal_id)
                    if current is not None:
                        raise TerminalGenerationMismatchError(
                            f"terminal {terminal_id} changed generation during cleanup"
                        )
                    deleted = True
                if deleted:
                    _deregister_v2_terminal_resources(
                        terminal_id,
                        expected_generation,
                        session_name=(metadata or {}).get("tmux_session") or expected_session,
                    )
            else:
                deleted = db_delete_terminal_if_generation(terminal_id, expected_generation)
                if not deleted:
                    current = get_terminal_metadata(terminal_id)
                    if current is not None:
                        raise TerminalGenerationMismatchError(
                            f"terminal {terminal_id} changed generation during cleanup"
                        )
                    deleted = True
        else:
            deleted = db_delete_terminal(terminal_id)
        logger.info(f"Deleted terminal: {terminal_id}")
        if deleted and metadata:
            dispatch_plugin_event(
                registry,
                "post_kill_terminal",
                PostKillTerminalEvent(
                    session_id=metadata["tmux_session"],
                    terminal_id=terminal_id,
                    generation=metadata.get("generation"),
                    agent_name=metadata.get("agent_profile"),
                ),
            )
        return deleted

    except Exception as e:
        logger.error(f"Failed to delete terminal {terminal_id}: {e}")
        raise


def reattach_existing_output_pipelines() -> dict:
    """Re-attach the FIFO -> EventBus output pipeline for terminals that already
    exist in the database (server restart recovery).

    FIFO readers and ``pipe-pane`` are normally wired only in create_terminal, so
    a restarted server leaves pre-existing terminals with no output feed: their
    status sticks at UNKNOWN forever and idle-gated inbox delivery to them never
    fires. For each DB terminal whose tmux window still exists, recreate the
    reader and stop/start pipe-pane (stop-then-start, not a bare toggle — a
    stale pane still reports pane_pipe=1). Terminals whose window is gone are
    skipped (stale rows; deletion is left to normal lifecycle paths).

    Every row is proven through the same identity boundary a control write
    uses, before anything is piped or typed. This runs at server startup,
    against rows written by a *previous* process, which is precisely when
    the recorded names are least trustworthy: a session and window torn
    down while the server was dead and recreated under the same names
    resolve perfectly, so the unverified form both nudged Enter into a
    stranger's pane and piped that stranger's output into this row's FIFO
    — mislabelled as this terminal's, and read as its provider status
    from then on. A row whose proof fails is skipped with zero bytes
    written; a row with no recorded identity keeps resolving by name,
    which is the documented boundary of the identity work rather than an
    exemption.
    """
    from cli_agent_orchestrator.clients.database import list_all_terminals

    backend = get_backend()
    reattached, skipped = [], []
    if backend.supports_event_inbox():
        return {"reattached": reattached, "skipped": skipped}
    for row in list_all_terminals():
        terminal_id = row["id"]
        session_name, window_name = row["tmux_session"], row["tmux_window"]
        try:
            # The listing is a display-shaped subset with no identity
            # fields; verifying against it would grade every row absent
            # and check nothing at all.
            metadata = get_terminal_metadata(terminal_id) or row
            target = verified_pane_target(
                terminal_id, metadata, operation="output pipeline re-attach"
            )
        except TerminalIdentityMismatchError as exc:
            logger.info("Refused to re-attach the output pipeline for %s: %s", terminal_id, exc)
            skipped.append(terminal_id)
            continue
        pane_id = None
        if target is not None:
            # The names the verified pane answers to *now*. ``pipe-pane``
            # and history addressing are name-shaped, so using the
            # recorded names here would re-open the same hole the proof
            # just closed.
            pane_id = target.pane_id
            session_name, window_name = target.session_name, target.window_name
        try:
            backend.get_history(session_name, window_name, tail_lines=1)
        except Exception:
            skipped.append(terminal_id)
            continue
        try:
            fifo_path = FIFO_DIR / f"{terminal_id}.fifo"

            def _probe_pane(s=session_name, w=window_name) -> str:
                return backend.get_history(s, w, tail_lines=PIPE_LIVENESS_TAIL_LINES)

            def _rearm_pipe(s=session_name, w=window_name, p=str(fifo_path)) -> None:
                backend.stop_pipe_pane(s, w)
                backend.pipe_pane(s, w, p)

            fifo_manager.create_reader(terminal_id, pane_probe=_probe_pane, rearm=_rearm_pipe)
            backend.stop_pipe_pane(session_name, window_name)
            backend.pipe_pane(session_name, window_name, str(fifo_path))
            # Nudge a fresh prompt line through the new pipe so StatusMonitor
            # leaves UNKNOWN promptly (same rationale as the create path).
            # Enter submits whatever a composer is holding, so it goes to
            # the proven pane id when there is one.
            backend.send_special_key(session_name, window_name, "Enter", pane_id=pane_id)
            reattached.append(terminal_id)
        except Exception:
            logger.warning("could not re-attach output pipeline for %s", terminal_id, exc_info=True)
            skipped.append(terminal_id)
    logger.info(
        "Re-attached output pipelines: %d re-attached, %d skipped", len(reattached), len(skipped)
    )
    return {"reattached": reattached, "skipped": skipped}


def reconcile_session_env() -> dict:
    """Drop persisted session-env rows whose tmux session no longer exists.

    Restart recovery companion to ``reattach_existing_output_pipelines``:
    sessions torn down while the server was dead leave ``session_env`` rows
    behind, and a later same-named session must not inherit stale forwarded
    env. Live-session rows are retained so windows created post-restart still
    receive the persisted env (issue #248 durability). Rows are recorded as
    removed only after their durable deletion is confirmed; failed deletions
    are retained and reported under ``failed`` (cond-0050).

    A **deliberately stopped** session is exempt. Its tmux session is gone
    by definition — that is what stopping means — so the existence probe
    alone would delete the forwarded env of every hibernated session at the
    next boot. The failure is silent, delayed, and surfaces much later as
    "resume worked but every worker is on the wrong binary", which is close
    to undiagnosable from the symptom.
    """
    from cli_agent_orchestrator.services import session_env, session_lifecycle

    backend_exists = get_backend().session_exists

    def _retain(session_name: str) -> bool:
        if backend_exists(session_name):
            return True
        record = session_lifecycle.describe(session_name)
        # An unreadable lifecycle store retains rather than deletes. Losing
        # env is irreversible; keeping a stale row is not, and the next
        # reconcile with a readable store cleans it up.
        return record["lifecycle"] == session_lifecycle.STOPPED or bool(record.get("unreadable"))

    return session_env.reconcile_session_env(_retain)
