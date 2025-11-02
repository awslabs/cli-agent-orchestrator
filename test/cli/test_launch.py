"""Tests for the `cao launch` command provider selection."""

from click.testing import CliRunner

import cli_agent_orchestrator.cli.commands.launch as launch_module


class DummyResponse:
    """Simple response stub for requests.post calls."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_launch_uses_agent_profile_provider(monkeypatch):
    """Verify the CLI honors the provider declared in the agent profile."""

    runner = CliRunner()
    captured = {}

    class Profile:
        provider = "codex_cli"

    monkeypatch.setattr(launch_module, "load_agent_profile", lambda _: Profile())
    monkeypatch.setattr(
        launch_module.requests,
        "post",
        lambda url, params: _capture_post(url, params, captured, "cao-session-codex", "codex-window"),
    )

    result = runner.invoke(launch_module.launch, ["--agents", "product_supervisor", "--headless"])

    assert result.exit_code == 0
    assert captured["params"]["provider"] == "codex_cli"


def test_launch_defaults_to_q_cli_when_profile_missing(monkeypatch):
    """Ensure the CLI falls back to q_cli if profile lookup fails or lacks a provider."""

    runner = CliRunner()
    captured = {}

    monkeypatch.setattr(launch_module, "load_agent_profile", lambda _: _raise_runtime())
    monkeypatch.setattr(
        launch_module.requests,
        "post",
        lambda url, params: _capture_post(url, params, captured, "cao-session-default", "default-window"),
    )

    result = runner.invoke(launch_module.launch, ["--agents", "unknown", "--headless"])

    assert result.exit_code == 0
    assert captured["params"]["provider"] == "q_cli"
    assert "Using provider 'q_cli'" in result.output


def _capture_post(url, params, captured, session_name, window_name):
    """Capture params for assertions while mimicking a successful POST call."""

    captured["url"] = url
    captured["params"] = params
    return DummyResponse({"session_name": session_name, "name": window_name})


def _raise_runtime():
    """Helper to raise RuntimeError for monkeypatch scenarios."""

    raise RuntimeError("profile missing")

