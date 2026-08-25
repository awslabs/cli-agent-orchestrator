"""Resolve a memory scope to its native or configured vault backing."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from threading import Lock
from typing import Optional, Union, cast

from cli_agent_orchestrator.clients.database import (
    get_project_id_by_alias,
    list_aliases_for_project,
)
from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeBinding:
    """A scope that continues to use the native CAO wiki."""

    scope: str
    scope_id: Optional[str]


@dataclass(frozen=True)
class VaultBinding:
    """A scope supplied by one configured vault mapping."""

    scope: str
    scope_id: Optional[str]
    vault_id: str
    root: str
    mapping: FolderMapping

    @property
    def index(self) -> bool:
        return self.mapping.index

    @property
    def inject(self) -> bool:
        return self.mapping.inject

    @property
    def writable(self) -> bool:
        return self.mapping.writable


ScopeBinding = Union[NativeBinding, VaultBinding]


@dataclass(frozen=True)
class BindingWarning:
    """A content-free configuration or native-fallback warning for vault status."""

    kind: str
    mapping: str
    detail: str


class VaultConfigUnavailableError(RuntimeError):
    """Raised when an enforcement path cannot read the vault configuration."""


_unmapped_project_writes: Counter[str] = Counter()
_non_writable_write_refusals: Counter[str] = Counter()
_secret_gate_write_refusals: Counter[str] = Counter()
_unmapped_project_writes_lock = Lock()


def resolve(
    scope: str,
    scope_id: Optional[str],
    *,
    vault_config: Optional[VaultConfig] = None,
) -> ScopeBinding:
    """Resolve ``(scope, scope_id)`` to a configured vault mapping, if any.

    Project ids are canonicalised through the persisted alias table before
    configuration matching. This keeps a renamed or moved project on the
    configured vault mapping instead of silently falling back to the native
    wiki.
    """
    canonical_scope_id = _canonical_scope_id(scope, scope_id)
    config = vault_config if vault_config is not None else _load_vault_config()
    if not config.enabled:
        return NativeBinding(scope=scope, scope_id=canonical_scope_id)

    for vault in config.vaults:
        for mapping in vault.mappings:
            canonical_mapping_scope_id = _canonical_scope_id(mapping.scope, mapping.scope_id)
            if mapping.scope == scope and _scope_ids_match(
                scope,
                canonical_mapping_scope_id,
                canonical_scope_id,
            ):
                return VaultBinding(
                    scope=scope,
                    scope_id=canonical_mapping_scope_id,
                    vault_id=vault.id,
                    root=vault.root,
                    mapping=mapping,
                )
    return NativeBinding(scope=scope, scope_id=canonical_scope_id)


def record_unmapped_project_write(
    scope_id: Optional[str], *, vault_config: Optional[VaultConfig] = None
) -> None:
    """Warn and count a native project write that misses configured mappings.

    This is deliberately observational rather than a refusal. A project that
    is not vault-mapped can be legitimate, but a silent native fallback can
    also be a project identity churn that would create two replicas. U8 owns
    the write-path call site and any durable-record decision; this counter is
    intentionally in-process only.
    """
    binding = resolve("project", scope_id, vault_config=vault_config)
    if isinstance(binding, VaultBinding):
        return

    config = vault_config if vault_config is not None else _load_vault_config()
    if not config.enabled or not _has_project_mapping(config):
        return

    counter_key = binding.scope_id or ""
    with _unmapped_project_writes_lock:
        _unmapped_project_writes[counter_key] += 1
        count = _unmapped_project_writes[counter_key]
    logger.warning(
        "unmapped_project_write scope_id=%r count=%d; writing native wiki while project vault mappings exist",
        binding.scope_id,
        count,
    )


def unmapped_project_write_count(scope_id: Optional[str] = None) -> int:
    """Return the status-visible count of native project writes that missed mappings."""
    with _unmapped_project_writes_lock:
        if scope_id is None:
            return sum(_unmapped_project_writes.values())
        return _unmapped_project_writes[scope_id]


def unmapped_project_identity_count() -> int:
    """Return the process-local number of affected project identities."""
    with _unmapped_project_writes_lock:
        return len(_unmapped_project_writes)


def record_non_writable_write_refusal(vault_id: str) -> None:
    """Count a process-local refusal caused by a non-writable vault mapping."""
    with _unmapped_project_writes_lock:
        _non_writable_write_refusals[vault_id] += 1


def non_writable_write_refusal_count(vault_id: Optional[str] = None) -> int:
    with _unmapped_project_writes_lock:
        return (
            sum(_non_writable_write_refusals.values())
            if vault_id is None
            else _non_writable_write_refusals[vault_id]
        )


def record_secret_gate_write_refusal(vault_id: str) -> None:
    """Count a process-local reject-mode secret-gate write refusal."""
    with _unmapped_project_writes_lock:
        _secret_gate_write_refusals[vault_id] += 1


def secret_gate_write_refusal_count(vault_id: Optional[str] = None) -> int:
    with _unmapped_project_writes_lock:
        return (
            sum(_secret_gate_write_refusals.values())
            if vault_id is None
            else _secret_gate_write_refusals[vault_id]
        )


def collect_binding_warnings(vault_config: VaultConfig) -> tuple[BindingWarning, ...]:
    """Collect deterministic, content-free warnings for the vault status surface."""
    warnings: list[BindingWarning] = []
    for vault in vault_config.vaults:
        for mapping in vault.mappings:
            if mapping.scope == "agent":
                warnings.append(
                    BindingWarning(
                        kind="agent_scope_recall_only",
                        mapping=mapping.folder,
                        detail=(
                            f"agent-scoped mapping {mapping.folder!r} is recall-only and "
                            "is not injected in this release"
                        ),
                    )
                )
            if mapping.scope != "project" or mapping.scope_id is None:
                continue

            canonical_project_id = get_project_id_by_alias(mapping.scope_id)
            aliases = (
                list_aliases_for_project(canonical_project_id)
                if canonical_project_id is not None
                else list_aliases_for_project(mapping.scope_id)
            )
            known_project = canonical_project_id is not None or bool(aliases)
            if not known_project:
                warnings.append(
                    BindingWarning(
                        kind="orphaned_mapping",
                        mapping=mapping.folder,
                        detail=(
                            f"project scope_id {mapping.scope_id!r} is not a known project id or alias"
                        ),
                    )
                )
                continue

            if canonical_project_id is not None and any(
                alias["alias"] == mapping.scope_id and alias["kind"] == "cwd_hash"
                for alias in aliases
            ):
                warnings.append(
                    BindingWarning(
                        kind="cwd_hash_scope_id",
                        mapping=mapping.folder,
                        detail=(
                            f"project scope_id {mapping.scope_id!r} is a cwd-hash alias "
                            "and may change after a folder rename"
                        ),
                    )
                )

    unmapped_writes = unmapped_project_write_count()
    if unmapped_writes:
        warnings.append(
            BindingWarning(
                kind="unmapped_project_write",
                mapping="",
                detail=(
                    f"{unmapped_writes} native project write(s) missed configured vault mappings"
                ),
            )
        )

    return tuple(
        sorted(
            warnings,
            key=lambda warning: (warning.kind, warning.mapping, warning.detail),
        )
    )


def _reset_unmapped_project_write_count() -> None:
    """Reset process-local warning counters for isolated tests."""
    with _unmapped_project_writes_lock:
        _unmapped_project_writes.clear()
        _non_writable_write_refusals.clear()
        _secret_gate_write_refusals.clear()


def _canonical_scope_id(scope: str, scope_id: Optional[str]) -> Optional[str]:
    if scope != "project" or scope_id is None:
        return scope_id
    return get_project_id_by_alias(scope_id) or scope_id


def _scope_ids_match(scope: str, left: Optional[str], right: Optional[str]) -> bool:
    if scope != "project" or left is None or right is None:
        return left == right
    return left.casefold() == right.casefold()


def _load_vault_config() -> VaultConfig:
    from cli_agent_orchestrator.services.settings_service import get_vault_config

    try:
        return cast(VaultConfig, get_vault_config())
    except Exception as exc:
        # ``cao config list`` is an introspection surface and deliberately
        # degrades to disabled configuration. Binding and maintenance enforce
        # security boundaries, so they must fail closed instead.
        logger.warning(
            "vault binding configuration unavailable: %s",
            str(exc),
        )
        raise VaultConfigUnavailableError(f"vault configuration unavailable: {exc}") from exc


def _has_project_mapping(config: VaultConfig) -> bool:
    return any(mapping.scope == "project" for vault in config.vaults for mapping in vault.mappings)
