"""Keep finished child agents inspectable instead of tearing them down (#1225 phase 1).

While ``terminal.keep_finished.enabled`` is on, a finished CHILD (a terminal whose
row records the agent that created it, ``caller_id``) is left running and idle
instead of being exited and deleted, so someone can open its pane afterwards and see
what it did and which tools it called. Two paths ask for that, both agent-side:

* a ``handoff`` that succeeded (the run-step route installs :func:`keep_finished`
  as the success-teardown hook), and
* an agent's ``delete_terminal`` call (the MCP tool sends ``keep_if_enabled``).

Everything else is unchanged: operator deletes, session teardown, workflows, flows,
remote workers, and every failure path.

A kept child is still torn down later, the normal way (:func:`teardown_kept`), by the
reaper (:func:`reap`) once it has been idle past ``idle_window_minutes``, or to make
room under ``max_per_session``. A child CAO sees working is never reaped.

The state is one content-free row per kept terminal in ``kept_terminals``; see
``clients.database.KeptTerminalModel``.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.settings_service import (
    KEEP_FINISHED_DEFAULTS,
    KEEP_FINISHED_SETTING,
    KeepFinishedSettings,
    KeepFinishedSettingsError,
    get_keep_finished_settings,
)
from cli_agent_orchestrator.services.status_monitor import status_monitor

logger = logging.getLogger(__name__)

# How often the reaper looks at kept children. The window is in minutes, so a
# minute's granularity is the same order as the setting itself.
REAPER_INTERVAL_SECONDS = 60

# A child in either state is working, or waiting on a person, and is never reaped
# or evicted. Read raw from status_monitor, like flow_service._is_terminal_busy.
BUSY_STATUSES = frozenset({TerminalStatus.PROCESSING, TerminalStatus.WAITING_USER_ANSWER})

KEEP_OFF = KeepFinishedSettings(**KEEP_FINISHED_DEFAULTS)

# Serializes keep decisions, so two children finishing at once in one session can
# never both take the last slot. One cao-server owns a CAO home (server_lock), so a
# process lock is enough. Teardowns run outside it.
_keep_lock = threading.Lock()


@dataclass(frozen=True)
class KeepOutcome:
    """What a keep request did. ``note`` is the sentence to tell the caller, if any."""

    kept: bool
    kept_until: Optional[datetime] = None
    note: Optional[str] = None
    evicted: Tuple[str, ...] = field(default_factory=tuple)


# The answer whenever keeping does not apply (setting off, not a child, no row):
# the caller does exactly what it did before #1225, and says nothing new.
NOT_APPLICABLE = KeepOutcome(kept=False)


def current_settings() -> KeepFinishedSettings:
    """The keep setting, or OFF when it is invalid (a typo can only give today's behaviour)."""
    try:
        return get_keep_finished_settings()
    except KeepFinishedSettingsError as exc:
        logger.warning("%s; keeping finished agents is OFF until it is fixed", exc)
        return KEEP_OFF


def log_startup_posture() -> None:
    """Log the keep setting once at server start when it is on or invalid; silent when off."""
    try:
        settings = get_keep_finished_settings()
    except KeepFinishedSettingsError as exc:
        logger.error(
            "%s is invalid: %s. Keeping finished agents stays OFF until you fix it.",
            KEEP_FINISHED_SETTING,
            exc,
        )
        return
    if settings.enabled:
        logger.info(
            "keeping finished agents: ON (idle window %d min, max %d per session, on_cap=%s)",
            settings.idle_window_minutes,
            settings.max_per_session,
            settings.on_cap,
        )


def _status(terminal_id: str) -> Optional[TerminalStatus]:
    try:
        return status_monitor.get_status(terminal_id)
    except Exception as exc:  # noqa: BLE001 — an unreadable status is "can't tell"
        logger.warning("keep: could not read the status of terminal %s: %s", terminal_id, exc)
        return None


def _is_idle(terminal_id: str) -> bool:
    """True only when CAO can read the status and it is not busy."""
    status = _status(terminal_id)
    return status is not None and status not in BUSY_STATUSES


def _runtime_is_live(metadata: Dict[str, Any]) -> bool:
    """True only on positive proof that this terminal's own pane is still there.

    A keep is only worth anything for a live agent someone can look at, and a row
    whose runtime is gone is exactly the one a delete must not skip: a failed create
    whose rollback retained the row for a deferred provider cleanup (the 424 replay
    of #1221 names it so it can be deleted), or a deferred-init failure tombstone.
    Identity is proved by terminal id (``cleanup_terminal_exact`` with
    ``close=False``, as the retention sweep does), never by the reusable
    session/window name. Anything short of STILL_PRESENT -- ABSENT, UNKNOWN, an
    error -- reads as "not live", so such a row is deleted as before.
    """
    if (
        metadata.get("deferred_init_failure")
        or metadata.get("deferred_init_external_owner")
        or metadata.get("deferred_init_runtime_reclaimed")
    ):
        return False
    try:
        result = backend_registry.get_backend().cleanup_terminal_exact(
            str(metadata["id"]),
            metadata.get("tmux_session"),
            metadata.get("tmux_window"),
            close=False,
        )
    except Exception as exc:  # noqa: BLE001 — unproven liveness is "not live" here
        logger.warning("keep: could not prove terminal %s is live: %s", metadata.get("id"), exc)
        return False
    return getattr(result, "outcome", None) == TerminalCleanupOutcome.STILL_PRESENT


def keep_finished(
    terminal_id: str, *, reason: str, registry: Optional[PluginRegistry] = None
) -> KeepOutcome:
    """Keep ``terminal_id`` instead of tearing it down, if the setting says so.

    ``reason`` is ``"handoff"`` (a handoff succeeded) or ``"delete_terminal"`` (an
    agent asked to delete it). Returns :data:`NOT_APPLICABLE` when the setting is
    off or invalid, the row is gone, the terminal is not a child, or its runtime is
    not provably live (see :func:`_runtime_is_live`); the caller then tears it down
    exactly as before. Otherwise the outcome says whether it was kept, until when,
    and what happened to any child evicted to stay within the cap.

    A note never claims a teardown: whether one happened is the caller's to report
    (#1203's ``torn_down``, or the delete's own answer), and an eviction is reported
    as what it really did.

    Never tears down ``terminal_id`` itself: when it is not kept the CALLER does
    its usual teardown, so the not-kept path is the pre-#1225 path. Evicted children
    are torn down here, after the decision, outside the lock.
    """
    settings = current_settings()
    if not settings.enabled:
        return NOT_APPLICABLE
    metadata = terminal_service.get_terminal_metadata(terminal_id)
    if not metadata or not metadata.get("caller_id") or not _runtime_is_live(metadata):
        return NOT_APPLICABLE
    existing = database.get_kept_terminal(terminal_id)
    if existing is not None:
        return KeepOutcome(kept=True, kept_until=existing["kept_until"], note=KEPT_NOTE)
    if reason == "delete_terminal" and not _is_idle(terminal_id):
        # An agent deleting a child that is still working is cancelling it. Keeping
        # it would leave it running and spending tokens, so it is deleted as before.
        return KeepOutcome(kept=False, note="Not kept: it was still working.")

    session_name = str(metadata["tmux_session"])
    cap = settings.max_per_session
    with _keep_lock:
        kept = database.list_kept_terminals(session_name)
        evict: List[str] = []
        if len(kept) >= cap:
            if settings.on_cap == "dont_keep":
                return KeepOutcome(kept=False, note=_cap_note(len(kept), cap, "dont_keep"))
            needed = len(kept) - cap + 1
            idle = [row["terminal_id"] for row in kept if _is_idle(row["terminal_id"])]
            if len(idle) < needed:
                return KeepOutcome(
                    kept=False,
                    note=(
                        f"Not kept: this session already keeps {len(kept)} finished agents "
                        f"({KEEP_FINISHED_SETTING}.max_per_session={cap}) and none of them "
                        "is idle."
                    ),
                )
            evict = idle[:needed]
        kept_until = database._utcnow() + timedelta(minutes=settings.idle_window_minutes)
        if not database.mark_terminal_kept(
            terminal_id, session_name, kept_until=kept_until, reason=reason, evict=evict
        ):
            return NOT_APPLICABLE

    note = KEPT_NOTE
    for victim in evict:
        note += " " + _eviction_sentence(victim, _evict(victim, registry), cap)
    return KeepOutcome(kept=True, kept_until=kept_until, note=note, evicted=tuple(evict))


# The rest of a kept child's report; callers put "kept until <time>" in front of it.
KEPT_NOTE = (
    f"Keeping finished agents is on for this machine ({KEEP_FINISHED_SETTING}): CAO tears "
    "it down once it has been idle that long, and an operator delete still removes it at once."
)


def _cap_note(count: int, cap: int, on_cap: str) -> str:
    return (
        f"Not kept: this session already keeps {count} finished agents "
        f"({KEEP_FINISHED_SETTING}.max_per_session={cap}, on_cap={on_cap})."
    )


def _eviction_sentence(terminal_id: str, outcome: str, cap: int) -> str:
    """Say what really happened to an evicted child: never a teardown that didn't happen."""
    lead = (
        f"To stay within {KEEP_FINISHED_SETTING}.max_per_session={cap}, the oldest kept "
        f"agent {terminal_id} is no longer kept"
    )
    if outcome == "torn_down":
        return f"{lead} and was torn down."
    if outcome == "working":
        return f"{lead}; it started working again, so it was left running."
    return f"{lead}, but its teardown did not complete, so it was not torn down."


def _evict(terminal_id: str, registry: Optional[PluginRegistry]) -> str:
    """Tear down a child whose keep was given up. ``torn_down``, ``working`` or ``not_torn_down``.

    Never raises. Its keep mark is already gone, so the cap holds whatever happens here.
    """
    if not _is_idle(terminal_id):
        # It started working between the decision and now: leave the process alone
        # rather than kill live work.
        logger.warning(
            "keep: terminal %s started working before its eviction; left running, no longer kept",
            terminal_id,
        )
        return "working"
    try:
        if teardown_kept(terminal_id, end_reason="kept_evicted", registry=registry):
            return "torn_down"
    except Exception as exc:  # noqa: BLE001 — an eviction must not fail the new keep
        logger.warning("keep: failed to tear down evicted terminal %s: %s", terminal_id, exc)
    return "not_torn_down"


def teardown_kept(
    terminal_id: str, *, end_reason: str, registry: Optional[PluginRegistry] = None
) -> bool:
    """Tear a kept child down the normal way, snapshot FIRST. True when its row is gone.

    1. scrollback + snapshot.json (0600), while the CLI still shows everything;
    2. the provider's exit command (best-effort);
    3. the runtime: window, worktree, provider cleanup (a Kiro helper's private home
       goes here, and only here), the ephemeral name;
    4. the row, with the launch record's ``end_reason``.

    ``terminal_service.delete_terminal`` is NOT used: it would take a second
    snapshot after the exit command and overwrite the first. A deferred runtime
    cleanup (Grok) keeps both rows, so the next reaper pass retries.
    """
    metadata = terminal_service.capture_terminal_snapshot(terminal_id)
    if metadata is None:
        database.unmark_terminal_kept(terminal_id)
        return False
    try:
        terminal_service.exit_terminal_cli(terminal_id)
    except Exception as exc:  # noqa: BLE001 — graceful exit is best-effort, as in handoff
        logger.warning("keep: failed to send the exit command to %s: %s", terminal_id, exc)
    if not terminal_service.dismantle_terminal_runtime(terminal_id, metadata):
        logger.warning("keep: teardown of %s deferred; retrying on the next pass", terminal_id)
        return False
    return terminal_service.delete_terminal_row(
        terminal_id, metadata, registry=registry, end_reason=end_reason
    )


def reap(registry: Optional[PluginRegistry] = None) -> Dict[str, int]:
    """One reaper pass over every kept child. Never raises; returns counts by outcome.

    Runs whatever the setting says now, so turning it off never strands a kept child:
    those still expire on schedule. Per child: busy -> its window starts again from
    now; idle and past its window -> torn down (``kept_expired``); status unreadable
    -> left for the next pass. Then, while the setting is on, a session holding more
    than ``max_per_session`` (the operator lowered it) loses its oldest idle ones.
    """
    counts: Counter[str] = Counter()
    try:
        counts["orphans"] += database.delete_orphan_kept_rows()
        settings = current_settings()
        window = timedelta(minutes=settings.idle_window_minutes)
        now = database._utcnow()
        for row in database.list_kept_terminals():
            terminal_id = row["terminal_id"]
            try:
                status = _status(terminal_id)
                if status is None:
                    counts["unknown"] += 1
                elif status in BUSY_STATUSES:
                    database.extend_kept_until(terminal_id, now + window)
                    counts["busy"] += 1
                elif row["kept_until"] <= now:
                    if teardown_kept(terminal_id, end_reason="kept_expired", registry=registry):
                        counts["expired"] += 1
                    else:
                        counts["deferred"] += 1
            except Exception as exc:  # noqa: BLE001 — one child must not stop the pass
                logger.warning("keep reaper: terminal %s: %s", terminal_id, exc)
                counts["errors"] += 1
        if settings.enabled:
            _trim_to_cap(settings.max_per_session, registry, counts)
    except Exception as exc:  # noqa: BLE001 — a maintenance pass never raises
        logger.warning("keep reaper pass failed: %s", exc)
        counts["errors"] += 1
    result = {key: value for key, value in counts.items() if value}
    if result:
        logger.info("keep reaper: %s", result)
    return result


def _trim_to_cap(cap: int, registry: Optional[PluginRegistry], counts: Counter[str]) -> None:
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for row in database.list_kept_terminals():
        by_session.setdefault(row["session_name"], []).append(row)
    for rows in by_session.values():
        excess = len(rows) - cap
        if excess <= 0:
            continue
        for row in rows:
            if excess <= 0:
                break
            terminal_id = row["terminal_id"]
            if not _is_idle(terminal_id):
                continue
            with _keep_lock:
                database.unmark_terminal_kept(terminal_id)
            excess -= 1
            _evict(terminal_id, registry)
            counts["evicted"] += 1
