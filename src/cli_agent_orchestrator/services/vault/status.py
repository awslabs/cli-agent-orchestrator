"""Read-only status projection for a configured vault."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

from sqlalchemy.orm import Session

from cli_agent_orchestrator.clients.database import (
    VAULT_NOTE_SCOPE_ID_SENTINEL,
    MemoryMetadataModel,
    SessionLocal,
    VaultFindingModel,
    VaultMigrationReceiptModel,
    VaultNoteModel,
    VaultRecallCounterModel,
)
from cli_agent_orchestrator.constants import MEMORY_BASE_DIR
from cli_agent_orchestrator.services.vault.binding import (
    collect_binding_warnings,
    non_writable_write_refusal_count,
    secret_gate_write_refusal_count,
    unmapped_project_identity_count,
    unmapped_project_write_count,
)
from cli_agent_orchestrator.services.vault.config import VaultConfig, VaultSpec
from cli_agent_orchestrator.services.vault.writer import boundary_write_refusal_count


@dataclass(frozen=True)
class VaultStatus:
    """Content-free status including a process-local unmapped-write counter.

    Durable unmapped-write recording is owned by U8.
    """

    vault_id: str
    status_counts: tuple[tuple[str, int], ...]
    finding_counts: tuple[tuple[str, int], ...]
    warnings: tuple[str, ...]
    process_local_unmapped_project_writes: int
    process_local_unmapped_project_identities: int
    process_local_non_writable_write_refusals: int
    process_local_secret_gate_write_refusals: int
    recall_counters: tuple[tuple[str, int], ...] = ()
    inert_mappings: tuple[tuple[str, int], ...] = ()
    migration_receipts: tuple[tuple[str, int], ...] = ()
    process_local_boundary_write_refusals: int = 0


def migration_receipt_write_guard_reason(
    db: Session,
    receipt: VaultMigrationReceiptModel,
    vault: VaultSpec,
    *,
    memory_base_dir: Path = MEMORY_BASE_DIR,
) -> Optional[str]:
    """Return one of the five reasons a receipt cannot exempt a vault write."""
    if receipt.vault_id != vault.id:
        return "target_vault_mismatch"
    if receipt.managed_relpath != f"{vault.managed_folder}/{receipt.cao_key}.md":
        return "managed_relpath_mismatch"
    from cli_agent_orchestrator.services.vault.migrate import (
        native_snapshot_inconsistency_reason,
    )

    native_reason = native_snapshot_inconsistency_reason(
        memory_base_dir,
        cast(str, receipt.native_relpath),
        cast(str, receipt.native_snapshot_sha256),
    )
    if native_reason is not None:
        return native_reason
    note = db.get(VaultNoteModel, receipt.vault_note_uid)
    if note is None:
        return "target_note_missing"
    if not (
        note.vault_id == receipt.vault_id
        and note.scope == receipt.scope
        and note.scope_id == receipt.scope_id
        and note.cao_key == receipt.cao_key
        and note.vault_relpath == receipt.managed_relpath
    ):
        return "target_note_identity_mismatch"
    if note.status != "indexed":
        return "target_note_not_indexed"
    logical_scope_id = (
        None if receipt.scope_id == VAULT_NOTE_SCOPE_ID_SENTINEL else receipt.scope_id
    )
    native = db.query(MemoryMetadataModel).filter(
        MemoryMetadataModel.key == receipt.cao_key,
        MemoryMetadataModel.scope == receipt.scope,
        MemoryMetadataModel.source_kind == "native",
        (
            MemoryMetadataModel.scope_id == logical_scope_id
            if logical_scope_id is not None
            else MemoryMetadataModel.scope_id.is_(None)
        ),
    )
    if native.one_or_none() is None:
        return "native_row_missing"
    return None


def migration_receipt_inconsistency_reason(
    db: Session,
    receipt: VaultMigrationReceiptModel,
    vault: VaultSpec,
    *,
    memory_base_dir: Path = MEMORY_BASE_DIR,
) -> Optional[str]:
    """Return write-guard or rollback-snapshot drift for status reporting."""
    guard_reason = migration_receipt_write_guard_reason(
        db,
        receipt,
        vault,
        memory_base_dir=memory_base_dir,
    )
    if guard_reason is not None:
        return guard_reason
    try:
        from cli_agent_orchestrator.services.vault.migrate import parse_edge_snapshot

        snapshots = parse_edge_snapshot(cast(str, receipt.superseded_edges))
    except ValueError:
        return "edge_snapshot_invalid"
    from cli_agent_orchestrator.services.memory_relationship_service import (
        MemoryRelationshipService,
    )

    expected = {
        item["id"]: (
            item["scope"],
            item["scope_id"],
            item["source_key"],
            item["target_key"],
            item["type"],
            item["origin"],
        )
        for item in snapshots
    }
    drift = MemoryRelationshipService().preflight_edge_identities(
        expected,
        require_status="superseded",
        db=db,
    )
    if drift:
        return drift[sorted(drift)[0]]
    return None


def get_vault_status(
    config: VaultConfig, *, vault_id: Optional[str] = None
) -> tuple[VaultStatus, ...]:
    """Return status from live config and process-local binding observations."""
    statuses = []
    with SessionLocal() as db:
        for vault in config.vaults:
            if vault_id is not None and vault.id != vault_id:
                continue
            notes = db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault.id).all()
            findings = (
                db.query(VaultFindingModel).filter(VaultFindingModel.vault_id == vault.id).all()
            )
            counters = (
                db.query(VaultRecallCounterModel)
                .filter(VaultRecallCounterModel.vault_id == vault.id)
                .all()
            )
            mapping_identities = {
                (mapping.scope, mapping.scope_id or VAULT_NOTE_SCOPE_ID_SENTINEL)
                for mapping in vault.mappings
            }
            receipts = [
                receipt
                for receipt in db.query(VaultMigrationReceiptModel)
                .filter(VaultMigrationReceiptModel.status == "active")
                .all()
                if receipt.vault_id == vault.id
                or (
                    cast(str, receipt.scope),
                    cast(str, receipt.scope_id),
                )
                in mapping_identities
            ]
            warnings = list(config.warnings)
            warnings.extend(warning.detail for warning in collect_binding_warnings(config))
            inconsistent = 0
            for receipt in receipts:
                reason = migration_receipt_inconsistency_reason(
                    db,
                    receipt,
                    vault,
                    memory_base_dir=MEMORY_BASE_DIR,
                )
                if reason is None:
                    continue
                inconsistent += 1
                warnings.append(
                    "migration_receipt "
                    f"receipt={receipt.receipt_id} "
                    f"identity={receipt.scope}:{receipt.scope_id or '-'}:{receipt.cao_key} "
                    f"reason={reason}"
                )
            inert_mappings = []
            for mapping in vault.mappings:
                if mapping.index:
                    continue
                stored_scope_id = mapping.scope_id or ""
                residual_rows = sum(
                    1
                    for note in notes
                    if cast(str, note.scope) == mapping.scope
                    and cast(str, note.scope_id) == stored_scope_id
                )
                label = (
                    f"{mapping.folder} ({mapping.scope}"
                    f"{':' + mapping.scope_id if mapping.scope_id else ''}) "
                    "inert: recall=off inject=off write=off"
                )
                inert_mappings.append((label, residual_rows))
            statuses.append(
                VaultStatus(
                    vault.id,
                    tuple(sorted(Counter(cast(str, note.status) for note in notes).items())),
                    tuple(sorted(Counter(cast(str, finding.code) for finding in findings).items())),
                    tuple(dict.fromkeys(warnings)),
                    unmapped_project_write_count(),
                    unmapped_project_identity_count(),
                    non_writable_write_refusal_count(vault.id),
                    secret_gate_write_refusal_count(vault.id),
                    tuple(
                        sorted(
                            (cast(str, counter.counter_name), cast(int, counter.value))
                            for counter in counters
                        )
                    ),
                    tuple(sorted(inert_mappings)),
                    (
                        ("active", len(receipts)),
                        ("inconsistent", inconsistent),
                    ),
                    boundary_write_refusal_count(vault.id),
                )
            )
    return tuple(statuses)
