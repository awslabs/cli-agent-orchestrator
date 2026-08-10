"""Exact-generation native ``/status`` identity repair (cond-0377C).

A missing native session id is repairable metadata, not a reason to throw
away the worker's conversation.  This is the bounded M3-A health
operation: for one *currently live, rostered* terminal, prove the exact
stored pane/session/window/process identity is live and the provider
composer is idle, type literal ``/status`` and one Enter exactly once,
parse only the pinned provider/build identity fields, persist the
repaired identity atomically, and leave an exclusive
``NativeSessionAttachmentModel`` owner for the exact running pane.

Ownership contract
==================

The operation reuses the existing seams instead of inventing a lease:

* ``callback_recovery.generation_lifecycle_claim`` serializes the exact
  terminal-generation lifecycle;
* ``pane_input_arbiter.pane_input_lease`` serializes every byte written
  to the exact pane;
* ``native_pane_input.TmuxPaneInput`` is the only transport;
* the provider-specific readiness observers reached by
  ``managed_launch_v2._await_native_pane_input_ready`` prove the composer
  is idle/ready before anything is typed;
* ``clients.tmux.TmuxClient`` proves the live pane/server identity tuple;
* ``stable_agent_roster.record_native_identity(..., db=db)`` and a new
  generation-conditional terminal writer commit atomically in one shared
  transaction with the immutable bounded evidence row.

``control_input_service`` is deliberately never used: this is not a
task/control message and must not manufacture its journal or receipts.

Claude modal handling (canary 2026-08-10, build 2.1.226)
========================================================

Claude renders ``/status`` as a modal whose ``Session ID:`` row is the
identity.  The single Escape that restores the composer is sent in a
``finally`` after the ``/status`` was submitted, so it runs on success,
parse failure, capture failure, timeout, persistence failure, and
cancellation alike, while the pane lease is still held.  If the Escape
itself also fails, the primary failure is preserved — but success is
never reported until the post-Escape styled composer proof succeeds.

Partial-failure ordering (documented, tested)
=============================================

1. Identity observation (``/status`` -> parse -> Escape -> composer
   proof) mutates nothing durable and touches nothing but the pane.
2. Attachment adoption commits first, in its own transaction: it is the
   exclusive-ownership claim for the exact observed pane/process.  If the
   atomic row+roster repair later fails, the conservative attachment
   remains visible and safe (never auto-released merely because metadata
   persistence failed), and an exact retry converges — without another
   ``/status`` when the prior adoption already names this exact owner.
3. The terminal row, the roster lineage, and the bounded evidence digest
   commit in one transaction, only after every exact fact (terminal ID,
   generation, tmux server/session/window/pane, pane PID/start marker,
   provider/harness, live lifecycle, roster live incarnation, parsed id)
   is re-verified immediately before commit.  Same id replays
   idempotently; a different id is a typed conflict and is never
   overwritten.

Kimi renders no session row before its first session-creating action: a
panel that parses as Kimi's with no ``Session session_<uuid>`` row
returns the typed ``identity-still-missing`` outcome with zero mutation
and no fabricated id.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import pane_input_arbiter as pia
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.callback_recovery import generation_lifecycle_claim
from cli_agent_orchestrator.services.control_input_contract import normalize_server_identity
from cli_agent_orchestrator.services.provider_contracts import normalized_version

logger = logging.getLogger(__name__)

#: The exact command typed into the pane, once, with exactly one Enter.
STATUS_COMMAND = "/status"

REPAIR_SCHEMA = "cao-native-status-repair-v1"

STATUS_REPAIRED = "repaired"
STATUS_IDENTITY_STILL_MISSING = "identity-still-missing"
STATUS_REFUSED = "refused"
STATUS_ERRORED = "errored"

#: Parser identities recorded in the evidence and the adoption receipt, so
#: a later reader knows which pinned build parser produced an identity.
PARSER_CLAUDE_MODAL = "claude-modal-v1"
PARSER_CODEX_STATUS = "codex-status-v1"
PARSER_KIMI_STATUS = "kimi-status-v1"
PARSER_MUSE_PANEL = "muse-panel-v1"

#: Bounds on the normalized capture used for parsing and digesting.  The
#: digest input is deterministically capped so an oversized screen cannot
#: produce an unbounded digest, and truncation never changes the parse
#: input (which is the tmux viewport itself, far smaller than the caps).
_MAX_NORMALIZED_ROWS = 2000
_MAX_NORMALIZED_ROW_CHARS = 4096

#: One pass over SGR escape sequences.  The canary's plain capture retains
#: literal ``[1m]`` fragments (which are not escapes and are left alone);
#: real ``ESC [ ... m`` sequences are stripped.  Deterministic and bounded.
_SGR_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_DETAIL_MAX = 500


class PanelParseError(ValueError):
    """The captured screen is not a usable, unambiguous status panel."""


class NativeStatusRepairError(RuntimeError):
    """Base class for the repair's typed failures."""


class NativeStatusRepairConflict(NativeStatusRepairError):
    """A refusal: nothing durable was mutated by this operation."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class NativeStatusRepairUnavailable(NativeStatusRepairError):
    """A transient failure; the operation may be retried exactly."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = "persistence-failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded(detail: str) -> str:
    detail = (detail or "").strip()
    return detail if len(detail) <= _DETAIL_MAX else detail[:_DETAIL_MAX] + "…"


def normalize_capture_rows(rows: Sequence[str]) -> list[str]:
    """Bounded, deterministic ANSI/style normalization of one capture.

    Strips SGR sequences, trims surrounding whitespace, and caps the row
    count and row width.  Literal styling fragments such as ``[1m]`` are
    *not* escapes and survive, exactly as the canary's plain capture
    retained them — the parsers simply never read those rows.
    """
    normalized: list[str] = []
    for raw in rows:
        if not isinstance(raw, str):
            raw = str(raw)
        cleaned = _SGR_SEQUENCE.sub("", raw).strip()
        if len(cleaned) > _MAX_NORMALIZED_ROW_CHARS:
            cleaned = cleaned[:_MAX_NORMALIZED_ROW_CHARS]
        normalized.append(cleaned)
        if len(normalized) >= _MAX_NORMALIZED_ROWS:
            break
    return normalized


def evidence_digest(rows: Sequence[str]) -> str:
    """The bounded SHA-256 digest of the normalized capture.

    This is the only thing persisted about the status output: never the
    raw rows, which may contain secrets.
    """
    normalized = normalize_capture_rows(rows)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def _canonical_uuid(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PanelParseError(f"the {label} is not a canonical UUID: {value!r}")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise PanelParseError(f"the {label} is not a canonical UUID: {value!r}") from exc
    if str(parsed) != value:
        raise PanelParseError(f"the {label} is not a canonical lowercase UUID: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Strict provider/build parsers — never a generic unscoped ``Session`` regex
# ---------------------------------------------------------------------------


_CLAUDE_HEADER_TOKENS = ("Settings", "Status", "Config")


def parse_claude_status(rows: Sequence[str], *, pinned_version: str) -> dict[str, Any]:
    """Parse the Claude 2.1.226 ``/status`` modal (canary 2026-08-10).

    Requires the modal header row, exactly one ``Version:`` row matching
    the pinned build, and exactly one ``Session ID:`` row whose value is a
    canonical lowercase UUID.  A second session row (a stale prior panel
    still on screen, or a duplicate render) is ambiguity and is refused.
    Model/MCP rows — which may carry styling fragments — are never read.
    """
    normalized = normalize_capture_rows(rows)
    if not any(all(token in row for token in _CLAUDE_HEADER_TOKENS) for row in normalized):
        raise PanelParseError(
            "the capture is not a Claude /status modal: no Settings/Status header row"
        )
    version_rows = [row for row in normalized if row.lstrip().startswith("Version:")]
    if len(version_rows) != 1:
        raise PanelParseError(
            f"the capture renders {len(version_rows)} 'Version:' rows; a truncated or "
            "duplicated panel is not an observation"
        )
    observed = normalized_version(version_rows[0].split(":", 1)[1].strip())
    if observed != pinned_version:
        raise PanelParseError(
            f"the modal reports build {observed!r}, not the pinned {pinned_version!r}; "
            "a drifted build has no repair parser"
        )
    session_rows = [row for row in normalized if row.lstrip().startswith("Session ID:")]
    if len(session_rows) != 1:
        raise PanelParseError(
            f"the capture renders {len(session_rows)} 'Session ID:' rows; a missing, "
            "duplicate, or stale prior panel cannot prove the session it names"
        )
    session_id = _canonical_uuid(
        session_rows[0].split(":", 1)[1].strip(), label="Claude Session ID"
    )
    return {"parser_key": PARSER_CLAUDE_MODAL, "session_id": session_id}


def parse_codex_status(rows: Sequence[str]) -> dict[str, Any]:
    """Parse the Codex 0.147.0 status row: exactly one ``Session: <uuid>``."""
    normalized = normalize_capture_rows(rows)
    session_rows = [row for row in normalized if row.startswith("Session:")]
    if len(session_rows) != 1:
        raise PanelParseError(
            f"the capture renders {len(session_rows)} 'Session:' rows; the Codex status "
            "panel must name exactly one canonical UUID"
        )
    session_id = _canonical_uuid(session_rows[0].split(":", 1)[1].strip(), label="Codex Session")
    return {"parser_key": PARSER_CODEX_STATUS, "session_id": session_id}


def parse_kimi_status(rows: Sequence[str]) -> dict[str, Any]:
    """Parse the Kimi 0.34.0 status panel: ``Session session_<uuid>``.

    A fresh untouched Kimi TUI may render no session row at all before its
    first session-creating action.  That is returned as a typed
    ``identity_still_missing`` verdict, never a fabricated id — but only
    when the capture is really a Kimi panel: a row starting with
    ``Session `` that is not another provider's known label (Claude's
    ``Session ID:`` / ``Session name:``).  Any other capture is refused as
    unparseable rather than misread.
    """
    normalized = normalize_capture_rows(rows)
    session_rows = [row for row in normalized if row.startswith("Session session_")]
    if len(session_rows) > 1:
        raise PanelParseError(
            "the capture renders more than one 'Session session_<uuid>' row; it cannot "
            "prove which session the pane runs"
        )
    if session_rows:
        raw = session_rows[0][len("Session ") :].strip()
        uuid_part = raw[len("session_") :] if raw.startswith("session_") else raw
        _canonical_uuid(uuid_part, label="Kimi session id")
        return {"parser_key": PARSER_KIMI_STATUS, "session_id": raw}
    # Kimi renders the session label without a colon ("Session
    # session_<uuid>", or a bare "Session -" before the first session
    # exists).  Every other provider's known session row carries a colon
    # ("Session ID:", "Session name:", "Session kind:"), so a colonless
    # "Session " row is the Kimi panel marker.
    panel_rows = [row for row in normalized if row.startswith("Session ") and ":" not in row]
    if panel_rows:
        return {"parser_key": PARSER_KIMI_STATUS, "identity_still_missing": True}
    raise PanelParseError("the capture is not a Kimi /status panel: no Session row at all")


def parse_muse_status(rows: Sequence[str]) -> dict[str, Any]:
    """Parse the Muse 0.1.0 panel, reusing the strict panel parse.

    The launch's pre-task gate (``require_pre_task_status`` and its
    zero-turn requirement) is deliberately NOT reused: a legacy pane has
    worked, and the panel still names the session it runs.  Only the
    session identity is taken, validated as a canonical UUID.
    """
    from cli_agent_orchestrator.services import muse_native_status

    normalized = normalize_capture_rows(rows)
    try:
        parsed = muse_native_status.parse_status_panel(normalized)
        session_id = muse_native_status.validate_discovered_session_id(parsed["session_id"])
    except (muse_native_status.MuseStatusParseError, muse_native_status.MuseStatusMismatch) as exc:
        raise PanelParseError(str(exc)) from exc
    return {"parser_key": PARSER_MUSE_PANEL, "session_id": session_id}


#: The pinned (provider, normalized build) -> parser table.  A build that
#: was never read has no entry here and therefore no repair parser: an
#: unproven build is refused, never guessed at with a generic regex.
_REPAIR_PARSER_PINS: dict[str, dict[str, dict[str, Any]]] = {
    "claude_code": {
        "2.1.226": {
            "parser_key": PARSER_CLAUDE_MODAL,
            "parse": parse_claude_status,
            "parse_kwargs": {"pinned_version": "2.1.226"},
            # The /status modal must be dismissed with exactly one Escape.
            "escape": True,
        },
    },
    "codex": {
        "0.147.0": {
            "parser_key": PARSER_CODEX_STATUS,
            "parse": parse_codex_status,
            "escape": False,
        },
    },
    "kimi_cli": {
        "0.34.0": {
            "parser_key": PARSER_KIMI_STATUS,
            "parse": parse_kimi_status,
            "escape": False,
        },
    },
    "muse_cli": {
        "0.1.0": {
            "parser_key": PARSER_MUSE_PANEL,
            "parse": parse_muse_status,
            "escape": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Terminal-row access (v2 vintage first, then the shared table)
# ---------------------------------------------------------------------------


def _terminal_row_from(session: Any, terminal_id: str) -> Optional[dict[str, Any]]:
    """The terminal row as a plain dict, v2 vintage first, or None.

    The v2 row lives only in ``managed_launch_v2_terminals``; the shared
    ``terminals`` row covers legacy launches.  Both carry the same exact
    pane/session/window/process tuple and lifecycle.
    """
    row = (
        session.query(database.ManagedLaunchV2TerminalModel)
        .filter(database.ManagedLaunchV2TerminalModel.id == terminal_id)
        .first()
    )
    if row is not None:
        return {
            "id": row.id,
            "provider": row.provider,
            "generation": row.generation,
            "lifecycle_state": row.v2_lifecycle_state,
            "pane_id": row.pane_id,
            "window_id": row.window_id,
            "session_id": row.v2_session_id,
            "server_socket_path": row.server_socket_path,
            "pane_pid": row.v2_pane_pid,
            "tmux_session": row.tmux_session,
        }
    row = (
        session.query(database.TerminalModel)
        .filter(database.TerminalModel.id == terminal_id)
        .first()
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "provider": row.provider,
        "generation": row.generation,
        "lifecycle_state": row.lifecycle_state,
        "pane_id": row.pane_id,
        "window_id": row.window_id,
        "session_id": row.session_id,
        "server_socket_path": row.server_socket_path,
        "pane_pid": row.pane_pid,
        "tmux_session": row.tmux_session,
    }


def _load_terminal_row(terminal_id: str) -> Optional[dict[str, Any]]:
    with database.SessionLocal() as db:
        return _terminal_row_from(db, terminal_id)


def _live_start_marker(pid: int) -> Optional[str]:
    """The pid's current start marker through the exact stored-marker
    producer (``ps -o lstart=``), so the comparison is format-identical."""
    from cli_agent_orchestrator.services.native_attachment_recovery import (
        _live_start_marker as _observed_live_start_marker,
    )

    try:
        return _observed_live_start_marker(pid)
    except Exception:  # noqa: BLE001 - evidence is best-effort by definition
        return None


# ---------------------------------------------------------------------------
# Exact-facts verification
# ---------------------------------------------------------------------------


def _verify_exact_facts(
    session: Any,
    *,
    terminal_id: str,
    generation: str,
    provider: str,
    pane_id: str,
    window_id: str,
    session_id: str,
    server_socket_path: str,
    pane_pid: int,
    process_identity: Mapping[str, Any],
    expected_session_id: Optional[str] = None,
) -> None:
    """Every exact fact must still match, immediately before any mutation.

    ``expected_session_id`` is supplied only at commit time: the lineage
    must be ``identity_missing`` or already bound to exactly this id — a
    different stored id is a typed conflict and is never overwritten.
    Raises :class:`NativeStatusRepairConflict` naming the drifted fact.
    """
    row = _terminal_row_from(session, terminal_id)
    if row is None:
        raise NativeStatusRepairConflict("terminal-not-found", "the terminal row is gone")
    if row["generation"] not in (None, generation):
        raise NativeStatusRepairConflict(
            "generation-mismatch",
            f"terminal {terminal_id} now holds generation {row['generation']!r}, "
            f"not the exact {generation!r}",
        )
    if row["lifecycle_state"] != "live":
        raise NativeStatusRepairConflict(
            "terminal-not-live",
            f"terminal {terminal_id} is {row['lifecycle_state']!r}, not live",
        )
    if row["provider"] != provider:
        raise NativeStatusRepairConflict(
            "provider-drift",
            f"terminal {terminal_id} now runs provider {row['provider']!r}, " f"not {provider!r}",
        )
    if (
        row["pane_id"] != pane_id
        or row["window_id"] != window_id
        or row["session_id"] != session_id
        or row["server_socket_path"] != server_socket_path
        or row["pane_pid"] != pane_pid
    ):
        raise NativeStatusRepairConflict(
            "pane-identity-drift",
            "the terminal row's pane/session/window/process tuple no longer matches "
            "the incarnation this repair was called for",
        )
    incarnation = roster.get_incarnation_by_terminal(terminal_id, generation=generation, db=session)
    if incarnation is None:
        raise NativeStatusRepairConflict(
            "no-roster-incarnation",
            f"no stable-agent incarnation is recorded for terminal {terminal_id} "
            f"generation {generation}",
        )
    if incarnation["disposition"] == roster.INCARNATION_RETIRED:
        raise NativeStatusRepairConflict(
            "incarnation-retired",
            f"incarnation {incarnation['incarnation_id']} is retired; the repair is "
            "refused for a dead incarnation",
        )
    if incarnation["disposition"] not in roster.LIVE_INCARNATION_DISPOSITIONS:
        raise NativeStatusRepairConflict(
            "incarnation-not-live",
            f"incarnation {incarnation['incarnation_id']} is {incarnation['disposition']!r}",
        )
    if incarnation["pane_id"] != pane_id or incarnation["pane_pid"] != pane_pid:
        raise NativeStatusRepairConflict(
            "pane-identity-drift",
            "the roster incarnation's pane/pid no longer matches the stored terminal row",
        )
    stored_identity = incarnation.get("process_identity")
    if stored_identity != dict(process_identity):
        raise NativeStatusRepairConflict(
            "process-identity-drift",
            "the roster incarnation's process identity no longer matches the identity "
            "this repair observed",
        )
    lineage = None
    if incarnation.get("lineage_id") is not None:
        lineage = (
            session.query(database.StableAgentLineageModel)
            .filter(database.StableAgentLineageModel.lineage_id == incarnation["lineage_id"])
            .one_or_none()
        )
    if lineage is not None:
        if lineage.harness != provider:
            raise NativeStatusRepairConflict(
                "identity-conflict",
                f"the lineage belongs to harness {lineage.harness!r}; native ids never "
                f"cross harness domains and a {provider!r} repair is refused",
            )
        if expected_session_id is not None:
            if (
                lineage.native_session_id is not None
                and lineage.native_session_id != expected_session_id
            ):
                raise NativeStatusRepairConflict(
                    "identity-conflict",
                    f"the lineage is already bound to native session "
                    f"{lineage.native_session_id!r}; repairing it with "
                    f"{expected_session_id!r} would overwrite a known identity",
                )


def _verify_live_pane(
    *,
    pane_id: str,
    window_id: str,
    session_id: str,
    server_socket_path: str,
    pane_pid: int,
    process_identity: Mapping[str, Any],
) -> None:
    """Prove the exact stored pane/server/process is live, right now."""
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    try:
        client = TmuxClient()
        live = client.pane_control_identity(pane_id=pane_id)
    except Exception as exc:  # noqa: BLE001 - an unobservable pane is a refusal
        raise NativeStatusRepairConflict(
            "pane-identity-drift", f"the pane's live identity could not be observed: {exc}"
        ) from exc
    if live is None:
        raise NativeStatusRepairConflict(
            "pane-identity-drift",
            f"pane {pane_id} is not on the tmux server this process reaches",
        )
    if (live.pane_id, live.window_id, live.session_id, live.pane_pid) != (
        pane_id,
        window_id,
        session_id,
        pane_pid,
    ):
        raise NativeStatusRepairConflict(
            "pane-identity-drift",
            f"the live pane tuple ({live.pane_id}, {live.window_id}, {live.session_id}, "
            f"pid {live.pane_pid}) does not match the stored tuple; the pane moved or "
            "was recycled",
        )
    try:
        server = client.observe_pane_server_identity(pane_id)
    except Exception as exc:  # noqa: BLE001 - an unobservable server is a refusal
        raise NativeStatusRepairConflict(
            "server-identity-drift",
            f"the pane's server identity could not be observed: {exc}",
        ) from exc
    if server is None:
        raise NativeStatusRepairConflict(
            "server-identity-drift",
            f"pane {pane_id} could not be proven to sit on the bound tmux server",
        )
    if normalize_server_identity(server_socket_path) != server:
        raise NativeStatusRepairConflict(
            "server-identity-drift",
            f"pane {pane_id} sits on tmux server {server!r}, not the bound "
            f"{server_socket_path!r}",
        )
    live_marker = _live_start_marker(pane_pid)
    if live_marker is None:
        raise NativeStatusRepairConflict(
            "process-identity-unobservable",
            f"the start marker of pid {pane_pid} could not be read",
        )
    if live_marker != process_identity["start_marker"]:
        raise NativeStatusRepairConflict(
            "process-identity-drift",
            f"pid {pane_pid} is alive but its start marker no longer matches the "
            "recorded incarnation",
        )


# ---------------------------------------------------------------------------
# Capture / parse / Escape
# ---------------------------------------------------------------------------


def _capture_panel_verdict(provider: str, pane_id: str, pin: Mapping[str, Any]) -> dict[str, Any]:
    """Capture until the pinned parser renders a verdict, or refuse.

    One ``/status`` was already submitted.  The observation is bounded by
    the shared native cold-start runway and never retyped: a second
    ``/status`` after a first landed would render a second panel and make
    the capture ambiguous, which the parser refuses rather than guesses
    at.  A parse failure or an unreadable pane polls until the bound, then
    raises a typed ``panel-unparsed`` refusal.
    """
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    deadline = time.monotonic() + v2.NATIVE_PANE_READY_TIMEOUT_SECONDS
    last_error: Optional[str] = None
    while True:
        try:
            rows = list(npi.capture_pane_screen(pane_id))
        except Exception as exc:  # noqa: BLE001 - an unread pane is not a parsed panel
            last_error = f"the pane's rendered screen could not be captured: {exc}"
        else:
            try:
                parsed = pin["parse"](rows, **pin.get("parse_kwargs", {}))
            except PanelParseError as exc:
                last_error = str(exc)
            else:
                if parsed.get("identity_still_missing"):
                    return {"kind": "still-missing"}
                return {
                    "kind": "id",
                    "session_id": parsed["session_id"],
                    "parser_key": pin["parser_key"],
                    "evidence_sha256": evidence_digest(rows),
                    "observed_at": _now(),
                }
        if time.monotonic() >= deadline:
            raise NativeStatusRepairConflict(
                "panel-unparsed",
                "the /status panel never rendered a usable identity within the bound; "
                f"last observation: {last_error or 'no capture was ever made'}",
            )
        time.sleep(v2._NATIVE_PANE_READY_POLL_SECONDS)


def _claude_composer_restored(rows: Sequence[str]) -> bool:
    """The canary's post-Escape composer proof: modal gone, composer back.

    The canary's post-Escape capture contains the divider/composer
    boundary (``---`` rows around a ``> `` prompt row) and no ``Session
    ID:`` row.  Both halves are required: a modal remnant still on screen
    is not a restored composer.
    """
    normalized = normalize_capture_rows(rows)
    if any(row.lstrip().startswith("Session ID:") for row in normalized):
        return False
    has_prompt = any(row.lstrip().startswith("> ") or row.lstrip() == ">" for row in normalized)
    has_divider = any(re.fullmatch(r"-{10,}", row.strip()) is not None for row in normalized)
    return has_prompt and has_divider


def _prove_composer_restored(pane_id: str) -> bool:
    """Bounded poll for the styled composer proof after the single Escape.

    Reads the styled capture (``-e``) exactly as the canary did; the
    proof is the rendered composer boundary, not an assumption that the
    key was accepted.  Never raises for a failed proof — it returns False
    and the caller refuses rather than reporting readiness.
    """
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    deadline = time.monotonic() + v2.NATIVE_PANE_READY_TIMEOUT_SECONDS
    while True:
        try:
            rows = list(npi.capture_pane_screen_styled(pane_id))
        except Exception:  # noqa: BLE001 - an unread pane is an unproven composer
            pass
        else:
            if _claude_composer_restored(rows):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(v2._NATIVE_PANE_READY_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Adoption and the atomic commit
# ---------------------------------------------------------------------------


def _prior_adoption_facts(
    *,
    provider: str,
    terminal_id: str,
    generation: str,
    pane_id: str,
    process_identity: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """A prior status-repair adoption of this exact owner, or None.

    The exact-retry convergence path: when an attachment already claims
    this exact running pane/process (verified live above) with a
    status-repair receipt, the identity it names was observed under this
    exact process and can be reused without another ``/status``.  Anything
    ambiguous — zero, or several matching rows — falls through to a fresh
    observation.
    """
    try:
        records = native_attachment.list_attachments(owner_terminal_id=terminal_id)
    except native_attachment.NativeAttachmentError as exc:
        raise NativeStatusRepairConflict("attachment-unavailable", str(exc)) from exc
    matches = [
        record
        for record in records
        if record["state"] in native_attachment.LIVE_STATES
        and record["provider"] == provider
        and record["owner"].get("generation") == generation
        and record["owner"].get("pane_id") == pane_id
        and record["owner"].get("process_identity") == dict(process_identity)
        and isinstance(record.get("adoption_receipt"), Mapping)
    ]
    if len(matches) != 1:
        return None
    receipt = matches[0]["adoption_receipt"]
    if not receipt.get("native_session_id") or not receipt.get("evidence_sha256"):
        return None
    return {
        "session_id": receipt["native_session_id"],
        "parser_key": receipt.get("parser_key"),
        "evidence_sha256": receipt["evidence_sha256"],
        "observed_at": receipt.get("observed_at"),
        "composer_restored": receipt.get("composer_restored"),
    }


def _adopt_running_owner(
    *,
    operation_id: str,
    request_digest: str,
    provider: str,
    session_id: str,
    terminal_id: str,
    generation: str,
    pane_id: str,
    process_identity: Mapping[str, Any],
    parser_key: str,
    provider_version: str,
    evidence_sha256: str,
    observed_at: str,
    composer_restored: Optional[bool],
) -> tuple[dict[str, Any], bool]:
    """Claim the exact running owner, or a typed refusal, before any
    row/roster mutation.  A conflict never tears down the legacy pane."""
    receipt = native_attachment.status_repair_adoption_receipt(
        operation_id=operation_id,
        request_digest=request_digest,
        provider=provider,
        native_session_id=session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=em.NATIVE_TUI,
        pane_id=pane_id,
        process_identity=process_identity,
        parser_key=parser_key,
        provider_version=provider_version,
        evidence_sha256=evidence_sha256,
        observed_at=observed_at,
        composer_restored=composer_restored,
    )
    intent = native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_STATUS_DISCOVERED,
        acquisition_receipt={
            "schema": "cao-native-status-repair-intent-v1",
            "provider": provider,
            "native_session_id": session_id,
            "operation_id": operation_id,
            "parser_key": parser_key,
            "provider_version": provider_version,
            "evidence_sha256": evidence_sha256,
            "task_bytes_submitted": False,
        },
        # The id was discovered from the provider's own status surface on
        # this exact running pane; nothing prior exists for it to re-admit
        # or replay.  Asserted explicitly anyway: these are obligations the
        # store checks, not descriptions it records.
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        note=f"native status repair {operation_id}",
    )
    try:
        return native_attachment.adopt_running_owner(
            provider=provider,
            native_session_id=session_id,
            terminal_id=terminal_id,
            generation=generation,
            execution_mode=em.NATIVE_TUI,
            pane_id=pane_id,
            process_identity=process_identity,
            receipt=receipt,
            intent=intent,
        )
    except native_attachment.NativeAttachmentConflict as exc:
        raise NativeStatusRepairConflict("attachment-conflict", str(exc)) from exc
    except native_attachment.NativeAttachmentError as exc:
        raise NativeStatusRepairConflict("attachment-unavailable", str(exc)) from exc


def _commit_repair(db: Any, facts: Mapping[str, Any]) -> None:
    """The atomic row + roster + evidence commit.

    Every exact fact is re-verified inside this transaction immediately
    before the writes, so a drift between observation and commit refuses
    with zero mutation.  Same id replays idempotently; a different stored
    id is a typed conflict and is never overwritten.  A roster failure
    rolls the terminal row and the evidence back with it.
    """
    _verify_exact_facts(
        db,
        terminal_id=facts["terminal_id"],
        generation=facts["generation"],
        provider=facts["provider"],
        pane_id=facts["pane_id"],
        window_id=facts["window_id"],
        session_id=facts["tmux_session_id"],
        server_socket_path=facts["server_socket_path"],
        pane_pid=facts["pane_pid"],
        process_identity=facts["process_identity"],
        expected_session_id=facts["session_id"],
    )
    written = database.set_terminal_native_session_id_conditional(
        facts["terminal_id"], facts["generation"], facts["session_id"], db=db
    )
    if not written:
        raise NativeStatusRepairConflict(
            "identity-conflict",
            "the terminal row moved between verification and write; refusing to "
            "overwrite whoever won",
        )
    try:
        roster.record_native_identity(
            terminal_id=facts["terminal_id"],
            generation=facts["generation"],
            native_session_id=facts["session_id"],
            harness=facts["provider"],
            acquisition_method=native_attachment.ACQUISITION_STATUS_DISCOVERED,
            continuity_note=f"native status repair {facts['operation_id']}",
            db=db,
        )
    except roster.StableAgentConflict as exc:
        raise NativeStatusRepairConflict("identity-conflict", str(exc)) from exc
    except roster.StableAgentError as exc:
        raise NativeStatusRepairUnavailable(str(exc)) from exc
    db.add(
        database.NativeStatusRepairEvidenceModel(
            operation_id=facts["operation_id"],
            request_digest=facts["request_digest"],
            terminal_id=facts["terminal_id"],
            generation=facts["generation"],
            provider=facts["provider"],
            provider_version=facts["provider_version"],
            native_session_id=facts["session_id"],
            parser_key=facts["parser_key"],
            evidence_sha256=facts["evidence_sha256"],
            observed_at=facts["observed_at"],
            created_at=_now(),
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# The operation
# ---------------------------------------------------------------------------


def repair_terminal_native_identity(
    *,
    terminal_id: str,
    generation: str,
    provider_version: str,
    operation_id: Optional[str] = None,
    request_digest: Optional[str] = None,
    caller: str = "cao.native-status-repair",
) -> dict[str, Any]:
    """Repair one currently live rostered terminal's native session id.

    The operation is serialized under the exact terminal-generation
    lifecycle claim and the per-pane input lease for its whole run —
    including the Claude Escape and the post-Escape composer proof — and
    returns a typed outcome in every case.  It never calls Stop, Pause,
    reincarnation, or task delivery, never tears down the pane, and never
    blocks teardown: every claim it holds is released before it returns.

    Args:
        terminal_id: The exact terminal id (immutable identity).
        generation: The exact roster generation (nonempty).
        provider_version: The installed provider build (a banner or a bare
            semver); the parser pin must name it exactly.
        operation_id: Explicit operation id for crash/retry truth;
            minted when omitted.
        request_digest: Caller-supplied digest of the repair request;
            derived deterministically when omitted.
        caller: Holder label recorded on the pane input lease.
    """
    op_id = operation_id or str(uuid.uuid4())
    req_digest = (
        request_digest
        or hashlib.sha256(
            "\x00".join((terminal_id, generation, provider_version or "")).encode("utf-8")
        ).hexdigest()
    )
    base: dict[str, Any] = {
        "schema": REPAIR_SCHEMA,
        "status": None,
        "reason": None,
        "detail": None,
        "operation_id": op_id,
        "request_digest": req_digest,
        "terminal_id": terminal_id,
        "generation": generation,
        "provider": None,
        "provider_version": normalized_version(provider_version),
        "native_session_id": None,
        "evidence_sha256": None,
        "parser_key": None,
        "attachment": None,
        "composer_restored": None,
        "task_bytes_submitted": False,
    }

    def refused(reason: str, detail: str) -> dict[str, Any]:
        outcome = dict(base)
        outcome.update(status=STATUS_REFUSED, reason=reason, detail=_bounded(detail))
        return outcome

    if not (terminal_id and generation and provider_version):
        return refused(
            "invalid-input",
            "terminal_id, generation, and provider_version are all required; "
            "the repair never guesses an identity",
        )

    row = _load_terminal_row(terminal_id)
    if row is None:
        return refused(
            "terminal-not-found",
            f"no terminal row is recorded for {terminal_id}",
        )
    if row["generation"] not in (None, generation):
        return refused(
            "generation-mismatch",
            f"terminal {terminal_id} holds generation {row['generation']!r}, "
            f"not the exact {generation!r}",
        )
    if row["lifecycle_state"] != "live":
        return refused(
            "terminal-not-live",
            f"terminal {terminal_id} is {row['lifecycle_state']!r}, not live",
        )
    provider = row["provider"]
    if provider not in _REPAIR_PARSER_PINS:
        return refused(
            "provider-unsupported",
            f"provider {provider!r} has no pinned native /status repair parser",
        )
    version = normalized_version(provider_version)
    pin = _REPAIR_PARSER_PINS[provider].get(version)
    if pin is None:
        return refused(
            "unsupported-build",
            f"provider {provider!r} build {provider_version!r} (normalized {version!r}) "
            "has no pinned repair parser; an unproven build is refused, never guessed",
        )

    try:
        incarnation = roster.get_incarnation_by_terminal(terminal_id, generation=generation)
    except roster.StableAgentError as exc:
        return refused("roster-unavailable", f"the roster could not be read: {exc}")
    if incarnation is None:
        return refused(
            "no-roster-incarnation",
            f"no stable-agent incarnation is recorded for terminal {terminal_id} "
            f"generation {generation}",
        )
    if incarnation["disposition"] == roster.INCARNATION_RETIRED:
        return refused(
            "incarnation-retired",
            f"incarnation {incarnation['incarnation_id']} is retired; a repair never "
            "revives a retired incarnation",
        )
    if incarnation["disposition"] not in roster.LIVE_INCARNATION_DISPOSITIONS:
        return refused(
            "incarnation-not-live",
            f"incarnation {incarnation['incarnation_id']} is "
            f"{incarnation['disposition']!r}, not live",
        )

    pane_id = row["pane_id"]
    window_id = row["window_id"]
    tmux_session_id = row["session_id"]
    server_socket_path = row["server_socket_path"]
    pane_pid = row["pane_pid"]
    if not (
        pane_id
        and window_id
        and tmux_session_id
        and server_socket_path
        and isinstance(pane_pid, int)
        and pane_pid > 0
    ):
        return refused(
            "pane-identity-incomplete",
            "the terminal row does not carry the complete exact pane/session/window/"
            "process tuple, so nothing can be proven about the pane",
        )
    if pane_id != incarnation["pane_id"] or pane_pid != incarnation["pane_pid"]:
        return refused(
            "pane-identity-drift",
            "the terminal row and the roster incarnation disagree about the pane/pid",
        )
    process_identity = incarnation.get("process_identity")
    if not isinstance(process_identity, Mapping) or not process_identity.get("start_marker"):
        return refused(
            "process-identity-unpublished",
            "the roster incarnation never published a process identity; an identity-less "
            "incarnation cannot prove which process runs the pane",
        )
    if process_identity.get("pid") != pane_pid:
        return refused(
            "pane-identity-drift",
            "the roster incarnation's process pid does not match the stored pane pid",
        )

    try:
        with generation_lifecycle_claim(terminal_id, generation):
            try:
                with pia.pane_input_lease(pane_id, holder=caller, timeout=0.0):
                    outcome = _repair_under_claims(
                        op_id,
                        req_digest,
                        base,
                        pin,
                        row,
                        process_identity,
                        pane_id,
                        window_id,
                        tmux_session_id,
                        server_socket_path,
                        pane_pid,
                        provider,
                        version,
                    )
            except pia.PaneBusyError as exc:
                return refused(
                    "pane-busy",
                    f"another writer holds the pane input lease for {pane_id}; "
                    "zero bytes were written and nothing was mutated",
                )
            except pia.PaneInputArbiterError as exc:
                return refused("pane-unwritable", str(exc))
    except NativeStatusRepairError as exc:
        return refused(getattr(exc, "reason", "errored"), str(exc))
    except Exception as exc:  # noqa: BLE001 - never let the operation escape untyped
        logger.exception(
            "native status repair for %s %s failed unexpectedly",
            terminal_id,
            generation,
        )
        outcome = dict(base)
        outcome.update(
            status=STATUS_ERRORED,
            reason="errored",
            detail=_bounded(f"{type(exc).__name__}: {exc}"),
        )
        return outcome
    return outcome


def _repair_under_claims(
    op_id: str,
    req_digest: str,
    base: dict[str, Any],
    pin: Mapping[str, Any],
    row: Mapping[str, Any],
    process_identity: Mapping[str, Any],
    pane_id: str,
    window_id: str,
    tmux_session_id: str,
    server_socket_path: str,
    pane_pid: int,
    provider: str,
    version: str,
) -> dict[str, Any]:
    """The observation and persistence, run under the lifecycle claim and
    the pane input lease."""
    terminal_id = row["id"]
    generation = row["generation"] if row["generation"] is not None else base["generation"]
    # Re-verify every exact fact now that the claims are held: drift
    # between load and claim is drift, and drift means zero bytes.
    with database.SessionLocal() as db:
        _verify_exact_facts(
            db,
            terminal_id=terminal_id,
            generation=generation,
            provider=provider,
            pane_id=pane_id,
            window_id=window_id,
            session_id=tmux_session_id,
            server_socket_path=server_socket_path,
            pane_pid=pane_pid,
            process_identity=process_identity,
        )
    _verify_live_pane(
        pane_id=pane_id,
        window_id=window_id,
        session_id=tmux_session_id,
        server_socket_path=server_socket_path,
        pane_pid=pane_pid,
        process_identity=process_identity,
    )

    # The provider composer must be idle/ready before anything is typed.
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    readiness_record = {
        "provider": provider,
        "terminal_id": terminal_id,
        "generation": generation,
        "session_name": row["tmux_session"],
    }
    try:
        observation = v2._await_native_pane_input_ready(readiness_record, pane_id)
    except Exception as exc:  # noqa: BLE001 - an unobservable pane is a refusal
        raise NativeStatusRepairConflict(
            "not-ready", f"the pane's readiness could not be observed: {exc}"
        ) from exc
    if (
        not observation.get("input_ready")
        or observation.get("provider_status") != TerminalStatus.IDLE.value
    ):
        raise NativeStatusRepairConflict(
            "not-ready",
            f"the provider composer is not idle/ready (observed "
            f"{observation.get('provider_status')!r}); zero status bytes were typed",
        )

    # Exact-retry convergence: a prior status-repair adoption already
    # names this exact verified owner, so no second /status is needed.
    prior = _prior_adoption_facts(
        provider=provider,
        terminal_id=terminal_id,
        generation=generation,
        pane_id=pane_id,
        process_identity=process_identity,
    )
    if prior is not None:
        return _finish_repair(
            op_id=op_id,
            req_digest=req_digest,
            base=base,
            pin=pin,
            row=row,
            process_identity=process_identity,
            pane_id=pane_id,
            window_id=window_id,
            tmux_session_id=tmux_session_id,
            server_socket_path=server_socket_path,
            pane_pid=pane_pid,
            provider=provider,
            version=version,
            session_id=prior["session_id"],
            parser_key=prior["parser_key"],
            evidence_sha256=prior["evidence_sha256"],
            observed_at=prior["observed_at"],
            composer_restored=prior.get("composer_restored"),
        )

    # The one observation: literal /status and exactly one Enter.
    typed = npi.TmuxPaneInput(pane_id)
    try:
        typed.send_literal(STATUS_COMMAND)
        typed.send_enter()
    except Exception as exc:  # noqa: BLE001 - the write itself failed
        raise NativeStatusRepairConflict(
            "pane-unwritable", f"the /status write was refused by tmux: {exc}"
        ) from exc

    # From here the /status has been submitted.  For the Claude modal the
    # single Escape and the post-Escape composer proof run in a finally
    # that preserves the primary failure on every path below: success,
    # parse failure, capture failure, timeout, persistence failure, and
    # cancellation.  The pane lease stays held the whole time.
    cleanup_error: Optional[BaseException] = None
    composer_restored: Optional[bool] = None

    def _escape_finally() -> None:
        nonlocal cleanup_error, composer_restored
        try:
            typed.send_key("Escape")
            composer_restored = _prove_composer_restored(pane_id)
        except BaseException as exc:  # noqa: BLE001 - cleanup never masks the primary
            cleanup_error = exc

    try:
        verdict = _capture_panel_verdict(provider, pane_id, pin)
        if verdict["kind"] == "still-missing":
            outcome = dict(base)
            outcome.update(
                status=STATUS_IDENTITY_STILL_MISSING,
                reason=STATUS_IDENTITY_STILL_MISSING,
                detail=(
                    "the Kimi /status panel renders no session id; a session may not "
                    "exist before the provider's first session-creating action. "
                    "Nothing was recorded and no id was fabricated."
                ),
                provider=provider,
            )
            return outcome
    except NativeStatusRepairError:
        raise
    finally:
        if pin.get("escape"):
            _escape_finally()

    # The finally has run.  A failed cleanup never becomes success: without
    # the post-Escape styled composer proof nothing is committed.
    if pin.get("escape") and composer_restored is not True:
        detail = "the post-Escape styled composer proof did not succeed, so the pane "
        "is not proven ready and nothing was committed"
        if cleanup_error is not None:
            detail += f" (the Escape cleanup itself failed: {cleanup_error})"
        raise NativeStatusRepairConflict("composer-not-restored", detail)

    return _finish_repair(
        op_id=op_id,
        req_digest=req_digest,
        base=base,
        pin=pin,
        row=row,
        process_identity=process_identity,
        pane_id=pane_id,
        window_id=window_id,
        tmux_session_id=tmux_session_id,
        server_socket_path=server_socket_path,
        pane_pid=pane_pid,
        provider=provider,
        version=version,
        session_id=verdict["session_id"],
        parser_key=verdict["parser_key"],
        evidence_sha256=verdict["evidence_sha256"],
        observed_at=verdict["observed_at"],
        composer_restored=composer_restored,
    )


def _finish_repair(
    *,
    op_id: str,
    req_digest: str,
    base: dict[str, Any],
    pin: Mapping[str, Any],
    row: Mapping[str, Any],
    process_identity: Mapping[str, Any],
    pane_id: str,
    window_id: str,
    tmux_session_id: str,
    server_socket_path: str,
    pane_pid: int,
    provider: str,
    version: str,
    session_id: str,
    parser_key: str,
    evidence_sha256: str,
    observed_at: str,
    composer_restored: Optional[bool],
) -> dict[str, Any]:
    """Adopt the exclusive owner, then commit row+roster+evidence
    atomically.  Attachment adoption commits first; if the atomic repair
    later fails, the conservative attachment remains and an exact retry
    can finish it."""
    terminal_id = row["id"]
    generation = row["generation"] if row["generation"] is not None else base["generation"]

    record, adopted = _adopt_running_owner(
        operation_id=op_id,
        request_digest=req_digest,
        provider=provider,
        session_id=session_id,
        terminal_id=terminal_id,
        generation=generation,
        pane_id=pane_id,
        process_identity=process_identity,
        parser_key=parser_key,
        provider_version=version,
        evidence_sha256=evidence_sha256,
        observed_at=observed_at,
        composer_restored=composer_restored,
    )

    facts = {
        "operation_id": op_id,
        "request_digest": req_digest,
        "terminal_id": terminal_id,
        "generation": generation,
        "provider": provider,
        "provider_version": version,
        "session_id": session_id,
        "parser_key": parser_key,
        "evidence_sha256": evidence_sha256,
        "observed_at": observed_at,
        "pane_id": pane_id,
        "window_id": window_id,
        "tmux_session_id": tmux_session_id,
        "server_socket_path": server_socket_path,
        "pane_pid": pane_pid,
        "process_identity": dict(process_identity),
    }
    try:
        with database.SessionLocal() as db:
            _commit_repair(db, facts)
    except NativeStatusRepairError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed, never half-repair
        raise NativeStatusRepairUnavailable(
            f"the terminal-row and roster repair did not commit: {exc}"
        ) from exc

    outcome = dict(base)
    outcome.update(
        status=STATUS_REPAIRED,
        reason=None,
        provider=provider,
        native_session_id=session_id,
        evidence_sha256=evidence_sha256,
        parser_key=parser_key,
        composer_restored=composer_restored if pin.get("escape") else None,
        attachment={
            "state": record["state"],
            "owner": record["owner"],
            "adoption_receipt": record.get("adoption_receipt"),
            "adopted": adopted,
        },
    )
    return outcome
