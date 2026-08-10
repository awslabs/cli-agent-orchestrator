"""Panel-attested native ``/status`` identity repair (cond-0377C).

A missing native session id is repairable metadata, not a reason to throw
away the worker's conversation.  This is the bounded M3-A health
operation: for one *currently live, rostered* terminal, prove the exact
stored pane/session/window/process identity is live and the provider
composer is idle, type literal ``/status`` and one Enter exactly once,
parse only the *panel-attested branded* provider/build identity fields,
persist the repaired identity atomically, and leave an exclusive
``NativeSessionAttachmentModel`` owner for the exact running pane.

Identity model
==============

The request's ``generation`` is an *expected model generation*, never an
arbitrary physical key.

* A managed/v2 terminal requires it and it must equal ``row.generation``.
* A legacy terminal has ``row.generation is None`` and a roster
  incarnation with generation ``None``; a supplied non-null expected
  model generation is refused.  The durable physical occurrence for a
  legacy row is its nonempty ``callback_target_generation``.
* The physical occurrence (model generation for managed, callback-target
  generation for legacy) is what binds attachment ownership, evidence,
  and the operation itself.  A managed occurrence is a model generation;
  the two are never conflated.

Ownership contract
==================

The operation reuses the existing seams instead of inventing a lease:

* ``callback_recovery.terminal_lifecycle_claim_set`` + ``generation_lifecycle_claims``
  take the canonical lifecycle claim set (model-generation,
  callback-target-generation, and pane as applicable) that terminal
  teardown/Stop itself takes, so Stop/delete is boundedly serialized
  against a running repair — a repair holds these claims from before its
  first status byte until after provider cleanup and the atomic commit.
* ``pane_input_arbiter.pane_input_lease`` serializes every byte written
  to the exact pane.
* ``native_pane_input.TmuxPaneInput`` is the only transport.
* the provider-specific turn-state observers prove the composer is
  idle/ready before anything is typed.
* ``clients.tmux.TmuxClient`` proves the live pane/server identity tuple.
* ``stable_agent_roster.record_native_identity(..., db=db)`` and a
  generation/occurrence-conditional terminal writer commit atomically in
  one shared transaction with the immutable bounded evidence row.

``control_input_service`` is deliberately never used: this is not a
task/control message and must not manufacture its journal or receipts.

Branded pinned builds
=====================

Every parser requires exactly one provider brand/version header and the
provider's strict required fields, and returns the *panel-observed*
provider version.  Receipts, evidence, and parser-key selection use that
observed value, never the caller's assertion; caller/provider metadata
selects the pre-status interaction plan only.

* Codex 0.147.0 — ``>_ OpenAI Codex (v0.147.0)`` and exactly one
  ``Session: <uuid>``.
* Kimi 0.34.0 — ``>_ Kimi Code (v0.34.0)`` and either a live canonical
  ``Session session_<uuid>`` row or the exact ``Session none`` (the
  verified fresh/no-turn missing-ID panel, typed ``identity-still-missing``
  with zero mutation and no fabricated id).  ``Session nonsense`` /
  ``Session -`` are malformed and refused.
* Muse 0.1.0 — ``>_ Muse Code (0.1.0)`` plus the strict status labels.
* Claude 2.1.226 — the branded Settings/Status modal with exact Version
  and Session ID, plus the unconditional Escape/composer recovery.

Claude modal handling (canary 2026-08-10, build 2.1.226)
========================================================

Claude renders ``/status`` as a modal whose ``Session ID:`` row is the
identity.  The single Escape that restores the composer is sent in a
``finally`` after the ``/status`` was submitted, so it runs on success,
parse failure, capture failure, timeout, persistence failure, and
cancellation alike, while the pane lease is still held.  If the Escape
itself also fails, the primary failure is preserved — but success is
never reported until the post-Escape styled composer proof succeeds.

Cancellation and Stop
=====================

Once the off-loop repair has typed ``/status``, a cancellation does NOT
release the lifecycle/pane claims while the worker thread keeps running:
the shared claims and the pane lease are held through provider cleanup
(especially the Claude Escape/composer proof) and released only when the
worker exits the operation.  Stop/delete is intentionally *boundedly
serialized* by the shared lifecycle claims.  The observation phase is
bounded by one shared deadline (readiness + capture + composer proof
compose into a single runway), never three sequential runway-length waits.

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
   expected model generation, physical occurrence, tmux
   server/session/window/pane, pane PID/start marker, provider/harness,
   live lifecycle, roster live incarnation, parsed id) is re-verified
   immediately before commit.  Same id replays idempotently; a different
   id is a typed conflict and is never overwritten.

Known-identity preflight and operation idempotency
==================================================

* Terminal metadata and roster lineage both carrying the same known id
  with an existing attachment is a typed ``already-known`` no-op (zero
  ``/status``, zero evidence, zero mutation); the same known id with no
  attachment is a typed ``attachment-unresolved`` outcome (a later
  attachment audit owns that concern, not this bounded repair).
* Both known but conflicting is a typed conflict with zero bytes.
* Exactly one known id is verified by ``/status``: the parsed id must
  equal the known value before adoption or any durable mutation.
* ``operation_id`` is an explicit canonical UUID bound to a server-derived
  digest of the immutable request inputs.  An exact successful retry
  adopts the recorded evidence with no second status interaction; the same
  operation id with a changed digest is a typed conflict before pane I/O.
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
from cli_agent_orchestrator.services.callback_recovery import (
    generation_lifecycle_claims,
    terminal_lifecycle_claim_set,
)
from cli_agent_orchestrator.services.control_input_contract import normalize_server_identity
from cli_agent_orchestrator.services.provider_contracts import normalized_version

logger = logging.getLogger(__name__)

#: The exact command typed into the pane, once, with exactly one Enter.
STATUS_COMMAND = "/status"

REPAIR_SCHEMA = "cao-native-status-repair-v1"

STATUS_REPAIRED = "repaired"
STATUS_ALREADY_KNOWN = "already-known"
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

#: A leading ``>_ `` composer-prompt marker on a panel row (tolerating the
#: leading space a box-drawn row leaves behind).  Deliberately requires the
#: underscore: a bare ``> `` IS the provider composer prompt, which the
#: post-Escape composer proof must still see.
_PROMPT_PREFIX = re.compile(r"^\s*>\s*_+\s*")

#: A provider brand header: ``Brand (vX.Y.Z)`` or ``Brand (X.Y.Z)``.
_BRAND_HEADER = re.compile(r"^(?P<brand>[A-Za-z][A-Za-z ]*) \((?:v)?(?P<version>\d+\.\d+\.\d+)\)$")

_DETAIL_MAX = 500

#: Poll interval for the bounded observation phases.
_POLL_SECONDS = 0.1


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


def _canonical_uuid(value: Any, *, label: str) -> str:
    """Return ``value`` when it is a canonical lowercase UUID.

    Never echoes the supplied value: the value comes from the pane and
    may contain anything, including secrets.  The error names only the
    field.
    """
    if not isinstance(value, str) or not value:
        raise PanelParseError(f"the {label} is not a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise PanelParseError(f"the {label} is not a canonical UUID") from exc
    if str(parsed) != value:
        raise PanelParseError(f"the {label} is not a canonical lowercase UUID")
    return value


_BOX_DRAWING = "│╭╰╯─"


def normalize_capture_rows(rows: Sequence[str]) -> list[str]:
    """Bounded, deterministic ANSI/box-drawing normalization of a capture.

    Strips SGR sequences, box-drawing furniture, and a leading
    composer-prompt marker, trims surrounding whitespace, and caps the row
    count and row width.  Literal styling fragments such as ``[1m]`` are
    *not* escapes and survive, exactly as the canary's plain capture
    retained them — the parsers simply never read those rows.
    """
    normalized: list[str] = []
    for raw in rows:
        if not isinstance(raw, str):
            raw = str(raw)
        cleaned = _SGR_SEQUENCE.sub("", raw)
        if _BOX_DRAWING:
            cleaned = cleaned.translate(str.maketrans("", "", _BOX_DRAWING))
        cleaned = _PROMPT_PREFIX.sub("", cleaned).strip()
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


# ---------------------------------------------------------------------------
# Branded provider parsers — never a generic unscoped ``Session`` regex
# ---------------------------------------------------------------------------


def _require_brand_header(
    normalized: Sequence[str], *, brand: str, expected_version: str, panel_name: str
) -> str:
    """Require exactly one branded version header, returning the observed
    version.  Errors name only the panel and the expected build, never a
    raw pane value."""
    observed: list[str] = []
    for row in normalized:
        match = _BRAND_HEADER.fullmatch(row)
        if match is None or match.group("brand") != brand:
            continue
        observed.append(match.group("version"))
    if len(observed) != 1:
        raise PanelParseError(
            f"the capture is not a branded {panel_name} status panel: it must render "
            f"exactly one '{brand}' brand header"
        )
    if observed[0] != expected_version:
        raise PanelParseError(
            f"the {panel_name} status panel does not attest the pinned {expected_version} "
            "build; a drifted build has no repair parser"
        )
    return observed[0]


_CLAUDE_HEADER_TOKENS = ("Settings", "Status", "Config")


def parse_claude_status(rows: Sequence[str], *, pinned_version: str = "2.1.226") -> dict[str, Any]:
    """Parse the Claude 2.1.226 ``/status`` modal (canary 2026-08-10).

    Requires the branded Settings/Status modal header row, exactly one
    ``Version:`` row attesting the pinned build, and exactly one
    ``Session ID:`` row whose value is a canonical lowercase UUID.  A
    second session row (a stale prior panel, or a duplicate render) is
    ambiguity and is refused.  Model/MCP rows — which may carry styling
    fragments — are never read.
    """
    normalized = normalize_capture_rows(rows)
    if not any(all(token in row for token in _CLAUDE_HEADER_TOKENS) for row in normalized):
        raise PanelParseError(
            "the capture is not a Claude /status modal: no Settings/Status header row"
        )
    version_rows = [row for row in normalized if row.lstrip().startswith("Version:")]
    if len(version_rows) != 1:
        raise PanelParseError(
            "the Claude modal must render exactly one 'Version:' row; a truncated or "
            "duplicated panel is not an observation"
        )
    observed = normalized_version(version_rows[0].split(":", 1)[1].strip())
    if observed != pinned_version:
        raise PanelParseError(
            f"the Claude modal does not attest the pinned {pinned_version} build; "
            "a drifted build has no repair parser"
        )
    session_rows = [row for row in normalized if row.lstrip().startswith("Session ID:")]
    if len(session_rows) != 1:
        raise PanelParseError(
            "the Claude modal must render exactly one 'Session ID:' row; a missing, "
            "duplicate, or stale prior panel cannot prove the session it names"
        )
    session_id = _canonical_uuid(
        session_rows[0].split(":", 1)[1].strip(), label="Claude Session ID"
    )
    return {
        "parser_key": PARSER_CLAUDE_MODAL,
        "provider_version": observed,
        "session_id": session_id,
    }


def parse_codex_status(rows: Sequence[str], *, pinned_version: str = "0.147.0") -> dict[str, Any]:
    """Parse the Codex 0.147.0 status panel.

    Requires the branded ``>_ OpenAI Codex (v0.147.0)`` header and exactly
    one ``Session: <uuid>`` row.
    """
    normalized = normalize_capture_rows(rows)
    observed = _require_brand_header(
        normalized,
        brand="OpenAI Codex",
        expected_version=pinned_version,
        panel_name="Codex",
    )
    session_rows = [row for row in normalized if row.lstrip().startswith("Session:")]
    if len(session_rows) != 1:
        raise PanelParseError("the Codex status panel must render exactly one 'Session:' row")
    session_id = _canonical_uuid(session_rows[0].split(":", 1)[1].strip(), label="Codex Session")
    return {
        "parser_key": PARSER_CODEX_STATUS,
        "provider_version": observed,
        "session_id": session_id,
    }


def parse_kimi_status(rows: Sequence[str], *, pinned_version: str = "0.34.0") -> dict[str, Any]:
    """Parse the Kimi 0.34.0 status panel.

    Requires the branded ``>_ Kimi Code (v0.34.0)`` header and either a
    live canonical ``Session session_<uuid>`` row or the exact ``Session
    none`` missing-ID marker.  ``Session nonsense`` and ``Session -`` are
    malformed and refused; nothing is fabricated.
    """
    normalized = normalize_capture_rows(rows)
    observed = _require_brand_header(
        normalized,
        brand="Kimi Code",
        expected_version=pinned_version,
        panel_name="Kimi",
    )
    session_rows = [row for row in normalized if row.startswith("Session session_")]
    if len(session_rows) > 1:
        raise PanelParseError(
            "the Kimi status panel must render exactly one 'Session session_<uuid>' row"
        )
    if session_rows:
        raw = session_rows[0][len("Session ") :].strip()
        uuid_part = raw[len("session_") :] if raw.startswith("session_") else raw
        _canonical_uuid(uuid_part, label="Kimi session id")
        return {
            "parser_key": PARSER_KIMI_STATUS,
            "provider_version": observed,
            "session_id": raw,
        }
    none_rows = [row for row in normalized if row == "Session none"]
    if len(none_rows) == 1:
        return {
            "parser_key": PARSER_KIMI_STATUS,
            "provider_version": observed,
            "identity_still_missing": True,
        }
    if len(none_rows) > 1:
        raise PanelParseError("the Kimi status panel renders more than one 'Session none' row")
    if any(row.startswith("Session ") for row in normalized):
        raise PanelParseError(
            "the Kimi status panel's Session row is neither a canonical session id "
            "nor the exact 'Session none' missing-id marker"
        )
    raise PanelParseError("the Kimi status panel renders no Session row at all")


def parse_muse_status(rows: Sequence[str], *, pinned_version: str = "0.1.0") -> dict[str, Any]:
    """Parse the Muse 0.1.0 panel.

    Requires the branded ``>_ Muse Code (0.1.0)`` header plus the strict
    status labels.  The launch's pre-task gate (zero-turn) is deliberately
    NOT reused: a legacy pane has worked, and the panel still names the
    session it runs.  Only the session identity is taken, validated as a
    canonical UUID.  Panel-side errors are converted to bounded messages
    that never carry raw field values.
    """
    from cli_agent_orchestrator.services import muse_native_status

    normalized = normalize_capture_rows(rows)
    observed = _require_brand_header(
        normalized,
        brand="Muse Code",
        expected_version=pinned_version,
        panel_name="Muse",
    )
    try:
        parsed = muse_native_status.parse_status_panel(normalized)
        session_id = muse_native_status.validate_discovered_session_id(parsed["session_id"])
    except (muse_native_status.MuseStatusParseError, muse_native_status.MuseStatusMismatch):
        raise PanelParseError(
            "the Muse status panel is incomplete, ambiguous, or truncated and does "
            "not name a usable session identity"
        ) from None
    return {
        "parser_key": PARSER_MUSE_PANEL,
        "provider_version": observed,
        "session_id": session_id,
    }


#: The provider interaction plans: which parser runs and whether the modal
#: needs its single Escape.  A build that was never read has no plan here
#: and therefore no repair parser: an unproven build is refused, never
#: guessed at with a generic regex.  Caller/provider metadata may select
#: the plan; the panel-attested build is what gets recorded.
_REPAIR_PARSER_PLANS: dict[str, dict[str, Any]] = {
    "claude_code": {
        "parser_key": PARSER_CLAUDE_MODAL,
        "parse": parse_claude_status,
        "escape": True,
        "supported_versions": ("2.1.226",),
    },
    "codex": {
        "parser_key": PARSER_CODEX_STATUS,
        "parse": parse_codex_status,
        "escape": False,
        "supported_versions": ("0.147.0",),
    },
    "kimi_cli": {
        "parser_key": PARSER_KIMI_STATUS,
        "parse": parse_kimi_status,
        "escape": False,
        "supported_versions": ("0.34.0",),
    },
    "muse_cli": {
        "parser_key": PARSER_MUSE_PANEL,
        "parse": parse_muse_status,
        "escape": False,
        "supported_versions": ("0.1.0",),
    },
}


def _resolve_plan(
    provider: str, provider_version: Optional[str]
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Select the pre-status interaction plan, or a typed refusal reason.

    ``provider_version`` is caller/provider metadata that selects the plan
    only; the panel-attested build is recorded from the parse.  A legacy
    row with no durable version metadata selects the provider's pinned
    plan, so legacy usefulness is never blocked on missing metadata.
    """
    plan = _REPAIR_PARSER_PLANS.get(provider)
    if plan is None:
        return None, "provider-unsupported"
    if provider_version:
        version = normalized_version(provider_version)
        if version not in plan["supported_versions"]:
            return None, "unsupported-build"
        return dict(plan, plan_version=version), None
    return dict(plan, plan_version=plan["supported_versions"][0]), None


# ---------------------------------------------------------------------------
# Terminal-row access (v2 vintage first, then the shared table)
# ---------------------------------------------------------------------------


def _terminal_row_from(session: Any, terminal_id: str) -> Optional[dict[str, Any]]:
    """The terminal row as a plain dict, v2 vintage first, or None.

    The v2 row lives only in ``managed_launch_v2_terminals``; the shared
    ``terminals`` row covers legacy launches.  The dict retains the
    ``callback_target_generation``, the current ``native_session_id``, and
    the ``vintage`` provenance needed for exact decisions.
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
            "callback_target_generation": None,
            "native_session_id": row.v2_native_session_id,
            "lifecycle_state": row.v2_lifecycle_state,
            "pane_id": row.pane_id,
            "window_id": row.window_id,
            "session_id": row.v2_session_id,
            "server_socket_path": row.server_socket_path,
            "pane_pid": row.v2_pane_pid,
            "tmux_session": row.tmux_session,
            "tmux_window": row.tmux_window,
            "vintage": "v2",
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
        "callback_target_generation": row.callback_target_generation,
        "native_session_id": row.native_session_id,
        "lifecycle_state": row.lifecycle_state,
        "pane_id": row.pane_id,
        "window_id": row.window_id,
        "session_id": row.session_id,
        "server_socket_path": row.server_socket_path,
        "pane_pid": row.pane_pid,
        "tmux_session": row.tmux_session,
        "tmux_window": row.tmux_window,
        "vintage": "legacy",
    }


def _load_terminal_row(terminal_id: str) -> Optional[dict[str, Any]]:
    with database.SessionLocal() as db:
        return _terminal_row_from(db, terminal_id)


def _resolve_occurrence(
    row: Mapping[str, Any], expected_generation: Optional[str]
) -> tuple[Optional[str], str]:
    """Resolve (model_generation, physical_occurrence) or raise a typed
    refusal.

    "Managed" means the row carries a model generation (a v2 row always
    does; a v1 ``terminals`` row may).  A managed row requires the expected
    model generation and binds its occurrence to it.  A legacy row
    (``generation is None``) refuses a supplied expected generation and
    binds its physical occurrence to the durable callback-target
    generation.  A legacy row never accepts an arbitrary physical
    generation as a model generation.
    """
    managed = row["generation"] is not None
    if managed:
        if not expected_generation:
            raise NativeStatusRepairConflict(
                "generation-required",
                "a managed terminal requires its exact model generation",
            )
        if row["generation"] != expected_generation:
            raise NativeStatusRepairConflict(
                "generation-mismatch",
                f"terminal {row['id']} holds model generation {row['generation']!r}, "
                f"not the exact {expected_generation!r}",
            )
        return expected_generation, expected_generation
    if expected_generation is not None:
        raise NativeStatusRepairConflict(
            "generation-mismatch",
            f"terminal {row['id']} is a legacy row with no model generation; a "
            "supplied expected generation is refused",
        )
    occurrence = row["callback_target_generation"]
    if not occurrence:
        raise NativeStatusRepairConflict(
            "callback-target-missing",
            f"legacy terminal {row['id']} has no pane-bound callback-target "
            "generation; nothing can be bound without it",
        )
    return None, occurrence


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
    model_generation: Optional[str],
    occurrence: str,
    provider: str,
    pane_id: str,
    window_id: str,
    session_id: str,
    server_socket_path: str,
    pane_pid: int,
    process_identity: Mapping[str, Any],
    expected_session_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Every exact fact must still match, immediately before any mutation.

    ``expected_session_id`` is supplied only at commit time: the lineage
    must be ``identity_missing`` or already bound to exactly this id — a
    different stored id is a typed conflict and is never overwritten.
    Returns the lineage dict so the caller can run the known-identity
    preflight without a second read.
    """
    row = _terminal_row_from(session, terminal_id)
    if row is None:
        raise NativeStatusRepairConflict("terminal-not-found", "the terminal row is gone")
    managed = row["generation"] is not None
    if managed:
        if model_generation is None or row["generation"] != model_generation:
            raise NativeStatusRepairConflict(
                "generation-mismatch",
                f"terminal {terminal_id} no longer holds the expected model generation",
            )
        if occurrence != model_generation:
            raise NativeStatusRepairConflict(
                "generation-mismatch",
                "the managed physical occurrence must be the model generation",
            )
    else:
        if row["generation"] is not None:
            raise NativeStatusRepairConflict(
                "generation-mismatch",
                f"terminal {terminal_id} now carries a model generation it did not "
                "have when the repair was called",
            )
        if row["callback_target_generation"] != occurrence:
            raise NativeStatusRepairConflict(
                "generation-mismatch",
                "the legacy callback-target generation no longer matches the "
                "occurrence this repair was called for",
            )
    if row["lifecycle_state"] != "live":
        raise NativeStatusRepairConflict(
            "terminal-not-live",
            f"terminal {terminal_id} is {row['lifecycle_state']!r}, not live",
        )
    if row["provider"] != provider:
        raise NativeStatusRepairConflict(
            "provider-drift",
            f"terminal {terminal_id} now runs a different provider",
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
    incarnation = roster.get_incarnation_by_terminal(
        terminal_id, generation=model_generation, db=session
    )
    if incarnation is None:
        raise NativeStatusRepairConflict(
            "no-roster-incarnation",
            f"no stable-agent incarnation is recorded for terminal {terminal_id} "
            "for this occurrence",
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
                f"the lineage belongs to a different harness; native ids never cross "
                "harness domains and this repair is refused",
            )
        if expected_session_id is not None:
            if (
                lineage.native_session_id is not None
                and lineage.native_session_id != expected_session_id
            ):
                raise NativeStatusRepairConflict(
                    "identity-conflict",
                    "the lineage is already bound to a different native session; "
                    "repairing it would overwrite a known identity",
                )
    return {
        "lineage_id": incarnation.get("lineage_id"),
        "native_session_id": lineage.native_session_id if lineage is not None else None,
        "agent_id": incarnation.get("agent_id"),
    }


def _verify_live_pane(
    *,
    pane_id: str,
    window_id: str,
    session_id: str,
    server_socket_path: str,
    pane_pid: int,
    process_identity: Mapping[str, Any],
    operation_id: str,
) -> None:
    """Prove the exact stored pane/server/process is live, right now.

    Internal details (unreadable servers, marker reads) are logged under
    the operation id; the typed refusal carries only bounded text.
    """
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    try:
        client = TmuxClient()
        live = client.pane_control_identity(pane_id=pane_id)
    except Exception as exc:  # noqa: BLE001 - an unobservable pane is a refusal
        logger.warning("repair %s: pane identity observation failed: %s", operation_id, exc)
        raise NativeStatusRepairConflict(
            "pane-identity-drift", "the pane's live identity could not be observed"
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
            "the live pane tuple does not match the stored tuple; the pane moved or "
            "was recycled",
        )
    try:
        server = client.observe_pane_server_identity(pane_id)
    except Exception as exc:  # noqa: BLE001 - an unobservable server is a refusal
        logger.warning("repair %s: server identity observation failed: %s", operation_id, exc)
        raise NativeStatusRepairConflict(
            "server-identity-drift", "the pane's server identity could not be observed"
        ) from exc
    if server is None:
        raise NativeStatusRepairConflict(
            "server-identity-drift",
            f"pane {pane_id} could not be proven to sit on the bound tmux server",
        )
    if normalize_server_identity(server_socket_path) != server:
        raise NativeStatusRepairConflict(
            "server-identity-drift", "pane {pane_id} sits on a different tmux server"
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
# Observation: readiness, /status, capture, Escape (one shared deadline)
# ---------------------------------------------------------------------------


def _await_idle_composer(
    *,
    provider: str,
    pane_id: str,
    terminal_id: str,
    session_name: str,
    window_name: str,
    deadline: float,
    operation_id: str,
) -> None:
    """Poll the provider's own turn-state detector until the composer is
    IDLE, or refuse with zero bytes typed.  Shares the one observation
    deadline with the capture and the composer proof, so a stuck pane is
    bounded once, not three times."""
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    observers = {
        "codex": npi.observe_codex_turn_state,
        "kimi_cli": npi.observe_kimi_turn_state,
        "claude_code": npi.observe_claude_turn_state,
        "muse_cli": npi.observe_muse_turn_state,
    }
    observer = observers.get(provider)
    if observer is None:
        raise NativeStatusRepairConflict("not-ready", "the provider has no turn-state observer")
    last_detail: str = "no observation was ever made"
    while True:
        try:
            status = observer(
                pane_id=pane_id,
                terminal_id=terminal_id,
                session_name=session_name,
                window_name=window_name,
            )
        except Exception as exc:  # noqa: BLE001 - an unread pane is not ready
            logger.debug("repair %s: readiness read failed: %s", operation_id, exc)
            last_detail = "the pane could not be read"
        else:
            if status == TerminalStatus.IDLE:
                return
            last_detail = f"provider status {status.value!r}"
        if time.monotonic() >= deadline:
            raise NativeStatusRepairConflict(
                "not-ready",
                "the provider composer never became idle within the observation "
                f"bound; zero status bytes were typed (last: {last_detail})",
            )
        time.sleep(v2._NATIVE_PANE_READY_POLL_SECONDS)


def _capture_panel_verdict(
    provider: str, pane_id: str, plan: Mapping[str, Any], deadline: float, operation_id: str
) -> dict[str, Any]:
    """Capture until the pinned parser renders a verdict, or refuse.

    One ``/status`` was already submitted.  The observation is bounded by
    the shared deadline and never retyped: a second ``/status`` after a
    first landed would render a second panel and make the capture
    ambiguous, which the parser refuses rather than guesses at.
    """
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    last_error: Optional[str] = None
    while True:
        try:
            rows = list(npi.capture_pane_screen(pane_id))
        except Exception as exc:  # noqa: BLE001 - an unread pane is not a parsed panel
            logger.debug("repair %s: panel capture failed: %s", operation_id, exc)
            last_error = "the pane's rendered screen could not be captured"
        else:
            try:
                parsed = plan["parse"](rows, **{"pinned_version": plan["plan_version"]})
            except PanelParseError as exc:
                last_error = str(exc)
            else:
                if parsed.get("identity_still_missing"):
                    return {
                        "kind": "still-missing",
                        "provider_version": parsed["provider_version"],
                    }
                return {
                    "kind": "id",
                    "session_id": parsed["session_id"],
                    "provider_version": parsed["provider_version"],
                    "parser_key": plan["parser_key"],
                    "evidence_sha256": evidence_digest(rows),
                    "observed_at": _now(),
                }
        if time.monotonic() >= deadline:
            raise NativeStatusRepairConflict(
                "panel-unparsed",
                "the /status panel never rendered a usable identity within the "
                f"observation bound; last observation: {last_error or 'no capture was ever made'}",
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


def _prove_composer_restored(pane_id: str, deadline: float, operation_id: str) -> bool:
    """Bounded poll for the styled composer proof after the single Escape.

    Reads the styled capture (``-e``) exactly as the canary did; the
    proof is the rendered composer boundary, not an assumption that the
    key was accepted.  Never raises for a failed proof — it returns False
    and the caller refuses rather than reporting readiness.
    """
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    while True:
        try:
            rows = list(npi.capture_pane_screen_styled(pane_id))
        except Exception as exc:  # noqa: BLE001 - an unread pane is an unproven composer
            logger.debug("repair %s: composer proof capture failed: %s", operation_id, exc)
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
    occurrence: str,
    pane_id: str,
    process_identity: Mapping[str, Any],
    operation_id: str,
    request_digest: str,
    plan: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """A prior status-repair adoption of this exact owner, or None.

    The exact-retry convergence path: when an attachment already claims
    this exact running pane/process (verified live above) with a fully
    validated status-repair receipt, the identity it names was observed
    under this exact process and can be reused without another
    ``/status``.  A superficially matching receipt is not enough — the
    schema, request digest, physical owner, panel-attested version, and
    parser key must all hold, and a Claude plan additionally requires the
    composer-restored proof.  Anything ambiguous falls through to a fresh
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
        and record["owner"].get("generation") == occurrence
        and record["owner"].get("pane_id") == pane_id
        and record["owner"].get("process_identity") == dict(process_identity)
        and isinstance(record.get("adoption_receipt"), Mapping)
    ]
    if len(matches) != 1:
        return None
    receipt = matches[0]["adoption_receipt"]
    if receipt.get("schema") != native_attachment.STATUS_REPAIR_ADOPTION_SCHEMA:
        return None
    if receipt.get("request_digest") != request_digest:
        return None
    if receipt.get("provider_version") != plan["plan_version"]:
        return None
    if receipt.get("parser_key") != plan["parser_key"]:
        return None
    if plan.get("escape") and receipt.get("composer_restored") is not True:
        return None
    if not receipt.get("native_session_id") or not receipt.get("evidence_sha256"):
        return None
    return {
        "session_id": receipt["native_session_id"],
        "parser_key": receipt["parser_key"],
        "provider_version": receipt["provider_version"],
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
    occurrence: str,
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
        generation=occurrence,
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
            generation=occurrence,
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


def _commit_repair(db: Any, facts: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The atomic row + roster + evidence commit.

    Every exact fact is re-verified inside this transaction immediately
    before the writes, so a drift between observation and commit refuses
    with zero mutation.  Same id replays idempotently; a different stored
    id is a typed conflict and is never overwritten.  A roster failure
    rolls the terminal row and the evidence back with it.  Returns the
    recorded evidence when a concurrent exact retry committed the same
    operation id first (the idempotent-adopt case), else None.
    """
    _verify_exact_facts(
        db,
        terminal_id=facts["terminal_id"],
        model_generation=facts["model_generation"],
        occurrence=facts["occurrence"],
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
        terminal_id=facts["terminal_id"],
        expected_generation=facts["model_generation"],
        physical_occurrence=facts["occurrence"],
        native_session_id=facts["session_id"],
        db=db,
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
            generation=facts["model_generation"],
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
            generation=facts["occurrence"],
            provider=facts["provider"],
            provider_version=facts["provider_version"],
            native_session_id=facts["session_id"],
            parser_key=facts["parser_key"],
            evidence_sha256=facts["evidence_sha256"],
            observed_at=facts["observed_at"],
            created_at=_now(),
        )
    )
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - a unique-key conflict is resolved below
        db.rollback()
        existing = _evidence_by_operation(facts["operation_id"])
        if existing is None:
            raise
        if existing["request_digest"] != facts["request_digest"]:
            raise NativeStatusRepairConflict(
                "operation-conflict",
                "the operation id is already bound to a different request digest",
            )
        return existing
    return None


def _evidence_by_operation(operation_id: str) -> Optional[dict[str, Any]]:
    with database.SessionLocal() as db:
        row = (
            db.query(database.NativeStatusRepairEvidenceModel)
            .filter(database.NativeStatusRepairEvidenceModel.operation_id == operation_id)
            .first()
        )
        if row is None:
            return None
        return {
            "operation_id": row.operation_id,
            "request_digest": row.request_digest,
            "terminal_id": row.terminal_id,
            "generation": row.generation,
            "provider": row.provider,
            "provider_version": row.provider_version,
            "native_session_id": row.native_session_id,
            "parser_key": row.parser_key,
            "evidence_sha256": row.evidence_sha256,
            "observed_at": row.observed_at,
        }


# ---------------------------------------------------------------------------
# The operation
# ---------------------------------------------------------------------------


def canonical_request_digest(
    *, terminal_id: str, generation: Optional[str], provider_version: Optional[str]
) -> str:
    """The canonical digest of the immutable operation inputs."""
    return hashlib.sha256(
        "\x00".join((terminal_id, generation or "", provider_version or "")).encode("utf-8")
    ).hexdigest()


def repair_terminal_native_identity(
    *,
    terminal_id: str,
    generation: Optional[str] = None,
    provider_version: Optional[str] = None,
    operation_id: str,
    caller: str = "cao.native-status-repair",
) -> dict[str, Any]:
    """Repair one currently live rostered terminal's native session id.

    ``generation`` is the *expected model generation*: required for
    managed/v2 rows and must equal ``row.generation``; legacy rows have
    none and must not be passed one (their physical occurrence is the
    ``callback_target_generation``).  ``operation_id`` is an explicit
    canonical UUID bound to a server-derived digest of the immutable
    inputs, so an exact retry is idempotent and a changed request is a
    typed conflict.

    The operation is serialized under the canonical lifecycle claim set
    (model-generation, callback-target-generation, pane) that terminal
    teardown itself takes, and the per-pane input lease, for its whole run
    — including the Claude Escape and the post-Escape composer proof.  It
    never calls Stop, Pause, reincarnation, or task delivery, and never
    tears down the pane; Stop/delete is boundedly serialized behind the
    shared claims until provider cleanup and the commit finish.
    """
    if not terminal_id or not operation_id:
        return {
            "schema": REPAIR_SCHEMA,
            "status": STATUS_REFUSED,
            "reason": "invalid-input",
            "detail": "terminal_id and operation_id are both required",
            "operation_id": operation_id,
            "request_digest": canonical_request_digest(
                terminal_id=terminal_id, generation=generation, provider_version=provider_version
            ),
            "terminal_id": terminal_id,
            "generation": generation,
            "model_generation": generation,
            "provider": None,
            "provider_version": normalized_version(provider_version) if provider_version else None,
            "native_session_id": None,
            "evidence_sha256": None,
            "parser_key": None,
            "attachment": None,
            "composer_restored": None,
            "task_bytes_submitted": False,
        }
    if _is_invalid_uuid(operation_id):
        return {
            "schema": REPAIR_SCHEMA,
            "status": STATUS_REFUSED,
            "reason": "invalid-input",
            "detail": "operation_id must be a canonical lowercase UUID",
            "operation_id": operation_id,
            "request_digest": canonical_request_digest(
                terminal_id=terminal_id, generation=generation, provider_version=provider_version
            ),
            "terminal_id": terminal_id,
            "generation": generation,
            "model_generation": generation,
            "provider": None,
            "provider_version": normalized_version(provider_version) if provider_version else None,
            "native_session_id": None,
            "evidence_sha256": None,
            "parser_key": None,
            "attachment": None,
            "composer_restored": None,
            "task_bytes_submitted": False,
        }
    req_digest = canonical_request_digest(
        terminal_id=terminal_id, generation=generation, provider_version=provider_version
    )

    # Operation-id idempotency: a completed exact retry adopts the recorded
    # evidence with no pane I/O; a changed immutable request is a typed
    # conflict before anything touches the pane.
    prior_evidence = _evidence_by_operation(operation_id)
    if prior_evidence is not None:
        return _evidence_outcome(
            prior_evidence,
            terminal_id=terminal_id,
            generation=generation,
            operation_id=operation_id,
            request_digest=req_digest,
        )

    base: dict[str, Any] = {
        "schema": REPAIR_SCHEMA,
        "status": None,
        "reason": None,
        "detail": None,
        "operation_id": operation_id,
        "request_digest": req_digest,
        "terminal_id": terminal_id,
        "generation": None,
        "model_generation": generation,
        "provider": None,
        "provider_version": normalized_version(provider_version) if provider_version else None,
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

    row = _load_terminal_row(terminal_id)
    if row is None:
        return refused("terminal-not-found", f"no terminal row is recorded for {terminal_id}")
    if row["lifecycle_state"] != "live":
        return refused(
            "terminal-not-live",
            f"terminal {terminal_id} is {row['lifecycle_state']!r}, not live",
        )
    provider = row["provider"]
    plan, plan_error = _resolve_plan(provider, provider_version)
    if plan_error is not None or plan is None:
        if provider not in _REPAIR_PARSER_PLANS:
            return refused(
                "provider-unsupported",
                f"provider {provider!r} has no pinned native /status repair parser",
            )
        return refused(
            "unsupported-build",
            f"provider {provider!r} build {provider_version!r} has no pinned repair "
            "parser; an unproven build is refused, never guessed",
        )

    # A terminals-table row missing its callback-target generation: use the
    # canonical get_terminal_metadata CAS/self-heal seam.  The heal derives
    # the target from the row's own model generation when one exists
    # (pane-bound, unambiguous); a heal that could only mint a random
    # occurrence is refused rather than fabricating a physical identity.
    if row["vintage"] == "legacy" and not row["callback_target_generation"]:
        database.get_terminal_metadata(terminal_id, warn_if_missing=False)
        row = _load_terminal_row(terminal_id)
        if row is None:
            return refused("terminal-not-found", f"no terminal row is recorded for {terminal_id}")
        if not row["callback_target_generation"] or row["generation"] is None:
            return refused(
                "callback-target-missing",
                "the legacy terminal has no pane-bound callback-target generation "
                "and none could be established without ambiguity; refusing without "
                "mutating",
            )

    try:
        model_generation, occurrence = _resolve_occurrence(row, generation)
    except NativeStatusRepairError as exc:
        return refused(getattr(exc, "reason", "errored"), str(exc))
    base["generation"] = occurrence

    try:
        incarnation = roster.get_incarnation_by_terminal(terminal_id, generation=model_generation)
    except roster.StableAgentError as exc:
        logger.warning("repair %s: roster read failed: %s", operation_id, exc)
        return refused("roster-unavailable", "the roster could not be read")
    if incarnation is None:
        return refused(
            "no-roster-incarnation",
            f"no stable-agent incarnation is recorded for terminal {terminal_id} "
            "for this occurrence",
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
        with generation_lifecycle_claims(terminal_lifecycle_claim_set(row)):
            try:
                with pia.pane_input_lease(pane_id, holder=caller, timeout=0.0):
                    outcome = _repair_under_claims(
                        operation_id=operation_id,
                        request_digest=req_digest,
                        base=base,
                        plan=plan,
                        row=row,
                        model_generation=model_generation,
                        occurrence=occurrence,
                        incarnation=incarnation,
                        process_identity=process_identity,
                        pane_id=pane_id,
                        window_id=window_id,
                        tmux_session_id=tmux_session_id,
                        server_socket_path=server_socket_path,
                        pane_pid=pane_pid,
                        provider=provider,
                    )
            except pia.PaneBusyError as exc:
                logger.warning("repair %s: pane lease busy: %s", operation_id, exc)
                return refused(
                    "pane-busy",
                    f"another writer holds the pane input lease for {pane_id}; "
                    "zero bytes were written and nothing was mutated",
                )
            except pia.PaneInputArbiterError as exc:
                logger.warning("repair %s: pane lease unusable: %s", operation_id, exc)
                return refused("pane-unwritable", "the pane input lease is unusable")
    except NativeStatusRepairError as exc:
        return refused(getattr(exc, "reason", "errored"), str(exc))
    except Exception as exc:  # noqa: BLE001 - never let the operation escape untyped
        logger.exception(
            "native status repair %s for terminal %s failed unexpectedly",
            operation_id,
            terminal_id,
        )
        outcome = dict(base)
        outcome.update(
            status=STATUS_ERRORED,
            reason="errored",
            detail="the repair failed unexpectedly; see the operation log for details",
        )
        return outcome
    return outcome


def _is_invalid_uuid(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return True
    return str(parsed) != value


def _evidence_outcome(
    evidence: Mapping[str, Any],
    *,
    terminal_id: str,
    generation: Optional[str],
    operation_id: str,
    request_digest: str,
) -> dict[str, Any]:
    """The truthful reconstructed outcome of a completed exact retry."""
    if evidence["request_digest"] != request_digest:
        return {
            "schema": REPAIR_SCHEMA,
            "status": STATUS_REFUSED,
            "reason": "operation-conflict",
            "detail": "the operation id is already bound to a different request digest",
            "operation_id": operation_id,
            "request_digest": request_digest,
            "terminal_id": terminal_id,
            "generation": evidence.get("generation"),
            "model_generation": generation,
            "provider": evidence["provider"],
            "provider_version": evidence["provider_version"],
            "native_session_id": evidence["native_session_id"],
            "evidence_sha256": evidence["evidence_sha256"],
            "parser_key": evidence["parser_key"],
            "attachment": None,
            "composer_restored": None,
            "task_bytes_submitted": False,
        }
    return {
        "schema": REPAIR_SCHEMA,
        "status": STATUS_REPAIRED,
        "reason": None,
        "detail": "exact retry of a completed repair; the recorded evidence is adopted",
        "operation_id": operation_id,
        "request_digest": request_digest,
        "terminal_id": terminal_id,
        "generation": evidence.get("generation"),
        "model_generation": generation,
        "provider": evidence["provider"],
        "provider_version": evidence["provider_version"],
        "native_session_id": evidence["native_session_id"],
        "evidence_sha256": evidence["evidence_sha256"],
        "parser_key": evidence["parser_key"],
        "attachment": None,
        "composer_restored": None,
        "task_bytes_submitted": False,
    }


def _repair_under_claims(
    *,
    operation_id: str,
    request_digest: str,
    base: dict[str, Any],
    plan: Mapping[str, Any],
    row: Mapping[str, Any],
    model_generation: Optional[str],
    occurrence: str,
    incarnation: Mapping[str, Any],
    process_identity: Mapping[str, Any],
    pane_id: str,
    window_id: str,
    tmux_session_id: str,
    server_socket_path: str,
    pane_pid: int,
    provider: str,
) -> dict[str, Any]:
    """The observation and persistence, run under the canonical lifecycle
    claims and the pane input lease."""
    terminal_id = row["id"]
    # Re-verify every exact fact now that the claims are held: drift
    # between load and claim is drift, and drift means zero bytes.
    with database.SessionLocal() as db:
        lineage = _verify_exact_facts(
            db,
            terminal_id=terminal_id,
            model_generation=model_generation,
            occurrence=occurrence,
            provider=provider,
            pane_id=pane_id,
            window_id=window_id,
            session_id=tmux_session_id,
            server_socket_path=server_socket_path,
            pane_pid=pane_pid,
            process_identity=process_identity,
        )

    # Known-identity preflight before bytes: no /status is ever typed when
    # the identity is already known, conflicting, or un-attached.
    preflight = _known_identity_preflight(
        provider=provider,
        terminal_id=terminal_id,
        terminal_known=row["native_session_id"],
        lineage_known=lineage["native_session_id"] if lineage else None,
    )
    if preflight["kind"] == "already-known":
        outcome = dict(base)
        outcome.update(
            status=STATUS_ALREADY_KNOWN,
            reason=None,
            detail=(
                "the identity is already known and attached; nothing was typed, "
                "nothing was recorded, and nothing was mutated"
            ),
            provider=provider,
            native_session_id=preflight["session_id"],
        )
        return outcome
    if preflight["kind"] == "attachment-unresolved":
        raise NativeStatusRepairConflict(
            "attachment-unresolved",
            "the identity is already known but no attachment records it; a later "
            "attachment audit/migration owns that concern, not this bounded "
            "missing-identity repair",
        )
    if preflight["kind"] == "conflict":
        raise NativeStatusRepairConflict(
            "identity-conflict",
            "the terminal row and the roster lineage already know different native "
            "session ids; a repair never chooses between them",
        )
    known_id = preflight["known_id"]

    _verify_live_pane(
        pane_id=pane_id,
        window_id=window_id,
        session_id=tmux_session_id,
        server_socket_path=server_socket_path,
        pane_pid=pane_pid,
        process_identity=process_identity,
        operation_id=operation_id,
    )

    # The provider composer must be idle/ready before anything is typed.
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    observation_deadline = time.monotonic() + v2.NATIVE_PANE_READY_TIMEOUT_SECONDS
    _await_idle_composer(
        provider=provider,
        pane_id=pane_id,
        terminal_id=terminal_id,
        session_name=row["tmux_session"],
        window_name=row["tmux_window"] or f"w-{terminal_id}",
        deadline=observation_deadline,
        operation_id=operation_id,
    )

    # Exact-retry convergence: a prior fully-validated status-repair
    # adoption already names this exact verified owner, so no second
    # /status is needed.
    prior = _prior_adoption_facts(
        provider=provider,
        terminal_id=terminal_id,
        occurrence=occurrence,
        pane_id=pane_id,
        process_identity=process_identity,
        operation_id=operation_id,
        request_digest=request_digest,
        plan=plan,
    )
    if prior is not None:
        if known_id is not None and prior["session_id"] != known_id:
            raise NativeStatusRepairConflict(
                "identity-conflict",
                "the prior adoption names a different id than the already-known "
                "identity; nothing was mutated",
            )
        return _finish_repair(
            operation_id=operation_id,
            request_digest=request_digest,
            base=base,
            plan=plan,
            row=row,
            model_generation=model_generation,
            occurrence=occurrence,
            process_identity=process_identity,
            pane_id=pane_id,
            window_id=window_id,
            tmux_session_id=tmux_session_id,
            server_socket_path=server_socket_path,
            pane_pid=pane_pid,
            provider=provider,
            session_id=prior["session_id"],
            parser_key=prior["parser_key"],
            provider_version=prior["provider_version"],
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
        logger.warning("repair %s: /status write refused: %s", operation_id, exc)
        raise NativeStatusRepairConflict(
            "pane-unwritable", "the /status write was refused by tmux"
        ) from exc

    # From here the /status has been submitted.  For the Claude modal the
    # single Escape and the post-Escape composer proof run in a finally
    # that preserves the primary failure on every path below: success,
    # parse failure, capture failure, timeout, persistence failure, and
    # cancellation.  The claims and the pane lease stay held the whole time.
    cleanup_error: Optional[BaseException] = None
    composer_restored: Optional[bool] = None

    def _escape_finally() -> None:
        nonlocal cleanup_error, composer_restored
        try:
            typed.send_key("Escape")
            composer_restored = _prove_composer_restored(
                pane_id, deadline=observation_deadline, operation_id=operation_id
            )
        except BaseException as exc:  # noqa: BLE001 - cleanup never masks the primary
            cleanup_error = exc

    try:
        verdict = _capture_panel_verdict(
            provider, pane_id, plan, deadline=observation_deadline, operation_id=operation_id
        )
        if verdict["kind"] == "still-missing":
            if known_id is not None:
                raise NativeStatusRepairConflict(
                    "identity-conflict",
                    "a known native id exists but the Kimi panel renders no session; "
                    "the known id could not be verified and nothing was mutated",
                )
            outcome = dict(base)
            outcome.update(
                status=STATUS_IDENTITY_STILL_MISSING,
                reason=STATUS_IDENTITY_STILL_MISSING,
                detail=(
                    "the Kimi /status panel renders 'Session none' before the first "
                    "session-creating action. Nothing was recorded and no id was "
                    "fabricated."
                ),
                provider=provider,
                provider_version=verdict["provider_version"],
            )
            return outcome
    except NativeStatusRepairError:
        raise
    finally:
        if plan.get("escape"):
            _escape_finally()

    # The finally has run.  A failed cleanup never becomes success: without
    # the post-Escape styled composer proof nothing is committed.
    if plan.get("escape") and composer_restored is not True:
        detail = "the post-Escape styled composer proof did not succeed, so the pane "
        "is not proven ready and nothing was committed"
        if cleanup_error is not None:
            detail += " (the Escape cleanup itself failed)"
        raise NativeStatusRepairConflict("composer-not-restored", detail)

    session_id = verdict["session_id"]
    if known_id is not None and session_id != known_id:
        raise NativeStatusRepairConflict(
            "identity-conflict",
            "the panel names a different id than the already-known identity; "
            "durable state was left unchanged",
        )

    return _finish_repair(
        operation_id=operation_id,
        request_digest=request_digest,
        base=base,
        plan=plan,
        row=row,
        model_generation=model_generation,
        occurrence=occurrence,
        process_identity=process_identity,
        pane_id=pane_id,
        window_id=window_id,
        tmux_session_id=tmux_session_id,
        server_socket_path=server_socket_path,
        pane_pid=pane_pid,
        provider=provider,
        session_id=session_id,
        parser_key=verdict["parser_key"],
        provider_version=verdict["provider_version"],
        evidence_sha256=verdict["evidence_sha256"],
        observed_at=verdict["observed_at"],
        composer_restored=composer_restored,
    )


def _known_identity_preflight(
    *,
    provider: str,
    terminal_id: str,
    terminal_known: Optional[str],
    lineage_known: Optional[str],
) -> dict[str, Any]:
    """The zero-byte identity decision: already-known, conflict,
    attachment-unresolved, or proceed (possibly to verify a known id)."""
    if terminal_known and lineage_known:
        if terminal_known != lineage_known:
            return {"kind": "conflict"}
        if native_attachment.get(provider, terminal_known) is None:
            return {"kind": "attachment-unresolved"}
        return {"kind": "already-known", "session_id": terminal_known}
    return {"kind": "proceed", "known_id": terminal_known or lineage_known}


def _finish_repair(
    *,
    operation_id: str,
    request_digest: str,
    base: dict[str, Any],
    plan: Mapping[str, Any],
    row: Mapping[str, Any],
    model_generation: Optional[str],
    occurrence: str,
    process_identity: Mapping[str, Any],
    pane_id: str,
    window_id: str,
    tmux_session_id: str,
    server_socket_path: str,
    pane_pid: int,
    provider: str,
    session_id: str,
    parser_key: str,
    provider_version: str,
    evidence_sha256: str,
    observed_at: str,
    composer_restored: Optional[bool],
) -> dict[str, Any]:
    """Adopt the exclusive owner, then commit row+roster+evidence
    atomically.  Attachment adoption commits first; if the atomic repair
    later fails, the conservative attachment remains and an exact retry
    can finish it."""
    terminal_id = row["id"]

    record, adopted = _adopt_running_owner(
        operation_id=operation_id,
        request_digest=request_digest,
        provider=provider,
        session_id=session_id,
        terminal_id=terminal_id,
        occurrence=occurrence,
        pane_id=pane_id,
        process_identity=process_identity,
        parser_key=parser_key,
        provider_version=provider_version,
        evidence_sha256=evidence_sha256,
        observed_at=observed_at,
        composer_restored=composer_restored,
    )

    facts = {
        "operation_id": operation_id,
        "request_digest": request_digest,
        "terminal_id": terminal_id,
        "model_generation": model_generation,
        "occurrence": occurrence,
        "provider": provider,
        "provider_version": provider_version,
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
            adopted_evidence = _commit_repair(db, facts)
    except NativeStatusRepairError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed, never half-repair
        logger.exception("repair %s: atomic commit failed: %s", operation_id, exc)
        raise NativeStatusRepairUnavailable(
            "the terminal-row and roster repair did not commit; the conservative "
            "attachment adoption remains and an exact retry can finish it"
        ) from exc
    if adopted_evidence is not None:
        return _evidence_outcome(
            adopted_evidence,
            terminal_id=terminal_id,
            generation=model_generation,
            operation_id=operation_id,
            request_digest=request_digest,
        )

    outcome = dict(base)
    outcome.update(
        status=STATUS_REPAIRED,
        reason=None,
        provider=provider,
        provider_version=provider_version,
        native_session_id=session_id,
        evidence_sha256=evidence_sha256,
        parser_key=parser_key,
        composer_restored=composer_restored if plan.get("escape") else None,
        attachment={
            "state": record["state"],
            "owner": record["owner"],
            "adoption_receipt": record.get("adoption_receipt"),
            "adopted": adopted,
        },
    )
    return outcome
