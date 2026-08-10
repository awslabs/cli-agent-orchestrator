"""Bounded exact-live-pane identity resolution (cond-0377D M3-A read seam).

Resolves one exact live tmux pane to its registered CAO terminal, unique
LIVE stable-agent incarnation, and stable agent/lineage identity.  This is
the fork primitive the conductor's ``conduct whoami`` will consume; it is
deterministic cooperative-local routing, not a security gate — it does not
authenticate the human or process that asked the question.

Identity model
==============

The request carries only the exact physical pane facts needed for lookup
(the immutable pane id and the canonical server socket identity).  A
caller-supplied ``TMUX_PANE``/``TMUX``/``CAO_TERMINAL_ID``/window name/
terminal id never proves identity: the service RE-OBSERVES the pane
through ``TmuxClient.pane_control_identity`` and the server through
``observe_pane_server_identity`` and binds:

* the canonical server socket identity plus the immutable tmux
  ``pane_id``/``window_id``/``session_id`` and positive ``pane_pid``
  (never session/window names);
* the registered terminal row's exact pane/server/process identity and
  physical occurrence (``generation``, or the legacy callback occurrence
  when the generation is null);
* ``stable_agent_roster.get_incarnation_by_terminal`` for the exact
  generation or the unique LIVE legacy incarnation;
* the roster's existing agent/lineage reads.

Outcomes are a versioned closed set: ``resolved``, or typed non-identity
for ``pane-unreadable-or-dead``, ``pane-unregistered``,
``terminal-pane-mismatch-or-superseded``, ``roster-incarnation-missing``,
and ``roster-incarnation-ambiguous-or-invalid``.  Absence and ambiguity
are normal typed answers, never guessed identities; an ambiguous or stale
row never picks the first match; an unregistered pane returns no stable
identity; a stopped/dead or superseded incarnation cannot resolve even if
historical roster records remain.

The lookup is strictly read-only: no terminal liveness, roster rows,
repair/migration journals, or attachments are mutated, and no write lease
is taken to perform a read.  Every tmux subprocess is bounded through the
existing timeout mechanism.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import native_status_repair as nsr
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.control_input_contract import normalize_server_identity

logger = logging.getLogger(__name__)

PANE_RESOLUTION_SCHEMA = "cao-m3-pane-identity-resolution-v1"

STATUS_RESOLVED = "resolved"
STATUS_PANE_UNREADABLE_OR_DEAD = "pane-unreadable-or-dead"
STATUS_PANE_UNREGISTERED = "pane-unregistered"
STATUS_TERMINAL_MISMATCH_OR_SUPERSEDED = "terminal-pane-mismatch-or-superseded"
STATUS_ROSTER_INCARNATION_MISSING = "roster-incarnation-missing"
STATUS_ROSTER_AMBIGUOUS_OR_INVALID = "roster-incarnation-ambiguous-or-invalid"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _live_start_marker(pid: int) -> Optional[str]:
    """The pid's current start marker through the exact stored-marker
    producer (``ps -o lstart=``), so the comparison is format-identical.
    Bounded and read-only; None means the marker could not be read."""
    from cli_agent_orchestrator.services.native_attachment_recovery import (
        _live_start_marker as _observed_live_start_marker,
    )

    try:
        return _observed_live_start_marker(pid)
    except Exception:  # noqa: BLE001 - evidence is best-effort by definition
        return None


def _bounded(detail: str) -> str:
    detail = (detail or "").strip()
    return detail if len(detail) <= 300 else detail[:300] + "…"


def _terminal_rows(session: Any) -> list[dict[str, Any]]:
    """Every terminal row (v2 vintage first, then the shared table) through
    the public occurrence-snapshot seam, deduplicated by terminal id."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in (database.ManagedLaunchV2TerminalModel, database.TerminalModel):
        for row in session.query(model).all():
            if row.id in seen:
                continue
            seen.add(row.id)
            snapshot = nsr.terminal_occurrence_snapshot(row.id, db=session)
            if snapshot is not None:
                rows.append(snapshot)
    return rows


def resolve_pane_identity(
    *,
    pane_id: str,
    server_socket_path: str,
    db: Any = None,
) -> dict[str, Any]:
    """Resolve one exact live pane to its registered terminal, unique LIVE
    incarnation, and stable agent/lineage.  Read-only; never mutates.

    ``pane_id`` is the immutable tmux pane id; ``server_socket_path`` is
    the canonical identity of the tmux server the caller observed the pane
    on.  The service re-observes both through the bounded tmux seams; a
    pane that is not provably live on exactly that server, is not
    registered to that exact tuple, or whose roster incarnation is not the
    unique LIVE one returns a typed non-identity.
    """

    def _resolve(session: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema": PANE_RESOLUTION_SCHEMA,
            "status": None,
            "reason": None,
            "observed_at": _now(),
            "pane": None,
            "terminal": None,
            "incarnation": None,
            "agent": None,
        }

        def non_identity(status: str, detail: str) -> dict[str, Any]:
            outcome = dict(base)
            outcome.update(status=status, reason=_bounded(detail))
            return outcome

        if (
            not isinstance(pane_id, str)
            or not pane_id
            or not isinstance(server_socket_path, str)
            or not server_socket_path
        ):
            return non_identity(
                STATUS_PANE_UNREADABLE_OR_DEAD,
                "pane_id and server_socket_path are both required",
            )

        # The bounded live observation by immutable pane id.
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        try:
            client = TmuxClient()
            live = client.pane_control_identity(pane_id=pane_id)
        except Exception as exc:  # noqa: BLE001 - an unobservable pane is a typed non-identity
            logger.warning("pane identity resolution: pane observation failed: %s", exc)
            return non_identity(
                STATUS_PANE_UNREADABLE_OR_DEAD, "the pane's live identity could not be observed"
            )
        if live is None or live.dead:
            return non_identity(
                STATUS_PANE_UNREADABLE_OR_DEAD,
                f"pane {pane_id} is not provably live on the tmux server this process reaches",
            )
        try:
            server = client.observe_pane_server_identity(pane_id)
        except Exception as exc:  # noqa: BLE001 - an unobservable server is a typed non-identity
            logger.warning("pane identity resolution: server observation failed: %s", exc)
            return non_identity(
                STATUS_PANE_UNREADABLE_OR_DEAD, "the pane's server identity could not be observed"
            )
        if server is None or normalize_server_identity(server_socket_path) != server:
            # The pane is not provably on the caller's canonical server:
            # identical pane ids on two isolated servers never cross-resolve.
            return non_identity(
                STATUS_PANE_UNREADABLE_OR_DEAD,
                f"pane {pane_id} is not provably on the bound tmux server",
            )
        observed = {
            "pane_id": live.pane_id,
            "window_id": live.window_id,
            "session_id": live.session_id,
            "pane_pid": live.pane_pid,
            "server_socket_path": server,
        }
        base["pane"] = observed

        # The registered terminal row: the pane id claims on THIS server
        # first, then the exact full tuple.  A reused pane id with a changed
        # window/session/pid is stale, never the current worker.
        rows = _terminal_rows(session)
        claiming = [
            row
            for row in rows
            if row["pane_id"] == observed["pane_id"]
            and normalize_server_identity(row["server_socket_path"] or "") == server
        ]
        if not claiming:
            return non_identity(
                STATUS_PANE_UNREGISTERED,
                f"no terminal row claims pane {pane_id} on this server",
            )
        exact = [
            row
            for row in claiming
            if row["window_id"] == observed["window_id"]
            and row["session_id"] == observed["session_id"]
            and row["pane_pid"] == observed["pane_pid"]
        ]
        if not exact:
            return non_identity(
                STATUS_TERMINAL_MISMATCH_OR_SUPERSEDED,
                f"pane {pane_id} was reused with a changed window/session/pid; "
                "the registered row is stale and cannot resolve",
            )
        if len(exact) > 1:
            return non_identity(
                STATUS_TERMINAL_MISMATCH_OR_SUPERSEDED,
                f"{len(exact)} terminal rows claim the exact pane tuple; ambiguous",
            )
        row = exact[0]
        if (
            row["lifecycle_state"] != "live"
            or row.get("superseded_by_terminal_id")
            or row.get("superseded_by_generation")
        ):
            return non_identity(
                STATUS_TERMINAL_MISMATCH_OR_SUPERSEDED,
                f"terminal {row['id']} is {row['lifecycle_state']!r} or carries a "
                "supersession pointer and cannot resolve to the current worker",
            )
        if row["generation"] is None and row.get("callback_target_generation") is None:
            return non_identity(
                STATUS_ROSTER_AMBIGUOUS_OR_INVALID,
                "the terminal row has no physical occurrence (generation and "
                "callback-target generation are both null); the exact occurrence cannot be "
                "resolved",
            )
        base["terminal"] = {
            "terminal_id": row["id"],
            "generation": row["generation"],
            "physical_occurrence": (
                row["generation"]
                if row["generation"] is not None
                else row["callback_target_generation"]
            ),
            "vintage": row["vintage"],
        }

        # The unique LIVE roster incarnation for the exact generation (or the
        # unique LIVE legacy incarnation when the generation is null).
        try:
            incarnation = roster.get_incarnation_by_terminal(
                row["id"], generation=row["generation"], db=session
            )
        except roster.StableAgentConflict as exc:
            return non_identity(STATUS_ROSTER_AMBIGUOUS_OR_INVALID, str(exc))
        except roster.StableAgentError as exc:
            logger.warning("pane identity resolution: roster read failed: %s", exc)
            return non_identity(STATUS_ROSTER_AMBIGUOUS_OR_INVALID, "the roster could not be read")
        if incarnation is None:
            return non_identity(
                STATUS_ROSTER_INCARNATION_MISSING,
                f"no roster incarnation is recorded for terminal {row['id']}",
            )
        if incarnation["disposition"] not in roster.LIVE_INCARNATION_DISPOSITIONS:
            return non_identity(
                STATUS_ROSTER_INCARNATION_MISSING,
                f"incarnation {incarnation['incarnation_id']} is "
                f"{incarnation['disposition']!r}; a stopped/dead or superseded "
                "incarnation cannot resolve even if historical records remain",
            )
        if (
            incarnation["pane_id"] != observed["pane_id"]
            or incarnation["pane_pid"] != observed["pane_pid"]
        ):
            return non_identity(
                STATUS_ROSTER_AMBIGUOUS_OR_INVALID,
                "the roster incarnation's pane/pid do not match the observed pane",
            )
        # The live process start marker must equal the incarnation's stored
        # process identity: a recycled pid with the same tmux tuple but a
        # changed start marker is stale, never the current worker.
        process_identity = incarnation.get("process_identity")
        if not isinstance(process_identity, dict) or not process_identity.get("start_marker"):
            return non_identity(
                STATUS_ROSTER_AMBIGUOUS_OR_INVALID,
                "the roster incarnation never published a process identity",
            )
        if process_identity.get("pid") != observed["pane_pid"]:
            return non_identity(
                STATUS_ROSTER_AMBIGUOUS_OR_INVALID,
                "the roster incarnation's process pid does not match the observed pid",
            )
        live_marker = _live_start_marker(observed["pane_pid"])  # type: ignore[arg-type]
        if live_marker is None or live_marker != process_identity["start_marker"]:
            return non_identity(
                STATUS_ROSTER_AMBIGUOUS_OR_INVALID,
                "the live process start marker no longer matches the recorded "
                "incarnation; the process was recycled",
            )
        base["incarnation"] = {
            "incarnation_id": incarnation["incarnation_id"],
            "disposition": incarnation["disposition"],
            "lineage_id": incarnation["lineage_id"],
        }

        # The stable agent and its lineage.
        try:
            agent = roster.get_agent(incarnation["agent_id"], db=session)
        except roster.StableAgentError as exc:
            logger.warning("pane identity resolution: agent read failed: %s", exc)
            return non_identity(
                STATUS_ROSTER_AMBIGUOUS_OR_INVALID, "the stable agent could not be read"
            )
        if (
            agent.get("current_incarnation_id") != incarnation["incarnation_id"]
            or agent.get("current_lineage_id") != incarnation["lineage_id"]
            or agent.get("agent_id") != incarnation["agent_id"]
        ):
            return non_identity(
                STATUS_ROSTER_AMBIGUOUS_OR_INVALID,
                "the stable agent's current incarnation/lineage pointers do not "
                "agree with the resolved incarnation",
            )
        lineage = agent.get("current_lineage") or {}
        if lineage.get("lineage_id") != incarnation["lineage_id"]:
            return non_identity(
                STATUS_ROSTER_AMBIGUOUS_OR_INVALID,
                "the stable agent's embedded current lineage does not match the "
                "resolved incarnation's lineage",
            )
        base["agent"] = {
            "agent_id": agent["agent_id"],
            "lineage_id": lineage.get("lineage_id"),
            "harness": lineage.get("harness"),
            "native_session_id": lineage.get("native_session_id"),
            "disposition": agent["disposition"],
        }
        base["status"] = STATUS_RESOLVED
        return base

    if db is not None:
        return _resolve(db)
    with database.SessionLocal() as session:
        return _resolve(session)
