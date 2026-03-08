"""Tests for agent profile utilities."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile, resolve_provider


class TestLoadAgentProfile:
    """Tests for load_agent_profile function."""

    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.frontmatter")
    def test_load_agent_profile_from_local_store(self, mock_frontmatter, mock_local_dir):
        """Test loading agent profile from local store."""
        # Setup mock local directory
        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = True
        mock_local_path.read_text.return_value = (
            "---\nname: test-agent\ndescription: Test agent\n---\nSystem prompt content"
        )
        mock_local_dir.__truediv__.return_value = mock_local_path

        # Setup frontmatter mock
        mock_parsed = MagicMock()
        mock_parsed.metadata = {"name": "test-agent", "description": "Test agent"}
        mock_parsed.content = "System prompt content"
        mock_frontmatter.loads.return_value = mock_parsed

        # Execute
        result = load_agent_profile("test-agent")

        # Verify
        assert result.name == "test-agent"
        assert result.description == "Test agent"
        assert result.system_prompt == "System prompt content"
        mock_local_path.exists.assert_called_once()

    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.frontmatter")
    def test_load_agent_profile_from_builtin_store(
        self, mock_frontmatter, mock_local_dir, mock_resources
    ):
        """Test loading agent profile from built-in store when local not found."""
        # Setup mock local directory (not found)
        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local_dir.__truediv__.return_value = mock_local_path

        # Setup built-in store mock
        mock_agent_store = MagicMock()
        mock_profile_file = MagicMock()
        mock_profile_file.is_file.return_value = True
        mock_profile_file.read_text.return_value = (
            "---\nname: builtin-agent\ndescription: Builtin agent\n---\nBuiltin prompt"
        )
        mock_agent_store.__truediv__.return_value = mock_profile_file
        mock_resources.files.return_value = mock_agent_store

        # Setup frontmatter mock
        mock_parsed = MagicMock()
        mock_parsed.metadata = {"name": "builtin-agent", "description": "Builtin agent"}
        mock_parsed.content = "Builtin prompt"
        mock_frontmatter.loads.return_value = mock_parsed

        # Execute
        result = load_agent_profile("builtin-agent")

        # Verify
        assert result.name == "builtin-agent"
        assert result.description == "Builtin agent"
        assert result.system_prompt == "Builtin prompt"

    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    def test_load_agent_profile_not_found(self, mock_local_dir, mock_resources):
        """Test loading agent profile that doesn't exist."""
        # Setup mock local directory (not found)
        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local_dir.__truediv__.return_value = mock_local_path

        # Setup built-in store mock (not found)
        mock_agent_store = MagicMock()
        mock_profile_file = MagicMock()
        mock_profile_file.is_file.return_value = False
        mock_agent_store.__truediv__.return_value = mock_profile_file
        mock_resources.files.return_value = mock_agent_store

        # Execute and verify
        with pytest.raises(RuntimeError, match="Failed to load agent profile"):
            load_agent_profile("nonexistent")

    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    def test_load_agent_profile_exception_handling(self, mock_local_dir):
        """Test exception handling in load_agent_profile."""
        # Setup mock to raise exception
        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.side_effect = Exception("File system error")
        mock_local_dir.__truediv__.return_value = mock_local_path

        # Execute and verify
        with pytest.raises(RuntimeError, match="Failed to load agent profile"):
            load_agent_profile("test-agent")


class TestResolveProvider:
    """Tests for resolve_provider function."""

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_profile_provider_when_valid(self, mock_load):
        """Profile with a valid provider key should override the fallback."""
        mock_load.return_value = AgentProfile(
            name="developer", description="Dev agent", provider="claude_code"
        )

        result = resolve_provider("developer", fallback_provider="kiro_cli")

        assert result == "claude_code"
        mock_load.assert_called_once_with("developer")

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_fallback_when_no_provider_key(self, mock_load):
        """Profile without a provider key should fall back to the caller's provider."""
        mock_load.return_value = AgentProfile(
            name="reviewer", description="Reviewer agent"
        )

        result = resolve_provider("reviewer", fallback_provider="kiro_cli")

        assert result == "kiro_cli"

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_fallback_when_provider_is_invalid(self, mock_load, caplog):
        """Profile with an invalid provider value should fall back and log a warning."""
        mock_load.return_value = AgentProfile(
            name="developer", description="Dev agent", provider="claud_code"
        )

        with caplog.at_level(logging.WARNING):
            result = resolve_provider("developer", fallback_provider="kiro_cli")

        assert result == "kiro_cli"
        assert "invalid provider" in caplog.text.lower()
        assert "claud_code" in caplog.text

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_fallback_when_profile_not_found(self, mock_load):
        """Missing profile should fall back without raising."""
        mock_load.side_effect = RuntimeError("Failed to load agent profile 'ghost'")

        result = resolve_provider("ghost", fallback_provider="q_cli")

        assert result == "q_cli"

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_all_valid_provider_types_accepted(self, mock_load):
        """Each ProviderType enum value should be accepted as a valid provider."""
        from cli_agent_orchestrator.constants import PROVIDERS

        for provider_value in PROVIDERS:
            mock_load.return_value = AgentProfile(
                name="agent", description="test", provider=provider_value
            )
            result = resolve_provider("agent", fallback_provider="kiro_cli")
            assert result == provider_value

