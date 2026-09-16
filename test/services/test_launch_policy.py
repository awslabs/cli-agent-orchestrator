"""Finite Phase B1 coverage for the pure effective launch-policy freeze."""

from __future__ import annotations

import base64
import json
import warnings
import zlib
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.constants import PROVIDERS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services import plan_identifier, private_plan_snapshot, workflow_journal
from cli_agent_orchestrator.services.launch_policy import (
    DERIVED_PROFILE_FIELDS,
    EXECUTION_AFFECTING_PROFILE_FIELDS,
    HARD_ENFORCEMENT_PROVIDERS,
    NO_RUNTIME_PROFILE_MCP_PROVIDERS,
    NON_EXECUTION_PROFILE_FIELDS,
    NON_RUNTIME_SKILL_PROMPT_PROVIDERS,
    PROBE_BOUND_PROFILE_MCP_PROVIDERS,
    PROFILE_DOCUMENT_NAME_PROVIDERS,
    PROFILE_EXPLICIT_MODEL_WINS_PROVIDERS,
    PROFILE_LOOKUP_NAME_PROVIDERS,
    PROFILE_MODEL_WINS_PROVIDERS,
    PROFILE_NAME_AFFECTS_EXECUTION_PROVIDERS,
    PROFILE_NAME_METADATA_ONLY_PROVIDERS,
    PROFILE_TIMEOUT_IGNORED_PROVIDERS,
    PROFILE_TIMEOUT_PROVIDERS,
    PROVIDERS_WITH_MODEL_SELECTION,
    PROVIDERS_WITHOUT_MODEL_SELECTION,
    RUNTIME_PROFILE_MCP_PROVIDERS,
    RUNTIME_SKILL_PROMPT_PROVIDERS,
    SOFT_ENFORCEMENT_PROVIDERS,
    EffectiveLaunchPolicy,
    LaunchPolicyDependencies,
    PolicyResolutionError,
    decode_launch_policy_material,
    deep_freeze,
    freeze_launch_policy,
    is_deep_frozen,
    resolve_binding_provider,
    resolve_launch_policy,
    to_plain_json_data,
)
from cli_agent_orchestrator.services.scope_admission import (
    AgentAdmissionError,
    admit_agent_launch,
)
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text


def _profile(**changes: object) -> AgentProfile:
    values: dict[str, object] = {
        "name": "phase-b1",
        "description": "metadata only",
        "provider": "claude_code",
        "system_prompt": "system",
        "prompt": "prompt",
        "role": "reviewer",
        "skills": ["review-*"],
        "container": {"path_maps": [{"host": "/source", "guest": "/workspace"}]},
        "provider_init_timeout": 91,
        "mcpServers": {
            "cao": {
                "command": "cao-mcp-server",
                "args": ["--stdio"],
                "env": {"MODE": "review"},
            }
        },
        "tools": ["*"],
        "toolAliases": {"shell": "execute_bash"},
        "allowedTools": ["fs_read"],
        "toolsSettings": {"fs_read": {"limit": 4}},
        "resources": ["file:///context.md"],
        "hooks": {"before": [{"command": "check"}]},
        "useLegacyMcpJson": False,
        "model": "model-a",
        "permissionMode": "plan",
        "native_agent": None,
        "codexProfile": None,
        "codexConfig": {"model_reasoning_effort": "high"},
        "hermesProfile": None,
        "claudeConfig": {"effort": "high"},
        "grokNativeWorkflows": False,
    }
    values.update(changes)
    return AgentProfile.model_validate(values)


def _dependencies(**changes: object) -> LaunchPolicyDependencies:
    values: dict[str, object] = {
        "provider": "claude_code",
        "target_key": "main",
        "agent_profile": "phase-b1",
        "provider_source": "profile",
        "role_tool_defaults": {
            "developer": ("execute_bash", "fs_read", "fs_write", "fs_list", "web_fetch"),
            "reviewer": ("fs_read", "fs_list"),
        },
        "server_provider_init_timeout": 60,
        "resolved_mcp_servers": {
            "cao": {
                "command": "/resolved/bin/cao-mcp-server",
                "args": ["--stdio"],
                "env": {"MODE": "review"},
            }
        },
        "selected_skill_catalog": "## Available Skills\n\n- review-one: content",
        "selected_skill_contents": {"review-one": "---\nname: review-one\n---\nFull instructions."},
        "provider_default_model_resolved": False,
        "provider_configuration_resolved": True,
        "provider_configuration": {},
        "is_root": False,
    }
    values.update(changes)
    return LaunchPolicyDependencies(**cast(Any, values))


def test_every_agent_profile_field_is_classified() -> None:
    assert set(AgentProfile.model_fields) == (
        EXECUTION_AFFECTING_PROFILE_FIELDS | DERIVED_PROFILE_FIELDS | NON_EXECUTION_PROFILE_FIELDS
    )
    assert DERIVED_PROFILE_FIELDS == {"name"}
    assert NON_EXECUTION_PROFILE_FIELDS == {
        "description",
        "capabilities",
        "tags",
    }


def test_binding_provider_explicit_declaration_wins_over_profile() -> None:
    assert resolve_binding_provider(
        _profile(provider="claude_code"),
        declared_provider="codex",
    ) == ("codex", "binding")


def test_binding_provider_inherits_profile_declaration() -> None:
    assert resolve_binding_provider(
        _profile(provider="claude_code"),
        declared_provider=None,
    ) == ("claude_code", "profile")


@pytest.mark.parametrize(
    ("profile_provider", "declared_provider", "declaration_key"),
    [
        ("claude_code", "claude-code", "binding"),
        ("unknown-provider", None, "profile"),
        (None, None, "provider"),
    ],
)
def test_binding_provider_refuses_invalid_or_missing_declarations(
    profile_provider: str | None,
    declared_provider: str | None,
    declaration_key: str,
) -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        resolve_binding_provider(
            _profile(provider=profile_provider),
            declared_provider=declared_provider,
        )

    assert caught.value.component == "provider"
    assert caught.value.declaration_key == declaration_key
    assert "claude-code" not in str(caught.value)
    assert "unknown-provider" not in str(caught.value)


def test_every_provider_is_explicitly_classified_for_all_policy_dimensions() -> None:
    all_providers = frozenset(PROVIDERS)
    dimensions = [
        (RUNTIME_SKILL_PROMPT_PROVIDERS, NON_RUNTIME_SKILL_PROMPT_PROVIDERS),
        (SOFT_ENFORCEMENT_PROVIDERS, HARD_ENFORCEMENT_PROVIDERS),
        (PROFILE_MODEL_WINS_PROVIDERS, PROFILE_EXPLICIT_MODEL_WINS_PROVIDERS),
        (PROFILE_TIMEOUT_PROVIDERS, PROFILE_TIMEOUT_IGNORED_PROVIDERS),
        (
            PROFILE_NAME_AFFECTS_EXECUTION_PROVIDERS,
            PROFILE_NAME_METADATA_ONLY_PROVIDERS,
        ),
        (PROVIDERS_WITHOUT_MODEL_SELECTION, PROVIDERS_WITH_MODEL_SELECTION),
    ]

    for included, excluded in dimensions:
        assert included.isdisjoint(excluded)
        assert included | excluded == all_providers


def test_runtime_mcp_capabilities_partition_every_provider() -> None:
    partitions = [
        RUNTIME_PROFILE_MCP_PROVIDERS,
        NO_RUNTIME_PROFILE_MCP_PROVIDERS,
        PROBE_BOUND_PROFILE_MCP_PROVIDERS,
    ]

    assert set().union(*partitions) == set(PROVIDERS)
    for index, partition in enumerate(partitions):
        for other in partitions[index + 1 :]:
            assert partition.isdisjoint(other)


def test_execution_profile_name_sources_partition_name_affecting_providers() -> None:
    assert PROFILE_LOOKUP_NAME_PROVIDERS.isdisjoint(PROFILE_DOCUMENT_NAME_PROVIDERS)
    assert (
        PROFILE_LOOKUP_NAME_PROVIDERS | PROFILE_DOCUMENT_NAME_PROVIDERS
        == PROFILE_NAME_AFFECTS_EXECUTION_PROVIDERS
    )


def test_runtime_skill_and_soft_enforcement_classifications_match_launch() -> None:
    from cli_agent_orchestrator.services import terminal_service

    assert RUNTIME_SKILL_PROMPT_PROVIDERS == frozenset(
        terminal_service.RUNTIME_SKILL_PROMPT_PROVIDERS
    )
    assert SOFT_ENFORCEMENT_PROVIDERS == frozenset(terminal_service.SOFT_ENFORCEMENT_PROVIDERS)


def test_material_is_compact_sorted_strict_json_and_hashes_exact_bytes() -> None:
    first = resolve_launch_policy(_profile(), _dependencies())
    second = resolve_launch_policy(_profile(), _dependencies())

    assert first == second
    assert first.material == private_plan_snapshot.structured_material_bytes(
        json.loads(first.material)
    )
    assert b'": ' not in first.material
    assert (
        decode_launch_policy_material(first.material, expected_policy_hash=first.policy_hash)
        == first.policy
    )


def test_binding_provenance_and_raw_declarations_are_frozen_and_decoded() -> None:
    frozen = resolve_launch_policy(
        _profile(provider="claude_code"),
        _dependencies(
            provider="codex",
            provider_source="binding",
            explicit_model="declared-model",
            explicit_allowed_tools=(),
        ),
    )
    decoded = decode_launch_policy_material(frozen.material)

    assert decoded.binding.target_key == "main"
    assert decoded.binding.agent_profile == "phase-b1"
    assert decoded.binding.provider_source == "binding"
    assert decoded.binding.declared_model == "declared-model"
    assert decoded.binding.declared_allowed_tools == ()
    assert decoded.derived.provider == "codex"
    assert decoded.profile["provider"] == "claude_code"


def test_same_provider_with_different_source_has_different_policy_material() -> None:
    inherited = resolve_launch_policy(
        _profile(provider="claude_code"),
        _dependencies(provider="claude_code", provider_source="profile"),
    )
    explicit = resolve_launch_policy(
        _profile(provider="claude_code"),
        _dependencies(provider="claude_code", provider_source="binding"),
    )

    assert inherited.policy.derived.provider == explicit.policy.derived.provider
    assert inherited.material != explicit.material
    assert inherited.policy_hash != explicit.policy_hash


def test_provider_model_tools_and_pair_changes_alter_individual_policy_hash() -> None:
    baseline_profile = _profile()
    baseline_dependencies = _dependencies(
        provider_source="binding",
        explicit_model="declared-model",
        explicit_allowed_tools=("fs_read",),
    )
    baseline = resolve_launch_policy(baseline_profile, baseline_dependencies)
    changed = [
        resolve_launch_policy(
            baseline_profile,
            replace(baseline_dependencies, provider="codex"),
        ),
        resolve_launch_policy(
            baseline_profile,
            replace(baseline_dependencies, explicit_model="other-model"),
        ),
        resolve_launch_policy(
            baseline_profile,
            replace(baseline_dependencies, explicit_allowed_tools=()),
        ),
        resolve_launch_policy(
            baseline_profile,
            replace(baseline_dependencies, target_key="secondary"),
        ),
        resolve_launch_policy(
            baseline_profile,
            replace(baseline_dependencies, agent_profile="other_profile"),
        ),
    ]

    assert all(policy.policy_hash != baseline.policy_hash for policy in changed)
    assert len({policy.policy_hash for policy in changed}) == len(changed)


def test_dishonest_profile_provider_source_is_refused() -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(
            _profile(provider="claude_code"),
            _dependencies(provider="codex", provider_source="profile"),
        )

    assert caught.value.component == "provider_source"
    assert caught.value.declaration_key == "provider"


@pytest.mark.parametrize(
    ("dependency_change", "component", "declaration_key"),
    [
        ({"provider_source": "default"}, "provider_source", "provider"),
        ({"target_key": ""}, "binding_identity", "agents"),
        ({"target_key": "bad/key"}, "binding_identity", "agents"),
        ({"target_key": "a" * 65}, "binding_identity", "agents"),
        ({"agent_profile": ""}, "binding_identity", "agents"),
        ({"agent_profile": "bad.profile"}, "binding_identity", "agents"),
        ({"agent_profile": "a" * 65}, "binding_identity", "agents"),
    ],
)
def test_binding_source_and_pair_identity_fail_closed(
    dependency_change: dict[str, object],
    component: str,
    declaration_key: str,
) -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(_profile(), _dependencies(**dependency_change))

    assert caught.value.component == component
    assert caught.value.declaration_key == declaration_key


def test_non_ascii_policy_crosses_real_private_snapshot_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "workflow.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", database, raising=True)
    profile = _profile(
        system_prompt="系统提示 🚀",
        mcpServers={
            "cao": {
                "command": "cao-mcp-server",
                "args": ["--标签"],
                "env": {"问候": "你好"},
            }
        },
    )
    dependencies = _dependencies(
        resolved_mcp_servers={
            "cao": {
                "command": "/resolved/bin/cao-mcp-server",
                "args": ["--标签"],
                "env": {"问候": "你好"},
            }
        },
        selected_skill_catalog="## 可用技能\n\n- 审查",
        selected_skill_contents={"审查": "完整说明 🌏"},
    )
    frozen = resolve_launch_policy(profile, dependencies)
    materials = {
        "artifact_hash": b"SCOPE = {'main': {}}\n",
        "declaration": private_plan_snapshot.structured_material_bytes(
            {"version": 1, "targets": {"main": {}}}
        ),
        "targets": private_plan_snapshot.structured_material_bytes({"main": {"commit": "a" * 40}}),
        "limits": private_plan_snapshot.structured_material_bytes({"max_steps": 10}),
        "retry_policy": private_plan_snapshot.structured_material_bytes({"retries": 0}),
        "policy": frozen.material,
        "memory": private_plan_snapshot.structured_material_bytes({"mode": "off"}),
    }
    components = plan_identifier.PlanV2Components(
        tier="script",
        artifact_hash=plan_identifier.digest_bytes(materials["artifact_hash"]),
        declaration=plan_identifier.digest_bytes(materials["declaration"]),
        targets=plan_identifier.digest_bytes(materials["targets"]),
        inputs=(),
        limits=plan_identifier.digest_bytes(materials["limits"]),
        retry_policy=plan_identifier.digest_bytes(materials["retry_policy"]),
        policy=plan_identifier.digest_bytes(frozen.material),
        memory=plan_identifier.digest_bytes(materials["memory"]),
    )
    workflow_journal.insert_run(
        run_id="non-ascii-policy",
        workflow_name="wf",
        spec_snapshot="source",
        inputs_json="{}",
        state="running",
        started_at="2026-09-16T00:00:00Z",
        tier="script",
    )

    private_plan_snapshot.freeze_and_attach("non-ascii-policy", components, materials)
    verified = private_plan_snapshot.private_snapshot_for_run("non-ascii-policy", components)
    generic = verified.decode_material("policy")
    typed = decode_launch_policy_material(
        verified.material("policy"), expected_policy_hash=frozen.policy_hash
    )

    assert generic["binding"] == {
        "agent_profile": "phase-b1",
        "declared_allowed_tools": None,
        "declared_model": None,
        "provider_source": "profile",
        "target_key": "main",
    }
    assert generic["profile"]["system_prompt"] == "系统提示 🚀"
    assert generic["derived"]["selected_skill_contents"]["审查"] == "完整说明 🌏"
    assert generic["derived"]["mcp_configuration"]["cao"]["env"]["问候"] == "你好"
    assert typed.binding.agent_profile == "phase-b1"
    assert typed == frozen.policy
    assert components.policy == frozen.policy_hash


@pytest.mark.parametrize(
    ("profile_change", "dependency_change"),
    [
        ({"system_prompt": "different"}, {}),
        ({"prompt": "different"}, {}),
        ({"hooks": {"after": [{"command": "different"}]}}, {}),
        ({"allowedTools": ["fs_write"]}, {}),
        ({"model": "model-b"}, {}),
        ({"permissionMode": "acceptEdits"}, {}),
        (
            {"mcpServers": {"cao": {"command": "other", "args": ["x"], "env": {"K": "V"}}}},
            {
                "resolved_mcp_servers": {
                    "cao": {"command": "/resolved/other", "args": ["x"], "env": {"K": "V"}}
                }
            },
        ),
        ({}, {"selected_skill_catalog": "## Available Skills\n\n- review-one: changed"}),
        ({}, {"selected_skill_contents": {"review-one": "Changed full instructions."}}),
        (
            {},
            {
                "resolved_mcp_servers": {
                    "cao": {
                        "command": "/resolved/bin/different",
                        "args": ["--stdio"],
                        "env": {"MODE": "review"},
                    }
                }
            },
        ),
        (
            {},
            {
                "resolved_mcp_servers": {
                    "cao": {
                        "command": "/resolved/bin/cao-mcp-server",
                        "args": ["--different"],
                        "env": {"MODE": "review"},
                    }
                }
            },
        ),
        (
            {},
            {
                "resolved_mcp_servers": {
                    "cao": {
                        "command": "/resolved/bin/cao-mcp-server",
                        "args": ["--stdio"],
                        "env": {"MODE": "write"},
                    }
                }
            },
        ),
    ],
)
def test_every_representative_execution_change_changes_hash(
    profile_change: dict[str, object],
    dependency_change: dict[str, object],
) -> None:
    baseline = resolve_launch_policy(_profile(), _dependencies())
    changed = resolve_launch_policy(_profile(**profile_change), _dependencies(**dependency_change))
    assert changed.policy_hash != baseline.policy_hash


def test_metadata_changes_do_not_change_hash() -> None:
    baseline = resolve_launch_policy(_profile(), _dependencies())
    changed = resolve_launch_policy(
        _profile(
            name="renamed",
            description="changed",
            capabilities=["different"],
            tags=["different"],
        ),
        _dependencies(),
    )
    assert changed == baseline


def test_opencode_execution_name_uses_requested_lookup_not_profile_display_name() -> None:
    frozen = resolve_launch_policy(
        _profile(name="display-name", provider="opencode_cli"),
        _dependencies(
            provider="opencode_cli",
            agent_profile="requested-profile",
            resolved_mcp_servers=None,
            selected_skill_catalog=None,
            selected_skill_contents=None,
        ),
    )

    assert frozen.policy.binding.agent_profile == "requested-profile"
    assert frozen.policy.derived.execution_profile_name == "requested-profile"


def test_antigravity_execution_name_uses_profile_document_name_and_fallback() -> None:
    named = resolve_launch_policy(
        _profile(name="display-name", provider="antigravity_cli"),
        _dependencies(provider="antigravity_cli", agent_profile="requested-profile"),
    )
    fallback = resolve_launch_policy(
        _profile(name="", provider="antigravity_cli"),
        _dependencies(provider="antigravity_cli", agent_profile="requested-profile"),
    )

    assert named.policy.derived.execution_profile_name == "display-name"
    assert fallback.policy.derived.execution_profile_name == "agent"


def test_role_defaults_and_mcp_names_are_resolved_without_settings_reads() -> None:
    profile = _profile(allowedTools=None)
    policy = resolve_launch_policy(profile, _dependencies())
    assert policy.policy.derived.allowed_tools == ("fs_read", "fs_list", "@cao")


def test_explicit_overrides_and_profile_timeout_are_effective() -> None:
    policy = resolve_launch_policy(
        _profile(),
        _dependencies(
            explicit_allowed_tools=("fs_write",),
            explicit_model="override-model",
        ),
    ).policy
    assert policy.derived.allowed_tools == ("fs_write",)
    assert policy.derived.model == "override-model"
    assert policy.derived.provider_init_timeout == 91


def test_explicit_empty_allowed_tools_remains_an_exact_override() -> None:
    policy = resolve_launch_policy(
        _profile(),
        _dependencies(explicit_allowed_tools=()),
    ).policy

    assert policy.derived.allowed_tools == ()


def test_server_timeout_and_explicit_provider_default_are_bound() -> None:
    policy = resolve_launch_policy(
        _profile(model=None, provider_init_timeout=None),
        _dependencies(
            server_provider_init_timeout=73,
            provider_default_model_resolved=True,
            provider_default_model="provider-default",
        ),
    ).policy
    assert policy.derived.provider_init_timeout == 73
    assert policy.derived.model == "provider-default"


def test_provider_that_ignores_profile_timeout_binds_server_timeout() -> None:
    policy = resolve_launch_policy(
        _profile(provider="codex"),
        _dependencies(provider="codex"),
    ).policy
    assert policy.derived.provider_init_timeout == 60


def test_freeze_producer_loads_profile_once_before_pure_resolution(tmp_path) -> None:
    source = tmp_path / "profile.md"
    source.write_text(
        "---\n"
        "name: display-name\n"
        "description: fixture\n"
        "provider: claude_code\n"
        "model: model-a\n"
        "allowedTools:\n"
        "  - fs_read\n"
        "---\n"
        "Fixture system prompt.\n",
        encoding="utf-8",
    )
    loads: list[str] = []

    def load_profile(name: str) -> AgentProfile:
        loads.append(name)
        return parse_agent_profile_text(source.read_text(encoding="utf-8"), name)

    frozen = freeze_launch_policy(
        "from-file",
        _dependencies(
            agent_profile="from-file",
            resolved_mcp_servers={},
            selected_skill_catalog="",
        ),
        declared_provider="codex",
        profile_loader=load_profile,
    )
    source.write_text("later mutation", encoding="utf-8")

    assert loads == ["from-file"]
    assert frozen.policy.binding.agent_profile == "from-file"
    assert frozen.policy.binding.provider_source == "binding"
    assert frozen.policy.derived.provider == "codex"
    assert frozen.policy.profile["provider"] == "claude_code"
    assert frozen.policy.profile["system_prompt"] == "Fixture system prompt."
    assert decode_launch_policy_material(frozen.material) == frozen.policy


def test_freeze_refuses_agent_profile_identity_different_from_request() -> None:
    loads: list[str] = []

    def load_profile(name: str) -> AgentProfile:
        loads.append(name)
        return _profile(name="accepted-display-name")

    with pytest.raises(PolicyResolutionError) as caught:
        freeze_launch_policy(
            "phase-b1",
            _dependencies(agent_profile="different-profile"),
            profile_loader=load_profile,
        )

    assert loads == ["phase-b1"]
    assert caught.value.component == "binding_identity"
    assert caught.value.declaration_key == "agents"


def test_resolved_mcp_key_mismatch_fails_closed() -> None:
    with pytest.raises(PolicyResolutionError, match="mcp_configuration"):
        resolve_launch_policy(
            _profile(),
            _dependencies(resolved_mcp_servers={"different": {"command": "safe"}}),
        )


def test_serializer_refusal_is_a_secret_safe_policy_error() -> None:
    secret = "秘密-token-value"

    class Unsupported:
        def __repr__(self) -> str:
            return secret

    profile = _profile(mcpServers={"cao": {"command": Unsupported()}})
    dependencies = _dependencies(resolved_mcp_servers={"cao": {"command": Unsupported()}})

    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(profile, dependencies)

    assert caught.value.component == "serialized_material"
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "dependency_change",
    [
        {"resolved_mcp_servers": {"cao": {"command": object()}}},
        {"provider_configuration": {"secret": object()}},
    ],
)
def test_dependency_serializer_refusal_is_a_secret_safe_policy_error(
    dependency_change: dict[str, object],
) -> None:
    secret = "dependency-secret-value"

    class Unsupported:
        def __repr__(self) -> str:
            return secret

    if "resolved_mcp_servers" in dependency_change:
        dependency_change["resolved_mcp_servers"] = {"cao": {"command": Unsupported()}}
    else:
        dependency_change["provider_configuration"] = {"secret": Unsupported()}

    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(_profile(), _dependencies(**dependency_change))

    assert caught.value.component == "serialized_material"
    assert str(caught.value) == (
        "launch policy component 'serialized_material' is unresolved for "
        "profile declaration key 'profile'"
    )
    assert secret not in str(caught.value)
    assert "Unsupported" not in str(caught.value)
    assert "pydantic" not in str(caught.value).lower()


@pytest.mark.parametrize("provider", ["claude-code", "unknown-provider", "not_registered_secret"])
def test_unknown_provider_fails_closed_before_policy_resolution(provider: str) -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(_profile(), _dependencies(provider=provider))

    assert caught.value.component == "provider"
    assert caught.value.declaration_key == "provider"
    assert provider not in str(caught.value)


def test_claude_permission_modes_distinguish_explicit_default_and_root_paths() -> None:
    explicit = resolve_launch_policy(
        _profile(permissionMode="bypassPermissions"),
        _dependencies(),
    ).policy.derived.permission_mode
    default = resolve_launch_policy(
        _profile(permissionMode=None),
        _dependencies(is_root=False),
    ).policy.derived.permission_mode
    root = resolve_launch_policy(
        _profile(permissionMode=None, allowedTools=["*"]),
        _dependencies(is_root=True),
    ).policy.derived.permission_mode

    assert explicit == "bypassPermissions"
    assert default == "cao-skip-permissions"
    assert root == "cao-root-no-skip-permissions"
    assert len({explicit, default, root}) == 3


@pytest.mark.parametrize(
    ("provider", "profile_change"),
    [
        ("opencode_cli", {}),
        ("hermes", {}),
        ("mock_cli", {}),
        ("claude_code", {"native_agent": "native-worker"}),
    ],
)
def test_provider_without_runtime_mcp_or_skills_binds_genuine_absence(
    provider: str,
    profile_change: dict[str, object],
) -> None:
    frozen = resolve_launch_policy(
        _profile(provider=provider, **profile_change),
        _dependencies(
            provider=provider,
            resolved_mcp_servers=None,
            selected_skill_catalog=None,
            selected_skill_contents=None,
        ),
    )

    assert to_plain_json_data(frozen.policy.profile["mcpServers"]) == _profile().mcpServers
    assert frozen.policy.derived.mcp_configuration == {}
    assert frozen.policy.derived.mcp_terminal_identity_env is False
    assert frozen.policy.derived.selected_skill_catalog is None
    assert frozen.policy.derived.selected_skill_contents is None


@pytest.mark.parametrize(
    ("provider", "profile_change"),
    [
        ("opencode_cli", {}),
        ("hermes", {}),
        ("mock_cli", {}),
        ("claude_code", {"native_agent": "native-worker"}),
    ],
)
@pytest.mark.parametrize(
    "resolved_mcp_servers",
    [
        {},
        {"cao": {"command": "first-command"}},
        {"cao": {"command": "different-command"}},
    ],
)
def test_provider_without_runtime_mcp_refuses_any_supplied_resolved_material(
    provider: str,
    profile_change: dict[str, object],
    resolved_mcp_servers: dict[str, object],
) -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(
            _profile(provider=provider, **profile_change),
            _dependencies(
                provider=provider,
                resolved_mcp_servers=resolved_mcp_servers,
                selected_skill_catalog=None,
                selected_skill_contents=None,
            ),
        )

    assert caught.value.component == "provider_capability"
    assert caught.value.declaration_key == "mcpServers"


def test_copilot_refuses_live_mcp_capability_even_without_profile_mcp() -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(
            _profile(provider="copilot_cli", mcpServers=None),
            _dependencies(
                provider="copilot_cli",
                resolved_mcp_servers=None,
                selected_skill_catalog=None,
                selected_skill_contents=None,
            ),
        )

    assert caught.value.component == "provider_capability"
    assert caught.value.declaration_key == "mcpServers"


@pytest.mark.parametrize(
    ("provider", "profile_change"),
    [
        ("opencode_cli", {}),
        ("hermes", {}),
        ("claude_code", {"native_agent": "native-worker"}),
    ],
)
@pytest.mark.parametrize(
    ("catalog", "contents"),
    [
        ("catalog", None),
        (None, {"skill": "content"}),
        ("catalog", {"skill": "content"}),
    ],
)
def test_provider_without_runtime_skill_consumption_refuses_supplied_material(
    provider: str,
    profile_change: dict[str, object],
    catalog: str | None,
    contents: dict[str, str] | None,
) -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(
            _profile(provider=provider, **profile_change),
            _dependencies(
                provider=provider,
                resolved_mcp_servers=None,
                selected_skill_catalog=catalog,
                selected_skill_contents=contents,
            ),
        )

    assert caught.value.component == "provider_capability"
    assert caught.value.declaration_key == "skills"


@pytest.mark.parametrize(
    ("profile", "dependencies", "component", "declaration_key"),
    [
        (
            _profile(model=None),
            _dependencies(),
            "provider_default",
            "model",
        ),
        (
            _profile(),
            _dependencies(selected_skill_catalog=None),
            "skill_catalog",
            "skills",
        ),
        (
            _profile(),
            _dependencies(resolved_mcp_servers=None),
            "mcp_configuration",
            "mcpServers",
        ),
        (
            _profile(provider="kiro_cli", engine="v2"),
            _dependencies(provider="kiro_cli"),
            "provider_capability",
            "engine",
        ),
        (
            _profile(provider="copilot_cli"),
            _dependencies(provider="copilot_cli"),
            "provider_capability",
            "mcpServers",
        ),
        (
            _profile(native_agent="native"),
            _dependencies(
                resolved_mcp_servers=None,
                selected_skill_catalog=None,
                selected_skill_contents=None,
                provider_configuration_resolved=False,
            ),
            "provider_configuration",
            "native_agent",
        ),
    ],
)
def test_unresolved_runtime_values_fail_closed_with_secret_safe_errors(
    profile: AgentProfile,
    dependencies: LaunchPolicyDependencies,
    component: str,
    declaration_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[str] = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: launched.append("subprocess"),  # pragma: no cover
    )

    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(profile, dependencies)

    assert launched == []
    message = str(caught.value)
    assert component in message
    assert declaration_key in message
    assert "model-a" not in message
    assert "/resolved/" not in message


def test_profile_and_source_mutation_cannot_change_frozen_material(tmp_path) -> None:
    profile = _profile()
    source = tmp_path / "profile.md"
    source.write_text("original", encoding="utf-8")
    frozen = resolve_launch_policy(profile, _dependencies())

    profile.system_prompt = "mutated object"
    profile.mcpServers["cao"]["command"] = "mutated command"  # type: ignore[index]
    source.write_text("mutated disk", encoding="utf-8")

    assert decode_launch_policy_material(frozen.material) == frozen.policy
    assert b"mutated object" not in frozen.material
    assert b"mutated command" not in frozen.material


def test_decoder_rejects_noncanonical_or_wrong_hash_material() -> None:
    frozen = resolve_launch_policy(_profile(), _dependencies())
    noncanonical = json.dumps(json.loads(frozen.material), indent=2).encode()

    with pytest.raises(ValueError, match="canonical"):
        decode_launch_policy_material(noncanonical)
    with pytest.raises(ValueError, match="hash"):
        decode_launch_policy_material(frozen.material, expected_policy_hash="0" * 64)


@pytest.mark.parametrize(
    "mutation",
    ["extra_derived", "missing_profile_field", "extra_binding", "missing_binding"],
)
def test_decoder_rejects_schema_drift(mutation: str) -> None:
    document = json.loads(resolve_launch_policy(_profile(), _dependencies()).material)
    if mutation == "extra_derived":
        document["derived"]["new_runtime_default"] = "unclassified"
    elif mutation == "missing_profile_field":
        del document["profile"]["hooks"]
    elif mutation == "extra_binding":
        document["binding"]["provider"] = "claude_code"
    else:
        del document["binding"]
    material = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with pytest.raises(ValueError, match="schema"):
        decode_launch_policy_material(material)


def test_effective_document_is_typed_and_contains_whole_profile() -> None:
    frozen = resolve_launch_policy(_profile(), _dependencies())
    decoded = decode_launch_policy_material(frozen.material)

    assert isinstance(decoded, EffectiveLaunchPolicy)
    assert set(decoded.profile) == EXECUTION_AFFECTING_PROFILE_FIELDS
    assert decoded.derived.mcp_configuration["cao"]["command"] == ("/resolved/bin/cao-mcp-server")
    assert decoded.derived.tool_policy.soft_enforcement is False
    assert decoded.derived.tool_policy.disallowed_native_tools


def test_dependency_dataclass_is_immutable() -> None:
    dependencies = _dependencies()
    with pytest.raises(Exception):
        dependencies.provider = "codex"  # type: ignore[misc]
    assert replace(dependencies, provider="codex").provider == "codex"


def test_open_policy_containers_are_recursively_frozen_three_levels_deep() -> None:
    frozen = resolve_launch_policy(
        _profile(
            hooks={"before": [{"command": "check", "meta": {"level": 3}}]},
            mcpServers={
                "cao": {
                    "command": "cao-mcp-server",
                    "args": ["--stdio"],
                    "env": {"NESTED": "value"},
                }
            },
        ),
        _dependencies(
            resolved_mcp_servers={
                "cao": {
                    "command": "/resolved/cao",
                    "args": ["--stdio"],
                    "env": {"NESTED": "value"},
                }
            },
            provider_configuration={"outer": {"middle": {"inner": ["one", {"two": "three"}]}}},
            selected_skill_contents={"skill": "content"},
        ),
    ).policy

    assert is_deep_frozen(frozen.profile)
    assert is_deep_frozen(frozen.derived.mcp_configuration)
    assert is_deep_frozen(frozen.derived.provider_configuration)
    assert is_deep_frozen(frozen.derived.selected_skill_contents)
    assert isinstance(frozen.profile, MappingProxyType)
    assert isinstance(frozen.profile["hooks"]["before"], tuple)
    assert isinstance(frozen.derived.provider_configuration["outer"]["middle"]["inner"], tuple)

    with pytest.raises(TypeError):
        frozen.profile["hooks"]["before"][0]["command"] = "mutated"
    with pytest.raises(TypeError):
        del frozen.derived.mcp_configuration["cao"]["env"]["NESTED"]
    for method in ("update", "pop", "clear", "setdefault"):
        with pytest.raises(AttributeError):
            getattr(frozen.derived.provider_configuration, method)
    with pytest.raises(AttributeError):
        frozen.profile["hooks"]["before"].append("mutated")  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        frozen.derived.provider = "codex"


def test_policy_is_detached_from_every_caller_held_source_container() -> None:
    profile = _profile()
    role_defaults = {
        "developer": ("execute_bash", "fs_read"),
        "reviewer": ("fs_read",),
    }
    resolved_mcp: dict[str, Any] = {
        "cao": {
            "command": "/resolved/cao",
            "args": ["--stdio"],
            "env": {"MODE": "review"},
        }
    }
    provider_configuration: dict[str, Any] = {
        "outer": {"middle": {"inner": ["stable", {"key": "value"}]}}
    }
    skill_contents = {"review-one": "stable content"}
    frozen = resolve_launch_policy(
        profile,
        _dependencies(
            role_tool_defaults=role_defaults,
            resolved_mcp_servers=resolved_mcp,
            provider_configuration=provider_configuration,
            selected_skill_contents=skill_contents,
        ),
    )
    before = frozen.policy.model_dump(mode="json")

    profile.hooks["before"][0]["command"] = "mutated"  # type: ignore[index]
    profile.mcpServers["cao"]["env"]["MODE"] = "mutated"  # type: ignore[index]
    profile.mcpServers["cao"]["args"].append("--mutated")  # type: ignore[index]
    profile.skills.append("mutated")  # type: ignore[union-attr]
    assert profile.container is not None
    assert profile.container.path_maps is not None
    profile.container.path_maps.append(profile.container.path_maps[0])
    profile.tools.append("mutated")  # type: ignore[union-attr]
    profile.toolAliases["mutated"] = "mutated"  # type: ignore[index]
    profile.allowedTools.append("mutated")  # type: ignore[union-attr]
    profile.toolsSettings["mutated"] = {"nested": ["mutated"]}  # type: ignore[index]
    profile.resources.append("mutated")  # type: ignore[union-attr]
    profile.codexConfig["mutated"] = {"nested": ["mutated"]}  # type: ignore[index]
    profile.claudeConfig["mutated"] = {"nested": ["mutated"]}  # type: ignore[index]
    resolved_mcp["cao"]["env"]["MODE"] = "mutated"
    provider_configuration["outer"]["middle"]["inner"][0] = "mutated"
    skill_contents["review-one"] = "mutated"
    role_defaults["reviewer"] = ("mutated",)

    assert frozen.policy.model_dump(mode="json") == before
    assert b"mutated" not in frozen.material


def test_deep_freeze_is_idempotent_and_plain_projection_preserves_empty_and_none() -> None:
    source: dict[str, Any] = {
        "empty": {},
        "none": None,
        "items": [{"nested": []}],
    }
    frozen = deep_freeze(source)
    frozen_again = deep_freeze(frozen)

    assert frozen_again is not frozen
    assert to_plain_json_data(frozen_again) == source
    assert is_deep_frozen(frozen)
    assert is_deep_frozen(frozen_again)
    assert not is_deep_frozen(source)
    assert to_plain_json_data(frozen) == source
    assert to_plain_json_data(frozen)["empty"] == {}
    assert to_plain_json_data(frozen)["none"] is None


@pytest.mark.parametrize(
    ("model_name", "mutation", "expected_code"),
    [
        ("derived", "missing", "missing"),
        ("derived", "extra", "extra_forbidden"),
        ("derived", "wrong_type", "string_type"),
        ("binding", "missing", "missing"),
        ("binding", "extra", "extra_forbidden"),
        ("binding", "wrong_type", "literal_error"),
    ],
)
def test_typed_model_validation_codes_are_preserved(
    model_name: str,
    mutation: str,
    expected_code: str,
) -> None:
    policy = resolve_launch_policy(_profile(), _dependencies()).policy
    model = type(policy.derived) if model_name == "derived" else type(policy.binding)
    value = (
        policy.derived.model_dump(mode="json")
        if model_name == "derived"
        else policy.binding.model_dump(mode="json")
    )
    required = "provider" if model_name == "derived" else "target_key"
    wrong = "provider" if model_name == "derived" else "provider_source"
    if mutation == "missing":
        del value[required]
    elif mutation == "extra":
        value["unexpected"] = "value"
    else:
        value[wrong] = 123 if model_name == "derived" else "default"

    with pytest.raises(ValidationError) as caught:
        model.model_validate(value)

    assert [error["type"] for error in caught.value.errors()] == [expected_code]


def test_typed_attributes_and_agent_profile_validation_remain_intact() -> None:
    profile = _profile()
    policy = resolve_launch_policy(profile, _dependencies()).policy
    reconstructed = AgentProfile.model_validate(
        {"name": profile.name, "description": profile.description, **policy.profile}
    )

    assert isinstance(policy.derived.provider, str)
    assert isinstance(policy.derived.allowed_tools, tuple)
    assert isinstance(policy.binding.declared_allowed_tools, type(None))
    assert to_plain_json_data(reconstructed.model_dump()) == profile.model_dump()


# Captured before E1-a from launch_policy.py SHA-256
# 81a18f89b274871999d30cd25ee921df8cfd3e35458c3fcde4381c308c4633f0.
# These are compressed fixture bytes, not output regenerated by the implementation
# under test. Decompression yields the exact pre-E1 canonical policy/aggregate bytes.
_PRE_E1_SOURCE_SHA256 = "81a18f89b274871999d30cd25ee921df8cfd3e35458c3fcde4381c308c4633f0"
_PRE_E1_CANONICAL_ZLIB_B64 = {
    "aggregate": (
        "eNrtV1Fv2zYQ/isB+zZEtWXZluSnZVs7YFvaYSnQhyQQKPIkEaZIQaTdBIH/+46krGSJG6TA0LRFAAOkj+Tdx7vv"
        "+EE3pNNSMAGGrM5vCK1B2aLrdSUkkBXpGmogKmNyHPZdk9UNKYXiQtVu+sh+DkzSHnhBpdSfcLRaS4yiNlLeWW01"
        "B7m3oqOt4NAXRm965h0Oro+JpX0NtlgDYiAtFYrsnJtebIF7KP8Nc04qU/RAObk8JqBqoWAfBa6AbazQag+8ULQ"
        "dV1vWFUyrStSbnrpd6HwXzBb6VigqC8SorLDXBagtWVVUGsAd4SZhjCgZLIfu0jlHxjgIbo9bk1SR2wSgBfOz4Y"
        "BQONxZOARtXBNK2MKKFvTGklUeHxMDEpjFrJi1kLJg1FKpsXKEPFzTyuKtTPDpsljclnyfXIVht3Cb43+GBHNhP"
        "rflxFEEw/1CTTMM7ze22zjbGy7c8LvUpRt66HD4E9GcNSAlzk81Xkn3OHunLZRar4cjH6hZ4/ARyrdgWROmZ0D"
        "7MO+FBYfL6MpikSqNFWgdjlCsXchaIO14uw8HmBOq8KtPutsKFfpCN6QRdeMY6OpzdbseSo6nDSJXdXF4/9/72I"
        "FyLvVIaFf2G9JR2xQt7UJD1hsw7vjkk+7XpqPMkaHRwTYwa3e5u0/xutfrd74QH/FchdczI08bJB+YexAaTK3x"
        "zQ0IGHxsptuWKu642ABbhzjYB2fQb6EfW/kh7wcK+MdhbO2R8qf3Gd92NnSHmzylBx7yvIeQi1A+vNdqMpl4Rl"
        "/Z162vZK/9C9XDVsAncJTyvPcngjH6yVPm2lhoixFX+E9CS5xIgQ+cT5TxFF0NrwkUpeP30Dneqffm/52BtUgGf"
        "2zPLZxK0SKXV3PHxo2Bv6Cm7PqUdX8Y19mBqIgHk9/SwmXcd3y827/FReNCrsg8ycokY5Qt8mRWptM5xPGMzdN8"
        "vuDzOVSc5+likVdsNssW8wVLWEYp5bN8nrI4gfgzj+tXUYPzy4daMBrIIUXYx/tyRfgfhIAwqr3rvvY1jiJjudD"
        "+pRi7ZeLYKBHEBLFO8ESEriLju4Y4DFvn4vT9b29GPpLd7nGNsf3mjsTcSdA9jYGrzsm5/SZE5tWro5MtFZKWE"
        "o7OfLddqAsVHQ39phWsjgbdeVSQyO0BdBtF0YVylVrdcYRunfktlu5IKIP5Yg68eU2erGbPoWNeQF/k7JuQs6d1"
        "95f084syPq8ypssqzacJRV1kScljFsdQLXmWxrRccPwtYUnLeLFI0mk8ZWmaLcvlIuGQVdMcIH9OZfzq30nH5Gf"
        "H/x9HJb+LD7HvTCNfvvheJPJFIn8giaRlls2qKX488jKGnC3SZBqzdFqmy9k0rWY0nefLOGMsz7MpQFUmPGPTCnh"
        "Fq6Rc8sOKc7kPPd4Rw0chctShFlEL0TYmu38B+GgoiA=="
    ),
    "baseline": (
        "eNqVVMtu2zAQ/BWDuRVWjAC9VKembVKgrZOiDpBDHRAUtZIIU6RAUk4MQ//eXVJ+1E6B9ESJy519zeyWFcqUytQs"
        "3zJRgwm8c7ZSGljOukZ4yIorNmUlSC0clFxobZ/xDNZqz3LTa31kbW0JeneLQGtVguPe9k5GwBF6yoJwNQS+gg1"
        "et0IZNhCMU2soYyp/h/nNKs8diBJdP0ph2dOUgamVgV0weAHZB2XNLn9uRLu3trLj0ppK1b0T9IpiEA6FcnWMkG"
        "U+lCpCS9u2wmAibObAW41JzbBPM/TIECrz4NbgGOWwJoj5/ZcbfOxgreCZDcOQIgZwrTJCc+yCCSpseHwfXA/4I"
        "LUqnZlg481rzeoIx3sqjt6QTQvDDh3GGxxAXwIWWcKR4azo4cimjAo8qBZsH1j+4WrKPGiQAdvuV0prLkUQ2iI1"
        "2MXF5HotlBaFhsmCjH5pliabpJIzayCfYKyAdbJznGTw1KqDA8JmWbY0NKf8CAhh6foWBzdRxmO3JCXvL4kixAf"
        "eWa3k5pgmButbw4Etv4gqOMhS+X89ua5Trp+Eb8bjvg9dT3c3paLjq7YFHQ46PL5jKYsGtMbvucXeWWLAnQ1QW"
        "LsaXR6EX+HxCMUtBNmkzwUIl76dCkB5eVsFJENlcdQt5ZFXQnsY0niS/PbVPZxqgBgax/05TpeeQoVYCMMaVTfU"
        "KCLCy8GeuIXeHjM3NX/9/c9d7KQamhtKk/i1ZZ0IDW9FR5lsWd2DJ/fZs3Ur3wlJrGtsuhspPDwNpyqtnV3dxUE"
        "8ol+F5fmxdPRGloM/SaHB1kbeFIAJQ4x9UKdsQK5SHNTbIqrSv1Xa/yPmM62ObIobc7/v9jKdn6q07UJSNH28Rb"
        "fn2qRFRG1NTMAW5bPZLCrrJVy2kRTOxrWdMo8lRf1Fj1Ff7yL7Nj5Ay/d5pX+W1HWtFW792EUf2Z6PuxV4QVIZ"
        "RRhBI1r8W0AIyKvotqMpfmrVoizy99TE3sMPqIXczGX3zdM2SpzHfHCOreA0vLilroY/8lNRCw=="
    ),
    "binding": (
        "eNqVVE1v2zAM/SuBehvqBgV2mW/d1g7YlnZYCvSwFoIs07YQfRiSnCYo/N9HSk6TJRvQnWRL4iP5+J5eWKVsrWz"
        "LyhcmWrCR9941SgMrWd+JAEV1yc5ZDVILDzUXWrtnXKNzOrDy19PBmXE1aIzbbWAcgq1VDZ4HN3hJoLt85ywK30"
        "LkK9jithHKspHAvFpjKJVzmgpsqyyi2EFr/NuAHKJydlczt8K8nhrZc+lso9rBC7pFmFK4BO1bQmRFEWKtHENo6"
        "YwRFhOzuYfgNBYxx1rnGFEgVBHAr8EzqmFNEIu7z9d42cNawTMbxzFnjOCNskJz7NpGFbc83Y9+ALxwSlDa2rMD"
        "m14rqSJRR0ghUHt0ieahhT3gFHcQZqgB28TzA7KP2x4PzpRVkUdlwA2RlR8uz1kADTIi0WGltOZSRKEdCoKdnc2"
        "u1kJpUWmYLekwPNpHW8xy04WzUM4wV8RO2SlOPghE1j4AYYuieLQ0qfIACGFp+wZHN1M2IF+Sig8XJApSAO8dMr"
        "M9FIbF/tbwhxRV+Mchu2pzlR9F6Kblboj9QHvXdWL8i3YVLR56XL5hE8sOtMbvhUPWHE3/1kWonFtNIT9B0BTvR"
        "Vjh8gDVDUTZ5c8lCJ+/vYpAGguuiaiHxuGwDZVTNkIHGPN8sute27vfVd4E7ikNiTTN+1MaL12FBrEQhnWq7Ygp"
        "UsJmf57VhdEBG7At//v9H7vc2Tg0OHQjCeyF9SJ23IieKnlh7QCBwufPzq9CLyTJrnN5bxLx+DQeG7X1bnWb5v"
        "GAcQ22F6bWMRplDuGohA4ZTsKpAAuGlHtvUNmBXOU8aLllMmZ4q7v/x8+TXdNaCLw7iSo9lLta9z5dHNvU9ETM9"
        "PEW456ak94iojUrASkq5/N5stYmXpgkCu/Sa50rTy0lA6aIyWDvkvq2IYLhr3Xlf5btdaUVPvaJxZBEX07PK/CKH"
        "DO5MIEmtPS3hBhRVylsJ1P81MqgO8r3ROIQ4Du0Qm4Xsv8a6DnKmsd6cI5GcBpeeqYux98/K03m"
    ),
    "empty": (
        "eNqNVE1v2zAM/S88DumCArvMt27YBmxLNywFehgKQZZpW7A+DElOGwT57yMlx+3SDNiJMimRj4+PPkCtXaNdB9UB"
        "ZIcuiTH4VhuECsZeRryqr2EFDSojAzZCGuMfySbvTYTKTca8iFrfoDl5KdFONxhE9FNQOeGcegVJhg6TGHBPbiu1"
        "gyOnCXqHTYbyd5nf0EYRUDbwsAJ0nXZ4qoJPqKakvTsBF07aJWrVKJR3re6mIPkWJT8Wd8JgtZNGEEaXdNoLdDuo"
        "Wmki0o3SSbFXEmbPpV5GThQjQ+A7HDPSwTMB5CF+pgYJSoMvApegLTHtdBJJW/RTgur99QoiGlSJWImDNkYomaTxN"
        "DmA1zHvEnUVS05mUYzeaLV/Sa6jsjt85vjXTHCj47+u3LBEqNwHGfvZ/JjSOLHvU6PZfDG+ZhNwJPON0Gx7NIbOG"
        "08t+UCnW5+w9n6Yn9zJOJC5x/ozJtWX4xZlKOegEzKu6NtEQ2o9TcAyjjKsY2GtiHbp7u6CcsoUPmbS+Sq2lIvSQ"
        "K+7nhXI83l6jpeR0+tIyF0nLt//eapdJMfUk6B57AcYZeqFlSMjOUA3YeTn60cfhjhKxWLoffHNyjo+HM8l3gU/3"
        "OZB3NO7ltqLi057Eh/GMwg9UcujhxoJMObaylsrXcNa7FENpQ7twRbDDsOyyq91P0sg/xyW1V4kvzlXvB1T2Q4+/"
        "M8OvNZ5wMJFGR/1Va3X66zop/TW5kkGn/9QAXcaH5EllXWfXxTn1ZssmX1MaMWCq3xDWYkbo+kHl4mKWaLV/DdBU"
        "bO+583JSXO2/LXFlEgM+dlJW3Q02pKWq3esxinid+yk2m/U+DXyZhehEh4i30rBjOeNvz7+AfR59Eg="
    ),
}

# Independently derived after E1 from the preserved pre-E1 ``baseline`` and
# ``binding`` member bytes above. The second member's binding target was changed
# to ``secondary`` before canonical serialization and digest calculation. This is
# a valid strictly ascending aggregate golden; it is not claimed as pre-E1 output.
_VALID_MULTI_PAIR_AGGREGATE_ZLIB_B64 = (
    "eJztVt9v2zYQ/leC69sg1fKPxLKelm3tgG1ph7pAHxKDoMiTRJgiBZJyYhj+3wuSsp05adcCwwYMeSLFI++Ox++7Tzv"
    "otBRMoIXidge0RuVIZ3QlJEIBXUMtpuUYkrhvC8UOSqG4ULWffmU/RyapQU6olPoeOXFaSwuF6qV8ZG01R3lY7YzeCI"
    "6GWN0bFhwOrhNw1NToyBq3UEBLhYK9d2PEBnlI5a9hbqGyxCDlkMCPjGpYJYCqFgoPwfABWe+EVof8iaLt0dqyjjCt"
    "KlH3hvpdPob340OZOkRIU+u4CK6ZbluqOBQwMmi13CAflUKNGNVpy7rUotmgAZ/Dxru4ef/LGyjA4EbgPez3+xjRoWm"
    "FopIIjsoJtyVhvzM9JjCUKo4phWHluWJ13o+1/nJ+j7dJquBUYSiASdpzJMzbH5X+/NL7RzahhCNOtKh7B8VinIBFic"
    "whJ3YtpCSMOip1DQW8enVxvaFC0lLixdIb7Z26U+lFvHKqFRYXTCuHysFTP9FgfalOB6CANE3vlH+n4pGjOxWW3/ZSX"
    "ghlnemZT96+9hDxeCAn8B5goqgTGzyh5YOHyioBLuyXtlzXMdefqG2G4X3vut6vveHCD79KXfrBYAcJ/C6kXDYoJSRw"
    "o5Vw2iPgnXZYar0ejnykdg0JfMLyLTrWxOkSqYlzIxz6vKyuHEFVacOw9XkUFZUW9/F5Iv2Ot/t4zgGP0PDcP4fX9Vu"
    "xqrRxUEAj6sYXygPh4WSP2DJIrVZC1eT5/X8eYkfW+HejQnl87aCjriEt7WJrqXu0/vjoXpu17SjzqGt0XBsgvF/tz1"
    "laG71+Fx7ikzbrSup7O1w9gQZNi/YshUbrdcBNiZU2GGKf2MkaZOsYp2XdMrDSfiu1v4fMT7g6oCl0zGO/O9L05pylb"
    "ecio/3kW3j7lJu+EfmyRiQIicVoNArMenCv2wAKo0PbjpmHKwX+hRMDv34I6Ntahy055hW/IbLrWgpqMVTRBrQXQ29FU"
    "nqqDCQMToO38LVE54Sqw7EDTIsdSNEKB8XMF7G3+AfWlG1vWPeb9d0oYj4ByxpsKfGPF7rUeH8QKNL4kAXQMs8nVUbZ"
    "JS/HuGCX82k2ZvOsnF9Nsnk1ofPZ4mqcM7ZY5BliVU55zrIKeUWraXnFv6A4/4pE3q6eCuRxAZ6TyUO8s6QtMq04Ndu"
    "/0cr/jzY+qtKZOOJD53903Is6fr86/he6+CH+u73I44s8vsjjPy6Pk8lsscgmPJ9PF+N8Mp1VfDbPMB+zKdL59GqB46"
    "sFwzybsizLL0vEbJbNp/NpRfMZxfwrSrM6xD9eVGiVxvBpZ8SGOkw3Y9h/BkJdDJw="
)
_VALID_MULTI_PAIR_AGGREGATE_SHA256 = (
    "13054bb5bb6ba0c9158b4a37ec4e680104fe76df086ed35c4c2c7f2e693c5452"
)


def _pre_e1_canonical_bytes(name: str) -> bytes:
    return zlib.decompress(base64.b64decode(_PRE_E1_CANONICAL_ZLIB_B64[name]))


def _stored_policy_snapshot(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
    policy_material: bytes,
) -> private_plan_snapshot.VerifiedPrivateSnapshot:
    database_path = tmp_path / "phase-b1-policy.db"
    assert database_path.parent == tmp_path
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE",
        database_path,
        raising=True,
    )
    materials = {
        "artifact_hash": b"SCOPE = {'main': {}}\n",
        "declaration": private_plan_snapshot.structured_material_bytes({"version": 1}),
        "targets": private_plan_snapshot.structured_material_bytes({"main": {}}),
        "limits": private_plan_snapshot.structured_material_bytes({}),
        "retry_policy": private_plan_snapshot.structured_material_bytes({}),
        "policy": policy_material,
        "memory": private_plan_snapshot.structured_material_bytes({"mode": "off"}),
    }
    components = plan_identifier.PlanV2Components(
        tier="script",
        artifact_hash=plan_identifier.digest_bytes(materials["artifact_hash"]),
        declaration=plan_identifier.digest_bytes(materials["declaration"]),
        targets=plan_identifier.digest_bytes(materials["targets"]),
        inputs=(),
        limits=plan_identifier.digest_bytes(materials["limits"]),
        retry_policy=plan_identifier.digest_bytes(materials["retry_policy"]),
        policy=plan_identifier.digest_bytes(materials["policy"]),
        memory=plan_identifier.digest_bytes(materials["memory"]),
    )
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="phase-b1",
        spec_snapshot="source",
        inputs_json="{}",
        state="running",
        started_at="2026-09-16T00:00:00Z",
        tier="script",
    )
    private_plan_snapshot.freeze_and_attach(run_id, components, materials)
    return private_plan_snapshot.private_snapshot_for_run(run_id, components)


def _byte_parity_corpus() -> dict[str, object]:
    policies = {
        "baseline": resolve_launch_policy(_profile(), _dependencies()),
        "binding": resolve_launch_policy(
            _profile(),
            _dependencies(
                provider_source="binding",
                explicit_model="declared",
                explicit_allowed_tools=(),
            ),
        ),
        "empty": resolve_launch_policy(
            _profile(mcpServers=None),
            _dependencies(
                resolved_mcp_servers={},
                selected_skill_catalog="",
                selected_skill_contents={},
            ),
        ),
    }
    entries = [
        {
            "target_key": policy.policy.binding.target_key,
            "agent_profile": policy.policy.binding.agent_profile,
            "policy": json.loads(policy.material),
            "policy_hash": policy.policy_hash,
        }
        for policy in policies.values()
    ]
    entries.sort(
        key=lambda value: (
            value["target_key"],
            value["agent_profile"],
            value["policy_hash"],
        )
    )
    aggregate = private_plan_snapshot.structured_material_bytes(
        {"schema": "execution-policy-private-v1", "policies": entries}
    )
    return {"policies": policies, "aggregate": aggregate}


def test_policy_bytes_hashes_and_aggregate_match_pre_freeze_corpus() -> None:
    assert _PRE_E1_SOURCE_SHA256 == (
        "81a18f89b274871999d30cd25ee921df8cfd3e35458c3fcde4381c308c4633f0"
    )
    corpus = _byte_parity_corpus()
    policies = cast(dict[str, Any], corpus["policies"])
    expected = {
        "baseline": (1687, "ab882f0ac5db1e9c57301c70b76207f2a749618cc9980eefb3d8c0fedfaf3b6d"),
        "binding": (1678, "76f7903a932c3bd1c11ef6d871ab5db5d6e6ab15537010c7786b653de8f09ee9"),
        "empty": (1410, "438b38cac5932b704e112c47945d44efdd97559fc228545c3c8aaad2947c13e1"),
    }

    for name, policy in policies.items():
        length, digest = expected[name]
        assert policy.material == _pre_e1_canonical_bytes(name)
        assert len(policy.material) == length
        assert policy.policy_hash == digest
        assert policy.material == private_plan_snapshot.structured_material_bytes(
            policy.policy.model_dump()
        )
        assert policy.material == private_plan_snapshot.structured_material_bytes(
            policy.policy.model_dump(mode="json")
        )
    aggregate = cast(bytes, corpus["aggregate"])
    assert aggregate == _pre_e1_canonical_bytes("aggregate")
    assert len(aggregate) == 5248
    assert plan_identifier.digest_bytes(aggregate) == (
        "ff5e5a61f017df040d0901f20eea0d59543024de0bb4da01ec26d388263347ce"
    )


def test_historical_aggregate_is_byte_parity_only_and_valid_golden_is_admitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical = _pre_e1_canonical_bytes("aggregate")
    duplicate_snapshot = _stored_policy_snapshot(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        run_id="historical-duplicate",
        policy_material=historical,
    )
    with pytest.raises(AgentAdmissionError) as duplicate:
        admit_agent_launch(
            duplicate_snapshot,
            target_key="main",
            agent_profile="phase-b1",
            requested_provider="claude_code",
        )
    assert duplicate.value.code == "agent_policy_unavailable"
    assert duplicate.value.status_code == 409

    valid_material = zlib.decompress(base64.b64decode(_VALID_MULTI_PAIR_AGGREGATE_ZLIB_B64))
    assert len(valid_material) == 3708
    assert plan_identifier.digest_bytes(valid_material) == _VALID_MULTI_PAIR_AGGREGATE_SHA256
    aggregate = json.loads(valid_material)
    assert [(entry["target_key"], entry["agent_profile"]) for entry in aggregate["policies"]] == [
        ("main", "phase-b1"),
        ("secondary", "phase-b1"),
    ]
    for entry in aggregate["policies"]:
        policy_material = private_plan_snapshot.structured_material_bytes(entry["policy"])
        assert plan_identifier.digest_bytes(policy_material) == entry["policy_hash"]
        assert entry["policy"]["binding"]["target_key"] == entry["target_key"]
        assert entry["policy"]["binding"]["agent_profile"] == entry["agent_profile"]

    valid_snapshot = _stored_policy_snapshot(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        run_id="valid-multi-pair",
        policy_material=valid_material,
    )
    main = admit_agent_launch(
        valid_snapshot,
        target_key="main",
        agent_profile="phase-b1",
        requested_provider="claude_code",
    )
    secondary = admit_agent_launch(
        valid_snapshot,
        target_key="secondary",
        agent_profile="phase-b1",
        requested_provider="claude_code",
        requested_model="declared",
        requested_allowed_tools=[],
    )
    assert main.binding.target_key == "main"
    assert secondary.binding.target_key == "secondary"


def test_model_dump_plain_projection_is_warning_free() -> None:
    policy = resolve_launch_policy(_profile(), _dependencies()).policy

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        python_dump = policy.model_dump()
        json_dump = policy.model_dump(mode="json")
        json_text = policy.model_dump_json()

    assert private_plan_snapshot.structured_material_bytes(
        python_dump
    ) == private_plan_snapshot.structured_material_bytes(json_dump)
    assert json_dump == json.loads(json_text)
    assert isinstance(python_dump["derived"]["allowed_tools"], tuple)
    assert isinstance(json_dump["derived"]["allowed_tools"], list)
    assert isinstance(python_dump["profile"], dict)
    assert isinstance(python_dump["derived"]["mcp_configuration"], dict)


def test_decode_expected_hash_returns_a_recursively_frozen_policy() -> None:
    produced = resolve_launch_policy(_profile(), _dependencies())
    decoded = decode_launch_policy_material(
        produced.material,
        expected_policy_hash=produced.policy_hash,
    )

    assert decoded == produced.policy
    assert is_deep_frozen(decoded.profile)
    assert is_deep_frozen(decoded.derived.mcp_configuration)
    with pytest.raises(TypeError):
        decoded.derived.mcp_configuration["cao"]["command"] = "mutated"
    with pytest.raises(ValueError, match="hash"):
        decode_launch_policy_material(
            produced.material,
            expected_policy_hash="0" * 64,
        )


class _UnsupportedPolicyValue:
    def __repr__(self) -> str:
        return "SECRET_CUSTOM_REPR"


class _SecretDeepcopyFailure:
    def __deepcopy__(self, memo: dict[int, object]) -> object:
        raise RuntimeError("SECRET_DEEPCOPY_VALUE")


@pytest.mark.parametrize(
    "dependency_change",
    [
        {
            "resolved_mcp_servers": MappingProxyType(
                {"cao": {"command": "/resolved/cao", "args": [], "env": {}}}
            )
        },
        {"provider_configuration": MappingProxyType({"configuration": "value"})},
        {"selected_skill_contents": MappingProxyType({"review-one": "instructions"})},
        {
            "resolved_mcp_servers": {
                "cao": {
                    "command": _SecretDeepcopyFailure(),
                    "args": [],
                    "env": {},
                }
            }
        },
        {"provider_configuration": {"configuration": _SecretDeepcopyFailure()}},
        {"selected_skill_contents": {"review-one": _SecretDeepcopyFailure()}},
        {"provider_configuration": {"configuration": {"unsupported"}}},
    ],
)
def test_dependency_copy_refusal_is_a_secret_safe_policy_error(
    dependency_change: dict[str, object],
) -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        resolve_launch_policy(_profile(), _dependencies(**dependency_change))

    assert caught.value.component == "serialized_material"
    assert caught.value.declaration_key == "profile"
    rendered = str(caught.value)
    assert "SECRET_DEEPCOPY_VALUE" not in rendered
    assert "cannot pickle" not in rendered
    assert "mappingproxy" not in rendered
    assert "RuntimeError" not in rendered


@pytest.mark.parametrize(
    "value",
    [
        {"unsupported": {"set-value"}},
        {"unsupported": _UnsupportedPolicyValue()},
        {"unsupported": b"bytes"},
    ],
)
def test_deep_freeze_unsupported_values_fail_with_safe_structural_path(
    value: object,
) -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        deep_freeze(value)

    assert caught.value.component == "policy_representation"
    assert caught.value.declaration_key.startswith("root.")
    assert "SECRET" not in str(caught.value)
    assert "unsupported" not in str(caught.value)


def test_deep_freeze_cycles_and_depth_fail_safely() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    too_deep: object = "leaf"
    for _ in range(80):
        too_deep = {"next": too_deep}

    for value in (cyclic, too_deep):
        with pytest.raises(PolicyResolutionError) as caught:
            deep_freeze(value)
        assert caught.value.component == "policy_representation"
        assert caught.value.declaration_key.startswith("root.")
        assert "self" not in str(caught.value)
        assert "next" not in str(caught.value)


def test_deep_freeze_detaches_supplied_mapping_proxy_from_live_backing_maps() -> None:
    nested_backing: dict[str, object] = {"stable": "before"}
    backing: dict[str, object] = {"nested": MappingProxyType(nested_backing)}
    supplied = MappingProxyType(backing)

    frozen = deep_freeze(supplied)
    backing["added"] = "after"
    nested_backing["stable"] = "after"
    nested_backing["added"] = "after"

    assert frozen is not supplied
    assert is_deep_frozen(frozen)
    assert to_plain_json_data(frozen) == {"nested": {"stable": "before"}}
