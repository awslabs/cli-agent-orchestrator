"""Read indexed vault candidates through one confined filesystem boundary."""

from __future__ import annotations

import errno
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator, Literal, Optional, Sequence

from sqlalchemy import and_, func

from cli_agent_orchestrator.clients.database import (
    MemoryMetadataModel,
    SessionLocal,
    TerminalModel,
    VaultNoteModel,
    VaultRecallCounterModel,
)
from cli_agent_orchestrator.models.memory import Memory
from cli_agent_orchestrator.services.memory_format import normalize_memory_tags
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.parser import split_frontmatter

_TRUNCATION_MARKER = "\n\n[Content truncated for recall]"
MEMORY_MANAGER_PROFILE = "memory_manager"
MEMORY_MANAGER_POLICY_ARM = "memory_manager"
logger = logging.getLogger(__name__)


class _NoRequesterIdentity:
    """Marker for server projections that have no requester terminal."""


# A server projection must not inherit the process's ambient terminal identity.
NO_REQUESTER_IDENTITY = _NoRequesterIdentity()
RequesterIdentity = Optional[str] | _NoRequesterIdentity


@dataclass(frozen=True)
class VaultCandidate:
    """An indexed vault row. Paths remain internal to this read boundary."""

    binding: VaultBinding
    metadata: MemoryMetadataModel
    note: VaultNoteModel
    require_injectable: bool
    policy_arm: str


@dataclass(frozen=True)
class VaultInjectionPolicy:
    """One immutable gate decision shared by every binding in a recall."""

    effective_require_injectable: bool
    arm: str
    is_curator: Optional[bool]
    reason: Optional[str] = None


@dataclass(frozen=True, eq=False)
class VaultCandidateResolution(Sequence[VaultCandidate]):
    """Candidates and the observable policy/exit arm for one vault binding."""

    candidates: tuple[VaultCandidate, ...]
    policy_arm: str
    exit_arm: str

    def __iter__(self) -> Iterator[VaultCandidate]:
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]

    def __bool__(self) -> bool:
        return bool(self.candidates)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, VaultCandidateResolution):
            return (
                self.candidates,
                self.policy_arm,
                self.exit_arm,
            ) == (
                other.candidates,
                other.policy_arm,
                other.exit_arm,
            )
        return list(self.candidates) == other


@dataclass(frozen=True, eq=False)
class VaultCandidateBatch(Sequence[VaultCandidate]):
    """Aggregated candidates retain each binding's resolution arm."""

    candidates: tuple[VaultCandidate, ...]
    resolutions: tuple[VaultCandidateResolution, ...]

    def __iter__(self) -> Iterator[VaultCandidate]:
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]

    def __bool__(self) -> bool:
        return bool(self.candidates)


def resolve_candidates(
    binding: VaultBinding,
    *,
    keys: Optional[Iterable[str]] = None,
    scope: str,
    scope_id: Optional[str],
    require_injectable: bool,
    terminal_id: RequesterIdentity,
    consumer: Literal["injected_context", "explicit_recall"],
    policy: Optional[VaultInjectionPolicy] = None,
) -> VaultCandidateResolution:
    """Return indexed candidates for one already-resolved vault binding."""
    policy = policy or _resolve_injection_policy(
        require_injectable,
        consumer=consumer,
        terminal_id=terminal_id,
    )
    if not binding.index:
        return _resolution(policy, (), "not_indexable")
    # Curator recall is inserted verbatim into another terminal's context.
    # Agent-scoped mappings are explicit-recall-only, matching the builder.
    if policy.is_curator is not False and binding.scope == "agent":
        return _resolution(policy, (), "curator_agent_scope_refused")
    # Callers may tighten the policy but can never waive the curator's gate.
    if policy.effective_require_injectable and not binding.inject:
        return _resolution(policy, (), "not_injectable")
    # Direct-call contract guard. Production callers derive these values from
    # the binding, but a future direct caller must not cross scopes silently.
    if binding.scope != scope or binding.scope_id != scope_id:
        return _resolution(policy, (), "scope_mismatch")

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
                return _resolution(policy, (), "empty_keys")
            query = query.filter(MemoryMetadataModel.key.in_(key_list))
        rows = query.order_by(MemoryMetadataModel.updated_at.desc(), MemoryMetadataModel.key).all()
        if not rows:
            return _resolution(policy, (), "no_rows")
        candidates = tuple(
            VaultCandidate(
                binding=binding,
                metadata=metadata,
                note=note,
                require_injectable=policy.effective_require_injectable,
                policy_arm=policy.arm,
            )
            for metadata, note in rows
        )
        return _resolution(policy, candidates, "candidates")


def _resolution(
    policy: VaultInjectionPolicy,
    candidates: tuple[VaultCandidate, ...],
    exit_arm: str,
) -> VaultCandidateResolution:
    logger.warning(
        "vault candidate resolution policy_arm=%s exit_arm=%s candidates=%d reason=%s",
        policy.arm,
        exit_arm,
        len(candidates),
        policy.reason or "none",
    )
    return VaultCandidateResolution(candidates, policy.arm, exit_arm)


def _resolve_injection_policy(
    require_injectable: bool,
    *,
    consumer: Literal["injected_context", "explicit_recall"],
    terminal_id: RequesterIdentity,
) -> VaultInjectionPolicy:
    """Combine a caller's request with the requesting terminal's policy."""
    effective_require_injectable = consumer != "explicit_recall" or require_injectable
    identityless_projection = terminal_id is NO_REQUESTER_IDENTITY
    ambient_terminal_id = None if identityless_projection else os.environ.get("CAO_TERMINAL_ID")
    requester_terminal_id = None if identityless_projection else terminal_id or ambient_terminal_id
    if not requester_terminal_id:
        policy = VaultInjectionPolicy(effective_require_injectable, "no_terminal", None)
    else:
        try:
            with SessionLocal() as db:
                terminal = db.get(TerminalModel, requester_terminal_id)
        except Exception as exc:  # noqa: BLE001 -- unresolved identity must fail closed
            policy = VaultInjectionPolicy(True, "unresolved", None, f"lookup_failed:{exc}")
        else:
            if terminal is None or not terminal.agent_profile:
                policy = VaultInjectionPolicy(True, "unresolved", None, "terminal_not_found")
            elif terminal.agent_profile == MEMORY_MANAGER_PROFILE:
                # Curators use the explicit-recall MCP surface, so ``consumer`` alone
                # cannot identify their injected destination. Identity is the second,
                # non-waivable tightening layer.
                policy = VaultInjectionPolicy(True, MEMORY_MANAGER_POLICY_ARM, True)
            else:
                policy = VaultInjectionPolicy(effective_require_injectable, "caller", False)
    _log_identity_anomaly(
        policy,
        terminal_id if isinstance(terminal_id, str) else None,
        ambient_terminal_id,
        requester_terminal_id,
    )
    return policy


def _log_identity_anomaly(
    policy: VaultInjectionPolicy,
    terminal_id: Optional[str],
    ambient_terminal_id: Optional[str],
    requester_terminal_id: Optional[str],
) -> None:
    """Expose use of ambient identity without changing explicit-ID precedence."""
    if terminal_id is None and ambient_terminal_id:
        logger.warning(
            "vault injection policy identity_source=ambient resolved_terminal_id=%s "
            "policy_arm=%s",
            requester_terminal_id,
            policy.arm,
        )
    elif terminal_id and ambient_terminal_id and terminal_id != ambient_terminal_id:
        logger.warning(
            "vault injection policy identity_mismatch explicit_terminal_id=%s "
            "ambient_terminal_id=%s resolved_terminal_id=%s policy_arm=%s",
            terminal_id,
            ambient_terminal_id,
            requester_terminal_id,
            policy.arm,
        )


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
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


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
