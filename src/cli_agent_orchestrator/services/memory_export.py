"""Phase 4 U2 — Memory Export.

Builds a deterministic ``.tar.gz`` bundle of one project's memories. The
inner archive is hashed via the canonical rule in ``_archive_format``
(devsecops T7) so import-side tampering is detectable. Rule-based; no
LLM, no sentinel envelope.

Hard contracts (devsecops):

- T7 ``content_hash`` placeholder substitution rule: build manifest with
  placeholder → compute hash over canonical inner bytes → rewrite
  manifest with real hash → archive.
- T11 ``agent`` scope EXCLUDED unconditionally (even with
  ``include_global=True``).
- T19 ``output_path`` containment + suffix + system-dir blacklist +
  ``O_NOFOLLOW``+``O_EXCL`` on the output file.
- T20 kill-switch silence (``memory.enabled=False`` → empty report).

The manifest records ``project_id`` plus ``id_kind`` — the identity tier
that produced it (``override`` / ``git_remote`` / ``cwd_hash`` /
``literal``) — so importers can reason about provenance.

Federation note: ``federated`` is treated as a plain scope value here. There
is no separate federation directory and no demotion logic; a ``federated``
export (if such rows exist) flows through the normal scope query.

Non-blocking promise: never raises out to callers; failures populate
``ExportReport.errors`` and emit ``export_failed``.
"""

from __future__ import annotations

import gzip
import io
import json as _json
import logging
import os
import platform
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.constants import MEMORY_BASE_DIR
from cli_agent_orchestrator.services._archive_format import (
    EXPORTED_BY_RE,
    IMPORTABLE_SCOPES,
    SUPPORTED_FORMAT_VERSION,
    compute_content_hash,
)
from cli_agent_orchestrator.services.audit_log import write_audit

logger = logging.getLogger(__name__)


SYSTEM_DIR_BLACKLIST: tuple = (
    "/etc",
    "/var",
    "/usr",
    "/bin",
    "/sbin",
    "/dev",
    "/proc",
    "/sys",
    "/tmp",
)


# -----------------------------------------------------------------------------
# Dataclass
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportReport:
    archive_path: Path
    project_id: str
    id_kind: str
    format_version: int
    n_wiki_files: int
    n_metadata_rows: int
    bytes_written: int
    content_hash: str
    started_at: datetime
    ended_at: datetime
    actor: str
    errors: list


def _empty_report(path: Path, actor: str) -> ExportReport:
    now = datetime.now(timezone.utc)
    return ExportReport(
        archive_path=path,
        project_id="",
        id_kind="",
        format_version=SUPPORTED_FORMAT_VERSION,
        n_wiki_files=0,
        n_metadata_rows=0,
        bytes_written=0,
        content_hash="",
        started_at=now,
        ended_at=now,
        actor=actor,
        errors=[],
    )


def _error_report(
    path: Path,
    actor: str,
    started: datetime,
    errors: list,
    *,
    project_id: str = "",
    id_kind: str = "",
    n_wiki_files: int = 0,
    n_metadata_rows: int = 0,
    content_hash: str = "",
) -> ExportReport:
    return ExportReport(
        archive_path=path,
        project_id=project_id,
        id_kind=id_kind,
        format_version=SUPPORTED_FORMAT_VERSION,
        n_wiki_files=n_wiki_files,
        n_metadata_rows=n_metadata_rows,
        bytes_written=0,
        content_hash=content_hash,
        started_at=started,
        ended_at=datetime.now(timezone.utc),
        actor=actor,
        errors=errors,
    )


def _is_memory_enabled_safe() -> bool:
    try:
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        return bool(is_memory_enabled())
    except Exception:
        return True


def _resolve_project_identity(scope: str, scope_id: Optional[str]) -> tuple[str, str]:
    """Return ``(project_id, id_kind)`` for the manifest.

    For ``project`` scope we resolve canonical identity via the U6
    precedence chain and report which tier produced it. For all other
    scopes the ``scope_id`` literal is the identity (``id_kind="literal"``).
    """
    if scope == "project":
        # Mirror the resolve_project_id precedence to capture the tier.
        from cli_agent_orchestrator.services.memory_service import (
            _git_remote_identity,
            _normalize_git_remote,
            _read_project_id_override,
            resolve_project_id,
        )

        try:
            override = _read_project_id_override()
            if override:
                return override, "override"
            cwd = Path.cwd()
            remote_url = _git_remote_identity(cwd)
            if remote_url:
                return _normalize_git_remote(remote_url), "git_remote"
            return resolve_project_id(cwd), "cwd_hash"
        except Exception as e:  # noqa: BLE001 — non-blocking; fall back to literal
            logger.debug(f"project identity resolution failed (non-fatal): {e}")
            return (scope_id or "global"), "literal"
    return (scope_id or ""), "literal"


def _validate_output_path(output_path: Path) -> tuple:
    """Return ``(resolved_path, error_or_None)``. Devsecops T19."""
    suffix = output_path.suffix.lower()
    if suffix not in (".gz", ".tgz"):
        return None, "output_path must end in .tar.gz or .tgz"
    if suffix == ".gz" and "".join(output_path.suffixes[-2:]) != ".tar.gz":
        return None, "output_path must end in .tar.gz or .tgz"
    try:
        resolved = output_path.resolve()
    except OSError as e:
        return None, f"output_path resolve failed: {type(e).__name__}"
    s = str(resolved)
    for prefix in SYSTEM_DIR_BLACKLIST:
        if s == prefix or s.startswith(prefix + os.sep):
            return None, f"output_path resolves under blacklisted system dir {prefix}"
    home_resolved = str(Path.home().resolve())
    cwd_resolved = str(Path.cwd().resolve())
    if not (
        s == home_resolved
        or s.startswith(home_resolved + os.sep)
        or s == cwd_resolved
        or s.startswith(cwd_resolved + os.sep)
    ):
        return None, "output_path must resolve under $HOME or cwd"
    return resolved, None


def _open_output_fd(target: Path) -> int:
    """``O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW|O_CLOEXEC`` mode 0o600 (T19.c/d)."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(str(target), flags, 0o600)


def _serialise_row(r: Any) -> dict:
    """Map a SQLAlchemy ``MemoryMetadataModel`` row to the dump-row schema.

    Covers every column of the current ``MemoryMetadataModel`` plus the
    archive's ``imported_from`` provenance field. The dead U3 ``is_federated``
    flag is gone.
    """

    def _ts(v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out: str = v.strftime("%Y-%m-%dT%H:%M:%SZ")
            return out
        return str(v)

    return {
        "id": str(r.id) if r.id is not None else "",
        "key": str(r.key),
        "memory_type": str(r.memory_type),
        "scope": str(r.scope),
        "scope_id": r.scope_id if r.scope_id is None else str(r.scope_id),
        "file_path": str(r.file_path) if r.file_path else "",
        "tags": str(r.tags or ""),
        "source_provider": (
            r.source_provider if r.source_provider is None else str(r.source_provider)
        ),
        "source_terminal_id": (
            r.source_terminal_id if r.source_terminal_id is None else str(r.source_terminal_id)
        ),
        "token_estimate": int(r.token_estimate) if r.token_estimate is not None else None,
        "created_at": _ts(r.created_at),
        "updated_at": _ts(r.updated_at),
        "last_compiled_at": _ts(getattr(r, "last_compiled_at", None)),
        "access_count": int(getattr(r, "access_count", 0) or 0),
        "last_accessed_at": _ts(getattr(r, "last_accessed_at", None)),
        "related_keys": (
            getattr(r, "related_keys", None)
            if getattr(r, "related_keys", None) is None
            else str(r.related_keys)
        ),
        # Import-side provenance. Not a column on this model — a freshly
        # exported row carries no import history — but the archive's
        # closed-key dump-row contract requires the field, so emit ``None``.
        # (This is distinct from the dead U3 ``is_federated`` flag, which was
        # ripped out.)
        "imported_from": (
            getattr(r, "imported_from", None)
            if getattr(r, "imported_from", None) is None
            else str(r.imported_from)
        ),
    }


def _fetch_export_rows(svc: Any, scope: str, scope_id: Optional[str], include_global: bool) -> list:
    """SELECT rows for the export. Excludes ``agent`` unconditionally (T11)."""
    from cli_agent_orchestrator.clients.database import MemoryMetadataModel

    rows: list = []
    with svc._get_db_session() as db:
        # Primary scope
        q = db.query(MemoryMetadataModel).filter(MemoryMetadataModel.scope == scope)
        if scope_id is not None:
            q = q.filter(MemoryMetadataModel.scope_id == scope_id)
        else:
            q = q.filter(MemoryMetadataModel.scope_id.is_(None))
        for r in q.all():
            rows.append(_serialise_row(r))

        # Session rows that share the project container
        if scope == "project" and scope_id is not None:
            q2 = (
                db.query(MemoryMetadataModel)
                .filter(MemoryMetadataModel.scope == "session")
                .filter(MemoryMetadataModel.scope_id == scope_id)
            )
            for r in q2.all():
                rows.append(_serialise_row(r))

        if include_global:
            qg = (
                db.query(MemoryMetadataModel)
                .filter(MemoryMetadataModel.scope == "global")
                .filter(MemoryMetadataModel.scope_id.is_(None))
            )
            for r in qg.all():
                rows.append(_serialise_row(r))

    # Deterministic order: lex sort by (scope, scope_id, key) so two
    # exports of the same DB produce identical bytes (devsecops T7).
    rows.sort(key=lambda r: (r["scope"], r["scope_id"] or "", r["key"]))
    return rows


def _read_wiki_file_bytes(svc: Any, scope: str, scope_id: Optional[str], key: str) -> bytes:
    try:
        path = svc.get_wiki_path(scope, scope_id, key)
    except Exception:
        return b""
    try:
        return Path(str(path)).read_bytes()
    except OSError:
        return b""


def _build_index_md(rows: list) -> bytes:
    lines: list = ["# CAO Memory Index", "", ""]
    by_scope: dict = {}
    for r in rows:
        by_scope.setdefault(r["scope"], []).append(r)
    for s in sorted(by_scope.keys()):
        lines.append(f"## {s}")
        for r in by_scope[s]:
            ts = r.get("updated_at") or ""
            tok = r.get("token_estimate") or 0
            lines.append(
                f"- [{r['key']}]({r['scope']}/{r['key']}.md) — "
                f"type:{r['memory_type']} tags:{r.get('tags', '')} "
                f"~{tok}tok updated:{ts}"
            )
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------


async def export(
    scope: str,
    scope_id: Optional[str],
    *,
    output_path: Path,
    include_global: bool = False,
    actor: str = "cli",
) -> ExportReport:
    """Export ``(scope, scope_id)`` and project siblings to a tar.gz archive."""
    started = datetime.now(timezone.utc)
    if not _is_memory_enabled_safe():
        return _empty_report(output_path, actor)

    if scope not in IMPORTABLE_SCOPES:
        return _error_report(
            output_path,
            actor,
            started,
            [f"scope {scope!r} not exportable (agent banned in U2)"],
        )

    resolved_output, err = _validate_output_path(output_path)
    if err is not None:
        try:
            await write_audit("export_failed", f"export_failed: {err}", actor=actor, reason=err)
        except Exception:
            pass
        return _error_report(output_path, actor, started, [err])

    from cli_agent_orchestrator.services.memory_service import MemoryService

    svc = MemoryService(base_dir=MEMORY_BASE_DIR)

    # Resolve project identity + the tier that produced it (id_kind).
    project_id, id_kind = _resolve_project_identity(scope, scope_id)
    if not project_id:
        project_id = "global"

    await write_audit(
        "export_started",
        f"export started: scope={scope}",
        scope=scope,
        scope_id=scope_id or "",
        output_path=str(resolved_output),
        actor=actor,
    )

    try:
        rows = _fetch_export_rows(svc, scope, scope_id, include_global)
        wiki_files: dict = {}
        for r in rows:
            data = _read_wiki_file_bytes(svc, r["scope"], r["scope_id"], r["key"])
            if data:
                wiki_files[r["key"]] = data

        index_md = _build_index_md(rows)
        scopes_present = sorted({r["scope"] for r in rows})
        if not scopes_present:
            scopes_present = [scope]

        manifest = {
            "format_version": SUPPORTED_FORMAT_VERSION,
            "project_id": project_id,
            "id_kind": id_kind,
            "created_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "exported_by": "cao 0.1",
            "scope_set": scopes_present,
            "n_wiki_files": len(wiki_files),
            "n_metadata_rows": len(rows),
            "content_hash": "sha256:" + ("0" * 64),
        }
        # Defensive: ``exported_by`` must satisfy the closed regex contract.
        eb_value = str(manifest["exported_by"])
        if not EXPORTED_BY_RE.match(eb_value):
            manifest["exported_by"] = "cao 0.1"

        # Compute final content hash, rewrite manifest, build tar.
        content_hash = compute_content_hash(
            manifest=manifest,
            dump_rows=rows,
            index_md=index_md,
            wiki_files=wiki_files,
        )
        manifest["content_hash"] = content_hash

        # Build the archive in-memory then write atomically.
        manifest_bytes = _json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        dump_bytes = _json.dumps(
            rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

        tar_buf = io.BytesIO()
        # Use deterministic mtime for reproducibility (T7 spirit).
        mtime = int(started.timestamp())

        def _add(name: str, data: bytes, tar: tarfile.TarFile) -> None:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o600
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(data))

        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            _add("manifest.json", manifest_bytes, tar)
            _add("sqlite-dump.json", dump_bytes, tar)
            _add("index.md", index_md, tar)
            for key in sorted(wiki_files.keys()):
                _add(f"wiki/{key}.md", wiki_files[key], tar)

        # Wrap in gzip with deterministic mtime (set explicitly via gzip).
        gz_buf = io.BytesIO()
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=gz_buf,
            mtime=mtime,
            compresslevel=6,
        ) as gz:
            gz.write(tar_buf.getvalue())
        archive_bytes = gz_buf.getvalue()

        # Atomic write to output path with O_EXCL refuse-overwrite.
        try:
            fd = _open_output_fd(resolved_output)
        except FileExistsError:
            err_msg = "output file already exists; refuse to overwrite"
            await write_audit(
                "export_failed",
                f"export_failed: {err_msg}",
                actor=actor,
                reason=err_msg,
            )
            return _error_report(
                output_path,
                actor,
                started,
                [err_msg],
                project_id=project_id,
                id_kind=id_kind,
                n_wiki_files=len(wiki_files),
                n_metadata_rows=len(rows),
                content_hash=content_hash,
            )
        try:
            os.write(fd, archive_bytes)
        finally:
            os.close(fd)
        if platform.system() != "Windows":
            try:
                os.chmod(str(resolved_output), 0o600)
            except OSError as e:
                logger.debug(f"export chmod failed (non-fatal): {e}")

        bytes_written = len(archive_bytes)
        ended = datetime.now(timezone.utc)
        await write_audit(
            "export_completed",
            f"export completed: {len(wiki_files)} wiki, {len(rows)} rows",
            n_wiki_files=str(len(wiki_files)),
            n_metadata_rows=str(len(rows)),
            content_hash=content_hash,
            bytes_written=str(bytes_written),
        )
        return ExportReport(
            archive_path=output_path,
            project_id=str(manifest["project_id"]),
            id_kind=id_kind,
            format_version=SUPPORTED_FORMAT_VERSION,
            n_wiki_files=len(wiki_files),
            n_metadata_rows=len(rows),
            bytes_written=bytes_written,
            content_hash=content_hash,
            started_at=started,
            ended_at=ended,
            actor=actor,
            errors=[],
        )
    except Exception as e:  # noqa: BLE001 — non-blocking promise
        reason = f"{type(e).__name__}: {str(e)[:160]}"
        try:
            await write_audit(
                "export_failed", f"export_failed: {reason}", actor=actor, reason=reason
            )
        except Exception:
            pass
        return _error_report(
            output_path,
            actor,
            started,
            [reason],
            project_id=project_id,
            id_kind=id_kind,
        )
