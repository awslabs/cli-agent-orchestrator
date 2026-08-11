"""Immutable, append-only restore contract for exact same-native-session resume
(cond-0378 B1).

B1 owns the durable *substrate* a later B2/B3 executor needs to decide whether
an exact resume of a stable agent's native lineage is supported — without
reconstructing mutable ambient state.  The record binds:

- the stable agent (``agent_id``) and its native lineage (``lineage_id``);
- the exact source incarnation, keyed by ``(terminal_id, generation)``;
- the no-secret relaunch facts: canonical working directory and trusted
  project root, harness/provider and bounded route provenance, the exact
  resolved executable identity/binary digest, profile/config material
  references and digests, observed/assigned model and effort, execution
  mode, and provider-home/profile-carrier facts — all as references and
  digests, never secret values.

Every fact that a legacy or currently unsupported launch path cannot
truthfully supply is recorded with an explicit typed state (``present`` /
``unavailable`` / ``missing``) — never fabricated or inferred.  A ``missing``
native session id is the truthful ``identity_missing`` lineage state.

Replay semantics are deterministic: the record is immutable after first
publication, keyed by the exact source incarnation.  Repeating the identical
contract adopts/returns the existing record; changed content for the same
source incarnation conflicts rather than overwrites; a new source incarnation
appends a new record.  The canonical payload and its sha256 digest are the
deterministic identity: the same contract always serializes to the same
bytes, so exact replay converges on the same row.

Every published contract is bound to the authoritative M3-A roster source:
``publish_contract`` resolves the exact roster incarnation and verifies the
contract's identity (agent, lineage, lineage harness, native session id
including truthful ``None``, bounded route provenance, and incarnation
execution mode) BEFORE the immutable slot is consumed, so a mismatched
contract can never occupy the source-incarnation slot.  The roster remains
the single authority for stable-agent/native-lineage facts; provider facts
not represented in M3-A (model, effort, executable digest, profile/provider-
home references, canonical paths) stay contract facts, not invented roster
checks.  Path-valued facts must be canonical real paths (no ``.``/``..`` or
symlink aliasing) so one directory has exactly one immutable digest.

The atomic dormant transition that retires the exact live source incarnation
and marks its stable agent dormant lives in ``stable_agent_roster`` (the
roster owns the incarnation/agent invariants); this module supplies the
immutable contract it validates against and preserves.  The read surface here
is deliberately small — enough for the transition, the tests, and B2/B3
composition — and never re-parses stored bytes as this binary's schema.

Storage: one additive ORM table (``restore_contracts``) created by
``clients/database.py`` (``create_all`` for fresh databases,
``_migrate_restore_contracts`` for existing ones).  All mutation runs inside
one SQLite transaction; no file or database lock is ever held across
provider, tmux, or network I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.exc import IntegrityError, OperationalError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import execution_mode as em

#: Versioned identity of the immutable restore-contract record itself.  A
#: reader that sees an unknown version in a stored record must degrade
#: truthfully — the read path returns stored bytes without re-validating
#: them against this binary's schema.
SCHEMA_VERSION = "cao-m3-restore-contract-v1"

#: Closed availability vocabulary for restore-contract facts.
FACT_PRESENT = "present"
FACT_UNAVAILABLE = "unavailable"
FACT_MISSING = "missing"
FACT_STATES = frozenset({FACT_PRESENT, FACT_UNAVAILABLE, FACT_MISSING})

#: Bounded reference/digest shapes for profile and provider-home facts.
_REFERENCE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:_(?:path|ref|version|sha256))$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

#: Closed route-provenance vocabulary, byte-compatible with the roster's own
#: bounded provenance so the two stores never disagree about what a route
#: fact means.
ROUTE_PROVENANCE_KEYS = frozenset(
    {"provider_route", "assigned_policy_sha256", "route_payload_sha256", "issuance_source"}
)
ROUTE_PROVENANCE_VALUE_MAX = 512

MAX_AGENT_ID_LEN = 64  # canonical lowercase UUID
MAX_TEXT_LEN = 512
MAX_PATH_LEN = 4096
MAX_MODEL_LEN = 256
MAX_REFERENCE_DICT_KEYS = 16
MAX_REFERENCE_DICT_BYTES = 4096


class RestoreContractError(RuntimeError):
    """Base error for restore-contract operations."""

    code = "restore-contract-error"


class RestoreContractInvalid(RestoreContractError):
    """A supplied contract value is malformed or violates the no-secret schema."""

    code = "restore-contract-invalid"


class RestoreContractConflict(RestoreContractError):
    """Changed content for the same source incarnation conflicts."""

    code = "restore-contract-conflict"


class RestoreContractNotFound(RestoreContractError):
    """No restore contract exists for the requested identity."""

    code = "restore-contract-not-found"


class RestoreContractUnavailable(RestoreContractError):
    """The restore-contract store could not be read or written."""

    code = "restore-contract-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_text(value: Any, *, field: str, max_len: int = MAX_TEXT_LEN) -> str:
    if not isinstance(value, str) or not value:
        raise RestoreContractInvalid(f"{field} must be a non-empty string; got {value!r}")
    if len(value) > max_len:
        raise RestoreContractInvalid(f"{field} must be at most {max_len} characters")
    return value


def _optional_text(value: Any, *, field: str, max_len: int = MAX_TEXT_LEN) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field=field, max_len=max_len)


def _require_canonical_realpath(value: Any, *, field: str) -> str:
    """A canonical absolute real path — no ``.``/``..`` or symlink aliasing.

    The exact canonical path is the immutable relaunch fact; a claimed path
    that is not already canonical is refused rather than silently normalized,
    because normalizing a claimed exact fact would let two spellings of one
    directory produce two different immutable digests.
    """
    path: str = _require_text(value, field=field, max_len=MAX_PATH_LEN)
    if not os.path.isabs(path):
        raise RestoreContractInvalid(f"{field} must be an absolute path; got {path!r}")
    canonical = os.path.realpath(path)
    if canonical != path:
        raise RestoreContractInvalid(
            f"{field} must be a canonical real path with no '.', '..', or symlink "
            f"aliasing; got {path!r} (canonical form: {canonical!r})"
        )
    return path


def _optional_canonical_realpath(value: Any, *, field: str) -> Optional[str]:
    if value is None:
        return None
    return _require_canonical_realpath(value, field=field)


def _require_uuid(value: Any, *, field: str) -> str:
    text: str = _require_text(value, field=field, max_len=MAX_AGENT_ID_LEN)
    try:
        if str(uuid.UUID(text)) != text:
            raise ValueError
    except ValueError as exc:
        raise RestoreContractInvalid(
            f"{field} must be a canonical lowercase UUID; got {text!r}"
        ) from exc
    return text


def _validate_route_provenance(value: Any) -> Optional[dict[str, str]]:
    """Bound and closed route provenance: only the known keys, bounded values."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RestoreContractInvalid(f"route_provenance must be a mapping; got {value!r}")
    unknown = sorted(set(value) - ROUTE_PROVENANCE_KEYS)
    if unknown:
        raise RestoreContractInvalid(
            f"route_provenance carries unknown key(s) {unknown}; the vocabulary is closed to "
            f"{sorted(ROUTE_PROVENANCE_KEYS)}"
        )
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str) or not item:
            raise RestoreContractInvalid(f"route_provenance.{key} must be a non-empty string")
        if len(item) > ROUTE_PROVENANCE_VALUE_MAX:
            raise RestoreContractInvalid(
                f"route_provenance.{key} must be at most {ROUTE_PROVENANCE_VALUE_MAX} characters"
            )
        if key.endswith("_sha256") and _SHA256_RE.fullmatch(item) is None:
            raise RestoreContractInvalid(
                f"route_provenance.{key} must be 64 lowercase hex characters"
            )
        result[key] = item
    return result


def _validate_reference_dict(value: Any, *, field: str) -> Optional[dict[str, str]]:
    """References-and-digests-only profile/provider-home facts.

    Keys are a closed shape — a lowercase stem ending in ``_path``, ``_ref``,
    ``_version``, or ``_sha256`` — and every digest slot must hold exactly 64
    lowercase hex characters.  This is cooperative schema discipline: the
    schema is shaped so profile/provider-home facts are references and digests,
    and an obviously mislabeled value (a non-digest in a digest slot, a
    content-bearing key) is refused.  It is not a secret scanner — a caller
    that insists on writing a secret VALUE into a reference slot is a bug that
    no key-shape rule can catch.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RestoreContractInvalid(
            f"{field} must be a mapping of references/digests; got {value!r}"
        )
    if len(value) > MAX_REFERENCE_DICT_KEYS:
        raise RestoreContractInvalid(
            f"{field} may carry at most {MAX_REFERENCE_DICT_KEYS} reference entries"
        )
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _REFERENCE_KEY_RE.fullmatch(key) is None:
            raise RestoreContractInvalid(
                f"{field} key {key!r} must be a lowercase reference name ending in "
                "_path/_ref/_version/_sha256 (references and digests only, never values)"
            )
        if not isinstance(item, str) or not item or len(item) > MAX_TEXT_LEN:
            raise RestoreContractInvalid(
                f"{field}.{key} must be a non-empty string of at most {MAX_TEXT_LEN} characters"
            )
        if key.endswith("_sha256") and _SHA256_RE.fullmatch(item) is None:
            raise RestoreContractInvalid(f"{field}.{key} must be 64 lowercase hex characters")
        if key.endswith("_path"):
            _require_canonical_realpath(item, field=f"{field}.{key}")
        result[key] = item
    if len(json.dumps(result, sort_keys=True, separators=(",", ":"))) > MAX_REFERENCE_DICT_BYTES:
        raise RestoreContractInvalid(
            f"{field} must serialize to at most {MAX_REFERENCE_DICT_BYTES} bytes"
        )
    return result


def _validate_executable(value: Any, *, field: str) -> Optional[dict[str, str]]:
    """The exact resolved executable identity: path + binary digest (+ version)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RestoreContractInvalid(f"{field} value must be a mapping; got {value!r}")
    path = _require_canonical_realpath(value.get("path"), field=f"{field}.path")
    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise RestoreContractInvalid(f"{field}.sha256 must be 64 lowercase hex characters")
    result: dict[str, str] = {"path": path, "sha256": sha256}
    version = value.get("version")
    if version is not None:
        result["version"] = _require_text(version, field=f"{field}.version", max_len=128)
    return result


def _validate_model_effort(value: Any, *, field: str) -> str:
    return _require_text(value, field=field, max_len=MAX_MODEL_LEN)


def _validated_fact(fact: "ContractFact", *, field: str, validator: Callable) -> "ContractFact":
    """Normalize a present fact's value through its structural validator."""
    if fact.state != FACT_PRESENT:
        return fact
    return ContractFact.present(validator(fact.value, field=field))


@dataclass(frozen=True)
class ContractFact:
    """One restore-contract fact with a closed availability state.

    ``present`` carries the value; ``unavailable`` names why a launch path
    could not truthfully supply the fact; ``missing`` marks a fact absent by
    definition (e.g. no native session id on an ``identity_missing``
    lineage).  The state travels into the canonical serialization, so an
    identical unavailability replays to the identical digest.
    """

    state: str
    value: Optional[Any] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.state not in FACT_STATES:
            raise RestoreContractInvalid(
                f"fact state must be one of {sorted(FACT_STATES)}; got {self.state!r}"
            )
        if self.state == FACT_PRESENT:
            if self.value is None:
                raise RestoreContractInvalid("a present fact must carry a value")
            if self.reason is not None:
                raise RestoreContractInvalid("a present fact carries no reason")
            return
        if self.value is not None:
            raise RestoreContractInvalid(f"a {self.state} fact carries no value")
        if self.state == FACT_UNAVAILABLE:
            if not isinstance(self.reason, str) or not self.reason:
                raise RestoreContractInvalid("an unavailable fact must name why it is unavailable")
            if len(self.reason) > MAX_TEXT_LEN:
                raise RestoreContractInvalid("an unavailable fact's reason is too long")
        elif self.reason is not None:
            raise RestoreContractInvalid("a missing fact carries no reason")

    @classmethod
    def present(cls, value: Any) -> "ContractFact":
        return cls(FACT_PRESENT, value)

    @classmethod
    def unavailable(cls, reason: str) -> "ContractFact":
        return cls(FACT_UNAVAILABLE, None, reason)

    @classmethod
    def missing(cls) -> "ContractFact":
        return cls(FACT_MISSING)

    def to_json(self) -> dict[str, Any]:
        """Deterministic serialized form (nulls included for exact replay)."""
        return {"state": self.state, "value": self.value, "reason": self.reason}


@dataclass(frozen=True)
class RestoreContract:
    """The immutable no-secret relaunch facts of one exact source incarnation.

    ``agent_id``, ``lineage_id``, and ``(terminal_id, generation)`` are the
    durable identities the record binds to; every other field is a
    reference/digest or a typed unavailable/missing state — never a secret
    value, and never a fabricated identity.
    """

    agent_id: str
    lineage_id: str
    terminal_id: str
    harness: str
    model: ContractFact
    effort: ContractFact
    working_directory: str
    executable: ContractFact
    profile_material: ContractFact
    provider_home_facts: ContractFact
    generation: Optional[str] = None
    #: None is the truthful ``identity_missing`` lineage state, never a
    #: fabricated id.
    native_session_id: Optional[str] = None
    provider: Optional[str] = None
    route_provenance: Optional[dict[str, str]] = None
    execution_mode: Optional[str] = None
    trusted_project_root: Optional[str] = None
    #: Kept last so the constructor stays keyword-friendly; it is part of the
    #: canonical payload and digest.
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RestoreContractInvalid(
                f"schema_version must be {SCHEMA_VERSION!r}; got {self.schema_version!r}"
            )
        object.__setattr__(self, "agent_id", _require_uuid(self.agent_id, field="agent_id"))
        object.__setattr__(self, "lineage_id", _require_uuid(self.lineage_id, field="lineage_id"))
        object.__setattr__(
            self, "terminal_id", _require_text(self.terminal_id, field="terminal_id", max_len=64)
        )
        object.__setattr__(
            self, "generation", _optional_text(self.generation, field="generation", max_len=128)
        )
        object.__setattr__(
            self,
            "native_session_id",
            _optional_text(self.native_session_id, field="native_session_id", max_len=512),
        )
        object.__setattr__(self, "harness", _require_text(self.harness, field="harness"))
        object.__setattr__(
            self, "provider", _optional_text(self.provider, field="provider", max_len=128)
        )
        object.__setattr__(
            self, "route_provenance", _validate_route_provenance(self.route_provenance)
        )
        if self.execution_mode is not None:
            object.__setattr__(
                self,
                "execution_mode",
                em.validate_mode(self.execution_mode, field="execution_mode"),
            )
        object.__setattr__(
            self,
            "working_directory",
            _require_canonical_realpath(self.working_directory, field="working_directory"),
        )
        object.__setattr__(
            self,
            "trusted_project_root",
            _optional_canonical_realpath(self.trusted_project_root, field="trusted_project_root"),
        )
        object.__setattr__(
            self,
            "model",
            _validated_fact(self.model, field="model", validator=_validate_model_effort),
        )
        object.__setattr__(
            self,
            "effort",
            _validated_fact(self.effort, field="effort", validator=_validate_model_effort),
        )
        object.__setattr__(
            self,
            "executable",
            _validated_fact(self.executable, field="executable", validator=_validate_executable),
        )
        object.__setattr__(
            self,
            "profile_material",
            _validated_fact(
                self.profile_material, field="profile_material", validator=_validate_reference_dict
            ),
        )
        object.__setattr__(
            self,
            "provider_home_facts",
            _validated_fact(
                self.provider_home_facts,
                field="provider_home_facts",
                validator=_validate_reference_dict,
            ),
        )

    def digest(self) -> str:
        """The deterministic sha256 over the canonical payload."""
        return contract_digest(self)


def _payload_dict(contract: RestoreContract) -> dict[str, Any]:
    return {
        "schema_version": contract.schema_version,
        "agent_id": contract.agent_id,
        "lineage_id": contract.lineage_id,
        "terminal_id": contract.terminal_id,
        "generation": contract.generation,
        "native_session_id": contract.native_session_id,
        "harness": contract.harness,
        "provider": contract.provider,
        "route_provenance": contract.route_provenance,
        "execution_mode": contract.execution_mode,
        "model": contract.model.to_json(),
        "effort": contract.effort.to_json(),
        "working_directory": contract.working_directory,
        "trusted_project_root": contract.trusted_project_root,
        "executable": contract.executable.to_json(),
        "profile_material": contract.profile_material.to_json(),
        "provider_home_facts": contract.provider_home_facts.to_json(),
    }


def canonical_payload(contract: RestoreContract) -> str:
    """The deterministic canonical serialization (sorted keys, compact JSON)."""
    return json.dumps(_payload_dict(contract), sort_keys=True, separators=(",", ":"))


def _canonical_json(value: Any) -> str:
    """Compact canonical JSON for byte-comparable mappings.

    Byte-compatible with the roster's own ``_canonical_json`` so a stored
    lineage ``route_provenance_json`` compares equal to the contract's
    canonical route provenance.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def contract_digest(contract: RestoreContract) -> str:
    """The sha256 of the canonical payload — the deterministic identity."""
    return hashlib.sha256(canonical_payload(contract).encode("utf-8")).hexdigest()


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _contract_row_dict(row: Any) -> dict[str, Any]:
    return {
        "contract_id": row.contract_id,
        "contract_digest": row.contract_digest,
        "schema_version": row.schema_version,
        "agent_id": row.agent_id,
        "lineage_id": row.lineage_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "native_session_id": row.native_session_id,
        "contract": _parse_json(row.contract_json),
        "created_at": row.created_at,
    }


def _contract_by_exact(db: Any, terminal_id: str, generation: Optional[str] = None) -> Any:
    """The one restore-contract row for exactly one source incarnation.

    A row whose generation is NULL (legacy launch) is matched only by a
    NULL-generation lookup, mirroring the roster incarnation identity.
    """
    query = db.query(database.RestoreContractModel).filter(
        database.RestoreContractModel.terminal_id == terminal_id
    )
    if generation is None:
        query = query.filter(database.RestoreContractModel.generation.is_(None))
    else:
        query = query.filter(database.RestoreContractModel.generation == generation)
    return query.one_or_none()


def _source_incarnation_by_exact(
    db: Any, terminal_id: str, generation: Optional[str] = None
) -> Any:
    """The authoritative roster incarnation for exactly one source identity.

    Publication and the dormant transition bind the immutable contract to the
    exact M3-A roster source — never to a terminal/generation uniqueness slot
    with detached identities.  A NULL generation matches the legacy
    NULL-generation incarnation only.
    """
    query = db.query(database.StableAgentIncarnationModel).filter(
        database.StableAgentIncarnationModel.terminal_id == terminal_id
    )
    if generation is None:
        query = query.filter(database.StableAgentIncarnationModel.generation.is_(None))
    else:
        query = query.filter(database.StableAgentIncarnationModel.generation == generation)
    return query.one_or_none()


def identity_mismatch_reason(
    payload: dict[str, Any], incarnation: Any, lineage: Any
) -> Optional[str]:
    """Why a contract payload does not bind to the authoritative M3-A rows.

    ``payload`` is the canonical contract payload dict, ``incarnation`` and
    ``lineage`` are the authoritative roster rows the contract claims to
    describe.  Returns a human reason when the contract does not match the
    exact roster identity — agent, lineage, lineage harness, native session id
    (including truthful ``None``), bounded route provenance, and incarnation
    execution mode — else ``None``.  ``terminal_id``/``generation`` are
    verified by the exact source-incarnation lookup itself, not restated here.

    Provider facts not represented in M3-A (model, effort, executable digest,
    profile/provider-home references, canonical paths) remain contract facts
    and are deliberately not invented roster checks.
    """
    if payload["agent_id"] != incarnation.agent_id:
        return (
            f"restore contract records stable agent {payload['agent_id']!r} but the source "
            f"incarnation belongs to {incarnation.agent_id!r}; a contract is bound to the "
            "exact roster identity, never a random or detached one"
        )
    if payload["lineage_id"] != incarnation.lineage_id:
        return (
            f"restore contract records lineage {payload['lineage_id']!r} but the source "
            f"incarnation is bound to {incarnation.lineage_id!r}; a contract is bound to the "
            "exact roster lineage"
        )
    if payload["harness"] != lineage.harness:
        return (
            f"restore contract records harness {payload['harness']!r} but the source lineage "
            f"belongs to {lineage.harness!r}; native ids never cross harness domains"
        )
    if payload["native_session_id"] != lineage.native_session_id:
        return (
            f"restore contract records native session id {payload['native_session_id']!r} but "
            f"lineage {lineage.lineage_id} is bound to {lineage.native_session_id!r}; the "
            "provider-native session lineage is immutable and never misclaimed"
        )
    route_value = payload["route_provenance"]
    if route_value is not None:
        try:
            contract_route = _canonical_json(route_value)
        except (TypeError, ValueError):
            return (
                f"restore contract route provenance {route_value!r} is not canonical-JSON "
                "serializable; the stored contract is malformed"
            )
    else:
        contract_route = None
    if contract_route != lineage.route_provenance_json:
        return (
            f"restore contract route provenance {payload['route_provenance']!r} does not match "
            f"the source lineage's recorded provenance {lineage.route_provenance_json!r}"
        )
    if payload["execution_mode"] != incarnation.execution_mode:
        return (
            f"restore contract execution mode {payload['execution_mode']!r} does not match the "
            f"source incarnation's {incarnation.execution_mode!r}; modes are separate launch "
            "branches and never cross"
        )
    return None


def stored_payload_mismatch(payload: Any, incarnation: Any, lineage: Any) -> Optional[str]:
    """Total revalidation of a STORED contract payload against authoritative rows.

    ``payload`` is the raw stored payload (the parsed ``contract_json`` from the
    read surface — possibly ``None`` for invalid JSON, a non-mapping, or a
    mapping missing identity keys).  Returns a refusal reason for EVERY
    malformed/unknown shape so the transition can never mutate on a stored
    contract it cannot fully verify, and never raises a raw ``KeyError`` or
    ``TypeError``.  Specifically:

    - non-mapping / invalid JSON (parsed to ``None``) is refused;
    - an unknown ``schema_version`` is refused for this binary's transition
      (the read surface stays lenient — unknown-schema records remain readable);
    - a missing required identity key is refused;
    - then the well-shaped payload is compared against the authoritative rows by
      :func:`identity_mismatch_reason`.

    This is bounded partial-write/schema-drift safety at the effect boundary,
    not a general corruption framework or hostile tamper protection.
    """
    if not isinstance(payload, dict):
        return (
            "stored restore contract payload is not a mapping (invalid JSON or "
            "non-object shape); it cannot authorize a dormant transition"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        return (
            f"stored restore contract schema_version {payload.get('schema_version')!r} is not "
            f"this binary's {SCHEMA_VERSION!r}; an unknown-schema contract cannot authorize "
            "this binary's dormant transition"
        )
    for key in (
        "agent_id",
        "lineage_id",
        "harness",
        "native_session_id",
        "route_provenance",
        "execution_mode",
    ):
        if key not in payload:
            return (
                f"stored restore contract payload is missing required identity key {key!r}; "
                "it cannot authorize a dormant transition"
            )
    return identity_mismatch_reason(payload, incarnation, lineage)


def publish_contract(contract: RestoreContract, db: Any = None) -> dict[str, Any]:
    """Create or adopt the immutable restore contract for one source incarnation.

    The contract is bound to the authoritative M3-A roster source: the exact
    source incarnation is resolved and the contract's identity (agent, lineage,
    lineage harness, native session id including truthful ``None``, bounded
    route provenance, incarnation execution mode) is verified against it BEFORE
    the immutable slot is consumed.  A mismatched contract refuses with zero
    mutation — it can never occupy the source-incarnation slot or consume
    history.

    Identical content for the same source incarnation adopts/returns the
    existing record.  Changed content for the same source incarnation
    conflicts rather than overwrites.  A new source incarnation appends a
    new record.

    ``db`` — when supplied, the write runs directly in the caller's transaction
    (no savepoint), so the caller's commit/rollback is the atomic boundary and
    a caller rollback truthfully leaves no contract.  When omitted, a
    standalone transaction is opened and committed, and a concurrent duplicate
    is adopted on retry.
    """
    if not isinstance(contract, RestoreContract):
        raise RestoreContractInvalid(f"contract must be a RestoreContract; got {contract!r}")
    payload_dict = _payload_dict(contract)
    payload = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _publish(session: Any) -> dict[str, Any]:
        # Resolve the authoritative source incarnation and refuse a contract
        # that cannot bind to it — zero rows written, zero slot consumed.
        incarnation = _source_incarnation_by_exact(
            session, contract.terminal_id, contract.generation
        )
        if incarnation is None:
            raise RestoreContractConflict(
                f"no roster incarnation exists for source {contract.terminal_id}/"
                f"{contract.generation}; a restore contract must bind to an "
                "authoritative M3-A source, never a detached identity"
            )
        lineage = (
            session.query(database.StableAgentLineageModel)
            .filter(database.StableAgentLineageModel.lineage_id == incarnation.lineage_id)
            .one_or_none()
        )
        if lineage is None:
            raise RestoreContractConflict(
                f"source incarnation {incarnation.incarnation_id} has no bound lineage; "
                "an identity-pending source cannot carry a restore contract"
            )
        mismatch = identity_mismatch_reason(payload_dict, incarnation, lineage)
        if mismatch is not None:
            raise RestoreContractConflict(mismatch)

        existing = _contract_by_exact(session, contract.terminal_id, contract.generation)
        if existing is not None:
            if existing.contract_json == payload:
                record = _contract_row_dict(existing)
                record["adopted"] = True
                return record
            raise RestoreContractConflict(
                f"restore contract for source incarnation {contract.terminal_id}/"
                f"{contract.generation} already exists with different content; changed "
                "content conflicts rather than overwriting"
            )
        stamp = _now()
        row = database.RestoreContractModel(
            contract_id=str(uuid.uuid4()),
            contract_digest=digest,
            schema_version=contract.schema_version,
            agent_id=contract.agent_id,
            lineage_id=contract.lineage_id,
            terminal_id=contract.terminal_id,
            generation=contract.generation,
            native_session_id=contract.native_session_id,
            contract_json=payload,
            created_at=stamp,
        )
        session.add(row)
        session.flush()
        record = _contract_row_dict(row)
        record["adopted"] = False
        return record

    if db is not None:
        # The caller owns this session's transaction: the write runs directly
        # in it (NO savepoint), so the caller's commit/rollback is the atomic
        # boundary and a rollback truthfully leaves no contract.  A lost
        # unique-slot race is a typed refusal the caller retries; the winner's
        # row is adopted/conflicted against on that retry.
        try:
            return _publish(db)
        except IntegrityError as exc:
            raise RestoreContractUnavailable(
                f"concurrent restore-contract write refused; retry to adopt: {exc}"
            ) from exc

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = _publish(session)
                session.commit()
                return result
        except IntegrityError as exc:
            # A concurrent writer won the source-incarnation slot.  Retry in a
            # fresh session so the next pass adopts/conflicts against the
            # winner's row.
            last_error = exc
            time.sleep(0.05)
        except OperationalError as exc:
            # SQLite read->write upgrade contention; roll back and retry.
            last_error = exc
            time.sleep(0.05)
    raise RestoreContractUnavailable(
        f"concurrent restore-contract writes kept conflicting; refusing after retry: {last_error}"
    )


# ---------------------------------------------------------------------------
# read / audit surface (deliberately small)
# ---------------------------------------------------------------------------


def get_contract_by_incarnation(
    terminal_id: str, generation: Optional[str] = None, db: Any = None
) -> Optional[dict[str, Any]]:
    """The immutable restore contract for one exact source incarnation, or None.

    A legacy row (no restore contract) truthfully reads as None; stored bytes
    are returned without re-validating them against this binary's schema, so
    an unknown future schema version stays readable.
    """

    def _get(session: Any) -> Optional[dict[str, Any]]:
        row = _contract_by_exact(session, terminal_id, generation)
        return _contract_row_dict(row) if row is not None else None

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def get_contract(contract_id: str, db: Any = None) -> dict[str, Any]:
    """One immutable restore contract by id."""

    def _get(session: Any) -> dict[str, Any]:
        row = (
            session.query(database.RestoreContractModel)
            .filter(database.RestoreContractModel.contract_id == contract_id)
            .one_or_none()
        )
        if row is None:
            raise RestoreContractNotFound(f"unknown restore contract: {contract_id}")
        return _contract_row_dict(row)

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def list_contracts(
    agent_id: Optional[str] = None, lineage_id: Optional[str] = None, db: Any = None
) -> list[dict[str, Any]]:
    """Append-only restore-contract history, oldest first; optionally scoped."""

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.RestoreContractModel)
        if agent_id is not None:
            query = query.filter(database.RestoreContractModel.agent_id == agent_id)
        if lineage_id is not None:
            query = query.filter(database.RestoreContractModel.lineage_id == lineage_id)
        rows = query.order_by(
            database.RestoreContractModel.created_at,
            database.RestoreContractModel.contract_id,
        ).all()
        return [_contract_row_dict(row) for row in rows]

    if db is not None:
        return _list(db)
    with database.SessionLocal() as session:
        return _list(session)
