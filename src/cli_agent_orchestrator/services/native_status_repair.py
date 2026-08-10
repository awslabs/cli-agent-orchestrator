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
import json
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
    provider: str,
    provider_version: Optional[str],
    durable_version: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Select the pre-status interaction plan, or a typed refusal reason.

    ``provider_version`` is caller/provider metadata that selects the plan
    only; the panel-attested build is recorded from the parse.  A
    managed-v2 ``durable_version`` from the reservation binding is
    authoritative where present: the caller's version must agree with it,
    and the panel-attested build must match it (enforced by the parser
    header).  A legacy row with no durable version selects the provider's
    pinned plan, so legacy usefulness is never blocked on missing metadata.
    """
    plan = _REPAIR_PARSER_PLANS.get(provider)
    if plan is None:
        return None, "provider-unsupported"
    caller_version = normalized_version(provider_version) if provider_version else None
    if durable_version:
        # The durable managed-v2 binding is authoritative: the caller's
        # version must agree with it (a disagreement is version-drift even
        # when the caller's version is itself unproven), and the plan runs
        # the durable build.
        durable = normalized_version(durable_version)
        if durable not in plan["supported_versions"]:
            return None, "unsupported-build"
        if caller_version and caller_version != durable:
            return None, "version-drift"
        return dict(plan, plan_version=durable), None
    if caller_version:
        if caller_version not in plan["supported_versions"]:
            return None, "unsupported-build"
        return dict(plan, plan_version=caller_version), None
    return dict(plan, plan_version=plan["supported_versions"][0]), None


#: The exact schema of a managed-v2 native binding; the strict binding
#: reader accepts nothing else.
BINDING_SCHEMA = "cao-managed-v2-native-binding-v1"


def _valid_native_id_for_provider(provider: str, native_id: Any) -> bool:
    """Whether ``native_id`` is a canonical session id for ``provider``
    (a ``session_<uuid>`` for Kimi, a canonical lowercase UUID otherwise)."""
    if not isinstance(native_id, str) or not native_id:
        return False
    value = native_id
    if provider == "kimi_cli":
        if not value.startswith("session_"):
            return False
        value = value[len("session_") :]
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return str(parsed) == value


def _load_validated_binding(
    session: Any,
    *,
    terminal_id: str,
    model_generation: Optional[str],
    provider: str,
    require_binding: bool,
) -> Optional[dict[str, Any]]:
    """The strictly validated managed-v2 binding for a v2 terminal, or None
    for an ordinary/legacy row that does not require one.

    ``require_binding`` is true only for a v2 terminal.  A v2 row whose
    exact reservation is absent, unbound, malformed, or incomplete fails
    closed with a typed bounded binding refusal: a pre-bind process is
    never typed into, and the repair never falls back to an unversioned
    legacy plan.  Legacy and v1-managed rows never consume a stale v2
    reservation whose terminal id merely collides.
    """
    if not require_binding:
        return None
    row = (
        session.query(database.ManagedLaunchV2ReservationModel)
        .filter(
            database.ManagedLaunchV2ReservationModel.terminal_id == terminal_id,
            database.ManagedLaunchV2ReservationModel.generation == model_generation,
        )
        .first()
    )
    if row is None:
        raise NativeStatusRepairConflict(
            "binding-unavailable",
            "the managed-v2 reservation for this terminal is absent; a pre-bind "
            "process is never typed into",
        )
    if (
        row.protocol_vintage != "v2"
        or row.provider != provider
        or row.terminal_id != terminal_id
        or row.generation != model_generation
    ):
        raise NativeStatusRepairConflict(
            "binding-unavailable",
            "the managed-v2 reservation does not agree with the terminal occurrence",
        )
    if row.state not in ("bound", "admitted"):
        raise NativeStatusRepairConflict(
            "binding-unavailable",
            "the managed-v2 reservation is not bound; a pre-bind process is never " "typed into",
        )
    # The reservation row's own persisted execution mode must resolve
    # exactly to native_tui for this TUI-only repair.  A legacy/null
    # execution mode resolves ACP and is refused even if the binding JSON
    # carries native-looking facts — the row is authoritative, never the
    # JSON alone.
    if str(row.execution_mode or "") != em.NATIVE_TUI:
        raise NativeStatusRepairConflict(
            "binding-unavailable",
            "the managed-v2 reservation is not a native-tui reservation; a TUI-only "
            "repair is refused",
        )
    if not row.binding_json:
        raise NativeStatusRepairConflict(
            "binding-unavailable",
            "the managed-v2 reservation has no binding; a pre-bind process is never " "typed into",
        )
    try:
        binding = json.loads(str(row.binding_json))
    except (TypeError, ValueError) as exc:
        raise NativeStatusRepairConflict(
            "binding-unreadable",
            "the managed-v2 binding record is unreadable; refusing to guess its facts",
        ) from exc
    if not isinstance(binding, Mapping):
        raise NativeStatusRepairConflict(
            "binding-unreadable", "the managed-v2 binding is not a mapping"
        )
    if binding.get("schema") != BINDING_SCHEMA:
        raise NativeStatusRepairConflict(
            "binding-unreadable", "the managed-v2 binding has an unknown schema"
        )
    if binding.get("execution_mode") != em.NATIVE_TUI:
        raise NativeStatusRepairConflict(
            "binding-unreadable",
            "the managed-v2 binding is not a native-tui binding; a TUI-only repair " "is refused",
        )
    native_session_id = binding.get("native_session_id")
    if not _valid_native_id_for_provider(provider, native_session_id):
        raise NativeStatusRepairConflict(
            "binding-unreadable", "the managed-v2 binding names no valid provider session"
        )
    version = binding.get("provider_version")
    normalized = normalized_version(version) if isinstance(version, str) and version else ""
    if not normalized:
        raise NativeStatusRepairConflict(
            "binding-unreadable", "the managed-v2 binding names no provider version"
        )
    return {
        "schema": str(binding.get("schema")),
        "execution_mode": str(binding.get("execution_mode")),
        "native_session_id": str(native_session_id),
        "provider_version": normalized,
    }


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
    row: Mapping[str, Any],
    expected_generation: Optional[str],
    physical_occurrence: Optional[str],
) -> tuple[Optional[str], str]:
    """Resolve (model_generation, physical_occurrence) or raise a typed
    refusal.

    "Managed" means the row carries a model generation (a v2 row always
    does; a v1 ``terminals`` row may).  A managed row requires the expected
    model generation and binds its occurrence to it; a supplied physical
    occurrence must equal it.  A legacy row (``generation is None``)
    refuses a supplied expected generation, *requires* the caller's
    physical occurrence, and binds it only when it equals the durable
    callback-target generation.  A legacy row never accepts an arbitrary
    physical generation as a model generation.
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
        if physical_occurrence is not None and physical_occurrence != row["generation"]:
            raise NativeStatusRepairConflict(
                "generation-mismatch",
                "a supplied physical occurrence must equal the managed model generation",
            )
        return expected_generation, expected_generation
    if expected_generation is not None:
        raise NativeStatusRepairConflict(
            "generation-mismatch",
            f"terminal {row['id']} is a legacy row with no model generation; a "
            "supplied expected generation is refused",
        )
    if not physical_occurrence:
        raise NativeStatusRepairConflict(
            "physical-occurrence-required",
            f"legacy terminal {row['id']} has no model generation; the repair "
            "requires the durable callback-target physical occurrence to bind the "
            "operation, and none was supplied",
        )
    if row["callback_target_generation"] != physical_occurrence:
        raise NativeStatusRepairConflict(
            "generation-mismatch",
            "the supplied physical occurrence does not equal the legacy terminal's "
            "durable callback-target generation",
        )
    return None, physical_occurrence


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
    expected_binding_native_id: Optional[str] = None,
    expected_binding_version: Optional[str] = None,
) -> dict[str, Any]:
    """Every exact fact must still match, immediately before any mutation.

    ``expected_session_id`` is supplied only at commit time: the lineage
    and the terminal must be ``identity_missing`` or already bound to
    exactly this id — a different stored id is a typed conflict and is
    never overwritten.  ``expected_binding_native_id`` /
    ``expected_binding_version`` are the pre-claim validated managed-v2
    binding facts: the current binding must still agree exactly, or a
    ``binding-drift`` refusal is raised so the canonical digest computed
    over them stays truthful.  Returns the current terminal, lineage, and
    binding identity facts so the caller can run the known-identity
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
        # The terminal row and the lineage must both be identity_missing or
        # already bound to exactly the id about to be adopted — a known
        # different id on either side is never overwritten.
        if row["native_session_id"] is not None and row["native_session_id"] != expected_session_id:
            raise NativeStatusRepairConflict(
                "identity-conflict",
                "the terminal row is already bound to a different native session; "
                "repairing it would overwrite a known identity",
            )
        if (
            lineage is not None
            and lineage.native_session_id is not None
            and lineage.native_session_id != expected_session_id
        ):
            raise NativeStatusRepairConflict(
                "identity-conflict",
                "the lineage is already bound to a different native session; "
                "repairing it would overwrite a known identity",
            )

    # The managed-v2 binding is an immutable constraint revalidated exactly
    # like the terminal/lineage facts: a binding that drifted between the
    # pre-claim load and here would invalidate the canonical digest computed
    # over it, so it is refused rather than silently re-resolved.
    binding: Optional[dict[str, Any]] = None
    if model_generation is not None and row["vintage"] == "v2":
        binding = _load_validated_binding(
            session,
            terminal_id=terminal_id,
            model_generation=model_generation,
            provider=provider,
            require_binding=True,
        )
        assert binding is not None  # require_binding=True raises, never returns None
        current_id = binding["native_session_id"]
        current_version = binding["provider_version"]
        if expected_binding_native_id is not None and current_id != expected_binding_native_id:
            raise NativeStatusRepairConflict(
                "binding-drift",
                "the managed-v2 binding now names a different native session than "
                "the one this operation was resolved over",
            )
        if expected_binding_version is not None and current_version != expected_binding_version:
            raise NativeStatusRepairConflict(
                "binding-drift",
                "the managed-v2 binding now attests a different provider version than "
                "the one this operation was resolved over",
            )
    elif expected_binding_native_id is not None or expected_binding_version is not None:
        raise NativeStatusRepairConflict(
            "binding-drift",
            "a managed-v2 binding expected by this operation is no longer present",
        )
    return {
        "lineage_id": incarnation.get("lineage_id"),
        "native_session_id": lineage.native_session_id if lineage is not None else None,
        "terminal_native_session_id": row["native_session_id"],
        "binding_native_session_id": binding["native_session_id"] if binding else None,
        "binding_provider_version": binding["provider_version"] if binding else None,
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
            "server-identity-drift", f"pane {pane_id} sits on a different tmux server"
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
) -> dict[str, Any]:
    """A prior status-repair adoption decision: reuse, reconcile, or absent.

    The exact-retry convergence path: when an attachment already claims
    this exact running pane/process (verified live above) with a fully
    validated status-repair receipt *for this exact operation*, the
    identity it names was observed under this exact process and can be
    reused without another ``/status``.

    Distinguishes absence from a partial operation.  An exact-owner
    attachment whose receipt fails schema, request digest, operation
    binding, panel-attested version, parser key, required evidence, or (for
    a Claude plan) composer-restored proof is a *partial operation*, not
    absence: typing ``/status`` again would be unsafe, so it returns a
    ``reconcile`` verdict with zero pane bytes.  Multiple exact-owner
    matches are ambiguity, not absence.
    """

    def _reconcile(reason: str) -> dict[str, Any]:
        return {"kind": "reconcile", "reason": reason}

    try:
        records = native_attachment.list_attachments(owner_terminal_id=terminal_id)
    except native_attachment.NativeAttachmentError as exc:
        logger.warning("repair %s: attachment listing failed: %s", operation_id, exc)
        raise NativeStatusRepairConflict(
            "attachment-unavailable", "the attachment store could not be read"
        ) from exc
    exact_facts = [
        record
        for record in records
        if record["state"] in native_attachment.LIVE_STATES
        and record["provider"] == provider
        and record["owner"].get("generation") == occurrence
        and record["owner"].get("pane_id") == pane_id
        and record["owner"].get("process_identity") == dict(process_identity)
    ]
    if len(exact_facts) > 1:
        return _reconcile("multiple exact-owner attachments exist; ambiguity is not absence")
    if not exact_facts:
        return {"kind": "absent"}
    record = exact_facts[0]
    record_owner = record.get("owner") or {}
    # An exact owner whose execution mode is not native_tui (e.g. an ACP
    # owner sharing the same terminal/occurrence/pane/process) is surfaced
    # as a pre-byte reconcile, never filtered into apparent absence and
    # never discovered only after /status bytes.
    if record_owner.get("execution_mode") != em.NATIVE_TUI:
        return _reconcile(
            "the exact owner holds a different execution mode; a TUI-only repair is "
            "refused before any pane bytes"
        )

    receipt = record.get("adoption_receipt")
    if receipt is None:
        # "No status-repair adoption receipt" means exactly None.  An
        # exact live owner with no receipt is a partial cond-0377C
        # operation only when its intent claims a status-repair discovery.
        # Ordinary managed/native launches truthfully carry a validated
        # pre-launch acquisition intent and no adoption receipt: they are
        # not a partial status repair, and the exact live owner already IS
        # the duplicate-attach barrier.
        try:
            intent = native_attachment.validate_attachment_intent(record.get("intent"))
        except native_attachment.NativeAttachmentInvalid as exc:
            logger.warning("repair %s: ordinary attachment intent invalid: %s", operation_id, exc)
            return _reconcile(
                "the exact-owner attachment's acquisition intent is invalid or unknown"
            )
        if not _valid_native_id_for_provider(provider, record.get("native_session_id")):
            return _reconcile("the exact-owner attachment names an invalid provider session")
        if intent.get("acquisition_method") == native_attachment.ACQUISITION_STATUS_DISCOVERED:
            return _reconcile(
                "the exact-owner attachment is a status repair with no adoption receipt; "
                "it is incomplete/corrupt"
            )
        return {"kind": "ordinary", "session_id": record.get("native_session_id")}
    if not isinstance(receipt, Mapping):
        # A present non-mapping receipt is corrupt/ambiguous, not absence.
        return _reconcile("the exact-owner attachment carries a malformed adoption receipt")

    # A reusable status-repair receipt must match the stored record AND the
    # current operation on every exact fact.  A corrupted receipt never
    # nominates a different session key: the receipt's native session id
    # must equal the attachment record key.
    record_owner = record.get("owner") or {}
    if receipt.get("schema") != native_attachment.STATUS_REPAIR_ADOPTION_SCHEMA:
        return _reconcile("the exact-owner adoption receipt has an invalid schema")
    if receipt.get("native_session_id") != record.get("native_session_id"):
        return _reconcile("the exact-owner adoption receipt names a different native session")
    if receipt.get("provider") != record.get("provider"):
        return _reconcile("the exact-owner adoption receipt names a different provider")
    if not _valid_native_id_for_provider(provider, receipt.get("native_session_id")):
        return _reconcile("the exact-owner adoption receipt names an invalid provider session")
    if (
        receipt.get("terminal_id") != record_owner.get("terminal_id")
        or receipt.get("terminal_id") != terminal_id
    ):
        return _reconcile("the exact-owner adoption receipt names a different terminal owner")
    if (
        receipt.get("generation") != record_owner.get("generation")
        or receipt.get("generation") != occurrence
    ):
        return _reconcile("the exact-owner adoption receipt names a different occurrence")
    if (
        receipt.get("execution_mode") != em.NATIVE_TUI
        or record_owner.get("execution_mode") != em.NATIVE_TUI
    ):
        return _reconcile("the exact-owner adoption receipt is not a native-tui owner")
    if receipt.get("pane_id") != record_owner.get("pane_id") or receipt.get("pane_id") != pane_id:
        return _reconcile("the exact-owner adoption receipt names a different pane")
    if receipt.get("process_identity") != record_owner.get("process_identity") or receipt.get(
        "process_identity"
    ) != dict(process_identity):
        return _reconcile("the exact-owner adoption receipt names a different process identity")
    if receipt.get("operation_id") != operation_id:
        return _reconcile("the exact-owner adoption receipt belongs to a different operation")
    if not isinstance(receipt.get("operation_id"), str) or _is_invalid_uuid(
        str(receipt.get("operation_id"))
    ):
        return _reconcile("the exact-owner adoption receipt has an invalid operation binding")
    if receipt.get("request_digest") != request_digest:
        return _reconcile("the exact-owner adoption receipt binds a different request digest")
    if receipt.get("provider_version") != plan["plan_version"]:
        return _reconcile("the exact-owner adoption receipt attests a different provider build")
    if receipt.get("parser_key") != plan["parser_key"]:
        return _reconcile("the exact-owner adoption receipt names a different parser")
    if plan.get("escape") and receipt.get("composer_restored") is not True:
        return _reconcile("the exact-owner adoption receipt lacks the composer-restored proof")
    evidence = receipt.get("evidence_sha256")
    if (
        not isinstance(evidence, str)
        or len(evidence) != 64
        or any(ch not in "0123456789abcdef" for ch in evidence)
    ):
        return _reconcile("the exact-owner adoption receipt has a malformed evidence digest")
    observed_at = receipt.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        return _reconcile("the exact-owner adoption receipt lacks an observed timestamp")
    return {
        "kind": "reuse",
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
        logger.warning("repair %s: attachment adoption refused: %s", operation_id, exc)
        raise NativeStatusRepairConflict(
            "attachment-conflict",
            "the exact live attachment could not be adopted because another owner "
            "or a conflicting state holds the session",
        ) from exc
    except native_attachment.NativeAttachmentError as exc:
        logger.warning("repair %s: attachment adoption unavailable: %s", operation_id, exc)
        raise NativeStatusRepairConflict(
            "attachment-unavailable", "the attachment store could not be written"
        ) from exc


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
        logger.warning(
            "repair %s: roster repair refused a known identity: %s",
            facts["operation_id"],
            exc,
        )
        raise NativeStatusRepairConflict(
            "identity-conflict", "the roster already binds a different known identity"
        ) from exc
    except roster.StableAgentError as exc:
        logger.warning("repair %s: roster repair unavailable: %s", facts["operation_id"], exc)
        raise NativeStatusRepairUnavailable("the roster repair could not be recorded") from exc
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


def raw_request_digest(
    *,
    terminal_id: str,
    generation: Optional[str],
    provider_version: Optional[str],
    physical_occurrence: Optional[str],
) -> str:
    """A deterministic digest of the raw caller inputs, for invalid-input
    diagnostics only.  No successful or evidence-bearing operation may use
    this pre-resolution digest."""
    return hashlib.sha256(
        "\x00".join(
            (
                terminal_id,
                generation or "",
                provider_version or "",
                physical_occurrence or "",
            )
        ).encode("utf-8")
    ).hexdigest()


def resolved_request_digest(
    *,
    terminal_id: str,
    model_generation: Optional[str],
    occurrence: str,
    provider: str,
    effective_version: str,
    binding_native_id: Optional[str] = None,
) -> str:
    """The canonical digest of the *resolved* operation facts.

    Computed only after the current row, canonical physical occurrence,
    provider, effective pinned plan version, and validated managed-v2
    binding are resolved — so spelling differences (an omitted vs
    explicitly-identical managed occurrence, or an omitted vs explicit
    effective version) are the same request, while a genuinely different
    provider, effective plan, physical occurrence, or binding identity is
    a different operation under the same operation id.
    """
    return hashlib.sha256(
        "\x00".join(
            (
                terminal_id,
                model_generation or "",
                occurrence,
                provider,
                normalized_version(effective_version) if effective_version else "",
                binding_native_id or "",
            )
        ).encode("utf-8")
    ).hexdigest()


def repair_terminal_native_identity(
    *,
    terminal_id: str,
    generation: Optional[str] = None,
    provider_version: Optional[str] = None,
    physical_occurrence: Optional[str] = None,
    operation_id: str,
    caller: str = "cao.native-status-repair",
) -> dict[str, Any]:
    """Repair one currently live rostered terminal's native session id.

    ``generation`` is the *expected model generation*: required for
    managed/v2 rows and must equal ``row.generation``; legacy rows have
    none and must not be passed one.  ``physical_occurrence`` is the
    durable physical identity of the terminal: required for legacy rows and
    must equal their callback-target generation; for managed rows it is
    derived from the model generation and, if supplied, must equal it.
    ``operation_id`` is an explicit canonical UUID bound to a
    server-derived digest of the immutable inputs (terminal, generation,
    provider plan, and physical occurrence), so an exact retry is
    idempotent and a changed request is a typed conflict.

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
            "request_digest": raw_request_digest(
                terminal_id=terminal_id,
                generation=generation,
                provider_version=provider_version,
                physical_occurrence=physical_occurrence,
            ),
            "terminal_id": terminal_id,
            "generation": generation,
            "model_generation": generation,
            "physical_occurrence": physical_occurrence,
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
            "request_digest": raw_request_digest(
                terminal_id=terminal_id,
                generation=generation,
                provider_version=provider_version,
                physical_occurrence=physical_occurrence,
            ),
            "terminal_id": terminal_id,
            "generation": generation,
            "model_generation": generation,
            "physical_occurrence": physical_occurrence,
            "provider": None,
            "provider_version": normalized_version(provider_version) if provider_version else None,
            "native_session_id": None,
            "evidence_sha256": None,
            "parser_key": None,
            "attachment": None,
            "composer_restored": None,
            "task_bytes_submitted": False,
        }
    req_digest = raw_request_digest(
        terminal_id=terminal_id,
        generation=generation,
        provider_version=provider_version,
        physical_occurrence=physical_occurrence,
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
        "physical_occurrence": physical_occurrence,
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
    if provider not in _REPAIR_PARSER_PLANS:
        return refused(
            "provider-unsupported",
            f"provider {provider!r} has no pinned native /status repair parser",
        )

    # A terminals-table row missing its callback-target generation: use the
    # canonical get_terminal_metadata CAS/self-heal seam, but ONLY when the
    # row already carries a non-null model generation the heal will
    # deterministically reuse.  A true generation-null legacy row is refused
    # without calling (or mutating through) the helper.
    if row["vintage"] == "legacy" and not row["callback_target_generation"]:
        if row["generation"] is None:
            return refused(
                "callback-target-missing",
                "the legacy terminal has no pane-bound callback-target generation "
                "and none can be established without ambiguity; refusing without "
                "mutating",
            )
        database.get_terminal_metadata(terminal_id, warn_if_missing=False)
        row = _load_terminal_row(terminal_id)
        if row is None:
            return refused("terminal-not-found", f"no terminal row is recorded for {terminal_id}")
        if not row["callback_target_generation"]:
            return refused(
                "callback-target-missing",
                "the legacy terminal's callback-target generation could not be "
                "established; refusing without mutating",
            )

    try:
        model_generation, occurrence = _resolve_occurrence(row, generation, physical_occurrence)
    except NativeStatusRepairError as exc:
        return refused(getattr(exc, "reason", "errored"), str(exc))
    base["generation"] = occurrence
    base["physical_occurrence"] = occurrence

    # The managed-v2 reservation binding is a durable, authoritative fact
    # (native identity, execution mode, provider version) for a v2 terminal;
    # a v2 terminal whose exact binding is absent/malformed/incomplete fails
    # closed, and a legacy (or v1-managed) row never consumes a stale v2
    # reservation.
    try:
        with database.SessionLocal() as db:
            binding = _load_validated_binding(
                db,
                terminal_id=terminal_id,
                model_generation=model_generation,
                provider=provider,
                require_binding=row["vintage"] == "v2",
            )
    except NativeStatusRepairError as exc:
        return refused(getattr(exc, "reason", "errored"), str(exc))
    durable_version = binding["provider_version"] if binding else None
    binding_native_id = binding["native_session_id"] if binding else None

    plan, plan_error = _resolve_plan(provider, provider_version, durable_version)
    if plan_error is not None or plan is None:
        if plan_error == "version-drift":
            return refused(
                "version-drift",
                "the supplied provider build does not agree with the durable "
                "managed-v2 binding provider version",
            )
        return refused(
            "unsupported-build",
            f"provider {provider!r} build {provider_version!r} has no pinned repair "
            "parser; an unproven build is refused, never guessed",
        )

    # The canonical digest is computed ONLY after the row, occurrence,
    # provider, effective plan version, and validated binding are resolved,
    # so spelling differences are the same request and a genuinely
    # different fact is a different operation.
    req_digest = resolved_request_digest(
        terminal_id=terminal_id,
        model_generation=model_generation,
        occurrence=occurrence,
        provider=provider,
        effective_version=plan["plan_version"],
        binding_native_id=binding_native_id,
    )
    base["request_digest"] = req_digest

    # Operation-id idempotency: a completed exact retry adopts the recorded
    # evidence with no pane I/O; a changed immutable request (including a
    # changed physical occurrence, provider plan, or binding identity) is a
    # typed conflict before anything touches the pane.
    prior_evidence = _evidence_by_operation(operation_id)
    if prior_evidence is not None:
        return _evidence_outcome(
            prior_evidence,
            terminal_id=terminal_id,
            generation=generation,
            occurrence=occurrence,
            provider=provider,
            effective_version=plan["plan_version"],
            binding_native_id=binding_native_id,
            operation_id=operation_id,
            request_digest=req_digest,
        )

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
                        binding=binding,
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
    occurrence: str,
    provider: str,
    effective_version: str,
    binding_native_id: Optional[str],
    operation_id: str,
    request_digest: str,
) -> dict[str, Any]:
    """The truthful reconstructed outcome of a completed exact retry.

    The recorded evidence is adopted only when its request digest AND its
    recorded terminal/provider/version/occurrence all still match the
    current resolved facts: a completed operation must never be adopted
    against a recycled terminal id, a different provider, a changed
    effective plan, or a changed physical occurrence.
    """

    def _conflict(detail: str) -> dict[str, Any]:
        return {
            "schema": REPAIR_SCHEMA,
            "status": STATUS_REFUSED,
            "reason": "operation-conflict",
            "detail": detail,
            "operation_id": operation_id,
            "request_digest": request_digest,
            "terminal_id": terminal_id,
            "generation": evidence.get("generation"),
            "model_generation": generation,
            "physical_occurrence": occurrence,
            "provider": evidence.get("provider"),
            "provider_version": evidence.get("provider_version"),
            "native_session_id": evidence.get("native_session_id"),
            "evidence_sha256": evidence.get("evidence_sha256"),
            "parser_key": evidence.get("parser_key"),
            "attachment": None,
            "composer_restored": None,
            "task_bytes_submitted": False,
        }

    if evidence["request_digest"] != request_digest:
        return _conflict("the operation id is already bound to a different request digest")
    if evidence.get("terminal_id") != terminal_id:
        return _conflict("the operation id is bound to a different terminal")
    if str(evidence.get("generation") or "") != occurrence:
        return _conflict(
            "the operation id is bound to a different physical occurrence than the "
            "terminal now carries; a recycled terminal is never adopted"
        )
    if evidence.get("provider") != provider:
        return _conflict("the operation id is bound to a different provider")
    if normalized_version(evidence.get("provider_version") or "") != normalized_version(
        effective_version
    ):
        return _conflict("the operation id is bound to a different effective provider plan")
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
        "physical_occurrence": occurrence,
        "provider": evidence.get("provider"),
        "provider_version": evidence.get("provider_version"),
        "native_session_id": evidence.get("native_session_id"),
        "evidence_sha256": evidence.get("evidence_sha256"),
        "parser_key": evidence.get("parser_key"),
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
    binding: Optional[Mapping[str, Any]],
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
    binding_native_id = binding["native_session_id"] if binding else None
    binding_version = binding["provider_version"] if binding else None
    # Re-verify every exact fact now that the claims are held: drift
    # between load and claim is drift, and drift means zero bytes.  The
    # returned CURRENT terminal, lineage, and binding native ids are the
    # only facts the known-identity preflight may use — never the pre-claim
    # snapshot.  The binding is revalidated exactly, so the canonical digest
    # computed over it stays truthful.
    with database.SessionLocal() as db:
        current = _verify_exact_facts(
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
            expected_binding_native_id=binding_native_id,
            expected_binding_version=binding_version,
        )

    # Recheck the completed-operation evidence now that the lifecycle
    # claims are held: two concurrent requests that both began before the
    # first evidence commit serialize here, and the second adopts the
    # first's evidence (or a changed-request conflict) rather than
    # proceeding as an ordinary call.
    prior_evidence = _evidence_by_operation(operation_id)
    if prior_evidence is not None:
        return _evidence_outcome(
            prior_evidence,
            terminal_id=terminal_id,
            generation=model_generation,
            occurrence=occurrence,
            provider=provider,
            effective_version=plan["plan_version"],
            binding_native_id=binding_native_id,
            operation_id=operation_id,
            request_digest=request_digest,
        )

    # Known-identity preflight before bytes: no /status is ever typed when
    # the identity is already known, conflicting, un-attached, or attached
    # to a non-exact owner.  The managed-v2 binding native id is an
    # authoritative durable known-identity fact alongside terminal/lineage.
    preflight = _known_identity_preflight(
        provider=provider,
        terminal_id=terminal_id,
        occurrence=occurrence,
        pane_id=pane_id,
        process_identity=process_identity,
        terminal_known=current["terminal_native_session_id"],
        lineage_known=current["native_session_id"],
        binding_known=current["binding_native_session_id"],
        operation_id=operation_id,
    )
    if preflight["kind"] == "already-known":
        outcome = dict(base)
        outcome.update(
            status=STATUS_ALREADY_KNOWN,
            reason=None,
            detail=(
                "the identity is already known and exactly attached; nothing was "
                "typed, nothing was recorded, and nothing was mutated"
            ),
            provider=provider,
            native_session_id=preflight["session_id"],
        )
        return outcome
    if preflight["kind"] == "attachment-unavailable":
        raise NativeStatusRepairConflict(
            "attachment-unavailable", "the attachment store could not be read"
        )
    if preflight["kind"] == "attachment-unresolved":
        raise NativeStatusRepairConflict(
            "attachment-unresolved",
            "the identity is already known but no attachment records it; a later "
            "attachment audit/migration owns that concern, not this bounded "
            "missing-identity repair",
        )
    if preflight["kind"] == "attachment-reconcile":
        raise NativeStatusRepairConflict(
            "attachment-reconcile",
            "the identity is known but its attachment is not an exact live owner "
            "for this repair's current facts; reconcile the attachment before "
            "proceeding",
        )
    if preflight["kind"] == "conflict":
        raise NativeStatusRepairConflict(
            "identity-conflict",
            "the terminal, roster lineage, or managed binding already know "
            "different native session ids; a repair never chooses between them",
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
    # adoption for THIS exact operation already names this exact verified
    # owner, so no second /status is needed.  An exact-owner attachment with
    # an invalid/mismatched receipt is a partial operation and is refused,
    # never retyped.
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
    ordinary_session: Optional[str] = None
    if prior["kind"] == "reconcile":
        raise NativeStatusRepairConflict(
            "attachment-reconcile",
            f"the exact-owner attachment's adoption receipt cannot be reconciled: "
            f"{prior['reason']}; no second /status was typed and nothing was adopted",
        )
    if prior["kind"] == "ordinary":
        # An ordinary exact live owner (pre-launch intent, no status-repair
        # receipt) is NOT a partial status repair: it is the duplicate-attach
        # barrier.  Proceed to panel verification; the panel must attest the
        # ordinary owner's session, and adoption reuses the same exact owner
        # without rewriting its intent.
        ordinary_session = prior["session_id"]
        if known_id is not None and ordinary_session != known_id:
            raise NativeStatusRepairConflict(
                "identity-conflict",
                "the exact live owner names a different id than the already-known "
                "identity; nothing was mutated",
            )
    if prior["kind"] == "reuse":
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
            binding=binding,
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
    if ordinary_session is not None and session_id != ordinary_session:
        raise NativeStatusRepairConflict(
            "identity-conflict",
            "the panel names a different session than the exact live owner; a "
            "second owner is never created for the same pane",
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
        binding=binding,
        session_id=session_id,
        parser_key=verdict["parser_key"],
        provider_version=verdict["provider_version"],
        evidence_sha256=verdict["evidence_sha256"],
        observed_at=verdict["observed_at"],
        composer_restored=composer_restored,
    )


def _attachment_is_exact_live_owner(
    record: Mapping[str, Any],
    *,
    provider: str,
    session_id: str,
    terminal_id: str,
    occurrence: str,
    pane_id: str,
    process_identity: Mapping[str, Any],
) -> bool:
    """Whether an attachment is a live owner whose identity is exactly this
    repair's current facts: provider/native id, owner terminal, physical
    occurrence, native-tui mode, pane, and exact process identity."""
    if record["state"] not in native_attachment.LIVE_STATES:
        return False
    owner = record.get("owner") or {}
    return (
        record.get("provider") == provider
        and record.get("native_session_id") == session_id
        and owner.get("terminal_id") == terminal_id
        and owner.get("generation") == occurrence
        and owner.get("execution_mode") == em.NATIVE_TUI
        and owner.get("pane_id") == pane_id
        and owner.get("process_identity") == dict(process_identity)
    )


def _known_identity_preflight(
    *,
    provider: str,
    terminal_id: str,
    occurrence: str,
    pane_id: str,
    process_identity: Mapping[str, Any],
    terminal_known: Optional[str],
    lineage_known: Optional[str],
    binding_known: Optional[str],
    operation_id: str,
) -> dict[str, Any]:
    """The zero-byte identity decision: already-known (only when BOTH repair
    targets are present with the exact live owner), conflict,
    attachment-unresolved, attachment-reconcile, or proceed (possibly to
    verify a single distinct known id).

    The durable known-identity facts are the terminal row, the roster
    lineage, and (for a managed-v2 terminal) the validated binding native
    id.  Any disagreement among known sources is a typed conflict.
    ``already-known`` requires BOTH actual repair targets — the terminal row
    and the current roster lineage — to be non-null, agree, agree with the
    binding when present, and be owned by the exact live attachment.  The
    binding never makes a missing target look complete: if either target is
    missing, the operation proceeds to verify the single distinct known id
    and fills the missing target(s) atomically."""
    known_ids = [
        value for value in (terminal_known, lineage_known, binding_known) if value is not None
    ]
    distinct = set(known_ids)
    if len(distinct) > 1:
        return {"kind": "conflict"}
    if terminal_known is None or lineage_known is None:
        # At least one actual repair target is missing: the binding alone
        # (or the present target) is a constraint to verify, never a
        # substitute for the missing target.
        return {"kind": "proceed", "known_id": known_ids[0] if known_ids else None}
    known_id = terminal_known  # == lineage_known after the disagreement check
    try:
        attachment = native_attachment.get(provider, known_id)
    except native_attachment.NativeAttachmentError as exc:
        logger.warning("repair %s: attachment lookup failed: %s", operation_id, exc)
        return {"kind": "attachment-unavailable"}
    if attachment is None:
        return {"kind": "attachment-unresolved"}
    if not _attachment_is_exact_live_owner(
        attachment,
        provider=provider,
        session_id=known_id,
        terminal_id=terminal_id,
        occurrence=occurrence,
        pane_id=pane_id,
        process_identity=process_identity,
    ):
        return {"kind": "attachment-reconcile"}
    return {"kind": "already-known", "session_id": known_id}


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
    binding: Optional[Mapping[str, Any]],
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
    binding_native_id = binding["native_session_id"] if binding else None
    binding_version = binding["provider_version"] if binding else None

    # Revalidate every exact fact — including the terminal, lineage, and
    # managed-binding native ids against the id about to be adopted —
    # immediately before attachment adoption, so a drift after the
    # observation cannot leave a wrong conservative claim.
    with database.SessionLocal() as db:
        _verify_exact_facts(
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
            expected_session_id=session_id,
            expected_binding_native_id=binding_native_id,
            expected_binding_version=binding_version,
        )

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
            occurrence=occurrence,
            provider=provider,
            effective_version=provider_version,
            binding_native_id=binding_native_id,
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
