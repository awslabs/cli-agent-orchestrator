"""Pure supplied-fact assembly of mock-only launch policy material."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from cli_agent_orchestrator.services import launch_material
from cli_agent_orchestrator.services.launch_material import (
    DependencyClosure,
    ExecFact,
    ExecObjectFacts,
    LaunchMaterialError,
    LocalLaunchMaterialError,
    assemble_launch_dependencies,
    provider_configuration_for_provider,
)
from cli_agent_orchestrator.services.launch_policy import (
    PolicyResolutionError,
    deep_freeze,
    is_deep_frozen,
)
from cli_agent_orchestrator.services.plan_identifier import digest_bytes
from cli_agent_orchestrator.services.private_plan_snapshot import structured_material_bytes
from cli_agent_orchestrator.utils import agent_profiles
from cli_agent_orchestrator.utils.profile_value_resolution import (
    AuthorityResolver,
    ProfileValueRefusal,
)

_FACT_ROOT = "/tmp/cao-u6-supplied-facts"


def _profile_text(*, provider: str = "mock_cli", metadata: str = "", body: str = "system") -> str:
    return (
        "---\n"
        "name: local-mock\n"
        "description: local fixture\n"
        f"provider: {provider}\n"
        f"{metadata}"
        "---\n"
        f"{body}\n"
    )


def _fact(path: str) -> ExecFact:
    return ExecFact(path=path, dev=1, ino=2, size=3, mtime_ns=4)


def _exec_objects() -> ExecObjectFacts:
    return ExecObjectFacts(
        interpreter=_fact(f"{_FACT_ROOT}/bin/python"),
        provider_program=_fact(f"{_FACT_ROOT}/bin/mock-provider"),
        launcher=_fact(f"{_FACT_ROOT}/bin/launcher"),
    )


def _closure() -> DependencyClosure:
    root = f"{_FACT_ROOT}/site-packages"
    return DependencyClosure(
        package_root=root,
        dotenv_init=_fact(f"{root}/dotenv/__init__.py"),
        dotenv_main=_fact(f"{root}/dotenv/main.py"),
        dotenv_parser=_fact(f"{root}/dotenv/parser.py"),
        dotenv_variables=_fact(f"{root}/dotenv/variables.py"),
    )


def _resolver() -> AuthorityResolver:
    return AuthorityResolver(authority_env={}, credential_names=())


def _assemble(**changes: object):
    values: dict[str, object] = {
        "profile_name": "local-mock",
        "profile_text": _profile_text(),
        "value_resolver": _resolver().resolve,
        "target_key": "main",
        "provider_configuration": {},
        "exec_objects": _exec_objects(),
        "dependency_closure": _closure(),
        "role_tool_defaults": {"developer": ("execute_bash",)},
        "server_provider_init_timeout": 60,
    }
    values.update(changes)
    return assemble_launch_dependencies(**cast(Any, values))


def _error(reason: str, **changes: object) -> LocalLaunchMaterialError:
    with pytest.raises(LocalLaunchMaterialError) as caught:
        _assemble(**changes)
    assert caught.value.reason == reason
    assert str(caught.value) == f"launch material unavailable:{reason}"
    assert repr(caught.value) == f"LocalLaunchMaterialError(reason={reason!r})"
    assert set(caught.value.__dict__) == {"reason"}
    return caught.value


def test_real_mock_policy_freeze_returns_actual_canonical_deep_frozen_material() -> None:
    result = _assemble()

    assert result.declared_provider is None
    assert result.resolved_provider == "mock_cli"
    assert result.provider_source == "profile"
    assert result.policy.policy.derived.provider == "mock_cli"
    assert result.policy.material == structured_material_bytes(json.loads(result.policy.material))
    assert result.policy.policy_hash == digest_bytes(result.policy.material)
    cao_d1 = result.policy.policy.derived.provider_configuration["cao_d1"]
    assert isinstance(cao_d1, MappingProxyType)
    assert is_deep_frozen(cao_d1)
    assert cao_d1["provider"] == "mock_cli"
    assert cao_d1["credential_names"] == ()
    assert cao_d1["store_path"] is None
    assert list(cao_d1["exec_objects"]) == [
        "interpreter",
        "provider_program",
        "launcher",
    ]
    assert [record["name"] for record in cao_d1["dependency_closure"]] == [
        "dotenv/__init__.py",
        "dotenv/main.py",
        "dotenv/parser.py",
        "dotenv/variables.py",
    ]
    assert all(not dataclasses.is_dataclass(record) for record in cao_d1["dependency_closure"])
    with pytest.raises(FrozenInstanceError):
        result.resolved_provider = "changed"
    with pytest.raises(TypeError):
        cast(dict[str, Any], cao_d1)["provider"] = "changed"


def test_released_error_name_exports_and_catches_real_api_refusal() -> None:
    assert LaunchMaterialError is LocalLaunchMaterialError
    assert "LaunchMaterialError" in launch_material.__all__

    with pytest.raises(LaunchMaterialError) as caught:
        _assemble(provider_configuration=None)

    assert caught.value.reason == "provider_configuration_uninspected"
    assert str(caught.value) == ("launch material unavailable:provider_configuration_uninspected")


@pytest.mark.parametrize(
    ("configuration", "reason"),
    [
        (None, "provider_configuration_uninspected"),
        ([], "provider_configuration_type"),
        ({1: "value"}, "provider_configuration_type"),
        ({"cao_d1": {}}, "reserved_namespace_collision"),
    ],
)
def test_provider_configuration_refusals_are_closed(
    configuration: object,
    reason: str,
) -> None:
    _error(reason, provider_configuration=configuration)


def test_provider_projection_is_fresh_frozen_and_removes_only_control_namespace() -> None:
    source: dict[str, Any] = {
        "vendor_path": "/vendor/legitimate/path",
        "nested": {"items": ["one", "two"]},
        "cao_d1": {"provider": "mock_cli"},
    }

    projected = provider_configuration_for_provider(source)
    source["vendor_path"] = "/mutated"
    source["nested"]["items"][0] = "mutated"

    assert isinstance(projected, MappingProxyType)
    assert is_deep_frozen(projected)
    assert "cao_d1" not in projected
    assert projected["vendor_path"] == "/vendor/legitimate/path"
    assert projected["nested"]["items"] == ("one", "two")
    assert provider_configuration_for_provider(projected) is not projected
    assert provider_configuration_for_provider(deep_freeze({"vendor": 1})) == {"vendor": 1}
    with pytest.raises(TypeError):
        cast(dict[str, Any], projected)["vendor_path"] = "mutated"


@pytest.mark.parametrize("configuration", [None, [], {1: "value"}])
def test_provider_projection_refuses_invalid_top_level_shape(configuration: object) -> None:
    with pytest.raises(LocalLaunchMaterialError) as caught:
        provider_configuration_for_provider(cast(Any, configuration))
    assert caught.value.reason == "provider_configuration_type"


@pytest.mark.parametrize(
    ("field", "bad_value", "reason"),
    [
        ("path", "relative/program", "exec_fact_shape"),
        ("path", "/owned/../escape", "exec_fact_shape"),
        ("dev", True, "exec_fact_shape"),
        ("ino", -1, "exec_fact_shape"),
        ("size", False, "exec_fact_shape"),
        ("mtime_ns", -1, "exec_fact_shape"),
    ],
)
def test_exec_fact_shape_refuses_relative_traversal_bool_and_negative_values(
    field: str,
    bad_value: object,
    reason: str,
) -> None:
    bad = replace(
        _fact(f"{_FACT_ROOT}/bin/python"),
        **cast(Any, {field: bad_value}),
    )
    _error(reason, exec_objects=replace(_exec_objects(), interpreter=bad))


def test_exec_object_substitution_and_wrong_types_refuse() -> None:
    _error("exec_fact_shape", exec_objects=cast(Any, object()))
    _error(
        "exec_fact_shape",
        exec_objects=replace(_exec_objects(), launcher=cast(Any, _closure())),
    )


@pytest.mark.parametrize(
    "closure",
    [
        object(),
        replace(_closure(), package_root="relative"),
        replace(_closure(), package_root=f"{_FACT_ROOT}/site-packages/"),
        replace(
            _closure(),
            dotenv_init=_fact(f"{_FACT_ROOT}/site-packages/__init__.py"),
        ),
        replace(
            _closure(),
            dotenv_main=cast(
                Any,
                _fact(f"{_FACT_ROOT}/site-packages/dotenv/wrong.py"),
            ),
        ),
    ],
)
def test_dependency_closure_requires_exact_four_module_facts(closure: object) -> None:
    _error("dependency_closure_incomplete", dependency_closure=closure)


def test_non_mock_provider_refuses_before_freeze_even_with_other_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("freeze must not run")

    monkeypatch.setattr(launch_material, "freeze_launch_policy", explode)

    _error(
        "provider_not_local_mock",
        profile_text=_profile_text(provider="claude_code"),
        credential_names=("credential",),
        store_path="/owned/store",
    )


def test_inherited_and_explicit_mock_provider_provenance_change_material() -> None:
    inherited = _assemble()
    explicit = _assemble(declared_provider="mock_cli")

    assert inherited.provider_source == "profile"
    assert explicit.provider_source == "binding"
    assert inherited.declared_provider is None
    assert explicit.declared_provider == "mock_cli"
    assert inherited.policy.material != explicit.policy.material
    assert inherited.policy.policy_hash != explicit.policy.policy_hash


def test_raw_profile_is_parsed_once_with_both_positional_arguments_and_local_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_parse = agent_profiles.parse_agent_profile_text
    actual_loads = agent_profiles.frontmatter.loads
    calls: list[tuple[str, str]] = []
    loads_calls: list[str] = []
    loaded_ids: list[int] = []

    def loads(profile_text: str):
        loads_calls.append(profile_text)
        return actual_loads(profile_text)

    def parse(profile_text: str, profile_name: str, *, value_resolver):
        calls.append((profile_text, profile_name))
        return actual_parse(profile_text, profile_name, value_resolver=value_resolver)

    actual_freeze = launch_material.freeze_launch_policy

    def freeze(profile_name: str, dependencies, **kwargs):
        loader = kwargs["profile_loader"]
        loaded_ids.extend((id(loader(profile_name)), id(loader(profile_name))))
        return actual_freeze(profile_name, dependencies, **kwargs)

    def default_loader(*args: object, **kwargs: object) -> None:
        raise AssertionError("default loader must not run")

    monkeypatch.setattr(agent_profiles.frontmatter, "loads", loads)
    monkeypatch.setattr(launch_material, "parse_agent_profile_text", parse)
    monkeypatch.setattr(launch_material, "freeze_launch_policy", freeze)
    monkeypatch.setattr(agent_profiles, "load_agent_profile", default_loader)
    raw = _profile_text(body="$UNCHANGED")

    result = _assemble(profile_text=raw)

    assert calls == [(raw, "local-mock")]
    assert loads_calls == [raw]
    assert len(set(loaded_ids)) == 1
    assert result.policy.policy.profile["system_prompt"] == "$UNCHANGED"


def test_assembler_never_stats_supplied_fact_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("stat must not run")

    monkeypatch.setattr(Path, "stat", explode)

    result = _assemble()

    assert result.resolved_provider == "mock_cli"


def test_wrong_local_loader_name_and_postfreeze_provider_mismatch_are_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def wrong_loader(profile_name: str, dependencies, **kwargs):
        kwargs["profile_loader"]("other")

    monkeypatch.setattr(launch_material, "freeze_launch_policy", wrong_loader)
    _error("loader_identity")

    def mismatched_provider(*args: object, **kwargs: object):
        return SimpleNamespace(
            policy=SimpleNamespace(derived=SimpleNamespace(provider="claude_code"))
        )

    monkeypatch.setattr(launch_material, "freeze_launch_policy", mismatched_provider)
    _error("provider_identity_mismatch")


def test_u1_same_key_authority_is_resolved_without_credentials_or_store() -> None:
    resolver = AuthorityResolver(
        authority_env={"PATH": "/captured/nonsecret/path"},
        credential_names=(),
    )
    profile_text = _profile_text(
        metadata=(
            "mcpServers:\n" "  local:\n" "    command: mock\n" "    env:\n" "      PATH: $PATH\n"
        )
    )

    result = _assemble(profile_text=profile_text, value_resolver=resolver.resolve)

    assert result.policy.policy.profile["mcpServers"]["local"]["env"]["PATH"] == (
        "/captured/nonsecret/path"
    )
    assert resolver.accepted == {("mcpServers", "local", "env", "PATH"): "PATH"}


def test_u1_credential_cycle_and_depth_refusals_propagate_unchanged() -> None:
    credential_resolver = AuthorityResolver(
        authority_env={},
        credential_names=("TOKEN",),
    )
    credential_profile = _profile_text(
        metadata=(
            "mcpServers:\n" "  local:\n" "    command: mock\n" "    env:\n" "      TOKEN: $TOKEN\n"
        )
    )
    with pytest.raises(ProfileValueRefusal) as credential:
        _assemble(
            profile_text=credential_profile,
            value_resolver=credential_resolver.resolve,
        )
    assert credential.value.code == "mcp_credential_destination_unsupported"

    cycle_profile = _profile_text(metadata=("hooks: &recursive\n  child: *recursive\n"))
    with pytest.raises(ProfileValueRefusal) as cycle:
        _assemble(profile_text=cycle_profile)
    assert cycle.value.code == "profile_document_cycle"

    nested = "hooks:\n"
    for depth in range(67):
        nested += f"{'  ' * (depth + 1)}child:\n"
    nested += f"{'  ' * 68}leaf: value\n"
    with pytest.raises(ProfileValueRefusal) as too_deep:
        _assemble(profile_text=_profile_text(metadata=nested))
    assert too_deep.value.code == "profile_document_depth_exceeded"


@pytest.mark.parametrize(
    ("credential_names", "store_path"),
    [
        (("TOKEN",), None),
        ((), "/owned/store"),
    ],
)
def test_credentials_and_store_are_not_supported(
    credential_names: tuple[str, ...],
    store_path: str | None,
) -> None:
    _error(
        "credential_capability_unavailable",
        credential_names=credential_names,
        store_path=store_path,
    )


def test_b1_policy_resolution_error_propagates_unchanged() -> None:
    with pytest.raises(PolicyResolutionError) as caught:
        _assemble(server_provider_init_timeout=0)

    assert caught.value.component == "provider_init_timeout"
    assert caught.value.declaration_key == "provider_init_timeout"


def test_cao_d1_is_plain_json_before_freeze_and_inputs_are_not_mutated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = {"vendor": {"paths": ["/one", "/two"]}}
    actual_freeze = launch_material.freeze_launch_policy

    def freeze(profile_name: str, dependencies, **kwargs):
        supplied = dependencies.provider_configuration
        assert type(supplied) is dict
        cao_d1 = supplied["cao_d1"]
        assert type(cao_d1) is dict
        assert type(cao_d1["exec_objects"]) is dict
        assert type(cao_d1["dependency_closure"]) is list
        assert all(type(record) is dict for record in cao_d1["dependency_closure"])
        assert not any(dataclasses.is_dataclass(value) for value in cao_d1.values())
        return actual_freeze(profile_name, dependencies, **kwargs)

    monkeypatch.setattr(launch_material, "freeze_launch_policy", freeze)

    result = _assemble(provider_configuration=configuration)
    configuration["vendor"]["paths"][0] = "/mutated"

    frozen = result.policy.policy.derived.provider_configuration
    assert frozen["vendor"]["paths"] == ("/one", "/two")
    assert "cao_d1" not in configuration
