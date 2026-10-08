import asyncio
import json
import os
import re
import sys
import time
from dataclasses import replace
from test.decisions.test_engine import setup_engine
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
    for key in (
        "CAO_DECISION_MODEL_ROUTE",
        "CAO_DECISION_EFFORT_ROUTE",
        "CAO_DECISION_ON_TIMEOUT_MS",
        "CAO_DECISION_CONFIDENCE_THRESHOLD",
    ):
        monkeypatch.delenv(key, raising=False)


def row_values(state="on", decision_status="done"):
    return dict(
        point="model.route",
        state=state,
        kind="assign",
        provider="codex",
        fallback_source="none",
        outcome="fallback",
        decision_status=decision_status,
    )


@pytest.mark.parametrize(
    "provider,config",
    [
        ("codex", {"codexConfig": {"model_reasoning_effort": "high"}}),
        ("claude_code", {"claudeConfig": {"effort": "max"}}),
    ],
)
def test_effort_uses_policy_not_provider_config(provider, config):
    from cli_agent_orchestrator.decisions.policy import (
        IDENTITY_POLICY,
        PolicyBounds,
        resolve_fallback,
    )
    from cli_agent_orchestrator.decisions.types import Fallback
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(name="worker", description="worker", **config)
    assert resolve_fallback(
        "effort.route",
        profile,
        PolicyBounds(default_effort="low", max_effort="medium"),
        {},
        provider,
    ) == Fallback("low", "policy")
    assert resolve_fallback("effort.route", profile, IDENTITY_POLICY, {}, provider) == Fallback(
        None, "none"
    )


def test_rejected_store_enforces_finality(setup_engine):
    _, _, _, store, events, _ = setup_engine
    record = store.insert(
        {
            **row_values(),
            "outcome": "rejected",
            "terminal_id": "ignored",
            "decision_status": "pending",
        },
        "message",
    )
    store.bind((record,), launch_status="launched")
    store.sweep()
    result = store.get(record)
    assert (
        result["launch_status"] == "not_launched"
        and result["decision_status"] == "done"
        and result["terminal_id"] is None
    )
    assert len(events) == 1


def test_sweep_emits_once_per_changed_row(setup_engine):
    _, _, _, store, events, _ = setup_engine
    on = store.insert(row_values(), "on")
    shadow = store.insert(row_values("shadow", "pending"), "shadow")
    store.sweep()
    assert {event["id"] for event in events} == {on, shadow} and len(events) == 2
    assert store.get(shadow)["decision_status"] == "interrupted"
    assert store.get(on)["launch_status"] == "unknown"
    store.sweep()
    store.bind((on, shadow), launch_status="not_launched")
    assert len(events) == 2


def test_key_cache_refresh_has_one_key_open(tmp_path, monkeypatch):
    from pathlib import Path

    from cli_agent_orchestrator.decisions.hashing import KeyCache, load_key

    path = tmp_path / "decision-hash.key"
    load_key(path)
    original = Path.open
    opened = []

    def observed(self, *args, **kwargs):
        if self == path:
            opened.append(1)
            if len(opened) > 1:
                raise FileNotFoundError("rotation between opens")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed)
    assert len(KeyCache(path).digest("message")[0]) == 64
    assert len(opened) == 1


def test_loose_key_file_is_restricted_on_read(tmp_path):
    import stat

    from cli_agent_orchestrator.decisions.hashing import KeyCache

    path = tmp_path / "decision-hash.key"
    path.write_bytes(b"k" * 32)
    path.chmod(0o644)
    expected = KeyCache(path).digest("message")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert KeyCache(path).digest("message") == expected


def test_owner_only_key_file_is_not_rechmodded(tmp_path, monkeypatch):
    from cli_agent_orchestrator.decisions.hashing import KeyCache, load_key

    path = tmp_path / "decision-hash.key"
    load_key(path)
    monkeypatch.setattr(os, "fchmod", Mock(side_effect=AssertionError("chmod")))
    assert len(KeyCache(path).digest("message")[0]) == 64


def test_key_file_that_cannot_be_restricted_is_not_used(setup_engine, tmp_path, monkeypatch):
    from cli_agent_orchestrator.decisions.hashing import KeyCache

    path = tmp_path / "loose.key"
    path.write_bytes(b"k" * 32)
    path.chmod(0o644)
    monkeypatch.setattr(os, "fchmod", Mock(side_effect=PermissionError("not owner")))
    with pytest.raises(PermissionError):
        KeyCache(path).digest("message")
    store = setup_engine[3]
    store._key = KeyCache(path)
    assert store.insert(row_values(), "message") is None
    assert store.list() == []


def test_key_cleanup_ignores_active_temp(tmp_path):
    from cli_agent_orchestrator.decisions.hashing import cleanup_temps

    key = tmp_path / "decision-hash.key"
    fresh = tmp_path / ".decision-hash-fresh"
    stale = tmp_path / ".decision-hash-stale"
    fresh.write_bytes(b"x")
    stale.write_bytes(b"x")
    old = time.time() - 120
    os.utime(stale, (old, old))
    cleanup_temps(key)
    assert fresh.exists() and not stale.exists()


def test_key_publication_retries_disappeared_temp(tmp_path, monkeypatch):
    from cli_agent_orchestrator.decisions.hashing import load_key

    original = os.link
    calls = []

    def link(source, target):
        calls.append(1)
        if len(calls) == 1:
            raise FileNotFoundError("creator temp disappeared")
        original(source, target)

    monkeypatch.setattr(os, "link", link)
    key = tmp_path / "decision-hash.key"
    assert len(load_key(key)) == 32 and len(calls) == 2
    assert not list(tmp_path.glob(".decision-hash-*"))


@pytest.mark.parametrize("size", [31, 33])
def test_invalid_key_is_isolated_and_logged(setup_engine, caplog, size):
    _, _, _, store, events, _ = setup_engine
    store.key_path.write_bytes(b"x" * size)
    assert store.insert(row_values(), "sentinel-not-logged") is None
    assert store.list() == [] and events == []
    assert len(caplog.records) == 1
    assert "hash key file" in caplog.text and "decision-hash.key" in caplog.text
    assert "sentinel-not-logged" not in caplog.text and "x" * size not in caplog.text


def test_record_hash_and_utf8_bytes(setup_engine):
    _, _, _, store, _, _ = setup_engine
    record = store.insert(row_values(), "é")
    data = store.get(record)
    assert re.fullmatch("[0-9a-f]{64}", data["message_hash"])
    assert re.fullmatch("[0-9a-f]{8}", data["hash_key_id"])
    assert data["message_bytes"] == 2


def test_empty_provider_validation_and_unset():
    from cli_agent_orchestrator.decisions.policy import PolicyBounds
    from cli_agent_orchestrator.decisions.settings import load_settings, set_tier

    policy = PolicyBounds(default_tier="small", max_tier="medium")
    policy.validate({"codex": {}})
    set_tier("codex", "small", "x")
    set_tier("codex", "small", None)
    assert "codex" not in load_settings().model_tiers


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["off", "shadow", "on"])
@pytest.mark.parametrize(
    "point,value,options",
    [
        ("model.route", "simple", "small, medium, large"),
        ("effort.route", "max", "low, medium, high"),
    ],
)
async def test_malformed_explicit_precedes_policy_without_record(
    setup_engine, state, point, value, options
):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyBounds
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    engine.settings_loader = lambda: replace(
        settings, points={point: PointSettings(PointState(state), decider.name)}
    )
    with pytest.raises(DecisionInputError) as error:
        await engine.prepare_launch(
            replace(
                req,
                target_kind=TargetKind.EPHEMERAL,
                field_states={point: value},
                policy=PolicyBounds(max_tier="medium"),
            )
        )
    assert str(error.value) == f"{point} must be one of {options} (got '{value}')"
    assert store.list() == [] and events == [] and decider.calls == 0


def test_full_same_user_definition():
    from cli_agent_orchestrator.decisions.settings import SAME_USER

    assert "'Operator only' means not settable through MCP or any agent-facing API." in SAME_USER
    assert "not a privilege boundary" in SAME_USER


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [7, None, []])
async def test_invalid_binding_never_uses_builtin(setup_engine, value, caplog):
    from cli_agent_orchestrator.decisions.settings import load_settings
    from cli_agent_orchestrator.services import settings_service

    engine, req, decider, store, _, _ = setup_engine
    settings_service.SETTINGS_FILE.write_text(
        json.dumps({"decisions": {"points": {"model.route": {"state": "on", "decider": value}}}})
    )
    engine.settings_loader = load_settings
    plan = await engine.prepare_launch(req)
    assert store.get(plan.record_ids[0])["reason"] == "decider_unavailable"
    assert decider.calls == 0 and "Invalid decider name" in caplog.text


@pytest.mark.parametrize(
    "data,action,label",
    [
        ({"decisions": []}, "point", "decisions"),
        ({"model_tiers": []}, "tier", "model_tiers"),
        ({"decisions": {"deciders": []}}, "table", "decisions.deciders"),
        (
            {"decisions": {"points": {"model.route": {"exclude_profiles": "worker"}}}},
            "exclude",
            "decisions.points.model.route.exclude_profiles",
        ),
        ({"decisions": []}, "tune", "decisions"),
    ],
)
def test_malformed_setters_preserve_file(data, action, label):
    from cli_agent_orchestrator.decisions import settings
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text(json.dumps(data))
    original = settings_service.SETTINGS_FILE.read_bytes()
    actions = {
        "point": lambda: settings.set_point("model.route", "on"),
        "tier": lambda: settings.set_tier("codex", "small", "x"),
        "table": lambda: settings.set_table("model.route", "small", profile="worker"),
        "exclude": lambda: settings.set_exclusions(add="worker"),
        "tune": lambda: settings.tune(threshold=0.9),
    }
    with pytest.raises(ValueError, match=re.escape(f'settings.json: "{label}" must be')):
        actions[action]()
    assert settings_service.SETTINGS_FILE.read_bytes() == original


def test_malformed_block_cli_and_ops_errors():
    from cli_agent_orchestrator.cli.commands.decisions import decisions
    from cli_agent_orchestrator.ops_mcp_server.decision_tools import register_decision_tools
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text('{"model_tiers": []}')
    result = CliRunner().invoke(decisions, ["tier", "codex", "small", "x"])
    assert (
        result.exit_code != 0
        and "must be an object" in result.output
        and "Traceback" not in result.output
    )

    class Surface:
        def __init__(self):
            self.tools = {}

        def tool(self, fn):
            self.tools[fn.__name__] = fn
            return fn

    surface = Surface()
    register_decision_tools(surface)
    result = surface.tools["decisions_set_tier"]("codex", "small", "x")
    assert result["success"] is False and "must be an object" in result["message"]


@pytest.mark.parametrize("value", [True, False, float("nan"), "bad", None])
def test_invalid_numeric_uses_defaults(value, caplog):
    from cli_agent_orchestrator.decisions.settings import load_settings
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "decisions": {
                    "on_timeout_ms": value,
                    "confidence_threshold": value,
                    "retention_days": value,
                }
            }
        )
    )
    settings = load_settings()
    assert (settings.on_timeout_ms, settings.confidence_threshold, settings.retention_days) == (
        1000,
        0.7,
        90,
    )
    assert "using default" in caplog.text


def test_store_purge_requires_explicit_scope(setup_engine):
    _, _, _, store, _, _ = setup_engine
    store.insert(row_values(), "message")
    with pytest.raises(ValueError, match="exactly one"):
        store.purge()
    with pytest.raises(ValueError, match="exactly one"):
        store.purge(before=__import__("datetime").datetime.now(), all_records=True)
    assert len(store.list()) == 1
    assert store.purge(all_records=True) == 1


@pytest.mark.parametrize("order", ["task_first", "bind_first", "suppressed"])
def test_store_emission_orders(setup_engine, order):
    _, _, _, store, events, _ = setup_engine
    record = store.insert(row_values("shadow", "pending"), "message")
    if order != "suppressed":
        store.shadow_started(record)
    if order == "bind_first":
        store.bind((record,), launch_status="launched")
    store.update_decision(
        record, {"decision_status": "done", "reason": "no_answer"}, emit=order != "suppressed"
    )
    store.bind((record,), launch_status="launched")
    store.bind((record,), launch_status="not_launched")
    assert len(events) == 1


def test_policy_checked_set_and_lower_tiers():
    from cli_agent_orchestrator.decisions.policy import (
        IDENTITY_POLICY,
        PolicyBounds,
        PolicyConfigError,
    )

    table = {"codex": {"small": "x", "medium": "m"}, "claude_code": {"large": "l"}}
    PolicyBounds(
        default_tier="small", max_tier="medium", allowed_providers=frozenset({"codex"})
    ).validate(table)
    IDENTITY_POLICY.validate(table)
    IDENTITY_POLICY.validate({"codex": {}})
    with pytest.raises(PolicyConfigError, match=r"model_tiers.codex.small is not mapped"):
        PolicyBounds(default_tier="medium", max_tier="medium").validate({"codex": {"medium": "m"}})


@pytest.mark.parametrize("empty_target", [None, {}], ids=["absent", "empty"])
def test_explicit_allowed_provider_requires_a_mapping(empty_target):
    from cli_agent_orchestrator.decisions.policy import PolicyBounds, PolicyConfigError

    tiers = {"claude_code": {"small": "model-c"}}
    if empty_target is not None:
        tiers["codex"] = empty_target
    policy = PolicyBounds(
        default_tier="small", allowed_providers=frozenset({"claude_code", "codex"})
    )
    with pytest.raises(
        PolicyConfigError, match=r"^model_tiers\.codex\.small is not mapped \(required by policy\)$"
    ) as raised:
        policy.validate(tiers)
    assert raised.value.reason == "policy_invalid"


@pytest.mark.parametrize("empty_target", [None, {}], ids=["absent", "empty"])
def test_unrestricted_provider_validation_keeps_empty_maps_unchanged(empty_target):
    from cli_agent_orchestrator.decisions.policy import IDENTITY_POLICY, PolicyBounds

    tiers = {"claude_code": {"small": "model-c"}}
    if empty_target is not None:
        tiers["codex"] = empty_target
    assert IDENTITY_POLICY.allowed_providers is None
    PolicyBounds(default_tier="small", allowed_providers=None).validate(tiers)
    IDENTITY_POLICY.validate({"codex": {}})


def test_exact_input_error_texts():
    from cli_agent_orchestrator.decisions.policy import (
        PolicyBounds,
        PolicyViolation,
        ProviderNotAllowedError,
        UnmappedTierError,
    )

    policy = PolicyBounds(default_tier="small", max_tier="medium")
    with pytest.raises(PolicyViolation) as error:
        policy.check_explicit("model.route", "large", "codex", {})
    assert str(error.value) == "tier 'large' is above the policy ceiling 'medium' for model.route"
    assert (
        str(ProviderNotAllowedError("codex", frozenset({"kiro_cli", "claude_code"})))
        == "provider 'codex' is not allowed; allowed providers: claude_code, kiro_cli"
    )
    assert (
        str(UnmappedTierError("model.route", "codex", "small", "default_unmapped"))
        == "model_tiers.codex.small is not mapped (policy default tier for model.route)"
    )


def test_cap_controls_and_fallback_sources():
    from cli_agent_orchestrator.decisions.policy import (
        IDENTITY_POLICY,
        PolicyBounds,
        resolve_fallback,
    )
    from cli_agent_orchestrator.decisions.types import Fallback
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    policy = PolicyBounds(default_tier="small", max_tier="medium")
    assert policy.cap("model.route", "small") == ("small", False)
    assert policy.cap("model.route", "medium") == ("medium", False)
    assert IDENTITY_POLICY.cap("model.route", "large") == ("large", False)
    profile = AgentProfile(name="worker", description="worker", model="profile-model")
    assert resolve_fallback("model.route", profile, policy, {}, "codex") == Fallback(
        "profile-model", "profile"
    )
    assert resolve_fallback(
        "model.route", None, policy, {"small": "mapped-model"}, "codex"
    ) == Fallback("mapped-model", "policy")


def test_settings_layers_limits_and_flags(monkeypatch):
    from cli_agent_orchestrator.decisions.settings import apply_flags, load_settings, tune
    from cli_agent_orchestrator.services import settings_service

    settings = load_settings()
    assert settings.on_timeout_ms == 1000 and settings.retention_days == 90
    assert all(point.state.value == "off" for point in settings.points.values())
    settings_service.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "decisions": {
                    "points": {"model.route": {"state": "invalid"}},
                    "on_timeout_ms": 100000,
                    "confidence_threshold": -1,
                    "shadow": {"max_concurrent": 0, "max_pending": -1, "timeout_ms": 1},
                }
            }
        )
    )
    settings = load_settings()
    assert settings.points["model.route"].state.value == "off"
    assert settings.on_timeout_ms == 5000 and settings.confidence_threshold == 0
    assert (settings.max_concurrent, settings.max_pending, settings.shadow_timeout_ms) == (1, 0, 50)
    monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "on")
    assert (
        load_settings(environment={"CAO_DECISION_MODEL_ROUTE": "invalid"})
        .points["model.route"]
        .state.value
        == "off"
    )
    monkeypatch.setenv("CAO_DECISION_ON_TIMEOUT_MS", "123")
    monkeypatch.setenv("CAO_DECISION_CONFIDENCE_THRESHOLD", ".83")
    monkeypatch.setenv("CAO_DECISION_EFFORT_ROUTE", "shadow")
    settings = load_settings()
    assert settings.on_timeout_ms == 123 and settings.confidence_threshold == 0.83
    assert settings.points["effort.route"].state.value == "shadow"
    apply_flags(["model.route=shadow", "effort.route=on"])
    assert load_settings().points["effort.route"].state.value == "on"
    for value in ("model.route", "unknown=on"):
        with pytest.raises(ValueError, match="decision flag must"):
            apply_flags([value])
    tune(retention_days=30)
    assert load_settings().retention_days == 30


@pytest.mark.parametrize(
    "kwargs",
    [
        {"on_timeout_ms": 49},
        {"on_timeout_ms": 5001},
        {"on_timeout_ms": 60.5},
        {"threshold": -0.01},
        {"threshold": 1.01},
        {"threshold": float("nan")},
        {"threshold": float("inf")},
        {"retention_days": 0},
        {"retention_days": -5},
        {"retention_days": 36501},
        {"retention_days": True},
        {"on_timeout_ms": 1000, "retention_days": 0},
    ],
)
def test_tune_refuses_out_of_range_and_keeps_file(kwargs):
    from cli_agent_orchestrator.decisions.settings import load_settings, tune
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text('{"decisions": {"retention_days": 30}}')
    before = settings_service.SETTINGS_FILE.read_bytes()
    with pytest.raises(ValueError, match="must be between"):
        tune(**kwargs)
    assert settings_service.SETTINGS_FILE.read_bytes() == before
    assert load_settings().retention_days == 30


def test_tune_accepts_range_bounds():
    from cli_agent_orchestrator.decisions.settings import load_settings, tune
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text("{}")
    tune(on_timeout_ms=50, threshold=0, retention_days=1)
    settings = load_settings()
    assert (settings.on_timeout_ms, settings.confidence_threshold, settings.retention_days) == (
        50,
        0,
        1,
    )
    tune(on_timeout_ms=5000, threshold=1.0, retention_days=36500)
    settings = load_settings()
    assert (settings.on_timeout_ms, settings.confidence_threshold, settings.retention_days) == (
        5000,
        1,
        36500,
    )


@pytest.mark.parametrize(
    "args, message",
    [
        (["--retention-days", "0"], "1<=x<=36500"),
        (["--retention-days", "-5"], "1<=x<=36500"),
        (["--retention-days", "36501"], "1<=x<=36500"),
        (["--on-timeout-ms", "49"], "50<=x<=5000"),
        (["--threshold", "1.5"], "0.0<=x<=1.0"),
    ],
)
def test_cli_tune_refuses_out_of_range(args, message):
    from cli_agent_orchestrator.cli.commands.decisions import decisions
    from cli_agent_orchestrator.services import settings_service

    result = CliRunner().invoke(decisions, ["tune", *args])
    assert result.exit_code == 2 and message in result.output
    assert not settings_service.SETTINGS_FILE.exists()


def test_cli_tune_refuses_non_finite_threshold():
    from cli_agent_orchestrator.cli.commands.decisions import decisions
    from cli_agent_orchestrator.services import settings_service

    result = CliRunner().invoke(decisions, ["tune", "--threshold", "nan"])
    assert result.exit_code == 1 and "threshold must be between 0 and 1" in result.output
    assert "Traceback" not in result.output and not settings_service.SETTINGS_FILE.exists()


@pytest.mark.parametrize(
    "key, value, attribute, expected",
    [
        ("retention_days", 0, "retention_days", 1),
        ("retention_days", -5, "retention_days", 1),
        ("on_timeout_ms", 1, "on_timeout_ms", 50),
        ("confidence_threshold", 9, "confidence_threshold", 1),
    ],
)
def test_hand_edited_out_of_range_is_clamped_with_warning(key, value, attribute, expected, caplog):
    from cli_agent_orchestrator.decisions.settings import load_settings
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text(json.dumps({"decisions": {key: value}}))
    assert getattr(load_settings(), attribute) == expected
    assert "out of range; clamped" in caplog.text


def test_in_range_settings_do_not_warn(caplog):
    from cli_agent_orchestrator.decisions.settings import load_settings
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text(
        json.dumps({"decisions": {"retention_days": 1, "on_timeout_ms": 5000}})
    )
    load_settings()
    assert "clamped" not in caplog.text


@pytest.mark.parametrize(
    "values",
    [
        ["model.route=onn"],
        ["model.route="],
        ["model.route=ON"],
        ["model.route=shadow", "effort.route=bad"],
    ],
)
def test_decision_flag_refuses_unknown_state_before_applying(values, monkeypatch):
    from cli_agent_orchestrator.decisions.settings import STATE_ENV, apply_flags

    with pytest.raises(ValueError, match=r"decision flag must be <point>=off\|shadow\|on"):
        apply_flags(values)
    assert all(name not in os.environ for name in STATE_ENV.values())


def test_server_rejects_invalid_decision_flag(monkeypatch, capsys):
    from cli_agent_orchestrator.api import main as api_main

    monkeypatch.setattr(sys, "argv", ["cao-server", "--decision", "model.route=onn"])
    with pytest.raises(SystemExit) as raised:
        api_main.main()
    assert raised.value.code == 2
    assert "decision flag must be <point>=off|shadow|on" in capsys.readouterr().err
    assert "CAO_DECISION_MODEL_ROUTE" not in os.environ


def test_model_tier_malformed_and_empty_maps(caplog):
    from cli_agent_orchestrator.services.model_tiers import load_model_tiers

    assert load_model_tiers({"model_tiers": {"codex": []}}) == {}
    assert load_model_tiers({"model_tiers": {"codex": {"small": "bad model"}}}) == {"codex": {}}
    assert caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["name", "version", "points", "unsupported"])
async def test_registry_declarations_are_cached(defect, caplog):
    from test.fixtures.decision_conformance import ConfidentDecider

    from cli_agent_orchestrator.decisions.registry import DeciderRegistry

    decider = ConfidentDecider()
    decider.name = "wrong" if defect == "name" else "fixture"
    decider.version = 7 if defect == "version" else "1"
    decider.points = (
        ["model.route"]
        if defect == "points"
        else frozenset({"effort.route"}) if defect == "unsupported" else frozenset({"model.route"})
    )
    factory = Mock(return_value=decider)
    registry = DeciderRegistry({"fixture": factory})
    assert registry.get("fixture", "model.route") is None
    assert registry.get("fixture", "model.route") is None
    assert factory.call_count == 1
    assert len(caplog.records) == (0 if defect == "unsupported" else 1)
    await registry.close()


@pytest.mark.asyncio
async def test_registry_metadata_lazy_duplicate_warning(monkeypatch, caplog):
    from cli_agent_orchestrator.decisions import registry as module
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider

    first = Mock(return_value=FixedTableDecider)
    second = Mock(return_value=FixedTableDecider)
    monkeypatch.setattr(
        module,
        "entry_points",
        lambda **kwargs: [
            SimpleNamespace(name="fixed_table", load=first),
            SimpleNamespace(name="fixed_table", load=second),
        ],
    )
    registry = module.DeciderRegistry()
    assert not first.called and not second.called
    assert "Duplicate decider entry-point name fixed_table" in caplog.text
    assert registry.get("fixed_table", "model.route") is not None
    assert not first.called and second.call_count == 1
    await registry.close()


@pytest.mark.asyncio
async def test_fixed_table_role_and_unsure():
    from cli_agent_orchestrator.decisions.fixed_table import FixedTableDecider
    from cli_agent_orchestrator.decisions.types import DecisionFacts, DecisionRequest

    decider = FixedTableDecider()
    request = DecisionRequest(
        1,
        "request",
        ("model.route",),
        "message",
        None,
        DecisionFacts("codex", "assign", "worker", "developer", 7, False),
    )
    result = await decider.decide(request, {"model.route": {"roles": {"developer": "medium"}}})
    assert result["model.route"].option == "medium"
    result = await decider.decide(request, {"model.route": {"profiles": {"worker": "unsure"}}})
    assert result["model.route"].option == "unsure"


def test_shadow_upper_limits():
    from cli_agent_orchestrator.decisions.settings import load_settings
    from cli_agent_orchestrator.services import settings_service

    settings_service.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "decisions": {
                    "shadow": {"max_concurrent": 99999, "max_pending": 999999, "timeout_ms": 999999}
                }
            }
        )
    )
    settings = load_settings()
    assert (settings.max_concurrent, settings.max_pending, settings.shadow_timeout_ms) == (
        1024,
        100000,
        60000,
    )


def test_telemetry_uses_explicit_attribute_allowlist(monkeypatch):
    from cli_agent_orchestrator.decisions import telemetry

    tracer = Mock()
    meter = Mock()
    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda *args: tracer)
    monkeypatch.setattr(telemetry.metrics, "get_meter", lambda *args: meter)
    tracer.start_as_current_span.return_value.__enter__ = Mock()
    tracer.start_as_current_span.return_value.__exit__ = Mock(return_value=False)
    telemetry.emit_record(
        {
            **row_values(),
            "message_hash": "private-hash",
            "hash_key_id": "private-key",
            "terminal_id": "private-id",
            "probabilities": {"small": 0.9},
            "description": "private-description",
            "unrecognized": "private-data",
        }
    )
    attributes = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert set(attributes) == {"cao.decision.point", "cao.decision.state", "cao.decision.outcome"}
    labels = meter.create_counter.return_value.add.call_args.args[1]
    assert set(labels) == {"cao.decision.point", "cao.decision.state", "cao.decision.outcome"}


def test_completed_shadow_recovery_must_not_export_twice(setup_engine):
    _, _, _, store, events, _ = setup_engine
    record = store.insert(row_values("shadow", "pending"), "message")
    store.shadow_started(record)
    store.update_decision(
        record, {"decision_status": "done", "outcome": "shadow", "latency_ms": 1.0}
    )
    assert len(events) == 1
    assert store.get(record)["launch_status"] == "pending"
    store.sweep()
    assert store.get(record)["launch_status"] == "unknown"
    assert len(events) == 1


def test_deferred_shadow_rows_emit_on_launch_sweep(setup_engine):
    _, _, _, store, events, _ = setup_engine
    records = []
    for reason, status in [
        ("not_honored", "done"),
        ("decider_unavailable", "done"),
        ("shadow_dropped", "dropped"),
        ("error", "done"),
    ]:
        records.append(
            store.insert(
                {**row_values("shadow", status), "outcome": "shadow", "reason": reason}, "message"
            )
        )
    store.sweep()
    assert len(events) == 4 and {row["id"] for row in events} == set(records)
    store.sweep()
    assert len(events) == 4


@pytest.mark.asyncio
async def test_failed_shadow_task_is_not_reexported_by_launch_sweep(setup_engine):
    engine, _, _, store, events, _ = setup_engine
    record = store.insert({**row_values("shadow", "pending"), "outcome": "shadow"}, "message")

    async def failed():
        raise RuntimeError("work failed")

    engine.runner.submit(record, failed)
    await engine.runner.drain()
    assert len(events) == 1 and store.get(record)["latency_ms"] is not None
    store.sweep()
    assert len(events) == 1 and store.get(record)["launch_status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields", [{"effort.route": "max"}, {"model.route": "xlarge"}, {"other.route": "auto"}]
)
async def test_installed_off_malformed_fields_are_untouched(setup_engine, fields):
    from cli_agent_orchestrator.decisions.settings import DecisionSettings
    from cli_agent_orchestrator.decisions.targets import TargetKind

    engine, req, decider, store, events, settings = setup_engine
    engine.settings_loader = DecisionSettings
    engine.registry.get = Mock(side_effect=AssertionError("registry loaded"))
    assert (
        await engine.prepare_launch(
            replace(req, target_kind=TargetKind.INSTALLED, field_states=fields)
        )
        is None
    )
    assert not engine.registry.get.called and decider.calls == 0
    assert store.list() == [] and events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["off", "shadow", "on"])
async def test_unknown_point_refused_before_policy_when_checks_run(setup_engine, state):
    from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyBounds
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState

    engine, req, decider, store, events, settings = setup_engine
    engine.settings_loader = lambda: replace(
        settings,
        points={p: PointSettings(PointState(state), decider.name) for p in settings.points},
    )
    with pytest.raises(DecisionInputError, match="^unknown decision point$"):
        await engine.prepare_launch(
            replace(
                req,
                target_kind=TargetKind.EPHEMERAL,
                field_states={"other.route": "auto", "model.route": "auto"},
                policy=PolicyBounds(max_tier="medium"),
            )
        )
    assert store.list() == [] and events == [] and decider.calls == 0
