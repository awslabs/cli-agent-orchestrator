"""Memory relationship service — the single authoritative boundary for the
``memory_relationships`` table (issue #511).

Every surface (compiler, wiki lint, REST API, CLI, MemoryGraphProvider, recall,
See Also, future importers) reads and writes relationships THROUGH this service;
no other component issues SQL against the table (FR-2.1 single-boundary
invariant). The service owns endpoint resolution + scope checks, type/status
validation, create/upsert, producer-scoped replacement, dedup, stale detection,
transactional writes, and read projections.

Design principles held:
- Absence is not deletion (principle 6): ``replace_set`` replaces only the
  ``(scope, scope_id, source_key, origin, type)`` tuple's rows, never another
  producer's or another type's.
- Confidence is evidence, not invented certainty (principle 7): legacy links and
  unscored producers store ``confidence=NULL``; a value is stored only when a
  producer supplies a validated number in [0, 1]; NULL is never coerced to 0 and
  ranking treats NULL as absence-of-evidence.
- Scope isolation is invariant (principle 3): both endpoints must resolve inside
  the same ``(scope, scope_id)``; cross-scope and self-links are rejected before
  persistence.

Fail-closed: every validation raises ``ValueError`` BEFORE any DB write.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from cli_agent_orchestrator.clients.database import (
    RELATIONSHIP_SCOPE_ID_SENTINEL,
    MemoryMetadataModel,
    MemoryRelationshipModel,
    SessionLocal,
    _utcnow,
)
from cli_agent_orchestrator.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Closed taxonomies (reuse the graph EdgeType string values, ADR-5).
VALID_TYPES = frozenset({"relates_to", "contradiction", "supersedes"})
VALID_STATUSES = frozenset({"active", "proposal", "rejected", "superseded", "deleted"})
# Statuses an OPERATOR reached through an explicit command (`relationships
# reject` / `relationships delete`). A producer recompute must never overwrite
# one: re-creation is the documented way back, exactly as ``promote()`` refuses
# these two. ``superseded`` is excluded on purpose — it is lifecycle-derived, not
# operator-authored.
CURATION_TERMINAL_STATUSES = frozenset({"rejected", "deleted"})
VALID_ORIGINS = frozenset(
    {"compiler", "wiki_lint", "human", "legacy_related_keys", "external_import"}
)

# Bounds (NFR-1.6). Final numeric values.
MAX_EDGES_PER_MUTATION = 64
MAX_ATTRIBUTES_BYTES = 2048

# The content-free audit event for relationship mutations (NFR-1.7). MUST be
# registered in NOWAIT_AUDIT_EVENTS or audit_log drops it silently.
AUDIT_EVENT = "relationship_mutation"


@dataclass
class RelationshipDTO:
    """The inspectable, content-free return/response contract (FR-5.4).

    Contains keys/type/origin/status/confidence/rank/attributes/timestamps and a
    derived ``stale`` flag — NEVER a memory body or prompt (NFR-1.7). ``scope_id``
    is denormalised back to ``None`` for global/federated (the row stores the
    sentinel).
    """

    id: str
    scope: str
    scope_id: Optional[str]
    source_key: str
    target_key: str
    type: str
    origin: str
    status: str
    confidence: Optional[float]
    rank: Optional[int]
    attributes: Optional[Dict[str, Any]]
    source_updated_at: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    stale: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "source_key": self.source_key,
            "target_key": self.target_key,
            "type": self.type,
            "origin": self.origin,
            "status": self.status,
            "confidence": self.confidence,
            "rank": self.rank,
            "attributes": self.attributes,
            "source_updated_at": self.source_updated_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stale": self.stale,
        }


@dataclass
class ReplaceReport:
    """Result of a producer-scoped ``replace_set`` (content-free)."""

    added: int = 0
    kept: int = 0
    removed: int = 0
    rejected: List[Dict[str, str]] = field(default_factory=list)
    # Rows left untouched because they carry a curation-terminal status (an
    # operator's reject/delete verdict). Reported so a producer recompute that
    # declines to overwrite a human decision is VISIBLE rather than silent —
    # content-free, like every other field here: target key + status only.
    preserved: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class EdgeInput:
    """One edge in a ``replace_set`` batch."""

    target_key: str
    confidence: Optional[float] = None
    rank: Optional[int] = None
    attributes: Optional[Dict[str, Any]] = None


def _iso(value: Any) -> Optional[str]:
    """Normalise a datetime/str timestamp to an ISO string (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _sort_dt(value: Any) -> datetime:
    """Normalise a ``created_at`` to a tz-AWARE UTC datetime for sorting.

    Sorting relationship rows on a raw ``created_at`` is unsafe in two ways, and
    a tz-aware sentinel alone only fixes the first:

    1. ``created_at`` is nullable with no DB default, so a NULL row previously
       fell back to a NAIVE ``datetime.min`` while a populated row on a
       ``DateTime(timezone=True)`` column is tz-AWARE — comparing them raises
       ``TypeError: can't compare offset-naive and offset-aware datetimes``.
    2. SQLite does not persist timezone offsets, so two POPULATED rows can also
       disagree on awareness depending on how each was written. Normalising both
       sides (rather than only the sentinel) closes that wider hole too.

    A naive value is interpreted as UTC — the only sane reading, since every
    write path stamps UTC (``_utcnow``).
    """
    if not isinstance(value, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _rank_then_created(row: Any) -> tuple:
    """The See-Also / recall projection order: ``(rank, created_at)``.

    NULL rank sorts last (``1_000_000`` sentinel, preserved from the original
    ordering); ``created_at`` is normalised by ``_sort_dt`` so a NULL or naive
    value never crashes the comparison.
    """
    return (row.rank if row.rank is not None else 1_000_000, _sort_dt(row.created_at))


class MemoryRelationshipService:
    """Sole reader/writer of the ``memory_relationships`` table."""

    def __init__(self) -> None:
        # Reuse MemoryService's static sanitizers; no instance state shared.
        self._sanitize_key = MemoryService._sanitize_key
        self._sanitize_scope_id = MemoryService._sanitize_scope_id

    # ------------------------------------------------------------------ #
    # scope_id normalisation (sentinel is scoped to memory_relationships)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_sentinel(scope_id: Optional[str]) -> str:
        """Map a logical scope_id to the memory_relationships storage form.

        None (global/federated) -> the NOT-NULL sentinel; else the value.
        """
        return scope_id if scope_id is not None else RELATIONSHIP_SCOPE_ID_SENTINEL

    @staticmethod
    def _from_sentinel(stored: Optional[str]) -> Optional[str]:
        """Denormalise a stored scope_id back to the logical value for the DTO."""
        if stored is None or stored == RELATIONSHIP_SCOPE_ID_SENTINEL:
            return None
        return stored

    # ------------------------------------------------------------------ #
    # validation (fail-closed, before any persist)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_type(type_: str) -> str:
        if type_ not in VALID_TYPES:
            raise ValueError(f"invalid relationship type: {type_!r}")
        return type_

    @staticmethod
    def _validate_status(status: str) -> str:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid relationship status: {status!r}")
        return status

    @staticmethod
    def _validate_origin(origin: str) -> str:
        if origin not in VALID_ORIGINS:
            raise ValueError(f"invalid relationship origin: {origin!r}")
        return origin

    @staticmethod
    def _validate_confidence(confidence: Optional[float]) -> Optional[float]:
        if confidence is None:
            return None
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ValueError(f"confidence must be a number in [0,1] or None: {confidence!r}")
        value = float(confidence)
        if value < 0.0 or value > 1.0:
            raise ValueError(f"confidence out of range [0,1]: {value}")
        return value

    @staticmethod
    def _validate_attributes(attributes: Optional[Dict[str, Any]]) -> Optional[str]:
        if attributes is None:
            return None
        if not isinstance(attributes, dict):
            raise ValueError("attributes must be a dict or None")
        # json.dumps raises TypeError for a non-serialisable value and ValueError
        # for a circular reference. A TypeError escaping here would break TWO
        # contracts, so it is re-raised as ValueError (the service's single
        # fail-closed validation type, per this module's docstring):
        #   1. every REST handler maps ONLY ValueError -> 400/404, so a TypeError
        #      surfaces as an unhandled 500 instead of a client error;
        #   2. replace_set's per-edge soft rejection catches ONLY ValueError, so
        #      one bad edge would abort the whole batch instead of being dropped.
        try:
            encoded = json.dumps(attributes, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as e:
            raise ValueError(f"attributes must be JSON-serialisable: {e}") from e
        # The limit is enforced in BYTES, so the message must report bytes — a
        # char count understates any multi-byte payload and misleads the caller
        # about how much to trim.
        encoded_bytes = len(encoded.encode("utf-8"))
        if encoded_bytes > MAX_ATTRIBUTES_BYTES:
            raise ValueError(
                f"attributes exceed {MAX_ATTRIBUTES_BYTES} bytes ({encoded_bytes} bytes)"
            )
        return encoded

    def _sanitize_endpoints(self, source_key: str, target_key: str) -> tuple:
        """Sanitise both endpoint keys; reject a self-link. Returns (src, tgt)."""
        src = self._sanitize_key(source_key)
        tgt = self._sanitize_key(target_key)
        if src == tgt:
            raise ValueError(f"self-link rejected: {src!r}")
        return src, tgt

    def _assert_endpoint_exists(
        self, db: Any, scope: str, scope_id: Optional[str], key: str
    ) -> None:
        """Assert a memory with ``key`` exists in the SAME (scope, scope_id).

        Queries MemoryMetadataModel, whose scope_id is genuinely nullable (real
        NULL for global), so match logical None with ``.is_(None)`` — NOT the
        relationship-table sentinel (which would match nothing for global).
        """
        q = db.query(MemoryMetadataModel).filter(
            MemoryMetadataModel.key == key,
            MemoryMetadataModel.scope == scope,
        )
        if scope_id is not None:
            q = q.filter(MemoryMetadataModel.scope_id == scope_id)
        else:
            q = q.filter(MemoryMetadataModel.scope_id.is_(None))
        if q.first() is None:
            raise ValueError(f"endpoint does not resolve in scope ({scope},{scope_id}): {key!r}")

    # ------------------------------------------------------------------ #
    # DTO projection
    # ------------------------------------------------------------------ #
    def _to_dto(
        self, row: Any, source_updated_lookup: Optional[datetime] = None
    ) -> RelationshipDTO:
        attrs = None
        if row.attributes_json:
            try:
                attrs = json.loads(row.attributes_json)
            except (ValueError, TypeError):
                attrs = None
        stale = False
        if row.source_updated_at is not None and source_updated_lookup is not None:
            stale = row.source_updated_at < source_updated_lookup
        return RelationshipDTO(
            id=row.id,
            scope=row.scope,
            scope_id=self._from_sentinel(row.scope_id),
            source_key=row.source_key,
            target_key=row.target_key,
            type=row.type,
            origin=row.origin,
            status=row.status,
            confidence=row.confidence,
            rank=row.rank,
            attributes=attrs,
            source_updated_at=_iso(row.source_updated_at),
            created_at=_iso(row.created_at),
            updated_at=_iso(row.updated_at),
            stale=stale,
        )

    def _audit(self, action: str, row: Any) -> None:
        """Emit a content-free relationship_mutation audit event (NFR-1.7)."""
        try:
            from cli_agent_orchestrator.services.audit_log import write_audit_nowait

            write_audit_nowait(
                AUDIT_EVENT,
                f"relationship {action}",
                action=action,
                id=row.id,
                scope=row.scope,
                scope_id=str(row.scope_id),
                source_key=row.source_key,
                target_key=row.target_key,
                type=row.type,
                origin=row.origin,
                status=row.status,
            )
        except Exception as e:  # pragma: no cover - audit must never break a write
            logger.debug(f"relationship audit emit failed ({action}): {e}")

    # ------------------------------------------------------------------ #
    # write operations
    # ------------------------------------------------------------------ #
    def create(
        self,
        scope: str,
        scope_id: Optional[str],
        source_key: str,
        target_key: str,
        type: str,
        origin: str,
        *,
        status: str = "active",
        confidence: Optional[float] = None,
        rank: Optional[int] = None,
        attributes: Optional[Dict[str, Any]] = None,
        source_updated_at: Optional[datetime] = None,
    ) -> RelationshipDTO:
        """Create-or-upsert one relationship. Validates fail-closed, then upserts
        on the dedup tuple (existing row updates mutable fields; FR-2.6). One
        transaction (NFR-1.5)."""
        self._validate_type(type)
        self._validate_status(status)
        self._validate_origin(origin)
        conf = self._validate_confidence(confidence)
        attrs_json = self._validate_attributes(attributes)
        src, tgt = self._sanitize_endpoints(source_key, target_key)
        sentinel = self._to_sentinel(scope_id)

        with SessionLocal() as db:
            self._assert_endpoint_exists(db, scope, scope_id, src)
            self._assert_endpoint_exists(db, scope, scope_id, tgt)
            existing = self._find_existing(db, scope, sentinel, src, tgt, type, origin)
            if existing is not None:
                existing.status = status
                existing.confidence = conf
                existing.rank = rank
                existing.attributes_json = attrs_json
                if source_updated_at is not None:
                    existing.source_updated_at = source_updated_at
                existing.updated_at = _utcnow()
                db.commit()
                db.refresh(existing)
                self._audit("create", existing)
                return self._to_dto(existing)
            row = MemoryRelationshipModel(
                id=str(uuid.uuid4()),
                scope=scope,
                scope_id=sentinel,
                source_key=src,
                target_key=tgt,
                type=type,
                origin=origin,
                status=status,
                confidence=conf,
                rank=rank,
                attributes_json=attrs_json,
                source_updated_at=source_updated_at,
            )
            db.add(row)
            # The existence check above is a read-then-insert with no
            # transactional protection, so two concurrent creates of the same
            # dedup tuple both miss and both insert. The loser hit the UNIQUE
            # index and raised IntegrityError — NOT a ValueError — so it escaped
            # every REST handler's ValueError->400 mapping as an unhandled 500
            # (human review, PR #524).
            #
            # The dedup index makes the winning row indistinguishable from the
            # one this call intended to write, so converge on it instead of
            # failing: that matches create()'s documented upsert contract, where
            # a same-tuple create returns the existing row. Any OTHER
            # IntegrityError (a genuine constraint violation) is re-raised as a
            # ValueError so the boundary policy stays uniform — the service
            # raises ValueError for caller-fixable input, and the API maps it.
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                winner = self._find_existing(db, scope, sentinel, src, tgt, type, origin)
                if winner is None:
                    raise ValueError(
                        "relationship create conflicted and the conflicting row "
                        "could not be resolved"
                    )
                self._audit("create", winner)
                return self._to_dto(winner)
            db.refresh(row)
            self._audit("create", row)
            return self._to_dto(row)

    def _find_existing(
        self,
        db: Any,
        scope: str,
        sentinel: str,
        src: str,
        tgt: str,
        type_: str,
        origin: str,
    ) -> Any:
        """Resolve the row matching ``create()``'s dedup tuple, or None.

        Extracted as the named seam ``create()`` uses to choose upsert-vs-insert,
        and reused to re-resolve the winner after an ``IntegrityError``. Having
        one named probe (rather than two inline copies of the same filter) is
        what lets the concurrency test blind exactly this step and drive the
        real conflict path.
        """
        return (
            db.query(MemoryRelationshipModel)
            .filter(
                MemoryRelationshipModel.scope == scope,
                MemoryRelationshipModel.scope_id == sentinel,
                MemoryRelationshipModel.source_key == src,
                MemoryRelationshipModel.target_key == tgt,
                MemoryRelationshipModel.type == type_,
                MemoryRelationshipModel.origin == origin,
            )
            .first()
        )

    def replace_set(
        self,
        scope: str,
        scope_id: Optional[str],
        source_key: str,
        origin: str,
        type: str,
        edges: List[EdgeInput],
    ) -> ReplaceReport:
        """Producer-scoped replacement (principle 6, FR-2.5).

        In ONE transaction, replaces exactly the rows matching
        ``(scope, scope_id, source_key, origin, type)`` with ``edges``: rows of
        that tuple whose target is absent from ``edges`` are deleted; edges are
        upserted. Rows of ANY OTHER origin or type — human, wiki_lint, legacy,
        supersedes, contradiction — are structurally outside the WHERE clause and
        are never touched. Producers MUST pass their FULL current set.

        Within this producer's own rows, a CURATION-TERMINAL status
        (``rejected``/``deleted`` — see ``CURATION_TERMINAL_STATUSES``) is
        preserved on BOTH branches: such a row is neither reactivated when the
        producer still reports the target nor deleted when it drops it. Each is
        listed in ``report.preserved`` so the refusal is observable. Without
        this, a recompile silently undid an operator's ``relationships reject``.
        """
        self._validate_type(type)
        self._validate_origin(origin)
        if len(edges) > MAX_EDGES_PER_MUTATION:
            raise ValueError(f"replace_set exceeds {MAX_EDGES_PER_MUTATION} edges: {len(edges)}")
        src = self._sanitize_key(source_key)
        sentinel = self._to_sentinel(scope_id)
        report = ReplaceReport()

        # Validate + resolve the incoming set first (fail-closed); collect valid
        # targets, report the rest (FR-1.5-style) without aborting the whole op.
        valid: Dict[str, EdgeInput] = {}
        with SessionLocal() as db:
            for edge in edges:
                try:
                    tgt = self._sanitize_key(edge.target_key)
                except ValueError:
                    report.rejected.append({"target": edge.target_key, "reason": "unsanitised"})
                    continue
                if tgt == src:
                    report.rejected.append({"target": tgt, "reason": "self"})
                    continue
                # Per-edge SOFT rejection for confidence/attributes, consistent
                # with self/dangling above (reviewer F3): one bad edge in a batch
                # is reported, not a hard abort of the whole replace_set.
                try:
                    self._validate_confidence(edge.confidence)
                    self._validate_attributes(edge.attributes)
                except ValueError:
                    report.rejected.append({"target": tgt, "reason": "invalid_attrs_or_confidence"})
                    continue
                try:
                    self._assert_endpoint_exists(db, scope, scope_id, tgt)
                except ValueError:
                    report.rejected.append({"target": tgt, "reason": "dangling"})
                    continue
                valid[tgt] = edge
            # Source must exist too.
            self._assert_endpoint_exists(db, scope, scope_id, src)

            existing_rows = (
                db.query(MemoryRelationshipModel)
                .filter(
                    MemoryRelationshipModel.scope == scope,
                    MemoryRelationshipModel.scope_id == sentinel,
                    MemoryRelationshipModel.source_key == src,
                    MemoryRelationshipModel.origin == origin,
                    MemoryRelationshipModel.type == type,
                )
                .all()
            )
            existing_by_target = {r.target_key: r for r in existing_rows}
            new_targets = set(valid.keys())

            # Delete this producer's rows whose target is not in the new set.
            # A curation-terminal row is RETAINED even when the producer drops
            # the target: deleting it would discard the operator's verdict just
            # as surely as reactivating it, and would let the next recompute
            # re-add the edge as a fresh "active" row with the rejection gone.
            for tgt, r in existing_by_target.items():
                if tgt not in new_targets:
                    if r.status in CURATION_TERMINAL_STATUSES:
                        report.preserved.append({"target": tgt, "status": r.status})
                        continue
                    db.delete(r)
                    report.removed += 1

            now = _utcnow()
            # Stamp the source memory's updated_at on every row this producer
            # writes. Without a writer the ``stale`` flag was INERT — it is
            # derived from ``source_updated_at``, which no production caller ever
            # set, so the DTO field, the CLI's --stale and the REST ?stale=true
            # could only ever report False (human review, PR #524). One batched
            # lookup for the whole set; None when the source has no updated_at,
            # which simply leaves staleness unknown as before.
            src_updated = self._source_updated_map(db, scope, scope_id, {src}).get(src)
            for tgt, edge in valid.items():
                r = existing_by_target.get(tgt)
                attrs_json = self._validate_attributes(edge.attributes)
                if r is not None:
                    # A CURATION-TERMINAL status is a human decision and must
                    # survive a producer recompute. Forcing "active" here let a
                    # recompile silently resurrect an edge an operator had
                    # explicitly rejected via `cao memory relationships reject`,
                    # with nothing in the report to show it happened — which
                    # defeats the curation surface this store ships.
                    #
                    # The set mirrors ``promote()``'s refusal list: rejected and
                    # deleted are operator verdicts reached through an explicit
                    # command, and re-creation (not recompute) is the documented
                    # way back. ``superseded`` is deliberately NOT terminal here:
                    # it is lifecycle-DERIVED rather than operator-authored, so a
                    # producer that still reports the edge is legitimate evidence
                    # the supersession no longer holds.
                    if r.status in CURATION_TERMINAL_STATUSES:
                        report.preserved.append({"target": tgt, "status": r.status})
                        continue
                    r.confidence = self._validate_confidence(edge.confidence)
                    r.rank = edge.rank
                    r.attributes_json = attrs_json
                    r.status = "active"
                    r.source_updated_at = src_updated
                    r.updated_at = now
                    report.kept += 1
                else:
                    db.add(
                        MemoryRelationshipModel(
                            id=str(uuid.uuid4()),
                            scope=scope,
                            scope_id=sentinel,
                            source_key=src,
                            target_key=tgt,
                            type=type,
                            origin=origin,
                            status="active",
                            confidence=self._validate_confidence(edge.confidence),
                            rank=edge.rank,
                            attributes_json=attrs_json,
                            source_updated_at=src_updated,
                        )
                    )
                    report.added += 1
            db.commit()
        # Content-free summary audit for the bulk producer write (reviewer F2):
        # replace_set is the highest-volume path (compiler/lint every compile),
        # so it must leave a forensic trail. Counts + endpoints/origin/type only,
        # never a memory body/prompt (NFR-1.7).
        self._audit_replace_set(scope, sentinel, src, origin, type, report)
        return report

    def _audit_replace_set(
        self,
        scope: str,
        sentinel: str,
        src: str,
        origin: str,
        type_: str,
        report: "ReplaceReport",
    ) -> None:
        try:
            from cli_agent_orchestrator.services.audit_log import write_audit_nowait

            write_audit_nowait(
                AUDIT_EVENT,
                f"relationship replace_set ({origin}/{type_})",
                action="replace_set",
                scope=scope,
                scope_id=str(sentinel),
                source_key=src,
                origin=origin,
                type=type_,
                added=str(report.added),
                kept=str(report.kept),
                removed=str(report.removed),
                rejected=str(len(report.rejected)),
            )
        except Exception as e:  # pragma: no cover - audit must never break a write
            logger.debug(f"replace_set audit emit failed: {e}")

    def _audit_purge(self, scope: str, sentinel: str, key: str, removed: int) -> None:
        """Content-free audit for a forget()-driven purge.

        Reuses ``AUDIT_EVENT`` (``relationship_mutation``), which is already in
        audit_log's closed NOWAIT whitelist — a fresh event type would be dropped
        silently.
        """
        try:
            from cli_agent_orchestrator.services.audit_log import write_audit_nowait

            write_audit_nowait(
                AUDIT_EVENT,
                "relationship purge (memory forgotten)",
                action="purge_for_key",
                scope=scope,
                scope_id=str(sentinel),
                source_key=key,
                removed=str(removed),
            )
        except Exception as e:  # pragma: no cover - audit must never break a write
            logger.debug(f"purge audit emit failed: {e}")

    def _get_row(self, db: Any, id: str) -> Any:
        row = db.query(MemoryRelationshipModel).filter(MemoryRelationshipModel.id == id).first()
        if row is None:
            raise ValueError(f"relationship not found: {id!r}")
        return row

    def patch(
        self,
        id: str,
        *,
        status: Optional[str] = None,
        confidence: Optional[float] = None,
        rank: Optional[int] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> RelationshipDTO:
        """Partial update of mutable fields (status/confidence/rank/attributes).
        Endpoints/type/origin/scope are immutable. A field left as ``None`` is
        unchanged (to clear confidence/attributes, pass an explicit sentinel via
        a future dedicated call; #511 has no clear-to-null API requirement)."""
        if status is not None:
            self._validate_status(status)
        with SessionLocal() as db:
            row = self._get_row(db, id)
            if status is not None:
                row.status = status
            if confidence is not None:
                row.confidence = self._validate_confidence(confidence)
            if rank is not None:
                row.rank = rank
            if attributes is not None:
                row.attributes_json = self._validate_attributes(attributes)
            row.updated_at = _utcnow()
            db.commit()
            db.refresh(row)
            self._audit("patch", row)
            return self._to_dto(row)

    def promote(self, id: str) -> RelationshipDTO:
        """proposal -> active (FR-2.2)."""
        with SessionLocal() as db:
            row = self._get_row(db, id)
            if row.status in ("rejected", "deleted"):
                raise ValueError(f"cannot promote a {row.status} relationship; re-create it")
            row.status = "active"
            row.updated_at = _utcnow()
            db.commit()
            db.refresh(row)
            self._audit("promote", row)
            return self._to_dto(row)

    def reject(self, id: str) -> RelationshipDTO:
        """-> rejected (FR-2.2)."""
        with SessionLocal() as db:
            row = self._get_row(db, id)
            row.status = "rejected"
            row.updated_at = _utcnow()
            db.commit()
            db.refresh(row)
            self._audit("reject", row)
            return self._to_dto(row)

    def soft_delete(self, id: str) -> RelationshipDTO:
        """-> deleted (auditable soft-delete; row retained, FR-2.4)."""
        with SessionLocal() as db:
            row = self._get_row(db, id)
            row.status = "deleted"
            row.updated_at = _utcnow()
            db.commit()
            db.refresh(row)
            self._audit("soft_delete", row)
            return self._to_dto(row)

    def purge_for_key(self, scope: str, scope_id: Optional[str], key: str) -> int:
        """HARD-delete every row touching ``key`` in either direction.

        Called when the underlying memory is FORGOTTEN (human review, PR #524).
        ``forget()`` removes the wiki file, the index entry and the
        memory_metadata row, but used to leave these rows behind still ``active``
        — so a later memory created with the SAME slug silently inherited the
        dead memory's edges, and every read path had to tolerate endpoints that
        no longer resolve.

        This is a HARD delete, not the ``soft_delete`` status transition: a
        soft-deleted row is a curation record ABOUT a live memory, whereas here
        the endpoint itself is gone and the row can never become meaningful
        again. Retaining it is what causes the slug-reuse inheritance bug.
        Deliberately status-agnostic for the same reason — a rejected or
        proposal row pointing at a deleted memory is equally meaningless.

        Returns the number of rows removed; content-free audit on a non-zero
        purge. Both directions are covered: an edge INTO the forgotten key is
        just as dangling as one out of it.
        """
        from sqlalchemy import or_

        k = self._sanitize_key(key)
        sentinel = self._to_sentinel(scope_id)
        with SessionLocal() as db:
            rows = (
                db.query(MemoryRelationshipModel)
                .filter(
                    MemoryRelationshipModel.scope == scope,
                    MemoryRelationshipModel.scope_id == sentinel,
                    or_(
                        MemoryRelationshipModel.source_key == k,
                        MemoryRelationshipModel.target_key == k,
                    ),
                )
                .all()
            )
            if not rows:
                return 0
            for r in rows:
                db.delete(r)
            db.commit()
        self._audit_purge(scope, sentinel, k, len(rows))
        return len(rows)

    # ------------------------------------------------------------------ #
    # read operations
    # ------------------------------------------------------------------ #
    def _source_updated_map(
        self, db: Any, scope: str, scope_id: Optional[str], source_keys: set
    ) -> Dict[str, datetime]:
        """Batch-load source memories' updated_at for staleness (avoid N+1)."""
        if not source_keys:
            return {}
        q = db.query(MemoryMetadataModel).filter(
            MemoryMetadataModel.scope == scope,
            MemoryMetadataModel.key.in_(list(source_keys)),
        )
        if scope_id is not None:
            q = q.filter(MemoryMetadataModel.scope_id == scope_id)
        else:
            q = q.filter(MemoryMetadataModel.scope_id.is_(None))
        return {m.key: m.updated_at for m in q.all() if m.updated_at is not None}

    def list_relationships(
        self,
        scope: str,
        scope_id: Optional[str] = None,
        source_key: Optional[str] = None,
        *,
        status: Optional[Any] = None,
        types: Optional[List[str]] = None,
        stale_only: bool = False,
        include_non_active: bool = False,
        source_keys: Optional[List[str]] = None,
    ) -> List[RelationshipDTO]:
        """Query relationships. Default returns ACTIVE only (FR-4.3); an explicit
        ``status`` or ``include_non_active`` widens. Computes the derived
        ``stale`` flag; ``stale_only`` filters to stale rows.

        ``source_keys`` bounds the rows FETCHED to edges leaving that set of
        source keys — used by the graph provider to scope its query to the
        current node set instead of loading every active relationship in the
        scope. It is a fetch bound ONLY: it can constrain the source side but
        says nothing about targets, so a caller that needs both endpoints inside
        a node set MUST still check the target side itself (an edge may
        legitimately point at a key outside the set).
        """
        sentinel = self._to_sentinel(scope_id)
        with SessionLocal() as db:
            q = db.query(MemoryRelationshipModel).filter(
                MemoryRelationshipModel.scope == scope,
                MemoryRelationshipModel.scope_id == sentinel,
            )
            if source_key is not None:
                q = q.filter(MemoryRelationshipModel.source_key == self._sanitize_key(source_key))
            if source_keys is not None:
                # An EMPTY set means "no nodes", which must yield no edges — not
                # "unfiltered". in_([]) is the correct, empty-result filter.
                #
                # A key that cannot sanitize (e.g. a malformed index entry like
                # "...") is DROPPED from the filter rather than allowed to raise.
                # Every stored source_key was sanitized at write time, so an
                # unsanitizable key can never match a row — but raising here
                # would abort the whole query, and the graph provider's except
                # branch would then silently discard EVERY edge in the scope
                # because of one bad index entry.
                sanitized_sources = []
                for k in source_keys:
                    try:
                        sanitized_sources.append(self._sanitize_key(k))
                    except ValueError:
                        continue
                q = q.filter(MemoryRelationshipModel.source_key.in_(sanitized_sources))
            if status is not None:
                if isinstance(status, (list, tuple, set)):
                    q = q.filter(MemoryRelationshipModel.status.in_(list(status)))
                else:
                    q = q.filter(MemoryRelationshipModel.status == status)
            elif not include_non_active:
                q = q.filter(MemoryRelationshipModel.status == "active")
            if types:
                q = q.filter(MemoryRelationshipModel.type.in_(list(types)))
            rows = q.all()
            src_map = self._source_updated_map(db, scope, scope_id, {r.source_key for r in rows})
            dtos = [self._to_dto(r, src_map.get(r.source_key)) for r in rows]
        if stale_only:
            dtos = [d for d in dtos if d.stale]
        return dtos

    def get(self, id: str) -> Optional[RelationshipDTO]:
        with SessionLocal() as db:
            row = db.query(MemoryRelationshipModel).filter(MemoryRelationshipModel.id == id).first()
            if row is None:
                return None
            src_map = self._source_updated_map(
                db, row.scope, self._from_sentinel(row.scope_id), {row.source_key}
            )
            return self._to_dto(row, src_map.get(row.source_key))

    def active_targets(
        self,
        scope: str,
        scope_id: Optional[str],
        source_key: str,
        type: str = "relates_to",
    ) -> List[str]:
        """Active edge targets of ``type`` from the source, in (rank, created_at)
        order — the See-Also / recall projection helper (FR-4.1/4.2)."""
        sentinel = self._to_sentinel(scope_id)
        with SessionLocal() as db:
            rows = (
                db.query(MemoryRelationshipModel)
                .filter(
                    MemoryRelationshipModel.scope == scope,
                    MemoryRelationshipModel.scope_id == sentinel,
                    MemoryRelationshipModel.source_key == self._sanitize_key(source_key),
                    MemoryRelationshipModel.type == type,
                    MemoryRelationshipModel.status == "active",
                )
                .all()
            )
        rows.sort(key=_rank_then_created)
        return [r.target_key for r in rows]

    def active_targets_for(
        self,
        scope: str,
        scope_id: Optional[str],
        source_keys: List[str],
        type: str = "relates_to",
    ) -> Dict[str, List[str]]:
        """BATCH form of ``active_targets``: the ordered active targets of
        ``type`` for MANY source keys in one ``(scope, scope_id)``, in ONE query.

        Mirrors the ``superseded_targets`` batching precedent. Recall's one-level
        expansion groups its primaries by ``(scope, scope_id)`` already, so one
        call per group replaces one query per primary (the N+1 this fixes).

        Returns ``{source_key: [target_key, ...]}`` keyed by the CALLER's
        original key strings, with each list in the same ``(rank, created_at)``
        order single-key ``active_targets`` returns. A source key with no active
        edges is ABSENT from the mapping (callers use ``.get(key, [])`` and treat
        absence as "the store has nothing", which is what triggers the legacy
        ``related_keys`` fallback).
        """
        if not source_keys:
            return {}
        sentinel = self._to_sentinel(scope_id)
        # Map sanitized -> caller's original, as superseded_targets does. A dict
        # also collapses duplicate inputs, so the IN list carries no repeats.
        sanitized = {self._sanitize_key(k): k for k in source_keys}
        with SessionLocal() as db:
            rows = (
                db.query(MemoryRelationshipModel)
                .filter(
                    MemoryRelationshipModel.scope == scope,
                    MemoryRelationshipModel.scope_id == sentinel,
                    MemoryRelationshipModel.type == type,
                    MemoryRelationshipModel.status == "active",
                    MemoryRelationshipModel.source_key.in_(list(sanitized.keys())),
                )
                .all()
            )
        grouped: Dict[str, List[Any]] = {}
        for row in rows:
            original = sanitized.get(row.source_key)
            if original is None:  # pragma: no cover - IN filter makes this unreachable
                continue
            grouped.setdefault(original, []).append(row)
        out: Dict[str, List[str]] = {}
        for original, group in grouped.items():
            group.sort(key=_rank_then_created)
            out[original] = [r.target_key for r in group]
        return out

    def is_superseded(self, scope: str, scope_id: Optional[str], key: str) -> bool:
        """True iff an ACTIVE ``supersedes`` edge TARGETS ``key`` (FR-4.6 ranking
        input): some memory supersedes this one, so it must not outrank active
        guidance."""
        sentinel = self._to_sentinel(scope_id)
        with SessionLocal() as db:
            hit = (
                db.query(MemoryRelationshipModel)
                .filter(
                    MemoryRelationshipModel.scope == scope,
                    MemoryRelationshipModel.scope_id == sentinel,
                    MemoryRelationshipModel.target_key == self._sanitize_key(key),
                    MemoryRelationshipModel.type == "supersedes",
                    MemoryRelationshipModel.status == "active",
                )
                .first()
            )
        return hit is not None

    def superseded_targets(self, scope: str, scope_id: Optional[str], keys: List[str]) -> set:
        """BATCH form of is_superseded (FR-4.6): given many candidate keys in one
        (scope, scope_id), return the subset that are the TARGET of an ACTIVE
        supersedes edge — in ONE query (avoids the N-query loop in a large recall
        ranking pass)."""
        if not keys:
            return set()
        sentinel = self._to_sentinel(scope_id)
        sanitized = {self._sanitize_key(k): k for k in keys}
        with SessionLocal() as db:
            rows = (
                db.query(MemoryRelationshipModel.target_key)
                .filter(
                    MemoryRelationshipModel.scope == scope,
                    MemoryRelationshipModel.scope_id == sentinel,
                    MemoryRelationshipModel.type == "supersedes",
                    MemoryRelationshipModel.status == "active",
                    MemoryRelationshipModel.target_key.in_(list(sanitized.keys())),
                )
                .all()
            )
        # Map sanitized targets back to the caller's original key strings.
        return {sanitized[r[0]] for r in rows if r[0] in sanitized}

    def contradictions_for(
        self, scope: str, scope_id: Optional[str], key: str
    ) -> List[RelationshipDTO]:
        """Active contradictions touching ``key`` from EITHER endpoint (FR-4.5
        symmetric visibility, reconstructed at QUERY time — the store holds one
        directed row, never a reciprocal duplicate).

        NO PRODUCTION CALLER YET (human review, PR #524). Retained rather than
        deleted because it is the only implementation of FR-4.5's symmetric read
        and is covered by tests: the graph provider projects each directed row
        as-is, so the "contradictions touching this key" question has no other
        answer in the codebase. Kept as the intended entry point for the
        contradiction-review surface; delete it if that surface is dropped rather
        than letting a second copy of this query appear elsewhere."""
        from sqlalchemy import or_

        sentinel = self._to_sentinel(scope_id)
        sk = self._sanitize_key(key)
        with SessionLocal() as db:
            rows = (
                db.query(MemoryRelationshipModel)
                .filter(
                    MemoryRelationshipModel.scope == scope,
                    MemoryRelationshipModel.scope_id == sentinel,
                    MemoryRelationshipModel.type == "contradiction",
                    MemoryRelationshipModel.status == "active",
                    or_(
                        MemoryRelationshipModel.source_key == sk,
                        MemoryRelationshipModel.target_key == sk,
                    ),
                )
                .all()
            )
            return [self._to_dto(r) for r in rows]
