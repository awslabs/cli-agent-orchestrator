"""Helper probe refusals cross the real API and reach their creator unchanged."""

import json
from test.providers.kiro_helper_support import operator_home  # noqa: F401
from test.services.test_ephemeral_service import CALLER, create_store  # noqa: F401
from test.services.test_kiro_helper_launch import (  # noqa: F401
    assert_refused_before_bind,
    audit,
    kiro,
)

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.plugins.registry import PluginRegistry
from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilityError
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.utils import orchestration


@pytest.fixture
def helper_http(kiro, monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
    monkeypatch.setattr(kiro.service.secrets, "token_hex", lambda n: "c" * (2 * n))
    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    with TestClient(main.app, base_url="http://localhost") as client:

        def call(method, url, **kwargs):
            kwargs.pop("timeout", None)
            if kwargs.get("params") is not None:
                kwargs["params"] = {
                    key: getattr(value, "value", value) for key, value in kwargs["params"].items()
                }
            response = client.request(
                method, url.removeprefix(orchestration.API_BASE_URL), **kwargs
            )
            kiro.seen.setdefault("responses", []).append((response.status_code, response.text))
            return response

        for method in ["get", "post", "delete"]:
            monkeypatch.setattr(
                orchestration.requests, method, lambda url, _m=method, **kw: call(_m, url, **kw)
            )
        yield kiro


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["assign", "handoff"])
@pytest.mark.parametrize("refusal", ["version", "flag", "field"])
async def test_helper_probe_refusal_reaches_creator_and_archive(
    helper_http, monkeypatch, path, refusal
):
    helper = helper_http
    if refusal == "version":
        (helper.fake / "version").write_text("3.1.0")
        rule, clue = "kiro_v3_unsupported_version", "3.1.0"
    elif refusal == "flag":
        script = helper.script.read_text()
        helper.script.write_text(
            script.replace(
                'encoding="utf-8").read())',
                'encoding="utf-8").read().replace("--trust-all-tools", "--absent"))',
            )
        )
        rule, clue = "unsupported_capability", "--trust-all-tools"
    else:
        # The rule comes from kind, never a message prefix (which can disagree).
        def reject(*args, **kwargs):
            raise KiroCapabilityError(
                "unsupported_capability", KiroEngine.V3, "different_prefix: missing wrapper flag"
            )

        monkeypatch.setattr(terminal_service, "probe_kiro_capabilities", reject)
        rule, clue = "unsupported_capability", "different_prefix"

    if path == "assign":
        result = orchestration._assign_impl(
            helper.name, "inspect", working_directory=str(helper.repo)
        )
    else:
        result = (
            await orchestration._handoff_impl(
                helper.name, "inspect", working_directory=str(helper.repo)
            )
        ).model_dump()
    assert result["success"] is False
    assert result["rule"] == rule, (result, helper.seen["responses"])
    assert clue in result["message"]
    if refusal != "field":
        assert "Use a supported kiro-cli version" in result["message"]
        assert "v2" not in result["message"]
    assert str(helper.fake) not in result["message"]
    assert audit(helper.name)["refusals"][rule]["count"] == 1
    assert_refused_before_bind(helper)


@pytest.mark.asyncio
async def test_installed_probe_refusal_keeps_plain_string_400(helper_http, monkeypatch):
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    message = "kiro_v3_unsupported_version: original installed remedy; retry with engine 'v2'."
    monkeypatch.setattr(
        terminal_service.agent_profiles,
        "load_agent_profile",
        lambda name: AgentProfile(name=name, description="installed"),
    )

    def reject(*args, **kwargs):
        raise KiroCapabilityError(
            "kiro_v3_unsupported_version", KiroEngine.V3, message, version="3.1.0"
        )

    monkeypatch.setattr(terminal_service, "probe_kiro_capabilities", reject)
    response = orchestration.requests.post(
        f"{orchestration.API_BASE_URL}/sessions/cao-session/terminals",
        params={
            "provider": "kiro_cli",
            "agent_profile": "installed",
            "engine": "v3",
            "caller_id": CALLER,
        },
    )
    assert response.status_code == 400, response.text
    assert response.content == json.dumps({"detail": message}, separators=(",", ":")).encode()
