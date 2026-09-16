"""Build, serialise and parse the frozen execution manifest (issue #583, unit ``manifest-envelope``).

One bounded, secret-redacted record of HOW a run was launched, persisted as a single
``workflow_run.manifest_json`` value (ADR-583-12). Every re-approval decision in Bolt 2 compares a
``plan_id`` read out of this envelope, and FR-9's guarantee that a resumed run is insulated from
later edits to durable memory rests on the memory record it carries.

LEAF MODULE: imports ``secret_gate``, ``constants``, ``dataclasses``, ``hashlib``, ``json`` and
``typing`` ONLY. No I/O, no state, no configuration, and NO LOGGING OF ANY KIND. Both are security
requirements rather than style preferences, and both are PRESERVATION requirements — the
pure-function shape already has them, and a later "improvement" is what would take them away:

* Nothing is persisted or emitted here beyond the returned envelope. The inputs include ``inputs``
  and ``permissions``, which is exactly where a credential shows up in practice (a token in a
  workflow input, a key in a permission grant).
* NO field value is echoed into a log line, a message, or an exception. The obvious diagnostic
  instinct ("log the document so we can see what was frozen") moves a credential out of a bounded,
  redacted column and into a log file, which is typically world-readable on the host and often
  shipped off it. This module therefore has no logger and no ``print``, matching
  ``secret_gate.py``'s, ``step_result.py``'s and ``step_fingerprint.py``'s posture.

TWO ORDERINGS ARE PART OF THE CONTRACT, and reversing either is a silent regression:

1. **REDACT BEFORE BOUNDING.** Inherited from ``step_result.build_envelope``, whose docstring calls
   this "not an implementation preference". Were bounding first, the redactor would only ever see the
   truncated prefix, so a secret STRADDLING the boundary would be persisted in the clear. The happy
   path produces an identical-looking envelope either way, which is what makes this the one property
   here that fails SILENTLY — hence a dedicated test.
2. **HASH THE MEMORY CONTENT BEFORE REDACTING IT.** ``FrozenMemoryRecord.content_hash`` identifies
   what was RESOLVED from CAO memory. Hashing post-redaction would make that identity depend on
   ``secret_gate._SECRET_PATTERNS``, so any later change to the ruleset would silently alter every
   historical hash and break FR-9's cross-resume comparison at the upgrade. A hash needs no
   redaction, which is why this module can touch the raw content and remain safe.

THE HASH/CONTENT ASYMMETRY IS DELIBERATE. ``content_hash`` covers the FULL resolved content;
``content`` stores what FITS. When the bound bites they describe different byte strings, and
``memory.truncated`` records it. That looks wrong until FR-9 is read precisely: its criterion is that
altering CAO memory after a failure does not change what a resumed run SEES. **Determinism is the
requirement; completeness is not.** A truncated block injected identically on every resume satisfies
FR-9; re-resolving to recover the dropped bytes would violate it, because re-resolution is exactly
what a post-failure memory edit changes.

WHAT THIS MODULE DOES NOT DO. It does not store the envelope (``manifest-column`` owns the column),
does not assemble the field values (``manifest-freeze`` owns run-start assembly), does not compute
``plan_id`` (``plan-identifier`` owns the scheme — this module treats the value as OPAQUE and never
parses it), and does not resolve memory (``memory-resolve-once`` owns that, and fills the record
shape defined here).
"""

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional

from cli_agent_orchestrator.constants import WORKFLOW_MANIFEST_MAX_BYTES
from cli_agent_orchestrator.services.secret_gate import redact_json_leaves

# Compact separators: the bound is on BYTES, so whitespace is storage spent for nothing.
# Same form as ``step_result.serialise_envelope``.
_JSON_SEPARATORS = (",", ":")
_V2_PLAN_ID_PREFIX = "plan-v2:"
_LOWER_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class FrozenMemoryRecord:
    """FR-9's contribution: the resolved memory block, frozen at run start.

    ``content_hash`` covers the FULL resolved content (see the module docstring); ``content`` holds
    what survived bounding. ``truncated`` is the flag that makes that asymmetry legible rather than
    looking like corruption.
    """

    content: str
    source: str
    content_hash: str
    truncated: bool


@dataclass(frozen=True)
class ExecutionManifestEnvelope:
    """The value persisted into ``workflow_run.manifest_json``.

    Frozen: FR-8's "frozen manifest" enforced structurally, so nothing downstream can mutate a
    manifest after it is built. ``plan_id`` is carried OPAQUELY — this module never parses it.

    SIX FIELDS ARE OPTIONAL, AND ``None`` MEANS OMITTED RATHER THAN NULL (issue #583 Bolt 2, unit
    ``manifest-freeze``). ``provider``, ``model``, ``profile``, ``permissions``, ``limits`` and
    ``retry_policy`` have NO run-level source in the script tier — each is a per-step argument to
    ``step()``. :func:`_document` therefore DROPS them when they are ``None`` instead of serialising
    ``null``, because a ``null`` asserts "this field exists and has no value" while the truth is that
    the field has no run-level existence in that tier. Six nulls would collapse "not applicable here",
    "not captured" and "genuinely absent" into one indistinguishable token.

    ``notes`` carries the explanation INTO the envelope. The envelope is what an agent reads when
    diagnosing a failed run (FR-12's concern), so a divergence from FR-8's field list explained only in
    a design document is one the reader of the DATA would rediscover — and most likely misread as
    missing data.
    """

    plan_id: str
    source_hash: str
    inputs: Any
    repo_baseline: Any
    provider: Optional[Any]
    model: Optional[Any]
    profile: Optional[Any]
    permissions: Optional[Any]
    limits: Optional[Any]
    retry_policy: Optional[Any]
    memory: FrozenMemoryRecord
    truncated: bool
    redacted: bool
    notes: str = ""


@dataclass(frozen=True)
class ApprovedPlanV2:
    """Immutable public approval bytes: opaque digests, never launch material."""

    plan_id: str
    components_json: str


class PlanV2PublicEnvelopeEncodingError(ValueError):
    """Approved public metadata is malformed; rejected values stay secret."""

    def __init__(self) -> None:
        super().__init__("plan-v2 public envelope is invalid")


def _validate_v2_approved(approved: Any) -> None:
    if not isinstance(approved, ApprovedPlanV2):
        raise PlanV2PublicEnvelopeEncodingError()
    plan_id = approved.plan_id
    digest = plan_id.removeprefix(_V2_PLAN_ID_PREFIX) if isinstance(plan_id, str) else ""
    if (
        not isinstance(plan_id, str)
        or not plan_id.startswith(_V2_PLAN_ID_PREFIX)
        or len(digest) != 64
        or any(character not in _LOWER_HEX for character in digest)
        or not isinstance(approved.components_json, str)
        or not approved.components_json.isascii()
    ):
        raise PlanV2PublicEnvelopeEncodingError()


@dataclass(frozen=True)
class PlanV2PublicEnvelope:
    """Public approval plus a bounded, lossy prefix of diagnostic evidence.

    ``evidence_dropped`` is the number of attempted appends omitted after the
    first overflow. It is diagnostic and UNATTESTED: this envelope is not a
    durable admission ledger. ``evidence_bytes`` caches the comma-joined UTF-8
    payload length so size calculation does not reserialize the prefix.
    Construction validates that cache by scanning the prefix, so a complete
    append remains O(prefix length), not O(1).
    """

    approved: ApprovedPlanV2
    evidence_json: tuple[str, ...] = ()
    evidence_dropped: int = 0
    evidence_bytes: int = 0

    def __post_init__(self) -> None:
        _validate_v2_approved(self.approved)
        if (
            not isinstance(self.evidence_json, tuple)
            or not all(isinstance(item, str) for item in self.evidence_json)
            or isinstance(self.evidence_dropped, bool)
            or not isinstance(self.evidence_dropped, int)
            or self.evidence_dropped < 0
            or isinstance(self.evidence_bytes, bool)
            or not isinstance(self.evidence_bytes, int)
            or self.evidence_bytes < 0
        ):
            raise EvidenceEncodingError("metadata")
        try:
            expected = sum(len(item.encode("utf-8")) for item in self.evidence_json)
        except UnicodeEncodeError:
            raise EvidenceEncodingError("metadata") from None
        expected += max(0, len(self.evidence_json) - 1)
        if self.evidence_bytes != expected:
            raise EvidenceEncodingError("metadata")

    @property
    def evidence_truncated(self) -> bool:
        return self.evidence_dropped > 0


class EvidenceEncodingError(ValueError):
    """Evidence was not JSON-compatible; rejected values and keys stay secret."""

    def __init__(self, location: str) -> None:
        if location == "metadata":
            super().__init__("plan-v2 evidence metadata is invalid")
        else:
            super().__init__(f"plan-v2 evidence is not JSON-compatible at {location}")


# Backward-compatible spelling for the additive, unreleased v2 API.
PlanV2EvidenceError = EvidenceEncodingError


class PlanV2PublicEnvelopeTooLargeError(ValueError):
    """The immutable public approval body cannot fit and must not be trimmed."""

    def __init__(self) -> None:
        super().__init__("plan-v2 public envelope exceeds size limit")


# The six fields a caller may legitimately omit, in FR-8's order. Named once so ``_document`` and
# ``parse`` cannot disagree about which keys are optional.
_OMITTABLE = ("provider", "model", "profile", "permissions", "limits", "retry_policy")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _document(envelope: ExecutionManifestEnvelope) -> Dict[str, Any]:
    """The wire shape. One place, so ``serialise`` and the bounding measurement cannot disagree.

    An omittable field whose value is ``None`` is DROPPED rather than serialised as ``null`` — see the
    envelope's docstring for why the distinction matters. Because the bounding measurement runs through
    this same function, an omitted field costs nothing against the byte bound either.
    """
    document: Dict[str, Any] = {
        "plan_id": envelope.plan_id,
        "source_hash": envelope.source_hash,
        "inputs": envelope.inputs,
        "repo_baseline": envelope.repo_baseline,
    }
    for name in _OMITTABLE:
        value = getattr(envelope, name)
        if value is not None:
            document[name] = value
    document["memory"] = {
        "content": envelope.memory.content,
        "source": envelope.memory.source,
        "content_hash": envelope.memory.content_hash,
        "truncated": envelope.memory.truncated,
    }
    document["truncated"] = envelope.truncated
    document["redacted"] = envelope.redacted
    if envelope.notes:
        document["notes"] = envelope.notes
    return document


def _encoded_length(envelope: ExecutionManifestEnvelope) -> int:
    return len(json.dumps(_document(envelope), separators=_JSON_SEPARATORS).encode("utf-8"))


def build(
    *,
    plan_id: str,
    source_hash: str,
    inputs: Any,
    repo_baseline: Any,
    provider: Optional[Any] = None,
    model: Optional[Any] = None,
    profile: Optional[Any] = None,
    permissions: Optional[Any] = None,
    limits: Optional[Any] = None,
    retry_policy: Optional[Any] = None,
    memory_content: str = "",
    memory_source: str = "",
    notes: str = "",
) -> ExecutionManifestEnvelope:
    """Produce a redacted, bounded, flagged envelope.

    TOTAL: raises nothing on well-typed input (INV: bounding is lossy but never rejects). An
    oversized document is truncated and flagged, never refused — a run must not fail because its
    manifest was large.

    Order, per the module docstring: hash the memory content, assemble, redact the whole tree, then
    measure and bound. ``redacted`` is a bare bool and the matched pattern names are DISCARDED —
    naming which pattern fired is itself a disclosure about the secret's shape.
    """
    # 1. Hash BEFORE redaction: the identity must not depend on the redaction ruleset.
    memory_hash = _sha256(memory_content)

    # 2. Assemble the pre-redaction document. ``inputs`` is arbitrary user-supplied JSON, which is
    #    why redaction below has to walk the tree rather than touch a single field.
    raw: Dict[str, Any] = {
        "plan_id": plan_id,
        "source_hash": source_hash,
        "inputs": inputs,
        "repo_baseline": repo_baseline,
        "provider": provider,
        "model": model,
        "profile": profile,
        "permissions": permissions,
        "limits": limits,
        "retry_policy": retry_policy,
        "notes": notes,
        "memory_content": memory_content,
        "memory_source": memory_source,
    }

    # 3. Redact BEFORE bounding. ``redacted`` is derived by comparison rather than by collecting
    #    pattern names, which keeps the names out of this function entirely.
    cleaned = redact_json_leaves(raw)
    redacted = cleaned != raw

    envelope = ExecutionManifestEnvelope(
        plan_id=cleaned["plan_id"],
        source_hash=cleaned["source_hash"],
        inputs=cleaned["inputs"],
        repo_baseline=cleaned["repo_baseline"],
        provider=cleaned["provider"],
        model=cleaned["model"],
        profile=cleaned["profile"],
        permissions=cleaned["permissions"],
        limits=cleaned["limits"],
        retry_policy=cleaned["retry_policy"],
        notes=cleaned["notes"],
        memory=FrozenMemoryRecord(
            content=cleaned["memory_content"],
            source=cleaned["memory_source"],
            content_hash=memory_hash,
            truncated=False,
        ),
        truncated=False,
        redacted=redacted,
    )

    # 4. Bound, INCLUSIVELY: a document of exactly the bound is NOT truncated.
    return _bound(envelope)


def _bound(envelope: ExecutionManifestEnvelope) -> ExecutionManifestEnvelope:
    """Trim the memory content until the encoded document fits, INCLUSIVELY.

    Factored out of :func:`build` so :func:`with_memory` bounds by the same code rather than by a
    second implementation of the same rule — the memory record is filled AFTER the freeze, and the
    freeze measured a document that did not yet contain it, so filling it can exceed a bound that
    previously passed.
    """
    overflow = _encoded_length(envelope) - WORKFLOW_MANIFEST_MAX_BYTES
    if overflow <= 0:
        return envelope

    # The memory content is the field that gives: it is the only field with no natural small bound,
    # and its hash was already taken over the FULL string, so bounding it loses bytes without losing
    # identity. Trim by the overflow (plus a margin for the JSON escaping of what remains), then
    # re-measure rather than trusting the arithmetic.
    content = envelope.memory.content
    keep = max(0, len(content.encode("utf-8")) - overflow)
    while True:
        trimmed = content.encode("utf-8")[:keep].decode("utf-8", errors="ignore")
        candidate = replace(
            envelope,
            memory=replace(envelope.memory, content=trimmed, truncated=True),
            truncated=True,
        )
        if _encoded_length(candidate) <= WORKFLOW_MANIFEST_MAX_BYTES or keep == 0:
            return candidate
        keep = max(0, keep - max(1, keep // 8))


def with_memory(
    envelope: ExecutionManifestEnvelope, *, content: str, source: str
) -> ExecutionManifestEnvelope:
    """Fill the memory record of an already-frozen envelope (issue #583 ``memory-resolve-once``).

    The freeze writes this record EMPTY — memory resolves lazily, at the first point a run needs it,
    and a run that creates no terminal must resolve nothing because an unused block is sensitive text
    kept for no reason. So FR-9's payload cannot ride the freeze, and this is how it arrives.

    EVERY OTHER FIELD IS RETURNED BYTE-IDENTICAL, AND THAT IS A CORRECTNESS REQUIREMENT RATHER THAN
    TIDINESS. The other fields are the inputs ``plan_identifier.compute`` hashed into the ``plan_id``
    that a human may already have approved. Rewriting one would change the identifier of a plan that
    has already been approved and possibly already run, and ``approval_gate`` would then refuse a run
    whose approval was perfectly valid. This is also why the envelope is NOT rebuilt by re-calling
    :func:`build` with the parsed fields: those fields have already been redacted, so a rebuild would
    redact them a second time and re-measure the whole document, moving bytes in exactly the fields
    that must not move.

    The module's ordering rules are preserved:

    * the hash is taken over the FULL ``content``, BEFORE redaction, so ``content_hash`` identifies
      what was RESOLVED from CAO memory rather than what survived the redaction ruleset of the day;
    * redaction runs BEFORE bounding, so a secret straddling the boundary cannot be persisted in the
      clear as the truncated prefix;
    * bounding runs through the shared :func:`_bound`, because the freeze measured a document that did
      not yet contain this record and filling it can push a previously-passing document over.

    ``redacted`` is OR-ed rather than replaced: the flag records that something in this document was
    redacted at some point, and a memory fill that redacts nothing must not clear a flag the freeze set.
    """
    memory_hash = _sha256(content)

    # Only the memory pair is redacted here. The rest of the tree was redacted at freeze time and is
    # deliberately not walked again — see the docstring on why re-processing it is the hazard.
    raw: Dict[str, Any] = {"memory_content": content, "memory_source": source}
    cleaned = redact_json_leaves(raw)

    filled = replace(
        envelope,
        memory=FrozenMemoryRecord(
            content=cleaned["memory_content"],
            source=cleaned["memory_source"],
            content_hash=memory_hash,
            truncated=False,
        ),
        redacted=envelope.redacted or cleaned != raw,
    )
    return _bound(filled)


def serialise(envelope: ExecutionManifestEnvelope) -> str:
    """Render an envelope to the string stored in ``workflow_run.manifest_json``."""
    return json.dumps(_document(envelope), separators=_JSON_SEPARATORS)


def parse(manifest_json: Optional[str]) -> Optional[ExecutionManifestEnvelope]:
    """Reconstruct an envelope from a stored value. TOTAL — never raises.

    ``None`` is the column's default and means "manifest absent". Empty or malformed input ALSO
    answers ``None``, matching ``step_result.parse_envelope``'s posture: a corrupt manifest degrades
    to "absent" rather than propagating a ``json`` exception into every read of the run row. Absent
    is a state the Bolt 2 approval gate already handles fail-closed, so degrading is safe; raising
    would turn one bad row into a failure on every read of it.
    """
    if not manifest_json:
        return None
    try:
        document = json.loads(manifest_json)
        memory = document["memory"]
        return ExecutionManifestEnvelope(
            plan_id=document["plan_id"],
            source_hash=document["source_hash"],
            inputs=document["inputs"],
            repo_baseline=document["repo_baseline"],
            provider=document.get("provider"),
            model=document.get("model"),
            profile=document.get("profile"),
            permissions=document.get("permissions"),
            limits=document.get("limits"),
            retry_policy=document.get("retry_policy"),
            memory=FrozenMemoryRecord(
                content=memory["content"],
                source=memory["source"],
                content_hash=memory["content_hash"],
                truncated=memory["truncated"],
            ),
            truncated=document["truncated"],
            redacted=document["redacted"],
            notes=document.get("notes", ""),
        )
    except Exception:  # noqa: BLE001 — totality is the contract; see the docstring
        return None


def build_v2_public(components: Any) -> PlanV2PublicEnvelope:
    """Build a public-verifiable v2 envelope from pre-redaction digests."""
    from cli_agent_orchestrator.services import plan_identifier

    document = plan_identifier.v2_component_document(components)
    envelope = PlanV2PublicEnvelope(
        approved=ApprovedPlanV2(
            plan_id=plan_identifier.compute_v2(components),
            components_json=json.dumps(document, separators=_JSON_SEPARATORS),
        )
    )
    if v2_public_size_bytes(envelope) > WORKFLOW_MANIFEST_MAX_BYTES:
        raise PlanV2PublicEnvelopeTooLargeError()
    return envelope


def _v2_public_document(envelope: PlanV2PublicEnvelope) -> Dict[str, Any]:
    return {
        "approved": {
            "plan_id": envelope.approved.plan_id,
            "components": json.loads(envelope.approved.components_json),
        },
        "evidence": [json.loads(item) for item in envelope.evidence_json],
        "evidence_dropped": envelope.evidence_dropped,
    }


def approved_v2_bytes(envelope: PlanV2PublicEnvelope) -> bytes:
    """Return the byte-identical authorization sub-document."""
    plan_id = json.dumps(envelope.approved.plan_id, separators=_JSON_SEPARATORS).encode("utf-8")
    return (
        b'{"plan_id":'
        + plan_id
        + b',"components":'
        + envelope.approved.components_json.encode("utf-8")
        + b"}"
    )


def serialise_v2_public(envelope: PlanV2PublicEnvelope) -> str:
    return json.dumps(_v2_public_document(envelope), separators=_JSON_SEPARATORS)


def v2_public_size_bytes(envelope: PlanV2PublicEnvelope) -> int:
    """Return the exact compact-JSON UTF-8 size using cached evidence length."""
    approved_size = len(approved_v2_bytes(envelope))
    # {"approved":<approved>,"evidence":[<payload>],"evidence_dropped":<n>}
    return (
        len(b'{"approved":')
        + approved_size
        + len(b',"evidence":[')
        + envelope.evidence_bytes
        + len(b'],"evidence_dropped":')
        + len(str(envelope.evidence_dropped).encode("ascii"))
        + len(b"}")
    )


def _validate_evidence_json(value: Any, location: str = "evidence") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise EvidenceEncodingError(location)
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_evidence_json(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceEncodingError(f"{location}.object.key")
            _validate_evidence_json(item, f"{location}.object.value")
        return
    raise EvidenceEncodingError(location)


def append_v2_evidence(envelope: PlanV2PublicEnvelope, evidence: Any) -> PlanV2PublicEnvelope:
    """Append redacted evidence without rewriting approved bytes.

    Evidence that would exceed the public envelope ceiling is omitted. The
    retained prefix is terminal after the first drop; every later append only
    increments ``evidence_dropped``. This bounded record is diagnostic and
    UNATTESTED, not a durable admission ledger. A caller must not fail an
    otherwise authorized run when diagnostic evidence encoding fails; it should
    catch :class:`EvidenceEncodingError` and record a drop at its integration
    boundary. Executable material never passes through this lossy path.
    """
    if envelope.evidence_truncated:
        return replace(envelope, evidence_dropped=envelope.evidence_dropped + 1)
    try:
        _validate_evidence_json(evidence)
        cleaned = redact_json_leaves(evidence)
        encoded = json.dumps(cleaned, separators=_JSON_SEPARATORS, allow_nan=False)
    except EvidenceEncodingError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise EvidenceEncodingError("evidence.structure") from None
    encoded_bytes = len(encoded.encode("utf-8"))
    separator_bytes = 1 if envelope.evidence_json else 0
    candidate = replace(
        envelope,
        evidence_json=envelope.evidence_json + (encoded,),
        evidence_bytes=envelope.evidence_bytes + separator_bytes + encoded_bytes,
    )
    # Reserve decimal growth for a long-lived dropped counter so adding loss
    # metadata cannot push an already accepted prefix over the ceiling.
    reserved_counter = replace(candidate, evidence_dropped=10**20 - 1)
    if v2_public_size_bytes(reserved_counter) <= WORKFLOW_MANIFEST_MAX_BYTES:
        return candidate
    return replace(envelope, evidence_dropped=1)


def verify_v2_public(value: Any) -> bool:
    """Verify a serialized or in-memory public envelope without private bytes."""
    from cli_agent_orchestrator.services import plan_identifier

    try:
        if isinstance(value, str):
            document = json.loads(value)
        elif isinstance(value, PlanV2PublicEnvelope):
            _validate_v2_approved(value.approved)
            document = _v2_public_document(value)
        elif isinstance(value, dict):
            document = value
        else:
            return False
        if not isinstance(document, dict):
            return False
        approved = document["approved"]
        return (
            isinstance(approved, dict)
            and set(approved) == {"plan_id", "components"}
            and plan_identifier.verify_v2_components(approved["components"], approved["plan_id"])
        )
    except (
        AttributeError,
        KeyError,
        RecursionError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False
