"""Deterministic, scoped projection of a scanned vault into derived state."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, cast

from cli_agent_orchestrator.clients.database import (
    MemoryMetadataModel,
    MemoryRelationshipModel,
    SessionLocal,
    VaultFindingModel,
    VaultNoteAliasModel,
    VaultNoteModel,
)
from cli_agent_orchestrator.models.memory import MemoryType
from cli_agent_orchestrator.models.relationship import VALID_TYPES
from cli_agent_orchestrator.services.memory_relationship_service import (
    MAX_EDGES_PER_MUTATION,
    EdgeInput,
    MemoryRelationshipService,
)
from cli_agent_orchestrator.services.vault.config import VaultSpec
from cli_agent_orchestrator.services.vault.findings import FindingCode, finding_severity
from cli_agent_orchestrator.services.vault.identity import cao_key, derive_note_uid
from cli_agent_orchestrator.services.vault.links import (
    LinkCandidate,
    extract_wikilinks,
    resolve_wikilink,
)
from cli_agent_orchestrator.services.vault.scan import ScanFinding, ScanNote, scan_vault


@dataclass(frozen=True)
class ReconcilePlan:
    """A stable, side-effect-free reconciliation decision."""

    vault_id: str
    notes: tuple[ScanNote, ...]
    run_id: str
    run_started_at: datetime


@dataclass(frozen=True)
class ReconcileReport:
    """Frozen, content-free reconciliation result."""

    vault_id: str
    run_id: str
    indexed: int
    quarantined: int
    skipped: int
    findings: int
    deleted: int
    rebuilt: bool

    @property
    def has_unresolved(self) -> bool:
        return self.quarantined > 0 or self.skipped > 0


@dataclass(frozen=True)
class _ProjectedNote:
    note: ScanNote
    key: str
    canonical_key: str
    note_uid: str
    memory_id: str
    managed: bool
    memory_type: str
    tags: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class _RenameResolution:
    """A conservative identity decision for a newly appeared path."""

    item: _ProjectedNote
    retained_former_path: Optional[str] = None
    alias_from: Optional[VaultNoteModel] = None
    findings: tuple[tuple[str, str, str, str], ...] = ()


def plan_reconcile(
    vault: VaultSpec,
    *,
    run_id: Optional[str] = None,
    run_started_at: Optional[datetime] = None,
) -> ReconcilePlan:
    """Scan once and capture the only run-wide provenance values."""
    started = run_started_at or datetime.now(timezone.utc)
    stable_run_id = (
        run_id or hashlib.sha256(f"{vault.id}\0{started.isoformat()}".encode("utf-8")).hexdigest()
    )
    return ReconcilePlan(vault.id, scan_vault(vault).notes, stable_run_id, started)


def reconcile(
    vault: VaultSpec,
    *,
    apply: bool = False,
    rebuild: bool = False,
    run_id: Optional[str] = None,
    run_started_at: Optional[datetime] = None,
) -> ReconcileReport:
    """Plan and optionally apply a deterministic vault projection.

    ``vault_finding`` is intentionally one row per ``(code, vault_relpath)``
    in a run. Multiple instances are folded into its content-free ``detail``
    count so the primary key derived from run/code/path cannot collide.
    """
    plan = plan_reconcile(vault, run_id=run_id, run_started_at=run_started_at)
    projected = _quarantine_key_collisions(
        tuple(_project_note(vault, note, plan.run_started_at) for note in plan.notes)
    )
    findings = _group_findings(projected)
    indexed = sum(note.note.status == "indexed" for note in projected)
    quarantined = sum(note.note.status == "quarantined" for note in projected)
    skipped = sum(note.note.status in {"skipped", "unsupported"} for note in projected)
    deleted = 0

    if apply:
        with SessionLocal() as db:
            with db.begin():
                if not rebuild:
                    _clear_stale_vault_edges(db, vault.id, projected)
                deleted, findings, projected = _apply_plan(
                    db, vault, plan, projected, findings, rebuild=rebuild
                )
                findings = _merge_findings(findings, _replace_vault_edges(projected, db=db) or ())
                _persist_findings(db, vault.id, plan, findings)
        _emit_audit_events(vault.id, plan.run_id, projected, indexed, quarantined, skipped)
    else:
        findings, projected = _preview_rename_findings(vault.id, projected, findings)
        _edge_groups, edge_findings = _project_vault_edges(projected)
        findings = _merge_findings(findings, edge_findings)

    return ReconcileReport(
        vault.id,
        plan.run_id,
        indexed,
        quarantined,
        skipped,
        len(findings),
        deleted,
        rebuild,
    )


def _preview_rename_findings(
    vault_id: str,
    projected: tuple[_ProjectedNote, ...],
    findings: tuple[tuple[str, str, str, str], ...],
) -> tuple[
    tuple[tuple[str, str, str, str], ...],
    tuple[_ProjectedNote, ...],
]:
    """Run rename classification against current rows without changing derived state."""
    with SessionLocal() as db:
        prior_by_path: dict[str, VaultNoteModel] = {
            cast(str, row.vault_relpath): row
            for row in db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault_id).all()
        }
        carried_alias_keys = _alias_identity_set(db, vault_id)
    resolutions = _resolve_renames(
        vault_id, projected, prior_by_path, carried_alias_keys=carried_alias_keys
    )
    return (
        _merge_findings(
            findings,
            tuple(finding for resolution in resolutions for finding in resolution.findings),
        ),
        tuple(resolution.item for resolution in resolutions),
    )


def _clear_stale_vault_edges(db, vault_id: str, projected: tuple[_ProjectedNote, ...]) -> None:
    """Retract removed or non-indexed sources through the relationship boundary."""
    prior_by_path: dict[str, VaultNoteModel] = {
        cast(str, row.vault_relpath): row
        for row in db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault_id).all()
    }
    carried_alias_keys = _alias_identity_set(db, vault_id)
    resolutions = _resolve_renames(
        vault_id, projected, prior_by_path, carried_alias_keys=carried_alias_keys
    )
    retained = {
        resolution.retained_former_path
        for resolution in resolutions
        if resolution.retained_former_path is not None
    }
    current = {item.note.vault_relpath for item in projected}
    removed = tuple(
        row
        for path, row in prior_by_path.items()
        if path not in current and path not in retained and row.status == "indexed"
    )
    retracted = tuple(
        prior_by_path[item.note.vault_relpath]
        for item in projected
        if item.note.status != "indexed"
        and item.note.vault_relpath in prior_by_path
        and prior_by_path[item.note.vault_relpath].status == "indexed"
    )
    service = MemoryRelationshipService()
    for row in removed + retracted:
        service.purge_for_key(
            cast(str, row.scope),
            None if cast(str, row.scope_id) == "" else cast(str, row.scope_id),
            cast(str, row.cao_key),
            origins=("vault",),
            db=db,
        )


def rebuild(vault: VaultSpec, *, run_id: Optional[str] = None) -> ReconcileReport:
    """Delete only vault-derived state and then reconcile it from the vault."""
    return reconcile(vault, apply=True, rebuild=True, run_id=run_id)


def _project_note(vault: VaultSpec, note: ScanNote, started: datetime) -> _ProjectedNote:
    mapping_relative = note.vault_relpath
    for mapping in vault.mappings:
        prefix = mapping.folder + "/"
        if (
            note.scope == mapping.scope
            and note.scope_id == mapping.scope_id
            and (note.vault_relpath == mapping.folder or note.vault_relpath.startswith(prefix))
        ):
            mapping_relative = (
                note.vault_relpath[len(prefix) :] if note.vault_relpath.startswith(prefix) else ""
            )
            break
    authored = note.parsed.cao.get("key") if note.parsed is not None else None
    key = cao_key(authored, mapping_relative)
    note_uid = derive_note_uid(vault.id, note.scope, note.scope_id, key)
    frontmatter = note.parsed.frontmatter if note.parsed is not None else {}
    cao = note.parsed.cao if note.parsed is not None else {}
    timestamp = _from_mtime(note.mtime_ns, started)
    return _ProjectedNote(
        note,
        key,
        key,
        note_uid,
        _digest("memory", note_uid),
        bool(cao.get("managed", False)),
        str(cao.get("type", MemoryType.REFERENCE.value)),
        _tags(frontmatter.get("tags")),
        _frontmatter_time(frontmatter.get("created"), timestamp),
        timestamp,
    )


def _quarantine_key_collisions(
    projected: tuple[_ProjectedNote, ...],
) -> tuple[_ProjectedNote, ...]:
    """Quarantine every note that shares a minted identity; never overwrite one."""
    collisions = Counter(item.note_uid for item in projected)
    return tuple(
        (
            item
            if collisions[item.note_uid] == 1
            else replace(
                item,
                key=f"{item.canonical_key}-collision-{_digest(item.note.vault_relpath)[:8]}",
                note_uid=_digest("collision", item.note_uid, item.note.vault_relpath),
                memory_id=_digest("memory", "collision", item.note_uid, item.note.vault_relpath),
                note=replace(
                    item.note,
                    status="quarantined",
                    findings=item.note.findings
                    + (
                        ScanFinding(
                            FindingCode.KEY_COLLISION,
                            "duplicate authored or derived cao.key",
                            finding_severity(FindingCode.KEY_COLLISION, secret_gate="reject"),
                        ),
                    ),
                ),
            )
        )
        for item in projected
    )


def _apply_plan(
    db,
    vault: VaultSpec,
    plan: ReconcilePlan,
    projected: tuple[_ProjectedNote, ...],
    findings: tuple[tuple[str, str, str, str], ...],
    *,
    rebuild: bool,
) -> tuple[int, tuple[tuple[str, str, str, str], ...], tuple[_ProjectedNote, ...]]:
    """Apply only vault-scoped deletes; native rows remain structurally untouched."""
    rebuild_excluded_paths: set[str] = set()
    if rebuild:
        rebuild_excluded_paths = {
            cast(str, row.vault_relpath)
            for row in db.query(VaultNoteModel)
            .filter(
                VaultNoteModel.vault_id == vault.id,
                VaultNoteModel.status == "excluded",
            )
            .all()
        }
        # Every rebuild delete is scoped to its derived producer. Release
        # one permits one configured vault, so metadata has no vault id.
        db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault.id).delete()
        db.query(VaultFindingModel).filter(VaultFindingModel.vault_id == vault.id).delete()
        db.query(VaultNoteAliasModel).filter(VaultNoteAliasModel.vault_id == vault.id).delete()
        db.query(MemoryMetadataModel).filter(MemoryMetadataModel.source_kind == "vault").delete()
        db.query(MemoryRelationshipModel).filter(MemoryRelationshipModel.origin == "vault").delete()
    else:
        db.query(VaultFindingModel).filter(VaultFindingModel.vault_id == vault.id).delete()

    prior_by_path: dict[str, VaultNoteModel] = {
        cast(str, row.vault_relpath): row
        for row in db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault.id).all()
    }
    carried_alias_keys = _alias_identity_set(db, vault.id)
    resolutions = _resolve_renames(
        vault.id,
        projected,
        prior_by_path,
        carried_alias_keys=carried_alias_keys,
    )
    if rebuild_excluded_paths:
        resolutions = tuple(
            (
                replace(
                    resolution,
                    item=replace(
                        resolution.item,
                        note=replace(resolution.item.note, status="excluded"),
                    ),
                    findings=resolution.findings
                    + (
                        _rename_finding(
                            FindingCode.DEINDEXED_RETAINED,
                            resolution.item.note.vault_relpath,
                        ),
                    ),
                )
                if resolution.item.note.vault_relpath in rebuild_excluded_paths
                and resolution.item.note.status == "indexed"
                else resolution
            )
            for resolution in resolutions
        )
    projected = tuple(resolution.item for resolution in resolutions)
    findings = _merge_findings(
        findings,
        tuple(finding for resolution in resolutions for finding in resolution.findings),
    )
    retained_paths = {
        resolution.retained_former_path
        for resolution in resolutions
        if resolution.retained_former_path is not None
    }
    current_paths = {item.note.vault_relpath for item in projected}
    deleted = 0
    for path, row in prior_by_path.items():
        if path not in current_paths and path not in retained_paths:
            _delete_projection(db, row)
            deleted += 1

    relationship_service = MemoryRelationshipService()
    for resolution in resolutions:
        item = resolution.item
        prior = prior_by_path.get(item.note.vault_relpath)
        if prior is None or prior.note_uid == item.note_uid:
            continue
        stored_scope = cast(str, prior.scope)
        stored_scope_id = cast(str, prior.scope_id)
        stored_key = cast(str, prior.cao_key)
        relationship_service.purge_for_key(
            stored_scope,
            None if stored_scope_id == "" else stored_scope_id,
            stored_key,
            origins=("vault",),
            db=db,
        )
        _delete_metadata_for_identity(db, stored_scope, stored_scope_id, stored_key)
        _delete_aliases_for_identity(db, vault.id, stored_scope, stored_scope_id, stored_key)
        db.delete(prior)
    db.flush()

    for resolution in resolutions:
        item = resolution.item
        if resolution.alias_from is not None:
            _record_rename_alias(db, vault.id, resolution.alias_from, plan.run_started_at)
        _upsert_note(db, vault.id, item, plan.run_started_at)
        if item.note.status == "indexed":
            _upsert_metadata(db, item)
        else:
            _delete_metadata_for_item(db, item)
    db.flush()
    _refresh_alias_provenance(db, vault.id, projected, plan.run_started_at)
    return deleted, findings, projected


def _persist_findings(
    db,
    vault_id: str,
    plan: ReconcilePlan,
    findings: tuple[tuple[str, str, str, str], ...],
) -> None:
    db.query(VaultFindingModel).filter(VaultFindingModel.vault_id == vault_id).delete()
    for code, path, severity, detail in findings:
        db.add(
            VaultFindingModel(
                id=_digest("finding", plan.run_id, code, path, severity),
                vault_id=vault_id,
                vault_relpath=path,
                code=code,
                severity=severity,
                detail=detail,
                reconcile_run_id=plan.run_id,
                created_at=plan.run_started_at,
            )
        )


def _delete_projection(db, row: VaultNoteModel) -> None:
    scope_id = None if row.scope_id == "" else row.scope_id
    db.query(MemoryMetadataModel).filter(
        MemoryMetadataModel.key == row.cao_key,
        MemoryMetadataModel.scope == row.scope,
        (
            MemoryMetadataModel.scope_id.is_(None)
            if scope_id is None
            else MemoryMetadataModel.scope_id == scope_id
        ),
        MemoryMetadataModel.source_kind == "vault",
    ).delete()
    db.delete(row)


def _delete_metadata_for_item(db, item: _ProjectedNote) -> None:
    """Retract only the vault projection for a now non-indexed note."""
    scope_filter = (
        MemoryMetadataModel.scope_id.is_(None)
        if item.note.scope_id is None
        else MemoryMetadataModel.scope_id == item.note.scope_id
    )
    db.query(MemoryMetadataModel).filter(
        MemoryMetadataModel.key == item.canonical_key,
        MemoryMetadataModel.scope == item.note.scope,
        scope_filter,
        MemoryMetadataModel.source_kind == "vault",
    ).delete()


def _delete_metadata_for_identity(db, scope: str, stored_scope_id: str, key: str) -> None:
    db.query(MemoryMetadataModel).filter(
        MemoryMetadataModel.key == key,
        MemoryMetadataModel.scope == scope,
        (
            MemoryMetadataModel.scope_id.is_(None)
            if stored_scope_id == ""
            else MemoryMetadataModel.scope_id == stored_scope_id
        ),
        MemoryMetadataModel.source_kind == "vault",
    ).delete()


def _delete_aliases_for_identity(
    db, vault_id: str, scope: str, stored_scope_id: str, key: str
) -> None:
    db.query(VaultNoteAliasModel).filter(
        VaultNoteAliasModel.vault_id == vault_id,
        VaultNoteAliasModel.cao_key == key,
        VaultNoteAliasModel.scope == scope,
        (
            VaultNoteAliasModel.scope_id.is_(None)
            if stored_scope_id == ""
            else VaultNoteAliasModel.scope_id == stored_scope_id
        ),
    ).delete()


def _alias_identity_set(db, vault_id: str) -> set[tuple[str, str, str]]:
    return {
        (
            cast(str, row.scope),
            cast(Optional[str], row.scope_id) or "",
            cast(str, row.cao_key),
        )
        for row in db.query(VaultNoteAliasModel)
        .filter(VaultNoteAliasModel.vault_id == vault_id)
        .all()
    }


def _resolve_renames(
    vault_id: str,
    projected: tuple[_ProjectedNote, ...],
    prior_by_path: dict[str, VaultNoteModel],
    *,
    carried_alias_keys: set[tuple[str, str, str]] | None = None,
) -> tuple[_RenameResolution, ...]:
    """Absorb only unambiguous pure renames; all other candidates remain new.

    A path-derived key is retained only for one same-scope former path with the
    exact same content hash.  This prevents a rename-plus-edit or duplicate
    content from silently changing identity.
    """
    carried_alias_keys = carried_alias_keys or set()
    appeared_paths = {
        item.note.vault_relpath
        for item in projected
        if item.note.vault_relpath not in prior_by_path
    }
    disappeared = tuple(
        row
        for path, row in prior_by_path.items()
        if path not in {item.note.vault_relpath for item in projected}
    )
    matching_by_path: dict[str, tuple[VaultNoteModel, ...]] = {}
    claims_by_former_path: Counter[str] = Counter()
    for item in projected:
        if item.note.vault_relpath not in appeared_paths:
            continue
        if item.note.parsed is not None and "key" in item.note.parsed.cao:
            continue
        matching = tuple(
            old
            for old in disappeared
            if old.scope == item.note.scope
            and old.scope_id == (item.note.scope_id or "")
            and old.content_sha256 == item.note.content_sha256
        )
        matching_by_path[item.note.vault_relpath] = matching
        for old in matching:
            claims_by_former_path[cast(str, old.vault_relpath)] += 1
    resolutions: list[_RenameResolution] = []
    for item in projected:
        if item.note.vault_relpath not in appeared_paths:
            existing = prior_by_path[item.note.vault_relpath]
            has_authored_key = item.note.parsed is not None and "key" in item.note.parsed.cao
            # A prior pure rename retained a path-derived key through the
            # alias table. Subsequent unchanged (or edited) scans must retain
            # that established identity instead of deriving a second one from
            # the new path and colliding on vault_relpath.
            existing_scope = cast(str, existing.scope)
            existing_scope_id = cast(str, existing.scope_id)
            existing_key = cast(str, existing.cao_key)
            carried_by_rename = (
                existing_scope,
                existing_scope_id,
                existing_key,
            ) in carried_alias_keys
            if not has_authored_key and (existing_key == item.key or carried_by_rename):
                item = replace(
                    item,
                    key=existing_key,
                    canonical_key=existing_key,
                    note_uid=cast(str, existing.note_uid),
                    memory_id=_digest("memory", cast(str, existing.note_uid)),
                )
            identity_changed = cast(str, existing.note_uid) != item.note_uid
            resolution_findings: tuple[tuple[str, str, str, str], ...] = ()
            if existing.status == "excluded" and item.note.status != "excluded":
                item = replace(item, note=replace(item.note, status="excluded"))
                resolution_findings = (
                    _rename_finding(
                        FindingCode.DEINDEXED_RETAINED,
                        item.note.vault_relpath,
                    ),
                )
            resolutions.append(_RenameResolution(item, findings=resolution_findings))
            continue
        same_scope = tuple(
            old
            for old in disappeared
            if old.scope == item.note.scope and old.scope_id == (item.note.scope_id or "")
        )
        has_authored_key = item.note.parsed is not None and "key" in item.note.parsed.cao
        matching = matching_by_path.get(item.note.vault_relpath, ())
        if has_authored_key:
            canonical = tuple(old for old in same_scope if old.note_uid == item.note_uid)
            if len(canonical) == 1 and canonical[0].status == "excluded":
                item = replace(item, note=replace(item.note, status="excluded"))
            resolutions.append(
                _RenameResolution(
                    item,
                    retained_former_path=(
                        cast(str, canonical[0].vault_relpath) if len(canonical) == 1 else None
                    ),
                )
            )
        elif (
            len(matching) == 1 and claims_by_former_path[cast(str, matching[0].vault_relpath)] == 1
        ):
            old = matching[0]
            old_key = cast(str, old.cao_key)
            old_note_uid = cast(str, old.note_uid)
            renamed_item = replace(
                item,
                key=old_key,
                note_uid=old_note_uid,
                memory_id=_digest("memory", old_note_uid),
            )
            rename_findings: tuple[tuple[str, str, str, str], ...] = ()
            if old.status == "excluded" and renamed_item.note.status != "excluded":
                renamed_item = replace(
                    renamed_item,
                    note=replace(renamed_item.note, status="excluded"),
                )
                rename_findings = (
                    _rename_finding(
                        FindingCode.DEINDEXED_RETAINED,
                        renamed_item.note.vault_relpath,
                    ),
                )
            resolutions.append(
                _RenameResolution(
                    renamed_item,
                    retained_former_path=cast(str, old.vault_relpath),
                    alias_from=old,
                    findings=rename_findings,
                )
            )
        elif len(matching) > 1 or any(
            claims_by_former_path[cast(str, old.vault_relpath)] > 1 for old in matching
        ):
            resolutions.append(
                _RenameResolution(
                    item,
                    findings=(
                        _rename_finding(FindingCode.RENAME_AMBIGUOUS, item.note.vault_relpath),
                    ),
                )
            )
        elif len(same_scope) == 1:
            resolutions.append(
                _RenameResolution(
                    item,
                    findings=(
                        _rename_finding(
                            FindingCode.RENAME_WITH_EDIT_UNRESOLVED,
                            item.note.vault_relpath,
                        ),
                    ),
                )
            )
        elif len(same_scope) > 1:
            resolutions.append(
                _RenameResolution(
                    item,
                    findings=(
                        _rename_finding(FindingCode.RENAME_AMBIGUOUS, item.note.vault_relpath),
                    ),
                )
            )
        else:
            resolutions.append(_RenameResolution(item))
    return tuple(resolutions)


def _record_rename_alias(db, vault_id: str, old: VaultNoteModel, created_at: datetime) -> None:
    alias = db.get(
        VaultNoteAliasModel,
        {"vault_id": vault_id, "former_relpath": old.vault_relpath},
    )
    if alias is None:
        alias = VaultNoteAliasModel(
            vault_id=vault_id,
            former_relpath=old.vault_relpath,
        )
        db.add(alias)
    alias.cao_key = old.cao_key
    alias.scope = old.scope
    alias.scope_id = None if old.scope_id == "" else old.scope_id
    alias.content_sha256 = old.content_sha256
    alias.created_at = created_at


def _refresh_alias_provenance(
    db, vault_id: str, projected: Iterable[_ProjectedNote], reconciled_at: datetime
) -> None:
    """Refresh run-minted provenance without changing alias identity columns."""
    refreshed = set()
    for item in projected:
        alias_scope_id = item.note.scope_id
        identity = (item.key, item.note.scope, alias_scope_id)
        if identity in refreshed:
            continue
        refreshed.add(identity)
        scope_filter = (
            VaultNoteAliasModel.scope_id.is_(None)
            if alias_scope_id is None
            else VaultNoteAliasModel.scope_id == alias_scope_id
        )
        db.query(VaultNoteAliasModel).filter(
            VaultNoteAliasModel.vault_id == vault_id,
            VaultNoteAliasModel.cao_key == item.key,
            VaultNoteAliasModel.scope == item.note.scope,
            scope_filter,
        ).update({"created_at": reconciled_at}, synchronize_session=False)


def _rename_finding(code: FindingCode, relpath: str) -> tuple[str, str, str, str]:
    return (
        code.value,
        relpath,
        finding_severity(code, secret_gate="reject"),
        code.value,
    )


def _merge_findings(
    findings: tuple[tuple[str, str, str, str], ...],
    additions: tuple[tuple[str, str, str, str], ...],
) -> tuple[tuple[str, str, str, str], ...]:
    """Fold additional findings into the same one-row-per-code-and-path invariant."""
    grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for code, path, severity, detail in findings + additions:
        if detail.startswith("count="):
            count_text, _, rest = detail.partition("; code=")
            count = int(count_text.removeprefix("count="))
            _, _, original_detail = rest.partition("; detail=")
            grouped[(code, path, severity)].extend([original_detail] * count)
        else:
            grouped[(code, path, severity)].append(detail)
    return tuple(
        (
            code,
            path,
            severity,
            f"count={len(details)}; code={code}; detail={','.join(sorted(set(details)))}",
        )
        for (code, path, severity), details in sorted(grouped.items())
    )


def _upsert_note(db, vault_id: str, item: _ProjectedNote, started: datetime) -> None:
    row = db.get(VaultNoteModel, item.note_uid)
    values = {
        "vault_id": vault_id,
        "scope": item.note.scope,
        "scope_id": item.note.scope_id or "",
        "cao_key": item.key,
        "vault_relpath": item.note.vault_relpath,
        "managed": item.managed,
        "content_sha256": item.note.content_sha256,
        "frontmatter_sha256": item.note.frontmatter_sha256,
        "size_bytes": item.note.size_bytes,
        "mtime_ns": item.note.mtime_ns,
        "status": item.note.status,
        "last_reconciled_at": started,
    }
    if row is None:
        db.add(VaultNoteModel(note_uid=item.note_uid, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)


def _upsert_metadata(db, item: _ProjectedNote) -> None:
    scope_filter = (
        MemoryMetadataModel.scope_id.is_(None)
        if item.note.scope_id is None
        else MemoryMetadataModel.scope_id == item.note.scope_id
    )
    row = (
        db.query(MemoryMetadataModel)
        .filter(
            MemoryMetadataModel.key == item.key,
            MemoryMetadataModel.scope == item.note.scope,
            scope_filter,
            MemoryMetadataModel.source_kind == "vault",
        )
        .first()
    )
    values = {
        "key": item.key,
        "memory_type": item.memory_type,
        "scope": item.note.scope,
        "scope_id": item.note.scope_id,
        "source_kind": "vault",
        "file_path": item.note.vault_relpath,
        "tags": item.tags,
        "token_estimate": len((item.note.text or "").split()),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
    if row is None:
        db.add(
            MemoryMetadataModel(id=item.memory_id, access_count=0, last_accessed_at=None, **values)
        )
    else:
        for key, value in values.items():
            setattr(row, key, value)


def _group_findings(
    projected: Iterable[_ProjectedNote],
) -> tuple[tuple[str, str, str, str], ...]:
    all_notes = tuple(projected)
    grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for item in all_notes:
        for finding in item.note.findings:
            grouped[(finding.code.value, item.note.vault_relpath, finding.severity)].append(
                finding.detail
            )
        if item.note.parsed is not None and item.note.status == "indexed":
            extracted = extract_wikilinks(item.note.parsed.region.body)
            for code in extracted.findings:
                grouped[
                    (
                        code.value,
                        item.note.vault_relpath,
                        finding_severity(code, secret_gate="reject"),
                    )
                ].append(code.value)
            candidates = _candidate_set(all_notes, item)
            for embed, raw in extracted.links:
                outcome = resolve_wikilink(raw, embed=embed, candidates=candidates)
                if outcome.finding_code is not None:
                    grouped[
                        (
                            outcome.finding_code.value,
                            item.note.vault_relpath,
                            finding_severity(outcome.finding_code, secret_gate="reject"),
                        )
                    ].append(outcome.finding_code.value)
    return tuple(
        (
            code,
            path,
            severity,
            f"count={len(details)}; code={code}; detail={','.join(sorted(set(details)))}",
        )
        for (code, path, severity), details in sorted(grouped.items())
    )


def _candidate_set(
    projected: tuple[_ProjectedNote, ...], item: _ProjectedNote
) -> tuple[LinkCandidate, ...]:
    return tuple(
        sorted(
            (
                LinkCandidate(
                    candidate.key,
                    candidate.note.vault_relpath,
                    _frontmatter_aliases(
                        candidate.note.parsed.frontmatter.get("aliases")
                        if candidate.note.parsed
                        else None
                    ),
                    excluded=candidate.note.status != "indexed",
                )
                for candidate in projected
                if candidate.note.scope == item.note.scope
                and candidate.note.scope_id == item.note.scope_id
            ),
            key=lambda candidate: (candidate.key, candidate.relpath),
        )
    )


def _canonical_target(
    raw_target: str, candidates: tuple[LinkCandidate, ...]
) -> tuple[Optional[str], Optional[FindingCode], dict[str, object]]:
    target = raw_target.strip()
    if target.startswith("[[") and target.endswith("]]"):
        outcome = resolve_wikilink(target[2:-2], embed=False, candidates=candidates)
        # The shaped target shares body-link resolution and exclusion gates,
        # but only an actual body attestation contributes fragment/embed data.
        return outcome.target_key, outcome.finding_code, {}
    matching = tuple(candidate for candidate in candidates if candidate.key == target)
    if not matching:
        return None, FindingCode.LINK_DANGLING, {}
    available = tuple(candidate for candidate in matching if not candidate.excluded)
    if not available:
        return None, FindingCode.LINK_EXCLUDED, {}
    if len(available) != 1 or len(matching) != len(available):
        return None, FindingCode.LINK_AMBIGUOUS, {}
    return available[0].key, None, {}


def _edge_finding(
    code: FindingCode, path: str, detail: Optional[str] = None
) -> tuple[str, str, str, str]:
    return (
        code.value,
        path,
        finding_severity(code, secret_gate="reject"),
        detail or code.value,
    )


def _canonical_vector(link: dict[str, Any]) -> tuple[str, Optional[float], str]:
    return (
        link.get("status", "active"),
        link.get("confidence"),
        link.get("origin", "human"),
    )


def _project_vault_edges(
    projected: Iterable[_ProjectedNote],
) -> tuple[
    dict[tuple[str, Optional[str], str, str], tuple[EdgeInput, ...]],
    tuple[tuple[str, str, str, str], ...],
]:
    """Build deterministic, bounded producer sets without mutating the store."""
    all_notes = tuple(projected)
    groups: dict[tuple[str, Optional[str], str, str], tuple[EdgeInput, ...]] = {}
    findings: list[tuple[str, str, str, str]] = []
    for item in all_notes:
        if item.note.status != "indexed":
            continue
        candidates = _candidate_set(all_notes, item)
        body_edges: dict[str, EdgeInput] = {}
        canonical: dict[tuple[str, str], tuple[dict[str, Any], dict[str, object]]] = {}
        conflicts: set[tuple[str, str]] = set()
        if item.note.parsed is not None:
            for embed, raw in extract_wikilinks(item.note.parsed.region.body).links:
                outcome = resolve_wikilink(raw, embed=embed, candidates=candidates)
                if outcome.outcome == "resolved" and outcome.target_key is not None:
                    body_edges.setdefault(
                        outcome.target_key,
                        EdgeInput(
                            target_key=outcome.target_key,
                            attributes=dict(outcome.attributes or {}) or None,
                        ),
                    )
            for link in item.note.parsed.cao.get("links", ()):
                link_type = link.get("type", "relates_to")
                target_key, finding_code, target_attributes = _canonical_target(
                    link["to"], candidates
                )
                if finding_code is not None:
                    findings.append(_edge_finding(finding_code, item.note.vault_relpath))
                    continue
                assert target_key is not None
                identity = (link_type, target_key)
                existing = canonical.get(identity)
                if existing is not None and _canonical_vector(existing[0]) != _canonical_vector(
                    link
                ):
                    conflicts.add(identity)
                    continue
                canonical.setdefault(identity, (link, target_attributes))

        edges_by_type: dict[str, dict[str, EdgeInput]] = defaultdict(dict)
        for target_key, body_edge in body_edges.items():
            edges_by_type["relates_to"][target_key] = body_edge
        for identity in conflicts:
            link_type, target_key = identity
            canonical.pop(identity, None)
            edges_by_type[link_type].pop(target_key, None)
            findings.append(
                _edge_finding(
                    FindingCode.CAO_LINK_CONFLICT,
                    item.note.vault_relpath,
                    f"type={link_type};target={target_key}",
                )
            )
        canonical_targets_by_type: dict[str, set[str]] = defaultdict(set)
        for (link_type, target_key), (link, target_attributes) in canonical.items():
            canonical_targets_by_type[link_type].add(target_key)
            canonical_body_edge = body_edges.get(target_key) if link_type == "relates_to" else None
            attributes = dict(target_attributes)
            if canonical_body_edge is not None:
                attributes.update(canonical_body_edge.attributes or {})
                attributes["attested_by"] = ["body", "frontmatter"]
            else:
                attributes["attested_by"] = ["frontmatter"]
            attributes["authored_origin"] = link.get("origin", "human")
            edges_by_type[link_type][target_key] = EdgeInput(
                target_key=target_key,
                status=link.get("status", "active"),
                confidence=link.get("confidence"),
                attributes=attributes,
            )

        for link_type in sorted(VALID_TYPES):
            typed_edges = edges_by_type.get(link_type, {})
            canonical_targets = canonical_targets_by_type.get(link_type, set())
            ordered_targets = sorted(canonical_targets) + sorted(
                target for target in typed_edges if target not in canonical_targets
            )
            if len(ordered_targets) > MAX_EDGES_PER_MUTATION:
                findings.append(
                    _edge_finding(
                        FindingCode.EDGE_LIMIT_EXCEEDED,
                        item.note.vault_relpath,
                        (
                            f"type={link_type};total={len(ordered_targets)};"
                            f"kept={MAX_EDGES_PER_MUTATION}"
                        ),
                    )
                )
            groups[(item.note.scope, item.note.scope_id, item.key, link_type)] = tuple(
                typed_edges[target] for target in ordered_targets[:MAX_EDGES_PER_MUTATION]
            )
    return groups, tuple(findings)


def _replace_vault_edges(
    projected: Iterable[_ProjectedNote],
    *,
    db=None,
) -> tuple[tuple[str, str, str, str], ...]:
    """Replace every vault-owned type group through the service boundary."""
    all_notes = tuple(projected)
    groups, findings = _project_vault_edges(all_notes)
    service = MemoryRelationshipService()
    for item in all_notes:
        if item.note.status != "indexed":
            continue
        for type_ in sorted(VALID_TYPES):
            # Retire the non-terminal producer set first so a fresh projection
            # receives fresh structural provenance while operator-terminal rows
            # remain preserved by the service contract.
            service.replace_set(
                item.note.scope,
                item.note.scope_id,
                item.key,
                "vault",
                type_,
                [],
                source_kind="vault",
                db=db,
            )
            service.replace_set(
                item.note.scope,
                item.note.scope_id,
                item.key,
                "vault",
                type_,
                list(groups.get((item.note.scope, item.note.scope_id, item.key, type_), ())),
                source_kind="vault",
                db=db,
            )
    return findings


def _emit_audit_events(
    vault_id: str,
    run_id: str,
    projected: Iterable[_ProjectedNote],
    indexed: int,
    quarantined: int,
    skipped: int,
) -> None:
    from cli_agent_orchestrator.services.audit_log import write_audit_nowait

    write_audit_nowait(
        "vault_reconcile_completed",
        "vault reconcile completed",
        vault_id=vault_id,
        run_id=run_id,
        indexed=indexed,
        quarantined=quarantined,
        skipped=skipped,
    )
    for item in projected:
        codes = tuple(sorted(finding.code.value for finding in item.note.findings))
        if FindingCode.SECRET_DETECTED.value in codes:
            write_audit_nowait(
                "vault_secret_quarantined",
                "vault secret quarantined",
                vault_id=vault_id,
                vault_relpath=item.note.vault_relpath,
                codes=",".join(codes),
            )
        if item.note.status != "quarantined":
            continue
        write_audit_nowait(
            "vault_note_quarantined",
            "vault note quarantined",
            vault_id=vault_id,
            vault_relpath=item.note.vault_relpath,
            codes=",".join(codes),
        )


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _from_mtime(mtime_ns: Optional[int], fallback: datetime) -> datetime:
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, timezone.utc) if mtime_ns else fallback


def _frontmatter_time(value, fallback: datetime) -> datetime:
    return value if isinstance(value, datetime) else fallback


def _tags(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(tag, str) for tag in value):
        return ",".join(sorted(value))
    return ""


def _frontmatter_aliases(value: object) -> tuple[str, ...]:
    """Return only usable frontmatter aliases; YAML null means no aliases."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(alias for alias in value if isinstance(alias, str))
    return ()
