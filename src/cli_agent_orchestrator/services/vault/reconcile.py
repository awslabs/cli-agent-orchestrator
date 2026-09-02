"""Deterministic, scoped projection of a scanned vault into derived state."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable, Optional, cast

from cli_agent_orchestrator.clients.database import (
    MemoryMetadataModel,
    MemoryRelationshipModel,
    SessionLocal,
    VaultFindingModel,
    VaultNoteAliasModel,
    VaultNoteModel,
)
from cli_agent_orchestrator.models.memory import MemoryType
from cli_agent_orchestrator.services.memory_relationship_service import (
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
    finding: Optional[tuple[str, str, str, str]] = None


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
        if not rebuild:
            _clear_stale_vault_edges(vault.id, projected)
        deleted, findings, projected = _apply_plan(
            vault, plan, projected, findings, rebuild=rebuild
        )
        _replace_vault_edges(projected)
        _emit_audit_events(vault.id, plan.run_id, projected, indexed, quarantined, skipped)
    else:
        findings = _preview_rename_findings(vault.id, projected, findings)

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
) -> tuple[tuple[str, str, str, str], ...]:
    """Run rename classification against current rows without changing derived state."""
    with SessionLocal() as db:
        prior_by_path: dict[str, VaultNoteModel] = {
            cast(str, row.vault_relpath): row
            for row in db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault_id).all()
        }
    resolutions = _resolve_renames(vault_id, projected, prior_by_path)
    return _merge_findings(
        findings,
        tuple(resolution.finding for resolution in resolutions if resolution.finding is not None),
    )


def _clear_stale_vault_edges(vault_id: str, projected: tuple[_ProjectedNote, ...]) -> None:
    """Retract removed or non-indexed sources through the relationship boundary."""
    with SessionLocal() as db:
        prior_by_path = {
            row.vault_relpath: row
            for row in db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault_id).all()
        }
        resolutions = _resolve_renames(vault_id, projected, prior_by_path)
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
        service.replace_set(
            row.scope,
            None if row.scope_id == "" else row.scope_id,
            row.cao_key,
            "vault",
            "relates_to",
            [],
            source_kind="vault",
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
    vault: VaultSpec,
    plan: ReconcilePlan,
    projected: tuple[_ProjectedNote, ...],
    findings: tuple[tuple[str, str, str, str], ...],
    *,
    rebuild: bool,
) -> tuple[int, tuple[tuple[str, str, str, str], ...], tuple[_ProjectedNote, ...]]:
    """Apply only vault-scoped deletes; native rows remain structurally untouched."""
    with SessionLocal() as db:
        if rebuild:
            # Every rebuild delete is scoped to its derived producer. Release
            # one permits one configured vault, so metadata has no vault id.
            db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault.id).delete()
            db.query(VaultFindingModel).filter(VaultFindingModel.vault_id == vault.id).delete()
            db.query(VaultNoteAliasModel).filter(VaultNoteAliasModel.vault_id == vault.id).delete()
            db.query(MemoryMetadataModel).filter(
                MemoryMetadataModel.source_kind == "vault"
            ).delete()
            db.query(MemoryRelationshipModel).filter(
                MemoryRelationshipModel.origin == "vault"
            ).delete()
        else:
            db.query(VaultFindingModel).filter(VaultFindingModel.vault_id == vault.id).delete()

        prior_by_path = {
            row.vault_relpath: row
            for row in db.query(VaultNoteModel).filter(VaultNoteModel.vault_id == vault.id).all()
        }
        resolutions = _resolve_renames(vault.id, projected, prior_by_path)
        projected = tuple(resolution.item for resolution in resolutions)
        findings = _merge_findings(
            findings,
            tuple(
                resolution.finding for resolution in resolutions if resolution.finding is not None
            ),
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
        for code, path, severity, detail in findings:
            db.add(
                VaultFindingModel(
                    id=_digest("finding", plan.run_id, code, path, severity),
                    vault_id=vault.id,
                    vault_relpath=path,
                    code=code,
                    severity=severity,
                    detail=detail,
                    reconcile_run_id=plan.run_id,
                    created_at=plan.run_started_at,
                )
            )
        db.commit()
    return deleted, findings, projected


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


def _resolve_renames(
    vault_id: str,
    projected: tuple[_ProjectedNote, ...],
    prior_by_path: dict[str, VaultNoteModel],
) -> tuple[_RenameResolution, ...]:
    """Absorb only unambiguous pure renames; all other candidates remain new.

    A path-derived key is retained only for one same-scope former path with the
    exact same content hash.  This prevents a rename-plus-edit or duplicate
    content from silently changing identity.
    """
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
            claims_by_former_path[old.vault_relpath] += 1
    resolutions: list[_RenameResolution] = []
    for item in projected:
        if item.note.vault_relpath not in appeared_paths:
            existing = prior_by_path[item.note.vault_relpath]
            has_authored_key = item.note.parsed is not None and "key" in item.note.parsed.cao
            # A prior pure rename retained a path-derived key through the
            # alias table. Subsequent unchanged (or edited) scans must retain
            # that established identity instead of deriving a second one from
            # the new path and colliding on vault_relpath.
            if not has_authored_key:
                item = replace(
                    item,
                    key=existing.cao_key,
                    canonical_key=existing.cao_key,
                    note_uid=existing.note_uid,
                    memory_id=_digest("memory", existing.note_uid),
                )
            resolutions.append(_RenameResolution(item))
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
            resolutions.append(
                _RenameResolution(
                    item,
                    retained_former_path=(
                        canonical[0].vault_relpath if len(canonical) == 1 else None
                    ),
                )
            )
        elif len(matching) == 1 and claims_by_former_path[matching[0].vault_relpath] == 1:
            old = matching[0]
            resolutions.append(
                _RenameResolution(
                    replace(
                        item,
                        key=old.cao_key,
                        note_uid=old.note_uid,
                        memory_id=_digest("memory", old.note_uid),
                    ),
                    retained_former_path=old.vault_relpath,
                    alias_from=old,
                )
            )
        elif len(matching) > 1 or any(
            claims_by_former_path[old.vault_relpath] > 1 for old in matching
        ):
            resolutions.append(
                _RenameResolution(
                    item,
                    finding=_rename_finding(FindingCode.RENAME_AMBIGUOUS, item.note.vault_relpath),
                )
            )
        elif len(same_scope) == 1:
            resolutions.append(
                _RenameResolution(
                    item,
                    finding=_rename_finding(
                        FindingCode.RENAME_WITH_EDIT_UNRESOLVED, item.note.vault_relpath
                    ),
                )
            )
        elif len(same_scope) > 1:
            resolutions.append(
                _RenameResolution(
                    item,
                    finding=_rename_finding(FindingCode.RENAME_AMBIGUOUS, item.note.vault_relpath),
                )
            )
        else:
            resolutions.append(_RenameResolution(item))
    return tuple(resolutions)


def _record_rename_alias(db, vault_id: str, old: VaultNoteModel, created_at: datetime) -> None:
    db.add(
        VaultNoteAliasModel(
            vault_id=vault_id,
            former_relpath=old.vault_relpath,
            cao_key=old.cao_key,
            scope=old.scope,
            scope_id=None if old.scope_id == "" else old.scope_id,
            content_sha256=old.content_sha256,
            created_at=created_at,
        )
    )


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
    return (code.value, relpath, finding_severity(code, secret_gate="reject"), code.value)


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


def _group_findings(projected: Iterable[_ProjectedNote]) -> tuple[tuple[str, str, str, str], ...]:
    all_notes = tuple(projected)
    grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    candidates = tuple(
        LinkCandidate(
            item.key,
            item.note.vault_relpath,
            tuple(item.note.parsed.frontmatter.get("aliases", ())) if item.note.parsed else (),
        )
        for item in all_notes
        if item.note.status == "indexed"
    )
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


def _replace_vault_edges(projected: Iterable[_ProjectedNote]) -> None:
    """Use the relationship-service boundary; never issue relationship SQL here."""
    indexed = tuple(item for item in projected if item.note.status == "indexed")
    candidates = tuple(
        LinkCandidate(
            item.key,
            item.note.vault_relpath,
            tuple(item.note.parsed.frontmatter.get("aliases", ())) if item.note.parsed else (),
        )
        for item in indexed
    )
    service = MemoryRelationshipService()
    for item in indexed:
        # Vault edges are derived projection rows. Retire this producer's active
        # set before replacing it so service-minted IDs and provenance describe
        # the current reconcile run rather than a prior vault snapshot.
        service.replace_set(
            item.note.scope,
            item.note.scope_id,
            item.key,
            "vault",
            "relates_to",
            [],
            source_kind="vault",
        )
        if item.note.parsed is None:
            continue
        edges = []
        for embed, raw in extract_wikilinks(item.note.parsed.region.body).links:
            outcome = resolve_wikilink(raw, embed=embed, candidates=candidates)
            if outcome.outcome == "resolved" and outcome.target_key is not None:
                edges.append(
                    EdgeInput(target_key=outcome.target_key, attributes=outcome.attributes)
                )
        service.replace_set(
            item.note.scope,
            item.note.scope_id,
            item.key,
            "vault",
            "relates_to",
            edges,
            source_kind="vault",
        )


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
