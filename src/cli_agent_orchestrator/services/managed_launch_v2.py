"""Managed-launch protocol v2: generation-private zero-task launch/bind/admit.

v2 adds the bind-before-admit seam on top of the v1 two-phase shape:
``reserve → launching → bound → admitting → admitted``.  No task bytes
reach the provider until the journaled ``native_bound`` reference — the
provider-native pre-turn identity receipts (creation + binding payload
digests), the fork-owned binding record, and the producer fencing token
— exists and matches the digest the caller presents.

Invariant: ``protocol_vintage`` is first-class and immutable; v1
reservations never gain v2 semantics; v2 rows live in the isolated
``managed_launch_v2_reservations`` table so every v1 query/deleter has
zero visibility into them; the launch nonce is stored only as a digest.

Failure mode prevented: admitting a task against an ambient or
recency-derived provider identity (wrong session after any other
activity), or against a generation whose native identity was lost in a
crash window — the bind step makes the exact pre-turn identity a
durable precondition of admission, so crash-before-bind always yields
zero task bytes.

Why this guard exists: the resume/admission contracts downstream can
only bind what was provably bound at launch; an unbound admission would
be unrecoverable except by blind respawn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from functools import partial
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.constants import COMPANION_DIR
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2AdmitRequest,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2CleanupRequest,
    ManagedLaunchV2NegativeRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import (
    generation_fence,
    heartbeat_store,
    native_attachment,
    native_tui_launch,
    provider_contracts,
    recovery_receipts,
    secret_gate,
)
from cli_agent_orchestrator.services.destructive_endpoint import write_binding_record
from cli_agent_orchestrator.services.managed_launch import (
    REASON_BIND_BRIDGE_NOT_DURABLY_READY,
    ManagedLaunchConflict,
    ManagedLaunchError,
    ManagedLaunchNotFound,
    ManagedLaunchNotReady,
    ManagedLaunchUnavailable,
)
from cli_agent_orchestrator.services.provider_contracts import (
    ProviderContractError,
    check_pinned_version,
    normalized_version,
)
from cli_agent_orchestrator.utils.terminal import generate_terminal_id, managed_window_name

logger = logging.getLogger(__name__)

_READINESS_RECEIPT_KINDS = {
    "codex": "codex-thread-start",
    "kimi_cli": "kimi-acp-session-new",
}
#: Readiness receipt kinds for native-TUI generations.
#:
#: The kind strings are disjoint from the ACP table above by design, so
#: "an ACP receipt can never satisfy a native bind" is structural rather
#: than a rule someone has to remember to check.  The two modes prove
#: readiness by different evidence — ACP by a provider transcript, native
#: by an attached pane running the bound session — and a receipt from the
#: wrong one is not weaker evidence, it is evidence about a different
#: thing entirely.
_NATIVE_TUI_READINESS_RECEIPT_KINDS = {
    "kimi_cli": "kimi-native-tui-attached",
    # Distinct from Kimi's for the same structural reason the native and
    # ACP kinds are distinct: the two providers prove readiness by
    # different evidence.  Kimi's pane is proven attached; Claude's
    # readiness is its own SessionStart hook naming the exact session id,
    # which is a claim about the provider rather than about the pane.
    "claude_code": "claude-native-session-start",
}
_ISSUANCE_SOURCES = {
    "codex": "app_server_thread_start",
    "kimi_cli": "acp_session_new",
    # Claude's identity is chosen, not discovered: a canonical uuid minted
    # before any provider I/O and handed to the launch as --session-id.
    "claude_code": "cli_session_id",
}
#: Canonical provider key to the *executable* name the version-pin tables
#: are keyed by.  The mapping exists because these are two different
#: namespaces and conflating them is how "claude" ends up published as a
#: provider on a shared surface.
_PINNED_PROVIDER = {
    "codex": "codex",
    "kimi_cli": "kimi",
    "claude_code": "claude",
}

#: How long a native launch watches for its pane to become input-ready,
#: and how often it looks.
#:
#: A pane that has started is not a pane that can be typed into: the
#: provider paints its status bar, connects its servers, and only then
#: draws a composer.  Measured cold that takes about two thirds of a
#: second, while a launcher that binds and admits in a straight line
#: arrives about a fifth of a second in — so typing into the boot screen
#: is the common ordering, not a rare race.
#:
#: Overshooting the bound only delays a launch that is already waiting on
#: a process; undershooting publishes the false readiness this exists to
#: remove.  The poll is fine because the window itself is sub-second.
NATIVE_PANE_READY_TIMEOUT_SECONDS = 10.0
_NATIVE_PANE_READY_POLL_SECONDS = 0.1

#: Machine-readable reasons for a native admission that wrote no bytes.
#:
#: Recorded on the reservation rather than only in an HTTP body a lost
#: response would destroy.  All three describe the same delivery fact —
#: nothing was sent — and differ only in whether asking again could
#: change the answer.  ``native_pane_unobservable`` is retryable because
#: "we could not look" is not evidence about the pane.
REFUSED_PROVIDER_NOT_YET_READY = "provider_not_yet_ready"
REFUSED_PANE_UNOBSERVABLE = "native_pane_unobservable"
REFUSED_NATIVE_IDENTITY = "native_binding_identity_refused"
_RETRYABLE_REFUSAL_REASONS = frozenset({REFUSED_PROVIDER_NOT_YET_READY, REFUSED_PANE_UNOBSERVABLE})

#: The immutable, redacted evidence written when a generation reaches
#: ``preflight_blocked``.  It is the single truthful cause a route-correct
#: recovery reads before it finalizes the generation: reason code, the
#: redacted human detail, the exact reservation/terminal/generation
#: identity, and ``task_bytes_submitted`` — always false in this state,
#: because every path that reaches this state does so before the launch
#: sequence has any turn to submit.
PREFLIGHT_FAILURE_SCHEMA = "cao-managed-launch-v2-preflight-failure-v1"

#: Machine-readable cause codes for a blocked generation.  The strings are
#: informative for a recovering conductor, not a gate; every one of them
#: means the same delivery fact — zero task bytes crossed.
#:
#: The set is *closed*.  A recovering caller is entitled to branch on these
#: values, so a code it has never seen is worse than a coarse one: it
#: reads as an unknown failure class and there is no safe default branch
#: for that.  A new failure site therefore reuses the member that is true
#: of it, or ``PREFLIGHT_REASON_GENERIC``, rather than inventing a name
#: that describes the call site more precisely than the contract allows.
PREFLIGHT_REASON_PROVIDER_UNSUPPORTED = "provider-unsupported"
PREFLIGHT_REASON_NATIVE_PREFLIGHT = "native-preflight"
PREFLIGHT_REASON_SESSION_BOOTSTRAP = "session-bootstrap"
PREFLIGHT_REASON_TUI_LAUNCH_REFUSED = "tui-launch-refused"
PREFLIGHT_REASON_READINESS = "readiness-receipt"
PREFLIGHT_REASON_GENERIC = "native-generic"

#: The closed set itself, held as data so that "is this reason lawful?" is
#: a question about one object rather than about six scattered constants.
PREFLIGHT_REASONS = frozenset(
    {
        PREFLIGHT_REASON_PROVIDER_UNSUPPORTED,
        PREFLIGHT_REASON_NATIVE_PREFLIGHT,
        PREFLIGHT_REASON_SESSION_BOOTSTRAP,
        PREFLIGHT_REASON_TUI_LAUNCH_REFUSED,
        PREFLIGHT_REASON_READINESS,
        PREFLIGHT_REASON_GENERIC,
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_json(value: Optional[str], default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ManagedLaunchUnavailable("managed-launch v2 record contains invalid JSON") from exc


def _mode_record(row: Any) -> dict[str, Any]:
    """The execution-mode fields of a row, tolerant of pre-contract rows.

    ``getattr`` with a ``None`` default rather than attribute access: a
    row loaded from a database that predates these columns has no such
    attribute, and that absence is exactly the legacy case the mode
    readers are built to classify as ACP.
    """
    return {
        "execution_mode": getattr(row, "execution_mode", None),
        "execution_mode_source": getattr(row, "execution_mode_source", None),
    }


#: The provider-native readiness receipt kind published as a sibling of
#: ``binding``, selected by the row's **bound mode** and provider — never
#: by what a receipt says about itself, so a receipt cannot nominate the
#: table that would accept it.
#:
#: Kimi native is deliberately absent, and its absence is meaningful
#: rather than an omission. The two providers' readiness belongs to
#: different evidence classes: Claude's is *provider-authored* — the
#: SessionStart hook is the provider's own statement about itself — while
#: Kimi's is an *observed attached pane* running the resumed session, with
#: no hook and no provider statement anywhere in it. Minting a Kimi proof
#: would mean relabelling this side's own pane observation as a provider
#: claim, which is the manufactured-agreement failure the no-echo rule
#: exists to prevent.
_NATIVE_READINESS_SIBLING_SCHEMAS: dict[str, dict[str, str]] = {
    em.NATIVE_TUI: {"claude_code": "cao-claude-native-readiness-v1"},
}

#: The only admissible reason in this lane for an object that carries no
#: provider-authored proof. A closed vocabulary, so a consumer can branch
#: on it rather than parse prose.
PROOF_ABSENT_PROVIDER_AUTHORS_NONE = "provider-authors-no-readiness-proof"

#: Fields the sibling publishes that only a running provider can author.
#: Held as data so the completeness check below cannot drift from the set
#: a consumer validates.
_NATIVE_READINESS_OBSERVED_FIELDS = (
    "session_start_hook_id",
    "composer_state",
    "pane_id",
    "provider_process_id",
    "observed_at",
)


def _incomplete_readiness_fields(row: Any, receipt: dict[str, Any]) -> list[str]:
    """Which parts of a proof-bearing sibling this receipt cannot supply.

    The single completeness rule, consulted by **both** the bind gate and
    the projection. They used to answer this question separately, with
    different sets, and the gap between them was reachable: a receipt rich
    enough to bind but too thin to project left a generation ``bound``
    whose readiness sibling was ``null`` forever — and a consumer reads
    that null as "not yet", so it waits for a readiness that has already
    been consumed and can never arrive again.

    Empty for a (mode, provider) pair that authors no proof: there is
    nothing to be incomplete about, and its no-proof object needs only the
    pane observation.
    """
    mode = em.mode_of_record(_mode_record(row))
    if _NATIVE_READINESS_SIBLING_SCHEMAS.get(mode, {}).get(row.provider) is None:
        return []
    built = _readiness_proof_fields(row, receipt, mode)
    missing = [
        field
        for field in _NATIVE_READINESS_OBSERVED_FIELDS
        if not isinstance(built.get(field), str) or not built[field]
    ]
    if not isinstance(built.get("native_session_id"), str) or not built["native_session_id"]:
        missing.append("native_session_id")
    if not built.get("input_ready"):
        missing.append("input_ready")
    return missing


def _readiness_proof_fields(row: Any, receipt: dict[str, Any], mode: str) -> dict[str, Any]:
    """The proof-bearing sibling's fields, assembled from row + observation.

    Split out so the completeness rule above and the projection below are
    literally the same computation rather than two that agree today.
    """
    # Local, matching the other native-module imports in this file, which
    # are deferred to keep the import graph acyclic.
    from cli_agent_orchestrator.services import claude_native_readiness

    observation = receipt.get("model_input_ready_observation") or {}
    session_start = receipt.get("provider_session_start") or {}
    return {
        "schema": _NATIVE_READINESS_SIBLING_SCHEMAS.get(mode, {}).get(row.provider),
        "provider": row.provider,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "execution_mode": mode,
        # The session the provider actually adopted, rather than the id
        # chosen for it — the two differ exactly when something went
        # wrong, which is when this field matters.
        "native_session_id": receipt.get("provider_session_id"),
        # Provider-authored: the hook Claude itself wrote, naming the
        # session it started.
        #
        # Read through the producer's own constant rather than a literal.
        # These were two independent spellings of one contract and they
        # drifted: the receipt published ``native_session_id`` while this
        # asked for ``session_id``, so the field was always absent, the
        # completeness rule always reported it missing, and every real
        # Claude native launch was refused 425 forever against a complete
        # provider-authored proof. Sharing the constant makes that class of
        # drift a NameError at import rather than a permanent refusal
        # discovered in production.
        "session_start_hook_id": session_start.get(claude_native_readiness.SESSION_START_ID_KEY),
        # Observed: what the composer detector read off the pane, when,
        # and where.
        "composer_state": observation.get("provider_status"),
        "pane_id": observation.get("pane_id"),
        "provider_process_id": published_process_id(receipt.get("process_identity")),
        "observed_at": observation.get("observed_at"),
        "input_ready": bool(receipt.get("model_input_ready")),
    }


def published_process_id(process_identity: Any) -> Optional[str]:
    """One process, rendered as the string the readiness sibling carries.

    A bare pid is not identity — pids are recycled, so a stale one can
    match an unrelated live process and forge a survivor, or match nothing
    and forge a *no*-survivor. The start marker travels with it for that
    reason; publishing the pid alone would hand a consumer exactly the
    forgeable half.

    Public because the control-identity projection publishes the same
    scalar under the same name. Two renderings of one process identity
    would agree on the pid and disagree on the half that makes it
    non-forgeable, which is the only half worth checking.
    """
    if not isinstance(process_identity, dict):
        return None
    pid = process_identity.get("pid")
    marker = process_identity.get("start_marker")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(marker, str) or not marker:
        return None
    return f"{pid}@{marker}"


def _native_readiness_sibling(row: Any) -> Optional[dict[str, Any]]:
    """The provider's own readiness proof, as a sibling of ``binding``.

    The consumer of a native binding requires this receipt and refuses an
    otherwise exact binding without it. Publishing it here closes a
    response contract that was internally inconsistent: the proof existed,
    durably, and the projection simply did not carry it.

    **Every field comes from the reservation's own durable row or from a
    provider observation. None is read from the request.** A field whose
    only available source would be the request is not admissible as proof
    — filling it would manufacture agreement between a claim and itself,
    which is indistinguishable from verification to anyone downstream.

    **Three states, not two.** The key is published *always*; an absent
    key and a null one mean opposite things ("this peer cannot answer"
    versus "this peer answered and there is nothing"), and a consumer is
    entitled to wait through the second while refusing the first outright.
    Within "answered" there are two further, distinct states:

    * ``None`` — **if and only if** nothing is durably published yet. A
      consumer reads this as not-yet and retries the same bind attempt.
    * a *no-proof object* — readiness is durable, and this bound mode's
      provider authors no readiness proof. Overloading ``None`` for this
      would repeat the error the effort-observability table rejects: one
      token that is true of one state and a lie about the other, leaving a
      consumer polling a condition that can never clear and reporting a
      timeout where the truth is a permanent gap.
    * a *proof-bearing object* — readiness is durable and the provider
      authored a statement about it.

    Which object form is used is chosen by the bound mode's evidence class
    as mapped per provider, never by anything the object says about
    itself.
    """
    from cli_agent_orchestrator.services.managed_provider_bridge import read_state

    mode = em.mode_of_record(_mode_record(row))
    schema = _NATIVE_READINESS_SIBLING_SCHEMAS.get(mode, {}).get(row.provider)
    try:
        state = read_state(row.reservation_id)
    except Exception as exc:  # noqa: BLE001 - unreadable is not "ready"
        logger.warning(
            "Could not read durable readiness for reservation %s: %s", row.reservation_id, exc
        )
        return None
    receipt = (state or {}).get("readiness")
    if (state or {}).get("state") != "ready" or not isinstance(receipt, dict):
        return None

    observation = receipt.get("model_input_ready_observation") or {}
    if schema is None:
        # Durable readiness whose provider authors no proof. Exactly these
        # keys and no others: every provider-authored field is *absent*
        # rather than null or empty, because a key whose only possible
        # source would be invention must not exist in the object at all —
        # a reader that finds it, even empty, has to decide whether it was
        # attempted and failed, and there was nothing to attempt.
        #
        # ``schema`` is explicitly null rather than omitted: an omitted key
        # is indistinguishable from a serialization bug, while a *string*
        # would name a kind nobody defined, which is a refusal.
        if mode != em.NATIVE_TUI or not bool(receipt.get("model_input_ready")):
            return None
        return {
            "schema": None,
            "proof_absent_reason": PROOF_ABSENT_PROVIDER_AUTHORS_NONE,
            # Derived from the row's provider, carried so an operator can
            # see which evidence class was applied without inferring it.
            "provider_receipt_kind": _NATIVE_TUI_READINESS_RECEIPT_KINDS.get(row.provider),
            "provider": row.provider,
            "terminal_id": row.terminal_id,
            "generation": row.generation,
            "execution_mode": mode,
            "input_ready": True,
            "input_ready_observation": observation,
        }
    # One completeness rule, shared with the bind gate. An incomplete
    # proof is not a weaker proof; it is an absent one — and because bind
    # asks the same question with the same function, a generation cannot
    # bind on a receipt this would then refuse to publish.
    if _incomplete_readiness_fields(row, receipt):
        return None
    return _readiness_proof_fields(row, receipt, mode)


def _row_dict(row: Any) -> dict[str, Any]:
    admission = _parse_json(row.admission_json, None)
    mode_record = _mode_record(row)
    # The durable cleanup proof, and the only thing that may project
    # ``cleaned``. Read with getattr so a row loaded by an older binary,
    # or from a database predating the additive column, reads as
    # not-cleaned rather than raising.
    cleanup = _parse_json(getattr(row, "cleanup_json", None), None)
    return {
        # Projected through the mode readers, never read raw: every
        # public and durable surface therefore shows a concrete mode,
        # and a legacy row projects as ACP with source 'legacy' instead
        # of a null that a downstream guard might read as "either".
        "execution_mode": em.mode_of_record(mode_record),
        "execution_mode_source": em.source_of_record(mode_record),
        "is_legacy_execution_mode": em.is_legacy_row(mode_record),
        "protocol_version": PROTOCOL_VERSION_V2,
        "protocol_vintage": row.protocol_vintage,
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "session_name": row.session_name,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "caller_id": row.caller_id,
        "working_directory": row.working_directory,
        "trusted_project_root": row.trusted_project_root,
        "obligation_generation": row.obligation_generation,
        "task_id": row.task_id,
        "run_id": row.run_id,
        "launch_nonce_digest": row.launch_nonce_digest,
        # PROJECTED, not stored. A cleaned generation answers ``cleaned``
        # while the row still holds the finalization verdict it actually
        # reached, because these are two facts about different things: the
        # verdict says how the generation ENDED, cleanup says what happened
        # to its RESOURCES. Overwriting the durable state would erase which
        # terminal outcome it had, and reservation history is immutable.
        #
        # It also keeps a safety gate honest without widening it: cleanup
        # still requires a durable ``negative``, and because that string
        # never changes, the gate keeps passing on retry exactly as
        # written. A durable transition to ``cleaned`` would have forced
        # that gate to accept a second state for no gain.
        "state": "cleaned" if cleanup is not None else row.state,
        # The durable state, unchanged, so nothing above is a claim a
        # consumer has to take on trust: it can read the projection and the
        # fact it was derived from in the same response.
        "durable_state": row.state,
        # The cleanup proof itself, always present as an explicit null
        # before a cleanup, so a consumer distinguishes "not cleaned" from
        # "this peer cannot say".
        "cleanup": cleanup,
        "request": _parse_json(row.request_json, {}),
        "binding": _parse_json(row.binding_json, None),
        # A first-class sibling of ``binding``, never nested inside it: the
        # binding digest both sides compute is taken over the binding
        # object alone, so a sibling cannot move a byte of it, while a
        # field added inside would silently change what admission is
        # judged against.
        #
        # The key is published on EVERY row, in every state, including
        # while the row is still launching and including for providers
        # whose pair defines no receipt kind. Always-present is the whole
        # discipline: a consumer reads an explicit null as "not yet" and
        # waits, and an absent key as "this peer cannot answer" and
        # refuses without waiting. Omitting the key when there is nothing
        # to say would turn every ordinary not-yet-ready moment into a
        # permanent refusal.
        "native_readiness": _native_readiness_sibling(row),
        "bind_intent": _parse_json(getattr(row, "bind_intent_json", None), None),
        "admission": admission,
        "launch_failure": (
            admission.get("launch_failure")
            if row.state == "launch-failed-bridge" and isinstance(admission, dict)
            else None
        ),
        # The immutable redacted zero-byte preflight evidence, surfaced on
        # every GET and recovery verb so a route-correct recovery reads the
        # cause without a second call.  None for any row that never blocked.
        "preflight_failure": _parse_json(getattr(row, "preflight_failure_json", None), None),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        **_published_terminal_facts(row.terminal_id),
    }


#: The canonical identity a caller may address a managed terminal by. Six
#: fields, not the five the writer boundary checks: a conductor deciding
#: whether to adopt a pane also has to know *which provider session* is
#: running in it, and a pane running a different session than the one
#: registered is a supersession that the other five fields cannot see.
PUBLISHED_IDENTITY_FIELDS = (
    "server_socket_path",
    "session_id",
    "window_id",
    "pane_id",
    "pane_pid",
    "native_session_id",
)

#: Every key this surface adds, so a peer can tell "the field is absent
#: because the row has no value" from "this build does not publish it".
PUBLISHED_TERMINAL_FIELDS = (
    "lifecycle_state",
    "lifecycle_reason",
    "superseded_by_terminal_id",
    "superseded_by_generation",
    *PUBLISHED_IDENTITY_FIELDS,
)


def _published_terminal_facts(terminal_id: Optional[str]) -> dict[str, Any]:
    """The terminal's lifecycle and canonical identity, for the reservation.

    Published here because this is the surface the conductor actually
    reads. Its non-adoptable gate consults ``lifecycle_state`` on the
    reservation response, and until this existed that key was simply
    absent — so the gate compared ``None`` against the non-adoptable set,
    never matched, and fell through to the adoption branches. A guard that
    fails open is worse than no guard, because the code reads as though the
    case is handled.

    Every key is always present, ``None`` when unknown. An absent key and a
    null one would otherwise be indistinguishable to a reader, and they
    mean opposite things: "this peer cannot answer" versus "this peer
    answered, and the row holds nothing". The first must fail closed; the
    second is ordinary.

    Read through the projection so this surface and the human views cannot
    drift: one authority for what a terminal's lifecycle is, rather than a
    second implementation that agrees today.
    """
    published: dict[str, Any] = {field: None for field in PUBLISHED_TERMINAL_FIELDS}
    if not terminal_id:
        return published
    try:
        from cli_agent_orchestrator.services import terminal_projection

        projected = terminal_projection.project_terminal(terminal_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable row publishes nulls
        logger.debug("Could not project terminal %s for a v2 response: %s", terminal_id, exc)
        return published
    if not projected:
        return published
    for field in PUBLISHED_TERMINAL_FIELDS:
        published[field] = projected.get(field)
    return published


def _query(db: Any, reservation_id: str) -> Any:
    return (
        db.query(database.ManagedLaunchV2ReservationModel)
        .filter(database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id)
        .first()
    )


def native_binding_digest(record: dict[str, Any]) -> Optional[str]:
    """The digest an admit call must present (over the journaled binding)."""
    binding = record.get("binding")
    if not binding:
        return None
    return hashlib.sha256(_canonical_json(binding).encode()).hexdigest()


def _reject_non_canonical_workdir(working_directory: str) -> None:
    """Refuse a working directory that is not already its own realpath.

    The provider files a session under the working-directory *string* it
    was handed and resolves a later resume against that same string,
    while the terminal UI that resume starts is a process whose reported
    cwd is always the realpath.  So a reservation naming ``/tmp/w`` on a
    platform where that is a symlink mints a session that a UI started in
    the very same physical directory can never find: the launch reports
    success, readiness publishes, bind succeeds against a session id that
    genuinely exists on disk, and the pane is dead about a second later
    with nothing on the reservation naming the cause.

    Refused, not rewritten, for two reasons.  Normalising here would
    change what the reservation echoes back, so a caller that compares a
    replayed reserve against the stored one would read its own ordinary
    retry as a conflict.  And the same string is handed on to the session
    mint and to the pane; a value corrected in one place and not the
    others reintroduces exactly the divergence this prevents.  Refusing
    puts the correction at the one boundary that can make it consistently
    — the caller — and names the form to send.

    This runs before any row, provider process, bootstrap, or session
    exists, so a refusal leaves nothing behind to reconcile.
    """
    if not os.path.isabs(working_directory):
        raise ManagedLaunchConflict(
            f"working_directory must be an absolute path; got {working_directory!r}"
        )
    canonical = os.path.realpath(working_directory)
    if canonical != working_directory:
        raise ManagedLaunchConflict(
            f"working_directory must be canonical; got {working_directory!r}, whose canonical "
            f"form is {canonical!r} — reserve with that instead. A session minted under a "
            "non-canonical path is filed where the terminal UI will never look for it."
        )
    if not os.path.isdir(canonical):
        raise ManagedLaunchConflict(
            f"working_directory must be an existing directory; {canonical!r} is not one"
        )


def _validate_reserve_identity(request: ManagedLaunchV2ReserveRequest) -> dict[str, Any]:
    _reject_non_canonical_workdir(request.working_directory)
    if request.provider == "codex":
        if request.trusted_project_root is None:
            raise ManagedLaunchConflict("Codex managed launches require trusted_project_root")
        if os.path.realpath(request.trusted_project_root) != request.trusted_project_root:
            raise ManagedLaunchConflict("trusted_project_root must be canonical")
    elif request.trusted_project_root is not None:
        raise ManagedLaunchConflict("trusted_project_root is valid only for provider=codex")
    if not os.path.isabs(request.provider_executable):
        raise ManagedLaunchConflict("provider_executable must be an absolute path")
    # Refused before anything persists, for the same reason the v1 surface
    # refuses it: a route the provider cannot honor should not become a
    # durable reservation that then has to be finalized.
    try:
        provider_contracts.validate_route_effort(request.expected_model, request.expected_effort)
    except provider_contracts.ProviderContractError as exc:
        raise ManagedLaunchConflict(str(exc)) from exc
    # The model half of the same pre-I/O question, asked beside the effort
    # half. The launch checks this again before minting an identity, and
    # that check stays: this one only moves the refusal earlier, so an
    # unpinnable Claude route costs no reservation, no allocated terminal
    # id, and no recovery verb to finalize.
    if request.provider == "claude_code":
        from cli_agent_orchestrator.services import claude_native_launch

        try:
            claude_native_launch.validate_requested_model(request.expected_model)
        except claude_native_launch.ClaudeNativeLaunchError as exc:
            raise ManagedLaunchConflict(str(exc)) from exc
    payload = request.model_dump(mode="json")
    # The raw nonce never persists; only its digest is stored.
    payload.pop("launch_nonce")
    return payload


#: Request keys introduced after the v2 reservation surface shipped.  A
#: reservation written before they existed simply has no such key.
_ADDITIVE_REQUEST_KEYS = ("execution_mode", "worker_class")


def _request_matches(stored_json: str, incoming: dict[str, Any]) -> bool:
    """Whether a replayed reserve carries the same immutable request.

    An exact byte match is the normal case.  The one accommodation is
    the upgrade boundary: a reservation written before the
    execution-mode contract has no mode keys at all, and treating that
    absence as "a different request" would turn an ordinary idempotent
    replay into a hard conflict for every in-flight reservation across a
    deploy.  So an absent stored key compares equal to an *unspecified*
    incoming value — and only to that.  A caller that now explicitly
    asks for a mode really is presenting a different request, and still
    conflicts.
    """
    if stored_json == _canonical_json(incoming):
        return True
    stored = _parse_json(stored_json, None)
    if not isinstance(stored, dict):
        return False
    normalized = dict(stored)
    for key in _ADDITIVE_REQUEST_KEYS:
        if key not in normalized and incoming.get(key) is None:
            normalized[key] = None
    return _canonical_json(normalized) == _canonical_json(incoming)


#: Modes ``launch_reserved`` has a real launch branch for.
#:
#: Reserving a mode and launching one are separate questions here, and
#: only the second is gated.  A reservation is a durable statement of
#: intent that ``bind_native`` and the run manifest are entitled to make
#: about a session whose process this surface did not start; refusing
#: native *reservations* would delete that vocabulary.  What must never
#: happen is a reservation that says ``native_tui`` whose process is the
#: ACP bridge, so the refusal sits at the one call that would otherwise
#: start that process.
#:
#: The distinction matters because the resolver defaults several worker
#: classes to native: without this gate, reserving
#: ``worker_class="persistent"`` and calling ``launch_reserved`` would be
#: enough to get an ACP bridge under a reservation row, binding receipt,
#: run manifest, and public status that all say ``native_tui`` — and
#: every one of those is evidence a consumer is entitled to trust.
#:
#: Adding ``em.NATIVE_TUI`` here is therefore the *last* step of building
#: the native branch below, never a precondition for it — and it is added
#: here now because ``_launch_native_tui`` exists: a native reservation
#: mints its own provider session, starts the provider's own TUI as the
#: pane's primary process, and publishes its own readiness receipt,
#: without the ACP bridge being involved at any point.
#:
#: The v1 surface's ``SUPPORTED_EXECUTION_MODES`` deliberately does *not*
#: gain native TUI alongside this.  That surface still has no native
#: branch, and the capability endpoint publishes the two sets separately
#: precisely so one can advertise a mode the other cannot run.
LAUNCHABLE_EXECUTION_MODES: tuple[str, ...] = (em.ACP, em.NATIVE_TUI)

#: Providers with a native-TUI launch branch, which is a narrower claim
#: than "providers this surface can launch".  Native TUI needs a pre-turn
#: session id the provider will resume by id; a provider without both is
#: refused rather than launched into an unresumable pane.
#: Derived from the adapters that actually exist rather than written out
#: by hand.  A provider is native-launchable only if every one of the
#: three surfaces it needs is implemented for it: an argv binder that can
#: bind a session exactly, a readiness receipt kind, and an issuance
#: source.  Listing a provider here that lacked one of those would
#: advertise a capability whose first use fails part-way through a launch.
NATIVE_TUI_PROVIDERS: frozenset[str] = frozenset(
    provider
    for provider in native_tui_launch.SUPPORTED_NATIVE_PROVIDERS
    if provider in _NATIVE_TUI_READINESS_RECEIPT_KINDS
    and provider in _ISSUANCE_SOURCES
    and provider in _PINNED_PROVIDER
)


#: Version of the published native-TUI capability block. Bumped only for
#: a breaking change to its shape, so a consumer can tell "this peer does
#: not advertise native TUI" from "this peer advertises it differently".
NATIVE_TUI_CAPABILITY_SCHEMA_VERSION = 1


def native_tui_capabilities() -> dict[str, Any]:
    """Per-provider native-TUI support, derived from implemented adapters.

    Published *additively* alongside the flat launchable-mode list, which
    is unchanged. The mode list still decides whether native TUI can be
    launched at all; this answers the narrower question of which
    providers have a real adapter behind it — something a flat list
    cannot express, and which a flat relaxation of that list would have
    got wrong by advertising native launch for providers with no branch
    to run it.

    A consumer must satisfy *both* gates, from one read of the
    capabilities response so the two facts cannot come from different
    moments: ``native_tui`` present in the launchable modes, *and*
    ``providers[<provider>].supported``. A peer too old to know about
    this block returns no ``native_tui`` key at all, which is the same
    answer as unsupported and must be a typed refusal taken before any
    reservation exists.

    Keyed by canonical provider. ``claude`` is an executable name and
    appears only under ``executable``.
    """
    from cli_agent_orchestrator.services import provider_contracts as contracts

    providers = {}
    for provider in sorted(NATIVE_TUI_PROVIDERS):
        executable = _PINNED_PROVIDER[provider]
        providers[provider] = {
            "supported": True,
            "id_source": _ISSUANCE_SOURCES[provider],
            "readiness_receipt_kind": _NATIVE_TUI_READINESS_RECEIPT_KINDS[provider],
            "executable": executable,
            "pinned_version": contracts.PINNED_VERSIONS[executable],
            # The exact accepted set, not a range: acceptance is exact-set
            # membership, and publishing a range would invite a consumer
            # to interpolate builds nobody has verified.
            "supported_versions": list(contracts.SUPPORTED_VERSIONS[executable]),
        }
    return {"schema_version": NATIVE_TUI_CAPABILITY_SCHEMA_VERSION, "providers": providers}


def native_control_adapter(provider: str) -> Any:
    """The native control adapter for one provider, or a refusal.

    Resolved by canonical provider rather than passed in, and never
    defaulted. The two adapters use disjoint record schemas and disjoint
    stores precisely so one provider's operation cannot answer for
    another's; picking the adapter from anything other than the binding's
    own provider would give that separation away at the last step.

    Public because the control-input path selects the same adapter for
    the same reason. A second provider->adapter table written next to
    that path could disagree with this one, and the disagreement would
    surface as one provider's keystroke plan typed at another's composer.
    """
    from cli_agent_orchestrator.services import claude_native_control, kimi_native_control

    adapters = {
        "kimi_cli": kimi_native_control,
        "claude_code": claude_native_control,
    }
    adapter = adapters.get(provider)
    if adapter is None:
        raise ManagedLaunchConflict(
            f"provider {provider!r} has no native control adapter; "
            f"native providers are {sorted(adapters)}"
        )
    return adapter


#: The pre-public spelling, kept so existing call sites and their tests
#: keep naming the same one function rather than a second copy of it.
_control_adapter = native_control_adapter


def _observe_turn_state(provider: str, **kwargs: Any) -> Any:
    """Read one pane's turn state through that provider's own detector.

    Dispatched by provider for the same reason the control adapter is:
    each detector describes one provider's rendering, and a shared one
    would eventually read a Claude screen with Kimi's rules and call a
    busy pane idle.
    """
    from cli_agent_orchestrator.services.native_pane_input import (
        observe_claude_turn_state,
        observe_kimi_turn_state,
    )

    observers = {
        "kimi_cli": observe_kimi_turn_state,
        "claude_code": observe_claude_turn_state,
    }
    observer = observers.get(provider)
    if observer is None:
        raise ManagedLaunchConflict(
            f"provider {provider!r} has no native turn-state observer; "
            f"native providers are {sorted(observers)}"
        )
    return observer(**kwargs)


#: The detector name recorded on a readiness observation, per provider.
#: Stored so a reader of a durable receipt knows which detector produced
#: it — two providers' status strings look alike and would otherwise be
#: indistinguishable after the fact.
TURN_OBSERVER_AUTHORITY = {
    "kimi_cli": "observe_kimi_turn_state",
    "claude_code": "observe_claude_turn_state",
}


def _resolve_reserve_mode(request: ManagedLaunchV2ReserveRequest) -> em.ExecutionModeResolution:
    """Resolve the mode at reservation time — before any provider I/O.

    Reserve is the earliest point at which the mode is knowable and the
    last one that is still free of provider effects, so an unresolvable
    or contradictory mode fails here with nothing launched.  Resolution
    failures surface as ``ManagedLaunchConflict`` so callers keep the
    single managed-launch error family instead of having to catch a
    second, unrelated one.
    """
    try:
        return em.resolve(
            launch_input=request.execution_mode,
            worker_class=request.worker_class,
        )
    except em.ExecutionModeError as exc:
        raise ManagedLaunchConflict(str(exc)) from exc


def _allocate_terminal_id(db: Any) -> str:
    for _ in range(128):
        candidate = generate_terminal_id()
        v1_taken = (
            db.query(database.TerminalModel).filter(database.TerminalModel.id == candidate).first()
            is not None
        )
        v2_taken = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter(database.ManagedLaunchV2ReservationModel.terminal_id == candidate)
            .first()
            is not None
        )
        if not v1_taken and not v2_taken:
            return candidate
    raise ManagedLaunchUnavailable("could not allocate a unique terminal id")


def reserve(request: ManagedLaunchV2ReserveRequest) -> tuple[dict[str, Any], bool]:
    """Create or idempotently return one immutable v2 reservation."""
    payload = _validate_reserve_identity(request)
    request_json = _canonical_json(payload)
    resolution = _resolve_reserve_mode(request)
    nonce_digest = hashlib.sha256(request.launch_nonce.encode()).hexdigest()
    try:
        with database.SessionLocal() as db:
            existing = _query(db, request.reservation_id)
            if existing is not None:
                if not _request_matches(existing.request_json, payload):
                    raise ManagedLaunchConflict(
                        "reservation_id is already bound to a different request"
                    )
                # The reserved mode is immutable. A replay that restates
                # a different mode is refused rather than adopted, so a
                # reservation can never change branch under a retry.
                em_existing = _mode_record(existing)
                if not em.is_legacy_row(em_existing):
                    try:
                        em.assert_immutable(em.mode_of_record(em_existing), request.execution_mode)
                    except em.ExecutionModeError as exc:
                        raise ManagedLaunchConflict(str(exc)) from exc
                return _row_dict(existing), False
            now = _now()
            row = database.ManagedLaunchV2ReservationModel(
                reservation_id=request.reservation_id,
                terminal_id=_allocate_terminal_id(db),
                generation=str(uuid.uuid4()),
                protocol_vintage="v2",
                session_name=request.session_name,
                provider=request.provider,
                agent_profile=request.agent_profile,
                caller_id=request.caller_id,
                working_directory=request.working_directory,
                trusted_project_root=request.trusted_project_root,
                obligation_generation=request.obligation_generation,
                task_id=request.task_id,
                run_id=request.run_id,
                launch_nonce_digest=nonce_digest,
                state="reserved",
                request_json=request_json,
                execution_mode=resolution.mode,
                execution_mode_source=resolution.source,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _row_dict(row), True
    except ManagedLaunchError:
        raise
    except IntegrityError:
        with database.SessionLocal() as db:
            existing = _query(db, request.reservation_id)
            if existing is None or not _request_matches(existing.request_json, payload):
                raise ManagedLaunchConflict("concurrent reservation conflict")
            return _row_dict(existing), False
    except Exception as exc:  # noqa: BLE001 - fail closed at the store boundary
        raise ManagedLaunchUnavailable(f"managed-launch v2 reservation failed: {exc}") from exc


def get(reservation_id: str) -> dict[str, Any]:
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"managed-launch v2 query failed: {exc}") from exc


def claim_launch(reservation_id: str) -> tuple[dict[str, Any], bool]:
    """Atomically claim the one no-task provider launch."""
    try:
        with database.SessionLocal() as db:
            updated = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchV2ReservationModel.state == "reserved",
                )
                .update({"state": "launching", "updated_at": _now()}, synchronize_session=False)
            )
            db.commit()
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if updated == 1:
                return _row_dict(row), True
            if row.state in {
                "launching",
                "bound",
                "admitting",
                "admitted",
                "preflight_blocked",
                "launch-failed-bridge",
            }:
                return _row_dict(row), False
            raise ManagedLaunchUnavailable(f"unknown managed-launch v2 state: {row.state!r}")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"managed-launch v2 claim failed: {exc}") from exc


def _build_preflight_failure(row: Any, *, reason: str, detail: str) -> dict[str, Any]:
    """The redacted, identity-bound evidence for one blocked generation.

    ``detail`` passes through the same credential gate the memory/export
    paths use, so raw provider output — which can carry credentials —
    never lands in a durable, GET-queryable record.  ``task_bytes_submitted``
    is a constant here, not a caller claim: reaching this state means the
    v2 launch path submitted no turn.
    """
    redacted, fired = secret_gate.redact_secrets(detail or "")
    return {
        "schema": PREFLIGHT_FAILURE_SCHEMA,
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "obligation_generation": row.obligation_generation,
        "provider": row.provider,
        "reason": reason,
        "detail": redacted,
        "detail_redactions": fired,
        "task_bytes_submitted": False,
        "failed_at": _now(),
    }


def _mark_preflight_blocked(
    reservation_id: str, detail: str, *, reason: str = PREFLIGHT_REASON_GENERIC
) -> dict[str, Any]:
    # The one gate every emission passes through, checked before the row is
    # read so a rejected reason cannot leave a half-written state behind.
    #
    # Refusing is the fail-closed answer even though it costs a 503, because
    # the two failures are not symmetrical. This record is immutable and
    # terminal: an unknown code written here is a code a recovering
    # conductor must branch on forever, with no branch to take and no way
    # to correct it after the fact. A refusal leaves the row in a state a
    # redrive can still resolve correctly once the bug is fixed.
    #
    # Safe to make loud because it can only ever fire on a defect in this
    # module: the reason is never caller-supplied — this is a private
    # function and every call site passes one of the module constants — so
    # no request can reach it.
    if reason not in PREFLIGHT_REASONS:
        raise ManagedLaunchUnavailable(
            f"refusing to record {reason!r} as a preflight cause: it is not one of "
            f"{sorted(PREFLIGHT_REASONS)}, and this evidence is immutable once written"
        )
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if row.state == "preflight_blocked":
                # Terminal and immutable: the first recorded cause stands.
                # A re-driven launch that fails differently must never
                # rewrite evidence a recovery may already have read.
                return _row_dict(row)
            if row.state not in {"reserved", "launching"}:
                raise ManagedLaunchConflict(f"preflight cannot block state {row.state!r}")
            row.state = "preflight_blocked"
            # The evidence is written in the SAME transaction as the state,
            # so the blocked state and its cause commit together or not at
            # all — a persistence failure raises below (fail closed) and no
            # blocked row is ever returned without its evidence.
            if row.preflight_failure_json is None:
                row.preflight_failure_json = _canonical_json(
                    _build_preflight_failure(row, reason=reason, detail=detail)
                )
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"preflight persistence failed: {exc}") from exc


def _mark_launch_failed_bridge(
    reservation_id: str,
    bridge_state: dict[str, Any],
) -> dict[str, Any]:
    """CAS the v2 fork-owned launch/delivery records to never-submitted."""
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        BridgeError,
        validate_launch_failure,
    )

    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            request = _parse_json(row.request_json, {})
            delivery_id = request.get("delivery_id")
            if not isinstance(delivery_id, str) or not delivery_id:
                raise ManagedLaunchConflict(
                    "v2 launch failure requires the immutable reservation delivery_id"
                )
            try:
                failure = validate_launch_failure(
                    bridge_state,
                    reservation_id=row.reservation_id,
                    terminal_id=row.terminal_id,
                    generation=row.generation,
                    delivery_id=delivery_id,
                    provider=row.provider,
                )
            except BridgeError as exc:
                raise ManagedLaunchConflict(str(exc)) from exc
            delivery = {
                "schema": "cao-managed-launch-delivery-terminal-v1",
                "delivery_id": delivery_id,
                "status": "never-submitted",
                "reservation_id": row.reservation_id,
                "terminal_id": row.terminal_id,
                "generation": row.generation,
                "failure_evidence_sha256": failure["evidence_sha256"],
                "finalized_at": failure["failed_at"],
                "launch_failure": failure,
            }
            if row.state == "launch-failed-bridge":
                if _parse_json(row.admission_json, None) != delivery:
                    raise ManagedLaunchConflict(
                        "v2 launch-failed-bridge evidence changed after finalization"
                    )
                return _row_dict(row)
            if row.state != "launching":
                raise ManagedLaunchConflict(
                    f"v2 bridge launch failure cannot finalize state {row.state!r}"
                )
            updated = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchV2ReservationModel.terminal_id == row.terminal_id,
                    database.ManagedLaunchV2ReservationModel.generation == row.generation,
                    database.ManagedLaunchV2ReservationModel.state == "launching",
                    database.ManagedLaunchV2ReservationModel.binding_json.is_(None),
                    database.ManagedLaunchV2ReservationModel.bind_intent_json.is_(None),
                    database.ManagedLaunchV2ReservationModel.admission_json.is_(None),
                )
                .update(
                    {
                        "state": "launch-failed-bridge",
                        "admission_json": _canonical_json(delivery),
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated != 1:
                raise ManagedLaunchConflict(
                    "v2 launch failure lost the exact reservation/generation/delivery CAS"
                )
            return _row_dict(_query(db, reservation_id))
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(
            f"v2 bridge launch failure finalization failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Route-correct v2 recovery surface
#
# A v2 preflight failure used to be a dead end: the row held ``state ==
# preflight_blocked`` with its cause discarded, and there was no v2 verb to
# finalize it — so ``conduct spawn --recover`` reached for the v1 verbs,
# received 404, and left the run and its breaker wedged.  These three verbs
# are the v2-native, idempotent, re-drivable recovery surface.  All of them
# operate only on the isolated v2 store, do zero task/provider I/O, and
# refuse a generation that ever left the zero-byte states — a blocked
# generation is finalized and replaced, never reused.
# ---------------------------------------------------------------------------

# States a zero-byte finalization may be claimed from.
#
# ``preflight_blocked`` never reached a binding at all.  ``bound`` is
# admitted here on a narrower condition, enforced below: only while the row
# carries no durable admission record.  The bind-before-admit seam is what
# makes that safe — an admission journals its intent before it touches the
# transport, so a bound row with no admission has provably never reached the
# write path and no task byte can have crossed.  Without this a launch that
# died between bind and admit was unfinalizable: it could not be finalized
# negative, and it could not be replaced either, because a generation is
# non-reusable once issued.  The generation stayed live forever and wedged
# the run.
#
# ``admitting`` and ``admitted`` are never finalizable, and neither is a
# bound row that does carry an admission: for those, "never submitted" is
# exactly the claim that cannot be proven.
#
# ``launching`` joins them, and it corrects the paragraph above rather than
# extending it. That text said a refusal "costs a finalizable reservation
# and nothing else", which was not true of a launching row as built: with
# no binding it was permanently unfinalizable *and* unreplaceable, which is
# a worse outcome than the refusal it followed. Two live generations sat in
# exactly that state. It is admissible if and only if the row carries no
# binding, no bind intent and no admission — under exactly that condition
# nothing could have delivered a task byte, because admission requires a
# bind and no bind was ever published, so the negative states a fact rather
# than assuming one.
_NEGATIVE_FINALIZABLE_STATES = frozenset({"preflight_blocked", "launching", "bound", "negative"})

#: The columns whose absence proves a ``launching`` generation never
#: reached the write path. Named once because the read-time check and the
#: CAS write condition must be the same set: if they could drift, a row
#: could pass the read and be finalized on a weaker condition than the one
#: that was proven.
_LAUNCHING_ZERO_BYTE_COLUMNS = ("binding_json", "bind_intent_json", "admission_json")


def _observed_pane_identity(db: Any, row: Any) -> Optional[dict[str, Any]]:
    """The pane this generation was observed on, from its own v2 row.

    Read from the durable row rather than re-observed here: this runs while
    finalizing, and a fresh tmux observation would answer "what is there
    now", which for a pane that has since died is a different question from
    "what was this generation on". The finalization needs the second.

    Returns ``None`` when the row is absent or its identity is partial. A
    partial tuple is not published for the same reason it is not stored:
    three of five fields is something a later check could pass against.
    """
    terminal = (
        db.query(database.ManagedLaunchV2TerminalModel)
        .filter(database.ManagedLaunchV2TerminalModel.id == row.terminal_id)
        .first()
    )
    if terminal is None:
        return None
    identity = {
        "server_socket_path": terminal.server_socket_path,
        "session_id": terminal.v2_session_id,
        "window_id": terminal.window_id,
        "pane_id": terminal.pane_id,
        "pane_pid": terminal.v2_pane_pid,
    }
    if any(value is None for value in identity.values()):
        return None
    return identity


def _assert_recovery_identity(row: Any, *, terminal_id: str, generation: str) -> None:
    """Refuse a recovery verb whose identity is not this exact generation."""
    if row.terminal_id != terminal_id or row.generation != generation:
        raise ManagedLaunchConflict(
            "recovery identity does not match the reservation "
            "(terminal_id/generation); refusing before any state change"
        )


def finalize_negative(
    reservation_id: str, request: ManagedLaunchV2NegativeRequest
) -> dict[str, Any]:
    """Finalize a proven zero-byte failure → ``negative``.

    Two states can prove it.  ``preflight_blocked`` never reached a binding.
    ``bound`` proves it too, but only while the row carries no admission
    record: bind-before-admit means an admission journals its intent before
    it touches the transport, so a bound row without one has never reached
    the write path.  That case is not hypothetical — a launch that dies
    between bind and admit lands there, and before this it was unfinalizable
    *and* unreplaceable, because a generation is non-reusable once issued.

    Everything else is refused, and refused for the same reason: for
    ``admitting``, ``admitted``, or a bound row that does carry an
    admission, "never submitted" is precisely the claim that cannot be
    proven, and asserting it would be a lie about spend.

    Idempotent by construction: a no-op from ``negative``, and the first
    finalization wins — a later call with a different ``finalize_id`` is an
    idempotent success that does not rewrite the recorded finalization.

    The stored ``preflight_failure`` evidence is preserved unchanged; this
    verb records only that the zero-byte failure has been adopted and
    finalized so the caller may release its breaker.
    """
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            _assert_recovery_identity(
                row, terminal_id=request.terminal_id, generation=request.generation
            )
            if request.obligation_generation != row.obligation_generation:
                raise ManagedLaunchConflict(
                    "recovery obligation_generation does not match the reservation row"
                )
            if row.state not in _NEGATIVE_FINALIZABLE_STATES:
                raise ManagedLaunchConflict(
                    f"zero-byte finalization requires state 'preflight_blocked', a "
                    f"'launching' row with no binding/bind intent/admission, or a "
                    f"'bound' row with no admission, not {row.state!r}"
                )
            if row.state == "launching":
                present = [
                    column
                    for column in _LAUNCHING_ZERO_BYTE_COLUMNS
                    if getattr(row, column) is not None
                ]
                if present:
                    # Any one of these means the generation reached far
                    # enough that "never submitted" is no longer a fact this
                    # verb can read off the row. A bind intent counts: it is
                    # journaled before the binding is published, so its
                    # presence means a bind was in flight and may still land.
                    raise ManagedLaunchConflict(
                        "zero-byte finalization refused: the launching reservation "
                        f"already carries {', '.join(present)}, so no-bytes-submitted "
                        "cannot be proven from this state"
                    )
            if row.state == "bound" and row.admission_json is not None:
                # The row reached the write path. Whether a byte actually
                # landed is exactly what an admission record exists to say,
                # and it is not this verb's to decide: finalizing here would
                # be asserting "never submitted" over the top of evidence
                # that submission was attempted.
                raise ManagedLaunchConflict(
                    "zero-byte finalization refused: the bound reservation carries an "
                    "admission record, so no-bytes-submitted cannot be proven — "
                    "reconcile the admission instead"
                )
            if row.state == "negative":
                # Already finalized: return the durable record unchanged so
                # a re-driven recovery converges instead of double-writing.
                return _row_dict(row)
            redacted_reason, _fired = secret_gate.redact_secrets(request.reason or "")
            claimed_state = row.state
            negative = {
                "schema": "cao-managed-launch-v2-negative-v1",
                "finalize_id": request.finalize_id,
                "terminal_id": row.terminal_id,
                "generation": row.generation,
                "obligation_generation": row.obligation_generation,
                "reason": redacted_reason,
                "task_bytes_submitted": False,
                # Which zero-byte state this was claimed from. A reader
                # auditing the finalization should not have to infer whether
                # the launch died before its binding or after it.
                "finalized_from_state": claimed_state,
                # The pane this generation was observed on, carried into the
                # finalization so a live pane stays reconcilable under its
                # exact generation instead of being orphaned by the very call
                # that closed its reservation. Preservation and finalizability
                # are then the same thing rather than opposites: the
                # generation is preserved *as* a typed negative naming what
                # was observed. Null when nothing was observed -- absent is a
                # different claim from unobserved, and only the row can say.
                "observed_pane_identity": _observed_pane_identity(db, row),
                "finalized_at": _now(),
            }
            # The CAS re-states, as a write condition, exactly the property
            # that was just proven by reading — so nothing can race the row
            # into a live state between the two.  ``admission_json`` must
            # still be NULL in both cases: that column is what a crossing
            # admission would have written, and its absence at write time is
            # the whole proof that no task byte was submitted.  The binding
            # column differs by state: a blocked generation never wrote one,
            # while a bound one is expected to carry it.
            filters = [
                database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                database.ManagedLaunchV2ReservationModel.terminal_id == row.terminal_id,
                database.ManagedLaunchV2ReservationModel.generation == row.generation,
                database.ManagedLaunchV2ReservationModel.state == claimed_state,
                database.ManagedLaunchV2ReservationModel.admission_json.is_(None),
            ]
            if claimed_state == "launching":
                # The same three columns the read proved absent, re-stated as
                # the write condition. A bind landing concurrently writes one
                # of them, so it wins and this finalization loses; a bind
                # arriving after finds the row negative and refuses. Exactly
                # one of the two can succeed.
                for column in _LAUNCHING_ZERO_BYTE_COLUMNS:
                    filters.append(
                        getattr(database.ManagedLaunchV2ReservationModel, column).is_(None)
                    )
            elif claimed_state == "preflight_blocked":
                filters.append(database.ManagedLaunchV2ReservationModel.binding_json.is_(None))
            else:
                filters.append(database.ManagedLaunchV2ReservationModel.binding_json.isnot(None))
            updated = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(*filters)
                .update(
                    {
                        "state": "negative",
                        "admission_json": _canonical_json(negative),
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated != 1:
                raise ManagedLaunchConflict(
                    f"v2 zero-byte finalization lost the exact {claimed_state} CAS"
                )
            return _row_dict(_query(db, reservation_id))
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"v2 negative finalization failed: {exc}") from exc


def reconcile(reservation_id: str) -> dict[str, Any]:
    """Read-only adoption of durable v2 facts — never launches, sends, or deletes.

    Returns the full row (including the immutable ``preflight_failure``
    evidence), whether the fork-owned terminal record still exists, and
    whether the row is past the point where a fresh launch could still be
    claimed.  Safe to call any number of times.
    """
    record = get(reservation_id)
    try:
        with database.SessionLocal() as db:
            terminal_present = (
                db.query(database.ManagedLaunchV2TerminalModel)
                .filter(
                    database.ManagedLaunchV2TerminalModel.id == record["terminal_id"],
                    database.ManagedLaunchV2TerminalModel.generation == record["generation"],
                )
                .first()
                is not None
            )
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"v2 reconcile failed: {exc}") from exc
    return {
        **record,
        "terminal_record_present": terminal_present,
        "recovery_only": record["state"] != "reserved",
    }


def cleanup(reservation_id: str, request: ManagedLaunchV2CleanupRequest) -> dict[str, Any]:
    """Release the fork-owned terminal record for a finalized zero-byte generation.

    Writes a durable cleanup record, once, first-writer-wins by
    ``cleanup_id``. That record — not the absence of the terminal row — is
    what projects the response's top-level ``state`` as ``cleaned``. An
    absent row is not proof a cleanup happened: it can be missing for
    having never existed, or for having been removed by something else, and
    projecting from it would report a cleanup nobody performed.

    ``cleaned`` is absorbing and idempotent. A retry with the same
    ``cleanup_id`` replays the byte-identical stored proof — the same
    ``cleaned_at``, the same ``terminal_record_removed`` — and performs no
    second delete. It is the *answer* that has to be idempotent, not just
    the effect: the answer is what crosses the boundary, and a consumer
    that persists a different proof on a retry has persisted a different
    fact. Recomputing it meant a retry after the first success reported
    ``terminal_record_removed: false`` and a fresh timestamp, so which
    proof a consumer stored depended on when it happened to ask.

    A *different* ``cleanup_id`` against an already-cleaned generation is a
    conflict, not a second cleanup.

    Valid only once the generation is finalized (``negative``); refused
    while it is still live. This releases only the v2 terminal *metadata*
    row — it never tears down a pane or process, which stays the
    destructive endpoint's job under its own containment gate.
    """
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            _assert_recovery_identity(
                row, terminal_id=request.terminal_id, generation=request.generation
            )
            stored = _parse_json(getattr(row, "cleanup_json", None), None)
            if stored is not None:
                _assert_same_cleanup(stored, request)
                # Absorbing: the stored proof is replayed exactly, and no
                # second delete is attempted.
                return _row_dict(row)
            if row.state != "negative":
                raise ManagedLaunchConflict(
                    f"v2 cleanup requires the finalized state 'negative', not {row.state!r}"
                )

        cleaned_at = _now()

        def _build(terminal_record_removed: bool) -> str:
            return _canonical_json(
                {
                    "schema": "cao-managed-launch-v2-cleanup-v1",
                    "cleanup_id": request.cleanup_id,
                    "terminal_id": request.terminal_id,
                    "generation": request.generation,
                    "terminal_record_removed": terminal_record_removed,
                    "cleaned_at": cleaned_at,
                }
            )

        database.record_v2_cleanup_first_writer(
            reservation_id,
            build_record=_build,
            terminal_id=request.terminal_id,
            generation=request.generation,
        )
        # Whether this call won or lost the race, the durable record is now
        # the single account of the cleanup. A loser converges on the
        # winner's proof when it carries the same id and conflicts when it
        # does not — the same rule the replay path applies, so a race and a
        # retry cannot be told apart by their outcome.
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            stored = _parse_json(getattr(row, "cleanup_json", None), None)
            if stored is None:
                raise ManagedLaunchUnavailable(
                    "v2 cleanup recorded no durable proof; refusing to report a "
                    "cleanup that cannot be read back"
                )
            _assert_same_cleanup(stored, request)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"v2 cleanup failed: {exc}") from exc


def _assert_same_cleanup(stored: dict[str, Any], request: ManagedLaunchV2CleanupRequest) -> None:
    """Refuse a second, different cleanup of an already-cleaned generation.

    Named rather than inlined because the replay path and the lost-race
    path must apply exactly the same rule: if they could differ, a caller
    could learn which one it took from the outcome, and a race would stop
    being indistinguishable from a retry.
    """
    if stored.get("cleanup_id") != request.cleanup_id:
        raise ManagedLaunchConflict(
            f"v2 generation is already cleaned under cleanup_id "
            f"{stored.get('cleanup_id')!r}; {request.cleanup_id!r} would be a second "
            "cleanup of one generation, not a retry of the first"
        )


def _validate_readiness_for_bind(row: Any, receipt: dict[str, Any]) -> None:
    """Accept only the exact provider-native readiness receipt for this row.

    The allowlist is selected by the row's *bound* mode, never by what the
    receipt says about itself, so a receipt cannot nominate the table that
    would accept it.
    """
    request = _parse_json(row.request_json, {})
    kinds = (
        _NATIVE_TUI_READINESS_RECEIPT_KINDS
        if em.mode_of_record(_mode_record(row)) == em.NATIVE_TUI
        else _READINESS_RECEIPT_KINDS
    )
    expected_kind = kinds.get(row.provider)
    if expected_kind is None or receipt.get("provider_receipt_kind") != expected_kind:
        raise ManagedLaunchConflict(
            f"readiness receipt kind is not the allowlisted provider-native kind: "
            f"{receipt.get('provider_receipt_kind')!r}"
        )
    expected = {
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "working_directory": row.working_directory,
    }
    mismatches = {
        key: {"expected": value, "observed": receipt.get(key)}
        for key, value in expected.items()
        if receipt.get(key) != value
    }
    # Model is compared outside the exact-equality set because a request
    # and an observation are not always the same string. Kimi reads its
    # route back as configured, so equality is right there. Claude reports
    # a *resolved* id — an alias request for the latest model in a family
    # comes back as the concrete member with a context-window suffix — so
    # equality would refuse every correctly-routed Claude session. The
    # family comparison is not looser: it refuses a different family, and
    # a full-name request is still satisfied by exactly itself.
    from cli_agent_orchestrator.services import claude_native_launch

    expected_model = request.get("expected_model")
    if row.provider == "claude_code":
        model_ok = claude_native_launch.observed_model_matches(
            str(expected_model or ""), receipt.get("model")
        )
    else:
        model_ok = receipt.get("model") == expected_model
    if not model_ok:
        mismatches["model"] = {"expected": expected_model, "observed": receipt.get("model")}
    # Effort is compared through the effort vocabulary rather than by
    # string equality, because the route and a truthful receipt speak
    # different alphabets here: a provider-default route says
    # ``provider-default`` and an honest native receipt says ``None``,
    # since that session was never told an effort and never read one back.
    # Raw equality refuses every such launch — after the pane exists and
    # the reservation is open — which moves the very symptom this sentinel
    # was introduced to remove from attestation to bind. The check is not
    # relaxed: a receipt reporting a concrete effort for such a route is
    # still a mismatch, because it means the session settled somewhere
    # nobody selected.
    #
    # The observability is taken from the *declaration* for this (provider,
    # model) pair, never from what the receipt says about itself — a
    # receipt that could nominate its own comparison could always nominate
    # the one it passes.
    expected_effort = request.get("expected_effort")
    observability = provider_contracts.effort_observability(
        row.provider, request.get("expected_model")
    )
    if not provider_contracts.effort_receipt_matches(
        expected_effort, receipt.get("effort"), observability=observability
    ):
        mismatches["effort"] = {
            "expected": expected_effort,
            "observed": receipt.get("effort"),
            "observability": observability,
        }
    for field in ("receipt_id", "provider_session_id", "provider_version"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            mismatches[field] = {"expected": "non-empty string", "observed": receipt.get(field)}
    if receipt.get("receipt_id") != receipt.get("provider_session_id"):
        mismatches["receipt_id"] = {
            "expected": receipt.get("provider_session_id"),
            "observed": receipt.get("receipt_id"),
        }
    if receipt.get("model_input_ready") is not True:
        mismatches["model_input_ready"] = {
            "expected": True,
            "observed": receipt.get("model_input_ready"),
        }
    if mismatches:
        raise ManagedLaunchConflict(
            "readiness receipt is not bound to the exact v2 reservation: "
            + _canonical_json(mismatches)
        )
    try:
        check_pinned_version(_PINNED_PROVIDER[row.provider], receipt["provider_version"])
    except ProviderContractError as exc:
        raise ManagedLaunchConflict(str(exc)) from exc


def bind_native(reservation_id: str, request: ManagedLaunchV2BindRequest) -> dict[str, Any]:
    """Journal the native-bound identity receipts; zero task bytes before this.

    Reads the provider-native readiness receipt from the generation's
    bridge (a fork-owned fact), derives the creation/binding payload
    digests, publishes the fork-owned binding record, issues the
    producer fencing token, and transitions ``launching → bound``.

    Crash safety: the bind intent — the exact canonical creation/binding/
    route payload bytes, the binding record, and the fencing token — is
    journaled to the reservation row and committed BEFORE any immutable
    external publication.  A crash on either side of the SQL/filesystem
    boundary is reconciled on retry against those journaled bytes: an
    already-published binding record that matches the intent is adopted
    and the row converges to ``bound``; a mismatch is a conflict, never
    a silent re-publication.
    """
    from cli_agent_orchestrator.services.managed_provider_bridge import read_state

    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if row.terminal_id != request.terminal_id or row.generation != request.generation:
                raise ManagedLaunchConflict("bind identity does not match the reservation")
            if row.state == "bound":
                existing = _parse_json(row.binding_json, None)
                if existing and existing.get("attempt_id") == request.attempt_id:
                    return _row_dict(row)
                raise ManagedLaunchConflict("reservation is already bound to a different attempt")
            if row.state != "launching":
                raise ManagedLaunchConflict(
                    f"native bind requires state 'launching', not {row.state!r}"
                )
            # The reserved mode is the mode of record for this bind. A
            # caller that restates a different one is refused here,
            # before any binding bytes are computed: modes are separate
            # launch branches and a bind must never cross them.
            try:
                bound_mode = em.assert_immutable(
                    em.mode_of_record(_mode_record(row)), request.execution_mode
                )
            except em.ExecutionModeError as exc:
                raise ManagedLaunchConflict(str(exc)) from exc

            intent = _parse_json(row.bind_intent_json, None)
            if intent is not None:
                if intent.get("schema") != "cao-managed-v2-bind-intent-v1":
                    raise ManagedLaunchUnavailable("bind intent journal has an unknown schema")
                if intent.get("attempt_id") != request.attempt_id:
                    raise ManagedLaunchConflict(
                        "a bind intent for a different attempt is already journaled"
                    )
            if intent is None:
                state = read_state(reservation_id)
                receipt = (state or {}).get("readiness")
                if not isinstance(receipt, dict) or (state or {}).get("state") != "ready":
                    # The one transient refusal on this surface. Typed, so
                    # a consumer retries the same attempt instead of
                    # inferring transience from the row state — an
                    # inference that read every permanent conflict leaving
                    # the row ``launching`` as a slow start.
                    raise ManagedLaunchNotReady(
                        "native bind requires the bridge's durable ready state with a "
                        "provider-native readiness receipt; none is published yet",
                        reason=REASON_BIND_BRIDGE_NOT_DURABLY_READY,
                    )
                # The same completeness rule the projection applies, asked
                # here and answered once. Binding on a receipt the sibling
                # would reject produces a generation that is bound and
                # projects ``null`` forever: the consumer then waits for a
                # readiness that has already been consumed and can never
                # arrive. Two rules for one question is how that gap
                # opened, so there is now one.
                missing = _incomplete_readiness_fields(row, receipt)
                if missing:
                    raise ManagedLaunchNotReady(
                        "the durable readiness receipt is not yet complete enough to "
                        f"publish as a binding proof; missing {sorted(missing)}",
                        reason=REASON_BIND_BRIDGE_NOT_DURABLY_READY,
                    )
                _validate_readiness_for_bind(row, receipt)
                intent = _build_bind_intent(db, row, reservation_id, request, receipt, bound_mode)
                # Journal the intent BEFORE the immutable external
                # publication; this commit is the recoverable boundary.
                row.bind_intent_json = _canonical_json(intent)
                row.updated_at = _now()
                db.commit()
                db.refresh(row)
            # Checked on both the fresh and the reconciled path, from the
            # journaled binding rather than the live receipt, so a retry
            # re-validates the exact session it is about to publish.
            _assert_session_not_foreign_held(
                row, intent["binding"]["native_session_id"], bound_mode
            )
            _reconcile_and_complete_bind(db, row, reservation_id, intent)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"native bind failed: {exc}") from exc


def _assert_session_not_foreign_held(row: Any, native_session_id: str, bound_mode: str) -> None:
    """Refuse a bind whose provider session another owner already holds.

    A provider session is a single-writer resource: two attachments
    interleave turns undetectably, and neither side can tell that the
    transcript it is reading contains another controller's work.  This is
    the bind-time half of that guarantee — it refuses to bind a
    generation onto a session that a *different* live owner holds, and
    refuses in either direction across modes, since ACP and the native
    TUI must never attach to one provider session at the same time.

    Deliberately a *check*, not a claim.  This function does not declare
    an attachment, because at bind time the ACP bootstrap that minted the
    session is still holding it — admission itself flows through that
    bridge.  Declaring ownership here would require asserting
    ``bootstrap_detached_before_launch``, which is false at this point,
    so the claim belongs to the later native-launch step where that
    obligation is genuinely true.  Recording a false obligation would
    corrupt the very evidence the attachment store exists to hold.

    A frozen ``ambiguous`` row refuses unconditionally: ambiguity means
    the owner is precisely what could not be established, and binding
    into that is the double-attach this exists to prevent.
    """
    held = native_attachment.get(row.provider, native_session_id)
    if held is None:
        return
    if held["state"] == native_attachment.AMBIGUOUS:
        raise ManagedLaunchConflict(
            f"{row.provider} session {native_session_id} has a frozen ambiguous "
            f"attachment ({held.get('ambiguity_reason')!r}); refusing to bind onto a "
            "session whose owner could not be established"
        )
    if held["state"] not in native_attachment.LIVE_STATES:
        return
    owner = held["owner"]
    if owner["terminal_id"] == row.terminal_id and owner["generation"] == row.generation:
        # The same generation re-binding its own session. Still not a free
        # pass: the recorded owner mode must match the mode being bound,
        # so a generation cannot switch branch under a retry.
        try:
            em.assert_same_mode(
                owner["execution_mode"],
                bound_mode,
                context=f"bind of {row.provider} session {native_session_id}",
            )
        except em.ExecutionModeError as exc:
            raise ManagedLaunchConflict(str(exc)) from exc
        return
    raise ManagedLaunchConflict(
        f"{row.provider} session {native_session_id} is already held in "
        f"{owner['execution_mode']!r} mode by terminal={owner['terminal_id']} "
        f"generation={owner['generation']}; a {bound_mode!r} bind for "
        f"terminal={row.terminal_id} generation={row.generation} would be a second "
        "concurrent attachment to one provider session"
    )


def _build_bind_intent(
    db: Any,
    row: Any,
    reservation_id: str,
    request: ManagedLaunchV2BindRequest,
    receipt: dict[str, Any],
    bound_mode: str,
) -> dict[str, Any]:
    """Compute the exact bind intent (payload bytes + token) for one attempt."""
    # The reservation's own durable request. Named apart from ``request``,
    # which is the *bind* request: the assigned/requested route facts are
    # statements about what was reserved, not about this attempt.
    reserved_request = _parse_json(row.request_json, {})
    terminal = (
        db.query(database.TerminalModel)
        .filter(database.TerminalModel.id == row.terminal_id)
        .first()
    )
    if terminal is None:
        terminal = (
            db.query(database.ManagedLaunchV2TerminalModel)
            .filter(database.ManagedLaunchV2TerminalModel.id == row.terminal_id)
            .first()
        )
    tmux_incarnation = (
        f"tmux:{terminal.tmux_session}:{terminal.window_id}:{terminal.pane_id}"
        if terminal is not None
        else f"tmux:{row.session_name}:unknown:unknown"
    )
    # This digest hashes the **assigned** route, stated here because it
    # binds a failure domain and must be the same value on both sides of
    # every later comparison. Assigned is the only choice that is always
    # present: the observed effort is null for a provider-default route
    # and for any pair whose effort cannot be read before a turn, so a
    # digest over the observation would change meaning with the provider
    # rather than with the route, and two peers comparing it would differ
    # for reasons neither could see.
    route_digest = hashlib.sha256(
        _canonical_json(
            {
                "model": reserved_request["expected_model"],
                "effort": reserved_request["expected_effort"],
                "agent_profile": row.agent_profile,
            }
        ).encode()
    ).hexdigest()
    created_at = _now()
    # The worktree head is a fork-observable fact (read-only git);
    # a worktree that cannot report its head fails the bind closed.
    import subprocess

    try:
        head_proc = subprocess.run(
            ["git", "-C", row.working_directory, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedLaunchConflict(
            f"worktree head is not readable for the binding: {exc}"
        ) from exc
    head = head_proc.stdout.strip()
    if (
        head_proc.returncode != 0
        or len(head) != 40
        or any(ch not in "0123456789abcdef" for ch in head)
    ):
        raise ManagedLaunchConflict(
            "worktree head is not a full lowercase hex OID; the binding " "requires the exact head"
        )
    creation_payload = recovery_receipts.creation_payload(
        provider=_PINNED_PROVIDER[row.provider],
        native_id=receipt["provider_session_id"],
        provider_version=receipt["provider_version"],
        issuance_source=_ISSUANCE_SOURCES[row.provider],
        obligation_generation=row.obligation_generation,
        task_id=row.task_id,
        run_id=row.run_id,
        created_at=created_at,
    )
    binding_payload = recovery_receipts.binding_payload(
        provider=_PINNED_PROVIDER[row.provider],
        native_id=receipt["provider_session_id"],
        launch_nonce_digest=row.launch_nonce_digest,
        provider_process_identity=f"bridge:{reservation_id}",
        tmux_incarnation=tmux_incarnation,
        terminal_generation=row.generation,
        worktree_realpath=row.working_directory,
        repository=os.path.basename(row.working_directory),
        head=head,
        assigned_route_digest=route_digest,
        bound_at=created_at,
    )
    token = heartbeat_store.issue_fencing_token(
        COMPANION_DIR, row.terminal_id, row.generation, request.attempt_id
    )
    # The fork-side route fact: an unobserved route payload (PF-2 is
    # red for every pinned provider — no provider-observed fields may
    # be claimed).  The conductor binds the §6.3 segment chain from
    # these facts; the heartbeat's route field binds this digest.
    route_payload = recovery_receipts.route_payload(
        provider=_PINNED_PROVIDER[row.provider],
        native_id=receipt["provider_session_id"],
        authority_status="unobserved",
        # Assigned and requested are statements about what this route ASKED
        # FOR, so they come from the reservation's own durable request. The
        # observed fields below stay null because PF-2 is red and nothing
        # here may claim a provider observation.
        #
        # They used to be read from the receipt, which was indistinguishable
        # while a receipt merely echoed the request. Once the receipt became
        # honest, a provider-default route reported a null *observed* effort
        # and that null arrived here as the *assigned* one — which the
        # receipt contract rightly refuses, because an assigned effort is
        # never unknown. A live Kimi generation with complete readiness
        # failed its bind with "assigned_effort must be a non-empty string"
        # for exactly that reason. The two fields answer different
        # questions and only the observed one may be absent.
        assigned_model=reserved_request["expected_model"],
        assigned_effort=reserved_request["expected_effort"],
        assigned_policy_sha256=receipt.get("profile_sha256") or ("0" * 64),
        assigned_profile_sha256=receipt.get("profile_sha256") or ("0" * 64),
        assigned_config_sha256=receipt.get("protected_config_sha256") or ("0" * 64),
        requested_model=reserved_request["expected_model"],
        requested_effort=reserved_request["expected_effort"],
        observed_model=None,
        observed_effort=None,
        protocol_version=None,
        event_sequence=None,
        native_turn_id=None,
        attested_at=created_at,
    )
    route_payload_digest = recovery_receipts.payload_digest(route_payload)
    # Both receipts carry the execution mode, and the binding digest an
    # admit call must present is computed over the binding — so a
    # receipt minted under one mode cannot satisfy admission under the
    # other.  This is the concrete reason the tag lives in the receipt
    # rather than only in the row: the row can be re-read, but the
    # digest is what the admission actually checks.
    binding_record = {
        "schema": "cao-generation-binding-v1",
        "reservation_id": reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "attempt_id": request.attempt_id,
        "launch_nonce_digest": row.launch_nonce_digest,
        "fencing_token_id": token.id,
        "provider": row.provider,
        "execution_mode": bound_mode,
        "native_session_id": receipt["provider_session_id"],
        "assigned_policy_sha256": receipt.get("profile_sha256"),
        "route_payload_sha256": route_payload_digest,
        "bound_at": created_at,
    }
    binding = {
        "schema": "cao-managed-v2-native-binding-v1",
        "attempt_id": request.attempt_id,
        "execution_mode": bound_mode,
        "native_session_id": receipt["provider_session_id"],
        "provider_version": receipt["provider_version"],
        "issuance_source": _ISSUANCE_SOURCES[row.provider],
        "creation_payload_sha256": recovery_receipts.payload_digest(creation_payload),
        "binding_payload_sha256": recovery_receipts.payload_digest(binding_payload),
        "fencing_token_id": token.id,
        "fence_no": token.fence_no,
        "assigned_route_digest": route_digest,
        "bound_at": created_at,
    }
    import base64

    return {
        "schema": "cao-managed-v2-bind-intent-v1",
        "attempt_id": request.attempt_id,
        "fencing_token": token.as_dict(),
        # The exact canonical payload bytes (base64 for JSON durability),
        # not only their digests: a reconciled retry verifies the journaled
        # bytes still digest to the journaled binding's payload digests.
        "creation_payload_b64": base64.b64encode(creation_payload).decode("ascii"),
        "binding_payload_b64": base64.b64encode(binding_payload).decode("ascii"),
        "route_payload_b64": base64.b64encode(route_payload).decode("ascii"),
        "binding_record": binding_record,
        "binding": binding,
    }


def _reconcile_and_complete_bind(
    db: Any, row: Any, reservation_id: str, intent: dict[str, Any]
) -> None:
    """Converge the journaled intent across the SQL/filesystem boundary.

    The binding record may already exist (crash after publication,
    before the SQL commit): it must match the journaled bytes exactly
    and the journaled fencing token must still be the registered one;
    then the row commits ``bound``.  Absent the record, it is published
    now from the journaled bytes.
    """
    from cli_agent_orchestrator.services.destructive_endpoint import binding_record_path

    record_path = binding_record_path(COMPANION_DIR, row.terminal_id, row.generation)
    expected_record = intent["binding_record"]
    if record_path.exists():
        try:
            existing = json.loads(record_path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedLaunchConflict(
                f"existing binding record is unreadable; cannot reconcile: {exc}"
            ) from exc
        if existing != expected_record:
            raise ManagedLaunchConflict(
                "existing binding record does not match the journaled bind "
                "intent; refusing to overwrite immutable publication"
            )
    registered = heartbeat_store.current_fencing_token(COMPANION_DIR, row.terminal_id)
    if registered is None or registered.id != intent["fencing_token"]["id"]:
        raise ManagedLaunchConflict(
            "journaled fencing token is not the registered one; refusing to bind"
        )
    # The journaled canonical payload bytes must still digest to the
    # journaled binding's payload digests (journal tamper check).
    import base64

    binding = intent["binding"]
    for field, digest_key in (
        ("creation_payload_b64", "creation_payload_sha256"),
        ("binding_payload_b64", "binding_payload_sha256"),
    ):
        if recovery_receipts.payload_digest(base64.b64decode(intent[field])) != binding[digest_key]:
            raise ManagedLaunchConflict(
                "journaled bind payload bytes do not match the journaled "
                "binding digests; refusing to reconcile"
            )
    if not record_path.exists():
        write_binding_record(
            COMPANION_DIR,
            terminal_id=row.terminal_id,
            generation=row.generation,
            reservation_id=reservation_id,
            attempt_id=intent["attempt_id"],
            launch_nonce_digest=row.launch_nonce_digest,
            fencing_token_id=intent["fencing_token"]["id"],
            provider=row.provider,
            native_session_id=expected_record["native_session_id"],
            assigned_policy_sha256=expected_record["assigned_policy_sha256"],
            route_payload_sha256=expected_record["route_payload_sha256"],
            bound_at=expected_record["bound_at"],
            # Read from the journaled record, not recomputed, so the
            # published bytes always equal the bytes a later reconcile
            # compares against.  ``.get`` because an intent journaled
            # before the execution-mode contract has no such key: that
            # record must publish exactly as it was journaled, without a
            # mode grafted on.
            execution_mode=expected_record.get("execution_mode"),
        )
    row.binding_json = _canonical_json(intent["binding"])
    row.state = "bound"
    row.updated_at = _now()
    db.commit()
    db.refresh(row)


def _admission_identity(request: ManagedLaunchV2AdmitRequest) -> dict[str, Any]:
    """The immutable identity one delivery id is bound to.

    Shared by the claim, the replay check, and the pre-I/O refusal so the
    three cannot disagree about what "the same delivery" means.  A
    refusal that recorded fewer fields than the claim would let a replay
    carrying a different message be answered from it.
    """
    return {
        "delivery_id": request.delivery_id,
        "message_sha256": request.message_sha256,
        "sender_id": request.sender_id,
        "orchestration_type": request.orchestration_type,
        "context": request.context.model_dump(mode="json"),
        "native_binding_digest": request.native_binding_digest,
    }


def _assert_same_admission_identity(
    existing: dict[str, Any], request: ManagedLaunchV2AdmitRequest
) -> None:
    """Refuse a delivery id that has come back carrying something else.

    The same id with a changed message, sender, context, orchestration
    type, or binding is a different immutable identity, never a safe
    replay — answering it from the stored record would report one task's
    outcome for another's bytes.
    """
    mismatches = [
        key for key, value in _admission_identity(request).items() if existing.get(key) != value
    ]
    if mismatches:
        raise ManagedLaunchConflict(
            f"delivery_id is already bound to a different admission identity: {sorted(mismatches)}"
        )


def _readiness_observation(
    *,
    pane_id: Optional[str],
    provider_status: Optional[str],
    input_ready: bool,
    detail: Optional[str],
    authority: str = "observe_native_turn_state",
) -> dict[str, Any]:
    """One record of whether a pane could actually be typed into, and when.

    Deliberately the same shape wherever readiness is decided, because a
    refusal that cited different evidence than the launch receipt would
    let the two disagree about the very thing they both claim to know.
    ``provider_status`` is ``None`` only when no observation was made at
    all — "we could not look" is not a reading of the screen, and
    flattening the two would let an unreadable pane pass as a quiet one.
    """
    return {
        # Named so a reader of the stored record knows which detector
        # produced it without having to guess from the status string.
        # Per-provider, because the two detectors read different
        # renderings and a shared label would make a Claude observation
        # indistinguishable from a Kimi one in the stored receipt.
        "authority": authority,
        "observed_at": _now(),
        "pane_id": pane_id,
        "provider_status": provider_status,
        "input_ready": input_ready,
        "detail": detail,
    }


def _is_retryable_refusal(admission: Optional[dict[str, Any]], delivery_id: str) -> bool:
    """Whether a stored admission is a refusal that a retry may supersede.

    True only for a refusal this exact delivery id earned for a reason
    that may stop being true — never for one that wrote bytes, may have
    written bytes, or named a permanent mismatch.  Everything this
    returns False for is a record that must be answered, not replaced.
    """
    if not isinstance(admission, dict):
        return False
    return (
        admission.get("status") == "refused"
        and admission.get("delivery_id") == delivery_id
        and admission.get("refusal_reason") in _RETRYABLE_REFUSAL_REASONS
    )


def claim_admission(
    reservation_id: str, request: ManagedLaunchV2AdmitRequest
) -> tuple[dict[str, Any], bool]:
    """Claim the one task admission — only against a matching native_bound."""
    actual_digest = hashlib.sha256(request.message.encode("utf-8")).hexdigest()
    if actual_digest != request.message_sha256:
        raise ManagedLaunchConflict("message_sha256 does not match message bytes")
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if request.delivery_id != _parse_json(row.request_json, {}).get("delivery_id"):
                raise ManagedLaunchConflict(
                    "delivery_id does not match the immutable v2 reservation delivery identity"
                )
            record = _row_dict(row)
            expected_digest = native_binding_digest(record)
            if expected_digest is None or request.native_binding_digest != expected_digest:
                # Bind-before-admit: without the journaled native_bound
                # reference the admission never happens — zero task bytes.
                raise ManagedLaunchConflict(
                    "task admission requires the journaled native_bound reference; "
                    "zero task bytes are sent without it"
                )
            # A delivery refused before any I/O leaves a durable record so
            # a lost response cannot read as maybe-sent. That record must
            # not then become the thing that blocks the retry it exists to
            # invite, so a claim may supersede it -- and only it. The swap
            # is conditioned on those exact stored bytes rather than on
            # "some refusal is present", so a refusal that changed between
            # the read and the write loses the race instead of being
            # silently overwritten.
            prior_admission_json = row.admission_json
            existing_before = _parse_json(prior_admission_json, None)
            superseding_refusal = _is_retryable_refusal(existing_before, request.delivery_id)
            if superseding_refusal:
                _assert_same_admission_identity(existing_before or {}, request)
            admission = {
                **_admission_identity(request),
                "status": "io-attempted",
                "attempted_at": _now(),
            }
            updated = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchV2ReservationModel.state == "bound",
                    database.ManagedLaunchV2ReservationModel.binding_json.is_not(None),
                    (
                        database.ManagedLaunchV2ReservationModel.admission_json
                        == prior_admission_json
                        if superseding_refusal
                        else database.ManagedLaunchV2ReservationModel.admission_json.is_(None)
                    ),
                )
                .update(
                    {
                        "admission_json": _canonical_json(admission),
                        "state": "admitting",
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            row = _query(db, reservation_id)
            if updated == 1:
                return _row_dict(row), True
            existing = _parse_json(row.admission_json, None)
            if existing is not None:
                # Replay binds the FULL admission identity: the same
                # delivery id with a changed message, sender, context,
                # orchestration type, or binding is a different immutable
                # identity and is refused — never treated as a safe replay.
                _assert_same_admission_identity(existing, request)
                return _row_dict(row), False
            raise ManagedLaunchConflict(f"task admission requires state 'bound', not {row.state!r}")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"task admission claim failed: {exc}") from exc


def complete_admission(
    reservation_id: str,
    delivery_id: str,
    provider_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Complete admission from the provider-native submission receipt."""
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            admission = _parse_json(row.admission_json, None)
            if not admission or admission.get("delivery_id") != delivery_id:
                raise ManagedLaunchConflict("delivery_id does not match the admission claim")
            if admission.get("status") == "admitted":
                if admission.get("provider_submission_receipt") != provider_receipt:
                    raise ManagedLaunchConflict(
                        "provider submission receipt changed after admission"
                    )
                return _row_dict(row)
            if row.state != "admitting":
                raise ManagedLaunchConflict(f"admission cannot complete from state {row.state!r}")
            mode = em.mode_of_record(_mode_record(row))
            if mode == em.NATIVE_TUI:
                # The mirror of the guard in complete_native_admission, so
                # the two completions exclude each other by construction
                # rather than by whoever calls them being careful. Provider
                # and receipt kind alone cannot tell them apart: a native
                # kimi row and an ACP kimi row agree on both, and a
                # well-formed ACP receipt naming the bound session would
                # otherwise mark a native generation admitted on a turn
                # that happened over a socket it never opened.
                raise ManagedLaunchConflict(
                    f"an immutable {em.NATIVE_TUI!r} row completes on its own control "
                    f"operation record, never on a provider submission receipt; a receipt "
                    f"is evidence about a different transport, not weaker evidence about "
                    f"this one"
                )
            expected_kind = {
                "codex": "codex-turn-start",
                "kimi_cli": "kimi-session-update",
            }.get(row.provider)
            if (
                expected_kind is None
                or provider_receipt.get("provider_receipt_kind") != expected_kind
            ):
                raise ManagedLaunchConflict("submission receipt kind is not provider-native")
            for field in ("receipt_id", "provider_session_id", "provider_turn_id"):
                if not isinstance(provider_receipt.get(field), str) or not provider_receipt[field]:
                    raise ManagedLaunchConflict(f"submission receipt lacks {field}")
            binding = _parse_json(row.binding_json, {})
            if provider_receipt["provider_session_id"] != binding.get("native_session_id"):
                raise ManagedLaunchConflict(
                    "submission receipt session does not match the bound native identity"
                )
            admission["provider_submission_receipt"] = provider_receipt
            admission["status"] = "admitted"
            admission["admitted_at"] = _now()
            row.admission_json = _canonical_json(admission)
            row.state = "admitted"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"task admission completion failed: {exc}") from exc


def complete_native_admission(
    reservation_id: str,
    delivery_id: str,
    operation: dict[str, Any],
    expected_payload_sha256: str,
) -> dict[str, Any]:
    """Complete a native admission from the control operation's own record.

    A native TUI emits no submission receipt.  There is no provider
    transcript, no turn id, and no socket that could carry one — the
    human's screen is the transcript.  So admission completes on the
    durable, identity-bound operation record the control adapter wrote,
    and on nothing else.  Reusing the ACP path here would mean either
    inventing a receipt kind or accepting an ACP receipt for a native
    generation, and both are refused: an ACP receipt is not weaker
    evidence about a native run, it is evidence about a different thing.

    What completes is the *admission* — this exact task was delivered
    into this exact bound session, exactly once.  That is a different
    claim from "the provider took the turn", which stays open in the
    operation's own ``provider_accepted`` field until something observes
    the pane.  Collapsing the two is the tempting error: a caller that
    read a successful write as an accepted turn would report work
    started that may still be sitting in a composer.
    """
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            # Resolved from the row rather than from the operation record:
            # the operation is the thing being checked, so letting it
            # choose its own validator would let a foreign record nominate
            # the schema it happens to satisfy.
            native_control = _control_adapter(row.provider)
            admission = _parse_json(row.admission_json, None)
            if not admission or admission.get("delivery_id") != delivery_id:
                raise ManagedLaunchConflict("delivery_id does not match the admission claim")
            if admission.get("status") == "admitted":
                if admission.get("native_submission") != operation:
                    raise ManagedLaunchConflict("native submission record changed after admission")
                return _row_dict(row)
            if row.state != "admitting":
                raise ManagedLaunchConflict(f"admission cannot complete from state {row.state!r}")
            mode = em.mode_of_record(_mode_record(row))
            if mode != em.NATIVE_TUI:
                # The mode is immutable, so this can only be reached by a
                # caller that routed an ACP row into the native branch.
                raise ManagedLaunchConflict(
                    f"native admission completion requires an immutable {em.NATIVE_TUI!r} "
                    f"row; this reservation is {mode!r} and completes over its bridge path"
                )
            if operation.get("schema") != native_control.RECORD_SCHEMA:
                raise ManagedLaunchConflict(
                    f"native admission requires a {native_control.RECORD_SCHEMA!r} "
                    f"operation record; got {operation.get('schema')!r}"
                )
            if operation.get("kind") != native_control.KIND_QUEUE:
                # Admission is ordinary first delivery. A steer targets a
                # running turn and a control op is a slash command; either
                # completing an admission would mean the task bytes went
                # somewhere other than the idle-gated queue path.
                raise ManagedLaunchConflict(
                    f"native admission requires a {native_control.KIND_QUEUE!r} "
                    f"operation; got {operation.get('kind')!r}"
                )
            if not operation.get("posted"):
                raise ManagedLaunchConflict(
                    "native admission requires an operation whose payload was posted; "
                    f"operation {operation.get('operation_id')!r} is "
                    f"{operation.get('state')!r}"
                )
            binding = _parse_json(row.binding_json, {}) or {}
            expected_identity = (
                binding.get("native_session_id"),
                row.terminal_id,
                row.generation,
                em.NATIVE_TUI,
            )
            actual_identity = (
                operation.get("native_session_id"),
                operation.get("terminal_id"),
                operation.get("generation"),
                operation.get("execution_mode"),
            )
            if actual_identity != expected_identity:
                raise ManagedLaunchConflict(
                    f"native submission identity {actual_identity} does not match the bound "
                    f"generation {expected_identity}; admission is refused rather than "
                    f"credited to another session"
                )
            if operation.get("payload_sha256") != expected_payload_sha256:
                # Binds the exact bytes. Without this the record could
                # certify that *something* was typed into the right pane
                # while the admitted task was a different message.
                #
                # Compared against a digest the caller computes with the
                # control adapter's own convention rather than against the
                # admission's ``message_sha256``: the adapter hashes the
                # canonical JSON encoding of the payload, and the admission
                # hashes the raw message bytes, so the two disagree on every
                # message. Restating the digest here would only re-encode
                # the mismatch; the caller states which bytes it admitted.
                raise ManagedLaunchConflict(
                    "native submission payload digest does not match the admitted message"
                )
            admission["native_submission"] = operation
            # Named on the admission itself so a reader never has to infer
            # it from the nested record. Admission is delivery; the turn is
            # a separate fact that may still be unobserved.
            admission["provider_accepted"] = bool(operation.get("provider_accepted"))
            admission["status"] = "admitted"
            admission["admitted_at"] = _now()
            row.admission_json = _canonical_json(admission)
            row.state = "admitted"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"native admission completion failed: {exc}") from exc


def mark_admission_refused(
    reservation_id: str,
    delivery_id: str,
    reason: str,
    detail: str,
) -> dict[str, Any]:
    """The state for "provably nothing was sent", which is not ambiguity.

    Kept distinct from :func:`mark_admission_ambiguous` because the two
    license opposite handling.  Ambiguity means the bytes may have landed,
    so nothing may be resent.  A refusal carries the control adapter's own
    proof that the payload was never posted, so the delivery is closed
    with a reason a caller can act on instead of a silence it must treat
    as maybe-delivered forever.
    """
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            admission = _parse_json(row.admission_json, None)
            if not admission or admission.get("delivery_id") != delivery_id:
                raise ManagedLaunchConflict("delivery_id does not match the admission claim")
            if admission.get("status") == "admitted":
                return _row_dict(row)
            admission["status"] = "refused"
            admission["refusal_reason"] = reason
            admission["detail"] = detail
            admission["updated_at"] = _now()
            row.admission_json = _canonical_json(admission)
            # The row is preserved rather than advanced: nothing was
            # delivered, so nothing downstream may treat this generation
            # as carrying a task.
            row.state = "admitting"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"refused admission persistence failed: {exc}") from exc


def refuse_admission_before_io(
    reservation_id: str,
    request: ManagedLaunchV2AdmitRequest,
    reason: str,
    detail: str,
    *,
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Close a delivery that provably never reached the pane, in one write.

    The refusals this serves happen before the admission is claimed and
    before a byte is written, so they used to exist only as an HTTP
    status.  A caller whose response is lost then re-reads the
    reservation, finds it still ``bound`` with no admission, and cannot
    tell "refused, nothing sent" from "the request may still be in
    flight" — and the only safe reading of that silence is
    maybe-delivered.

    Written as a single atomic update rather than a claim followed by
    :func:`mark_admission_refused`, because the intermediate state that
    pair passes through is ``io-attempted`` — the record that says bytes
    may have gone out.  A crash between the two writes would manufacture
    exactly the false ambiguity this exists to remove.

    The same immutable identity a claim would have bound is recorded, so
    a replayed delivery id is still checked against the full identity
    rather than being answered from a thinner record, and the readiness
    observation the refusal rests on is stored beside it.

    A retryable refusal leaves the row ``bound``: the state a claim
    requires is preserved, so the same delivery can complete later
    without a new reservation, a new binding, or a second copy of the
    task.  A permanent one advances to ``admitting`` — still zero bytes,
    but closed.
    """
    actual_digest = hashlib.sha256(request.message.encode("utf-8")).hexdigest()
    if actual_digest != request.message_sha256:
        # A malformed request, not a delivery outcome. It gets the same
        # non-durable refusal a claim would have given it: recording a
        # refusal under an identity the caller misdescribed would bind
        # the delivery id to bytes nobody can reproduce.
        raise ManagedLaunchConflict("message_sha256 does not match message bytes")
    retryable = reason in _RETRYABLE_REFUSAL_REASONS
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if request.delivery_id != _parse_json(row.request_json, {}).get("delivery_id"):
                raise ManagedLaunchConflict(
                    "delivery_id does not match the immutable v2 reservation delivery identity"
                )
            prior_admission_json = row.admission_json
            existing = _parse_json(prior_admission_json, None)
            if existing is not None:
                # Identity before anything else, including before handing
                # the stored record back. A delivery id carrying different
                # bytes is a different delivery, and answering it with the
                # winner's record would report one task's outcome for
                # another's — the same substitution the replay check
                # exists to refuse, reached by the concurrent route.
                _assert_same_admission_identity(existing, request)
                if not _is_retryable_refusal(existing, request.delivery_id):
                    # Admitted, maybe-sent, or a permanent refusal decided
                    # first. Each is authoritative and is returned
                    # unchanged — overwriting one could relabel a delivery
                    # that did write bytes as one that never ran.
                    return _row_dict(row)
            admission = {
                **_admission_identity(request),
                "status": "refused",
                "refusal_reason": reason,
                # Stored rather than re-derived by a reader from the
                # reason string: which reasons may be retried is this
                # module's decision, and a caller re-implementing that
                # table would drift from it silently.
                "retryable": retryable,
                "detail": detail,
                "readiness_observation": observation,
                "refused_at": _now(),
            }
            updated = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchV2ReservationModel.state == "bound",
                    database.ManagedLaunchV2ReservationModel.binding_json.is_not(None),
                    # Compare-and-swap on the exact bytes just read, so a
                    # refusal that changed underneath — including one a
                    # concurrent claim replaced with io-attempted — loses
                    # rather than being clobbered.
                    (
                        database.ManagedLaunchV2ReservationModel.admission_json
                        == prior_admission_json
                        if existing is not None
                        else database.ManagedLaunchV2ReservationModel.admission_json.is_(None)
                    ),
                )
                .update(
                    {
                        "admission_json": _canonical_json(admission),
                        # Retryable refusals hold the row at ``bound``:
                        # that is the state a later claim requires, and
                        # advancing it would make the refusal the thing
                        # that blocks the retry it invites. Permanent ones
                        # advance no further than ``admitting`` — nothing
                        # was delivered, so nothing downstream may treat
                        # this generation as carrying a task.
                        "state": "bound" if retryable else "admitting",
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            row = _query(db, reservation_id)
            if updated == 1:
                return _row_dict(row)
            contender = _parse_json(row.admission_json, None)
            if contender is not None:
                # A concurrent request got there first. Its record is
                # authoritative, but only for the delivery it belongs to —
                # so the identity is checked again against what actually
                # landed, not only against what was read before the swap.
                _assert_same_admission_identity(contender, request)
                return _row_dict(row)
            raise ManagedLaunchConflict(f"task admission requires state 'bound', not {row.state!r}")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"refused admission persistence failed: {exc}") from exc


def _refusal_was_persisted(record: dict[str, Any], delivery_id: str, reason: str) -> bool:
    """Whether the refusal just attempted is the one now stored.

    False means another handler for this same delivery id won the write
    — its record may say the bytes were sent, or may be about to.  The
    refusal is then true only of *this* request, and answering with it
    would tell one caller nothing was sent while the other sends it.
    """
    admission = record.get("admission") or {}
    return (
        admission.get("delivery_id") == delivery_id
        and admission.get("status") == "refused"
        and admission.get("refusal_reason") == reason
    )


def _persist_pre_io_refusal(
    reservation_id: str,
    request: ManagedLaunchV2AdmitRequest,
    reason: str,
    detail: str,
    *,
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Make the refusal durable, or refuse to claim one at all.

    Ordering is the point: the response is the part that can be lost, so
    the record has to exist first for a re-read to be able to answer the
    question the lost response would have.

    Which makes the failure path the whole contract.  A refusal that was
    answered but not stored is worse than no refusal: the caller is told
    "provably nothing was sent" while the only durable state remains a
    bare ``bound`` row, so anyone who re-reads it — including the same
    caller after a lost response — sees the silence that has to be read
    as maybe-delivered.  Two observers of one delivery would disagree,
    which is this seam's own defect reintroduced one layer up.

    So a storage fault is never downgraded into a proven-refusal answer.
    It is reported as what it is, leaving the response and the stored
    state both saying "unresolved, ask again".

    A :class:`ManagedLaunchConflict` is passed through untouched: it means
    the request is not the delivery it claims to be, which is an accurate
    answer about *this* request rather than a claim about the refusal.
    """
    try:
        return refuse_admission_before_io(
            reservation_id, request, reason, detail, observation=observation
        )
    except ManagedLaunchConflict:
        raise
    except ManagedLaunchError as exc:
        logger.warning(
            "v2 reservation %s: pre-I/O refusal %r could not be persisted: %s",
            reservation_id,
            reason,
            exc,
        )
        raise ManagedLaunchUnavailable(
            f"the task was not sent, but that refusal ({reason}) could not be recorded, so "
            f"it is reported as unresolved rather than as a proven refusal a reader of this "
            f"reservation would not find: {exc}"
        ) from exc


def mark_admission_ambiguous(reservation_id: str, delivery_id: str, detail: str) -> dict[str, Any]:
    """The honest at-most-once state: possibly submitted, never replayed."""
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            admission = _parse_json(row.admission_json, None)
            if not admission or admission.get("delivery_id") != delivery_id:
                raise ManagedLaunchConflict("delivery_id does not match the admission claim")
            if admission.get("status") == "admitted":
                return _row_dict(row)
            admission["status"] = "ambiguous_preserved"
            admission["detail"] = detail
            admission["updated_at"] = _now()
            row.admission_json = _canonical_json(admission)
            row.state = "admitting"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"ambiguous admission persistence failed: {exc}") from exc


async def launch_reserved(reservation_id: str, *, registry=None) -> dict[str, Any]:
    """Launch a reserved v2 generation without carrying task bytes.

    Mirrors the v1 zero-keystroke managed spawn; the native bind is a
    separate explicit step so a crash anywhere before bind yields zero
    task bytes by construction.
    """
    import asyncio

    from cli_agent_orchestrator.services import terminal_service
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        BRIDGE_VERSION,
        launch_binding_identity,
        profile_digest,
        read_state,
        request_bridge,
        write_request,
    )

    # Gate the mode before claiming, not after.  ``claim_launch`` moves
    # the reservation to ``launching``, and a refusal raised past that
    # point would strand the row in a state that says a launch is in
    # flight when none is.  Read-then-claim leaves an unlaunchable
    # reservation exactly as it was.
    # ``get`` projects through the mode readers, so this is always a
    # concrete mode — a legacy row reads ACP rather than null.
    reserved_mode = str(get(reservation_id)["execution_mode"])
    if reserved_mode not in LAUNCHABLE_EXECUTION_MODES:
        raise ManagedLaunchConflict(
            f"reservation {reservation_id} is bound to execution_mode {reserved_mode!r}, "
            f"which this surface has no launch branch for; launchable modes are "
            f"{list(LAUNCHABLE_EXECUTION_MODES)}. Refusing rather than starting the ACP "
            "bridge under a reservation that says otherwise"
        )

    record, should_launch = claim_launch(reservation_id)
    if not should_launch:
        return record
    request = record["request"]
    try:
        executable = request["provider_executable"]
        digest = request["provider_executable_sha256"]
        if os.path.realpath(executable) != executable or not os.path.isfile(executable):
            raise ManagedLaunchConflict("provider executable must be a canonical absolute path")
        with open(executable, "rb") as handle:
            if hashlib.sha256(handle.read()).hexdigest() != digest:
                raise ManagedLaunchConflict("provider executable digest drifted from the pin")
        rendezvous_identity = launch_binding_identity(
            project=request.get("project") or record["run_id"],
            task_id=record["task_id"] or record["run_id"],
            terminal_id=record["terminal_id"],
            terminal_generation=record["generation"],
            working_directory=record["working_directory"],
            actor=record["caller_id"],
        )
        bridge_request = {
            "bridge_version": BRIDGE_VERSION,
            "controller_pid": os.getpid(),
            "reservation_id": reservation_id,
            "terminal_id": record["terminal_id"],
            "generation": record["generation"],
            "provider": record["provider"],
            "agent_profile": record["agent_profile"],
            "profile_sha256": profile_digest(record["agent_profile"]),
            "model": request["expected_model"],
            "effort": request["expected_effort"],
            "working_directory": record["working_directory"],
            "provider_executable": executable,
            "provider_executable_sha256": digest,
            # v2 identity fields: the bridge emits fenced heartbeats only
            # when these are present (v1 requests never carry them).  The
            # immutable project identity persists from the reservation; it
            # is never silently dropped (model_dump includes the key with
            # a None value when unset, so a plain .get default would not
            # fall back).
            "project": request.get("project") or record["run_id"],
            "task_id": record["task_id"],
            "delivery_id": request["delivery_id"],
            "run_id": record["run_id"],
            "obligation_generation": record["obligation_generation"],
            "assigned_policy_sha256": profile_digest(record["agent_profile"]),
            "rendezvous_identity": rendezvous_identity,
        }
        write_request(reservation_id, bridge_request)
    except Exception as exc:  # noqa: BLE001 - no provider I/O occurred
        # Preflight in the literal sense: the pinned binary is checked and
        # the durable request is written before anything is started, so a
        # failure here is the same class as the native banner/environment
        # check further down — verification that ran ahead of the launch,
        # not a launch that went wrong.
        return _mark_preflight_blocked(
            reservation_id, str(exc), reason=PREFLIGHT_REASON_NATIVE_PREFLIGHT
        )

    # The two modes diverge here and nowhere earlier: everything above is
    # reservation identity and the durable request, which both modes need
    # in exactly the same shape.  Below this line they share no code, so
    # neither can partially become the other.
    if reserved_mode == em.NATIVE_TUI:
        return await _launch_native_tui(reservation_id, record, bridge_request)

    try:
        await terminal_service.create_terminal(
            provider=record["provider"],
            agent_profile=record["agent_profile"],
            session_name=record["session_name"],
            new_session=False,
            working_directory=record["working_directory"],
            registry=registry,
            caller_id=record["caller_id"],
            defer_init=False,
            initial_message=None,
            reserved_terminal_id=record["terminal_id"],
            terminal_generation=record["generation"],
            trusted_project_root=record["trusted_project_root"],
            expected_model=request["expected_model"],
            expected_effort=request["expected_effort"],
            preserve_on_init_failure=True,
            managed_native_command=[
                os.path.abspath(sys.executable),
                "-I",
                "-m",
                "cli_agent_orchestrator.services.managed_provider_bridge",
                "--reservation-id",
                reservation_id,
            ],
            # v2 terminals persist only to the isolated v2 surface; the
            # shared terminals table (and every old cleanup/list path)
            # never sees them.
            protocol_vintage="v2",
        )
    except Exception as exc:  # noqa: BLE001 - preserve and expose, never cleanup/retry
        try:
            state = read_state(reservation_id)
        except Exception:  # noqa: BLE001 - generic startup evidence remains truthful
            state = None
        if state and state.get("state") == "launch-failed-bridge":
            return _mark_launch_failed_bridge(reservation_id, state)
        # The generic bucket, and deliberately so: this is the ACP bridge
        # branch, where no TUI is launched and no native session is
        # bootstrapped, so every more specific member of the closed set
        # would name something that did not happen.  The detail carries
        # the actual cause; the code says only which class of answer a
        # recovery is looking at.
        return _mark_preflight_blocked(reservation_id, str(exc), reason=PREFLIGHT_REASON_GENERIC)

    try:
        status = await asyncio.to_thread(
            request_bridge, reservation_id, {"op": "status"}, timeout=120.0
        )
    except Exception as exc:  # noqa: BLE001 - query durable state before classifying
        try:
            state = read_state(reservation_id)
        except Exception:  # noqa: BLE001
            state = None
        if state and state.get("state") == "launch-failed-bridge":
            return _mark_launch_failed_bridge(reservation_id, state)
        detail = str((state or {}).get("error") or exc)
        return _mark_preflight_blocked(
            reservation_id,
            f"exact provider readiness was not established: {detail}",
            reason=PREFLIGHT_REASON_READINESS,
        )
    if status.get("state") != "ready" or not isinstance(status.get("readiness"), dict):
        return _mark_preflight_blocked(
            reservation_id,
            "exact provider session did not return a readiness receipt",
            reason=PREFLIGHT_REASON_READINESS,
        )
    return get(reservation_id)


class _V2NativePane:
    """The reserved v2 pane, with the provider's own TUI as its argv.

    Exists so the native launch reuses the ordinary v2 terminal creation
    path — registry journalling, the reserved terminal id, the persisted
    row, the pane-process verification — instead of reaching for tmux
    directly and leaving a running provider that no v2 record describes.

    ``create_pane`` is synchronous because the launch ordering it belongs
    to is; it hands the coroutine back to the loop it was started from.
    That is safe here and only here: the launch runs in a worker thread,
    so the loop is idle while this blocks, and it cannot be the loop's own
    thread waiting on itself.
    """

    def __init__(self, *, record: dict[str, Any], request: dict[str, Any], loop, registry) -> None:
        self._record = record
        self._request = request
        self._loop = loop
        self._registry = registry
        self._window = managed_window_name(record["terminal_id"], record["generation"])

    def create_pane(self, *, argv: Any) -> str:
        import asyncio

        return asyncio.run_coroutine_threadsafe(self._create(list(argv)), self._loop).result()

    async def _create(self, argv: list[str]) -> str:
        from cli_agent_orchestrator.services import terminal_service

        await terminal_service.create_terminal(
            provider=self._record["provider"],
            agent_profile=self._record["agent_profile"],
            session_name=self._record["session_name"],
            new_session=False,
            working_directory=self._record["working_directory"],
            registry=self._registry,
            caller_id=self._record["caller_id"],
            defer_init=False,
            initial_message=None,
            reserved_terminal_id=self._record["terminal_id"],
            terminal_generation=self._record["generation"],
            trusted_project_root=self._record["trusted_project_root"],
            expected_model=self._request["expected_model"],
            expected_effort=self._request["expected_effort"],
            preserve_on_init_failure=True,
            # The TUI is the pane's OWN argv. Nothing is typed into a
            # shell, so there is no window in which a partially-typed
            # command line could be interrupted into something else.
            managed_native_command=argv,
            protocol_vintage="v2",
            # The pane runs the provider's own full-screen TUI, so its
            # status comes from the native observer and the FIFO monitor
            # is never scheduled for it.
            native_status_source=True,
        )
        return self._window

    def observe(self) -> Any:
        from cli_agent_orchestrator.services import native_tui_launch, terminal_service

        return native_tui_launch.TmuxNativePane(
            terminal_service.get_backend(),
            session_name=self._record["session_name"],
            window_name=self._window,
            terminal_id=self._record["terminal_id"],
        ).observe()


async def _launch_native_tui(
    reservation_id: str,
    record: dict[str, Any],
    bridge_request: dict[str, Any],
    *,
    registry=None,
) -> dict[str, Any]:
    """Launch one reserved generation as a provider-native TUI.

    The ordering is the point.  A provider session is minted first, by a
    conversation that sends no turn and is proven dead before anything
    else happens; only then is exclusive ownership of that session
    claimed; only then does a pane start.  Every failure below leaves the
    reservation blocked with zero task bytes admitted — the bootstrap
    submits none by construction, and the TUI is never typed into.

    ``preflight_blocked`` here means "no task bytes crossed", which is
    what a caller needs to know, not "no process ran" — a failed native
    launch may well have started and ended a provider process.
    """
    import asyncio

    from cli_agent_orchestrator.services import (
        claude_native_launch,
        claude_native_readiness,
        kimi_native_bootstrap,
    )
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        BRIDGE_VERSION,
        native_child_environment,
        provider_version_banner,
        publish_native_ready_state,
    )

    provider = record["provider"]
    if provider not in NATIVE_TUI_PROVIDERS:
        # Refused rather than quietly launched some other way: the whole
        # value of a closed mode is that an unsupported combination stops
        # instead of finding a path that "works".
        return _mark_preflight_blocked(
            reservation_id,
            f"provider {provider!r} has no native TUI launch branch; native providers are "
            f"{sorted(NATIVE_TUI_PROVIDERS)}",
            reason=PREFLIGHT_REASON_PROVIDER_UNSUPPORTED,
        )

    request = record["request"]
    executable = bridge_request["provider_executable"]
    digest = bridge_request["provider_executable_sha256"]

    try:
        version_output = await asyncio.to_thread(provider_version_banner, bridge_request)
        environment = native_child_environment(bridge_request)
    except Exception as exc:  # noqa: BLE001 - nothing was started
        return _mark_preflight_blocked(
            reservation_id,
            f"native preflight failed: {exc}",
            reason=PREFLIGHT_REASON_NATIVE_PREFLIGHT,
        )

    # How a session acquires its identity is the one place the two native
    # providers genuinely differ, and the difference decides the launch
    # form. Kimi's id is *discovered*: a separate ACP conversation mints
    # it, sends no turn, and is proven dead before anything else happens,
    # after which the TUI resumes that id. Claude's id is *chosen*: a
    # canonical uuid minted here, before any provider I/O at all, and
    # handed to the TUI as --session-id. The consequence is that a Claude
    # launch which dies before its first turn still has a recorded
    # identity, because the identity preceded the launch.
    readiness_hook: Optional[dict[str, Any]] = None
    launch_extra_args: Optional[list[str]] = None
    try:
        if provider == "claude_code":
            bootstrap, readiness_hook = await asyncio.to_thread(
                _mint_claude_native_session,
                record=record,
                request=request,
                version_output=version_output,
                digest=digest,
            )
            launch_kind = native_tui_launch.LAUNCH_KIND_NEW
            # The model is pinned on the launch argv itself. There is no
            # later moment that could apply it: by the time anything could
            # send a slash command the session is already running, and the
            # first turn would have gone to whatever route the provider
            # chose. The value was validated before the identity was
            # minted, so this cannot introduce an unpinnable one.
            launch_extra_args = [
                "--model",
                bootstrap["requested_model"],
                "--settings",
                claude_native_readiness.settings_argument(readiness_hook["settings"]),
            ]
        else:
            bootstrap = await asyncio.to_thread(
                _mint_native_session,
                kimi_native_bootstrap,
                executable=executable,
                digest=digest,
                version_output=version_output,
                environment=environment,
                record=record,
                request=request,
            )
            launch_kind = native_tui_launch.LAUNCH_KIND_RESUME
    except Exception as exc:  # noqa: BLE001 - no turn was ever submitted
        return _mark_preflight_blocked(
            reservation_id,
            f"native session bootstrap failed: {exc}",
            reason=PREFLIGHT_REASON_SESSION_BOOTSTRAP,
        )

    try:
        intent = (
            _claude_bootstrap_intent(bootstrap, reservation_id=reservation_id)
            if provider == "claude_code"
            else kimi_native_bootstrap.bootstrap_intent(
                bootstrap, note=f"v2 native launch of reservation {reservation_id}"
            )
        )
        transport = _V2NativePane(
            record=record,
            request=request,
            loop=asyncio.get_running_loop(),
            registry=registry,
        )
        outcome = await asyncio.to_thread(
            native_tui_launch.start,
            provider=provider,
            native_session_id=bootstrap["native_session_id"],
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            execution_mode=em.NATIVE_TUI,
            intent=intent,
            binary=executable,
            binary_sha256=digest,
            working_directory=record["working_directory"],
            transport=transport,
            extra_args=launch_extra_args,
            launch_kind=launch_kind,
        )
    except Exception as exc:  # noqa: BLE001 - the attachment store holds the detail
        return _mark_preflight_blocked(
            reservation_id,
            f"native TUI launch refused: {exc}",
            reason=PREFLIGHT_REASON_TUI_LAUNCH_REFUSED,
        )

    # Waited for here, at the only place that knows the pane just started,
    # and deliberately not at admission: a caller admits in a straight
    # line with a request deadline of its own, so a wait moved there would
    # have to fit inside a budget it cannot see, and a caller that gives
    # up first cannot receive the truthful answer at all. The launch is
    # already waiting on a process, so the wait costs nothing it was not
    # spending. A pane that never becomes ready is not blocked here: the
    # receipt says so, and the existing bind gate refuses on it.
    #
    # Read from the attachment's ``owner`` rather than from the launch
    # handle: the handle is whatever the transport returned to name what
    # it created, while the owner's ``pane_id`` is the exact pane the
    # ownership store recorded — and is the same field admission later
    # validates and delivers into. Watching anything else would let the
    # receipt certify a pane that is not the one a task goes to.
    # For Claude, the provider's own SessionStart hook is the
    # authoritative readiness and it is awaited *first*. Nothing weaker
    # may stand in for it: a live pane proves a process was spawned, an
    # elapsed interval proves nothing at all, and a rendered composer
    # belongs to whatever session Claude actually opened — including the
    # wrong one, which is exactly what a resume falling back to its
    # interactive picker looks like from outside. Only the hook names the
    # session id.
    session_start: Optional[dict[str, Any]] = None
    if provider == "claude_code" and readiness_hook is not None:
        try:
            session_start = await asyncio.to_thread(
                claude_native_readiness.await_session_start,
                readiness_hook["readiness_path"],
                bootstrap["native_session_id"],
            )
        except claude_native_readiness.ClaudeNativeReadinessError as exc:
            # Zero task bytes were submitted: the launch never got as far
            # as being bindable, and admission requires a bind.
            return _mark_preflight_blocked(
                reservation_id,
                f"claude native readiness was never proven: {exc}",
                reason=PREFLIGHT_REASON_READINESS,
            )
        # The model analogue of the session-id check, on the same
        # evidence and at the same moment. The hook the provider itself
        # wrote names the model that is actually answering; comparing it
        # to the pinned request is the only thing that can catch a launch
        # that started on a different route, and it must happen before
        # anything is admitted. Zero task bytes have crossed here, so a
        # refusal costs a finalizable reservation rather than work spent
        # on the wrong capability and quota pool.
        observed_model = (session_start or {}).get("model")
        if not claude_native_launch.observed_model_matches(
            bootstrap["requested_model"], observed_model
        ):
            return _mark_preflight_blocked(
                reservation_id,
                (
                    "the running Claude session is not on the requested route: requested "
                    f"{bootstrap['requested_model']!r}, the provider's own session-start "
                    f"proof observed {observed_model!r} "
                    f"({claude_native_launch.observed_model_mismatch_detail(bootstrap['requested_model'], observed_model)}). "
                    "Refusing rather than admitting a "
                    "task to a model nobody asked for"
                ),
                reason=PREFLIGHT_REASON_READINESS,
            )
        # Preserve the provider-authored route exactly.  In particular,
        # ``[1m]`` is part of an explicitly pinned Claude route even though
        # it is not part of the base model name.  Bind validates this receipt
        # a second time, so stripping the suffix here would make a route that
        # passed preflight fail permanently at bind.
        bootstrap["observed_model"] = str(observed_model).strip()

    # The post-proof boundary, and the only correct one. Before this point
    # the session id is what the launcher *intended* to run — a chosen uuid
    # for Claude, a minted one for Kimi — and persisting it there would
    # record an assertion. By here it is proven: Claude's own SessionStart
    # hook has named this exact uuid, and Kimi's bootstrap minted it and
    # proved the minting process dead before the TUI could attach.
    #
    # Fail closed. A pane whose row does not say which provider session it
    # is running defeats the native-session half of the supersession test:
    # a pane that later holds a *different* session looks identical to the
    # right one, because the comparison has nothing to compare. Zero task
    # bytes have been submitted at this point, so refusing costs a
    # finalizable reservation rather than a lost turn.
    try:
        persisted = await asyncio.to_thread(
            database.set_terminal_v2_native_session_id,
            record["terminal_id"],
            bootstrap["native_session_id"],
        )
    except Exception as exc:  # noqa: BLE001 - treated as a failure to persist
        persisted = False
        logger.warning(
            "Could not persist the proven native session for %s: %s",
            record["terminal_id"],
            exc,
        )
    if not persisted:
        return _mark_preflight_blocked(
            reservation_id,
            (
                "the proven native session id could not be durably recorded against "
                f"terminal {record['terminal_id']}; refusing rather than publishing a "
                "readiness receipt for a pane whose row cannot say which session it runs"
            ),
            reason=PREFLIGHT_REASON_READINESS,
        )

    readiness = await asyncio.to_thread(
        _await_native_pane_input_ready,
        record,
        ((outcome.get("attachment") or {}).get("owner") or {}).get("pane_id"),
    )
    try:
        await asyncio.to_thread(
            publish_native_ready_state,
            reservation_id,
            _native_readiness_receipt(
                record=record,
                request=request,
                bootstrap=bootstrap,
                outcome=outcome,
                version_output=version_output,
                bridge_version=BRIDGE_VERSION,
                readiness=readiness,
                session_start=session_start,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a pane exists but bind cannot read it
        return _mark_preflight_blocked(
            reservation_id,
            f"native readiness receipt could not be published: {exc}",
            reason=PREFLIGHT_REASON_READINESS,
        )
    return get(reservation_id)


CLAUDE_BOOTSTRAP_SCHEMA = "cao-claude-native-bootstrap-v1"
CLAUDE_BOOTSTRAP_INTENT_SCHEMA = "cao-claude-native-bootstrap-intent-v1"


def _mint_claude_native_session(
    *,
    record: dict[str, Any],
    request: dict[str, Any],
    version_output: str,
    digest: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Choose a Claude session identity, and arrange for it to prove itself.

    Spends no provider I/O whatsoever. The identity is a canonical uuid
    minted here, and the second half of the work is preparing the
    generation-private file the provider's SessionStart hook will write
    to — both done *before* the launch, so the hook has somewhere to
    write the instant Claude starts and so the identity is recorded even
    if the launch never succeeds.

    The version is checked before the id is minted rather than after. A
    drifted build is a refusal, and refusing after minting would leave a
    recorded identity for a session that was never going to be started.
    """
    from cli_agent_orchestrator.services import claude_native_launch, claude_native_readiness

    check_pinned_version(_PINNED_PROVIDER["claude_code"], version_output)

    # Pinned before the id is minted, for the same reason the version is:
    # an unpinnable model is a refusal, and refusing after minting would
    # leave a recorded identity for a session nobody was going to start.
    # There is no default this side may choose — a launch without an
    # explicit model runs on whatever the provider prefers, which is how a
    # route requested as sonnet came up as a 1M Opus session.
    try:
        pinned_model = claude_native_launch.validate_requested_model(request["expected_model"])
    except claude_native_launch.ClaudeNativeLaunchError as exc:
        raise ManagedLaunchConflict(str(exc)) from exc

    native_session_id = claude_native_launch.mint_session_id()
    hook = claude_native_readiness.prepare(
        COMPANION_DIR, record["terminal_id"], record["generation"]
    )
    bootstrap = {
        "schema": CLAUDE_BOOTSTRAP_SCHEMA,
        "provider": "claude_code",
        "native_session_id": native_session_id,
        # Named so a receipt reader knows the identity was *assigned*,
        # not read back from the provider. The distinction matters when
        # reconciling: a chosen id exists whether or not the provider
        # ever ran, and an absent SessionStart is then a fact about the
        # provider rather than about the id.
        "id_source": _ISSUANCE_SOURCES["claude_code"],
        "provider_version": normalized_version(version_output),
        "binary_sha256": digest,
        "working_directory": record["working_directory"],
        # The value the launch will pin, named as *requested* rather than
        # as the route. Nothing has observed a model at this point — the
        # provider has not been started — so a key called ``model`` here
        # would be a claim about a session that does not exist. The
        # observation arrives from the SessionStart proof and is checked
        # against this before any task byte.
        "requested_model": pinned_model,
        "observed_model": None,
        # An effort is requested and cannot be seen. Claude exposes no
        # pre-turn effort surface — the same fact the route attestor
        # already records when it declines to name one — so the request is
        # kept, the observation is explicitly null, and the pair's
        # declared observability says which of those two facts this is.
        # Adopting the provider-default sentinel instead would be a
        # different and false claim: that the model has no effort surface
        # at all, which would silently discard a real requested effort.
        "requested_effort": request["expected_effort"],
        "observed_effort": None,
        "effort_observability": provider_contracts.effort_observability(
            "claude_code", pinned_model
        ),
        "effort_unobserved_reason": (
            "Claude exposes no pre-turn effort surface: the value is settled by the "
            "first turn, and this launch sends none. The requested effort is recorded "
            "as requested; no effort was observed and none is claimed"
        ),
        "readiness_path": str(hook["readiness_path"]),
        "task_bytes_submitted": False,
        "minted_at": _now(),
    }
    return bootstrap, hook


def _claude_bootstrap_intent(bootstrap: dict[str, Any], *, reservation_id: str) -> dict[str, Any]:
    """The launch intent recorded against a chosen Claude identity.

    Built by ``native_attachment.acquire_intent`` rather than assembled
    here, because the attachment store accepts only an intent it built
    itself: the intent is the set of obligations that module validates,
    and a hand-written dict carrying its own schema is a caller asserting
    those obligations instead of having them checked.  The Claude-specific
    facts ride in ``acquisition_receipt``, which is exactly the field the
    Kimi path uses for its bootstrap receipt.
    """
    return native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
        acquisition_receipt={
            "schema": CLAUDE_BOOTSTRAP_INTENT_SCHEMA,
            "provider": "claude_code",
            "native_session_id": bootstrap["native_session_id"],
            "id_source": bootstrap["id_source"],
            "provider_version": bootstrap["provider_version"],
            "working_directory": bootstrap["working_directory"],
            "task_bytes_submitted": False,
        },
        # A chosen id names a session that did not exist until this launch,
        # so there is nothing prior for it to re-admit or replay. Asserted
        # explicitly anyway: these are obligations the store checks, not
        # descriptions it records.
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        note=f"v2 native launch of reservation {reservation_id}",
    )


def _mint_native_session(
    bootstrap_module: Any,
    *,
    executable: str,
    digest: str,
    version_output: str,
    environment: dict[str, str],
    record: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    """Mint the provider session and prove the minting process exited."""
    transport = bootstrap_module.StdioAcpBootstrap(
        kimi_binary=executable,
        env=environment,
        working_directory=record["working_directory"],
    )
    receipt: dict[str, Any] = bootstrap_module.mint_session(
        kimi_binary=executable,
        binary_sha256=digest,
        version_output=version_output,
        working_directory=record["working_directory"],
        model=request["expected_model"],
        effort=request["expected_effort"],
        transport=transport,
    )
    return receipt


def _await_native_pane_input_ready(
    record: dict[str, Any], pane_handle: Optional[str]
) -> dict[str, Any]:
    """Watch the launched pane until it can be typed into, or give up.

    A pane that has started is not a pane that accepts input.  The
    provider paints a screen and connects its servers first, and a task
    delivered into that window is absorbed with no error anywhere — the
    launch looks perfect and the task simply never happened.  Waiting
    here is what lets the readiness receipt state something that was
    observed instead of something that was assumed.

    Elapsed time is a bound on how long to keep looking, never evidence
    of readiness: the only thing that ends this loop successfully is the
    provider's own detector reading its own screen, the same authority a
    refusal at admission cites.  A pane that cannot be read at all is
    reported as unread rather than as not-ready, because "we could not
    look" and "we looked and it was busy" are different facts and only
    the second describes the pane.

    Returns the observation rather than raising, because a pane that
    never became ready is not a launch failure to hide — it is a fact
    the receipt must carry, so the bind gate can refuse on it.
    """
    from cli_agent_orchestrator.models.terminal import TerminalStatus

    if not pane_handle:
        return _readiness_observation(
            pane_id=None,
            provider_status=None,
            input_ready=False,
            detail="the launch outcome names no pane, so readiness could not be observed",
        )
    window_name = managed_window_name(record["terminal_id"], record["generation"])
    authority = TURN_OBSERVER_AUTHORITY.get(record["provider"], "observe_native_turn_state")
    deadline = time.monotonic() + NATIVE_PANE_READY_TIMEOUT_SECONDS
    while True:
        try:
            status = _observe_turn_state(
                record["provider"],
                pane_id=pane_handle,
                terminal_id=record["terminal_id"],
                session_name=record["session_name"],
                window_name=window_name,
            )
        except Exception as exc:  # noqa: BLE001 - an unread pane, not a failed launch
            observation = _readiness_observation(
                pane_id=pane_handle,
                provider_status=None,
                input_ready=False,
                detail=f"the pane could not be read: {exc}",
                authority=authority,
            )
        else:
            observation = _readiness_observation(
                pane_id=pane_handle,
                provider_status=status.value,
                input_ready=status is TerminalStatus.IDLE,
                detail=None,
                authority=authority,
            )
            if observation["input_ready"]:
                return observation
        if time.monotonic() >= deadline:
            return observation
        time.sleep(_NATIVE_PANE_READY_POLL_SECONDS)


def _native_readiness_receipt(
    *,
    record: dict[str, Any],
    request: dict[str, Any],
    bootstrap: dict[str, Any],
    outcome: dict[str, Any],
    version_output: str,
    bridge_version: str,
    readiness: dict[str, Any],
    session_start: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The readiness receipt a native generation offers to ``bind_native``.

    Carries no provider transcript digest, because there is no provider
    transcript: the bootstrap sent nothing and the TUI is a terminal, not
    a protocol.  What stands in its place is stronger for this mode — the
    exact argv digest that started the pane, and the observed process
    identity of the pane running it.  Both are checkable against the
    durable attachment record, which a self-reported transcript is not.

    The route is the bootstrap's read-back value rather than the
    reservation's request, so a session that silently settled elsewhere
    fails the exact-route check instead of being certified by it.
    """
    return {
        "bridge_version": bridge_version,
        "reservation_id": record["reservation_id"],
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        # Observed, or explicitly absent — never the request wearing an
        # observation's name. The Kimi bootstrap reads its route back off
        # the session it configured, so ``model``/``effort`` there are
        # genuine readings. The Claude bootstrap can read a model (the
        # provider's session-start proof names it) and cannot read an
        # effort at all, so it supplies the first and ``None`` for the
        # second. A receipt that filled either from the reservation would
        # certify a route by comparing a claim with itself.
        "model": bootstrap.get("observed_model", bootstrap.get("model")),
        "effort": bootstrap.get("observed_effort", bootstrap.get("effort")),
        # The declared observability of this pair, carried so a reader can
        # tell "no effort was requested" from "an effort was requested and
        # cannot be seen yet" — different facts that a bare null would
        # flatten into one.
        "effort_observability": bootstrap.get(
            "effort_observability",
            provider_contracts.effort_observability(
                record["provider"], bootstrap.get("model") or request["expected_model"]
            ),
        ),
        "requested_effort": bootstrap.get("requested_effort", request["expected_effort"]),
        "effort_unobserved_reason": bootstrap.get("effort_unobserved_reason"),
        "working_directory": record["working_directory"],
        "receipt_id": bootstrap["native_session_id"],
        "provider_session_id": bootstrap["native_session_id"],
        "provider_version": version_output,
        "provider_receipt_kind": _NATIVE_TUI_READINESS_RECEIPT_KINDS[record["provider"]],
        # Observed, never asserted. This field is what bind takes as
        # permission to admit a task, and it used to be a constant — so a
        # generation whose pane was still booting was certified ready by a
        # receipt that had never looked at it. Carrying the observation
        # alongside it means a reader can check the claim rather than
        # trust it, and a refusal at admission can cite the same evidence.
        "model_input_ready": bool(readiness.get("input_ready")),
        "model_input_ready_observation": readiness,
        # The provider's own statement that this exact session started,
        # for providers whose readiness has one. Present for Claude, where
        # the SessionStart hook names the session id; ``None`` for Kimi,
        # whose readiness is the attached pane running the resumed session
        # and which has no such hook. Kept as its own field rather than
        # folded into the pane observation, because they answer different
        # questions: the pane observation says the composer can be typed
        # into, and this says the provider adopted the identity it was
        # given rather than some other one.
        "provider_session_start": session_start,
        "provider_session_start_proven": session_start is not None,
        "execution_mode": em.NATIVE_TUI,
        "native_launch_outcome": outcome["outcome"],
        "launch_argv_sha256": outcome["launch_argv_sha256"],
        "pane_handle": outcome.get("pane_handle"),
        # Read from the attachment's ``owner`` rather than restated from
        # the observation: the identity that matters is the one the
        # exclusive-ownership store actually recorded, because that is
        # what a later no-survivor proof will have to name.
        "process_identity": ((outcome.get("attachment") or {}).get("owner") or {}).get(
            "process_identity"
        ),
        "acquisition_receipt_sha256": hashlib.sha256(
            _canonical_json(bootstrap).encode("utf-8")
        ).hexdigest(),
        # Echoed from the reservation's own request, not restated: an
        # unexpected drift must fail the exact-route check rather than be
        # papered over here.
        "expected_model": request["expected_model"],
        "expected_effort": request["expected_effort"],
    }


def resolve_native_identity_of_record(record: dict[str, Any]) -> dict[str, Any]:
    """Who durably holds this generation's provider session, or a refusal.

    The durable half of the native identity question, split out so the
    two callers that must answer it — native task admission and the
    identity-bound control-input path — ask one implementation rather
    than two that can disagree.  A second implementation of this question
    would not fail loudly; it would agree on every healthy generation and
    diverge on exactly the unhealthy one that mattered.

    Every check here reads a durable store, so it is free of provider I/O
    and safe on a read-only projection.
    :func:`validate_native_identity_against_live_pane` adds the live-pane
    comparison on top for callers that are about to write.

    Three properties are the point of this function rather than
    incidental to it:

    - **The attachment store is the authority, not the reservation row.**
      The row records what was *bound*; the store records who holds the
      session *now*.  A control arrives arbitrarily later than the bind,
      so a generation that has since been replaced must not be able to
      reach the pane that replaced it.
    - **An unresolved earlier ambiguity blocks.** An operation that may
      or may not have landed makes the transcript order unreconstructable
      if anything further is sent, so it is reconciled by exact id first.
    - **Absence is refusal, never a weaker identity.** No attachment, not
      attached, the wrong owner, no pane id, or a process identity
      missing either half is each its own refusal: a task must never be
      typed at an unidentified process, and a partial identity is one
      some later check would pass against.

    Returns:
        The proven identity: provider, native session id, pane id, tmux
        session and window names, and the recorded ``process_identity``
        of the process holding the session (pid *with* its start marker,
        never a bare pid — pids recycle, so a bare one can forge either a
        survivor or a no-survivor).

    Raises:
        ManagedLaunchConflict: The identity is absent, partial, or held
            by someone else.  Nothing was read from the pane.
    """
    provider = record["provider"]
    if provider not in NATIVE_TUI_PROVIDERS:
        raise ManagedLaunchConflict(
            f"native admission is not supported for provider {provider!r}; "
            f"native providers are {sorted(NATIVE_TUI_PROVIDERS)}"
        )
    binding = record.get("binding")
    if not isinstance(binding, dict) or not binding:
        raise ManagedLaunchConflict(
            "native admission requires the journaled native binding; without it there is "
            "no proven session to deliver into and zero task bytes are sent"
        )
    if binding.get("execution_mode") != em.NATIVE_TUI:
        raise ManagedLaunchConflict(
            f"the journaled binding carries execution_mode "
            f"{binding.get('execution_mode')!r}, not {em.NATIVE_TUI!r}"
        )
    native_session_id = binding.get("native_session_id")
    if not isinstance(native_session_id, str) or not native_session_id:
        raise ManagedLaunchConflict("the journaled binding carries no native session id")

    blocking = _control_adapter(provider).unresolved_ambiguity(native_session_id)
    if blocking is not None:
        # Checked here rather than left to the adapter so the refusal
        # happens before the admission is claimed. An earlier operation
        # that may or may not have landed makes the transcript order
        # unreconstructable if anything further is sent.
        raise ManagedLaunchConflict(
            f"control operation {blocking['operation_id']!r} on session "
            f"{native_session_id} is ambiguous and must be reconciled by exact id "
            f"before any further input; zero task bytes were sent"
        )

    attachment = native_attachment.get(provider, native_session_id)
    if attachment is None:
        raise ManagedLaunchConflict(
            f"no attachment record for {provider} session {native_session_id}; "
            f"native admission requires a live owned attachment"
        )
    if attachment["state"] != native_attachment.ATTACHED:
        raise ManagedLaunchConflict(
            f"{provider} session {native_session_id} is {attachment['state']!r}, not "
            f"{native_attachment.ATTACHED!r}; only an attached session accepts a task"
        )
    owner = attachment["owner"]
    expected_owner = (record["terminal_id"], record["generation"], em.NATIVE_TUI)
    actual_owner = (owner["terminal_id"], owner["generation"], owner["execution_mode"])
    if actual_owner != expected_owner:
        raise ManagedLaunchConflict(
            f"{provider} session {native_session_id} is held by {actual_owner}, not "
            f"{expected_owner}; the task is refused rather than delivered to another "
            f"owner's pane"
        )

    pane_id = owner.get("pane_id")
    if not isinstance(pane_id, str) or not pane_id:
        raise ManagedLaunchConflict(
            f"the attachment for session {native_session_id} records no pane id, so the "
            f"exact pane to deliver into cannot be named"
        )
    recorded_process = owner.get("process_identity") or {}
    recorded_pid = recorded_process.get("pid")
    recorded_marker = recorded_process.get("start_marker")
    if not isinstance(recorded_pid, int) or not isinstance(recorded_marker, str):
        raise ManagedLaunchConflict(
            f"the attachment for session {native_session_id} records no usable process "
            f"identity; a task must never be typed at an unidentified process"
        )

    return {
        "provider": provider,
        "native_session_id": native_session_id,
        "pane_id": pane_id,
        "session_name": record["session_name"],
        "window_name": managed_window_name(record["terminal_id"], record["generation"]),
        "process_identity": {"pid": recorded_pid, "start_marker": recorded_marker},
    }


def validate_native_identity_against_live_pane(record: dict[str, Any]) -> dict[str, Any]:
    """Prove the bound native identity is still exactly what was bound.

    Runs before anything is claimed and before a single byte is written,
    so every refusal below leaves zero task admission and zero provider
    I/O with the reservation row untouched.  That ordering is the
    contract: a caller that receives a refusal here knows the task was
    not delivered, rather than having to treat it as maybe-delivered.

    The durable half is :func:`resolve_native_identity_of_record`.  What
    this adds is the live comparison: the pane in front of us right now,
    against what the store recorded at attach — pane id, pid, and process
    start marker together.

    Public because native admission and the identity-bound control-input
    path both have to ask it immediately before writing.  A control
    arrives arbitrarily later than the bind that authorised it, so the
    projection it was resolved from is a statement about the past; only
    re-proving here closes the gap between "checked" and "wrote".
    """
    from cli_agent_orchestrator.services import native_tui_launch, terminal_service

    identity = resolve_native_identity_of_record(record)
    native_session_id = identity["native_session_id"]
    pane_id = identity["pane_id"]
    recorded_pid = identity["process_identity"]["pid"]
    recorded_marker = identity["process_identity"]["start_marker"]
    session_name = identity["session_name"]
    window_name = identity["window_name"]
    pane = native_tui_launch.TmuxNativePane(
        terminal_service.get_backend(),
        session_name=session_name,
        window_name=window_name,
        terminal_id=record["terminal_id"],
    )
    # An observation that could not be made raises out of here as
    # unavailable (503) rather than becoming a refusal (409). "The pane
    # is gone" and "we could not look" license opposite handling, and
    # reporting the second as the first would close a delivery that is
    # still open.
    try:
        observed = pane.observe()
    except native_tui_launch.NativeLaunchError as exc:
        raise ManagedLaunchUnavailable(
            f"the bound native pane could not be observed, so the task was not sent: {exc}"
        ) from exc
    if observed is None:
        raise ManagedLaunchConflict(
            f"the bound native pane for session {native_session_id} no longer exists; "
            f"the task is refused rather than typed into whatever replaced it"
        )
    observed_identity = (
        str(observed["pane_id"]),
        observed["pid"],
        observed["start_marker"],
    )
    recorded_identity = (pane_id, recorded_pid, recorded_marker)
    if observed_identity != recorded_identity:
        raise ManagedLaunchConflict(
            f"the live pane identity {observed_identity} does not match the attached "
            f"identity {recorded_identity}; the process holding this session was "
            f"replaced and the task is refused with zero bytes sent"
        )

    return identity


#: The pre-public spelling, kept so existing call sites keep naming the
#: same one validator rather than a second copy of it.
_validate_native_admission_identity = validate_native_identity_against_live_pane


def _settle_native_admission(
    reservation_id: str,
    delivery_id: str,
    operation: dict[str, Any],
    expected_payload_sha256: str,
    *,
    provider: str,
) -> dict[str, Any]:
    """Map one control-operation outcome onto the admission record.

    Reads the adapter's own transport-vs-provider split rather than
    re-deciding it: ``posted`` is the only field that says bytes were
    written, and it is what admission turns on.  Anything that is not
    posted and not a typed refusal is treated as ambiguous, so an
    unrecognised state can never be read as a delivery.
    """
    native_control = _control_adapter(provider)

    if operation.get("posted"):
        return complete_native_admission(
            reservation_id, delivery_id, operation, expected_payload_sha256
        )
    state = operation.get("state")
    if state == native_control.REFUSED:
        return mark_admission_refused(
            reservation_id,
            delivery_id,
            operation.get("refusal_reason") or "unspecified control refusal",
            f"control operation {operation.get('operation_id')!r} was refused before any "
            f"input was written; no task bytes reached the session",
        )
    return mark_admission_ambiguous(
        reservation_id,
        delivery_id,
        operation.get("ambiguity_reason")
        or (
            f"control operation {operation.get('operation_id')!r} is {state!r}; whether "
            f"the task bytes landed is unknown and must be reconciled by exact id"
        ),
    )


def _reconcile_native_admission(
    reservation_id: str,
    record: dict[str, Any],
    request: ManagedLaunchV2AdmitRequest,
    expected_payload_sha256: str,
    *,
    may_refuse_absent_operation: bool = True,
) -> dict[str, Any]:
    """Answer a replayed native admission from stored state, sending nothing.

    v2 has no reconcile endpoint; a lost admission response is recovered
    by replaying the same delivery id, which lands here.  The delivery id
    is also the control operation id, so the exact operation is
    addressable — no scan, no recency guess, no "the last thing we typed".

    ``may_refuse_absent_operation`` distinguishes the two ways of getting
    here.  A replay arriving fresh from the wire against a stored claim
    means the handler that made it is gone, so a missing operation record
    is proof it crashed before opening one.  Losing a claim race means the
    handler that won it is alive in this process right now, and the same
    missing record only means "not opened yet" — reading it as proof would
    publish a zero-byte verdict about bytes that are still being written.
    """
    native_control = _control_adapter(record["provider"])

    admission = record.get("admission") or {}
    if admission.get("status") == "admitted":
        return record
    if admission.get("status") == "refused":
        # Already answered, with evidence. A refusal names why nothing was
        # sent; re-deriving it from the control store would only overwrite
        # that precise reason with the vaguer one this function infers
        # from an absent operation — turning "the bound identity was
        # wrong" into "no operation was opened", which is true but tells a
        # reader nothing about what to fix.
        return record
    operation = native_control.get(request.delivery_id)
    if operation is None:
        if not may_refuse_absent_operation:
            # A live sibling owns this delivery. Its record is returned as
            # it stands — not-yet-admitted reads as unresolved, which is
            # the honest answer while another handler may still be typing.
            return record
        # The adapter journals its intent before any I/O, so an absent
        # operation record means the crash landed between claiming the
        # admission and opening the operation — provably before anything
        # was typed.
        return mark_admission_refused(
            reservation_id,
            request.delivery_id,
            "no_control_operation",
            "the admission was claimed but no control operation was ever opened, so the "
            "task was never written to the pane",
        )
    return _settle_native_admission(
        reservation_id,
        request.delivery_id,
        operation,
        expected_payload_sha256,
        provider=record["provider"],
    )


async def _admit_native_tui(
    reservation_id: str,
    record: dict[str, Any],
    request: ManagedLaunchV2AdmitRequest,
) -> dict[str, Any]:
    """Deliver one admitted task into a native TUI, with no ACP bridge.

    A native generation starts no bridge process and owns no control
    socket, so the bridge is not merely unnecessary here — it does not
    exist, and waiting on it is how a native admission used to end in a
    two-minute timeout and a fabricated ambiguity.  Delivery instead goes
    through the control adapter's idle-gated queue operation into the
    exact bound session.
    """
    import asyncio

    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services.canonical_json import canonical_sha256
    from cli_agent_orchestrator.services.native_pane_input import TmuxPaneInput

    native_control = _control_adapter(record["provider"])

    # The control adapter digests the payload with its own canonical
    # encoding, which is not the admission's raw-bytes ``message_sha256``.
    # Computed once here from the same string the delivery will carry, so
    # completion can bind the exact bytes without either side guessing at
    # the other's convention.
    expected_payload_sha256 = canonical_sha256(request.message)

    stored_admission = record.get("admission")
    if stored_admission is not None and not _is_retryable_refusal(
        stored_admission, request.delivery_id
    ):
        # A replay is answered from stored state before anything looks at
        # the pane. Running the first-delivery checks here would ask the
        # wrong question: the control adapter blocks a session holding an
        # unresolved ambiguity, and on a replay that ambiguity is this very
        # operation — so the gate would refuse to reconcile the one thing
        # that needs reconciling, permanently. Nothing is sent on this path
        # under any outcome, so there is nothing for a live check to protect.
        record, _ = claim_admission(reservation_id, request)
        # ``claim_admission`` cannot mint a fresh claim over an existing
        # admission (its update is filtered on a null admission), so the
        # call is purely the replay-identity check: a same-delivery-id
        # request with a different message, sender, context, or binding is
        # refused rather than answered from the earlier admission's state.
        return await asyncio.to_thread(
            _reconcile_native_admission,
            reservation_id,
            record,
            request,
            expected_payload_sha256,
        )

    # Every refusal below is decided before the transport is touched, so
    # each one is a proven zero-byte outcome — and each is written to the
    # reservation before the caller is answered. Without that, the only
    # trace of a refusal is an HTTP response, and a lost response leaves a
    # bare bound row that has to be read as maybe-delivered: a delivery
    # that provably never happened, preserved as ambiguous forever.
    try:
        identity = await asyncio.to_thread(_validate_native_admission_identity, record)
    except ManagedLaunchConflict as exc:
        # A permanent mismatch. Recorded as non-retryable and closed: the
        # bound identity is not going to become correct by asking again.
        refused = _persist_pre_io_refusal(
            reservation_id,
            request,
            REFUSED_NATIVE_IDENTITY,
            str(exc),
            observation=_readiness_observation(
                pane_id=None,
                provider_status=None,
                input_ready=False,
                detail="the bound identity was refused before the pane's readiness was read",
            ),
        )
        if not _refusal_was_persisted(refused, request.delivery_id, REFUSED_NATIVE_IDENTITY):
            return refused
        raise
    except ManagedLaunchUnavailable as exc:
        # The pane could not be observed at all. Recorded as retryable,
        # because that is a statement about this attempt rather than about
        # the generation, and closing the delivery on it would discard a
        # session that may be perfectly alive.
        refused = _persist_pre_io_refusal(
            reservation_id,
            request,
            REFUSED_PANE_UNOBSERVABLE,
            str(exc),
            observation=_readiness_observation(
                pane_id=None,
                provider_status=None,
                input_ready=False,
                detail=str(exc),
            ),
        )
        if not _refusal_was_persisted(refused, request.delivery_id, REFUSED_PANE_UNOBSERVABLE):
            return refused
        raise

    observed_at = _now()
    try:
        status = await asyncio.to_thread(
            partial(
                _observe_turn_state,
                record["provider"],
                pane_id=identity["pane_id"],
                terminal_id=record["terminal_id"],
                session_name=identity["session_name"],
                window_name=identity["window_name"],
            )
        )
    except Exception as exc:  # noqa: BLE001 - an unread pane, not a busy one
        refused = _persist_pre_io_refusal(
            reservation_id,
            request,
            REFUSED_PANE_UNOBSERVABLE,
            f"the bound native pane could not be read, so the task was not sent: {exc}",
            observation=_readiness_observation(
                pane_id=identity["pane_id"],
                provider_status=None,
                input_ready=False,
                detail=f"the pane could not be read: {exc}",
            ),
        )
        if not _refusal_was_persisted(refused, request.delivery_id, REFUSED_PANE_UNOBSERVABLE):
            return refused
        raise ManagedLaunchUnavailable(
            f"the bound native pane's readiness could not be observed, so the task was "
            f"not sent: {exc}"
        ) from exc
    if status is not TerminalStatus.IDLE:
        # Idle-gated by the provider's own reading of its own screen.
        # The boot window is the case that matters: the provider paints
        # its status bar before it can accept input, and a task delivered
        # into that window is absorbed with no error anywhere.
        #
        # Retryable, and the row is held at ``bound`` so the same delivery
        # id can still complete once the pane settles.
        detail = (
            f"the bound native pane reads {status.value!r}, not idle; the task is refused "
            f"rather than typed into a session that is mid-turn or still booting. Zero "
            f"task bytes were sent and this delivery id may be admitted once it reads idle"
        )
        refused = _persist_pre_io_refusal(
            reservation_id,
            request,
            REFUSED_PROVIDER_NOT_YET_READY,
            detail,
            observation=_readiness_observation(
                pane_id=identity["pane_id"],
                provider_status=status.value,
                input_ready=False,
                detail=None,
            ),
        )
        if not _refusal_was_persisted(refused, request.delivery_id, REFUSED_PROVIDER_NOT_YET_READY):
            return refused
        raise ManagedLaunchConflict(detail)

    record, should_send = claim_admission(reservation_id, request)
    if not should_send:
        # Lost the claim to a concurrent request for the same delivery id.
        # The sibling that won it is running *now*, so an absent control
        # operation here means "not opened yet", not "crashed before
        # opening" — refusing on it would publish a zero-byte verdict
        # about a delivery that is still being written.
        return await asyncio.to_thread(
            _reconcile_native_admission,
            reservation_id,
            record,
            request,
            expected_payload_sha256,
            may_refuse_absent_operation=False,
        )

    # The delivery id is the operation id: it is caller-minted, immutable
    # on the reservation, and already the identity a replay carries, so a
    # lost response addresses the exact operation with nothing derived.
    observation = native_control.turn_observation(
        active_turn_id=None,
        observed_at=observed_at,
        observer="managed_launch_v2.admit_reserved",
    )
    try:
        operation = await asyncio.to_thread(
            native_control.queue,
            operation_id=request.delivery_id,
            native_session_id=identity["native_session_id"],
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            execution_mode=em.NATIVE_TUI,
            text=request.message,
            observation=observation,
            transport=TmuxPaneInput(identity["pane_id"]),
            # The version the generation was *bound* to, not one probed
            # now. Which keystroke breaks a composer line without sending
            # is a fact about the build that is running, and the binding
            # is the record of which build that is.
            provider_version=(record.get("binding") or {}).get("provider_version"),
        )
    except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
        # Deliberately conservative. The adapter turns transport failures
        # into ambiguous records itself, so an exception escaping it is a
        # store or programming failure whose position relative to the
        # keystrokes is unknown — and treating unknown as "not sent" is
        # what would license a duplicate task.
        return mark_admission_ambiguous(
            reservation_id,
            request.delivery_id,
            f"native control raised while delivering the task: {exc}",
        )
    return _settle_native_admission(
        reservation_id,
        request.delivery_id,
        operation,
        expected_payload_sha256,
        provider=record["provider"],
    )


async def admit_reserved(
    reservation_id: str,
    request: ManagedLaunchV2AdmitRequest,
    *,
    registry=None,
) -> dict[str, Any]:
    """Admit one task after native bind, with no blind retry on ambiguity."""
    import asyncio

    from cli_agent_orchestrator.services.managed_provider_bridge import (
        read_state,
        request_bridge,
    )

    record = get(reservation_id)
    # The fence check is the admission boundary: a sealed generation
    # rejects every post-fence input with zero provider I/O.
    generation_fence.assert_admission_open(
        COMPANION_DIR, record["terminal_id"], record["generation"]
    )
    # Admission branches on the immutable mode exactly as launch does,
    # and the two branches share no code below this point. The ACP bridge
    # is a lawful admission transport for ACP rows only: a native
    # generation never starts one, so requiring it there would mean
    # waiting on a socket that will never exist. Neither branch can
    # partially become the other, and neither is ever a fallback for the
    # other's failure.
    if record["execution_mode"] == em.NATIVE_TUI:
        return await _admit_native_tui(reservation_id, record, request)

    record, should_send = claim_admission(reservation_id, request)
    if not should_send:
        if record["state"] == "admitting":
            state = read_state(reservation_id)
            receipt = state.get("submission") if state else None
            if isinstance(receipt, dict):
                return complete_admission(reservation_id, request.delivery_id, receipt)
        return record
    command = {
        "op": "admit",
        "reservation_id": reservation_id,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "delivery_id": request.delivery_id,
        "message": request.message,
        "message_sha256": request.message_sha256,
        "sender_id": request.sender_id,
        "orchestration_type": request.orchestration_type,
        "context": request.context.model_dump(mode="json"),
    }
    try:
        response = await asyncio.to_thread(request_bridge, reservation_id, command, timeout=120.0)
    except Exception as exc:  # noqa: BLE001 - delivery may have crossed the boundary
        try:
            state = read_state(reservation_id)
        except Exception:  # noqa: BLE001
            state = None
        receipt = state.get("submission") if state else None
        if isinstance(receipt, dict):
            return complete_admission(reservation_id, request.delivery_id, receipt)
        return mark_admission_ambiguous(reservation_id, request.delivery_id, str(exc))
    receipt = response.get("receipt")
    if not isinstance(receipt, dict):
        return mark_admission_ambiguous(
            reservation_id,
            request.delivery_id,
            "provider bridge returned no structured submission receipt",
        )
    return complete_admission(reservation_id, request.delivery_id, receipt)


def attempt_resume(reservation_id: str, *, containment_proven: bool = False) -> dict[str, Any]:
    """The v2 resume seam — fail-closed while prior-generation proof is red.

    Prior-generation quiescence requires the containment-backed
    no-survivor proof, which is PF-1b-gated; while unproven every resume
    attempt is refused (45) and journaled as a refusal payload fact.
    """
    record = get(reservation_id)
    attempt_id = str(uuid.uuid4())
    binding = record.get("binding") or {}
    payload = recovery_receipts.resume_refusal_payload(
        provider=_PINNED_PROVIDER.get(record["provider"], "codex"),
        native_id=binding.get("native_session_id", "unbound"),
        resume_attempt_id=attempt_id,
        refusal_code=45,
        reason="prior-generation-unproven" if not containment_proven else "containment-lost",
        at=_now(),
    )
    digest = recovery_receipts.payload_digest(payload)
    if not containment_proven:
        raise ManagedLaunchConflict(
            "resume refused (45 prior-generation-unproven): prior-generation "
            "quiescence requires the containment-backed no-survivor proof, "
            f"which is PF-1b-gated and not green; refusal payload {digest}"
        )
    raise ManagedLaunchConflict(
        "resume refused (45): the resume admission state machine is conductor-owned; "
        "the fork seam records facts only"
    )
