"""Dry-run-first migration from native CAO memory into a managed vault folder.

Migration reads native history directly rather than calling ``recall()``:
recall increments access metadata, so using it for a dry-run would mutate the
same ``access_count`` field the migration reports as lossy.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.clients.database import MemoryMetadataModel
from cli_agent_orchestrator.constants import MEMORY_BASE_DIR
from cli_agent_orchestrator.services.memory_relationship_service import (
    MemoryRelationshipService,
    RelationshipDTO,
)
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import VaultSpec
from cli_agent_orchestrator.services.vault.parser import MAX_CAO_LINKS
from cli_agent_orchestrator.services.vault.reconcile import reconcile
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

    rows = _native_rows(memory_service, scope, scope_id)
    report = MigrationReport(planned=len(rows), dry_run=not apply)
    relationships = relationship_service or MemoryRelationshipService()
    refresh_note = refresh or (lambda _path: reconcile(vault, apply=True))

    for row in rows:
        try:
            links = _links_for(relationships, row)
            body, history_loss = _native_history(
                memory_service,
                row,
                max_body_bytes=vault.max_note_bytes - vault.max_frontmatter_bytes,
            )
        except Exception as exc:
            report.failed += 1
            report.errors[row.key] = str(exc)
            continue
        losses = _losses_for(row, history_loss, len(links))
        if losses:
            report.lossy_fields[row.key] = losses
        if not apply:
            continue

        try:
            write_managed_note(
                vault=vault,
                binding=binding,
                key=row.key,
                body=body,
                cao={"type": row.memory_type, "links": links[:MAX_CAO_LINKS]},
                frontmatter={"tags": _tags(row.tags), "created": row.created_at},
                expected_content_sha256=None,
                refresh=refresh_note,
            )
        except Exception as exc:  # One bad source must not abort the migration corpus.
            report.failed += 1
            report.errors[row.key] = str(exc)
            continue

        report.migrated += 1
        if delete_source:
            try:
                if asyncio.run(memory_service.forget(row.key, scope=scope, scope_id=scope_id)):
                    report.deleted_source += 1
            except Exception as exc:  # Durable vault note remains observable as an item failure.
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


def _native_history(
    memory_service: MemoryService, row: MemoryMetadataModel, *, max_body_bytes: int
) -> tuple[str, int]:
    """Return native append-only history, reporting complete sections that do not fit."""
    source = os.path.realpath(str(memory_service.get_wiki_path(row.scope, row.scope_id, row.key)))
    memory_base = os.path.realpath(str(MEMORY_BASE_DIR))
    if not source.startswith(memory_base + os.sep):
        raise ValueError("native migration source escapes memory base")
    # Native source read: the guarded bare string is the value reaching this sink.
    text = Path(source).read_text(encoding="utf-8")
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


def _links_for(
    relationships: MemoryRelationshipService, row: MemoryMetadataModel
) -> list[dict[str, Any]]:
    rows = relationships.list_relationships(
        row.scope,
        row.scope_id,
        source_key=row.key,
        include_non_active=True,
    )
    return [_link_from(dto) for dto in rows]


def _link_from(dto: RelationshipDTO) -> dict[str, Any]:
    link: dict[str, Any] = {
        "to": dto.target_key,
        "type": dto.type,
        "status": dto.status,
        "origin": dto.origin,
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
