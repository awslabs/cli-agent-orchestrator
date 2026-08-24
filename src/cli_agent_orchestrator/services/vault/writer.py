"""Managed-folder vault note writes with vault-local atomic staging."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from cli_agent_orchestrator.services.secret_gate import scan_for_secrets
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import VaultSpec
from cli_agent_orchestrator.services.vault.parser import (
    frontmatter_boundary,
    locate_top_level_cao_blocks,
)
from cli_agent_orchestrator.utils.atomic_file import _file_lock, _lock_path_for
from cli_agent_orchestrator.utils.path_validation import (
    safe_join_under_base,
    validate_path_component,
)

logger = logging.getLogger(__name__)


class VaultWriteConflictError(RuntimeError):
    """Raised when a managed note changed since its reconciled content hash."""


class VaultSecretWriteError(ValueError):
    """Raised when a reject-mode vault mapping receives credential-shaped content."""


@dataclass(frozen=True)
class VaultWriteResult:
    """Content-free result of one managed vault-note write."""

    path: str
    content_sha256: str
    ignored_frontmatter_keys: tuple[str, ...] = ()


def write_managed_note(
    *,
    vault: VaultSpec,
    binding: VaultBinding,
    key: str,
    body: str,
    cao: Mapping[str, Any],
    expected_content_sha256: Optional[str],
    refresh: Optional[Callable[[str], None]] = None,
    frontmatter: Optional[Mapping[str, Any]] = None,
) -> VaultWriteResult:
    """Replace one CAO-owned note and refresh its projection after publication.

    ``body`` is the full body that the caller has rendered. ``frontmatter`` may
    seed only the standard ``tags`` and ``created`` keys on a new note; an
    existing user-owned value is preserved and reported as ignored. ``refresh``
    runs after the durable publish so callers can perform the scoped
    reconciliation without coupling the filesystem safety boundary to database
    access.
    """
    if vault.id != binding.vault_id:
        raise ValueError("vault binding does not belong to the requested vault")
    if not binding.writable:
        raise ValueError(f"vault mapping {binding.mapping.folder!r} is not writable")
    seeded_frontmatter = _validated_seed_frontmatter(frontmatter)

    managed_base, target = _managed_target(vault, key)
    lock_path = _lock_path_for(Path(os.path.realpath(target)))

    with _file_lock(lock_path, timeout=10.0):
        existing = _read_contained_text(managed_base, target)
        _check_expected_hash(target, existing, expected_content_sha256)
        boundary = _existing_frontmatter_boundary(target, existing)
        try:
            rendered, ignored_frontmatter_keys = _merge_frontmatter(
                existing,
                body,
                key=key,
                cao=cao,
                boundary=boundary,
                seeded_frontmatter=seeded_frontmatter,
            )
        except ValueError as exc:
            raise _conflict(target) from exc
        rendered_cao = _render_cao(key, cao, boundary[1] if boundary is not None else "\n")
        _check_secret_gate(body, rendered_cao, binding)
        mode = _target_mode(target)
        _publish_managed_note(vault.root, managed_base, target, rendered, mode)

    result = VaultWriteResult(
        path=target,
        content_sha256=_sha256(rendered),
        ignored_frontmatter_keys=ignored_frontmatter_keys,
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


def _managed_target(vault: VaultSpec, key: str) -> tuple[str, str]:
    validate_path_component(key, "vault key")
    components = tuple(vault.managed_folder.split("/"))
    managed_base = safe_join_under_base(
        vault.root, *components, description="managed_folder component"
    )
    target = safe_join_under_base(vault.root, *components, f"{key}.md", description="vault key")
    return managed_base, target


def _read_contained_text(managed_base: str, target: str) -> str:
    if not os.path.exists(target):
        return ""
    managed_base = os.path.realpath(managed_base)
    target = os.path.realpath(target)
    if not target.startswith(managed_base + os.sep):
        raise ValueError("vault write target escapes managed_folder")
    # F16: bare str plus the inline positive guard immediately above this sink.
    with open(target, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


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
    rendered_cao = _render_cao(key, cao, newline, indentation=indentation)

    if retained and not retained.endswith(("\n", "\r")):
        retained += newline
    frontmatter = f"---{newline}{retained}{rendered_seeds}{rendered_cao}---{newline}"
    return prefix + frontmatter + (body if body else existing_body), ignored


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
    for region, content in (("body", body), ("cao", rendered_cao)):
        secret_pattern = scan_for_secrets(content)
        if secret_pattern is None:
            continue
        if binding.mapping.secret_gate == "reject":
            logger.warning(
                "vault_write_secret_rejected pattern=%s region=%s",
                secret_pattern,
                region,
            )
            raise VaultSecretWriteError(
                f"vault write rejected: {region} matched credential pattern {secret_pattern!r}"
            )
        logger.warning("vault_write_secret_warn pattern=%s region=%s", secret_pattern, region)
        continue


def _umask_default_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _target_mode(target: str) -> int:
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        return _umask_default_mode()


def _publish_managed_note(
    vault_root: str, managed_base: str, target: str, content: str, mode: int
) -> None:
    """Stage under ``managed_folder`` and atomically replace the intended note."""
    vault_root = os.path.realpath(vault_root)
    managed_base = os.path.realpath(managed_base)
    if not managed_base.startswith(vault_root + os.sep):
        raise ValueError("vault write target escapes managed_folder")
    if not os.path.isdir(managed_base):
        raise FileNotFoundError(f"managed_folder does not exist: {managed_base!r}")

    # F16: bare str and a single-positive guard immediately above this sink.
    fd, temp_path = tempfile.mkstemp(prefix="_cao-", suffix=".tmp", dir=managed_base)
    try:
        os.close(fd)
        temp_path = os.path.realpath(temp_path)
        if not temp_path.startswith(managed_base + os.sep):
            raise ValueError("vault write target escapes managed_folder")
        # F16: bare str and a single-positive guard immediately above this sink.
        with open(temp_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        target = os.path.realpath(target)
        if not target.startswith(managed_base + os.sep):
            raise ValueError("vault write target escapes managed_folder")
        if not temp_path.startswith(managed_base + os.sep):
            raise ValueError("vault write target escapes managed_folder")
        # F16: both bare-string paths have single-positive guards immediately above.
        os.replace(temp_path, target)
        # Unlike the shared helper, this writer fsyncs the parent directory so
        # a completed replace survives a crash as well as a process failure.
        directory_fd = os.open(managed_base, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        # A crash or failed replace must not leave a scanner-invisible _cao temp.
        try:
            temp_path = os.path.realpath(temp_path)
            if temp_path.startswith(managed_base + os.sep):
                # F16: bare str and a single-positive guard immediately above this sink.
                os.unlink(temp_path)
            else:
                logger.warning("vault_write_temp_cleanup_skipped_outside_managed_folder")
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("vault_write_temp_cleanup_failed", exc_info=True)


def _sha256(content: str) -> str:
    normalized = content[1:] if content.startswith("\ufeff") else content
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
