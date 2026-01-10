"""Integration tests for OpenAutoGLM provider with provider manager."""

from unittest.mock import Mock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.providers.open_autoglm import OpenAutoGLMProvider


class TestOpenAutoGLMProviderManagerIntegration:
    """Test OpenAutoGLM provider integration with provider manager."""

    @patch("cli_agent_orchestrator.providers.manager.get_terminal_metadata")
    def test_create_openautoglm_provider(self, mock_get_metadata):
        """Test creating OpenAutoGLM provider through manager."""
        provider = provider_manager.create_provider(
            ProviderType.OPEN_AUTOGLM.value,
            "test1234",
            "test-session",
            "window-0",
            "mobile_agent"
        )

        assert isinstance(provider, OpenAutoGLMProvider)
        assert provider.terminal_id == "test1234"
        assert provider.session_name == "test-session"
        assert provider.window_name == "window-0"
        assert provider._agent_profile == "mobile_agent"

        # Verify provider is stored in manager
        assert "test1234" in provider_manager._providers
        assert provider_manager._providers["test1234"] is provider

    @patch("cli_agent_orchestrator.providers.manager.get_terminal_metadata")
    def test_get_openautoglm_provider_on_demand(self, mock_get_metadata):
        """Test getting OpenAutoGLM provider on-demand from database."""
        # Mock database metadata
        mock_get_metadata.return_value = {
            "provider": ProviderType.OPEN_AUTOGLM.value,
            "tmux_session": "test-session",
            "tmux_window": "window-0",
            "agent_profile": "mobile_agent"
        }

        provider = provider_manager.get_provider("test1234")

        assert isinstance(provider, OpenAutoGLMProvider)
        assert provider.terminal_id == "test1234"
        assert provider.session_name == "test-session"
        assert provider.window_name == "window-0"
        assert provider._agent_profile == "mobile_agent"

        # Verify provider is cached
        assert "test1234" in provider_manager._providers

    @patch("cli_agent_orchestrator.providers.manager.get_terminal_metadata")
    def test_get_provider_with_no_metadata(self, mock_get_metadata):
        """Test get provider fails when no metadata found."""
        mock_get_metadata.return_value = None

        with pytest.raises(ValueError, match="Terminal test1234 not found in database"):
            provider_manager.get_provider("test1234")

    def test_cleanup_openautoglm_provider(self):
        """Test cleaning up OpenAutoGLM provider."""
        # First create a provider
        provider = provider_manager.create_provider(
            ProviderType.OPEN_AUTOGLM.value,
            "test1234",
            "test-session",
            "window-0"
        )

        # Mock the cleanup method
        with patch.object(provider, 'cleanup') as mock_cleanup:
            provider_manager.cleanup_provider("test1234")

            # Verify cleanup was called and provider removed
            mock_cleanup.assert_called_once()
            assert "test1234" not in provider_manager._providers

    def test_cleanup_nonexistent_provider(self):
        """Test cleanup of non-existent provider doesn't raise error."""
        # Should not raise an exception
        provider_manager.cleanup_provider("nonexistent123")

    def test_list_providers_includes_openautoglm(self):
        """Test that listing providers includes OpenAutoGLM provider."""
        # Create multiple providers including OpenAutoGLM
        provider_manager.create_provider(
            ProviderType.OPEN_AUTOGLM.value,
            "auto1234",
            "session1",
            "window1"
        )
        provider_manager.create_provider(
            ProviderType.CLAUDE_CODE.value,
            "claude1234",
            "session2",
            "window2"
        )

        providers = provider_manager.list_providers()

        assert "auto1234" in providers
        assert providers["auto1234"] == "OpenAutoGLMProvider"
        assert "claude1234" in providers
        assert providers["claude1234"] == "ClaudeCodeProvider"

    @patch("cli_agent_orchestrator.providers.manager.get_terminal_metadata")
    def test_unknown_provider_type_raises_error(self, mock_get_metadata):
        """Test that unknown provider type raises ValueError."""
        mock_get_metadata.return_value = {
            "provider": "unknown_provider",
            "tmux_session": "test-session",
            "tmux_window": "window-0",
            "agent_profile": None
        }

        with pytest.raises(ValueError, match="Unknown provider type: unknown_provider"):
            provider_manager.get_provider("test1234")

    @patch("cli_agent_orchestrator.providers.manager.get_terminal_metadata")
    def test_openautoglm_provider_creation_error_handling(self, mock_get_metadata):
        """Test error handling during OpenAutoGLM provider creation."""
        mock_get_metadata.return_value = {
            "provider": ProviderType.OPEN_AUTOGLM.value,
            "tmux_session": "test-session",
            "tmux_window": "window-0",
            "agent_profile": None
        }

        # Mock OpenAutoGLMProvider to raise an exception
        with patch("cli_agent_orchestrator.providers.manager.OpenAutoGLMProvider") as mock_provider_class:
            mock_provider_class.side_effect = Exception("Failed to initialize")

            with pytest.raises(Exception, match="Failed to initialize"):
                provider_manager.get_provider("test1234")

            # Should not be cached after failed creation
            assert "test1234" not in provider_manager._providers