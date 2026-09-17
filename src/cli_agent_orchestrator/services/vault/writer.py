"""Managed-folder vault note writes with vault-local atomic staging."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import secrets
import stat
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Optional

import yaml

from cli_agent_orchestrator.services.memory_append import (
    MemoryAppendEntry,
    append_section,
)
from cli_agent_orchestrator.services.memory_reconciliation import _first_symlink_component
from cli_agent_orchestrator.services.secret_gate import scan_for_secrets
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.boundary import normalize_relpath
from cli_agent_orchestrator.services.vault.config import VaultSpec
from cli_agent_orchestrator.services.vault.parser import (
    FrontmatterRegion,
    frontmatter_boundary,
    locate_top_level_cao_blocks,
    split_frontmatter,
)
from cli_agent_orchestrator.services.vault.vault_lock import vault_projection_lock
from cli_agent_orchestrator.utils.atomic_file import _file_lock, _lock_path_for
from cli_agent_orchestrator.utils.path_validation import validate_path_component

logger = logging.getLogger(__name__)
_boundary_write_refusals: Counter[str] = Counter()
_boundary_write_refusals_lock = Lock()


class VaultWriteConflictError(RuntimeError):
    """Raised when a managed note changed since its reconciled content hash."""


class VaultSecretWriteError(ValueError):
    """Raised when a reject-mode vault mapping receives credential-shaped content."""


class VaultWriteBoundaryError(ValueError):
    """Raised when a managed write cannot stay on its configured lexical path."""


@dataclass(frozen=True)
class VaultWriteResult:
    """Content-free result of one managed vault-note write."""

    path: str
    content_sha256: str
    ignored_frontmatter_keys: tuple[str, ...] = ()
    first_section_at: Optional[datetime] = None
    timestamp_clamped: bool = False


def boundary_write_refusal_count(vault_id: Optional[str] = None) -> int:
    """Return the process-local, content-free managed-boundary refusal count."""
    with _boundary_write_refusals_lock:
        if vault_id is None:
            return sum(_boundary_write_refusals.values())
        return _boundary_write_refusals[vault_id]


@contextmanager
def _count_boundary_refusal(vault_id: str) -> Iterator[None]:
    try:
        yield
    except VaultWriteBoundaryError:
        with _boundary_write_refusals_lock:
            _boundary_write_refusals[vault_id] += 1
        raise


def write_managed_note(
    *,
    vault: VaultSpec,
    binding: VaultBinding,
    key: str,
    body: Optional[str],
    cao: Mapping[str, Any],
    expected_content_sha256: Optional[str],
    refresh: Optional[Callable[[str], None]] = None,
    frontmatter: Optional[Mapping[str, Any]] = None,
    mode: Literal["replace", "append"] = "replace",
    entry: Optional[MemoryAppendEntry] = None,
) -> VaultWriteResult:
    """Write one CAO-owned note and refresh its projection after publication.

    Replace mode keeps the direct-writer contract. Append mode accepts only an
    entry and renders against the body read through the held managed descriptor.
    ``frontmatter`` may seed only the standard ``tags`` and ``created`` keys on
    a new note; an existing user-owned value is preserved and reported as
    ignored. ``refresh`` runs after the durable publish and note-flock release.
    """
    if vault.id != binding.vault_id:
        raise ValueError("vault binding does not belong to the requested vault")
    if not binding.writable:
        raise ValueError(f"vault mapping {binding.mapping.folder!r} is not writable")
    if mode not in {"replace", "append"}:
        raise ValueError(f"unsupported vault write mode: {mode!r}")
    if mode == "replace" and (body is None or entry is not None):
        raise ValueError("replace mode requires body and does not accept entry")
    if mode == "append" and (body is not None or entry is None):
        raise ValueError("append mode requires entry and does not accept body")
    seeded_frontmatter = _validated_seed_frontmatter(frontmatter)

    with vault_projection_lock(vault):
        root_real, managed_folder, managed_base, target_name, target = _managed_target(vault, key)
        lock_path = _lock_path_for(Path(target))

        with _count_boundary_refusal(vault.id), _file_lock(lock_path, timeout=10.0):
            symlink = _first_symlink_component(Path(managed_base), Path(root_real))
            if symlink is not None:
                raise VaultWriteBoundaryError(
                    f"vault managed_folder contains a symlinked component: {str(symlink)!r}"
                )
            managed_fd = _open_managed_dir_fd(root_real, managed_folder)
            try:
                existing = _read_contained_text(managed_fd, target_name, target)
                _check_expected_hash(target, existing, expected_content_sha256)
                boundary = _existing_frontmatter_boundary(target, existing)
                append_result = None
                rendered_body = body
                if mode == "append":
                    assert entry is not None
                    existing_body = boundary[0].body if boundary is not None else ""
                    append_result = append_section(existing_body, entry)
                    rendered_body = append_result.content
                assert rendered_body is not None
                try:
                    rendered, ignored_frontmatter_keys = _merge_frontmatter(
                        existing,
                        rendered_body,
                        key=key,
                        cao=cao,
                        boundary=boundary,
                        seeded_frontmatter=seeded_frontmatter,
                    )
                except ValueError as exc:
                    raise _conflict(target) from exc
                rendered_cao = _render_cao(
                    key,
                    _merge_cao_fields(existing, cao, boundary),
                    boundary[1] if boundary is not None else "\n",
                )
                _check_indexable(rendered, vault)
                secret_body = entry.content if entry is not None else rendered_body
                _check_secret_gate(secret_body, rendered_cao, binding)
                target_mode = _target_mode(managed_fd, target_name)
                _publish_managed_note(managed_fd, target_name, rendered, target_mode)
            finally:
                os.close(managed_fd)

        result = VaultWriteResult(
            path=target,
            content_sha256=_sha256(rendered),
            ignored_frontmatter_keys=ignored_frontmatter_keys,
            first_section_at=(
                append_result.first_section_at if append_result is not None else None
            ),
            timestamp_clamped=(
                append_result.timestamp_clamped if append_result is not None else False
            ),
        )
        if refresh is not None:
            try:
                refresh(target)
            except Exception as exc:
                # Import lazily: MemoryService imports this module for the vault arm.
                from cli_agent_orchestrator.services.memory_service import (
                    MemoryPartialWriteError,
                )

                raise MemoryPartialWriteError(
                    key=key,
                    scope=binding.scope,
                    scope_id=binding.scope_id,
                    file_path=target,
                ) from exc
        return result


def _managed_target(vault: VaultSpec, key: str) -> tuple[str, str, str, str, str]:
    key = validate_path_component(key, "vault key")
    managed_folder = normalize_relpath(vault.managed_folder)
    root_real = os.path.realpath(vault.root)
    managed_base = os.path.join(root_real, *managed_folder.split("/"))
    candidate = os.path.normpath(os.path.join(managed_base, f"{key}.md"))
    # This lexical same-value check is the shape CodeQL recognizes. It does not
    # replace the descriptor/O_NOFOLLOW TOCTOU controls below; realpath or
    # safe_join would consult mutable filesystem state and change path semantics.
    if not candidate.startswith(managed_base + os.sep):
        raise ValueError("vault note target must stay within the managed folder")
    target_name = os.path.basename(candidate)
    return root_real, managed_folder, managed_base, target_name, candidate


def _open_managed_dir_fd(root_real: str, managed_folder: str) -> int:
    """Open every managed-folder component without following a symlink."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(root_real, directory_flags)
    try:
        for component in normalize_relpath(managed_folder).split("/"):
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise VaultWriteBoundaryError(
                        "vault managed_folder contains a symlinked or non-directory component"
                    ) from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _read_contained_text(managed_fd: int, target_name: str, target: str) -> str:
    """Read the target through the same verified directory used to publish it."""
    target_name = validate_path_component(target_name, "vault note filename")
    try:
        fd = os.open(
            target_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=managed_fd,
        )
    except FileNotFoundError:
        return ""
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise VaultWriteBoundaryError(
                f"vault write target is a symlink or invalid entry: {target!r}"
            ) from exc
        raise
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise VaultWriteBoundaryError(f"vault write target is not a regular file: {target!r}")
        with os.fdopen(fd, "r", encoding="utf-8", newline="", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def _check_expected_hash(
    target: str, existing: str, expected_content_sha256: Optional[str]
) -> None:
    actual = _sha256(existing)
    if expected_content_sha256 is None:
        if existing:
            raise VaultWriteConflictError(
                f"vault note changed at {target!r}; run `cao memory vault reconcile --apply` before writing"
            )
        return
    if actual != expected_content_sha256:
        raise VaultWriteConflictError(
            f"vault note changed at {target!r}; run `cao memory vault reconcile --apply` before writing"
        )


def _existing_frontmatter_boundary(target: str, existing: str):
    if not existing:
        return None
    try:
        boundary = frontmatter_boundary(existing)
    except ValueError as exc:
        raise _conflict(target) from exc
    if boundary is None:
        raise _conflict(target)
    return boundary


def _conflict(target: str) -> VaultWriteConflictError:
    return VaultWriteConflictError(
        f"vault note changed at {target!r}; run `cao memory vault reconcile --apply` before writing"
    )


def _merge_frontmatter(
    existing: str,
    body: str,
    *,
    key: str,
    cao: Mapping[str, Any],
    boundary,
    seeded_frontmatter: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    """Preserve every non-``cao`` frontmatter byte while replacing ``cao``."""
    if boundary is None:
        prefix, raw, existing_body, newline = "", "", "", "\n"
    else:
        region, newline = boundary
        prefix = existing[: region.start]
        raw = _frontmatter_text_region(existing, region.start, region.end, newline)
        existing_body = region.body
    retained, indentation = _remove_cao_block(raw)
    existing_keys = _top_level_frontmatter_keys(raw)
    ignored = tuple(key for key in seeded_frontmatter if key in existing_keys)
    seeds = {key: value for key, value in seeded_frontmatter.items() if key not in existing_keys}
    rendered_seeds = _render_seed_frontmatter(seeds, newline)
    rendered_cao = _render_cao(
        key,
        _merge_cao_fields(existing, cao, boundary),
        newline,
        indentation=indentation,
    )

    if retained and not retained.endswith(("\n", "\r")):
        retained += newline
    frontmatter = f"---{newline}{retained}{rendered_seeds}{rendered_cao}---{newline}"
    return prefix + frontmatter + (body if body else existing_body), ignored


def _merge_cao_fields(
    existing: str,
    cao: Mapping[str, Any],
    boundary: Optional[tuple[FrontmatterRegion, str]],
) -> Mapping[str, Any]:
    """Update caller-owned CAO fields while retaining authored links."""
    merged = dict(cao)
    if "links" in merged or boundary is None:
        return merged
    region, _newline = boundary
    try:
        loaded = yaml.safe_load(region.raw)
    except yaml.YAMLError:
        return merged
    if not isinstance(loaded, Mapping):
        return merged
    existing_cao = loaded.get("cao")
    if isinstance(existing_cao, Mapping) and "links" in existing_cao:
        merged["links"] = existing_cao["links"]
    return merged


def _validated_seed_frontmatter(
    frontmatter: Optional[Mapping[str, Any]],
) -> Mapping[str, Any]:
    if frontmatter is None:
        return {}
    unsupported = sorted(set(frontmatter) - {"tags", "created"})
    if unsupported:
        raise ValueError(f"unsupported top-level frontmatter key: {unsupported[0]!r}")
    return dict(frontmatter)


def _top_level_frontmatter_keys(raw: str) -> set[str]:
    document = yaml.compose(raw, Loader=yaml.SafeLoader)
    if not isinstance(document, yaml.MappingNode):
        return set()
    return {
        key.value
        for key, _value in document.value
        if isinstance(key, yaml.ScalarNode) and key.value in {"tags", "created"}
    }


def _render_seed_frontmatter(values: Mapping[str, Any], newline: str) -> str:
    if not values:
        return ""
    rendered = yaml.safe_dump(
        dict(values),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return str(rendered.replace("\n", newline))


def _frontmatter_text_region(text: str, start: int, end: int, newline: str) -> str:
    """Return the original text between fences, including trailing blank lines.

    ``FrontmatterRegion.raw`` intentionally omits the newline that precedes
    the closing fence for parser compatibility. The writer must retain that
    byte when it represents a user's blank line after the ``cao`` block.
    """
    fenced = text[start:end]
    opening = f"---{newline}"
    closing = f"---{newline}"
    return fenced[len(opening) : -len(closing)]


def _remove_cao_block(raw: str) -> tuple[str, str]:
    """Remove semantic top-level ``cao`` entries while preserving all other bytes."""
    locations = locate_top_level_cao_blocks(raw)
    retained = raw
    for start, end in reversed(locations.spans):
        retained = retained[:start] + retained[end:]
    return retained, locations.indentation


def _render_cao(key: str, cao: Mapping[str, Any], newline: str, *, indentation: str = "") -> str:
    value = dict(cao)
    value["key"] = key
    value["managed"] = True
    rendered = yaml.safe_dump(
        {"cao": value},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return "".join(
        f"{indentation}{line}" if line else line
        for line in str(rendered.replace("\n", newline)).splitlines(keepends=True)
    )


def _check_secret_gate(body: str, rendered_cao: str, binding: VaultBinding) -> None:
    """Check authored body and generated metadata, preserving user frontmatter."""
    matched_pattern_name = scan_for_secrets(f"{body}\n{rendered_cao}")
    if matched_pattern_name is None:
        return
    if binding.mapping.secret_gate == "reject":
        from cli_agent_orchestrator.services.vault.binding import record_secret_gate_write_refusal

        record_secret_gate_write_refusal(binding.vault_id)
        logger.warning("vault_write_secret_rejected pattern=%s", matched_pattern_name)
        raise VaultSecretWriteError(
            f"vault write rejected: note matched credential pattern {matched_pattern_name!r}"
        )
    logger.warning("vault_write_secret_warn pattern=%s", matched_pattern_name)


def _check_indexable(rendered: str, vault: VaultSpec) -> None:
    """Reject bytes that reconciliation would quarantine or skip."""
    if "\x00" in rendered:
        raise ValueError("vault write contains NUL byte")
    if len(rendered.encode("utf-8")) > vault.max_note_bytes:
        raise ValueError("vault write exceeds max_note_bytes")
    try:
        split_frontmatter(rendered, vault.max_frontmatter_bytes)
    except ValueError as exc:
        if str(exc) == "frontmatter_too_large":
            raise ValueError("vault write exceeds max_frontmatter_bytes") from exc
        raise


def _umask_default_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _target_mode(managed_fd: int, target_name: str) -> int:
    target_name = validate_path_component(target_name, "vault note filename")
    try:
        metadata = os.stat(target_name, dir_fd=managed_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _umask_default_mode()
    if not stat.S_ISREG(metadata.st_mode):
        raise VaultWriteBoundaryError("vault write target is not a regular file")
    return stat.S_IMODE(metadata.st_mode)


def _publish_managed_note(managed_fd: int, target_name: str, content: str, mode: int) -> None:
    """Atomically replace one entry relative to a held managed-directory descriptor."""
    target_name = validate_path_component(target_name, "vault note filename")
    temp_name = ""
    fd = -1
    for _attempt in range(128):
        temp_name = f"_cao-{secrets.token_hex(12)}.tmp"
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode,
                dir_fd=managed_fd,
            )
            break
        except FileExistsError:
            continue
    if fd < 0:
        raise FileExistsError("unable to reserve a unique managed-note temp entry")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(
            temp_name,
            target_name,
            src_dir_fd=managed_fd,
            dst_dir_fd=managed_fd,
        )
        os.fsync(managed_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name, dir_fd=managed_fd)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("vault_write_temp_cleanup_failed", exc_info=True)


def _sha256(content: str) -> str:
    normalized = content[1:] if content.startswith("\ufeff") else content
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
