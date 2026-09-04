"""Read-only status projection for a configured vault."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Optional, cast

from cli_agent_orchestrator.clients.database import (
    SessionLocal,
    VaultFindingModel,
    VaultNoteModel,
    VaultRecallCounterModel,
)
from cli_agent_orchestrator.services.vault.binding import (
    collect_binding_warnings,
    non_writable_write_refusal_count,
    secret_gate_write_refusal_count,
    unmapped_project_identity_count,
    unmapped_project_write_count,
)
from cli_agent_orchestrator.services.vault.config import VaultConfig


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
            warnings = list(config.warnings)
            warnings.extend(warning.detail for warning in collect_binding_warnings(config))
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
                )
            )
    return tuple(statuses)
