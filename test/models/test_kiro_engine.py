"""Unit coverage for Kiro engine selection and resolution (#1167)."""

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import (
    KiroEngine,
    canonical_kiro_engine,
    parse_kiro_engine,
    resolve_kiro_engine,
)


@pytest.mark.parametrize(
    ("explicit", "profile", "expected"),
    [
        (None, None, KiroEngine.V2),
        ("v2", None, KiroEngine.V2),
        (None, "kas", KiroEngine.KAS),
        ("kas", "kas", KiroEngine.KAS),
    ],
)
def test_resolve_kiro_engine_uses_creation_precedence(explicit, profile, expected):
    assert resolve_kiro_engine(explicit=explicit, profile=profile) == expected


def test_resolve_kiro_engine_rejects_conflicting_creation_values():
    with pytest.raises(ValueError, match="conflict"):
        resolve_kiro_engine(explicit="v2", profile="kas")


def test_persisted_engine_is_authoritative_during_restore():
    assert resolve_kiro_engine(explicit="v2", profile="v2", persisted="kas") == KiroEngine.KAS


def test_agent_profile_rejects_unknown_engine_before_creation():
    with pytest.raises(ValidationError, match="engine"):
        AgentProfile(name="bad", description="bad", engine="v4")


# ---------------------------------------------------------------------------
# #1167 Phase 1 (D1): ``v3`` is canonical and ``kas`` is an input alias
# stored as ``v3``. Provisional names and messages, per the issue.
# ---------------------------------------------------------------------------


def test_kas_is_a_python_alias_of_the_one_v3_member():
    assert KiroEngine.KAS is KiroEngine.V3
    assert [member.value for member in KiroEngine] == ["v2", "v3"]


@pytest.mark.parametrize("spelling", ["v3", "kas", KiroEngine.V3, KiroEngine.KAS])
def test_every_v3_spelling_parses_to_the_v3_member(spelling):
    assert parse_kiro_engine(spelling) is KiroEngine.V3
    assert KiroEngine(spelling) is KiroEngine.V3


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("v2", "v2"),
        ("v3", "v3"),
        ("kas", "v3"),
        (KiroEngine.V2, "v2"),
        (KiroEngine.KAS, "v3"),
        # Pure: an invalid value is returned unchanged, never rejected.
        ("bogus", "bogus"),
        ("KAS", "KAS"),
    ],
)
def test_canonical_kiro_engine_maps_without_validating(value, expected):
    assert canonical_kiro_engine(value) == expected


@pytest.mark.parametrize("value", ["v4", "KAS", "V3", "kas ", ""])
def test_parse_rejects_values_outside_the_canonical_set_and_alias(value):
    with pytest.raises(ValueError, match="expected one of: v2, v3"):
        parse_kiro_engine(value)


def test_resolution_treats_kas_and_v3_as_one_engine():
    assert resolve_kiro_engine(explicit="kas", profile="v3") is KiroEngine.V3
    assert resolve_kiro_engine(explicit="v3", profile="kas") is KiroEngine.V3
    assert resolve_kiro_engine(persisted="kas") is KiroEngine.V3
    # The default stays v2, and nothing else is inherited.
    assert resolve_kiro_engine() is KiroEngine.V2


def test_conflict_message_names_the_canonical_spellings():
    with pytest.raises(ValueError) as excinfo:
        resolve_kiro_engine(explicit="v2", profile="kas")
    assert str(excinfo.value) == (
        "Kiro engine conflict: explicit selection 'v2' differs from selected profile 'v3'"
    )


@pytest.mark.parametrize("spelling", ["v3", "kas"])
def test_every_typed_model_field_accepts_kas_and_stores_v3(spelling):
    from cli_agent_orchestrator.api.main import RunStepRequest
    from cli_agent_orchestrator.models.flow import Flow
    from cli_agent_orchestrator.models.terminal import Terminal
    from cli_agent_orchestrator.models.workflow import WorkflowStep

    models = [
        AgentProfile(name="p", description="p", engine=spelling),
        Flow(
            name="f",
            file_path="/f.md",
            schedule="* * * * *",
            agent_profile="developer",
            engine=spelling,
        ),
        WorkflowStep(id="s", provider="kiro_cli", agent="dev", prompt="go", engine=spelling),
        Terminal(
            id="abcd1234",
            name="w",
            session_name="s",
            provider="kiro_cli",
            agent_profile="developer",
            engine=spelling,
        ),
        RunStepRequest(provider="kiro_cli", agent="dev", prompt="go", engine=spelling),
    ]
    for model in models:
        # ``==``: Terminal stores enum values (a str equal to the member).
        assert model.engine == KiroEngine.V3, type(model).__name__
        assert model.model_dump(mode="json")["engine"] == "v3", type(model).__name__
        reparsed = type(model).model_validate_json(
            model.model_dump_json().replace('"engine":"v3"', f'"engine":"{spelling}"')
        )
        assert reparsed.engine == KiroEngine.V3, type(model).__name__


def test_json_schema_lists_each_engine_value_once():
    schema = AgentProfile.model_json_schema()
    assert schema["$defs"]["KiroEngine"]["enum"] == ["v2", "v3"]
