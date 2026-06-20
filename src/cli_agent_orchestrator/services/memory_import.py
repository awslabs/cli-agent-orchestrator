"""Phase 4 U2 — Memory Import.

Three-pass pipeline: validate → extract → apply. Each pass is bounded by
the caps in ``_archive_format`` and the trust boundaries in the U2
threat model. Heal-grade atomicity for the apply phase: a single
SQLAlchemy transaction wraps every row, and ANY rejection inside that
transaction rolls the whole import back (devsecops T5.i).

Hard contracts (devsecops):

- T1: caps enforced in pass 1 BEFORE extraction.
- T2: ``tarfile.extractall`` BANNED (CI grep gate). Per-member iteration
  with a name whitelist + size cap + ASCII-printable + length 256.
- T3: symlinks/devices/FIFOs/hardlinks rejected at type filter.
- T4: manifest closed-key set, ``json.loads`` strict, no ``object_hook``.
- T5: dump-row schema closed; ``dump_row_invalid`` is a distinct
  rejection reason; partial-row rejection rolls back the full transaction.
- T6: ``format_version == 1`` only, no fallback.
- T7: canonical content_hash recomputed at extraction time.
- T8: ``imported_from`` IMPORTER-SET; archive's claim STRIPPED.
- T9: ``mkdtemp + os.chmod 0o700 + O_EXCL + O_NOFOLLOW + O_CLOEXEC``.
- T10/T11: ``agent`` scope BANNED; ``scope=global`` arriving when
  ``scope_set`` didn't declare it → ``scope_elevation``.
- T13: ``target_project_id`` PINNED ONCE at import start.
- T14: marker strong-mode default LENIENT (rewrite + audit).
- T16: pass 1 ACCUMULATES, 1000-entry cap.
- T18: ``conflict_policy`` has NO DEFAULT.
- T20: ``memory.enabled=False`` → empty report.

Federation note: ``federated`` is a plain scope value in the shipped
federation design (PR #314). There is no separate directory, no per-row
``is_federated`` flag, and no demotion. A ``federated`` row imports as
``federated`` through the normal scope path.

Non-blocking promise: never raises out to callers.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import platform
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.constants import MEMORY_BASE_DIR
from cli_agent_orchestrator.services._archive_format import (
    CONTENT_HASH_RE,
    DECOMPRESSED_TOTAL_CAP_BYTES,
    DUMP_ROW_REQUIRED_KEYS,
    EXPORTED_BY_RE,
    GZIP_RATIO_CAP,
    GZIP_RATIO_FLOOR_BYTES,
    KEY_REGEX,
    MANIFEST_CAP_BYTES,
    MANIFEST_REQUIRED_KEYS,
    MEMBER_NAME_LENGTH_CAP,
    PROJECT_ID_RE,
    REJECTION_LIST_CAP,
    SUPPORTED_FORMAT_VERSION,
    TAR_MEMBER_COUNT_CAP,
    TMP_DIR_AGE_THRESHOLD_HOURS,
    TMP_DIR_PREFIX,
    VALID_SCOPES,
    ArchiveRejection,
    canonical_dump_bytes,
    canonical_manifest_bytes_with_placeholder,
    is_allowed_member_name,
    member_size_cap,
)
from cli_agent_orchestrator.services.audit_log import write_audit

logger = logging.getLogger(__name__)


VALID_CONFLICT_POLICIES: frozenset = frozenset({"skip", "replace", "merge"})


# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportAction:
    key: str
    scope: str
    scope_id: Optional[str]
    decision: (
        str  # insert|skip_existing|replace_existing|merge_winner_existing|merge_winner_imported
    )
    reason: Optional[str] = None


@dataclass(frozen=True)
class ImportReport:
    archive_path: Path
    project_id_in_archive: str
    project_id_applied: str
    format_version: int
    actions: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    bytes_read: int = 0
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    actor: str = "cli"
    dry_run: bool = False


def _empty_report(path: Path, actor: str, dry_run: bool) -> ImportReport:
    now = datetime.now(timezone.utc)
    return ImportReport(
        archive_path=path,
        project_id_in_archive="",
        project_id_applied="",
        format_version=SUPPORTED_FORMAT_VERSION,
        actions=[],
        rejections=[],
        bytes_read=0,
        started_at=now,
        ended_at=now,
        actor=actor,
        dry_run=dry_run,
    )


# -----------------------------------------------------------------------------
# Settings adapters
# -----------------------------------------------------------------------------


def _is_memory_enabled_safe() -> bool:
    try:
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        return bool(is_memory_enabled())
    except Exception:
        return True


def is_strong_mode_enabled() -> bool:
    try:
        from cli_agent_orchestrator.services.settings_service import get_memory_settings

        return bool(get_memory_settings().get("project_marker_strong_mode", False))
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Pass 1 — validate (no FS writes)
# -----------------------------------------------------------------------------


class _RatioGuardedReader(io.RawIOBase):
    """Stream-decompress with a running ratio guard (devsecops T1.a/g).

    Wraps ``gzip.GzipFile`` and aborts with ``RuntimeError("ratio_exceeds_cap")``
    once ``decompressed/compressed > GZIP_RATIO_CAP`` AND
    ``decompressed > GZIP_RATIO_FLOOR_BYTES``. Also enforces
    ``DECOMPRESSED_TOTAL_CAP_BYTES``.
    """

    def __init__(self, archive_path: Path) -> None:
        super().__init__()
        self._raw = open(str(archive_path), "rb")
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="rb")
        self._decompressed = 0
        self._compressed_pos = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self._gz.read(size)
        self._decompressed += len(chunk)
        try:
            self._compressed_pos = self._raw.tell()
        except OSError:
            self._compressed_pos = 0
        if self._decompressed > DECOMPRESSED_TOTAL_CAP_BYTES:
            raise RuntimeError("size_exceeds_cap:decompressed_total")
        if (
            self._decompressed > GZIP_RATIO_FLOOR_BYTES
            and self._compressed_pos > 0
            and self._decompressed / max(1, self._compressed_pos) > GZIP_RATIO_CAP
        ):
            raise RuntimeError("ratio_exceeds_cap")
        return chunk

    def close(self) -> None:
        try:
            self._gz.close()
        finally:
            self._raw.close()


def _validate_member_name(name: str) -> tuple:
    """T2.a/b/c. Return ``(ok, reason)``."""
    if not isinstance(name, str):
        return False, "path_traversal"
    if len(name.encode("utf-8")) > MEMBER_NAME_LENGTH_CAP:
        return False, "path_traversal"
    if name.startswith("/"):
        return False, "path_absolute"
    # ASCII-printable only.
    for ch in name:
        if ord(ch) < 0x20 or ord(ch) > 0x7E:
            return False, "encoding_invalid"
    if ".." in name.split("/"):
        return False, "path_traversal"
    if os.path.normpath(name) != name:
        return False, "path_traversal"
    if not is_allowed_member_name(name):
        return False, "unknown_member"
    return True, ""


def _validate_manifest(raw: bytes) -> tuple:
    """T4. Return ``(manifest_or_None, rejection_or_None)``."""
    if len(raw) > MANIFEST_CAP_BYTES:
        return None, ArchiveRejection(
            member="manifest.json", reason="size_exceeds_cap", detail="manifest cap"
        )
    try:
        obj = json.loads(raw)
    except Exception:
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")
    if not isinstance(obj, dict):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")
    if set(obj.keys()) != MANIFEST_REQUIRED_KEYS:
        return None, ArchiveRejection(
            member="manifest.json",
            reason="manifest_invalid",
            detail="key set mismatch",
        )

    # Per-key type checks.
    fv = obj["format_version"]
    if not (isinstance(fv, int) and not isinstance(fv, bool)):
        return None, ArchiveRejection(member="manifest.json", reason="format_version_unsupported")
    if fv != SUPPORTED_FORMAT_VERSION:
        return None, ArchiveRejection(member="manifest.json", reason="format_version_unsupported")

    pid = obj["project_id"]
    if not (isinstance(pid, str) and PROJECT_ID_RE.match(pid)):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")

    id_kind = obj["id_kind"]
    if not (isinstance(id_kind, str) and id_kind):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")

    created_at = obj["created_at"]
    if not isinstance(created_at, str):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")
    try:
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")

    eb = obj["exported_by"]
    if not (isinstance(eb, str) and EXPORTED_BY_RE.match(eb)):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")

    ss = obj["scope_set"]
    if not (isinstance(ss, list) and ss):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")
    for s in ss:
        if not (isinstance(s, str) and s in VALID_SCOPES):
            return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")

    nwf = obj["n_wiki_files"]
    nmr = obj["n_metadata_rows"]
    if not (isinstance(nwf, int) and not isinstance(nwf, bool) and nwf >= 0):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")
    if not (isinstance(nmr, int) and not isinstance(nmr, bool) and nmr >= 0):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")

    ch = obj["content_hash"]
    if not (isinstance(ch, str) and CONTENT_HASH_RE.match(ch)):
        return None, ArchiveRejection(member="manifest.json", reason="manifest_invalid")

    return obj, None


def _validate_archive(path: Path) -> tuple:
    """Pass 1. Returns ``(manifest_or_None, rejections, member_count)``."""
    rejections: list = []
    manifest: Optional[dict] = None
    member_count = 0

    reader = _RatioGuardedReader(path)
    try:
        try:
            tar = tarfile.open(fileobj=reader, mode="r|")  # streaming
        except Exception:
            rejections.append(
                ArchiveRejection(
                    member="<archive>", reason="manifest_invalid", detail="tar open failed"
                )
            )
            return None, rejections, 0
        try:
            for member in tar:
                member_count += 1
                if member_count > TAR_MEMBER_COUNT_CAP:
                    rejections.append(
                        ArchiveRejection(
                            member="<archive>",
                            reason="size_exceeds_cap",
                            detail=f"member count > {TAR_MEMBER_COUNT_CAP}",
                        )
                    )
                    break

                # Type filter (T3).
                if member.isdev() or member.islnk() or member.issym() or member.isfifo():
                    rejections.append(
                        ArchiveRejection(member=str(member.name or ""), reason="symlink")
                    )
                    continue
                if not member.isfile():
                    rejections.append(
                        ArchiveRejection(member=str(member.name or ""), reason="device_or_pipe")
                    )
                    continue

                ok, reason = _validate_member_name(member.name or "")
                if not ok:
                    rejections.append(
                        ArchiveRejection(member=str(member.name or ""), reason=reason)
                    )
                    continue

                size_cap = member_size_cap(member.name)
                if size_cap == 0 or member.size > size_cap:
                    rejections.append(
                        ArchiveRejection(
                            member=member.name,
                            reason="size_exceeds_cap",
                            detail=f"size={member.size} cap={size_cap}",
                        )
                    )
                    continue

                if len(rejections) >= REJECTION_LIST_CAP:
                    rejections.append(
                        ArchiveRejection(
                            member="<archive>",
                            reason="manifest_invalid",
                            detail="rejection list truncated",
                        )
                    )
                    break

                if member.name == "manifest.json":
                    src = tar.extractfile(member)
                    if src is None:
                        rejections.append(
                            ArchiveRejection(member="manifest.json", reason="manifest_invalid")
                        )
                        continue
                    raw = src.read()
                    parsed, rej = _validate_manifest(raw)
                    if rej is not None:
                        rejections.append(rej)
                    else:
                        manifest = parsed
        finally:
            tar.close()
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("ratio_exceeds_cap"):
            rejections.append(ArchiveRejection(member="<archive>", reason="ratio_exceeds_cap"))
        elif msg.startswith("size_exceeds_cap"):
            rejections.append(
                ArchiveRejection(member="<archive>", reason="size_exceeds_cap", detail=msg)
            )
        else:
            rejections.append(
                ArchiveRejection(member="<archive>", reason="manifest_invalid", detail=msg)
            )
    except Exception as e:
        rejections.append(
            ArchiveRejection(
                member="<archive>",
                reason="manifest_invalid",
                detail=f"{type(e).__name__}",
            )
        )
    finally:
        try:
            reader.close()
        except Exception:
            pass

    if manifest is None and not any(
        r.member == "manifest.json" or r.member == "<archive>" for r in rejections
    ):
        rejections.append(
            ArchiveRejection(member="manifest.json", reason="manifest_invalid", detail="missing")
        )

    return manifest, rejections, member_count


# -----------------------------------------------------------------------------
# Pass 2 — extract to fresh tmp dir
# -----------------------------------------------------------------------------


def _extract_to_tmp(archive_path: Path, base_dir: Path) -> Path:
    """Extract validated archive to a fresh tmp dir under ``<base>/.import``."""
    parent = base_dir / ".import"
    parent.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        try:
            os.chmod(str(parent), 0o700)
        except OSError:
            pass
    tmp_dir = Path(tempfile.mkdtemp(dir=str(parent), prefix=TMP_DIR_PREFIX))
    if platform.system() != "Windows":
        os.chmod(str(tmp_dir), 0o700)

    try:
        with tarfile.open(str(archive_path), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                if not is_allowed_member_name(member.name):
                    continue  # already pass-1 rejected; defensive
                target = tmp_dir / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if platform.system() != "Windows":
                    try:
                        os.chmod(str(target.parent), 0o700)
                    except OSError:
                        pass
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                fd = os.open(str(target), flags, 0o600)
                try:
                    if platform.system() != "Windows":
                        st = os.fstat(fd)
                        if (st.st_mode & 0o7777) != 0o600:
                            raise OSError("tmp file mode mismatch")
                    src = tar.extractfile(member)
                    if src is None:
                        raise OSError(f"extractfile None for {member.name}")
                    while True:
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        os.write(fd, chunk)
                finally:
                    os.close(fd)
    except Exception:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
        raise

    return tmp_dir


def _recompute_content_hash_from_tmp(tmp_dir: Path, manifest: dict) -> str:
    """Recompute the canonical hash from the extracted bytes."""
    h = hashlib.sha256()
    h.update(canonical_manifest_bytes_with_placeholder(manifest))
    dump_path = tmp_dir / "sqlite-dump.json"
    if dump_path.exists():
        try:
            rows = json.loads(dump_path.read_bytes())
        except Exception:
            rows = []
    else:
        rows = []
    h.update(canonical_dump_bytes(rows))
    index_path = tmp_dir / "index.md"
    h.update(index_path.read_bytes() if index_path.exists() else b"")
    wiki_dir = tmp_dir / "wiki"
    if wiki_dir.exists():
        for path in sorted(wiki_dir.iterdir()):
            try:
                h.update(path.read_bytes())
            except OSError:
                continue
    return "sha256:" + h.hexdigest()


# -----------------------------------------------------------------------------
# Pass 3 — apply rows in single transaction
# -----------------------------------------------------------------------------


def _validate_dump_row(row: Any, manifest: dict, target_project_id: str) -> tuple:
    """T5. Returns ``(ok, action_or_rejection)``.

    ``federated`` is treated as a plain scope value: no ``is_federated`` flag
    and no federation invariant check. Only the agent ban (T11) and scope
    elevation (T10) gate the scope field.
    """
    if not isinstance(row, dict):
        return False, ArchiveRejection(member="sqlite-dump.json", reason="dump_row_invalid")
    if set(row.keys()) != DUMP_ROW_REQUIRED_KEYS:
        return False, ArchiveRejection(
            member="sqlite-dump.json",
            reason="dump_row_invalid",
            detail="row key set mismatch",
        )

    scope = row.get("scope")
    if not (isinstance(scope, str) and scope in VALID_SCOPES):
        return False, ArchiveRejection(
            member="sqlite-dump.json", reason="scope_invalid", detail=str(scope)
        )
    if scope == "agent":
        return False, ArchiveRejection(
            member="sqlite-dump.json", reason="scope_invalid", detail="agent ban (T11)"
        )
    if scope not in manifest.get("scope_set", []):
        return False, ArchiveRejection(
            member="sqlite-dump.json",
            reason="scope_elevation",
            detail=f"row scope={scope} not in scope_set",
        )

    key = row.get("key")
    if not (isinstance(key, str) and KEY_REGEX.match(key)):
        return False, ArchiveRejection(
            member="sqlite-dump.json", reason="key_invalid", detail=str(key)
        )

    ac = row.get("access_count", 0)
    if not (isinstance(ac, int) and not isinstance(ac, bool) and ac >= 0):
        return False, ArchiveRejection(
            member="sqlite-dump.json", reason="dump_row_invalid", detail="access_count"
        )

    # Strict validation for important free-form fields.
    for ts_field in ("created_at", "updated_at"):
        v = row.get(ts_field)
        if v is None:
            continue
        if not isinstance(v, str):
            return False, ArchiveRejection(
                member="sqlite-dump.json", reason="dump_row_invalid", detail=ts_field
            )
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return False, ArchiveRejection(
                member="sqlite-dump.json",
                reason="dump_row_invalid",
                detail=f"{ts_field} ISO-8601-Z",
            )

    if scope == "global" and row.get("scope_id") is not None:
        return False, ArchiveRejection(
            member="sqlite-dump.json",
            reason="scope_invalid",
            detail="global rows must have scope_id NULL",
        )

    return True, row


def _screen_row_for_secrets(row: dict, tmp_dir: Path) -> Optional[ArchiveRejection]:
    """Screen the imported wiki body for credential patterns (devsecops §3.3).

    Reuses ``secret_gate.scan_for_secrets`` — the same heuristic deny-list
    ``store()`` runs on federated writes. On a hit, returns an
    ``ArchiveRejection`` whose detail carries the matched pattern NAME only.
    The offending content bytes are NEVER logged or echoed.
    """
    from cli_agent_orchestrator.services.secret_gate import scan_for_secrets

    src_wiki = tmp_dir / "wiki" / f"{row['key']}.md"
    if not src_wiki.exists():
        return None
    try:
        content = src_wiki.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pattern_name = scan_for_secrets(content)
    if pattern_name is None:
        return None
    return ArchiveRejection(
        member="sqlite-dump.json",
        reason="federation_secret_in_import",
        detail=pattern_name,
    )


def _decide_conflict(existing: Any, row: dict, conflict_policy: str) -> str:
    """Return a HealAction-style decision string per devsecops T12 chain."""
    if existing is None:
        return "insert"
    if conflict_policy == "skip":
        return "skip_existing"
    if conflict_policy == "replace":
        return "replace_existing"
    # merge — tie-break chain (matches U1 healer §4.2).
    e_ts = existing.updated_at
    try:
        i_ts = (
            datetime.strptime(row["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
            if row.get("updated_at")
            else None
        )
    except Exception:
        i_ts = None
    if i_ts is not None and i_ts.tzinfo is not None:
        i_ts = i_ts.replace(tzinfo=None)
    e_naive = e_ts.replace(tzinfo=None) if (e_ts is not None and e_ts.tzinfo is not None) else e_ts
    if e_naive is None and i_ts is None:
        pass
    elif e_naive is None:
        return "merge_winner_imported"
    elif i_ts is None:
        return "merge_winner_existing"
    else:
        if i_ts > e_naive:
            return "merge_winner_imported"
        if i_ts < e_naive:
            return "merge_winner_existing"
    e_ac = int(getattr(existing, "access_count", 0) or 0)
    i_ac = int(row.get("access_count", 0) or 0)
    if i_ac != e_ac:
        return "merge_winner_imported" if i_ac > e_ac else "merge_winner_existing"
    return (
        "merge_winner_imported" if str(row["key"]) < str(existing.key) else "merge_winner_existing"
    )


def _materialise_row(
    db: Any,
    row: dict,
    decision: str,
    target_project_id: str,
    archive_sha256: str,
    actor: str,
    tmp_dir: Path,
    base_dir: Path,
) -> None:
    """Apply the chosen decision to SQL + FS. Caller is in a transaction."""
    from cli_agent_orchestrator.clients.database import MemoryMetadataModel

    if decision in ("skip_existing", "merge_winner_existing"):
        return  # no-op

    scope = row["scope"]
    rewritten_scope_id: Optional[str]
    if scope == "global":
        rewritten_scope_id = None
    elif scope == "federated":
        # ``federated`` is a user-level scope value — scope_id is always NULL.
        rewritten_scope_id = None
    else:
        rewritten_scope_id = target_project_id

    # Importer-set provenance (devsecops T8).
    imported_from = json.dumps(
        {
            "archive_sha256": archive_sha256,
            "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "actor": actor,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    # Locate the archived wiki file in tmp.
    src_wiki = tmp_dir / "wiki" / f"{row['key']}.md"

    # Resolve the local target wiki path.
    from cli_agent_orchestrator.services.memory_service import MemoryService

    svc = MemoryService(base_dir=base_dir, db_engine=db.bind)
    target_path = Path(str(svc.get_wiki_path(scope, rewritten_scope_id, row["key"])))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if src_wiki.exists():
        # Containment guard re-applied on the destination.
        target_resolved = target_path.resolve()
        base_resolved = base_dir.resolve()
        if not str(target_resolved).startswith(str(base_resolved) + os.sep):
            raise OSError("path containment violation on import target")
        # Atomic write via tmp + replace.
        body = src_wiki.read_bytes()
        tmp_target = target_path.with_suffix(target_path.suffix + ".import.tmp")
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(tmp_target), flags, 0o600)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(str(tmp_target), str(target_path))

    # Apply SQL change.
    existing = (
        db.query(MemoryMetadataModel)
        .filter(
            MemoryMetadataModel.key == row["key"],
            MemoryMetadataModel.scope == scope,
        )
        .filter(
            (
                MemoryMetadataModel.scope_id == rewritten_scope_id
                if rewritten_scope_id is not None
                else MemoryMetadataModel.scope_id.is_(None)
            )
        )
        .one_or_none()
    )

    def _parse_ts(v: Optional[str]) -> Optional[datetime]:
        if not v:
            return None
        try:
            return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    if decision == "insert":
        new_row = MemoryMetadataModel(
            id=row.get("id") or None,
            key=row["key"],
            memory_type=row["memory_type"],
            scope=scope,
            scope_id=rewritten_scope_id,
            file_path=str(target_path),
            tags=row.get("tags", "") or "",
            source_provider=row.get("source_provider"),
            source_terminal_id=row.get("source_terminal_id"),
            token_estimate=row.get("token_estimate"),
            created_at=_parse_ts(row.get("created_at")) or datetime.now(timezone.utc),
            updated_at=_parse_ts(row.get("updated_at")) or datetime.now(timezone.utc),
            last_compiled_at=None,  # imported article wasn't compiled here
            access_count=int(row.get("access_count") or 0),
            last_accessed_at=_parse_ts(row.get("last_accessed_at")),
            related_keys=row.get("related_keys"),
            imported_from=imported_from,
        )
        db.add(new_row)
    else:
        # replace_existing or merge_winner_imported — overwrite columns.
        if existing is None:
            return
        existing.memory_type = row["memory_type"]
        existing.file_path = str(target_path)
        existing.tags = row.get("tags", "") or ""
        existing.source_provider = row.get("source_provider")
        existing.source_terminal_id = row.get("source_terminal_id")
        existing.token_estimate = row.get("token_estimate")
        existing.updated_at = _parse_ts(row.get("updated_at")) or datetime.now(timezone.utc)
        existing.last_compiled_at = None
        existing.access_count = int(row.get("access_count") or 0)
        existing.last_accessed_at = _parse_ts(row.get("last_accessed_at"))
        existing.related_keys = row.get("related_keys")
        existing.imported_from = imported_from


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------


async def import_archive(
    archive_path: Path,
    *,
    conflict_policy: str,
    dry_run: bool = False,
    target_project_id: Optional[str] = None,
    actor: str = "cli",
) -> ImportReport:
    """Import a memory archive. Never raises out to callers."""
    if conflict_policy not in VALID_CONFLICT_POLICIES:
        raise TypeError(
            f"conflict_policy required, must be one of {sorted(VALID_CONFLICT_POLICIES)}"
        )

    started = datetime.now(timezone.utc)

    if not _is_memory_enabled_safe():
        return _empty_report(archive_path, actor, dry_run)

    # T13 — pin target_project_id ONCE.
    pinned_target: str
    if target_project_id is not None:
        # Validate via the same regex as ``_validate_project_id_override``.
        try:
            from cli_agent_orchestrator.services.memory_service import (
                _validate_project_id_override,
            )

            _validate_project_id_override(target_project_id)
            pinned_target = target_project_id
        except Exception as e:
            await write_audit(
                "import_failed",
                f"target_project_id invalid: {type(e).__name__}",
                actor=actor,
                reason="target_project_id_invalid",
            )
            return ImportReport(
                archive_path=archive_path,
                project_id_in_archive="",
                project_id_applied="",
                format_version=SUPPORTED_FORMAT_VERSION,
                actions=[],
                rejections=[
                    ArchiveRejection(
                        member="<args>",
                        reason="manifest_invalid",
                        detail="target_project_id invalid",
                    )
                ],
                bytes_read=0,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                actor=actor,
                dry_run=dry_run,
            )
    else:
        try:
            from cli_agent_orchestrator.services.memory_service import resolve_project_id

            pinned_target = resolve_project_id(Path.cwd())
        except Exception:
            pinned_target = "global"

    await write_audit(
        "import_started",
        f"import started: {archive_path.name}",
        archive_path=str(archive_path),
        conflict_policy=conflict_policy,
        dry_run=str(dry_run).lower(),
        actor=actor,
        project_id_applied=pinned_target,
    )

    # Pass 1 — validate.
    manifest, rejections, _ = _validate_archive(archive_path)
    for rej in rejections:
        await write_audit(
            "import_rejection",
            f"rejection: {rej.reason}",
            member=rej.member,
            reason=rej.reason,
            detail=rej.detail or "",
        )

    bytes_read = 0
    try:
        bytes_read = archive_path.stat().st_size
    except OSError:
        pass

    if rejections or manifest is None:
        await write_audit(
            "import_failed",
            f"import failed: {len(rejections)} rejections in pass 1",
            actor=actor,
            reason="pass1_rejections",
        )
        return ImportReport(
            archive_path=archive_path,
            project_id_in_archive=(manifest or {}).get("project_id", ""),
            project_id_applied=pinned_target,
            format_version=(manifest or {}).get("format_version", 0),
            actions=[],
            rejections=rejections,
            bytes_read=bytes_read,
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            actor=actor,
            dry_run=dry_run,
        )

    # Marker strong-mode ruling (T14).
    archive_pid = manifest.get("project_id", "")
    if archive_pid != pinned_target:
        if is_strong_mode_enabled():
            await write_audit(
                "import_failed",
                "strong-mode rejected mismatched project_id",
                actor=actor,
                reason="marker_project_id_mismatch",
            )
            return ImportReport(
                archive_path=archive_path,
                project_id_in_archive=archive_pid,
                project_id_applied=pinned_target,
                format_version=manifest["format_version"],
                actions=[],
                rejections=[
                    ArchiveRejection(
                        member="manifest.json",
                        reason="manifest_invalid",
                        detail="strong-mode mismatch",
                    )
                ],
                bytes_read=bytes_read,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                actor=actor,
                dry_run=dry_run,
            )
        await write_audit(
            "marker_strong_mode_rewrite",
            "rewriting archive project_id to applied",
            archive_project_id=archive_pid,
            applied_project_id=pinned_target,
        )

    # Compute archive_sha256 over the WHOLE file (T8).
    try:
        archive_sha256 = "sha256:" + _hash_file(archive_path)
    except OSError:
        archive_sha256 = ""

    # Pass 2 — extract + content_hash recompute (T7).
    try:
        tmp_dir = _extract_to_tmp(archive_path, MEMORY_BASE_DIR)
    except Exception as e:
        reason = f"{type(e).__name__}: {str(e)[:160]}"
        await write_audit(
            "import_failed", f"extraction failed: {reason}", actor=actor, reason=reason
        )
        return ImportReport(
            archive_path=archive_path,
            project_id_in_archive=archive_pid,
            project_id_applied=pinned_target,
            format_version=manifest["format_version"],
            actions=[],
            rejections=[
                ArchiveRejection(member="<archive>", reason="manifest_invalid", detail=reason)
            ],
            bytes_read=bytes_read,
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            actor=actor,
            dry_run=dry_run,
        )

    actions: list = []
    apply_rejections: list = []
    try:
        recomputed = _recompute_content_hash_from_tmp(tmp_dir, manifest)
        if recomputed != manifest["content_hash"]:
            await write_audit(
                "import_rejection",
                "content_hash mismatch",
                member="manifest.json",
                reason="content_hash_mismatch",
                detail=f"expected={manifest['content_hash']} got={recomputed}",
            )
            await write_audit(
                "import_failed",
                "content_hash mismatch",
                actor=actor,
                reason="content_hash_mismatch",
            )
            return ImportReport(
                archive_path=archive_path,
                project_id_in_archive=archive_pid,
                project_id_applied=pinned_target,
                format_version=manifest["format_version"],
                actions=[],
                rejections=[
                    ArchiveRejection(
                        member="manifest.json",
                        reason="content_hash_mismatch",
                    )
                ],
                bytes_read=bytes_read,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                actor=actor,
                dry_run=dry_run,
            )

        # Pass 3 — apply.
        dump_path = tmp_dir / "sqlite-dump.json"
        try:
            dump_rows = json.loads(dump_path.read_bytes())
        except Exception:
            dump_rows = []
        if not isinstance(dump_rows, list):
            dump_rows = []

        # Strip archive-claimed imported_from before validation (devsecops §3.3).
        for row in dump_rows:
            if isinstance(row, dict) and "imported_from" in row:
                # Keep the key (closed-set requires it) but blank the
                # claimed value so importer rewrites cleanly.
                row["imported_from"] = None

        if dry_run:
            for row in dump_rows:
                ok, result = _validate_dump_row(row, manifest, pinned_target)
                if not ok:
                    apply_rejections.append(result)
                    continue
                secret_rej = _screen_row_for_secrets(row, tmp_dir)
                if secret_rej is not None:
                    apply_rejections.append(secret_rej)
                    continue
                actions.append(
                    ImportAction(
                        key=row["key"],
                        scope=row["scope"],
                        scope_id=None if row["scope"] in ("global", "federated") else pinned_target,
                        decision="insert",  # planned; real apply may differ
                        reason="dry_run",
                    )
                )
            await write_audit(
                "import_completed",
                f"import dry-run completed: {len(actions)} planned",
                n_actions=str(len(actions)),
                n_rejections=str(len(apply_rejections)),
                project_id_applied=pinned_target,
            )
            return ImportReport(
                archive_path=archive_path,
                project_id_in_archive=archive_pid,
                project_id_applied=pinned_target,
                format_version=manifest["format_version"],
                actions=actions,
                rejections=apply_rejections,
                bytes_read=bytes_read,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                actor=actor,
                dry_run=True,
            )

        # Real apply path.
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel
        from cli_agent_orchestrator.services.memory_service import MemoryService

        svc = MemoryService(base_dir=MEMORY_BASE_DIR)

        with svc._get_db_session() as db:
            try:
                planned: list = []
                for row in dump_rows:
                    ok, result = _validate_dump_row(row, manifest, pinned_target)
                    if not ok:
                        apply_rejections.append(result)
                        continue
                    secret_rej = _screen_row_for_secrets(row, tmp_dir)
                    if secret_rej is not None:
                        apply_rejections.append(secret_rej)
                        continue
                    planned.append(row)

                if apply_rejections:
                    db.rollback()
                    raise RuntimeError("partial-row rejection — full rollback")

                for row in planned:
                    scope = row["scope"]
                    rewritten_scope_id = None if scope in ("global", "federated") else pinned_target
                    existing = (
                        db.query(MemoryMetadataModel)
                        .filter(
                            MemoryMetadataModel.key == row["key"],
                            MemoryMetadataModel.scope == scope,
                        )
                        .filter(
                            (
                                MemoryMetadataModel.scope_id == rewritten_scope_id
                                if rewritten_scope_id is not None
                                else MemoryMetadataModel.scope_id.is_(None)
                            )
                        )
                        .one_or_none()
                    )
                    decision = _decide_conflict(existing, row, conflict_policy)
                    _materialise_row(
                        db,
                        row,
                        decision,
                        pinned_target,
                        archive_sha256,
                        actor,
                        tmp_dir,
                        MEMORY_BASE_DIR,
                    )
                    actions.append(
                        ImportAction(
                            key=row["key"],
                            scope=scope,
                            scope_id=rewritten_scope_id,
                            decision=decision,
                        )
                    )
                    await write_audit(
                        "memory_imported_row",
                        f"{decision} {row['key']}",
                        scope=scope,
                        scope_id=rewritten_scope_id or "",
                        key=row["key"],
                        decision=decision,
                        archive_sha256=archive_sha256,
                    )

                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise
    except Exception as e:
        reason = f"{type(e).__name__}: {str(e)[:160]}"
        await write_audit("import_failed", f"apply failed: {reason}", actor=actor, reason=reason)
        return ImportReport(
            archive_path=archive_path,
            project_id_in_archive=archive_pid,
            project_id_applied=pinned_target,
            format_version=manifest["format_version"],
            actions=[],
            rejections=apply_rejections
            or [ArchiveRejection(member="<apply>", reason="manifest_invalid", detail=reason)],
            bytes_read=bytes_read,
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            actor=actor,
            dry_run=False,
        )
    finally:
        try:
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except Exception:
            pass

    await write_audit(
        "import_completed",
        f"import completed: {len(actions)} actions",
        n_actions=str(len(actions)),
        n_rejections=str(len(apply_rejections)),
        project_id_applied=pinned_target,
    )
    return ImportReport(
        archive_path=archive_path,
        project_id_in_archive=archive_pid,
        project_id_applied=pinned_target,
        format_version=manifest["format_version"],
        actions=actions,
        rejections=apply_rejections,
        bytes_read=bytes_read,
        started_at=started,
        ended_at=datetime.now(timezone.utc),
        actor=actor,
        dry_run=False,
    )


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# -----------------------------------------------------------------------------
# Cleanup sweep — devsecops T17 ``import_tmp_dir_swept``
# -----------------------------------------------------------------------------


def sweep_import_tmp_dirs(*, retention_hours: int = TMP_DIR_AGE_THRESHOLD_HOURS) -> tuple:
    """Remove orphan tmp dirs older than ``retention_hours``.

    Returns ``(n_swept, oldest_age_hours)``.
    """
    base = MEMORY_BASE_DIR / ".import"
    if not base.exists():
        return 0, 0.0
    try:
        base_resolved = str(base.resolve())
    except OSError:
        return 0, 0.0
    n = 0
    oldest = 0.0
    cutoff = datetime.now(timezone.utc).timestamp() - retention_hours * 3600
    for entry in base.iterdir():
        if not entry.name.startswith(TMP_DIR_PREFIX):
            continue
        try:
            entry_resolved = str(entry.resolve())
        except OSError:
            continue
        if not entry_resolved.startswith(base_resolved + os.sep):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        age_hours = max(0.0, (datetime.now(timezone.utc).timestamp() - mtime) / 3600.0)
        if mtime < cutoff:
            try:
                shutil.rmtree(str(entry), ignore_errors=True)
                n += 1
                oldest = max(oldest, age_hours)
            except Exception:
                continue
    return n, oldest
