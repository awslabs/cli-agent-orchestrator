"""One read projection over both terminal stores, for every human view.

The CLI and the dashboard used to answer "what terminals are there?"
from different queries, and disagreed in ways that were not cosmetic. A
window that had been deleted still had a row, nothing ever demoted it,
and status was derived live — so its card reported provider status
``Unknown`` forever, indistinguishable from a healthy worker whose state
had not been detected yet. ``cao session status`` picked ``terminals[0]``
from the raw list, so with several stale rows it reliably named a dead
one. And managed v2 terminals appeared in neither view, because they live
in their own table by design.

This module is the single answer to that question, and both views are
required to render it identically. What it adds is a *read*: the v2
isolation invariant is a write/consume boundary — old-binary machine
paths (v1 query, list, watchdog, cleanup) must keep zero v2 visibility —
not a rule that humans may not see managed workers. Every projected row
is explicitly labelled with its ``protocol_vintage`` so the two vintages
are distinguishable rather than blended.

Liveness here is observation, never inference. One enumeration of the
tmux server answers every row at the same instant; a row whose recorded
pane is absent from a server that *did* answer is dead; a row whose pane
resolves to a different incarnation is superseded; and a row that could
not be observed at all is ``unknown-liveness`` — not live, not reaped,
not attachable. Nothing here writes to a pane, spends a provider turn, or
kills anything: a demotion is something the system noticed, not something
it did.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    get_terminal_metadata,
    get_terminal_metadata_v2,
    list_terminals_by_session,
    report_terminal_missing_from_every_store,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service

logger = logging.getLogger(__name__)

LIFECYCLE_LIVE = "live"
LIFECYCLE_SUPERSEDED = "superseded"
LIFECYCLE_DEAD = "dead"
LIFECYCLE_UNKNOWN_LIVENESS = "unknown-liveness"

#: The lifecycle states that make a terminal a lawful target. Exactly one,
#: named here so that "is this attachable?" is a question about this
#: constant rather than about every call site's idea of liveness.
ATTACHABLE_LIFECYCLE_STATES = frozenset({LIFECYCLE_LIVE})

#: Rendered by both views, in this order, for every terminal. Held as data
#: rather than as two hand-written dict literals: the invariant is that
#: the CLI and the dashboard show the *same* fields, and two literals drift
#: the moment someone adds a field to one of them.
PROJECTION_FIELDS = (
    "terminal_id",
    "name",
    "session_name",
    "provider",
    "agent_profile",
    "caller_id",
    "generation",
    "callback_target_generation",
    "protocol_vintage",
    "server_socket_path",
    "session_id",
    "window_id",
    "pane_id",
    "pane_pid",
    "native_session_id",
    "lifecycle_state",
    "lifecycle_reason",
    "superseded_by_terminal_id",
    "superseded_by_generation",
    "status",
    "fifo_monitored",
    "last_active",
)


def _observed_panes() -> Optional[Dict[str, Dict[str, str]]]:
    """One enumeration for a whole projection, or None if unreadable."""
    backend = get_backend()
    if getattr(backend, "supports_pane_identity", False) is not True:
        return None
    try:
        return backend.observe_pane_identities()
    except Exception as exc:  # pragma: no cover - an unreadable server is a state
        logger.warning("Could not enumerate tmux panes for the terminal projection: %s", exc)
        return None


def _v2_row_identity(row: Dict[str, Any]) -> Dict[str, Any]:
    """Read a v2 row's identity through its prefixed column names.

    The v2 store prefixes these columns because the vintage receipt
    records bare column names and requires them unique across the whole v2
    surface. The prefix is storage detail and must not reach a view, so it
    is stripped exactly here.
    """
    return {
        "session_id": row.get("v2_session_id"),
        "pane_pid": row.get("v2_pane_pid"),
        "native_session_id": row.get("v2_native_session_id"),
        "lifecycle_state": row.get("v2_lifecycle_state"),
        "lifecycle_reason": row.get("v2_lifecycle_reason"),
        "superseded_by_terminal_id": row.get("v2_superseded_by_terminal_id"),
        "superseded_by_generation": row.get("v2_superseded_by_generation"),
    }


def observed_lifecycle(
    row: Dict[str, Any],
    panes: Optional[Dict[str, Dict[str, str]]],
) -> tuple[str, Optional[str]]:
    """Classify one row against a single enumeration of live panes.

    Returns ``(state, reason)``. The row's stored lifecycle is *not*
    consulted: a stored state is what was true when it was written, and a
    view that repeated it would keep showing a demoted row as live for as
    long as nobody happened to write to it.
    """
    completeness = terminal_service.identity_completeness(row)
    if completeness == terminal_service.IDENTITY_ABSENT:
        # Nothing was ever recorded, so there is nothing to observe
        # against. Such a row is not promoted to live on the strength of
        # there being no evidence against it.
        return LIFECYCLE_UNKNOWN_LIVENESS, "no recorded identity"
    if completeness == terminal_service.IDENTITY_PARTIAL:
        # Every row the previously deployed build created lands here on the
        # first read after an upgrade, because it wrote three of the five
        # fields. Completing it from an observation of its own pane is what
        # keeps installing this build from grading the whole existing fleet
        # unknown — and the caller mutates ``row`` in place so the rest of
        # this projection sees the completed identity.
        upgraded = terminal_service.upgrade_observed_identity(row["id"], row)
        if upgraded is not None:
            row.update(upgraded)
            completeness = terminal_service.identity_completeness(row)
    if completeness == terminal_service.IDENTITY_PARTIAL:
        # The same rule the write and attach paths apply: a partial
        # identity is not checked on the fields it happens to have. A view
        # that rendered such a row as live would be the one place the
        # system still told an operator it was safe to use.
        missing = ", ".join(
            field for field in terminal_service.IDENTITY_FIELDS if not row.get(field)
        )
        return LIFECYCLE_UNKNOWN_LIVENESS, f"identity incomplete: missing {missing}"
    if panes is None:
        return LIFECYCLE_UNKNOWN_LIVENESS, "tmux server could not be read"

    recorded_pane = row["pane_id"]
    observed = panes.get(recorded_pane)
    if observed is None:
        return LIFECYCLE_DEAD, f"pane {recorded_pane} is absent from its server"
    if observed.get("dead") == "1":
        return LIFECYCLE_DEAD, f"pane {recorded_pane} is dead"

    differences = [
        label
        for field, label in (
            ("server_socket_path", "tmux server"),
            ("window_id", "window"),
            ("session_id", "session"),
            ("pane_pid", "pane process"),
        )
        if str(observed.get(field)) != str(row[field])
    ]
    if differences:
        return LIFECYCLE_SUPERSEDED, f"{', '.join(differences)} differs from what was registered"
    return LIFECYCLE_LIVE, None


def _provider_status(terminal_id: str) -> Optional[str]:
    """The provider's own status, which is meaningful only for a live pane."""
    try:
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        return status_monitor.get_status(terminal_id).value
    except Exception as exc:  # pragma: no cover - detection failure is not liveness
        logger.debug("Provider status unavailable for %s: %s", terminal_id, exc)
        return None


def _is_native_tui(terminal_id: str) -> bool:
    """Whether this terminal's pane runs a provider's own full-screen TUI.

    Read from the reservation's ``execution_mode``, which is the fact the
    launch decided and stored, rather than inferred from the presence of a
    FIFO or the shape of the argv. Both of those are consequences of the
    mode and would invert the dependency: the ACP bridge is also launched
    by argv and does have a FIFO, so either inference would call it native.
    """
    try:
        from cli_agent_orchestrator.clients.database import (
            ManagedLaunchV2ReservationModel,
            SessionLocal,
        )
        from cli_agent_orchestrator.services import execution_mode as em

        with SessionLocal() as db:
            row = (
                db.query(ManagedLaunchV2ReservationModel)
                .filter(ManagedLaunchV2ReservationModel.terminal_id == terminal_id)
                .first()
            )
            return row is not None and row.execution_mode == em.NATIVE_TUI
    except Exception as exc:  # pragma: no cover - an absent v2 surface is not native
        logger.debug("Execution mode unavailable for %s: %s", terminal_id, exc)
        return False


def project_row(
    row: Dict[str, Any],
    panes: Optional[Dict[str, Dict[str, str]]],
    *,
    vintage: str,
) -> Dict[str, Any]:
    """One terminal, as both human views must render it."""
    if vintage == "v2":
        row = {**row, **_v2_row_identity(row)}
    state, reason = observed_lifecycle(row, panes)
    native_tui = vintage == "v2" and _is_native_tui(row["id"])
    if state == LIFECYCLE_LIVE and native_tui:
        # A native TUI has no FIFO monitor, so no provider will ever
        # classify it. Reporting ``unknown`` here promised a detection that
        # was never coming and left every native worker looking pending
        # forever; the truthful answer names the absence instead. The rest
        # of the projection is unchanged -- identity and lifecycle are read
        # from the row exactly as for any other terminal, because those are
        # observed and only the classification is missing.
        status = TerminalStatus.NOT_FIFO_MONITORED.value
    elif state == LIFECYCLE_LIVE:
        # ``unknown`` is a legitimate provider answer here and means only
        # "live pane, state not yet detected".
        status = _provider_status(row["id"]) or "unknown"
    else:
        # A row whose identity does not resolve reports its lifecycle, not
        # a provider status. Reporting provider ``unknown`` for a deleted
        # window is what produced a wall of phantom cards that looked like
        # workers waiting to be talked to.
        status = state
    return {
        "terminal_id": row["id"],
        # The three pre-existing display keys, kept alongside the canonical
        # ones. The MCP tools, the CLI and the UI state service all read
        # this listing by these names; renaming them would be a breaking
        # change dressed up as a projection, and this change is meant to be
        # additive. ``terminal_id``/``name``/``session_name`` are the names
        # both views are required to agree on going forward.
        "id": row["id"],
        "tmux_session": row.get("tmux_session"),
        "tmux_window": row.get("tmux_window"),
        "name": row.get("tmux_window"),
        "session_name": row.get("tmux_session"),
        "provider": row.get("provider"),
        "agent_profile": row.get("agent_profile"),
        "caller_id": row.get("caller_id"),
        "generation": row.get("generation"),
        "callback_target_generation": row.get("callback_target_generation"),
        "protocol_vintage": vintage,
        "server_socket_path": row.get("server_socket_path"),
        "session_id": row.get("session_id"),
        "window_id": row.get("window_id"),
        "pane_id": row.get("pane_id"),
        "pane_pid": row.get("pane_pid"),
        "native_session_id": row.get("native_session_id"),
        "lifecycle_state": state,
        "lifecycle_reason": reason,
        # Stated rather than left to be inferred from the status: a
        # consumer deciding whether to wait for a classification needs to
        # know none is coming, and that is a different fact from whichever
        # status happens to be showing.
        "fifo_monitored": not native_tui,
        "superseded_by_terminal_id": row.get("superseded_by_terminal_id"),
        "superseded_by_generation": row.get("superseded_by_generation"),
        "status": status,
        "last_active": row.get("last_active"),
    }


def project_session(session_name: str) -> List[Dict[str, Any]]:
    """Every terminal in a session, both vintages, one instant."""
    panes = _observed_panes()
    projected = []
    for row in list_terminals_by_session(session_name):
        vintage = row.get("protocol_vintage") or "v1"
        # ``list_terminals_by_session`` returns a display-shaped subset, so
        # the identity fields are re-read from the row's own store rather
        # than assumed present. Projecting an absent field as None would
        # publish "this terminal has no pane", which is a different claim
        # from "this listing did not fetch it".
        full = (
            get_terminal_metadata_v2(row["id"])
            if vintage == "v2"
            else get_terminal_metadata(row["id"])
        )
        projected.append(project_row(full or row, panes, vintage=vintage))
    return projected


def project_terminal(terminal_id: str) -> Optional[Dict[str, Any]]:
    """One terminal by id, from whichever store holds it.

    The v1 probe is silent: this is a two-tier resolver on a dashboard-refresh
    hot path, so for a healthy v2-only terminal the v1 miss is the expected
    first-tier outcome. Warning there reported live terminals as missing on
    every card refresh (COND-0242). The miss is reported once, rate-limited,
    only when no store holds the terminal.
    """
    panes = _observed_panes()
    row = get_terminal_metadata(terminal_id, warn_if_missing=False)
    if row is not None:
        return project_row(row, panes, vintage="v1")
    try:
        row = get_terminal_metadata_v2(terminal_id)
    except Exception as exc:  # pragma: no cover - an uninstalled v2 surface is absent
        logger.debug("v2 terminal lookup unavailable for %s: %s", terminal_id, exc)
        return None
    if row is None:
        report_terminal_missing_from_every_store(terminal_id)
        return None
    return project_row(row, panes, vintage="v2")


def live_terminals(session_name: str) -> List[Dict[str, Any]]:
    """Only the terminals a caller may lawfully address.

    Everything demoted is excluded rather than deprioritised. A resolver
    that merely sorted live rows first would still pick a dead one when
    that is all there is, which is exactly the failure this replaces.
    """
    return [
        row
        for row in project_session(session_name)
        if row["lifecycle_state"] in ATTACHABLE_LIFECYCLE_STATES
    ]
