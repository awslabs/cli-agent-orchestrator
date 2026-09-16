"""Finite deterministic memory material producer for gated workflow approval.

The producer freezes one call frame only. It does not cache, persist, discover
terminals, dispatch a curator, or infer target identity from process state.
Callers supply each declared target/profile context explicitly and later store
the returned exact ``material`` through the private plan snapshot boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional

from cli_agent_orchestrator.constants import MEMORY_SCOPE_BUDGET_CHARS
from cli_agent_orchestrator.services import memory_service as memory_service_module
from cli_agent_orchestrator.services.memory_service import (
    MemoryService,
    MemoryStrictReadError,
    _normalize_git_remote,
    _validate_project_id_override,
)
from cli_agent_orchestrator.services.plan_identifier import digest_bytes
from cli_agent_orchestrator.services.private_plan_snapshot import structured_material_bytes

MEMORY_SNAPSHOT_SCHEMA_VERSION = 1
MemoryMode = Literal["exact-snapshot", "off"]
ScopeSelection = tuple[tuple[str, Optional[str]], ...]
_PROJECT_ID_SOURCES = frozenset({"override-env", "override-settings", "git-remote", "cwd-hash"})


class MemoryFreezeError(RuntimeError):
    """A deterministic memory component could not be frozen safely."""

    def __init__(self, component: str, declaration_key: str) -> None:
        self.component = component
        self.declaration_key = declaration_key
        super().__init__(
            f"memory freeze component '{component}' is invalid for "
            f"declaration key '{declaration_key}'"
        )


@dataclass(frozen=True)
class InheritedMemoryScope:
    """A generated target's already-frozen parent memory selection."""

    scope_selection: ScopeSelection
    project_id_source: str
    project_id_value: str

    @classmethod
    def for_project(
        cls,
        *,
        project_id_source: str,
        project_id_value: str,
        session_scope_id: Optional[str],
    ) -> "InheritedMemoryScope":
        return cls(
            scope_selection=(
                ("session", session_scope_id),
                ("project", project_id_value),
                ("global", None),
            ),
            project_id_source=project_id_source,
            project_id_value=project_id_value,
        )

    @classmethod
    def from_binding(cls, binding: "FrozenMemoryBinding") -> "InheritedMemoryScope":
        if binding.memory_mode != "exact-snapshot" or not binding.project_id_value:
            raise MemoryFreezeError("inherited scope", "memory_mode")
        return cls(
            scope_selection=binding.scope_selection,
            project_id_source=binding.project_id_source,
            project_id_value=binding.project_id_value,
        )


@dataclass(frozen=True)
class MemoryFreezeRequest:
    """Explicit scope-compiler input for one ``(target, agent)`` pair."""

    target_key: str
    agent_profile: str
    context: Mapping[str, Any]
    memory_mode: MemoryMode
    inherited_scope: Optional[InheritedMemoryScope] = None


@dataclass(frozen=True)
class FrozenMemoryBinding:
    """Immutable exact bytes and provenance for one declared pair."""

    target_key: str
    agent_profile: str
    memory_mode: MemoryMode
    scope_selection: ScopeSelection
    project_id_source: str
    project_id_value: Optional[str]
    context_bytes: bytes
    block_bytes: bytes
    content_hash: str

    @property
    def block(self) -> str:
        return self.block_bytes.decode("utf-8")


@dataclass(frozen=True)
class FrozenMemorySnapshot:
    """Canonical private memory material and its exact byte digest."""

    bindings: tuple[FrozenMemoryBinding, ...]
    material: bytes
    memory_digest: str

    def binding_for(self, target_key: str, agent_profile: str) -> FrozenMemoryBinding:
        for binding in self.bindings:
            if binding.target_key == target_key and binding.agent_profile == agent_profile:
                return binding
        raise MemoryFreezeError("binding", "target_agent")

    def block_for(self, target_key: str, agent_profile: str) -> str:
        """Return the exact text the runtime owner must prepend after digest verification."""
        return self.binding_for(target_key, agent_profile).block


def _json_bytes(value: Any, component: str) -> bytes:
    try:
        return structured_material_bytes(value)
    except Exception:
        raise MemoryFreezeError(component, "structured_material") from None


def _context_cwd(context: Mapping[str, Any]) -> Optional[Path]:
    raw = context.get("cwd") or context.get("working_directory")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MemoryFreezeError("context", "cwd")
    if "\x00" in raw:
        raise MemoryFreezeError("project identity", "cwd")
    return Path(raw)


def _settings_project_override() -> Optional[str]:
    try:
        from cli_agent_orchestrator.services.settings_service import (
            get_memory_settings,
        )

        raw = get_memory_settings().get("project_id")
    except Exception:
        raise MemoryFreezeError("project identity", "override-settings") from None
    if not raw:
        return None
    if not isinstance(raw, str):
        raise MemoryFreezeError("project identity", "override-settings")
    try:
        return _validate_project_id_override(raw)
    except ValueError:
        raise MemoryFreezeError("project identity", "override-settings") from None


def _resolve_scope(context: Mapping[str, Any]) -> InheritedMemoryScope:
    raw_env = os.environ.get("CAO_PROJECT_ID")
    if raw_env:
        try:
            project_id = _validate_project_id_override(raw_env)
        except ValueError:
            raise MemoryFreezeError("project identity", "override-env") from None
        source = "override-env"
    else:
        settings_override = _settings_project_override()
        if settings_override:
            project_id = settings_override
            source = "override-settings"
        else:
            cwd = _context_cwd(context)
            if cwd is None:
                raise MemoryFreezeError("project identity", "cwd")
            remote = memory_service_module._git_remote_identity(cwd)
            if remote:
                project_id = _normalize_git_remote(remote)
                source = "git-remote"
            else:
                try:
                    project_id = hashlib.sha256(
                        os.path.realpath(str(cwd)).encode("utf-8")
                    ).hexdigest()[:12]
                except (OSError, UnicodeError):
                    raise MemoryFreezeError("project identity", "cwd") from None
                source = "cwd-hash"

    raw_session = context.get("session_name") or context.get("session")
    if raw_session is not None and not isinstance(raw_session, str):
        raise MemoryFreezeError("context", "session_name")
    session_scope_id = MemoryService._sanitize_scope_id(raw_session) if raw_session else None
    return InheritedMemoryScope.for_project(
        project_id_source=source,
        project_id_value=project_id,
        session_scope_id=session_scope_id,
    )


def _validated_inherited_scope(scope: InheritedMemoryScope) -> InheritedMemoryScope:
    expected_names = ("session", "project", "global")
    if (
        scope.project_id_source not in _PROJECT_ID_SOURCES
        or not isinstance(scope.project_id_value, str)
        or not scope.project_id_value
        or tuple(name for name, _scope_id in scope.scope_selection) != expected_names
        or scope.scope_selection[1][1] != scope.project_id_value
        or scope.scope_selection[2][1] is not None
    ):
        raise MemoryFreezeError("inherited scope", "scope_selection")
    return scope


def _freeze_binding(
    request: MemoryFreezeRequest,
    *,
    memory_service: MemoryService,
) -> FrozenMemoryBinding:
    if not isinstance(request.target_key, str) or not request.target_key:
        raise MemoryFreezeError("target key", "target_key")
    if not isinstance(request.agent_profile, str) or not request.agent_profile:
        raise MemoryFreezeError("agent profile", "agent_profile")
    if request.memory_mode not in ("exact-snapshot", "off"):
        raise MemoryFreezeError("memory mode", "memory_mode")
    context_bytes = _json_bytes(dict(request.context), "context")

    if request.memory_mode == "off":
        return FrozenMemoryBinding(
            target_key=request.target_key,
            agent_profile=request.agent_profile,
            memory_mode="off",
            scope_selection=(),
            project_id_source="off",
            project_id_value=None,
            context_bytes=context_bytes,
            block_bytes=b"",
            content_hash=digest_bytes(b""),
        )

    resolved = (
        _validated_inherited_scope(request.inherited_scope)
        if request.inherited_scope is not None
        else _resolve_scope(request.context)
    )
    try:
        block = memory_service.get_memory_context_strict(
            resolved.scope_selection,
            budget_chars=3 * MEMORY_SCOPE_BUDGET_CHARS,
        )
    except MemoryStrictReadError as exc:
        raise MemoryFreezeError(exc.component, "memory_read") from None
    except Exception:
        raise MemoryFreezeError("memory read", "memory_read") from None
    try:
        block_bytes = block.encode("utf-8")
    except UnicodeError:
        raise MemoryFreezeError("memory block", "encoding") from None
    return FrozenMemoryBinding(
        target_key=request.target_key,
        agent_profile=request.agent_profile,
        memory_mode="exact-snapshot",
        scope_selection=resolved.scope_selection,
        project_id_source=resolved.project_id_source,
        project_id_value=resolved.project_id_value,
        context_bytes=context_bytes,
        block_bytes=block_bytes,
        content_hash=digest_bytes(block_bytes),
    )


def _binding_document(binding: FrozenMemoryBinding) -> dict[str, Any]:
    return {
        "target_key": binding.target_key,
        "agent_profile": binding.agent_profile,
        "memory_mode": binding.memory_mode,
        "scope_selection": [
            {"scope": scope, "scope_id": scope_id} for scope, scope_id in binding.scope_selection
        ],
        "project_id_source": binding.project_id_source,
        "project_id_value": binding.project_id_value,
        "context": json.loads(binding.context_bytes),
        "snapshot_block": binding.block,
        "content_hash": binding.content_hash,
    }


def freeze_memory_snapshot(
    requests: Iterable[MemoryFreezeRequest],
    *,
    memory_service: MemoryService,
) -> FrozenMemorySnapshot:
    """Freeze all declared pairs with per-scope character budgets and no fallback."""
    ordered_requests = sorted(
        tuple(requests),
        key=lambda request: (request.target_key, request.agent_profile),
    )
    pairs = [(request.target_key, request.agent_profile) for request in ordered_requests]
    if len(set(pairs)) != len(pairs):
        raise MemoryFreezeError("binding", "duplicate_pair")

    bindings = tuple(
        _freeze_binding(request, memory_service=memory_service) for request in ordered_requests
    )
    material = _json_bytes(
        {
            "schema_version": MEMORY_SNAPSHOT_SCHEMA_VERSION,
            "bindings": [_binding_document(binding) for binding in bindings],
        },
        "material",
    )
    return FrozenMemorySnapshot(
        bindings=bindings,
        material=material,
        memory_digest=digest_bytes(material),
    )
