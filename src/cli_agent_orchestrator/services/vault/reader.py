"""Read indexed vault candidates through one confined filesystem boundary."""

from __future__ import annotations

import errno
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, func

from cli_agent_orchestrator.clients.database import (
    MemoryMetadataModel,
    SessionLocal,
    VaultNoteModel,
    VaultRecallCounterModel,
)
from cli_agent_orchestrator.models.memory import Memory
from cli_agent_orchestrator.services.memory_format import normalize_memory_tags
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.parser import split_frontmatter

_TRUNCATION_MARKER = "\n\n[Content truncated for recall]"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VaultCandidate:
    """An indexed vault row. Paths remain internal to this read boundary."""

    binding: VaultBinding
    metadata: MemoryMetadataModel
    note: VaultNoteModel


def resolve_candidates(
    binding: VaultBinding,
    *,
    keys: Optional[Iterable[str]] = None,
    scope: str,
    scope_id: Optional[str],
    require_injectable: bool,
) -> list[VaultCandidate]:
    """Return indexed candidates for one already-resolved vault binding."""
    if require_injectable and not binding.inject:
        return []
    if binding.scope != scope or binding.scope_id != scope_id:
        return []

    with SessionLocal() as db:
        query = (
            db.query(MemoryMetadataModel, VaultNoteModel)
            .join(
                VaultNoteModel,
                and_(
                    VaultNoteModel.cao_key == MemoryMetadataModel.key,
                    VaultNoteModel.scope == MemoryMetadataModel.scope,
                    func.coalesce(VaultNoteModel.scope_id, "")
                    == func.coalesce(MemoryMetadataModel.scope_id, ""),
                ),
            )
            .filter(
                MemoryMetadataModel.source_kind == "vault",
                MemoryMetadataModel.scope == scope,
                VaultNoteModel.vault_id == binding.vault_id,
                VaultNoteModel.status == "indexed",
            )
        )
        if scope_id is None:
            query = query.filter(MemoryMetadataModel.scope_id.is_(None))
        else:
            query = query.filter(MemoryMetadataModel.scope_id == scope_id)
        if keys is not None:
            key_list = list(keys)
            if not key_list:
                return []
            query = query.filter(MemoryMetadataModel.key.in_(key_list))
        rows = query.order_by(MemoryMetadataModel.updated_at.desc(), MemoryMetadataModel.key).all()
        return [
            VaultCandidate(binding=binding, metadata=metadata, note=note) for metadata, note in rows
        ]


def load_candidate(
    candidate: VaultCandidate,
    *,
    max_body_chars: int,
    require_injectable: bool,
) -> Optional[Memory]:
    """Load one candidate, silently skipping unreadable or escaped rows.

    This function owns every taint-reachable vault read sink. It returns a
    built memory rather than a path so callers cannot accidentally bypass its
    confinement check. Stale notes are served with ``index_freshness="stale"``
    in ordinary recall; release one has no watcher or implicit reconcile.
    """
    if require_injectable and not candidate.binding.inject:
        return None
    relative_path = candidate.metadata.file_path
    root = os.path.realpath(candidate.binding.root)
    path = os.path.join(root, relative_path)
    real_path = os.path.realpath(path)
    try:
        fd, expected = _open_confined_fd(root, real_path)
    except ValueError as exc:
        arm = "eloop" if str(exc) == "symlink escapes vault root" else "lexical"
        logger.warning("vault recall containment refusal arm=%s", arm)
        _record_path_escape(candidate)
        return None
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino):
            _record_path_escape(candidate)
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
        after = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if _stat_identity(before) != _stat_identity(after):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return None

    try:
        # Reconciliation has already enforced the configured frontmatter cap.
        # This read-side split only separates the indexed body.
        region = split_frontmatter(text, 65536)
    except ValueError:
        return None
    body = _strip_leading_h1(region.body)
    truncated = len(body) > max_body_chars
    if truncated:
        body = body[: max(0, max_body_chars - len(_TRUNCATION_MARKER))] + _TRUNCATION_MARKER

    metadata = candidate.metadata
    note = candidate.note
    mtime = datetime.fromtimestamp(after.st_mtime, tz=timezone.utc)
    indexed_at = note.last_reconciled_at
    freshness = (
        "fresh"
        if after.st_size == note.size_bytes and after.st_mtime_ns == note.mtime_ns
        else "stale"
    )
    return Memory(
        id=note.note_uid,
        key=metadata.key,
        memory_type=metadata.memory_type,
        scope=metadata.scope,
        scope_id=metadata.scope_id,
        file_path=path,
        tags=normalize_memory_tags(metadata.tags),
        source_provider=metadata.source_provider,
        source_terminal_id=metadata.source_terminal_id,
        created_at=metadata.created_at or mtime,
        updated_at=mtime,
        access_count=int(metadata.access_count or 0),
        last_compiled_at=metadata.last_compiled_at,
        related_keys=metadata.related_keys,
        content=body,
        source_kind="vault",
        source_path=note.vault_relpath,
        indexed_at=indexed_at,
        index_freshness=freshness,
        content_truncated=truncated,
        token_estimate=len(body) // 4,
    )


def increment_counter(vault_id: str, counter_name: str, amount: int) -> None:
    """Persist a positive recall outcome count without recording note content."""
    if amount <= 0:
        return
    try:
        with SessionLocal() as db:
            row = db.get(VaultRecallCounterModel, (vault_id, counter_name))
            if row is None:
                db.add(
                    VaultRecallCounterModel(
                        vault_id=vault_id, counter_name=counter_name, value=amount
                    )
                )
            else:
                row.value += amount
            db.commit()
    except Exception as exc:  # noqa: BLE001 -- recall must remain available
        logger.warning("vault recall counter update failed: %s", exc)


def _strip_leading_h1(body: str) -> str:
    """Remove one ordinary Markdown H1 without imposing native wiki syntax."""
    lines = body.lstrip("\n").splitlines()
    if lines and lines[0].startswith("# ") and not lines[0].startswith("## "):
        return "\n".join(lines[1:]).lstrip("\n")
    return body


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Fields whose change means a caller must not consume this read window."""
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _open_confined_fd(root: str, real_path: str) -> tuple[int, os.stat_result]:
    """Traverse a guarded resolved path from a held root fd.

    This intentionally changes read-time behavior: a symlinked-but-internal
    component resolves before traversal and succeeds. Scan refuses such
    components, so an indexed note cannot normally take that path. A component
    swapped after resolution still fails closed via ``O_NOFOLLOW``.
    """
    if not real_path.startswith(root + os.sep):
        raise ValueError("path escapes vault root")
    # CAO config accepts POSIX absolute roots only; U1 rejects Windows roots,
    # where ``realpath`` may preserve a trailing separator.
    segments = real_path[len(root) + 1 :].split(os.sep)
    directory_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    try:
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        # The root can be swapped to a symlink after realpath and before this
        # held-fd walk begins, so this initial open needs its own normalisation.
        _raise_if_symlink_error(exc, root, None)
        raise
    parent_fd = root_fd
    try:
        for segment in segments[:-1]:
            try:
                child_fd = os.open(segment, directory_flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_if_symlink_error(exc, segment, parent_fd)
                raise
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = child_fd
        expected = os.stat(segments[-1], dir_fd=parent_fd, follow_symlinks=False)
        try:
            fd = os.open(segments[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            _raise_if_symlink_error(exc, segments[-1], parent_fd)
            raise
        return fd, expected
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def _raise_if_symlink_error(error: OSError, segment: str, parent_fd: int | None) -> None:
    """Normalise platform-specific ``O_NOFOLLOW`` symlink errors to ValueError."""
    # Linux and macOS report O_NOFOLLOW symlinks as ELOOP, including
    # O_DIRECTORY opens. A symlink-to-non-directory may instead be ENOTDIR,
    # which is confirmed below; historic BSD EMLINK is not a CAO platform.
    if error.errno == errno.ELOOP:
        raise ValueError("symlink escapes vault root") from error
    if error.errno != errno.ENOTDIR:
        return
    try:
        mode = os.stat(segment, dir_fd=parent_fd, follow_symlinks=False).st_mode
    except OSError:
        return
    if stat.S_ISLNK(mode):
        raise ValueError("symlink escapes vault root") from error


def _record_path_escape(candidate: VaultCandidate) -> None:
    """Make recall-side containment refusals visible without exposing paths."""
    increment_counter(candidate.binding.vault_id, "path_escapes_root", 1)
