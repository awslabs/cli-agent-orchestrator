"""Provider-native receipt facts for the native-session ledger.

The fork owns provider-native *facts*; the conductor owns the SQLite
native-session ledger those facts are appended to.  This module builds
the exact canonical bytes for every receipt-payload kind, the kind-
independent receipt hash object, and the route-segment hash object, so
the conductor-side writer can insert payload bytes whose
``payload_sha256`` it can re-verify, and every reader re-derives the
identical chain.

Invariant: all hash domains are canonical-JSON objects (the
``canonical_json`` encoding rules) with a leading ``domain`` field for
domain separation and a single trailing newline; integers are
non-negative base-10; ``null`` marks an absent optional.

Failure mode prevented: a payload hashed from ad-hoc or reordered bytes
produces a digest the ledger cannot reproduce, which either wedges
appends (prev-hash mismatch) or, worse, lets two different facts share
one identity.

Why this guard exists: the ledger's append-only hash chain, freeze-on-
tamper behavior, and cross-repository verification all assume these
bytes are derived by one deterministic algorithm, pinned here by the
design's golden vectors.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from cli_agent_orchestrator.services import provider_contracts
from cli_agent_orchestrator.services.canonical_json import (
    CanonicalEncodingError,
    build_canonical,
    encode_canonical,
)

RECEIPT_DOMAIN = "cao-receipt-v1"
SEGMENT_DOMAIN = "cao-route-segment-v1"

PROVIDERS = ("codex", "claude", "kimi")
ISSUANCE_SOURCES = (
    "app_server_thread_start",
    "acp_session_new",
    "cli_session_id",
    "trusted_hook",
)
RECEIPT_KINDS = (
    "creation",
    "binding",
    "rebind",
    "resume_attempt",
    "resume_success",
    "resume_refusal",
    "route",
    "closure",
)
AUTHORITY_STATUSES = ("observed", "unobserved", "drifted", "degraded")
CLOSURE_KINDS = ("exit", "teardown", "no-survivor")


class ReceiptFactError(ValueError):
    """A receipt fact violates its fixed schema."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReceiptFactError(message)


def _require_hex(value: str, length: int, field: str) -> None:
    _require(
        isinstance(value, str)
        and len(value) == length
        and all(ch in "0123456789abcdef" for ch in value),
        f"{field} must be {length} lowercase hex characters",
    )


def _require_str(value: Any, field: str) -> None:
    _require(isinstance(value, str) and bool(value), f"{field} must be a non-empty string")


def _payload_bytes(kind: str, fields: list[tuple[str, Any]]) -> bytes:
    """Encode one receipt payload under its domain-separated schema."""
    _require(kind in RECEIPT_KINDS, f"unknown receipt kind: {kind!r}")
    obj = build_canonical([("domain", f"cao-receipt-payload-{kind}-v1"), *fields])
    return encode_canonical(obj)


def payload_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def creation_payload(
    *,
    provider: str,
    native_id: str,
    provider_version: str,
    issuance_source: str,
    obligation_generation: str,
    task_id: Optional[str],
    run_id: str,
    created_at: str,
) -> bytes:
    """Immutable identity payload, one per provider-native session id."""
    _require(provider in PROVIDERS, f"unknown provider: {provider!r}")
    _require_str(native_id, "native_id")
    _require_str(provider_version, "provider_version")
    _require(
        issuance_source in ISSUANCE_SOURCES,
        f"issuance_source must be one of {ISSUANCE_SOURCES}",
    )
    _require_str(obligation_generation, "obligation_generation")
    _require(task_id is None or isinstance(task_id, str), "task_id must be a string or null")
    _require_str(run_id, "run_id")
    _require_str(created_at, "created_at")
    return _payload_bytes(
        "creation",
        [
            ("provider", provider),
            ("native_id", native_id),
            ("provider_version", provider_version),
            ("issuance_source", issuance_source),
            ("obligation_generation", obligation_generation),
            ("task_id", task_id),
            ("run_id", run_id),
            ("created_at", created_at),
        ],
    )


def binding_payload(
    *,
    provider: str,
    native_id: str,
    launch_nonce_digest: str,
    provider_process_identity: str,
    tmux_incarnation: str,
    terminal_generation: str,
    worktree_realpath: str,
    repository: str,
    head: str,
    assigned_route_digest: str,
    bound_at: str,
    rebind: bool = False,
    supersedes_binding_seq: Optional[int] = None,
) -> bytes:
    """Binding (or rebind) of a native session to one terminal generation.

    A rebind is a distinct receipt kind: a resume appends new history and
    never rewrites the prior binding.
    """
    _require(provider in PROVIDERS, f"unknown provider: {provider!r}")
    _require_str(native_id, "native_id")
    _require_hex(launch_nonce_digest, 64, "launch_nonce_digest")
    _require_str(provider_process_identity, "provider_process_identity")
    _require_str(tmux_incarnation, "tmux_incarnation")
    _require_str(terminal_generation, "terminal_generation")
    _require_str(worktree_realpath, "worktree_realpath")
    _require_str(repository, "repository")
    _require_hex(head, 40, "head")
    _require_hex(assigned_route_digest, 64, "assigned_route_digest")
    _require_str(bound_at, "bound_at")
    fields: list[tuple[str, Any]] = [
        ("provider", provider),
        ("native_id", native_id),
        ("launch_nonce_digest", launch_nonce_digest),
        ("provider_process_identity", provider_process_identity),
        ("tmux_incarnation", tmux_incarnation),
        ("terminal_generation", terminal_generation),
        ("worktree_realpath", worktree_realpath),
        ("repository", repository),
        ("head", head),
        ("assigned_route_digest", assigned_route_digest),
    ]
    if rebind:
        _require(
            isinstance(supersedes_binding_seq, int) and supersedes_binding_seq >= 1,
            "a rebind must name the superseded binding sequence (integer >= 1)",
        )
        fields.append(("supersedes_binding_seq", supersedes_binding_seq))
    else:
        _require(
            supersedes_binding_seq is None,
            "supersedes_binding_seq is lawful only on a rebind payload",
        )
    fields.append(("bound_at", bound_at))
    return _payload_bytes("rebind" if rebind else "binding", fields)


def route_payload(
    *,
    provider: str,
    native_id: str,
    authority_status: str,
    assigned_model: str,
    assigned_effort: str,
    assigned_policy_sha256: str,
    assigned_profile_sha256: str,
    assigned_config_sha256: str,
    requested_model: Optional[str],
    requested_effort: Optional[str],
    observed_model: Optional[str],
    observed_effort: Optional[str],
    protocol_version: Optional[str],
    event_sequence: Optional[int],
    native_turn_id: Optional[str],
    attested_at: str,
) -> bytes:
    """Route-authority payload carrying the assigned policy digests.

    ``observed`` is lawful only from a proven model-input-bound non-echo
    provider receipt; with no proven provider the honest status is
    ``unobserved`` and the observed fields are all null.
    """
    _require(provider in PROVIDERS, f"unknown provider: {provider!r}")
    _require_str(native_id, "native_id")
    _require(
        authority_status in AUTHORITY_STATUSES,
        f"authority_status must be one of {AUTHORITY_STATUSES}",
    )
    _require_str(assigned_model, "assigned_model")
    _require_str(assigned_effort, "assigned_effort")
    _require_hex(assigned_policy_sha256, 64, "assigned_policy_sha256")
    _require_hex(assigned_profile_sha256, 64, "assigned_profile_sha256")
    _require_hex(assigned_config_sha256, 64, "assigned_config_sha256")
    observed_fields = (
        native_turn_id,
        observed_model,
        observed_effort,
        protocol_version,
        event_sequence,
    )
    present = sum(field is not None for field in observed_fields)
    if authority_status == "unobserved":
        _require(present == 0, "an unobserved route must carry no observed fields")
    elif authority_status == "observed":
        _require(present == 5, "an observed route requires all five observed fields")
        # Effort agreement goes through the one judge that knows the three
        # observability classes, never a local ``==``. A raw comparison
        # here would be a second rule answering a question that already
        # has an authority, and it would answer it worse: it cannot tell
        # "no effort surface" from "an effort that cannot be read before
        # the first turn", and it would read the provider-default sentinel
        # sitting in the observed slot as agreement when nothing was
        # observed at all.
        _require(
            observed_model == assigned_model
            and provider_contracts.effort_receipt_matches(
                assigned_effort,
                observed_effort,
                observability=provider_contracts.effort_observability_for_recovery_provider(
                    provider, assigned_model
                ),
            ),
            "an observed route requires observation == assignment",
        )
    elif authority_status == "drifted":
        _require(present == 5, "a drifted route requires all five observed fields")
        # Drift is the exact complement of agreement, so it must be decided
        # by the same judge and simply negated. A second rule here does not
        # merely duplicate the first, it disagrees with it: for a pair whose
        # effort is unobservable, a receipt echoing the assigned effort into
        # the observed slot is a claim nothing could have produced, which
        # the agreement rule refuses -- while a raw ``!=`` reads the equal
        # strings as agreement and refuses to call it drift. The tuple then
        # satisfies neither status and the drift cannot be recorded at all,
        # which is the one outcome a drift check exists to prevent.
        _require(
            observed_model != assigned_model
            or not provider_contracts.effort_receipt_matches(
                assigned_effort,
                observed_effort,
                observability=provider_contracts.effort_observability_for_recovery_provider(
                    provider, assigned_model
                ),
            ),
            "a drifted route requires a mismatch against the assignment",
        )
    else:  # degraded
        _require(
            0 < present < 5,
            "a degraded route is a partial receipt (some observed fields, not all)",
        )
    _require_str(attested_at, "attested_at")
    return _payload_bytes(
        "route",
        [
            ("provider", provider),
            ("native_id", native_id),
            ("authority_status", authority_status),
            ("assigned_model", assigned_model),
            ("assigned_effort", assigned_effort),
            ("assigned_policy_sha256", assigned_policy_sha256),
            ("assigned_profile_sha256", assigned_profile_sha256),
            ("assigned_config_sha256", assigned_config_sha256),
            ("requested_model", requested_model),
            ("requested_effort", requested_effort),
            ("observed_model", observed_model),
            ("observed_effort", observed_effort),
            ("protocol_version", protocol_version),
            ("event_sequence", event_sequence),
            ("native_turn_id", native_turn_id),
            ("attested_at", attested_at),
        ],
    )


def resume_attempt_payload(
    *,
    provider: str,
    native_id: str,
    resume_attempt_id: str,
    terminal_generation: str,
    extends_receipt_sha256: str,
    at: str,
) -> bytes:
    _require(provider in PROVIDERS, f"unknown provider: {provider!r}")
    _require_str(native_id, "native_id")
    _require_str(resume_attempt_id, "resume_attempt_id")
    _require_str(terminal_generation, "terminal_generation")
    _require_hex(extends_receipt_sha256, 64, "extends_receipt_sha256")
    _require_str(at, "at")
    return _payload_bytes(
        "resume_attempt",
        [
            ("provider", provider),
            ("native_id", native_id),
            ("resume_attempt_id", resume_attempt_id),
            ("terminal_generation", terminal_generation),
            ("extends_receipt_sha256", extends_receipt_sha256),
            ("at", at),
        ],
    )


def resume_success_payload(
    *,
    provider: str,
    native_id: str,
    resume_attempt_id: str,
    terminal_generation: str,
    native_verified_evidence_digest: str,
    at: str,
) -> bytes:
    _require(provider in PROVIDERS, f"unknown provider: {provider!r}")
    _require_str(native_id, "native_id")
    _require_str(resume_attempt_id, "resume_attempt_id")
    _require_str(terminal_generation, "terminal_generation")
    _require_hex(native_verified_evidence_digest, 64, "native_verified_evidence_digest")
    _require_str(at, "at")
    return _payload_bytes(
        "resume_success",
        [
            ("provider", provider),
            ("native_id", native_id),
            ("resume_attempt_id", resume_attempt_id),
            ("terminal_generation", terminal_generation),
            ("native_verified_evidence_digest", native_verified_evidence_digest),
            ("at", at),
        ],
    )


def resume_refusal_payload(
    *,
    provider: str,
    native_id: str,
    resume_attempt_id: str,
    refusal_code: int,
    reason: str,
    at: str,
) -> bytes:
    _require(provider in PROVIDERS, f"unknown provider: {provider!r}")
    _require_str(native_id, "native_id")
    _require_str(resume_attempt_id, "resume_attempt_id")
    _require(
        isinstance(refusal_code, int) and 40 <= refusal_code <= 45,
        "refusal_code must be an integer in 40..45 (the resume outcome contract)",
    )
    _require_str(reason, "reason")
    _require_str(at, "at")
    return _payload_bytes(
        "resume_refusal",
        [
            ("provider", provider),
            ("native_id", native_id),
            ("resume_attempt_id", resume_attempt_id),
            ("refusal_code", refusal_code),
            ("reason", reason),
            ("at", at),
        ],
    )


def closure_payload(
    *,
    provider: str,
    native_id: str,
    closure_kind: str,
    evidence_digest: str,
    at: str,
) -> bytes:
    _require(provider in PROVIDERS, f"unknown provider: {provider!r}")
    _require_str(native_id, "native_id")
    _require(closure_kind in CLOSURE_KINDS, f"closure_kind must be one of {CLOSURE_KINDS}")
    _require_hex(evidence_digest, 64, "evidence_digest")
    _require_str(at, "at")
    return _payload_bytes(
        "closure",
        [
            ("provider", provider),
            ("native_id", native_id),
            ("closure_kind", closure_kind),
            ("evidence_digest", evidence_digest),
            ("at", at),
        ],
    )


def receipt_hash_object(
    *,
    provider: str,
    native_id: str,
    chain_seq: int,
    kind: str,
    prev_receipt_sha256: Optional[str],
    payload_sha256: str,
    at: str,
) -> dict[str, Any]:
    """The kind-independent receipt hash object.

    ``chain_seq`` is 1-based and contiguous per ``(provider, native_id)``;
    ``prev_receipt_sha256`` is null exactly when ``chain_seq`` is 1.
    """
    _require(kind in RECEIPT_KINDS, f"unknown receipt kind: {kind!r}")
    _require(isinstance(chain_seq, int) and chain_seq >= 1, "chain_seq must be an integer >= 1")
    _require(
        (chain_seq == 1) == (prev_receipt_sha256 is None),
        "prev_receipt_sha256 is null exactly at the genesis receipt",
    )
    if prev_receipt_sha256 is not None:
        _require_hex(prev_receipt_sha256, 64, "prev_receipt_sha256")
    _require_hex(payload_sha256, 64, "payload_sha256")
    _require_str(at, "at")
    return build_canonical(
        [
            ("domain", RECEIPT_DOMAIN),
            ("provider", provider),
            ("native_id", native_id),
            ("chain_seq", chain_seq),
            ("kind", kind),
            ("prev_receipt_sha256", prev_receipt_sha256),
            ("payload_sha256", payload_sha256),
            ("at", at),
        ]
    )


def receipt_digest(**kwargs: Any) -> str:
    return hashlib.sha256(encode_canonical(receipt_hash_object(**kwargs))).hexdigest()


def segment_hash_object(
    *,
    provider: str,
    native_id: str,
    receipt_sha256: str,
    predecessor_segment_hash: Optional[str],
    native_turn_id: Optional[str],
    assigned_model: str,
    assigned_effort: str,
    assigned_policy_sha256: str,
    assigned_profile_sha256: str,
    assigned_config_sha256: str,
    requested_model: Optional[str],
    requested_effort: Optional[str],
    observed_model: Optional[str],
    observed_effort: Optional[str],
    protocol_version: Optional[str],
    event_sequence: Optional[int],
    authority_status: str,
) -> dict[str, Any]:
    """The route-segment hash object, bound to its backing route receipt."""
    _require(provider in PROVIDERS, f"unknown provider: {provider!r}")
    _require_str(native_id, "native_id")
    _require_hex(receipt_sha256, 64, "receipt_sha256")
    if predecessor_segment_hash is not None:
        _require_hex(predecessor_segment_hash, 64, "predecessor_segment_hash")
    _require(
        authority_status in AUTHORITY_STATUSES,
        f"authority_status must be one of {AUTHORITY_STATUSES}",
    )
    return build_canonical(
        [
            ("domain", SEGMENT_DOMAIN),
            ("provider", provider),
            ("native_id", native_id),
            ("receipt_sha256", receipt_sha256),
            ("predecessor_segment_hash", predecessor_segment_hash),
            ("native_turn_id", native_turn_id),
            ("assigned_model", assigned_model),
            ("assigned_effort", assigned_effort),
            ("assigned_policy_sha256", assigned_policy_sha256),
            ("assigned_profile_sha256", assigned_profile_sha256),
            ("assigned_config_sha256", assigned_config_sha256),
            ("requested_model", requested_model),
            ("requested_effort", requested_effort),
            ("observed_model", observed_model),
            ("observed_effort", observed_effort),
            ("protocol_version", protocol_version),
            ("event_sequence", event_sequence),
            ("authority_status", authority_status),
        ]
    )


def segment_digest(**kwargs: Any) -> str:
    return hashlib.sha256(encode_canonical(segment_hash_object(**kwargs))).hexdigest()
