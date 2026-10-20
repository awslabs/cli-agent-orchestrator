"""E2 policy contracts use the same snapshot as ephemeral creation and claims."""

from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401
from unittest.mock import Mock

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.model_tiers import load_model_tiers


def test_A_S1_one_snapshot(create_store, monkeypatch):
    service, _, _, _, raw = create_store
    raw["model_tiers"] = {"claude_code": {"small": "model-small"}}
    reader = Mock(return_value=raw)
    monkeypatch.setattr(settings_service, "_load_or_raise", reader)
    settings = service.read_settings()
    assert settings["_model_tiers"] == load_model_tiers(raw)
    reader.assert_called_once()
    reader.reset_mock()
    name = create(create_store)["name"]
    reader.assert_called_once()
    reader.reset_mock()
    claimed = service.claim_ephemeral_agent(name, {}, CALLER)
    reader.assert_called_once()
    service.end_claim(name, claimed["claim_id"])
    reader.reset_mock()
    service.bind_ephemeral_agent(name, "eeeeeeee", CALLER, "claude_code", None, None)
    reader.assert_called_once()
    assert database.get_ephemeral_agent(name)["state"] == "launched"


import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.decisions.policy import PolicyConfigError


def mapped(raw):
    raw["model_tiers"] = {
        p: {t: "private-" + p + "-" + t for t in ("small", "medium", "large")}
        for p in ("claude_code", "codex")
    }


def assert_no_create(env, spec, rule):
    with pytest.raises(env[0].EphemeralPolicyError) as err:
        create(env, spec)
    assert err.value.rule == rule and err.value.status_code == 400
    assert not (env[2] / "ephemeral").exists()
    with env[1]() as db:
        assert db.query(database.EphemeralAgentModel).count() == 0
    return err.value


@pytest.mark.parametrize("key", ["max_tier", "default_tier"])
@pytest.mark.parametrize("value", ["huge", 3, True, ["small"]])
def test_A_V1_closed_sets(create_store, key, value):
    create_store[4]["ephemeral"][key] = value
    err = assert_no_create(create_store, SPEC, "policy_config_error:" + key)
    assert err.detail == "ephemeral." + key + " must be one of small, medium, large"


@pytest.mark.parametrize("default", [None, "large"])
def test_A_V2_relation(create_store, default):
    create_store[4]["ephemeral"].update(max_tier="medium", default_tier=default)
    err = assert_no_create(create_store, SPEC, "policy_config_error:default_tier")
    assert "ephemeral.default_tier" in err.detail and "ephemeral.max_tier" in err.detail


@pytest.mark.parametrize("invalid", [None, "bad id", "x" * 129])
@pytest.mark.parametrize("point", ["default", "ceiling"])
def test_A_V3_A_V4_mapping_coverage(create_store, invalid, point):
    raw = create_store[4]
    mapped(raw)
    raw["ephemeral"].update(allowed_providers=["claude_code", "codex"], default_tier="small")
    tier = "small" if point == "default" else "medium"
    if point == "ceiling":
        raw["ephemeral"]["max_tier"] = "medium"
    raw["model_tiers"]["codex"][tier] = invalid
    err = assert_no_create(create_store, SPEC, "policy_config_error:model_tiers")
    assert "model_tiers.codex." + tier in err.detail
    assert "private-" not in err.message


@pytest.mark.parametrize(
    "default,ceiling,complete",
    itertools.product(
        [None, "small", "medium", "large"], [None, "small", "medium", "large"], [False, True]
    ),
)
def test_A_V5_policy_parity(create_store, default, ceiling, complete):
    raw = create_store[4]
    mapped(raw)
    raw["ephemeral"].update(
        default_tier=default, max_tier=ceiling, allowed_providers=["claude_code", "codex"]
    )
    if not complete:
        del raw["model_tiers"]["codex"]["small"]
    settings = create_store[0].read_settings()
    bounds = create_store[0].policy_bounds(settings)
    try:
        bounds.validate(settings["_model_tiers"])
    except PolicyConfigError:
        with pytest.raises(create_store[0].EphemeralPolicyError):
            create_store[0].validate_block(settings)
    else:
        create_store[0].validate_block(settings)


def test_A_R5_A_R6_block_error_reverts_claim(create_store):
    service, _, home, _, raw = create_store
    name = create(create_store)["name"]
    raw["ephemeral"]["default_tier"] = "small"
    with pytest.raises(service.EphemeralPolicyError) as err:
        service.claim_ephemeral_agent(name, {"idempotency_key": "attempt"}, CALLER)
    assert err.value.rule == "policy_config_error:model_tiers"
    assert "operator must fix model_tiers" in err.value.detail
    assert "ephemeral.model_tiers" not in err.value.detail
    assert "re-create" not in err.value.detail
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending"
    assert all(row[k] is None for k in ("claim_id", "claim_expires_at", "idempotency_key"))
    archive = json.loads(Path(row["audit_path"]).read_text())
    assert archive["refusals"][err.value.rule]["count"] == 1
    assert (home / "ephemeral/live" / (name + ".md")).exists()
