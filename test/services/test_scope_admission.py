"""Admission checks against policies retrieved from the private snapshot store."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services import plan_identifier as pi
from cli_agent_orchestrator.services import private_plan_snapshot as snapshots
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services.launch_policy import (
    EffectiveAgentBinding,
    EffectiveDerivedPolicy,
    EffectiveLaunchPolicy,
    LaunchPolicy,
    LaunchPolicyDependencies,
    is_deep_frozen,
    resolve_launch_policy,
)
from cli_agent_orchestrator.services.scope_admission import (
    AgentAdmissionError,
    LaunchAdmissionError,
    admit_agent_launch,
    policy_for_binding,
)


@pytest.fixture(autouse=True)
def _private_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database_file = tmp_path / "workflow.db"
    assert database_file.parent == tmp_path
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE",
        database_file,
        raising=True,
    )
    return database_file


def _profile(**changes: object) -> AgentProfile:
    values: dict[str, object] = {
        "name": "admission-profile",
        "description": "admission fixture",
        "provider": "claude_code",
        "system_prompt": "system",
        "prompt": "prompt",
        "role": "reviewer",
        "allowedTools": ["fs_read"],
        "model": "profile-model",
        "permissionMode": "plan",
    }
    values.update(changes)
    return AgentProfile.model_validate(values)


def _policy(**changes: object) -> LaunchPolicy:
    values: dict[str, object] = {
        "provider": "claude_code",
        "target_key": "main",
        "agent_profile": "admission-profile",
        "provider_source": "profile",
        "role_tool_defaults": {
            "developer": ("execute_bash", "fs_read"),
            "reviewer": ("fs_read",),
        },
        "server_provider_init_timeout": 60,
        "resolved_mcp_servers": {},
        "selected_skill_catalog": "",
        "selected_skill_contents": {},
        "provider_default_model_resolved": False,
        "provider_configuration_resolved": True,
        "provider_configuration": {},
        "is_root": False,
    }
    profile_changes = cast(dict[str, object], changes.pop("profile", {}))
    values.update(changes)
    return resolve_launch_policy(
        _profile(**profile_changes),
        LaunchPolicyDependencies(**cast(Any, values)),
    )


def _deep_policy() -> LaunchPolicy:
    return _policy(
        explicit_allowed_tools=("fs_read",),
        profile={
            "skills": ["review-*"],
            "hooks": {"before": [{"command": "check", "meta": {"level": 3}}]},
            "mcpServers": {
                "cao": {
                    "command": "cao-mcp-server",
                    "args": ["--stdio"],
                    "env": {"MODE": "review"},
                }
            },
        },
        resolved_mcp_servers={
            "cao": {
                "command": "/resolved/cao",
                "args": ["--stdio"],
                "env": {"MODE": "review"},
            }
        },
        selected_skill_catalog="## Available Skills\n\n- review-one: content",
        selected_skill_contents={"review-one": "stable instructions"},
        provider_configuration={"outer": {"middle": {"inner": ["stable", {"key": "value"}]}}},
    )


def _aggregate(*policies: LaunchPolicy) -> dict[str, object]:
    entries = [
        {
            "target_key": policy.policy.binding.target_key,
            "agent_profile": policy.policy.binding.agent_profile,
            "policy": json.loads(policy.material),
            "policy_hash": policy.policy_hash,
        }
        for policy in policies
    ]
    entries.sort(key=lambda entry: (entry["target_key"], entry["agent_profile"]))
    return {"schema": "execution-policy-private-v1", "policies": entries}


def _verified(
    aggregate: object,
    *,
    run_id: str = "admission-run",
) -> snapshots.VerifiedPrivateSnapshot:
    policy_material = snapshots.structured_material_bytes(aggregate)
    materials = {
        "artifact_hash": b"SCOPE = {'main': {}}\n",
        "declaration": snapshots.structured_material_bytes({"version": 1}),
        "targets": snapshots.structured_material_bytes({"main": {}}),
        "limits": snapshots.structured_material_bytes({}),
        "retry_policy": snapshots.structured_material_bytes({}),
        "policy": policy_material,
        "memory": snapshots.structured_material_bytes({"mode": "off"}),
    }
    components = pi.PlanV2Components(
        tier="script",
        artifact_hash=pi.digest_bytes(materials["artifact_hash"]),
        declaration=pi.digest_bytes(materials["declaration"]),
        targets=pi.digest_bytes(materials["targets"]),
        inputs=(),
        limits=pi.digest_bytes(materials["limits"]),
        retry_policy=pi.digest_bytes(materials["retry_policy"]),
        policy=pi.digest_bytes(materials["policy"]),
        memory=pi.digest_bytes(materials["memory"]),
    )
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="source",
        inputs_json="{}",
        state="running",
        started_at="2026-09-16T00:00:00Z",
        tier="script",
    )
    snapshots.freeze_and_attach(run_id, components, materials)
    return snapshots.private_snapshot_for_run(run_id, components)


def _admit(
    snapshot: snapshots.VerifiedPrivateSnapshot,
    **changes: object,
):
    values: dict[str, object] = {
        "target_key": "main",
        "agent_profile": "admission-profile",
        "requested_provider": "claude_code",
        "requested_model": None,
        "requested_allowed_tools": None,
    }
    values.update(changes)
    return admit_agent_launch(snapshot, **cast(Any, values))


def _assert_error(
    snapshot: snapshots.VerifiedPrivateSnapshot,
    code: str,
    status_code: int,
    **changes: object,
) -> AgentAdmissionError:
    with pytest.raises(AgentAdmissionError) as caught:
        _admit(snapshot, **changes)
    assert caught.value.code == code
    assert caught.value.status_code == status_code
    assert caught.value.retryable is False
    return caught.value


class _HostileValue:
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("SECRET_COMPARISON")

    def __repr__(self) -> str:
        raise RuntimeError("SECRET_REPR")

    def __str__(self) -> str:
        raise RuntimeError("SECRET_STRING")


class _HostileString(str):
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("SECRET_STRING_COMPARISON")


class _SpoofBuiltinContainerMeta(type):
    equality_calls = 0

    def __eq__(cls, other: object) -> bool:
        _SpoofBuiltinContainerMeta.equality_calls += 1
        return other is list


class _ChangingToolsContainer(metaclass=_SpoofBuiltinContainerMeta):
    iteration_calls = 0

    def __iter__(self):
        _ChangingToolsContainer.iteration_calls += 1
        if _ChangingToolsContainer.iteration_calls == 1:
            return iter(("fs_read",))
        return iter((_HostileValue(),))


def test_real_policy_freeze_store_retrieval_and_admission_round_trip() -> None:
    frozen = _policy()
    snapshot = _verified(_aggregate(frozen))

    admitted = _admit(snapshot)

    assert admitted == frozen.policy
    assert admitted.binding.declared_model is None
    assert admitted.binding.declared_allowed_tools is None


def test_real_store_admission_returns_typed_deep_frozen_policy_graph() -> None:
    admitted = _admit(
        _verified(_aggregate(_deep_policy())),
        requested_allowed_tools=["fs_read"],
    )

    assert isinstance(admitted, EffectiveLaunchPolicy)
    assert isinstance(admitted.binding, EffectiveAgentBinding)
    assert isinstance(admitted.derived, EffectiveDerivedPolicy)
    assert isinstance(admitted.profile, MappingProxyType)
    assert isinstance(admitted.profile["hooks"], MappingProxyType)
    assert isinstance(admitted.profile["hooks"]["before"], tuple)
    assert isinstance(admitted.profile["hooks"]["before"][0], MappingProxyType)
    assert isinstance(admitted.derived.mcp_configuration, MappingProxyType)
    assert isinstance(admitted.derived.mcp_configuration["cao"], MappingProxyType)
    assert isinstance(admitted.derived.mcp_configuration["cao"]["args"], tuple)
    assert isinstance(admitted.derived.provider_configuration, MappingProxyType)
    assert isinstance(
        admitted.derived.provider_configuration["outer"]["middle"]["inner"],
        tuple,
    )
    assert isinstance(admitted.derived.selected_skill_contents, MappingProxyType)
    assert isinstance(admitted.binding.declared_allowed_tools, tuple)
    assert isinstance(admitted.derived.allowed_tools, tuple)
    assert isinstance(admitted.derived.tool_policy.allowed_native_tools, tuple)
    assert is_deep_frozen(admitted.profile)
    assert is_deep_frozen(admitted.derived.mcp_configuration)
    assert is_deep_frozen(admitted.derived.provider_configuration)
    assert is_deep_frozen(admitted.derived.selected_skill_contents)


def test_real_store_admitted_policy_refuses_shallow_nested_and_model_mutation() -> None:
    produced = _deep_policy()
    plain_counterexample = json.loads(produced.material)
    plain_counterexample["profile"]["system_prompt"] = "MUTATED"
    plain_counterexample["profile"]["hooks"]["before"][0]["command"] = "MUTATED"
    assert plain_counterexample["profile"]["system_prompt"] == "MUTATED"
    assert plain_counterexample["profile"]["hooks"]["before"][0]["command"] == "MUTATED"

    admitted = _admit(
        _verified(_aggregate(produced)),
        requested_allowed_tools=["fs_read"],
    )

    with pytest.raises(TypeError):
        cast(dict[str, Any], admitted.profile)["system_prompt"] = "MUTATED"
    with pytest.raises(TypeError):
        cast(dict[str, Any], admitted.profile["hooks"]["before"][0])["command"] = "MUTATED"
    with pytest.raises(TypeError):
        cast(list[str], admitted.derived.mcp_configuration["cao"]["args"])[0] = "MUTATED"
    with pytest.raises(TypeError):
        cast(
            dict[str, Any],
            admitted.derived.provider_configuration["outer"]["middle"],
        )["inner"] = "MUTATED"
    with pytest.raises(ValidationError):
        admitted.derived.provider = "codex"
    with pytest.raises(ValidationError):
        admitted.binding.target_key = "other"


def test_same_real_snapshot_admissions_are_fresh_and_plain_projection_is_detached() -> None:
    snapshot = _verified(_aggregate(_deep_policy()))

    first = _admit(snapshot, requested_allowed_tools=["fs_read"])
    second = _admit(snapshot, requested_allowed_tools=["fs_read"])
    projection = first.model_dump()

    assert first is not second
    assert first.profile is not second.profile
    assert first.profile["hooks"] is not second.profile["hooks"]
    assert first.derived.mcp_configuration is not second.derived.mcp_configuration
    assert (
        first.derived.provider_configuration["outer"]
        is not second.derived.provider_configuration["outer"]
    )
    projection["profile"]["system_prompt"] = "MUTATED"
    projection["profile"]["hooks"]["before"][0]["command"] = "MUTATED"
    projection["derived"]["mcp_configuration"]["cao"]["args"][0] = "MUTATED"
    projection["derived"]["provider_configuration"]["outer"]["middle"]["inner"][0] = "MUTATED"

    assert first.profile["system_prompt"] == "system"
    assert second.profile["system_prompt"] == "system"
    assert first.profile["hooks"]["before"][0]["command"] == "check"
    assert second.derived.mcp_configuration["cao"]["args"][0] == "--stdio"
    assert first.derived.provider_configuration["outer"]["middle"]["inner"][0] == "stable"

    third = _admit(snapshot, requested_allowed_tools=["fs_read"])
    assert third is not first
    assert third.profile is not first.profile
    assert third.profile["system_prompt"] == "system"
    assert third.derived.mcp_configuration["cao"]["args"][0] == "--stdio"


@pytest.mark.parametrize("provider_source", ["profile", "binding"])
def test_admission_retains_provider_provenance(provider_source: str) -> None:
    frozen = _policy(provider_source=provider_source)
    admitted = _admit(_verified(_aggregate(frozen)))

    assert admitted.binding.provider_source == provider_source


def test_well_formed_document_without_pair_is_not_declared() -> None:
    other = _policy(target_key="other")
    snapshot = _verified(_aggregate(other))

    _assert_error(snapshot, "agent_not_declared", 403)


@pytest.mark.parametrize(
    "aggregate",
    [
        {},
        {"schema": "wrong", "policies": []},
        {"schema": "execution-policy-private-v1", "policies": {}, "extra": 1},
    ],
)
def test_malformed_aggregate_is_unavailable(aggregate: object) -> None:
    snapshot = _verified(aggregate)
    error = _assert_error(snapshot, "agent_policy_unavailable", 409)
    assert str(error) == "agent admission policy is unavailable"


def test_missing_and_noncanonical_policy_material_are_unavailable() -> None:
    missing = snapshots.VerifiedPrivateSnapshot("plan", ())
    noncanonical = snapshots.VerifiedPrivateSnapshot(
        "plan",
        (("policy", b'{ "policies":[],"schema":"execution-policy-private-v1"}'),),
    )

    _assert_error(missing, "agent_policy_unavailable", 409)
    _assert_error(noncanonical, "agent_policy_unavailable", 409)


@pytest.mark.parametrize("defect", ["wrong_hash", "pair_mismatch", "duplicate", "unordered"])
def test_corrupt_or_ambiguous_entry_is_unavailable(defect: str) -> None:
    first = _policy()
    second = _policy(target_key="z")
    aggregate = _aggregate(first, second)
    entries = cast(list[dict[str, object]], aggregate["policies"])
    if defect == "wrong_hash":
        entries[0]["policy_hash"] = "0" * 64
    elif defect == "pair_mismatch":
        entries[0]["target_key"] = "different"
    elif defect == "duplicate":
        entries.append(dict(entries[0]))
    else:
        entries.reverse()

    _assert_error(_verified(aggregate), "agent_policy_unavailable", 409)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"requested_provider": "codex"}, "agent_provider_mismatch"),
        ({"requested_model": "different"}, "agent_model_mismatch"),
        ({"requested_allowed_tools": ["fs_read"]}, "agent_tools_mismatch"),
    ],
)
def test_requested_declaration_mismatches_are_refused(
    changes: dict[str, object], code: str
) -> None:
    snapshot = _verified(_aggregate(_policy()))
    _assert_error(snapshot, code, 403, **changes)


def test_recognised_provider_mismatch_only_echoes_enum_validated_ids() -> None:
    error = _assert_error(
        _verified(_aggregate(_policy())),
        "agent_provider_mismatch",
        403,
        requested_provider="codex",
    )

    assert str(error) == (
        "requested provider 'codex' does not match approved provider 'claude_code'"
    )


def test_none_and_empty_tool_declarations_remain_distinct() -> None:
    omitted = _verified(_aggregate(_policy()), run_id="omitted")
    explicit_empty = _verified(
        _aggregate(_policy(explicit_allowed_tools=())),
        run_id="explicit-empty",
    )

    _assert_error(omitted, "agent_tools_mismatch", 403, requested_allowed_tools=[])
    _assert_error(explicit_empty, "agent_tools_mismatch", 403)
    assert _admit(explicit_empty, requested_allowed_tools=[]).binding.declared_allowed_tools == ()
    _assert_error(
        explicit_empty,
        "agent_tools_mismatch",
        403,
        requested_allowed_tools=cast(Any, "fs_read"),
    )


def test_malformed_inner_policy_suppresses_raw_validation_cause() -> None:
    aggregate = _aggregate(_policy())
    entry = cast(list[dict[str, object]], aggregate["policies"])[0]
    policy = cast(dict[str, object], entry["policy"])
    policy["unexpected_SECRET_VALUE"] = "SECRET_INNER_POLICY"
    entry["policy_hash"] = pi.digest_bytes(snapshots.structured_material_bytes(policy))

    error = _assert_error(
        _verified(aggregate),
        "agent_policy_unavailable",
        409,
    )
    rendered = "".join(traceback.format_exception(error))
    chained: list[BaseException] = []
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        chained.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)

    assert "SECRET_INNER_POLICY" not in rendered
    assert "unexpected_SECRET_VALUE" not in rendered
    assert chained == [error]


@pytest.mark.parametrize(
    ("field", "unsafe", "code"),
    [
        ("target_key", _HostileValue(), "agent_not_declared"),
        ("target_key", _HostileString("main"), "agent_not_declared"),
        ("agent_profile", _HostileValue(), "agent_not_declared"),
        ("requested_provider", _HostileString("claude_code"), "agent_provider_mismatch"),
        ("requested_model", _HostileValue(), "agent_model_mismatch"),
        ("requested_model", _HostileString("profile-model"), "agent_model_mismatch"),
        ("requested_allowed_tools", [_HostileValue()], "agent_tools_mismatch"),
        ("requested_allowed_tools", (_HostileString("fs_read"),), "agent_tools_mismatch"),
        ("requested_allowed_tools", _HostileValue(), "agent_tools_mismatch"),
    ],
)
def test_untrusted_request_shapes_refuse_without_dispatching_user_callbacks(
    field: str,
    unsafe: object,
    code: str,
) -> None:
    snapshot = _verified(_aggregate(_policy()))
    caught_error: AgentAdmissionError | None = None

    try:
        _admit(snapshot, **{field: unsafe})
    except AgentAdmissionError as error:
        caught_error = error
        rendered = "".join(traceback.format_exception(error))
    else:
        pytest.fail("unsafe request unexpectedly admitted")

    assert caught_error is not None
    assert caught_error.code == code
    assert caught_error.status_code == 403
    assert caught_error.__cause__ is None
    assert caught_error.__context__ is None
    assert "SECRET_" not in rendered


def test_tool_container_gate_uses_type_identity_without_iterating_untrusted_shape(
    _private_db: Path,
    tmp_path: Path,
) -> None:
    assert _private_db.parent == tmp_path
    snapshot = _verified(_aggregate(_policy(explicit_allowed_tools=("fs_read",))))
    requested = _ChangingToolsContainer()
    _SpoofBuiltinContainerMeta.equality_calls = 0
    _ChangingToolsContainer.iteration_calls = 0

    error = _assert_error(
        snapshot,
        "agent_tools_mismatch",
        403,
        requested_allowed_tools=cast(Any, requested),
    )

    assert str(error) == "requested tools do not match the approved declaration"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "SECRET_" not in "".join(traceback.format_exception(error))
    assert _SpoofBuiltinContainerMeta.equality_calls == 0
    assert _ChangingToolsContainer.iteration_calls == 0


def test_profile_model_wins_but_admission_compares_raw_declared_model() -> None:
    frozen = _policy(
        provider="cursor_cli",
        provider_source="binding",
        explicit_model="ignored-explicit",
        profile={"provider": "cursor_cli", "model": "profile-wins"},
        selected_skill_catalog=None,
        selected_skill_contents=None,
    )
    snapshot = _verified(_aggregate(frozen))

    assert frozen.policy.derived.model == "profile-wins"
    assert frozen.policy.binding.declared_model == "ignored-explicit"
    assert (
        _admit(
            snapshot,
            requested_provider="cursor_cli",
            requested_model="ignored-explicit",
        )
        == frozen.policy
    )
    _assert_error(
        snapshot,
        "agent_model_mismatch",
        403,
        requested_provider="cursor_cli",
        requested_model="profile-wins",
    )


def test_snapshot_policy_reads_do_not_load_live_execution_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = _policy()
    snapshot = _verified(_aggregate(frozen))

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("live loader called")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.launch_policy.resolve_launch_policy", explode
    )
    monkeypatch.setattr("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile", explode)
    monkeypatch.setattr(Path, "read_text", explode)

    assert _admit(snapshot) == frozen.policy
    assert (
        policy_for_binding(
            snapshot,
            target_key="main",
            agent_profile="admission-profile",
            expected_policy_hash=frozen.policy_hash,
        )
        == frozen.policy
    )


@pytest.mark.parametrize(
    ("field", "unsafe"),
    [
        ("target_key", "target\nSECRET_TARGET"),
        ("agent_profile", "x" * 500 + "SECRET_PROFILE"),
        ("requested_provider", "provider\nSECRET_PROVIDER"),
        ("requested_model", "model\nSECRET_MODEL"),
        ("requested_allowed_tools", ["tool\nSECRET_TOOL"]),
    ],
)
def test_unsafe_requested_values_never_appear_in_error_or_traceback(
    field: str, unsafe: object
) -> None:
    snapshot = _verified(_aggregate(_policy()))
    try:
        _admit(snapshot, **{field: unsafe})
    except AgentAdmissionError as exc:
        message = str(exc)
        rendered = "".join(traceback.format_exception(exc))
    else:
        pytest.fail("unsafe request unexpectedly admitted")

    assert "SECRET_" not in rendered
    if field == "requested_provider":
        assert message == "requested provider is not a recognised provider id"


def test_policy_for_binding_reads_each_valid_pair_from_real_verified_store() -> None:
    first = _policy(target_key="alpha")
    second = _policy(target_key="beta")
    snapshot = _verified(_aggregate(first, second))

    first_read = policy_for_binding(
        snapshot,
        target_key="alpha",
        agent_profile="admission-profile",
        expected_policy_hash=first.policy_hash,
    )
    second_read = policy_for_binding(
        snapshot,
        target_key="beta",
        agent_profile="admission-profile",
        expected_policy_hash=second.policy_hash,
    )

    assert first_read == first.policy
    assert second_read == second.policy


def test_policy_for_binding_external_hash_is_authoritative_over_valid_entry_hash() -> None:
    frozen = _policy()
    snapshot = _verified(_aggregate(frozen))

    with pytest.raises(LaunchAdmissionError) as caught:
        policy_for_binding(
            snapshot,
            target_key="main",
            agent_profile="admission-profile",
            expected_policy_hash="0" * 64,
        )

    assert caught.value.code == "policy_hash_mismatch"
    assert caught.value.status_code == 409
    assert caught.value.retryable is False
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_policy_for_binding_refuses_bad_nonselected_aggregate_entry() -> None:
    selected = _policy(target_key="alpha")
    other = _policy(target_key="beta")
    aggregate = _aggregate(selected, other)
    entries = cast(list[dict[str, object]], aggregate["policies"])
    entries[1]["policy_hash"] = "0" * 64
    snapshot = _verified(aggregate)

    with pytest.raises(AgentAdmissionError) as caught:
        policy_for_binding(
            snapshot,
            target_key="alpha",
            agent_profile="admission-profile",
            expected_policy_hash=selected.policy_hash,
        )

    assert type(caught.value) is AgentAdmissionError
    assert caught.value.code == "agent_policy_unavailable"
    assert caught.value.status_code == 409


def test_policy_for_binding_missing_pair_uses_launch_refusal() -> None:
    frozen = _policy(target_key="other")
    snapshot = _verified(_aggregate(frozen))

    with pytest.raises(LaunchAdmissionError) as caught:
        policy_for_binding(
            snapshot,
            target_key="main",
            agent_profile="admission-profile",
            expected_policy_hash=frozen.policy_hash,
        )

    assert caught.value.code == "frozen_policy_unavailable"
    assert caught.value.status_code == 409
    assert caught.value.retryable is False


def test_policy_for_binding_duplicate_pair_remains_aggregate_unavailable() -> None:
    frozen = _policy()
    aggregate = _aggregate(frozen)
    entries = cast(list[dict[str, object]], aggregate["policies"])
    entries.append(dict(entries[0]))
    snapshot = _verified(aggregate)

    with pytest.raises(AgentAdmissionError) as caught:
        policy_for_binding(
            snapshot,
            target_key="main",
            agent_profile="admission-profile",
            expected_policy_hash=frozen.policy_hash,
        )

    assert type(caught.value) is AgentAdmissionError
    assert caught.value.code == "agent_policy_unavailable"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"target_key": "bad\nlabel"}, "frozen_policy_unavailable"),
        ({"target_key": _HostileString("main")}, "frozen_policy_unavailable"),
        ({"agent_profile": _HostileValue()}, "frozen_policy_unavailable"),
        ({"expected_policy_hash": ""}, "policy_hash_mismatch"),
        ({"expected_policy_hash": _HostileString("0" * 64)}, "policy_hash_mismatch"),
        ({"expected_policy_hash": _HostileValue()}, "policy_hash_mismatch"),
    ],
)
def test_policy_for_binding_rejects_untrusted_inputs_without_callbacks(
    changes: dict[str, object],
    code: str,
) -> None:
    frozen = _policy()
    snapshot = _verified(_aggregate(frozen))
    values: dict[str, object] = {
        "target_key": "main",
        "agent_profile": "admission-profile",
        "expected_policy_hash": frozen.policy_hash,
    }
    values.update(changes)

    with pytest.raises(LaunchAdmissionError) as caught:
        policy_for_binding(snapshot, **cast(Any, values))

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.code == code
    assert caught.value.status_code == 409
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "SECRET_" not in rendered


def test_policy_for_binding_returns_fresh_typed_deep_frozen_policies() -> None:
    frozen = _deep_policy()
    snapshot = _verified(_aggregate(frozen))

    first = policy_for_binding(
        snapshot,
        target_key="main",
        agent_profile="admission-profile",
        expected_policy_hash=frozen.policy_hash,
    )
    second = policy_for_binding(
        snapshot,
        target_key="main",
        agent_profile="admission-profile",
        expected_policy_hash=frozen.policy_hash,
    )

    assert isinstance(first, EffectiveLaunchPolicy)
    assert isinstance(first.binding, EffectiveAgentBinding)
    assert isinstance(first.derived, EffectiveDerivedPolicy)
    assert is_deep_frozen(first.profile)
    assert is_deep_frozen(first.derived.mcp_configuration)
    assert first is not second
    assert first.profile is not second.profile
    assert first.derived.mcp_configuration is not second.derived.mcp_configuration
