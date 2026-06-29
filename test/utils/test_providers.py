"""Tests for the shared provider-installation helpers."""

from unittest.mock import patch

from cli_agent_orchestrator.utils import providers

_P = "cli_agent_orchestrator.utils.providers"


def test_provider_binary_known_and_unknown():
    assert providers.provider_binary("opencode_cli") == "opencode"
    assert providers.provider_binary("does_not_exist") is None


@patch(f"{_P}.shutil.which")
def test_provider_binary_installed(mock_which):
    mock_which.side_effect = lambda b: "/usr/bin/opencode" if b == "opencode" else None
    assert providers.provider_binary_installed("opencode_cli") is True
    assert providers.provider_binary_installed("codex") is False
    # Unknown provider → False without consulting PATH.
    assert providers.provider_binary_installed("ghost") is False


@patch(f"{_P}.shutil.which")
def test_installed_providers(mock_which):
    installed = {"claude", "codex"}
    mock_which.side_effect = lambda b: f"/usr/bin/{b}" if b in installed else None
    result = providers.installed_providers()
    assert "claude_code" in result
    assert "codex" in result
    assert "opencode_cli" not in result
