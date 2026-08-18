"""The operator-message operation (Lane C, design §8.3).

A sibling typed operation to control-input — never an extension of it
(D11): long text (≤ 8192 UTF-8 bytes), multi-line through the provider's
build-proven composer-newline plan, and staged image references, with the
same identity, lease, journal, and reconciliation discipline as the
control path.  Control-input's 512-byte short-command invariant is
untouched.

Discipline, in request order:

1. Shape is 422; every terminal-level failure is 200 with a typed outcome
   from the same closed vocabulary (``accepted / refused / ambiguous /
   unsupported``), with the §8.3 message-specific reasons added.  Only a
   ``refused`` outcome proves zero bytes and permits an operator-initiated
   fresh attempt with a **new** operation id.
2. A duplicate ``operation_id`` carrying an identical payload replays the
   stored answer with zero new I/O — checked against the provider
   adapter's operation store *before* attachment state is ever consulted
   (§8.4).  A divergent payload on a reused id is ``request-rebound``.
   A message is **never re-sent automatically**; a lost response is
   resolved by one exact-id ``GET`` against the journaled record.
3. At-most-once is journaled through the provider adapter's operation
   store (§8.3, OD6): ``intended → posted`` with ``ambiguous`` frozen
   until exact-id reconcile.  The store's ``posted`` state maps to the
   wire ``accepted``: it means the whole payload plus exactly one
   submitting Enter provably reached the pane under the lease — the same
   fact control-input's journal calls ``delivered``.  Provider-side
   acceptance is a separate, later observation and is never claimed here.
4. Every write holds the pane-input lease with
   ``holder="operator-message:{operation_id}"`` (it does not ride the
   unleased v2 admission path, F2), performs the control path's exact
   under-lease re-proof (pane alive, ``window_id``, ``pane_pid``, canonical
   server socket) immediately before invoking the plan and after any
   copy-mode cancel, observes the kimi dispatch grace and the
   readiness/idle gate, and threads the 20 s write deadline through every
   read.
5. Images are staged-file references, never bytes (D12): the composer
   draft's ``[Image #N]`` tokens are substituted server-side with the
   registry's per-provider ``reference_template`` (kimi: the proven
   ``ReadMediaFile`` directive phrasing; claude: the documented bare
   path), translated host→guest through the terminal profile's
   ``container.path_maps`` longest-prefix match.  A staged path with no
   matching map is refused ``attachment-not-ready`` — never substituted
   as an unreadable host path.  Substitution itself needs only read-only
   attachment facts: the ``ready → submitted(operation_id)`` binding
   happens at the last safe pre-write point — the adapter's pre-write
   hook, after the journal claim and every deliverability/idle gate —
   so a zero-byte refusal (path substitution, lease/copy-mode,
   readiness/native preflight, journal claim) never strands a ready
   image as submitted to an operation that wrote nothing, and a fresh
   operation id may retry the unchanged image (Lane C r1).

Two refusal reasons surface beyond §8.3's list, both adapter truths the
operation store can hold and a caller must be able to act on:
``unresolved-ambiguity`` (an earlier operation on this session is frozen
ambiguous; reconcile it by exact id first) and ``provider-refused`` (an
external reconciler resolved this operation as refused by the provider).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple, cast

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import image_attachments, provider_controls
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    AMBIGUOUS,
    REASON_COPY_MODE_ACTIVE,
    REASON_GENERATION_FENCED,
    REASON_HANDOFF_HELD,
    REASON_HANDOFF_HOLD_UNDECIDABLE,
    REASON_IDENTITY_MISMATCH,
    REASON_ILLEGAL_CONTROL_BYTES,
    REASON_LINEAGE_UNPROVEN,
    REASON_MANAGED_ACP_PANE,
    REASON_OWNER_LOST_BEFORE_WRITE,
    REASON_PANE_BUSY,
    REASON_PANE_DEAD,
    REASON_PROVIDER_UNSUPPORTED,
    REASON_REQUEST_REBOUND,
    REASON_RESPONSE_LOST,
    REASON_SERVER_IDENTITY_MISMATCH,
    REASON_SERVER_IDENTITY_UNBOUND,
    REASON_SERVER_IDENTITY_UNREADABLE,
    REASON_STALE_GENERATION,
    REASON_UNKNOWN_TERMINAL,
    REASON_WRITE_DEADLINE,
    REASON_WRITE_INCOMPLETE,
    REFUSED,
    normalize_expected_identity,
    server_identity_refusal,
)
from cli_agent_orchestrator.services.control_input_journal import ControlInputBinding
from cli_agent_orchestrator.services.pane_input_arbiter import (
    PaneBusyError,
    pane_input_lease,
)

logger = logging.getLogger(__name__)

#: §8.3 message-specific refusal reasons (plus the two adapter-surface
#: truths documented in the module docstring).
REASON_ATTACHMENT_UNKNOWN = image_attachments.REASON_ATTACHMENT_UNKNOWN
REASON_ATTACHMENT_NOT_READY = image_attachments.REASON_ATTACHMENT_NOT_READY
REASON_ATTACHMENT_TOO_LARGE = image_attachments.REASON_ATTACHMENT_TOO_LARGE
REASON_ATTACHMENT_TYPE_UNSUPPORTED = image_attachments.REASON_ATTACHMENT_TYPE_UNSUPPORTED
REASON_MESSAGE_TOO_LARGE = "message-too-large"
REASON_MULTILINE_UNPROVEN = "multiline-unproven"
REASON_UNRESOLVED_AMBIGUITY = "unresolved-ambiguity"
REASON_PROVIDER_REFUSED = "provider-refused"

MAX_TEXT_BYTES = provider_controls.OPERATOR_MESSAGE_MAX_TEXT_BYTES
MAX_ATTACHMENTS = provider_controls.OPERATOR_MESSAGE_MAX_ATTACHMENTS

_TOKEN_PATTERN = re.compile(r"\[Image #(\d+)\]")
_OPERATION_KIND = "operator-message"

#: The closed reason → outcome binding for this operation's vocabulary
#: (mirroring the control contract's import-time discipline).  Refusal
#: reasons reused from the control path keep their deployed binding;
#: ``write-incomplete``/``response-lost`` are the ambiguous pair.
_REASON_OUTCOMES: Dict[str, str] = {
    REASON_UNKNOWN_TERMINAL: REFUSED,
    REASON_IDENTITY_MISMATCH: REFUSED,
    REASON_STALE_GENERATION: REFUSED,
    REASON_LINEAGE_UNPROVEN: REFUSED,
    REASON_MANAGED_ACP_PANE: REFUSED,
    REASON_PANE_DEAD: REFUSED,
    REASON_PANE_BUSY: REFUSED,
    REASON_COPY_MODE_ACTIVE: REFUSED,
    REASON_GENERATION_FENCED: REFUSED,
    REASON_HANDOFF_HELD: REFUSED,
    REASON_HANDOFF_HOLD_UNDECIDABLE: REFUSED,
    REASON_WRITE_DEADLINE: REFUSED,
    REASON_PROVIDER_UNSUPPORTED: REFUSED,
    REASON_ILLEGAL_CONTROL_BYTES: REFUSED,
    REASON_REQUEST_REBOUND: REFUSED,
    REASON_OWNER_LOST_BEFORE_WRITE: REFUSED,
    REASON_SERVER_IDENTITY_UNBOUND: REFUSED,
    REASON_SERVER_IDENTITY_UNREADABLE: REFUSED,
    REASON_SERVER_IDENTITY_MISMATCH: REFUSED,
    REASON_ATTACHMENT_UNKNOWN: REFUSED,
    REASON_ATTACHMENT_NOT_READY: REFUSED,
    REASON_ATTACHMENT_TOO_LARGE: REFUSED,
    REASON_ATTACHMENT_TYPE_UNSUPPORTED: REFUSED,
    REASON_MESSAGE_TOO_LARGE: REFUSED,
    REASON_MULTILINE_UNPROVEN: REFUSED,
    REASON_UNRESOLVED_AMBIGUITY: REFUSED,
    REASON_PROVIDER_REFUSED: REFUSED,
    REASON_WRITE_INCOMPLETE: AMBIGUOUS,
    REASON_RESPONSE_LOST: AMBIGUOUS,
}
assert set(_REASON_OUTCOMES.values()) <= {REFUSED, AMBIGUOUS}

#: The wire outcome reported for a store record that reached a terminal
#: answer; ``delivered`` mirrors the control journal's word for "all bytes
#: plus the one Enter provably written".
_REASON_DELIVERED = "delivered"


class OperatorMessageRequestInvalid(ValueError):
    """A request-shape error; the API layer answers 422."""


class AttachmentRefusal(RuntimeError):
    """A typed attachment-route answer (upload/delete).

    Carries the HTTP status and the typed body fields so the route layer
    stays thin; ``record`` is the durable manifest record (e.g. the
    ``failed`` record of a refused upload) when one exists.
    """

    def __init__(
        self,
        status_code: int,
        reason_code: str,
        detail: str,
        record: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.reason_code = reason_code
        self.detail = detail
        self.record = record

    def as_response(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "outcome": REFUSED,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }
        if self.record is not None:
            body["attachment"] = self.record
        return body


@dataclass(frozen=True)
class OperatorMessageResult:
    """One typed operator-message answer (200 on the wire)."""

    operation_id: str
    outcome: str
    reason_code: str
    detail: str
    replayed: bool = False
    record_state: Optional[str] = None
    operation: Optional[Dict[str, Any]] = None
    http_status: int = 200
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome in (REFUSED, AMBIGUOUS):
            bound = _REASON_OUTCOMES.get(self.reason_code)
            if bound is None:
                raise ValueError(
                    f"reason {self.reason_code!r} is not in the operator-message vocabulary"
                )
            if bound != self.outcome:
                raise ValueError(
                    f"reason {self.reason_code!r} is bound to {bound}, not {self.outcome}"
                )

    def as_response(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "operation_id": self.operation_id,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "replayed": self.replayed,
        }
        if self.record_state is not None:
            body["record_state"] = self.record_state
        if self.operation is not None:
            body["operation"] = self.operation
        body.update(self.extra)
        return body


def _utc_isots() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _result(
    operation_id: str,
    outcome: str,
    reason_code: str,
    detail: str,
    **kwargs: Any,
) -> OperatorMessageResult:
    return OperatorMessageResult(
        operation_id=operation_id,
        outcome=outcome,
        reason_code=reason_code,
        detail=detail,
        **kwargs,
    )


# --- Request shape -----------------------------------------------------------


def _require_operation_id(operation_id: Any) -> str:
    if not isinstance(operation_id, str) or not operation_id:
        raise OperatorMessageRequestInvalid("operation_id must be a non-empty uuid string")
    try:
        parsed = uuidlib.UUID(operation_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise OperatorMessageRequestInvalid(
            f"operation_id must be a uuid; got {operation_id!r}"
        ) from exc
    return str(parsed)


def _validate_attachment_ids(attachments: Any) -> List[str]:
    if attachments is None:
        return []
    if not isinstance(attachments, list) or any(
        not isinstance(item, str) or not item for item in attachments
    ):
        raise OperatorMessageRequestInvalid("attachments must be a list of attachment id strings")
    if len(attachments) != len(set(attachments)):
        raise OperatorMessageRequestInvalid("attachments must not repeat an id")
    if len(attachments) > MAX_ATTACHMENTS:
        raise OperatorMessageRequestInvalid(
            f"attachments carries {len(attachments)} ids; at most "
            f"{MAX_ATTACHMENTS} images may ride one operator message (§8.3)"
        )
    return list(attachments)


def _validate_token_map(token_map: Any) -> Dict[str, str]:
    if token_map is None:
        return {}
    if not isinstance(token_map, dict):
        raise OperatorMessageRequestInvalid("token_map must be an object")
    validated: Dict[str, str] = {}
    for key, value in token_map.items():
        if not isinstance(key, str) or not key.isdigit():
            raise OperatorMessageRequestInvalid(
                f"token_map key {key!r} is not an image-token number"
            )
        if not isinstance(value, str) or not value:
            raise OperatorMessageRequestInvalid(
                f"token_map value for #{key} must be an attachment id string"
            )
        validated[key] = value
    return validated


def _cross_validate_tokens(text: str, attachments: List[str], token_map: Dict[str, str]) -> None:
    """The §8.4 token contract: every token mapped, every mapping real.

    A token without a mapping, a mapping to an unlisted attachment, or an
    attachment no token references is a 422 shape error — never silently
    dropped, never partially submitted.
    """
    tokens_in_text = set(_TOKEN_PATTERN.findall(text))
    unmapped = tokens_in_text - set(token_map)
    if unmapped:
        raise OperatorMessageRequestInvalid(
            f"draft carries [Image #{sorted(unmapped)[0]}] with no token_map entry; "
            "a token without a mapping is a shape error, never silently dropped"
        )
    stale = set(token_map) - tokens_in_text
    if stale:
        raise OperatorMessageRequestInvalid(
            f"token_map maps #{sorted(stale)[0]} but the draft carries no such token"
        )
    attachment_set = set(attachments)
    mapped = set(token_map.values())
    unknown = mapped - attachment_set
    if unknown:
        raise OperatorMessageRequestInvalid(
            f"token_map references attachment {sorted(unknown)[0]!r}, which is not "
            "in the attachments list"
        )
    unreferenced = attachment_set - mapped
    if unreferenced:
        raise OperatorMessageRequestInvalid(
            f"attachment {sorted(unreferenced)[0]!r} is listed but no [Image #N] "
            "token references it"
        )


def _request_digest(
    terminal_id: str, text: str, attachments: List[str], token_map: Dict[str, str]
) -> str:
    """The whole-request replay identity (§8.3 at-most-once row).

    Digests everything the operator sent — draft text, attachment ids, and
    the token mapping — so a same-id request whose attachments changed is a
    conflict, not a replay.  Computable from the request alone, which is
    what lets an identical replay answer from the operation store before
    attachment state is ever consulted (§8.4).
    """
    canonical = json.dumps(
        {
            "kind": _OPERATION_KIND,
            "terminal_id": terminal_id,
            "text": text,
            "attachments": attachments,
            "token_map": token_map,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Operation-store access (OD6: the id spans two per-provider stores) -------


def _provider_stores() -> List[Any]:
    from cli_agent_orchestrator.services import (
        claude_native_control,
        codex_native_control,
        kimi_native_control,
    )

    return [kimi_native_control, claude_native_control, codex_native_control]


def _find_operation(operation_id: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Exact-id lookup across both provider stores.

    Returns ``(record, errors)``.  A store that cannot be read is not a
    store without the record: any error with no hit means "unknown", never
    "proven absent" — the caller answers ambiguously, because refusing on a
    guess could license a duplicate send.
    """
    record: Optional[Dict[str, Any]] = None
    errors: List[str] = []
    for store in _provider_stores():
        try:
            found = store.get(operation_id)
        except Exception as exc:  # noqa: BLE001 - "could not look" is not "absent"
            errors.append(f"{store.__name__.rsplit('.', 1)[-1]}: {exc}")
            continue
        if found is not None:
            record = found
            break
    return record, errors


# --- Store-record → wire outcome mapping --------------------------------------

_ADAPTER_REFUSAL_MAP = {
    "active_turn_in_progress": REASON_PANE_BUSY,
    "attachment_not_owned": REASON_IDENTITY_MISMATCH,
    "unresolved_ambiguity": REASON_UNRESOLVED_AMBIGUITY,
    "unsupported_control": REASON_PROVIDER_UNSUPPORTED,
    "provider_refused": REASON_PROVIDER_REFUSED,
    "image_attachment_unknown": REASON_ATTACHMENT_UNKNOWN,
    "image_attachment_not_ready": REASON_ATTACHMENT_NOT_READY,
}


def _outcome_for_record(
    operation_id: str,
    record: Dict[str, Any],
    *,
    replayed: bool,
    multiline: bool,
) -> OperatorMessageResult:
    """Map one journaled operation record to the wire vocabulary.

    ``posted``/``accepted``/``completed`` all mean the payload provably
    reached the pane exactly once (the control journal's ``delivered``);
    ``refused`` is terminal and reattemptable only as a *fresh* id;
    ``ambiguous`` is the frozen unknown; a stranded ``intended`` (crash
    between journaling and typing) is reported as the unknown it is —
    never silently as success or failure.
    """
    state = record.get("state")
    if state in ("posted", "accepted", "completed"):
        return _result(
            operation_id,
            ACCEPTED,
            _REASON_DELIVERED,
            "the message was typed into the provider composer under the pane-input "
            "lease and submitted with exactly one Enter (journaled state "
            f"{state!r})",
            replayed=replayed,
            record_state=state,
            operation=record,
        )
    if state == "refused":
        adapter_reason = record.get("refusal_reason") or ""
        if adapter_reason == "composer_newline_unproven":
            reason = REASON_MULTILINE_UNPROVEN if multiline else REASON_PROVIDER_UNSUPPORTED
        else:
            reason = _ADAPTER_REFUSAL_MAP.get(adapter_reason, REASON_PROVIDER_UNSUPPORTED)
        observation = record.get("observation") or {}
        detail = observation.get("detail") or f"the provider adapter refused: {adapter_reason}"
        return _result(
            operation_id,
            REFUSED,
            reason,
            detail,
            replayed=replayed,
            record_state=state,
            operation=record,
        )
    if state == "ambiguous":
        return _result(
            operation_id,
            AMBIGUOUS,
            REASON_WRITE_INCOMPLETE,
            f"the write's outcome is unknown and frozen: "
            f"{record.get('ambiguity_reason') or 'no detail recorded'}; reconcile by "
            "exact operation id — the message is never re-sent automatically",
            replayed=replayed,
            record_state=state,
            operation=record,
        )
    # ``intended``: journaled, never typed (or the record of a crash between
    # the two).  Reported as the unknown it is.
    return _result(
        operation_id,
        AMBIGUOUS,
        REASON_RESPONSE_LOST,
        "the operation was journaled but no transport record exists (a crash "
        "window); whether anything was typed is unknown — reconcile by exact "
        "operation id, never resend",
        replayed=replayed,
        record_state=state,
        operation=record,
    )


# --- Container path translation (§8.4) ----------------------------------------


def _terminal_agent_profile(terminal_id: str) -> Optional[Any]:
    """The terminal's agent profile object, or None when it names none."""
    from cli_agent_orchestrator.services import control_input_service
    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

    metadata = control_input_service._terminal_metadata(terminal_id)
    name = (metadata or {}).get("agent_profile")
    if not name:
        return None
    return load_agent_profile(name)


def _translate_staged_path(
    host_path: str, profile: Optional[Any], record: Mapping[str, Any]
) -> str:
    """Host → guest by longest-prefix match, mirroring the provider layer.

    Mirrors ``providers/base.py:558-587`` exactly (including the root-map
    fix: ``best_len`` starts at -1 so a ``/`` map can win).  When the
    profile declares container path maps and none covers the staged path,
    the honest answer is ``attachment-not-ready`` — never an unreadable
    host path handed to a containerized provider (§8.4).
    """
    path_maps = None
    if profile is not None and getattr(profile, "container", None) is not None:
        path_maps = profile.container.path_maps
    if not path_maps:
        return host_path

    best_match = None
    best_len = -1
    for mapping in path_maps:
        host_prefix = mapping.host.rstrip("/")
        if host_path == host_prefix or host_path.startswith(host_prefix + "/"):
            if len(host_prefix) > best_len:
                best_match = mapping
                best_len = len(host_prefix)
    if best_match is None:
        raise image_attachments.AttachmentBindingError(
            REASON_ATTACHMENT_NOT_READY,
            f"image attachment {record.get('attachment_id')} is staged at {host_path}, "
            f"which maps to no guest path under this terminal's container.path_maps; "
            "refusing rather than substituting a path the provider cannot read",
        )
    guest_prefix = str(best_match.guest).rstrip("/")
    return guest_prefix + host_path[best_len:]


def _substitute_references(
    terminal_id: str,
    text: str,
    token_map: Dict[str, str],
    bound_records: List[Dict[str, Any]],
    reference_template: str,
) -> str:
    """Replace each ``[Image #N]`` token with the provider's reference.

    Every replacement is the registry's ``reference_template`` with the
    translated absolute staged path — kimi's proven ``ReadMediaFile``
    directive or claude's documented bare path (§8.4/§8.6).
    """
    by_id = {record["attachment_id"]: record for record in bound_records}
    profile: Optional[Any] = None
    profile_loaded = False
    substituted = text
    for token, attachment_id in token_map.items():
        record = by_id[attachment_id]
        if not profile_loaded:
            try:
                profile = _terminal_agent_profile(terminal_id)
            except Exception as exc:  # noqa: BLE001 - fail closed
                raise image_attachments.AttachmentBindingError(
                    REASON_ATTACHMENT_NOT_READY,
                    f"the terminal's agent profile could not be loaded, so whether a "
                    f"container path translation applies is unknowable: {exc}; refusing "
                    "rather than substituting a possibly unreadable path",
                ) from exc
            profile_loaded = True
        host_path = str(image_attachments.staged_absolute_path(record))
        guest_path = _translate_staged_path(host_path, profile, record)
        reference = reference_template.replace("{path}", guest_path)
        substituted = substituted.replace(f"[Image #{token}]", reference)
    return substituted


# --- Attachment routes (upload / list / delete) --------------------------------


def _capability_blocks(
    resolved: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """This terminal's build-exact ``operator_message``/``image`` blocks."""
    controls = provider_controls.controls_for(resolved.provider, resolved.provider_version)
    if controls is None:
        return None, None
    return controls["operator_message"], controls["image"]


def upload_attachment(
    terminal_id: str, *, display_filename: Optional[str], content: bytes
) -> Dict[str, Any]:
    """Stage one uploaded image for one terminal (§8.4).

    Raises :class:`LookupError` (→ 404) for an unknown terminal and
    :class:`AttachmentRefusal` (→ typed 422) for capability, size, type,
    and content failures; returns the ``ready`` record on success.
    """
    from cli_agent_orchestrator.services import control_input_service

    resolved = control_input_service.resolve_control_identity(terminal_id)
    if resolved is None:
        raise LookupError(f"no terminal {terminal_id!r} is known to this server")
    _operator_block, image_block = _capability_blocks(resolved)
    if image_block is None:
        raise AttachmentRefusal(
            422,
            REASON_PROVIDER_UNSUPPORTED,
            f"provider {resolved.provider!r} on build "
            f"{resolved.provider_version!r} advertises no image capability; "
            "unproven providers refuse image uploads honestly",
        )
    try:
        return image_attachments.stage_upload(
            terminal_id,
            display_filename=display_filename,
            content=content,
            allowed_formats=frozenset(image_block["formats"]),
        )
    except image_attachments.AttachmentValidationError as exc:
        raise AttachmentRefusal(
            422, exc.reason_code, exc.detail, record=getattr(exc, "record", None)
        ) from exc


def list_terminal_attachments(terminal_id: str) -> List[Dict[str, Any]]:
    from cli_agent_orchestrator.services import control_input_service

    if control_input_service.resolve_control_identity(terminal_id) is None:
        raise LookupError(f"no terminal {terminal_id!r} is known to this server")
    return image_attachments.list_attachments(terminal_id)


def delete_terminal_attachment(terminal_id: str, attachment_id: str) -> Dict[str, Any]:
    from cli_agent_orchestrator.services import control_input_service

    if control_input_service.resolve_control_identity(terminal_id) is None:
        raise LookupError(f"no terminal {terminal_id!r} is known to this server")
    try:
        return image_attachments.remove_attachment(terminal_id, attachment_id)
    except image_attachments.AttachmentNotFoundError as exc:
        raise LookupError(str(exc)) from exc
    except image_attachments.AttachmentBindingError as exc:
        # A submitted image is retained read-only for its TTL (§8.4).
        raise AttachmentRefusal(409, exc.reason_code, exc.detail) from exc


# --- The submit pipeline --------------------------------------------------------


def submit_operator_message(
    terminal_id: str,
    *,
    operation_id: Any,
    text: Any,
    attachments: Any,
    token_map: Any,
    expected_identity: Any,
    lease_timeout: float = 0.0,
) -> OperatorMessageResult:
    """Submit one text+image operator message, at most once, or say why not."""
    from cli_agent_orchestrator.services import control_input_service
    from cli_agent_orchestrator.services.control_input_service import (
        WRITE_DEADLINE_SECONDS,
    )

    operation_id = _require_operation_id(operation_id)
    if not isinstance(text, str):
        raise OperatorMessageRequestInvalid("text must be a string")
    attachment_ids = _validate_attachment_ids(attachments)
    tokens = _validate_token_map(token_map)
    if expected_identity is None:
        raise OperatorMessageRequestInvalid(
            "expected_identity is required: an operator message binds the same "
            "9-field identity as a control input"
        )
    if not text.strip() and not attachment_ids:
        raise OperatorMessageRequestInvalid(
            "the draft carries no text and no attachments; there is nothing to send"
        )
    _cross_validate_tokens(text, attachment_ids, tokens)

    # The §8.3 text budget — a typed refusal, never a silent truncation.
    text_bytes = len(text.encode("utf-8"))
    if text_bytes > MAX_TEXT_BYTES:
        return _result(
            operation_id,
            REFUSED,
            REASON_MESSAGE_TOO_LARGE,
            f"text is {text_bytes} UTF-8 bytes, over the {MAX_TEXT_BYTES}-byte "
            "operator-message limit; the draft is refused whole, never truncated",
        )
    screened = control_input_service.screen_inbox_payload_text(text)
    if screened is not None:
        return _result(operation_id, REFUSED, screened[0], screened[1])

    # Replay check first (§8.4): an identical same-id POST answers from the
    # operation store with zero attachment or pane I/O.
    digest = _request_digest(terminal_id, text, attachment_ids, tokens)
    existing, lookup_errors = _find_operation(operation_id)
    if existing is None and lookup_errors:
        # Fail closed (r16 Sol P1.1): a store that could not be read is not
        # a store without the record.  Absence is unprovable, so a fresh
        # send could license a duplicate — answer the honest unknown before
        # any identity, attachment, lease, or adapter I/O, exactly as the
        # exact-id reconcile does.
        return _result(
            operation_id,
            AMBIGUOUS,
            REASON_RESPONSE_LOST,
            f"the operation store could not be read ({'; '.join(lookup_errors)}), so "
            "whether a record exists is unknown; nothing was submitted — reconcile "
            "by exact operation id, never resend",
        )
    if existing is not None:
        if (
            existing.get("kind") != _OPERATION_KIND
            or existing.get("terminal_id") != terminal_id
            or existing.get("payload_sha256") != digest
        ):
            return _result(
                operation_id,
                REFUSED,
                REASON_REQUEST_REBOUND,
                "operation id was already used with a different payload; a "
                "caller-minted id is immutable",
            )
        return _outcome_for_record(operation_id, existing, replayed=True, multiline="\n" in text)

    resolved = control_input_service.resolve_control_identity(terminal_id)
    if resolved is None:
        return _result(
            operation_id,
            REFUSED,
            REASON_UNKNOWN_TERMINAL,
            f"no terminal {terminal_id!r} is known to this server; nothing was typed",
        )
    try:
        expected = normalize_expected_identity(expected_identity)
    except ValueError as exc:
        return _result(operation_id, REFUSED, REASON_IDENTITY_MISMATCH, str(exc))
    identity_refusal = control_input_service.screen_expected_identity(expected, resolved)
    if identity_refusal is not None:
        return _result(operation_id, REFUSED, identity_refusal[0], identity_refusal[1])
    if resolved.pane_id is None or resolved.pane_dead:
        return _result(
            operation_id,
            REFUSED,
            REASON_PANE_DEAD,
            f"pane {resolved.recorded_pane_id!r} is gone or dead; nothing was typed",
        )
    if resolved.window_id is None or resolved.pane_pid is None:
        return _result(
            operation_id,
            REFUSED,
            REASON_LINEAGE_UNPROVEN,
            "the pane's window and root process could not both be observed; " "nothing was typed",
        )
    server_refusal = server_identity_refusal(
        bound=resolved.bound_server_socket_path,
        observed=resolved.observed_server_socket_path,
    )
    if server_refusal is not None:
        return _result(operation_id, REFUSED, server_refusal[0], server_refusal[1])

    # The capability gate (§8.5): no advertised operator_message block means
    # this provider/build cannot receive a message — refused honestly.
    operator_block, image_block = _capability_blocks(resolved)
    if operator_block is None:
        return _result(
            operation_id,
            REFUSED,
            REASON_PROVIDER_UNSUPPORTED,
            f"provider {resolved.provider!r} on build {resolved.provider_version!r} "
            "advertises no operator-message capability; the message was not typed",
        )
    if attachment_ids and image_block is None:
        return _result(
            operation_id,
            REFUSED,
            REASON_PROVIDER_UNSUPPORTED,
            f"provider {resolved.provider!r} on build {resolved.provider_version!r} "
            "advertises no image capability; attachments were not submitted",
        )

    # This lane proves the receiver is parked at an input-ready composer by
    # reading the turn state of the deterministic managed window, whose name
    # is derived from the generation.  A row that records none -- the ordinary
    # legacy shape, which ``conduct status`` reports as
    # ``<id>@None(legacy-no-generation/live)`` -- cannot have that window
    # named, so its readiness cannot be observed and no byte may be typed on
    # an unproven composer.  Answered here, from the resolution: it needs
    # nothing the attachment binding, the admission, or the pane lease
    # produces, and it sits after the replay so an operation already recorded
    # as delivered is still answered from its record rather than told nothing
    # was typed.
    if resolved.terminal_generation is None:
        return _result(
            operation_id,
            REFUSED,
            REASON_LINEAGE_UNPROVEN,
            f"terminal {terminal_id} records no generation, so the managed window "
            "whose turn state proves this composer is idle cannot be named; nothing "
            "was typed",
        )

    # Reference substitution needs read-only attachment facts only.  The
    # ready → submitted(operation_id) binding itself happens at the last
    # safe pre-write point — the adapter's pre-write hook, after the
    # journal claim and every deliverability/idle gate — so a zero-byte
    # refusal anywhere in the lease/gate run never strands a ready image
    # as submitted to an operation that wrote nothing (Lane C r1).  The
    # checks below mirror bind_for_submit's read side (without the
    # transition) so the common failures answer with the same immediate
    # typed refusal as before; anything that races them is re-checked
    # atomically by the hook under the manifest lock.
    attachment_records: List[Dict[str, Any]] = []
    for attachment_id in attachment_ids:
        try:
            record = image_attachments.get_attachment(terminal_id, attachment_id)
        except image_attachments.AttachmentNotFoundError:
            return _result(
                operation_id,
                REFUSED,
                REASON_ATTACHMENT_UNKNOWN,
                f"no image attachment {attachment_id!r} is staged for terminal "
                f"{terminal_id!r}; nothing was submitted",
            )
        record_state = record["state"]
        if record_state == image_attachments.STATE_SUBMITTED:
            bound_to = record.get("bound_operation_id")
            if bound_to != operation_id:
                return _result(
                    operation_id,
                    REFUSED,
                    REASON_ATTACHMENT_NOT_READY,
                    f"image attachment {attachment_id} is already submitted to "
                    f"operation {bound_to}; a different operation may not reference it",
                )
        elif record_state != image_attachments.STATE_READY:
            return _result(
                operation_id,
                REFUSED,
                REASON_ATTACHMENT_NOT_READY,
                f"image attachment {attachment_id} is {record_state}, not ready; only "
                "a validated, fully staged image may be submitted",
            )
        if record["format"] not in (image_block or {}).get("formats", ()):
            return _result(
                operation_id,
                REFUSED,
                REASON_ATTACHMENT_TYPE_UNSUPPORTED,
                f"image attachment {record['attachment_id']} is {record['format']}, "
                f"which {resolved.provider} does not advertise "
                f"({(image_block or {}).get('formats')}); unproven formats are refused",
            )
        attachment_records.append(record)

    try:
        substituted = _substitute_references(
            terminal_id,
            text,
            tokens,
            attachment_records,
            (image_block or {}).get("reference_template", "{path}"),
        )
    except image_attachments.AttachmentBindingError as exc:
        return _result(operation_id, REFUSED, exc.reason_code, exc.detail)

    binding = ControlInputBinding(
        request_id=operation_id,
        terminal_id=terminal_id,
        pane_id=resolved.pane_id,
        window_id=resolved.window_id,
        pane_pid=resolved.pane_pid,
        request_sha256=digest,
        generation=resolved.terminal_generation,
        server_socket_path=resolved.bound_server_socket_path,
    )
    client = control_input_service._tmux_client()
    deadline = time.monotonic() + WRITE_DEADLINE_SECONDS
    try:
        from cli_agent_orchestrator.services import generation_fence, task_handoff

        # Hold the shared generation fence through the adapter's durable claim
        # and every literal/submit key; a pane lease alone would let this lane
        # race a completed park receipt.  ``binding.generation`` is non-None
        # here because the readiness gate above refuses every generation-less
        # row.  Should a managed row ever reach the admission without one, it
        # raises ``GenerationUnselectable`` rather than admitting an unfenced
        # byte, and the ``FencedError`` handler below settles that as a typed
        # zero-byte refusal.
        with (
            control_input_service.provider_byte_admission(
                resolved, terminal_id, binding.generation
            ),
            pane_input_lease(
                resolved.pane_id,
                holder=f"operator-message:{operation_id}",
                timeout=lease_timeout,
            ),
        ):
            # The same live re-read the control path requires: a pane that
            # died or was replaced between resolution and write is a
            # refusal, never a write into a stranger's composer.
            try:
                live = client.pane_control_identity(
                    pane_id=binding.pane_id, deadline_monotonic=deadline
                )
            except subprocess.TimeoutExpired as exc:
                return _result(
                    operation_id,
                    REFUSED,
                    REASON_WRITE_DEADLINE,
                    f"the pre-write identity read exceeded its bound before any "
                    f"byte: {exc}; nothing was typed",
                )
            if live is None or live.dead:
                return _result(
                    operation_id,
                    REFUSED,
                    REASON_PANE_DEAD,
                    f"pane {binding.pane_id} is gone or dead as of the write lease; "
                    "nothing was typed",
                )
            if live.window_id != binding.window_id or live.pane_pid != binding.pane_pid:
                return _result(
                    operation_id,
                    REFUSED,
                    REASON_IDENTITY_MISMATCH,
                    f"pane {binding.pane_id} now reports window {live.window_id!r} "
                    f"and root pid {live.pane_pid}, not the bound "
                    f"{binding.window_id!r} / {binding.pane_pid}; nothing was typed",
                )
            live_server_refusal = server_identity_refusal(
                bound=binding.server_socket_path,
                observed=live.server_socket_path,
            )
            if live_server_refusal is not None:
                return _result(
                    operation_id, REFUSED, live_server_refusal[0], live_server_refusal[1]
                )
            copy_mode_refusal = control_input_service._copy_mode_guard_refusal(
                client, binding, deadline_monotonic=deadline
            )
            if copy_mode_refusal is not None:
                return _result(operation_id, REFUSED, copy_mode_refusal[0], copy_mode_refusal[1])
            dispatch_key = (
                control_input_service._native_kimi_dispatch_key(resolved, binding)
                if resolved.provider == "kimi_cli"
                else None
            )
            if dispatch_key is not None and control_input_service._native_kimi_dispatch_is_guarded(
                dispatch_key
            ):
                return _result(
                    operation_id,
                    REFUSED,
                    REASON_PANE_BUSY,
                    "a preceding Enter was sent to this exact Kimi pane generation "
                    "inside its dispatch grace; the ready-looking frame may be "
                    "stale, so nothing was typed",
                )
            # The readiness/idle gate, exactly as in the control path: IDLE
            # and COMPLETED both mean an input-ready composer; every other
            # observed state — and any observation failure — is a zero-byte
            # refusal.
            from cli_agent_orchestrator.services import managed_launch_v2
            from cli_agent_orchestrator.utils.terminal import managed_window_name

            try:
                turn_status = managed_launch_v2._observe_turn_state(
                    cast(str, resolved.provider),
                    pane_id=binding.pane_id,
                    terminal_id=terminal_id,
                    session_name=resolved.session_name,
                    window_name=managed_window_name(
                        terminal_id, cast(str, resolved.terminal_generation)
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - "could not look" is not idle
                return _result(
                    operation_id,
                    REFUSED,
                    REASON_PANE_BUSY,
                    f"the receiver's turn state could not be observed, so nothing "
                    f"was typed: {exc}",
                )
            if turn_status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
                return _result(
                    operation_id,
                    REFUSED,
                    REASON_PANE_BUSY,
                    f"the receiver is {turn_status.value}, not idle; nothing was typed",
                )
            adapter, plan, refusal = control_input_service._native_composer_preflight(
                resolved, binding, text=substituted, deadline_monotonic=deadline
            )
            if refusal is not None:
                reason, detail = refusal
                if reason == REASON_PROVIDER_UNSUPPORTED and "\n" in substituted:
                    reason = REASON_MULTILINE_UNPROVEN
                return _result(operation_id, REFUSED, reason, detail)
            assert adapter is not None and plan is not None

            def pre_write() -> Optional[Tuple[str, str]]:
                """The last safe binding point (Lane C r1): the adapter runs
                this after the journal claim and every deliverability/idle
                gate, immediately before the transport write.  A refusal
                here is journaled with zero bytes typed and leaves a ready
                image retrievable by a fresh operation; after it succeeds,
                any later ambiguity keeps the binding — the honest post-claim
                state, never a rollback that could race the write."""
                try:
                    image_attachments.bind_for_submit(terminal_id, operation_id, attachment_ids)
                except image_attachments.AttachmentBindingError as exc:
                    reason = (
                        adapter.REFUSED_IMAGE_UNKNOWN
                        if exc.reason_code == REASON_ATTACHMENT_UNKNOWN
                        else adapter.REFUSED_IMAGE_NOT_READY
                    )
                    return reason, exc.detail
                return None

            observation = adapter.turn_observation(
                active_turn_id=None,
                observed_at=_utc_isots(),
                observer="operator_message_service",
            )
            transport = control_input_service._NativeComposerTransport(
                client,
                binding.pane_id,
                binding.server_socket_path,
                deadline_monotonic=deadline,
            )
            try:
                record = adapter.operator_message(
                    operation_id=operation_id,
                    native_session_id=resolved.native_session_id,
                    terminal_id=terminal_id,
                    generation=resolved.terminal_generation,
                    execution_mode=resolved.execution_mode,
                    text=substituted,
                    payload_sha256=digest,
                    observation=observation,
                    transport=transport,
                    provider_version=resolved.provider_version,
                    pre_write=pre_write,
                )
            except adapter.NativeControlConflict as exc:
                return _result(operation_id, REFUSED, REASON_REQUEST_REBOUND, str(exc))
            except adapter.NativeControlInvalid as exc:
                # Unreachable by construction: this service screens text and
                # shape more strictly than the adapter does.  Surfaced as a
                # shape error (422) rather than a 500, and logged so the
                # disagreement is found.
                logger.error("operator-message adapter screening disagreement: %s", exc)
                raise OperatorMessageRequestInvalid(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - uncertainty, not failure
                # The store's own failure mode: whether anything was typed is
                # unknowable from here, so freeze the record if it exists and
                # answer ambiguously — never silently classify.
                try:
                    adapter.mark_ambiguous(
                        operation_id=operation_id,
                        reason=f"the operation store or transport raised mid-submit: {exc}",
                    )
                except Exception:  # noqa: BLE001 - best effort freeze
                    pass
                logger.error(
                    "operator-message %s raised mid-submit for %s: %s",
                    operation_id,
                    terminal_id,
                    exc,
                )
                return _result(
                    operation_id,
                    AMBIGUOUS,
                    REASON_RESPONSE_LOST,
                    f"the submit raised before its outcome was journaled: {exc}; "
                    "reconcile by exact operation id — the message is never "
                    "re-sent automatically",
                )
            if dispatch_key is not None and record.get("state") in (
                "posted",
                "accepted",
                "completed",
            ):
                control_input_service._mark_native_kimi_dispatch(dispatch_key)
            return _outcome_for_record(operation_id, record, replayed=False, multiline="\n" in text)
    except (generation_fence.FencedError, task_handoff.TaskHandoffHeld) as exc:
        return _result(
            operation_id,
            REFUSED,
            control_input_service._admission_refusal_reason(exc),
            str(exc),
        )
    except PaneBusyError as exc:
        return _result(
            operation_id,
            REFUSED,
            REASON_PANE_BUSY,
            f"another writer holds pane {resolved.pane_id}: {exc}; nothing was typed",
        )


# --- Reconciliation ------------------------------------------------------------


def reconcile_operator_message(operation_id: Any) -> OperatorMessageResult:
    """The exact-id answer for a caller whose response was lost.

    The journaled record is the answer; nothing is ever re-sent.  No
    record on a readable store proves nothing was typed (the intent is
    committed before the first byte), so the message may be sent again;
    a store that cannot be read is the unknown it truthfully is.
    """
    operation_id = _require_operation_id(operation_id)
    record, errors = _find_operation(operation_id)
    if record is None:
        if errors:
            return _result(
                operation_id,
                AMBIGUOUS,
                REASON_RESPONSE_LOST,
                f"the operation store could not be read ({'; '.join(errors)}), so "
                "whether a record exists is unknown; do not resend — try the "
                "reconcile again",
            )
        return _result(
            operation_id,
            REFUSED,
            REASON_OWNER_LOST_BEFORE_WRITE,
            "no operator-message record exists for this id on this server. The "
            "intent is committed before the first byte, so this proves nothing "
            "was typed and the message may be sent again",
        )
    return _outcome_for_record(
        operation_id,
        record,
        replayed=False,
        multiline=_record_multiline_hint(record),
    )


def _record_multiline_hint(record: Dict[str, Any]) -> bool:
    """Whether the journaled plan carried a multi-line encoding."""
    plan = (record.get("intent") or {}).get("keystroke_plan") or {}
    return plan.get("encoding") == "soft-newline-lines-then-enter"
