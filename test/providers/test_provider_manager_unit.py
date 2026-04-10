"""Unit tests for ProviderManager."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.manager import ProviderManager


def test_create_provider_codex_stores_mapping():
    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.CODEX.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )

    assert isinstance(provider, CodexProvider)
    assert manager.get_provider("t1") is provider


@patch("cli_agent_orchestrator.providers.manager.KiroCliProvider")
def test_create_provider_kiro_does_not_forward_skill_prompt(mock_kiro_provider):
    manager = ProviderManager()
    mock_instance = MagicMock()
    mock_kiro_provider.return_value = mock_instance

    provider = manager.create_provider(
        ProviderType.KIRO_CLI.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile="developer",
        skill_prompt="## Available Skills",
    )

    assert provider is mock_instance
    assert manager.get_provider("t1") is mock_instance
    mock_kiro_provider.assert_called_once_with("t1", "s1", "w1", "developer", None)


@patch("cli_agent_orchestrator.providers.manager.QCliProvider")
def test_create_provider_q_does_not_forward_skill_prompt(mock_q_provider):
    manager = ProviderManager()
    mock_instance = MagicMock()
    mock_q_provider.return_value = mock_instance

    provider = manager.create_provider(
        ProviderType.Q_CLI.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile="developer",
        skill_prompt="## Available Skills",
    )

    assert provider is mock_instance
    assert manager.get_provider("t1") is mock_instance
    mock_q_provider.assert_called_once_with("t1", "s1", "w1", "developer", None)


def test_create_provider_copilot_stores_mapping():
    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.COPILOT_CLI.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )

    assert isinstance(provider, CopilotCliProvider)
    assert manager.get_provider("t1") is provider


@patch("cli_agent_orchestrator.providers.manager.CopilotCliProvider")
def test_create_provider_copilot_does_not_forward_skill_prompt(mock_copilot_provider):
    manager = ProviderManager()
    mock_instance = MagicMock()
    mock_copilot_provider.return_value = mock_instance

    provider = manager.create_provider(
        ProviderType.COPILOT_CLI.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile="developer",
        skill_prompt="## Available Skills",
    )

    assert provider is mock_instance
    assert manager.get_provider("t1") is mock_instance
    mock_copilot_provider.assert_called_once_with("t1", "s1", "w1", "developer", None)


def test_create_provider_unknown_type_raises():
    manager = ProviderManager()
    with pytest.raises(ValueError, match="Unknown provider type"):
        manager.create_provider(
            "unknown",
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
        )


def test_get_provider_creates_on_demand_from_metadata():
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": ProviderType.CODEX.value,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": None,
        },
    ):
        provider = manager.get_provider("t1")

    assert isinstance(provider, CodexProvider)
    assert manager.get_provider("t1") is provider


def test_get_provider_creates_copilot_on_demand_from_metadata():
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": ProviderType.COPILOT_CLI.value,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": None,
        },
    ):
        provider = manager.get_provider("t1")

    assert isinstance(provider, CopilotCliProvider)
    assert manager.get_provider("t1") is provider


def test_cleanup_provider_calls_cleanup_and_removes():
    manager = ProviderManager()
    provider = MagicMock()
    manager._providers["t1"] = provider

    manager.cleanup_provider("t1")

    provider.cleanup.assert_called_once()
    assert manager._providers.get("t1") is None


def test_create_provider_q_cli_without_agent_profile_raises():
    """Test Q CLI provider requires agent_profile."""
    manager = ProviderManager()
    with pytest.raises(ValueError, match="Q CLI provider requires agent_profile parameter"):
        manager.create_provider(
            ProviderType.Q_CLI.value,
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
        )


def test_create_provider_kiro_cli_without_agent_profile_raises():
    """Test Kiro CLI provider requires agent_profile."""
    manager = ProviderManager()
    with pytest.raises(ValueError, match="Kiro CLI provider requires agent_profile parameter"):
        manager.create_provider(
            ProviderType.KIRO_CLI.value,
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
        )


def test_create_provider_claude_code():
    """Test creating Claude Code provider."""
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.CLAUDE_CODE.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )

    assert isinstance(provider, ClaudeCodeProvider)
    assert manager.get_provider("t1") is provider


def test_get_provider_not_in_database_raises():
    """Test get_provider raises when terminal not found in database."""
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value=None,
    ):
        with pytest.raises(ValueError, match="Terminal t1 not found in database"):
            manager.get_provider("t1")


def test_cleanup_provider_handles_exception():
    """Test cleanup_provider handles exceptions gracefully."""
    manager = ProviderManager()
    provider = MagicMock()
    provider.cleanup.side_effect = Exception("Cleanup failed")
    manager._providers["t1"] = provider

    # Should not raise
    manager.cleanup_provider("t1")

    provider.cleanup.assert_called_once()
    # Provider should still be removed even if cleanup fails
    assert manager._providers.get("t1") is None


def test_cleanup_provider_nonexistent_terminal():
    """Test cleanup_provider with nonexistent terminal."""
    manager = ProviderManager()

    # Should not raise
    manager.cleanup_provider("nonexistent")


def test_list_providers():
    """Test list_providers returns correct mapping."""
    from cli_agent_orchestrator.providers.codex import CodexProvider

    manager = ProviderManager()
    manager.create_provider(
        ProviderType.CODEX.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )
    manager.create_provider(
        ProviderType.CLAUDE_CODE.value,
        terminal_id="t2",
        tmux_session="s2",
        tmux_window="w2",
        agent_profile=None,
    )

    result = manager.list_providers()

    assert result == {
        "t1": "CodexProvider",
        "t2": "ClaudeCodeProvider",
    }
