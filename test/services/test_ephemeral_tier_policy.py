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
