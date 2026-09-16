"""Parse finite workflow execution scope and freeze actual Git target bindings.

This module is intentionally independent of profile loading and launch.  It parses
one module-level ``SCOPE`` literal without executing workflow code, resolves only
explicit target mappings, and returns immutable private material for the plan-v2
freeze pipeline.

Raw paths occur only in :class:`TargetBinding` private material.  Exceptions and
the public summary expose safe declaration keys and opaque digests only.

This is not a claim that arbitrary Python cannot mutate a module namespace.
Downstream consumers must use the immutable declaration returned here and must
never re-read ``SCOPE`` from an executed workflow module.  The full workflow
source remains a separate plan component.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from cli_agent_orchestrator.services.plan_identifier import digest_bytes
from cli_agent_orchestrator.services.private_plan_snapshot import structured_material_bytes
from cli_agent_orchestrator.utils.git_baseline import derive_baseline

_SCOPE_VERSION = 1
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MEMORY_MODES = frozenset({"exact-snapshot", "off"})
_GIT_TIMEOUT_SECONDS = 10
_MAX_GIT_OUTPUT_BYTES = 16 * 1024
_GIT_REDIRECT_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_CONFIG",
    }
)


class ScopeDeclarationError(ValueError):
    """Non-retryable declaration refusal suitable for an HTTP 422 boundary."""

    status_code = 422
    retryable = False

    def __init__(self, code: str, *, target_key: Optional[str] = None, detail: str = "") -> None:
        self.code = code
        self.target_key = target_key if _is_safe_name(target_key) else None
        suffix = f" for SCOPE target '{self.target_key}'" if self.target_key else ""
        message = detail or "invalid finite SCOPE declaration"
        super().__init__(f"{message}{suffix} [{code}]")


class TargetResolutionError(ValueError):
    """Non-retryable explicit-target refusal suitable for an HTTP 422 boundary."""

    status_code = 422
    retryable = False

    def __init__(self, code: str, *, target_key: Optional[str] = None, detail: str = "") -> None:
        self.code = code
        self.target_key = target_key if _is_safe_name(target_key) else None
        suffix = f" for SCOPE target '{self.target_key}'" if self.target_key else ""
        message = detail or "declared target could not be frozen"
        super().__init__(f"{message}{suffix} [{code}]")


class AgentBindingError(ValueError):
    """Pure agent-binding refusal with only fixed codes and safe labels."""

    status_code = 422
    retryable = False

    def __init__(
        self,
        code: str,
        *,
        target_key: Optional[str] = None,
        agent_profile: Optional[str] = None,
    ) -> None:
        self.code = code
        self.target_key = target_key if _is_safe_name(target_key) else None
        self.agent_profile = agent_profile if _is_safe_name(agent_profile) else None
        labels = []
        if self.target_key is not None:
            labels.append(f"target '{self.target_key}'")
        if self.agent_profile is not None:
            labels.append(f"agent '{self.agent_profile}'")
        suffix = f" for {', '.join(labels)}" if labels else ""
        super().__init__(f"invalid declared agent binding{suffix} [{code}]")


@dataclass(frozen=True, order=True)
class AgentProfileName:
    """A syntactically safe profile name; launch policy resolves its contents."""

    value: str


@dataclass(frozen=True, order=True)
class ScopeTarget:
    """One finite declaration entry."""

    key: str
    allowed_agent_profiles: Tuple[AgentProfileName, ...]
    memory_mode: str
    ephemeral_parent_key: Optional[str] = None


@dataclass(frozen=True)
class ExecutionScope:
    """Canonical immutable form of a parsed ``SCOPE`` declaration."""

    version: int
    targets: Tuple[ScopeTarget, ...]
    material_bytes: bytes
    digest: str

    def target(self, key: str) -> ScopeTarget:
        for target in self.targets:
            if target.key == key:
                return target
        raise KeyError(key)


@dataclass(frozen=True, order=True)
class AgentBinding:
    """One pure, syntax-validated target/profile binding."""

    target_key: str
    agent_profile: str
    declared_provider: Optional[str]
    declared_model: Optional[str]
    declared_allowed_tools: Optional[Tuple[str, ...]]


@dataclass(frozen=True)
class GitWorkingCopyOption:
    """An explicit operator/run-request mapping to an existing working copy."""

    requested_working_directory: str


@dataclass(frozen=True)
class EphemeralChildOption:
    """A planned child mapping with no generated future path."""

    parent_key: str
    baseline_ref: str


TargetOption = Union[GitWorkingCopyOption, EphemeralChildOption]


@dataclass(frozen=True)
class ActualInstanceIdentity:
    """Physical identity shared by every actual Git working-copy consumer."""

    repo_realpath: str
    workcopy_realpath: str
    workcopy_rel: str
    filesystem_identity: Tuple[Tuple[str, int, int], ...]
    instance_key: str
    git_common_dir_realpath: str


@dataclass(frozen=True)
class TargetBinding:
    """Private immutable binding of a declaration key to an actual Git target."""

    key: str
    workcopy_role: str
    commit: str
    dirty_digest: str
    worktree_state: Tuple[Tuple[str, str], ...]
    instance_key: str
    target_digest: str
    private_material_bytes: bytes
    repo_realpath: str
    workcopy_realpath: str
    workcopy_rel: str
    requested_working_directory: Optional[str]
    requested_realpath: Optional[str]
    requested_subdirectory: Optional[str]
    parent_key: Optional[str]
    intended_baseline_ref: Optional[str]
    filesystem_identity: Tuple[Tuple[str, int, int], ...]


@dataclass(frozen=True)
class PlannedChildBinding:
    """A frozen child plan with no claim of an actual working-copy instance."""

    key: str
    parent_key: str
    parent_target_digest: str
    parent_instance_key: str
    intended_baseline_ref: str
    intended_baseline_commit: str
    provenance_key: str
    target_digest: str
    private_material_bytes: bytes


ResolvedTargetBinding = Union[TargetBinding, PlannedChildBinding]


@dataclass(frozen=True)
class ExecutionScopeFreeze:
    """Canonical declaration plus complete private target material."""

    declaration: ExecutionScope
    bindings: Tuple[ResolvedTargetBinding, ...]
    targets_material_bytes: bytes
    targets_digest: str
    private_material_bytes: bytes

    def public_summary(self) -> Dict[str, Any]:
        """Render path-free information safe for approval/status surfaces."""

        targets = []
        for binding in self.bindings:
            if isinstance(binding, TargetBinding):
                targets.append(
                    {
                        "instance_key": binding.instance_key,
                        "key": binding.key,
                        "kind": "git-working-copy",
                        "role": binding.workcopy_role,
                        "target_digest": binding.target_digest,
                    }
                )
            else:
                targets.append(
                    {
                        "key": binding.key,
                        "kind": "git-ephemeral-child",
                        "parent_key": binding.parent_key,
                        "provenance_key": binding.provenance_key,
                        "role": "ephemeral-child",
                        "target_digest": binding.target_digest,
                    }
                )
        return {
            "schema": "execution-scope-public-v2",
            "declaration_digest": self.declaration.digest,
            "targets_digest": self.targets_digest,
            "targets": targets,
        }


def _is_safe_name(value: object) -> bool:
    return isinstance(value, str) and _SAFE_NAME_RE.fullmatch(value) is not None


class _ModuleScopeBinderVisitor(ast.NodeVisitor):
    """Find module-executed binders without treating nested bodies as module writes."""

    def __init__(self) -> None:
        self.refused = False

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "SCOPE" and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.refused = True

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            effective_name = alias.asname or alias.name.split(".", 1)[0]
            if effective_name == "SCOPE":
                self.refused = True

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*" or (alias.asname or alias.name) == "SCOPE":
                self.refused = True

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name == "SCOPE":
            self.refused = True
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name == "SCOPE":
            self.refused = True
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name == "SCOPE":
            self.refused = True

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest == "SCOPE":
            self.refused = True
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in {
            "globals",
            "vars",
            "exec",
            "eval",
        }:
            self.refused = True
        self.generic_visit(node)

    def _visit_definition(
        self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef]
    ) -> None:
        if node.name == "SCOPE":
            self.refused = True
        for decorator in node.decorator_list:
            self.visit(decorator)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword)
            return
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        # The nested body does not create module-local ordinary bindings.

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_definition(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _contains_nested_scope_write(statement: ast.stmt) -> bool:
    visitor = _ModuleScopeBinderVisitor()
    visitor.visit(statement)
    return visitor.refused


def _dict_entries(
    node: ast.AST,
    *,
    dynamic_code: str = "scope_dynamic_expression",
) -> Dict[str, ast.AST]:
    if not isinstance(node, ast.Dict):
        raise ScopeDeclarationError(dynamic_code)
    result: Dict[str, ast.AST] = {}
    for key_node, value_node in zip(node.keys, node.values):
        if key_node is None or not isinstance(key_node, ast.Constant):
            raise ScopeDeclarationError(dynamic_code)
        key = key_node.value
        if not isinstance(key, str):
            raise ScopeDeclarationError(dynamic_code)
        if key in result:
            raise ScopeDeclarationError("scope_duplicate_key")
        result[key] = value_node
    return result


def _literal_scalar(node: ast.AST) -> object:
    if not isinstance(node, ast.Constant):
        raise ScopeDeclarationError("scope_dynamic_expression")
    if isinstance(node.value, (str, int, bool)) or node.value is None:
        return node.value
    raise ScopeDeclarationError("scope_dynamic_expression")


def _require_exact_keys(
    entries: Mapping[str, ast.AST],
    *,
    required: Sequence[str],
    allowed: Sequence[str],
    target_key: Optional[str] = None,
) -> None:
    required_set = frozenset(required)
    allowed_set = frozenset(allowed)
    missing = sorted(required_set.difference(entries))
    if missing:
        raise ScopeDeclarationError("scope_missing_key", target_key=target_key)
    unknown = sorted(set(entries).difference(allowed_set))
    if unknown:
        raise ScopeDeclarationError("scope_unknown_key", target_key=target_key)


def parse_scope_declaration(source: str) -> ExecutionScope:
    """Parse exactly one module-level literal ``SCOPE`` assignment.

    Parsing uses the AST only.  The workflow module, imports, calls, comprehensions,
    names, and operators are never evaluated.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        raise ScopeDeclarationError("scope_source_malformed") from None

    declarations: list[ast.AST] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SCOPE" for target in statement.targets
        ):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                raise ScopeDeclarationError("scope_dynamic_assignment")
            declarations.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "SCOPE"
        ):
            if statement.value is None:
                raise ScopeDeclarationError("scope_dynamic_assignment")
            declarations.append(statement.value)
        elif _contains_nested_scope_write(statement):
            raise ScopeDeclarationError("scope_dynamic_assignment")

    if not declarations:
        raise ScopeDeclarationError("scope_missing")
    if len(declarations) != 1:
        raise ScopeDeclarationError("scope_duplicate_assignment")

    top = _dict_entries(declarations[0])
    _require_exact_keys(
        top,
        required=("version", "targets"),
        allowed=("version", "targets"),
    )
    version = _literal_scalar(top["version"])
    if isinstance(version, bool) or not isinstance(version, int):
        raise ScopeDeclarationError("scope_version_invalid")
    if version != _SCOPE_VERSION:
        raise ScopeDeclarationError("scope_version_unsupported")

    target_nodes = _dict_entries(top["targets"])
    if not target_nodes:
        raise ScopeDeclarationError("scope_targets_empty")

    parsed_targets: list[ScopeTarget] = []
    for key, target_node in target_nodes.items():
        safe_key = key if _is_safe_name(key) else None
        if safe_key is None:
            raise ScopeDeclarationError("scope_target_key_invalid")
        fields = _dict_entries(target_node)
        _require_exact_keys(
            fields,
            required=("agents", "memory"),
            allowed=("agents", "memory", "worktree"),
            target_key=safe_key,
        )

        agents_node = fields["agents"]
        if not isinstance(agents_node, ast.List) or not agents_node.elts:
            raise ScopeDeclarationError("scope_agents_invalid", target_key=safe_key)
        profile_names: list[AgentProfileName] = []
        seen_profiles: set[str] = set()
        for profile_node in agents_node.elts:
            profile = _literal_scalar(profile_node)
            if not _is_safe_name(profile):
                raise ScopeDeclarationError("scope_agent_invalid", target_key=safe_key)
            assert isinstance(profile, str)
            if profile in seen_profiles:
                raise ScopeDeclarationError("scope_duplicate_agent", target_key=safe_key)
            seen_profiles.add(profile)
            profile_names.append(AgentProfileName(profile))

        memory_mode = _literal_scalar(fields["memory"])
        if not isinstance(memory_mode, str) or memory_mode not in _MEMORY_MODES:
            raise ScopeDeclarationError("scope_memory_mode_invalid", target_key=safe_key)

        parent_key: Optional[str] = None
        if "worktree" in fields:
            worktree = _literal_scalar(fields["worktree"])
            prefix = "ephemeral-child-of:"
            if not isinstance(worktree, str) or not worktree.startswith(prefix):
                raise ScopeDeclarationError("scope_worktree_invalid", target_key=safe_key)
            parent_key = worktree[len(prefix) :]
            if not _is_safe_name(parent_key):
                raise ScopeDeclarationError("scope_parent_invalid", target_key=safe_key)

        parsed_targets.append(
            ScopeTarget(
                key=safe_key,
                allowed_agent_profiles=tuple(profile_names),
                memory_mode=memory_mode,
                ephemeral_parent_key=parent_key,
            )
        )

    targets_by_key = {target.key: target for target in parsed_targets}
    for target in parsed_targets:
        parent = target.ephemeral_parent_key
        if parent is not None and parent not in targets_by_key:
            raise ScopeDeclarationError("scope_parent_missing", target_key=target.key)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ScopeDeclarationError("scope_parent_cycle", target_key=key)
        if key in visited:
            return
        visiting.add(key)
        parent = targets_by_key[key].ephemeral_parent_key
        if parent is not None:
            visit(parent)
        visiting.remove(key)
        visited.add(key)

    for key in sorted(targets_by_key):
        visit(key)

    canonical_targets = tuple(sorted(parsed_targets, key=lambda target: target.key))
    material = {
        "targets": {
            target.key: {
                **{
                    "agents": [profile.value for profile in target.allowed_agent_profiles],
                    "memory": target.memory_mode,
                },
                **(
                    {"worktree": f"ephemeral-child-of:{target.ephemeral_parent_key}"}
                    if target.ephemeral_parent_key is not None
                    else {}
                ),
            }
            for target in canonical_targets
        },
        "version": version,
    }
    material_bytes = structured_material_bytes(material)
    return ExecutionScope(
        version=version,
        targets=canonical_targets,
        material_bytes=material_bytes,
        digest=digest_bytes(material_bytes),
    )


def _parse_mapping_option(target: ScopeTarget, value: object) -> TargetOption:
    if isinstance(value, str):
        if target.ephemeral_parent_key is not None:
            raise TargetResolutionError("target_mapping_kind_invalid", target_key=target.key)
        return GitWorkingCopyOption(value)
    if not isinstance(value, Mapping):
        raise TargetResolutionError("target_mapping_type_invalid", target_key=target.key)
    if not all(isinstance(key, str) for key in value):
        raise TargetResolutionError("target_mapping_key_invalid", target_key=target.key)
    keys = frozenset(value)
    if target.ephemeral_parent_key is None:
        if keys != {"path"} or not isinstance(value.get("path"), str):
            raise TargetResolutionError("target_mapping_shape_invalid", target_key=target.key)
        return GitWorkingCopyOption(value["path"])
    if keys != {"parent", "baseline"}:
        raise TargetResolutionError("target_mapping_shape_invalid", target_key=target.key)
    parent = value.get("parent")
    baseline = value.get("baseline")
    if not _is_safe_name(parent) or not isinstance(baseline, str) or not baseline:
        raise TargetResolutionError("target_mapping_shape_invalid", target_key=target.key)
    assert isinstance(parent, str)
    if parent != target.ephemeral_parent_key:
        raise TargetResolutionError("target_parent_ambiguous", target_key=target.key)
    return EphemeralChildOption(parent_key=parent, baseline_ref=baseline)


def resolve_target_mappings(
    declaration: ExecutionScope, mappings: object
) -> Tuple[Tuple[str, TargetOption], ...]:
    """Validate a complete explicit operator/run-request target mapping."""

    if not isinstance(mappings, Mapping):
        raise TargetResolutionError("target_mappings_invalid")
    declared_keys = {target.key for target in declaration.targets}
    supplied_keys = set(mappings)
    for supplied_key in supplied_keys:
        if not isinstance(supplied_key, str) or supplied_key not in declared_keys:
            safe_key = supplied_key if _is_safe_name(supplied_key) else None
            raise TargetResolutionError("target_mapping_unknown", target_key=safe_key)
    for target in declaration.targets:
        if target.key not in mappings:
            raise TargetResolutionError("target_mapping_missing", target_key=target.key)
    return tuple(
        (target.key, _parse_mapping_option(target, mappings[target.key]))
        for target in declaration.targets
    )


def _agent_binding_error(
    suffix: str, *, target_key: object = None, agent_profile: object = None
) -> AgentBindingError:
    code = "agent_bindings_invalid" if suffix == "bindings_invalid" else f"agent_binding_{suffix}"
    return AgentBindingError(
        code,
        target_key=target_key if isinstance(target_key, str) else None,
        agent_profile=agent_profile if isinstance(agent_profile, str) else None,
    )


def _parse_agent_binding(target_key: str, agent_profile: str, value: object) -> AgentBinding:
    if value is None:
        fields: Mapping[str, object] = {}
    elif isinstance(value, str):
        fields = {"provider": value}
    elif isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise _agent_binding_error(
                "shape_invalid", target_key=target_key, agent_profile=agent_profile
            )
        if not set(value).issubset({"provider", "model", "allowed_tools"}):
            raise _agent_binding_error(
                "shape_invalid", target_key=target_key, agent_profile=agent_profile
            )
        fields = value
    else:
        raise _agent_binding_error(
            "type_invalid", target_key=target_key, agent_profile=agent_profile
        )

    provider: Optional[str] = None
    if "provider" in fields:
        candidate = fields["provider"]
        if not _is_safe_name(candidate):
            raise _agent_binding_error(
                "provider_invalid", target_key=target_key, agent_profile=agent_profile
            )
        assert isinstance(candidate, str)
        provider = candidate

    model: Optional[str] = None
    if "model" in fields:
        candidate = fields["model"]
        if not isinstance(candidate, str) or not candidate or not candidate.strip():
            raise _agent_binding_error(
                "model_invalid", target_key=target_key, agent_profile=agent_profile
            )
        model = candidate

    tools: Optional[Tuple[str, ...]] = None
    if "allowed_tools" in fields:
        candidate = fields["allowed_tools"]
        if isinstance(candidate, (str, bytes)) or not isinstance(candidate, (list, tuple)):
            raise _agent_binding_error(
                "tools_invalid", target_key=target_key, agent_profile=agent_profile
            )
        if not all(
            isinstance(tool, str) and bool(tool) and bool(tool.strip()) for tool in candidate
        ):
            raise _agent_binding_error(
                "tools_invalid", target_key=target_key, agent_profile=agent_profile
            )
        tools = tuple(candidate)

    return AgentBinding(target_key, agent_profile, provider, model, tools)


def resolve_agent_bindings(
    declaration: ExecutionScope, bindings: object
) -> Tuple[AgentBinding, ...]:
    """Resolve the complete pure target/profile binding matrix.

    Mapping inputs cannot represent duplicate keys; duplicate detection for raw
    JSON/CLI option streams belongs to those parsers before this pure API.
    """

    if not isinstance(bindings, Mapping):
        raise _agent_binding_error("bindings_invalid")
    declared = {
        target.key: {profile.value for profile in target.allowed_agent_profiles}
        for target in declaration.targets
    }
    supplied_targets = set(bindings)
    for key in supplied_targets:
        if not isinstance(key, str) or key not in declared:
            raise _agent_binding_error("target_unknown", target_key=key)
        if not isinstance(bindings[key], Mapping):
            raise _agent_binding_error("type_invalid", target_key=key)
    for key in sorted(declared):
        if key not in bindings:
            raise _agent_binding_error("target_missing", target_key=key)

    resolved: list[AgentBinding] = []
    for target_key in sorted(declared):
        target_bindings = bindings[target_key]
        assert isinstance(target_bindings, Mapping)
        supplied_profiles = set(target_bindings)
        for profile in supplied_profiles:
            if not isinstance(profile, str) or profile not in declared[target_key]:
                raise _agent_binding_error(
                    "agent_unknown", target_key=target_key, agent_profile=profile
                )
        for profile in sorted(declared[target_key]):
            if profile not in target_bindings:
                raise _agent_binding_error(
                    "agent_missing", target_key=target_key, agent_profile=profile
                )
            resolved.append(_parse_agent_binding(target_key, profile, target_bindings[profile]))
    return tuple(resolved)


def _run_git(
    cwd: str,
    args: Sequence[str],
    *,
    target_key: str,
    nonzero_code: str,
) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_REDIRECT_ENV and not key.startswith("GIT_CONFIG_")
    }
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise TargetResolutionError("target_git_probe_incomplete", target_key=target_key) from None
    if completed.returncode != 0:
        raise TargetResolutionError(nonzero_code, target_key=target_key)
    if len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES:
        raise TargetResolutionError("target_git_probe_truncated", target_key=target_key)
    stdout = completed.stdout
    if stdout.endswith(b"\n"):
        stdout = stdout[:-1]
        if stdout.endswith(b"\r"):
            stdout = stdout[:-1]
    if b"\n" in stdout or b"\r" in stdout:
        raise TargetResolutionError("target_git_probe_incomplete", target_key=target_key)
    try:
        return stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise TargetResolutionError("target_git_probe_incomplete", target_key=target_key) from None


def _require_git_environment_safe(target_key: str) -> None:
    if any(key == "GIT_CONFIG" or key.startswith("GIT_CONFIG_") for key in os.environ):
        raise TargetResolutionError("target_git_config_environment_unsafe", target_key=target_key)
    if any(key in _GIT_REDIRECT_ENV for key in os.environ):
        raise TargetResolutionError("target_git_environment_unsafe", target_key=target_key)


def _filesystem_identity(path: str) -> Tuple[int, int]:
    path_stat = os.stat(path)
    return path_stat.st_dev, path_stat.st_ino


@dataclass(frozen=True)
class _GitInspection:
    requested_realpath: str
    requested_subdirectory: str
    repo_realpath: str
    workcopy_realpath: str
    workcopy_rel: str
    workcopy_role: str
    commit: str
    filesystem_identity: Tuple[Tuple[str, int, int], ...]
    instance_key: str
    git_common_dir_realpath: str


def _requested_location(requested: str, workcopy_realpath: str, target_key: str) -> Tuple[str, str]:
    requested_prefix = _run_git(
        requested,
        ("rev-parse", "--show-prefix"),
        target_key=target_key,
        nonzero_code="target_git_inspection_incomplete",
    )
    requested_realpath = os.path.realpath(requested)
    prefix = requested_prefix[:-1] if requested_prefix.endswith("/") else requested_prefix
    components = prefix.split("/") if prefix else []
    if requested_prefix.startswith("/") or any(
        component in {"", ".", ".."} for component in components
    ):
        raise TargetResolutionError("target_requested_path_mismatch", target_key=target_key)
    expected_requested = os.path.join(workcopy_realpath, *components)
    try:
        if not os.path.samefile(expected_requested, requested_realpath):
            raise TargetResolutionError("target_requested_path_mismatch", target_key=target_key)
    except TargetResolutionError:
        raise
    except OSError:
        raise TargetResolutionError(
            "target_requested_path_mismatch", target_key=target_key
        ) from None
    return requested_realpath, prefix or "."


def derive_actual_instance_identity(
    workcopy_dir: str, *, target_key: str
) -> ActualInstanceIdentity:
    """Resolve the stable physical identity of an existing Git working copy."""

    if not os.path.isabs(workcopy_dir):
        raise TargetResolutionError("target_path_not_absolute", target_key=target_key)
    if not os.path.isdir(workcopy_dir):
        raise TargetResolutionError("target_path_unavailable", target_key=target_key)
    _require_git_environment_safe(target_key)
    top_level = _run_git(
        workcopy_dir,
        ("rev-parse", "--show-toplevel"),
        target_key=target_key,
        nonzero_code="target_not_git",
    )
    workcopy_realpath = os.path.realpath(top_level)
    superproject = _run_git(
        workcopy_realpath,
        ("rev-parse", "--show-superproject-working-tree"),
        target_key=target_key,
        nonzero_code="target_git_inspection_incomplete",
    )
    if superproject:
        raise TargetResolutionError("target_unsupported_submodule", target_key=target_key)
    common_dir = _run_git(
        workcopy_realpath,
        ("rev-parse", "--git-common-dir"),
        target_key=target_key,
        nonzero_code="target_git_inspection_incomplete",
    )
    _requested_location(workcopy_dir, workcopy_realpath, target_key)
    common_dir_path = (
        common_dir if os.path.isabs(common_dir) else os.path.join(workcopy_realpath, common_dir)
    )
    git_common_dir_realpath = os.path.realpath(common_dir_path)
    common_dir_components = Path(git_common_dir_realpath).parts
    if any(
        common_dir_components[index : index + 2] == (".git", "modules")
        for index in range(len(common_dir_components) - 1)
    ):
        raise TargetResolutionError("target_unsupported_submodule", target_key=target_key)
    repo_realpath = os.path.realpath(os.path.dirname(git_common_dir_realpath))
    try:
        repo_device, repo_inode = _filesystem_identity(repo_realpath)
        workcopy_device, workcopy_inode = _filesystem_identity(workcopy_realpath)
    except OSError:
        raise TargetResolutionError(
            "target_filesystem_identity_unavailable", target_key=target_key
        ) from None
    filesystem_identity = (
        ("repo", repo_device, repo_inode),
        ("workcopy", workcopy_device, workcopy_inode),
    )
    instance_material = {
        "filesystem": {
            label: {"device": device, "inode": inode}
            for label, device, inode in filesystem_identity
        },
        "schema": "git-working-copy-instance-v1",
    }
    _require_git_environment_safe(target_key)
    return ActualInstanceIdentity(
        repo_realpath=repo_realpath,
        workcopy_realpath=workcopy_realpath,
        workcopy_rel=os.path.relpath(workcopy_realpath, repo_realpath),
        filesystem_identity=filesystem_identity,
        instance_key=digest_bytes(structured_material_bytes(instance_material)),
        git_common_dir_realpath=git_common_dir_realpath,
    )


def _inspect_git_target(requested: str, target_key: str) -> _GitInspection:
    actual_identity = derive_actual_instance_identity(requested, target_key=target_key)
    head = _run_git(
        actual_identity.workcopy_realpath,
        ("rev-parse", "--verify", "HEAD"),
        target_key=target_key,
        nonzero_code="target_git_inspection_incomplete",
    )
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise TargetResolutionError("target_git_inspection_incomplete", target_key=target_key)
    requested_realpath, requested_subdirectory = _requested_location(
        requested, actual_identity.workcopy_realpath, target_key
    )
    _require_git_environment_safe(target_key)

    return _GitInspection(
        requested_realpath=requested_realpath,
        requested_subdirectory=requested_subdirectory,
        repo_realpath=actual_identity.repo_realpath,
        workcopy_realpath=actual_identity.workcopy_realpath,
        workcopy_rel=actual_identity.workcopy_rel,
        workcopy_role=(
            "primary"
            if actual_identity.workcopy_realpath == actual_identity.repo_realpath
            else "declared-linked"
        ),
        commit=head,
        filesystem_identity=actual_identity.filesystem_identity,
        instance_key=actual_identity.instance_key,
        git_common_dir_realpath=actual_identity.git_common_dir_realpath,
    )


def _worktree_state(baseline: Mapping[str, object], target_key: str) -> Tuple[str, str]:
    if set(baseline) != {"available", "commit", "worktree_state"}:
        raise TargetResolutionError("target_baseline_incomplete", target_key=target_key)
    if baseline.get("available") is not True or not isinstance(baseline.get("commit"), str):
        raise TargetResolutionError("target_baseline_incomplete", target_key=target_key)
    state = baseline.get("worktree_state")
    if not isinstance(state, Mapping):
        raise TargetResolutionError("target_baseline_incomplete", target_key=target_key)
    status = state.get("status")
    if status == "clean" and set(state) == {"status"}:
        return "clean", "clean"
    digest = state.get("digest")
    if (
        status == "dirty"
        and set(state) == {"status", "digest"}
        and isinstance(digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    ):
        return "dirty", digest
    raise TargetResolutionError("target_baseline_incomplete", target_key=target_key)


def _binding_from_private_material(
    *,
    key: str,
    workcopy_role: str,
    commit: str,
    dirty_digest: str,
    worktree_state: Tuple[Tuple[str, str], ...],
    instance_key: str,
    private: Dict[str, object],
    repo_realpath: str,
    workcopy_realpath: str,
    workcopy_rel: str,
    requested_working_directory: Optional[str],
    requested_realpath: Optional[str],
    requested_subdirectory: Optional[str],
    parent_key: Optional[str],
    intended_baseline_ref: Optional[str],
    filesystem_identity: Tuple[Tuple[str, int, int], ...],
) -> TargetBinding:
    private_material_bytes = structured_material_bytes(private)
    return TargetBinding(
        key=key,
        workcopy_role=workcopy_role,
        commit=commit,
        dirty_digest=dirty_digest,
        worktree_state=worktree_state,
        instance_key=instance_key,
        target_digest=digest_bytes(private_material_bytes),
        private_material_bytes=private_material_bytes,
        repo_realpath=repo_realpath,
        workcopy_realpath=workcopy_realpath,
        workcopy_rel=workcopy_rel,
        requested_working_directory=requested_working_directory,
        requested_realpath=requested_realpath,
        requested_subdirectory=requested_subdirectory,
        parent_key=parent_key,
        intended_baseline_ref=intended_baseline_ref,
        filesystem_identity=filesystem_identity,
    )


def _freeze_git_target(target: ScopeTarget, option: GitWorkingCopyOption) -> TargetBinding:
    requested = option.requested_working_directory
    if not os.path.isabs(requested):
        raise TargetResolutionError("target_path_not_absolute", target_key=target.key)
    if not os.path.isdir(requested):
        raise TargetResolutionError("target_path_unavailable", target_key=target.key)

    _require_git_environment_safe(target.key)
    initial_inspection = _inspect_git_target(requested, target.key)
    _require_git_environment_safe(target.key)
    initial_baseline = derive_baseline(initial_inspection.workcopy_realpath)
    _require_git_environment_safe(target.key)
    final_baseline = derive_baseline(initial_inspection.workcopy_realpath)
    _require_git_environment_safe(target.key)
    final_inspection = _inspect_git_target(requested, target.key)
    _require_git_environment_safe(target.key)
    if initial_inspection != final_inspection:
        raise TargetResolutionError("target_binding_unstable", target_key=target.key)
    if initial_baseline != final_baseline:
        raise TargetResolutionError("target_baseline_unstable", target_key=target.key)

    state_status, dirty_digest = _worktree_state(final_baseline, target.key)
    if final_baseline["commit"] != final_inspection.commit:
        raise TargetResolutionError("target_baseline_unstable", target_key=target.key)

    filesystem_identity = final_inspection.filesystem_identity
    instance_key = final_inspection.instance_key
    state = (("status", state_status),)
    private: Dict[str, object] = {
        "commit": final_inspection.commit,
        "dirty_digest": dirty_digest,
        "filesystem_identity": {
            label: {"device": device, "inode": inode}
            for label, device, inode in filesystem_identity
        },
        "instance_key": instance_key,
        "key": target.key,
        "kind": "git-working-copy",
        "repo_realpath": final_inspection.repo_realpath,
        "requested_realpath": final_inspection.requested_realpath,
        "requested_subdirectory": final_inspection.requested_subdirectory,
        "requested_working_directory": requested,
        "schema": "execution-target-private-v1",
        "workcopy_realpath": final_inspection.workcopy_realpath,
        "workcopy_rel": final_inspection.workcopy_rel,
        "workcopy_role": final_inspection.workcopy_role,
        "worktree_state": dict(state),
    }
    return _binding_from_private_material(
        key=target.key,
        workcopy_role=final_inspection.workcopy_role,
        commit=final_inspection.commit,
        dirty_digest=dirty_digest,
        worktree_state=state,
        instance_key=instance_key,
        private=private,
        repo_realpath=final_inspection.repo_realpath,
        workcopy_realpath=final_inspection.workcopy_realpath,
        workcopy_rel=final_inspection.workcopy_rel,
        requested_working_directory=requested,
        requested_realpath=final_inspection.requested_realpath,
        requested_subdirectory=final_inspection.requested_subdirectory,
        parent_key=None,
        intended_baseline_ref=None,
        filesystem_identity=filesystem_identity,
    )


def _freeze_ephemeral_target(
    target: ScopeTarget,
    option: EphemeralChildOption,
    parent: ResolvedTargetBinding,
) -> PlannedChildBinding:
    if not isinstance(parent, TargetBinding):
        raise TargetResolutionError("target_parent_not_actual", target_key=target.key)
    _require_git_environment_safe(target.key)
    resolved_commit = _run_git(
        parent.workcopy_realpath,
        (
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{option.baseline_ref}^{{commit}}",
        ),
        target_key=target.key,
        nonzero_code="target_baseline_ref_invalid",
    )
    _require_git_environment_safe(target.key)
    if not re.fullmatch(r"[0-9a-f]{40,64}", resolved_commit):
        raise TargetResolutionError("target_baseline_ref_invalid", target_key=target.key)
    provenance_material = {
        "child_declaration_key": target.key,
        "intended_baseline_commit": resolved_commit,
        "parent_instance_key": parent.instance_key,
        "schema": "git-ephemeral-child-provenance-v1",
    }
    provenance_key = digest_bytes(structured_material_bytes(provenance_material))
    private: Dict[str, object] = {
        "intended_baseline_commit": resolved_commit,
        "intended_baseline_ref": option.baseline_ref,
        "key": target.key,
        "kind": "git-ephemeral-child",
        "parent_instance_key": parent.instance_key,
        "parent_key": option.parent_key,
        "parent_target_digest": parent.target_digest,
        "provenance_key": provenance_key,
        "schema": "execution-target-ephemeral-child-private-v1",
    }
    private_material_bytes = structured_material_bytes(private)
    return PlannedChildBinding(
        key=target.key,
        parent_key=option.parent_key,
        parent_target_digest=parent.target_digest,
        parent_instance_key=parent.instance_key,
        intended_baseline_ref=option.baseline_ref,
        intended_baseline_commit=resolved_commit,
        provenance_key=provenance_key,
        target_digest=digest_bytes(private_material_bytes),
        private_material_bytes=private_material_bytes,
    )


def freeze_execution_scope(declaration: ExecutionScope, mappings: object) -> ExecutionScopeFreeze:
    """Freeze complete private material for every explicitly mapped target."""

    options = dict(resolve_target_mappings(declaration, mappings))
    bindings_by_key: Dict[str, ResolvedTargetBinding] = {}

    for target in declaration.targets:
        option = options[target.key]
        if target.ephemeral_parent_key is None:
            if not isinstance(option, GitWorkingCopyOption):
                raise TargetResolutionError("target_mapping_kind_invalid", target_key=target.key)
            bindings_by_key[target.key] = _freeze_git_target(target, option)
        elif isinstance(option, GitWorkingCopyOption):
            raise TargetResolutionError("target_mapping_kind_invalid", target_key=target.key)

    unresolved = [target for target in declaration.targets if target.key not in bindings_by_key]
    while unresolved:
        progress = False
        for target in tuple(unresolved):
            option = options[target.key]
            if not isinstance(option, EphemeralChildOption):
                raise TargetResolutionError("target_mapping_kind_invalid", target_key=target.key)
            parent = bindings_by_key.get(option.parent_key)
            if parent is None:
                continue
            bindings_by_key[target.key] = _freeze_ephemeral_target(target, option, parent)
            unresolved.remove(target)
            progress = True
        if not progress:
            raise TargetResolutionError("target_parent_unresolvable", target_key=unresolved[0].key)

    bindings = tuple(bindings_by_key[target.key] for target in declaration.targets)
    targets_material = {
        "schema": "execution-targets-private-v1",
        "targets": [
            json.loads(binding.private_material_bytes.decode("utf-8")) for binding in bindings
        ],
    }
    targets_material_bytes = structured_material_bytes(targets_material)
    private_material_bytes = structured_material_bytes(
        {
            "declaration": json.loads(declaration.material_bytes.decode("utf-8")),
            "schema": "execution-scope-private-v1",
            "targets": targets_material["targets"],
        }
    )
    return ExecutionScopeFreeze(
        declaration=declaration,
        bindings=bindings,
        targets_material_bytes=targets_material_bytes,
        targets_digest=digest_bytes(targets_material_bytes),
        private_material_bytes=private_material_bytes,
    )


__all__ = [
    "ActualInstanceIdentity",
    "AgentBinding",
    "AgentBindingError",
    "AgentProfileName",
    "EphemeralChildOption",
    "ExecutionScope",
    "ExecutionScopeFreeze",
    "GitWorkingCopyOption",
    "PlannedChildBinding",
    "ResolvedTargetBinding",
    "ScopeDeclarationError",
    "ScopeTarget",
    "TargetBinding",
    "TargetOption",
    "TargetResolutionError",
    "digest_bytes",
    "derive_actual_instance_identity",
    "freeze_execution_scope",
    "parse_scope_declaration",
    "resolve_agent_bindings",
    "resolve_target_mappings",
]
