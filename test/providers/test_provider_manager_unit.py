"""Unit tests for ProviderManager."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.codex import CodexProvider
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


def test_cleanup_provider_calls_cleanup_and_removes():
    manager = ProviderManager()
    provider = MagicMock()
    manager._providers["t1"] = provider

    manager.cleanup_provider("t1")

    provider.cleanup.assert_called_once()
    assert manager._providers.get("t1") is None
