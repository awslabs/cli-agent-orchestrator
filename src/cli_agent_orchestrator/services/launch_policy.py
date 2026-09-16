"""Pure, canonical effective launch-policy resolution for workflow approval.

This module deliberately separates external reads from policy computation.
The future freeze producer must load an :class:`AgentProfile`, settings, skill
catalog, MCP command resolution, and provider configuration before calling
``resolve_launch_policy``.  Admission and launch can then consume only the
stored ``material`` through ``decode_launch_policy_material``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Annotated, Any, Callable, Literal, Mapping, Optional, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    PlainSerializer,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticSerializationError

from cli_agent_orchestrator.constants import PROVIDERS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine, resolve_kiro_engine
from cli_agent_orchestrator.services.plan_identifier import digest_bytes
from cli_agent_orchestrator.services.private_plan_snapshot import (
    PrivateSnapshotIntegrityError,
    decode_material,
    structured_material_bytes,
)
from cli_agent_orchestrator.utils.tool_mapping import (
    get_allowed_tools,
    get_disallowed_tools,
)

POLICY_SCHEMA_VERSION = 1
PROVIDER_SOURCES = ("binding", "profile")
_BINDING_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

EXECUTION_AFFECTING_PROFILE_FIELDS = {
    "provider",
    "system_prompt",
    "prompt",
    "role",
    "engine",
    "skills",
    "container",
    "provider_init_timeout",
    "mcpServers",
    "tools",
    "toolAliases",
    "allowedTools",
    "toolsSettings",
    "resources",
    "hooks",
    "useLegacyMcpJson",
    "model",
    "permissionMode",
    "native_agent",
    "codexProfile",
    "codexConfig",
    "hermesProfile",
    "claudeConfig",
    "grokNativeWorkflows",
}
DERIVED_PROFILE_FIELDS = {"name"}
NON_EXECUTION_PROFILE_FIELDS = {"description", "capabilities", "tags"}

RUNTIME_SKILL_PROMPT_PROVIDERS = frozenset(
    {
        "claude_code",
        "codex",
        "kimi_cli",
        "antigravity_cli",
        "omp",
        "grok_cli",
        "mcode",
    }
)
NON_RUNTIME_SKILL_PROMPT_PROVIDERS = frozenset(
    {
        "kiro_cli",
        "copilot_cli",
        "opencode_cli",
        "hermes",
        "cursor_cli",
        "mock_cli",
    }
)
SOFT_ENFORCEMENT_PROVIDERS = frozenset(
    {
        "kimi_cli",
        "codex",
        "antigravity_cli",
        "omp",
        "mcode",
    }
)
HARD_ENFORCEMENT_PROVIDERS = frozenset(
    {
        "kiro_cli",
        "claude_code",
        "copilot_cli",
        "opencode_cli",
        "hermes",
        "cursor_cli",
        "grok_cli",
        "mock_cli",
    }
)
PROFILE_MODEL_WINS_PROVIDERS = frozenset({"antigravity_cli", "cursor_cli"})
PROFILE_EXPLICIT_MODEL_WINS_PROVIDERS = frozenset(
    {
        "kiro_cli",
        "claude_code",
        "codex",
        "kimi_cli",
        "copilot_cli",
        "opencode_cli",
        "hermes",
        "omp",
        "grok_cli",
        "mcode",
        "mock_cli",
    }
)
PROFILE_TIMEOUT_PROVIDERS = frozenset(
    {
        "claude_code",
        "kimi_cli",
        "antigravity_cli",
        "grok_cli",
        "mcode",
    }
)
PROFILE_TIMEOUT_IGNORED_PROVIDERS = frozenset(
    {
        "kiro_cli",
        "codex",
        "copilot_cli",
        "opencode_cli",
        "hermes",
        "cursor_cli",
        "omp",
        "mock_cli",
    }
)
PROFILE_LOOKUP_NAME_PROVIDERS = frozenset(
    {
        "copilot_cli",
        "opencode_cli",
    }
)
PROFILE_DOCUMENT_NAME_PROVIDERS = frozenset({"antigravity_cli"})
PROFILE_NAME_AFFECTS_EXECUTION_PROVIDERS = (
    PROFILE_LOOKUP_NAME_PROVIDERS | PROFILE_DOCUMENT_NAME_PROVIDERS
)
PROFILE_NAME_METADATA_ONLY_PROVIDERS = frozenset(
    {
        "kiro_cli",
        "claude_code",
        "codex",
        "kimi_cli",
        "hermes",
        "cursor_cli",
        "omp",
        "grok_cli",
        "mcode",
        "mock_cli",
    }
)
PROVIDERS_WITHOUT_MODEL_SELECTION = frozenset({"mock_cli"})
PROVIDERS_WITH_MODEL_SELECTION = frozenset(
    {
        "kiro_cli",
        "claude_code",
        "codex",
        "kimi_cli",
        "copilot_cli",
        "opencode_cli",
        "hermes",
        "cursor_cli",
        "antigravity_cli",
        "omp",
        "grok_cli",
        "mcode",
    }
)

RUNTIME_PROFILE_MCP_PROVIDERS = frozenset(
    {
        "claude_code",
        "codex",
        "kimi_cli",
        "cursor_cli",
        "antigravity_cli",
        "omp",
        "grok_cli",
        "mcode",
    }
)
NO_RUNTIME_PROFILE_MCP_PROVIDERS = frozenset(
    {
        "kiro_cli",
        "opencode_cli",
        "hermes",
        "mock_cli",
    }
)
PROBE_BOUND_PROFILE_MCP_PROVIDERS = frozenset({"copilot_cli"})

_ALL_PROVIDERS = frozenset(PROVIDERS)

_PROVIDER_CLASSIFICATIONS = (
    (RUNTIME_SKILL_PROMPT_PROVIDERS, NON_RUNTIME_SKILL_PROMPT_PROVIDERS),
    (SOFT_ENFORCEMENT_PROVIDERS, HARD_ENFORCEMENT_PROVIDERS),
    (PROFILE_MODEL_WINS_PROVIDERS, PROFILE_EXPLICIT_MODEL_WINS_PROVIDERS),
    (PROFILE_TIMEOUT_PROVIDERS, PROFILE_TIMEOUT_IGNORED_PROVIDERS),
    (
        PROFILE_NAME_AFFECTS_EXECUTION_PROVIDERS,
        PROFILE_NAME_METADATA_ONLY_PROVIDERS,
    ),
    (PROVIDERS_WITHOUT_MODEL_SELECTION, PROVIDERS_WITH_MODEL_SELECTION),
)

for _included_providers, _excluded_providers in _PROVIDER_CLASSIFICATIONS:
    if (
        _included_providers & _excluded_providers
        or _included_providers | _excluded_providers != _ALL_PROVIDERS
    ):
        raise RuntimeError("launch policy provider classification is incomplete")

_MCP_PROVIDER_CLASSIFICATIONS = (
    RUNTIME_PROFILE_MCP_PROVIDERS,
    NO_RUNTIME_PROFILE_MCP_PROVIDERS,
    PROBE_BOUND_PROFILE_MCP_PROVIDERS,
)
if frozenset().union(*_MCP_PROVIDER_CLASSIFICATIONS) != _ALL_PROVIDERS or sum(
    len(providers) for providers in _MCP_PROVIDER_CLASSIFICATIONS
) != len(_ALL_PROVIDERS):
    raise RuntimeError("launch policy MCP provider classification is incomplete")


class PolicyResolutionError(ValueError):
    """An execution-affecting policy component could not be frozen safely."""

    def __init__(self, component: str, declaration_key: str):
        self.component = component
        self.declaration_key = declaration_key
        super().__init__(
            f"launch policy component '{component}' is unresolved for "
            f"profile declaration key '{declaration_key}'"
        )


_MAX_POLICY_REPRESENTATION_DEPTH = 64
_JSON_SCALAR_TYPES = (str, int, float, bool)


def _representation_error(path: str) -> PolicyResolutionError:
    return PolicyResolutionError("policy_representation", path)


def is_deep_frozen(value: Any) -> bool:
    """Return whether every container uses the private immutable representation."""

    def check(item: Any, active: set[int], depth: int) -> bool:
        if depth > _MAX_POLICY_REPRESENTATION_DEPTH:
            return False
        if item is None or isinstance(item, _JSON_SCALAR_TYPES):
            return True
        if isinstance(item, MappingProxyType):
            identity = id(item)
            if identity in active:
                return False
            active.add(identity)
            try:
                return all(
                    isinstance(key, str) and check(nested, active, depth + 1)
                    for key, nested in item.items()
                )
            finally:
                active.remove(identity)
        if isinstance(item, tuple):
            identity = id(item)
            if identity in active:
                return False
            active.add(identity)
            try:
                return all(check(nested, active, depth + 1) for nested in item)
            finally:
                active.remove(identity)
        return False

    return check(value, set(), 0)


def deep_freeze(value: Any) -> Any:
    """Detach and recursively freeze JSON-shaped data without retaining live views."""

    def freeze(item: Any, active: set[int], depth: int, path: str) -> Any:
        if depth > _MAX_POLICY_REPRESENTATION_DEPTH:
            raise _representation_error(f"{path}.depth")
        if item is None or isinstance(item, _JSON_SCALAR_TYPES):
            return item
        if isinstance(item, bytes):
            raise _representation_error(f"{path}.bytes")
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise _representation_error(f"{path}.cycle")
            active.add(identity)
            try:
                detached: dict[str, Any] = {}
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise _representation_error(f"{path}.mapping.key")
                    detached[key] = freeze(
                        nested,
                        active,
                        depth + 1,
                        f"{path}.mapping.value",
                    )
                return MappingProxyType(detached)
            finally:
                active.remove(identity)
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in active:
                raise _representation_error(f"{path}.cycle")
            active.add(identity)
            try:
                return tuple(
                    freeze(nested, active, depth + 1, f"{path}.sequence.item") for nested in item
                )
            finally:
                active.remove(identity)
        raise _representation_error(f"{path}.value")

    return freeze(value, set(), 0, "root")


def to_plain_json_data(value: Any) -> Any:
    """Project the immutable representation to fresh JSON dict/list containers."""

    def project(item: Any, active: set[int], depth: int, path: str) -> Any:
        if depth > _MAX_POLICY_REPRESENTATION_DEPTH:
            raise _representation_error(f"{path}.depth")
        if item is None or isinstance(item, _JSON_SCALAR_TYPES):
            return item
        if isinstance(item, bytes):
            raise _representation_error(f"{path}.bytes")
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise _representation_error(f"{path}.cycle")
            active.add(identity)
            try:
                plain: dict[str, Any] = {}
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise _representation_error(f"{path}.mapping.key")
                    plain[key] = project(
                        nested,
                        active,
                        depth + 1,
                        f"{path}.mapping.value",
                    )
                return plain
            finally:
                active.remove(identity)
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in active:
                raise _representation_error(f"{path}.cycle")
            active.add(identity)
            try:
                return [
                    project(nested, active, depth + 1, f"{path}.sequence.item") for nested in item
                ]
            finally:
                active.remove(identity)
        raise _representation_error(f"{path}.value")

    return project(value, set(), 0, "root")


FrozenJsonMapping = Annotated[
    Mapping[str, Any],
    AfterValidator(deep_freeze),
    PlainSerializer(
        to_plain_json_data,
        return_type=dict[str, Any],
        when_used="always",
    ),
]
FrozenStringMapping = Annotated[
    Mapping[str, str],
    AfterValidator(deep_freeze),
    PlainSerializer(
        to_plain_json_data,
        return_type=dict[str, str],
        when_used="always",
    ),
]


def resolve_binding_provider(
    profile: AgentProfile,
    *,
    declared_provider: Optional[str],
) -> tuple[str, str]:
    """Resolve provider precedence without defaults, reloads, or live reads."""
    if declared_provider is not None:
        if declared_provider not in _ALL_PROVIDERS:
            raise PolicyResolutionError("provider", "binding")
        return declared_provider, "binding"
    if profile.provider is not None:
        if profile.provider not in _ALL_PROVIDERS:
            raise PolicyResolutionError("provider", "profile")
        return profile.provider, "profile"
    raise PolicyResolutionError("provider", "provider")


@dataclass(frozen=True)
class LaunchPolicyDependencies:
    """Explicit read-side inputs needed by the pure policy resolver.

    ``provider_default_model_resolved=True`` distinguishes a genuinely unset
    provider default (``None``) from an uninspected live CLI/config default.
    ``provider_configuration`` is the already-snapshotted effective external
    configuration used by named/native provider profiles; no path is retained.
    """

    provider: str
    target_key: str
    agent_profile: str
    provider_source: str
    role_tool_defaults: Mapping[str, tuple[str, ...]]
    server_provider_init_timeout: int
    resolved_mcp_servers: Optional[Mapping[str, Any]]
    selected_skill_catalog: Optional[str]
    selected_skill_contents: Optional[Mapping[str, str]]
    explicit_allowed_tools: Optional[tuple[str, ...]] = None
    explicit_model: Optional[str] = None
    explicit_engine: Optional[KiroEngine | str] = None
    provider_default_model: Optional[str] = None
    provider_default_model_resolved: bool = False
    provider_configuration: Optional[Mapping[str, Any]] = None
    provider_configuration_resolved: bool = False
    is_root: bool = False


class EffectiveToolPolicy(BaseModel):
    """Provider-native consequence of the CAO allowlist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    soft_enforcement: bool
    allowed_native_tools: tuple[str, ...]
    disallowed_native_tools: tuple[str, ...]


class EffectiveDerivedPolicy(BaseModel):
    """Every launch-time value derived from profile/config declarations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    execution_profile_name: Optional[str]
    allowed_tools: tuple[str, ...]
    engine: Optional[str]
    model: Optional[str]
    model_source: str
    provider_init_timeout: int
    permission_mode: Optional[str]
    selected_skill_catalog: Optional[str]
    selected_skill_contents: Optional[FrozenStringMapping]
    mcp_configuration: FrozenJsonMapping
    mcp_terminal_identity_env: bool
    provider_configuration: FrozenJsonMapping
    tool_policy: EffectiveToolPolicy

    @model_validator(mode="after")
    def _check_frozen_representation(self) -> EffectiveDerivedPolicy:
        values = (
            self.mcp_configuration,
            self.provider_configuration,
            self.selected_skill_contents,
        )
        if not all(value is None or is_deep_frozen(value) for value in values):
            raise _representation_error("derived.container")
        return self


class EffectiveAgentBinding(BaseModel):
    """Immutable declaration provenance for one target/profile pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_key: str
    agent_profile: str
    provider_source: Literal["binding", "profile"]
    declared_model: Optional[str]
    declared_allowed_tools: Optional[tuple[str, ...]]

    @model_validator(mode="after")
    def _check_frozen_representation(self) -> EffectiveAgentBinding:
        if not is_deep_frozen(self.declared_allowed_tools):
            raise _representation_error("binding.declared_allowed_tools")
        return self


class EffectiveLaunchPolicy(BaseModel):
    """Typed document decoded by freeze, admission, and launch consumers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    binding: EffectiveAgentBinding
    profile: FrozenJsonMapping
    derived: EffectiveDerivedPolicy

    @model_validator(mode="after")
    def _check_frozen_representation(self) -> EffectiveLaunchPolicy:
        if not is_deep_frozen(self.profile):
            raise _representation_error("profile.container")
        return self


@dataclass(frozen=True)
class LaunchPolicy:
    """Immutable canonical material plus the digest over those exact bytes."""

    material: bytes
    policy_hash: str
    policy: EffectiveLaunchPolicy = field(compare=True)


def _json_bytes(value: Any) -> bytes:
    try:
        return structured_material_bytes(value)
    except PrivateSnapshotIntegrityError:
        raise PolicyResolutionError("serialized_material", "profile") from None


def _json_copy(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        raise PolicyResolutionError("serialized_material", "profile") from None


def _contains_representation_error(error: ValidationError) -> bool:
    for detail in error.errors(include_url=False, include_input=False):
        context = detail.get("ctx")
        if isinstance(context, dict) and isinstance(context.get("error"), PolicyResolutionError):
            return True
    return False


def _resolve_allowed_tools(
    profile: AgentProfile, dependencies: LaunchPolicyDependencies
) -> tuple[str, ...]:
    if dependencies.explicit_allowed_tools is not None:
        return tuple(dependencies.explicit_allowed_tools)
    elif profile.allowedTools is not None:
        allowed = list(profile.allowedTools)
    elif profile.role:
        role_default = dependencies.role_tool_defaults.get(profile.role)
        # Match resolve_allowed_tools: an unknown role falls back unrestricted.
        allowed = list(role_default) if role_default is not None else ["*"]
    else:
        developer = dependencies.role_tool_defaults.get("developer")
        if developer is None:
            raise PolicyResolutionError("role_default", "role")
        allowed = list(developer)

    if profile.mcpServers and "*" not in allowed:
        for server_name in profile.mcpServers:
            reference = f"@{server_name}"
            if reference not in allowed:
                allowed.append(reference)
    return tuple(allowed)


def _resolve_model(
    profile: AgentProfile, dependencies: LaunchPolicyDependencies
) -> tuple[Optional[str], str]:
    if profile.native_agent and dependencies.provider == "claude_code":
        return None, "native_agent_owned"
    if dependencies.provider in PROFILE_MODEL_WINS_PROVIDERS and profile.model is not None:
        return profile.model, "profile"
    if dependencies.explicit_model is not None:
        return dependencies.explicit_model, "explicit"
    if profile.model is not None:
        return profile.model, "profile"
    if dependencies.provider in PROVIDERS_WITHOUT_MODEL_SELECTION:
        return None, "not_applicable"
    if not dependencies.provider_default_model_resolved:
        raise PolicyResolutionError("provider_default", "model")
    return dependencies.provider_default_model, "provider_default"


def _resolve_engine(profile: AgentProfile, dependencies: LaunchPolicyDependencies) -> Optional[str]:
    if dependencies.provider != "kiro_cli":
        if dependencies.explicit_engine is not None:
            raise PolicyResolutionError("engine", "provider")
        return None
    # terminal_service requires a live wrapper-capability probe for every Kiro
    # launch. Approval freeze must never perform that probe.
    resolve_kiro_engine(explicit=dependencies.explicit_engine, profile=profile.engine)
    raise PolicyResolutionError("provider_capability", "engine")


def _resolve_permission_mode(
    profile: AgentProfile,
    dependencies: LaunchPolicyDependencies,
    allowed_tools: tuple[str, ...],
) -> Optional[str]:
    if dependencies.provider == "claude_code":
        if profile.permissionMode:
            return profile.permissionMode
        if "*" in allowed_tools and dependencies.is_root:
            return "cao-root-no-skip-permissions"
        return "cao-skip-permissions"
    if dependencies.provider == "codex":
        if profile.codexProfile and "*" not in allowed_tools:
            return "codexProfile"
        return "bypassPermissionsAndSandbox"
    if dependencies.provider == "grok_cli":
        return "force"
    if dependencies.provider in {
        "kimi_cli",
        "antigravity_cli",
        "hermes",
        "copilot_cli",
        "cursor_cli",
    }:
        return "provider-headless-bypass"
    return profile.permissionMode


def _resolve_execution_profile_name(
    profile: AgentProfile,
    dependencies: LaunchPolicyDependencies,
) -> Optional[str]:
    if dependencies.provider in PROFILE_LOOKUP_NAME_PROVIDERS:
        return dependencies.agent_profile
    if dependencies.provider in PROFILE_DOCUMENT_NAME_PROVIDERS:
        return profile.name or "agent"
    return None


def _require_provider_configuration(
    profile: AgentProfile, dependencies: LaunchPolicyDependencies
) -> dict[str, Any]:
    named_configuration = dependencies.provider != "mock_cli" or (
        bool(profile.native_agent) or bool(profile.codexProfile) or bool(profile.hermesProfile)
    )
    if named_configuration and not dependencies.provider_configuration_resolved:
        declaration_key = (
            "native_agent"
            if profile.native_agent
            else (
                "codexProfile"
                if profile.codexProfile
                else "hermesProfile" if profile.hermesProfile else "provider"
            )
        )
        raise PolicyResolutionError("provider_configuration", declaration_key)
    if not dependencies.provider_configuration_resolved:
        return {}
    return cast(dict[str, Any], _json_copy(dependencies.provider_configuration or {}))


def resolve_launch_policy(
    profile: AgentProfile, dependencies: LaunchPolicyDependencies
) -> LaunchPolicy:
    """Resolve one canonical effective policy without reads, probes, or writes."""
    classified = (
        EXECUTION_AFFECTING_PROFILE_FIELDS | DERIVED_PROFILE_FIELDS | NON_EXECUTION_PROFILE_FIELDS
    )
    if set(AgentProfile.model_fields) != classified:
        raise PolicyResolutionError("profile_field_classification", "profile")
    if not isinstance(profile, AgentProfile):
        raise TypeError("profile must be an AgentProfile")
    if dependencies.provider not in _ALL_PROVIDERS:
        raise PolicyResolutionError("provider", "provider")
    if dependencies.provider_source not in PROVIDER_SOURCES:
        raise PolicyResolutionError("provider_source", "provider")
    if dependencies.provider_source == "profile" and profile.provider != dependencies.provider:
        raise PolicyResolutionError("provider_source", "provider")
    if (
        _BINDING_IDENTITY_RE.fullmatch(dependencies.target_key) is None
        or _BINDING_IDENTITY_RE.fullmatch(dependencies.agent_profile) is None
    ):
        raise PolicyResolutionError("binding_identity", "agents")
    if dependencies.server_provider_init_timeout <= 0:
        raise PolicyResolutionError("provider_init_timeout", "provider_init_timeout")

    allowed_tools = _resolve_allowed_tools(profile, dependencies)
    engine = _resolve_engine(profile, dependencies)

    runtime_profile_mcp = dependencies.provider in RUNTIME_PROFILE_MCP_PROVIDERS
    if dependencies.provider == "claude_code" and profile.native_agent:
        runtime_profile_mcp = False
    if dependencies.provider in PROBE_BOUND_PROFILE_MCP_PROVIDERS:
        # Whether Copilot accepts the runtime MCP flag is decided by a live
        # ``copilot --help`` probe today. Copilot always attempts to inject CAO's
        # own MCP server, even when the profile declares no MCP servers, and no
        # pre-approval provider work is allowed.
        raise PolicyResolutionError("provider_capability", "mcpServers")
    if runtime_profile_mcp:
        if profile.mcpServers and dependencies.resolved_mcp_servers is None:
            raise PolicyResolutionError("mcp_configuration", "mcpServers")
        mcp_configuration = _json_copy(dependencies.resolved_mcp_servers or {})
        if set(mcp_configuration) != set(profile.mcpServers or {}):
            raise PolicyResolutionError("mcp_configuration", "mcpServers")
    else:
        if dependencies.resolved_mcp_servers is not None:
            raise PolicyResolutionError("provider_capability", "mcpServers")
        mcp_configuration = {}

    runtime_skill_prompt = dependencies.provider in RUNTIME_SKILL_PROMPT_PROVIDERS
    if dependencies.provider == "claude_code" and profile.native_agent:
        runtime_skill_prompt = False
    if runtime_skill_prompt:
        if dependencies.selected_skill_catalog is None:
            raise PolicyResolutionError("skill_catalog", "skills")
        if dependencies.selected_skill_contents is None:
            raise PolicyResolutionError("skill_contents", "skills")
        skill_catalog = dependencies.selected_skill_catalog
        skill_contents = _json_copy(dependencies.selected_skill_contents)
    else:
        if (
            dependencies.selected_skill_catalog is not None
            or dependencies.selected_skill_contents is not None
        ):
            raise PolicyResolutionError("provider_capability", "skills")
        skill_catalog = None
        skill_contents = None

    model, model_source = _resolve_model(profile, dependencies)
    provider_configuration = _require_provider_configuration(profile, dependencies)
    init_timeout = (
        profile.provider_init_timeout
        if (
            dependencies.provider in PROFILE_TIMEOUT_PROVIDERS
            and profile.provider_init_timeout is not None
        )
        else dependencies.server_provider_init_timeout
    )

    tool_policy = EffectiveToolPolicy(
        soft_enforcement=dependencies.provider in SOFT_ENFORCEMENT_PROVIDERS,
        allowed_native_tools=tuple(get_allowed_tools(dependencies.provider, list(allowed_tools))),
        disallowed_native_tools=tuple(
            get_disallowed_tools(dependencies.provider, list(allowed_tools))
        ),
    )
    try:
        raw_profile = profile.model_dump(mode="json")
    except PydanticSerializationError:
        raise PolicyResolutionError("serialized_material", "profile") from None
    effective_profile = {
        key: _json_copy(raw_profile[key]) for key in sorted(EXECUTION_AFFECTING_PROFILE_FIELDS)
    }
    try:
        policy = EffectiveLaunchPolicy(
            schema_version=POLICY_SCHEMA_VERSION,
            binding=EffectiveAgentBinding(
                target_key=dependencies.target_key,
                agent_profile=dependencies.agent_profile,
                provider_source=cast(Literal["binding", "profile"], dependencies.provider_source),
                declared_model=dependencies.explicit_model,
                declared_allowed_tools=dependencies.explicit_allowed_tools,
            ),
            profile=effective_profile,
            derived=EffectiveDerivedPolicy(
                provider=dependencies.provider,
                execution_profile_name=_resolve_execution_profile_name(profile, dependencies),
                allowed_tools=allowed_tools,
                engine=engine,
                model=model,
                model_source=model_source,
                provider_init_timeout=init_timeout,
                permission_mode=_resolve_permission_mode(profile, dependencies, allowed_tools),
                selected_skill_catalog=skill_catalog,
                selected_skill_contents=skill_contents,
                mcp_configuration=mcp_configuration,
                mcp_terminal_identity_env=bool(mcp_configuration),
                provider_configuration=provider_configuration,
                tool_policy=tool_policy,
            ),
        )
        material = _json_bytes(policy.model_dump(mode="json"))
    except ValidationError as exc:
        if _contains_representation_error(exc):
            raise PolicyResolutionError("serialized_material", "profile") from None
        raise
    except PydanticSerializationError:
        raise PolicyResolutionError("serialized_material", "profile") from None
    return LaunchPolicy(
        material=material,
        policy_hash=digest_bytes(material),
        policy=policy,
    )


def freeze_launch_policy(
    profile_name: str,
    dependencies: LaunchPolicyDependencies,
    *,
    declared_provider: Optional[str] = None,
    profile_loader: Optional[Callable[[str], AgentProfile]] = None,
) -> LaunchPolicy:
    """External-read producer entrypoint; consumers use stored material only.

    B2 supplies the settings, skill-content, MCP-resolution, and provider-config
    producer beside this profile loader. Keeping that I/O outside
    ``resolve_launch_policy`` lets launch reconstruct from the private snapshot
    without touching mutable files.
    """
    if profile_loader is None:
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        profile_loader = load_agent_profile
    profile = profile_loader(profile_name)
    if dependencies.agent_profile != profile_name:
        raise PolicyResolutionError("binding_identity", "agents")
    provider, provider_source = resolve_binding_provider(
        profile,
        declared_provider=declared_provider,
    )
    resolved_dependencies = replace(
        dependencies,
        provider=provider,
        provider_source=provider_source,
    )
    return resolve_launch_policy(profile, resolved_dependencies)


def decode_launch_policy_material(
    material: bytes, *, expected_policy_hash: Optional[str] = None
) -> EffectiveLaunchPolicy:
    """Strictly decode stored policy bytes for admission or launch.

    Canonical re-encoding rejects whitespace, key-order, duplicate-normalisation
    and alternate JSON representations. The optional expected digest is checked
    over the exact input bytes before the typed document is returned.
    """
    if expected_policy_hash is not None:
        actual = digest_bytes(material)
        if actual != expected_policy_hash:
            raise ValueError("launch policy material hash mismatch")
    try:
        decoded = decode_material("policy", material)
    except PrivateSnapshotIntegrityError:
        raise ValueError("launch policy material is not canonical or valid") from None
    try:
        policy = EffectiveLaunchPolicy.model_validate(decoded)
    except ValidationError as exc:
        raise ValueError("launch policy material does not match schema") from exc
    if (
        policy.schema_version != POLICY_SCHEMA_VERSION
        or set(policy.profile) != EXECUTION_AFFECTING_PROFILE_FIELDS
    ):
        raise ValueError("launch policy material does not match schema")
    return policy
