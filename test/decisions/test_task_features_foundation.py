"""#1199: the four task-feature points, and the single task.features switch that controls them."""

import json

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.decisions import settings
from cli_agent_orchestrator.decisions.types import (
    FEATURE_POINTS,
    FEATURES_SWITCH,
    POINTS,
    ROUTE_POINTS,
    ROUTING_FEATURES,
    SWITCHES,
    DecisionAnswer,
    LaunchPlan,
)
from cli_agent_orchestrator.services import settings_service

# The names and wire order the VT1 packages declare (jev e2202cc6, jevk5 eb1c60d3, mapping.py).
VT1_FEATURE_POINTS = (
    "task.long_context",
    "task.deep_reasoning",
    "task.read_only",
    "task.needs_network",
)


@pytest.fixture
def egress(monkeypatch, tmp_path):
    from cli_agent_orchestrator.decisions import egress

    monkeypatch.setattr(egress, "HALT_FILE", tmp_path / "decisions.halt")
    monkeypatch.setattr(egress, "EGRESS_LOCK_FILE", tmp_path / "decisions-egress.lock")
    monkeypatch.setattr(egress, "_settings_loader", settings.load_settings)
    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", tmp_path)
    for key in settings.STATE_ENV.values():
        monkeypatch.delenv(key, raising=False)
    return egress


def test_the_feature_points_match_the_vt1_packages_and_answer_yes_or_no():
    assert FEATURE_POINTS == VT1_FEATURE_POINTS
    for point in FEATURE_POINTS:
        assert POINTS[point].options == ("yes", "no")
        assert POINTS[point].question.endswith("?")
    assert ROUTING_FEATURES == ("long_context", "deep_reasoning", "read_only")
    assert set(POINTS) == set(ROUTE_POINTS) | set(FEATURE_POINTS)


def test_one_switch_controls_the_features_and_no_feature_point_is_a_switch():
    assert FEATURES_SWITCH == "task.features"
    assert SWITCHES == ("model.route", "effort.route", "provider.route", "task.features")
    assert not set(FEATURE_POINTS) & set(SWITCHES)
    assert set(settings.STATE_ENV) == set(SWITCHES)
    assert settings.STATE_ENV["task.features"] == "CAO_DECISION_TASK_FEATURES"
    assert set(settings.DecisionSettings().points) == set(SWITCHES)


def test_the_switch_is_off_by_default_and_loads_its_state_and_decider(egress):
    assert settings.load_settings().points["task.features"].state.value == "off"
    settings.set_point("task.features", "shadow", "jev")
    loaded = settings.load_settings().points["task.features"]
    assert (loaded.state.value, loaded.decider) == ("shadow", "jev")
    # A feature point under decisions.points is not a switch, so it is ignored.
    raw = settings_service._load_or_raise()
    raw["decisions"]["points"]["task.long_context"] = {"state": "on", "decider": "jev"}
    settings_service._save(raw)
    assert "task.long_context" not in settings.load_settings().points


def test_the_switch_reads_its_environment_override(egress, monkeypatch):
    monkeypatch.setenv("CAO_DECISION_TASK_FEATURES", "on")
    assert settings.load_settings().points["task.features"].state.value == "on"
    settings.apply_flags(["task.features=shadow"])
    assert settings.load_settings().points["task.features"].state.value == "shadow"
    with pytest.raises(ValueError):
        settings.apply_flags(["task.long_context=on"])


@pytest.mark.parametrize("point", VT1_FEATURE_POINTS)
def test_a_single_feature_point_cannot_be_set(egress, point):
    with pytest.raises(ValueError):
        settings.set_point(point, "on", "jev")
    with pytest.raises(ValueError):
        settings.set_table(point, "yes", profile="worker")


def test_the_halt_turns_the_switch_off(egress):
    settings.set_point("task.features", "on", "jev")
    egress.HALT_FILE.touch()
    assert settings.load_settings().points["task.features"].state.value == "off"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,binding,point,expected",
    [
        ("on", "jev", "task.features", True),
        ("shadow", "jev", "task.features", True),
        ("off", "jev", "task.features", False),
        ("on", "jevk5", "task.features", False),
        # The packages ask the permit with the switch, never with one feature's name.
        ("on", "jev", "task.long_context", False),
    ],
)
async def test_the_egress_permit_honours_the_switch(egress, state, binding, point, expected):
    settings.set_point("task.features", state, binding)
    async with egress.egress_permit(point, "jev") as allowed:
        assert allowed is expected


def test_off_turns_the_switch_off_too(egress, monkeypatch):
    from cli_agent_orchestrator.cli.commands.decisions import decisions

    settings.set_point("task.features", "on", "jev")
    monkeypatch.setenv("CAO_DECISION_TASK_FEATURES", "on")
    result = CliRunner().invoke(decisions, ["off"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["points"]["task.features"]["state"] == "off"
    raw = settings_service._load_or_raise()
    assert raw["decisions"]["points"]["task.features"] == {"state": "off", "decider": "jev"}


def test_the_cli_sets_the_switch_and_lists_feature_records(egress, monkeypatch):
    from cli_agent_orchestrator.cli.commands.decisions import decisions
    from cli_agent_orchestrator.decisions import operator

    seen = []
    monkeypatch.setattr(operator, "records", lambda **kwargs: seen.append(kwargs) or [])
    runner = CliRunner()
    result = runner.invoke(decisions, ["set", "task.features", "shadow", "--decider", "jev"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["points"]["task.features"]["decider"] == "jev"
    assert runner.invoke(decisions, ["set", "task.long_context", "on"]).exit_code == 2
    assert (
        runner.invoke(decisions, ["table", "task.features", "--profile", "w", "yes"]).exit_code == 2
    )
    result = runner.invoke(decisions, ["list", "--point", "task.read_only"])
    assert result.exit_code == 0, result.output
    assert seen[-1]["point"] == "task.read_only"


def test_the_fixed_table_never_declares_a_feature():
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider

    assert FixedTableDecider.points == frozenset(ROUTE_POINTS)


class _Declares:
    version = "1"

    def __init__(self, name, points):
        self.name, self.points = name, frozenset(points)

    async def decide(self, request, config):
        return None


def test_the_registry_serves_a_group_only_to_a_decider_declaring_all_of_it():
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry

    full = _Declares("full", ("model.route",) + VT1_FEATURE_POINTS)
    three = _Declares("three", ("model.route",) + VT1_FEATURE_POINTS[:3])
    registry = DeciderRegistry({"full": lambda: full, "three": lambda: three})
    assert registry.get_all("full", FEATURE_POINTS) is full
    assert registry.get_all("three", FEATURE_POINTS) is None
    assert registry.get_all("full", ()) is None
    # A three-feature decider still serves its own points one at a time.
    assert registry.get("three", "task.long_context") is three
    # The registry still refuses a decider that declares a point core doesn't know (C8).
    odd = _Declares("odd", ("task.writes_code",))
    assert DeciderRegistry({"odd": lambda: odd}).get("odd", "task.writes_code") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("point", VT1_FEATURE_POINTS)
async def test_a_feature_point_is_never_a_launch_field(point):
    from cli_agent_orchestrator.decisions.engine import DecisionEngine, DelegationRequest
    from cli_agent_orchestrator.decisions.policy import DecisionInputError
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import DecisionSettings

    engine = DecisionEngine(
        DeciderRegistry({}), None, None, settings_loader=lambda: DecisionSettings()
    )
    from cli_agent_orchestrator.decisions.targets import TargetKind

    req = DelegationRequest(
        "assign",
        "claude_code",
        "Ramones-log_triage-3f9a",
        None,
        "do it",
        field_states={point: "auto"},
        target_kind=TargetKind.EPHEMERAL,
    )
    with pytest.raises(DecisionInputError) as error:
        await engine.prepare_launch(req)
    assert str(error.value) == "unknown decision point"


def test_a_plan_has_no_hint_by_default():
    assert LaunchPlan().hints == ()
    assert LaunchPlan("m", (1,), "high", "codex") == LaunchPlan("m", (1,), "high", "codex", ())


def test_a_feature_answer_has_the_ordinary_answer_shape():
    answer = DecisionAnswer("yes", {"yes": 0.9, "no": 0.05, "unsure": 0.05})
    assert answer.option in POINTS["task.read_only"].options
