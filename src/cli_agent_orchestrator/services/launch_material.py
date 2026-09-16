"""Pure supplied-fact assembly for the local mock launch-policy slice."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, Optional, cast

from cli_agent_orchestrator.services.launch_policy import (
    LaunchPolicy,
    LaunchPolicyDependencies,
    deep_freeze,
    freeze_launch_policy,
    resolve_binding_provider,
    to_plain_json_data,
)
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text
from cli_agent_orchestrator.utils.profile_value_resolution import ValueResolver

_ERROR_REASONS = frozenset(
    {
        "provider_configuration_uninspected",
        "provider_configuration_type",
        "reserved_namespace_collision",
        "provider_not_local_mock",
        "exec_fact_shape",
        "dependency_closure_incomplete",
        "loader_identity",
        "provider_identity_mismatch",
        "credential_capability_unavailable",
    }
)
_DEPENDENCY_FACTS = (
    ("dotenv/__init__.py", "dotenv_init"),
    ("dotenv/main.py", "dotenv_main"),
    ("dotenv/parser.py", "dotenv_parser"),
    ("dotenv/variables.py", "dotenv_variables"),
)


class LocalLaunchMaterialError(Exception):
    """Fixed local refusal for supplied launch-material inputs."""

    def __init__(self, reason: str) -> None:
        if reason not in _ERROR_REASONS:
            raise ValueError("unknown launch material error reason")
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return f"launch material unavailable:{self.reason}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(reason={self.reason!r})"


LaunchMaterialError = LocalLaunchMaterialError


@dataclass(frozen=True)
class ExecFact:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ExecObjectFacts:
    interpreter: ExecFact
    provider_program: ExecFact
    launcher: ExecFact


@dataclass(frozen=True)
class DependencyClosure:
    package_root: str
    dotenv_init: ExecFact
    dotenv_main: ExecFact
    dotenv_parser: ExecFact
    dotenv_variables: ExecFact


@dataclass(frozen=True)
class AssembledLaunchMaterial:
    policy: LaunchPolicy
    declared_provider: Optional[str]
    resolved_provider: str
    provider_source: str


def _refuse(reason: str) -> NoReturn:
    raise LocalLaunchMaterialError(reason) from None


def _plain_configuration(
    configuration: Mapping[str, Any],
    *,
    none_reason: str,
) -> dict[str, Any]:
    if configuration is None:
        _refuse(none_reason)
    if not isinstance(configuration, Mapping):
        _refuse("provider_configuration_type")
    if any(type(key) is not str for key in configuration):
        _refuse("provider_configuration_type")
    return cast(dict[str, Any], to_plain_json_data(configuration))


def provider_configuration_for_provider(
    configuration: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return a fresh immutable vendor-only provider configuration."""
    plain = _plain_configuration(
        configuration,
        none_reason="provider_configuration_type",
    )
    plain.pop("cao_d1", None)
    return cast(Mapping[str, Any], deep_freeze(plain))


def _is_absolute_clean_path(path: object) -> bool:
    return type(path) is str and path.startswith("/") and ".." not in path.split("/")


def _fact_record(fact: object, *, reason: str) -> dict[str, Any]:
    if type(fact) is not ExecFact or not _is_absolute_clean_path(fact.path):
        _refuse(reason)
    integer_values = (fact.dev, fact.ino, fact.size, fact.mtime_ns)
    if any(type(value) is not int or value < 0 for value in integer_values):
        _refuse(reason)
    return {
        "path": fact.path,
        "dev": fact.dev,
        "ino": fact.ino,
        "size": fact.size,
        "mtime_ns": fact.mtime_ns,
    }


def _exec_records(exec_objects: object) -> dict[str, dict[str, Any]]:
    if type(exec_objects) is not ExecObjectFacts:
        _refuse("exec_fact_shape")
    return {
        "interpreter": _fact_record(exec_objects.interpreter, reason="exec_fact_shape"),
        "provider_program": _fact_record(
            exec_objects.provider_program,
            reason="exec_fact_shape",
        ),
        "launcher": _fact_record(exec_objects.launcher, reason="exec_fact_shape"),
    }


def _dependency_records(
    dependency_closure: object,
) -> tuple[str, list[dict[str, Any]]]:
    reason = "dependency_closure_incomplete"
    if type(dependency_closure) is not DependencyClosure:
        _refuse(reason)
    root = dependency_closure.package_root
    if not _is_absolute_clean_path(root) or root == "/" or root.endswith("/"):
        _refuse(reason)

    records: list[dict[str, Any]] = []
    for relative_name, attribute in _DEPENDENCY_FACTS:
        fact = getattr(dependency_closure, attribute)
        record = _fact_record(fact, reason=reason)
        if record["path"] != f"{root}/{relative_name}":
            _refuse(reason)
        records.append({"name": relative_name, **record})
    return root, records


def assemble_launch_dependencies(
    *,
    profile_name: str,
    profile_text: str,
    value_resolver: ValueResolver,
    target_key: str,
    declared_provider: Optional[str] = None,
    provider_configuration: Mapping[str, Any],
    exec_objects: ExecObjectFacts,
    dependency_closure: DependencyClosure,
    role_tool_defaults: Mapping[str, tuple[str, ...]],
    server_provider_init_timeout: int,
    resolved_mcp_servers: Optional[Mapping[str, Any]] = None,
    selected_skill_catalog: Optional[str] = None,
    selected_skill_contents: Optional[Mapping[str, str]] = None,
    explicit_allowed_tools: Optional[tuple[str, ...]] = None,
    explicit_model: Optional[str] = None,
    explicit_engine: Optional[str] = None,
    provider_default_model: Optional[str] = None,
    provider_default_model_resolved: bool = False,
    credential_names: tuple[str, ...] = (),
    store_path: Optional[str] = None,
    is_root: bool = False,
) -> AssembledLaunchMaterial:
    """Assemble one mock-only launch policy from already-captured facts."""
    configuration = _plain_configuration(
        provider_configuration,
        none_reason="provider_configuration_uninspected",
    )
    if "cao_d1" in configuration:
        _refuse("reserved_namespace_collision")

    profile = parse_agent_profile_text(
        profile_text,
        profile_name,
        value_resolver=value_resolver,
    )
    resolved_provider, provider_source = resolve_binding_provider(
        profile,
        declared_provider=declared_provider,
    )
    if resolved_provider != "mock_cli":
        _refuse("provider_not_local_mock")
    if type(credential_names) is not tuple or credential_names or store_path is not None:
        _refuse("credential_capability_unavailable")

    exec_records = _exec_records(exec_objects)
    package_root, dependency_records = _dependency_records(dependency_closure)
    configuration["cao_d1"] = {
        "provider": resolved_provider,
        "credential_names": [],
        "store_path": None,
        "exec_objects": exec_records,
        "package_root": package_root,
        "dependency_closure": dependency_records,
    }
    dependencies = LaunchPolicyDependencies(
        provider=resolved_provider,
        target_key=target_key,
        agent_profile=profile_name,
        provider_source=provider_source,
        role_tool_defaults=role_tool_defaults,
        server_provider_init_timeout=server_provider_init_timeout,
        resolved_mcp_servers=resolved_mcp_servers,
        selected_skill_catalog=selected_skill_catalog,
        selected_skill_contents=selected_skill_contents,
        explicit_allowed_tools=explicit_allowed_tools,
        explicit_model=explicit_model,
        explicit_engine=explicit_engine,
        provider_default_model=provider_default_model,
        provider_default_model_resolved=provider_default_model_resolved,
        provider_configuration=configuration,
        provider_configuration_resolved=True,
        is_root=is_root,
    )

    def local_loader(name: str):
        if type(name) is not str or name != profile_name:
            _refuse("loader_identity")
        return profile

    frozen = freeze_launch_policy(
        profile_name,
        dependencies,
        declared_provider=declared_provider,
        profile_loader=local_loader,
    )
    if frozen.policy.derived.provider != resolved_provider:
        _refuse("provider_identity_mismatch")
    return AssembledLaunchMaterial(
        policy=frozen,
        declared_provider=declared_provider,
        resolved_provider=resolved_provider,
        provider_source=provider_source,
    )


__all__ = [
    "AssembledLaunchMaterial",
    "DependencyClosure",
    "ExecFact",
    "ExecObjectFacts",
    "LaunchMaterialError",
    "LocalLaunchMaterialError",
    "assemble_launch_dependencies",
    "provider_configuration_for_provider",
]
