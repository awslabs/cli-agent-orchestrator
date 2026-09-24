"""Dry-run-first migration from native CAO memory into a managed vault folder.

Migration reads native history directly rather than calling ``recall()``:
recall increments access metadata, so using it for a dry-run would mutate the
same ``access_count`` field the migration reports as lossy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, cast

from cli_agent_orchestrator.clients.database import (
    VAULT_NOTE_SCOPE_ID_SENTINEL,
    MemoryMetadataModel,
    VaultMigrationReceiptModel,
    VaultNoteModel,
)
from cli_agent_orchestrator.constants import MEMORY_BASE_DIR
from cli_agent_orchestrator.models.relationship import (
    VALID_ORIGINS,
    VALID_STATUSES,
    VALID_TYPES,
)
from cli_agent_orchestrator.services.memory_relationship_service import (
    EdgeIdentity,
    MemoryRelationshipService,
    RelationshipDTO,
)
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import VaultSpec
from cli_agent_orchestrator.services.vault.parser import MAX_CAO_LINKS
from cli_agent_orchestrator.services.vault.reconcile import reconcile
from cli_agent_orchestrator.services.vault.vault_lock import vault_projection_lock
from cli_agent_orchestrator.services.vault.writer import write_managed_note

_HISTORY_HEADING = re.compile(r"(?m)^## \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\n")
_LOSSY_FIELDS = (
    "access_count",
    "last_accessed_at",
    "last_compiled_at",
    "source_provider",
    "source_terminal_id",
    "related_keys",
)
_HISTORY_LOSS = "append_only_section_history"
_LINKS_LOSS = "cao.links"


@dataclass
class MigrationReport:
    """Content-free result of migrating one native memory scope."""

    planned: int = 0
    migrated: int = 0
    deleted_source: int = 0
    failed: int = 0
    dry_run: bool = True
    errors: dict[str, str] = field(default_factory=dict)
    lossy_fields: dict[str, dict[str, int]] = field(default_factory=dict)
    skipped_vault_authoritative: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _NativeSnapshot:
    """Exact native bytes and their safe path relative to the memory base."""

    relpath: str
    sha256: str
    text: str


def migrate_scope(
    memory_service: MemoryService,
    vault: VaultSpec,
    binding: VaultBinding,
    *,
    scope: str,
    scope_id: Optional[str],
    apply: bool = False,
    delete_source: bool = False,
    confirm_delete_source: bool = False,
    relationship_service: Optional[MemoryRelationshipService] = None,
    refresh: Optional[Callable[[str], None]] = None,
) -> MigrationReport:
    """Migrate native rows in one scope, without deleting them by default.

    This function intentionally owns no vault filesystem sink. Publication is
    delegated exclusively to ``write_managed_note``; the native source is read
    through ``MemoryService``'s canonical native wiki path.
    """
    _validate_delete_options(apply, delete_source, confirm_delete_source)
    if binding.scope != scope or binding.scope_id != scope_id:
        raise ValueError("vault binding does not match migration scope")

    lock = vault_projection_lock(vault) if apply else nullcontext()
    with lock:
        rows = _native_rows(memory_service, scope, scope_id)
        report = MigrationReport(planned=len(rows), dry_run=not apply)
        relationships = relationship_service or MemoryRelationshipService()
        refresh_note: Callable[[str], None]
        if refresh is None:

            def refresh_projection(_path: str) -> None:
                reconcile(vault, apply=True)

            refresh_note = refresh_projection
        else:
            refresh_note = refresh

        for row in rows:
            row_key = cast(str, row.key)
            row_scope = cast(str, row.scope)
            row_scope_id = cast(Optional[str], row.scope_id)
            try:
                native = _read_native_snapshot(memory_service, row)
            except Exception as exc:
                report.failed += 1
                report.errors[row_key] = str(exc)
                continue

            receipt_id = _receipt_id(vault.id, row_scope, row_scope_id, row_key)
            receipt = _receipt_for(memory_service, receipt_id)
            if receipt is not None:
                if receipt.status != "active":
                    report.skipped_vault_authoritative[row_key] = "receipt_not_active"
                    continue
                if native.sha256 != receipt.native_snapshot_sha256:
                    report.skipped_vault_authoritative[row_key] = "native_source_drift"
                    continue
                if not _target_matches_migration_baseline(
                    memory_service,
                    receipt,
                ):
                    report.skipped_vault_authoritative[row_key] = "target_content_drift"
                    continue
                report.skipped_vault_authoritative[row_key] = "already_migrated"
                continue

            try:
                all_paired_links = _links_for(relationships, row)
                paired_links = all_paired_links[:MAX_CAO_LINKS]
                relationship_rows = [item[0] for item in paired_links]
                links = [item[1] for item in paired_links]
                body, history_loss = _native_history(
                    native.text,
                    max_body_bytes=vault.max_note_bytes - vault.max_frontmatter_bytes,
                )
            except Exception as exc:
                report.failed += 1
                report.errors[row_key] = str(exc)
                continue
            losses = _losses_for(row, history_loss, len(all_paired_links))
            if losses:
                report.lossy_fields[row_key] = losses
            if not apply:
                continue

            try:
                write = write_managed_note(
                    vault=vault,
                    binding=binding,
                    key=row.key,
                    body=body,
                    cao={"type": row.memory_type, "links": links},
                    frontmatter={"tags": _tags(row.tags), "created": row.created_at},
                    expected_content_sha256=None,
                    refresh=refresh_note,
                )
                _record_receipt(
                    memory_service,
                    row=row,
                    receipt_id=receipt_id,
                    native=native,
                    vault=vault,
                    published_content_sha256=write.content_sha256,
                    copied_edges=relationship_rows,
                    relationship_service=relationships,
                )
            except Exception as exc:  # One bad source must not abort the migration corpus.
                report.failed += 1
                report.errors[row.key] = str(exc)
                continue

            report.migrated += 1
            if delete_source:
                try:
                    forgotten = asyncio.run(
                        memory_service.forget(
                            row.key,
                            scope=scope,
                            scope_id=scope_id,
                            target="native",
                        )
                    )
                    if forgotten.source_kind != "native":
                        raise RuntimeError("migration delete-source did not target native memory")
                    if forgotten:
                        report.deleted_source += 1
                except Exception as exc:  # Durable note remains observable as an item failure.
                    report.failed += 1
                    report.errors[row.key] = str(exc)
        return report


def _validate_delete_options(apply: bool, delete_source: bool, confirm_delete_source: bool) -> None:
    if delete_source and not apply:
        raise ValueError("--delete-source requires --apply")
    if delete_source and not confirm_delete_source:
        raise ValueError("--delete-source requires --confirm-delete-source")


def _native_rows(
    memory_service: MemoryService, scope: str, scope_id: Optional[str]
) -> list[MemoryMetadataModel]:
    with memory_service._get_db_session() as db:
        query = db.query(MemoryMetadataModel).filter(
            MemoryMetadataModel.scope == scope,
            MemoryMetadataModel.source_kind == "native",
        )
        if scope_id is None:
            query = query.filter(MemoryMetadataModel.scope_id.is_(None))
        else:
            query = query.filter(MemoryMetadataModel.scope_id == scope_id)
        return list(query.order_by(MemoryMetadataModel.key).all())


def _read_native_snapshot(
    memory_service: MemoryService,
    row: MemoryMetadataModel,
) -> _NativeSnapshot:
    source = Path(
        os.path.realpath(str(memory_service.get_wiki_path(row.scope, row.scope_id, row.key)))
    )
    memory_base = Path(os.path.realpath(str(MEMORY_BASE_DIR)))
    if not source.is_relative_to(memory_base):
        raise ValueError("native migration source escapes memory base")
    raw = source.read_bytes()
    return _NativeSnapshot(
        relpath=source.relative_to(memory_base).as_posix(),
        sha256=hashlib.sha256(raw).hexdigest(),
        text=raw.decode("utf-8"),
    )


def native_snapshot_inconsistency_reason(
    memory_base_dir: Path,
    native_relpath: str,
    expected_sha256: str,
) -> Optional[str]:
    """Validate one retained native snapshot inside the reviewed read-sink module."""
    base = Path(memory_base_dir).resolve()
    native_path = (base / native_relpath).resolve()
    if not native_path.is_relative_to(base):
        return "native_relpath_invalid"
    if not native_path.is_file():
        return "native_source_missing"
    if hashlib.sha256(native_path.read_bytes()).hexdigest() != expected_sha256:
        return "native_snapshot_mismatch"
    return None


def _native_history(text: str, *, max_body_bytes: int) -> tuple[str, int]:
    """Return native append-only history, reporting complete sections that do not fit."""
    starts = [match.start() for match in _HISTORY_HEADING.finditer(text)]
    if not starts:
        return text, 0
    sections = [
        text[start : starts[index + 1] if index + 1 < len(starts) else len(text)]
        for index, start in enumerate(starts)
    ]
    kept: list[str] = []
    used = 0
    for section in sections:
        size = len(section.encode("utf-8"))
        if used + size > max_body_bytes:
            break
        kept.append(section)
        used += size
    return "".join(kept), len(sections) - len(kept)


def _receipt_id(vault_id: str, scope: str, scope_id: Optional[str], key: str) -> str:
    identity = "\0".join((vault_id, scope, scope_id or "", key))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _receipt_for(
    memory_service: MemoryService,
    receipt_id: str,
) -> Optional[VaultMigrationReceiptModel]:
    with memory_service._get_db_session() as db:
        return cast(
            Optional[VaultMigrationReceiptModel], db.get(VaultMigrationReceiptModel, receipt_id)
        )


def _target_matches_migration_baseline(
    memory_service: MemoryService,
    receipt: VaultMigrationReceiptModel,
) -> bool:
    with memory_service._get_db_session() as db:
        note = db.get(VaultNoteModel, receipt.vault_note_uid)
        return bool(
            note is not None
            and note.vault_id == receipt.vault_id
            and note.scope == receipt.scope
            and note.scope_id == receipt.scope_id
            and note.cao_key == receipt.cao_key
            and note.vault_relpath == receipt.managed_relpath
            and note.status == "indexed"
            and note.content_sha256 == receipt.published_content_sha256
        )


def _record_receipt(
    memory_service: MemoryService,
    *,
    row: MemoryMetadataModel,
    receipt_id: str,
    native: _NativeSnapshot,
    vault: VaultSpec,
    published_content_sha256: str,
    copied_edges: list[RelationshipDTO],
    relationship_service: MemoryRelationshipService,
) -> None:
    stored_scope_id = row.scope_id or VAULT_NOTE_SCOPE_ID_SENTINEL
    managed_relpath = f"{vault.managed_folder}/{row.key}.md"
    audit_sink: list[Callable[[], None]] = []
    with memory_service._get_db_session() as db:
        with db.begin():
            if db.get(VaultMigrationReceiptModel, receipt_id) is not None:
                raise ValueError("receipt_already_recorded")
            note = (
                db.query(VaultNoteModel)
                .filter(
                    VaultNoteModel.vault_id == vault.id,
                    VaultNoteModel.scope == row.scope,
                    VaultNoteModel.scope_id == stored_scope_id,
                    VaultNoteModel.cao_key == row.key,
                    VaultNoteModel.vault_relpath == managed_relpath,
                    VaultNoteModel.status == "indexed",
                )
                .one()
            )
            recorded_edges: list[dict[str, str]] = []
            expected: dict[str, EdgeIdentity] = {}
            for copied in copied_edges:
                expected[copied.id] = (
                    copied.scope,
                    relationship_service._to_sentinel(copied.scope_id),
                    copied.source_key,
                    copied.target_key,
                    copied.type,
                    copied.origin,
                )
            prior_statuses = (
                relationship_service.supersede_ids(
                    [copied.id for copied in copied_edges],
                    expect=expected,
                    db=db,
                    audit_sink=audit_sink,
                )
                if copied_edges
                else {}
            )
            for copied in copied_edges:
                identity = expected[copied.id]
                recorded_edges.append(
                    {
                        "id": copied.id,
                        "prior_status": prior_statuses[copied.id],
                        "scope": identity[0],
                        "scope_id": identity[1],
                        "source_key": identity[2],
                        "target_key": identity[3],
                        "type": identity[4],
                        "origin": identity[5],
                    }
                )
            serialized_edges = _serialize_edge_snapshot(recorded_edges)
            db.add(
                VaultMigrationReceiptModel(
                    receipt_id=receipt_id,
                    scope=row.scope,
                    scope_id=stored_scope_id,
                    cao_key=row.key,
                    native_relpath=native.relpath,
                    native_snapshot_sha256=native.sha256,
                    vault_id=vault.id,
                    managed_relpath=managed_relpath,
                    vault_note_uid=note.note_uid,
                    published_content_sha256=published_content_sha256,
                    superseded_edges=serialized_edges,
                    status="active",
                )
            )
        for emit_audit in audit_sink:
            emit_audit()


def _links_for(
    relationships: MemoryRelationshipService, row: MemoryMetadataModel
) -> list[tuple[RelationshipDTO, dict[str, Any]]]:
    rows = relationships.list_relationships(
        row.scope,
        row.scope_id,
        source_key=row.key,
        include_non_active=True,
    )
    return [(dto, _link_from(dto)) for dto in rows]


_EDGE_SNAPSHOT_FIELDS = frozenset(
    {
        "id",
        "prior_status",
        "scope",
        "scope_id",
        "source_key",
        "target_key",
        "type",
        "origin",
    }
)


def parse_edge_snapshot(raw: str) -> list[dict[str, str]]:
    """Parse and validate the fixed-shape, content-free receipt edge snapshot."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("edge_snapshot_invalid") from exc
    if not isinstance(value, list) or len(value) > MAX_CAO_LINKS:
        raise ValueError("edge_snapshot_invalid")
    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != _EDGE_SNAPSHOT_FIELDS
            or any(not isinstance(item[field], str) for field in _EDGE_SNAPSHOT_FIELDS)
            or item["id"] in seen
            or item["type"] not in VALID_TYPES
            or item["origin"] not in VALID_ORIGINS
            or item["prior_status"] not in VALID_STATUSES
        ):
            raise ValueError("edge_snapshot_invalid")
        seen.add(item["id"])
        parsed.append(item)
    return parsed


def _serialize_edge_snapshot(snapshot: list[dict[str, str]]) -> str:
    parse_edge_snapshot(json.dumps(snapshot))
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def _link_from(dto: RelationshipDTO) -> dict[str, Any]:
    authored_origin = (dto.attributes or {}).get("authored_origin")
    link: dict[str, Any] = {
        "to": dto.target_key,
        "type": dto.type,
        "status": dto.status,
        "origin": (authored_origin if authored_origin in VALID_ORIGINS else dto.origin),
    }
    if dto.confidence is not None:
        link["confidence"] = dto.confidence
    return link


def _losses_for(row: MemoryMetadataModel, history_loss: int, link_count: int) -> dict[str, int]:
    losses: dict[str, int] = {}
    for name in _LOSSY_FIELDS:
        value = getattr(row, name)
        if value not in (None, 0, ""):
            losses[name] = 1
    if history_loss:
        losses[_HISTORY_LOSS] = history_loss
    if link_count > MAX_CAO_LINKS:
        losses[_LINKS_LOSS] = link_count - MAX_CAO_LINKS
    return losses


def _tags(value: str) -> str | list[str]:
    """Retain the native tag value in an ordinary Obsidian top-level field."""
    tags = [tag.strip() for tag in value.split(",") if tag.strip()]
    return tags if len(tags) > 1 else (tags[0] if tags else "")
