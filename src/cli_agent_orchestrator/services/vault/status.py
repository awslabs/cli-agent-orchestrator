"""Read-only status projection for a configured vault."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Optional

from cli_agent_orchestrator.clients.database import SessionLocal, VaultFindingModel, VaultNoteModel
from cli_agent_orchestrator.services.vault.binding import (
    collect_binding_warnings,
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
            warnings = list(config.warnings)
            warnings.extend(warning.detail for warning in collect_binding_warnings(config))
            statuses.append(
                VaultStatus(
                    vault.id,
                    tuple(sorted(Counter(note.status for note in notes).items())),
                    tuple(sorted(Counter(finding.code for finding in findings).items())),
                    tuple(dict.fromkeys(warnings)),
                    unmapped_project_write_count(),
                )
            )
    return tuple(statuses)
