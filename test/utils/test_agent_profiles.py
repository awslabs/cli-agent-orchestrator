"""Tests for agent profile utilities."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile


class TestLoadAgentProfile:
    """Tests for load_agent_profile function."""

    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.frontmatter")
    def test_load_agent_profile_from_local_store(self, mock_frontmatter, mock_local_dir):
        """Test loading agent profile from local store."""
        # Setup mock local directory
        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = True
        mock_local_path.read_text.return_value = "---\nname: test-agent\ndescription: Test agent\n---\nSystem prompt content"
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
    def test_load_agent_profile_from_builtin_store(self, mock_frontmatter, mock_local_dir, mock_resources):
        """Test loading agent profile from built-in store when local not found."""
        # Setup mock local directory (not found)
        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local_dir.__truediv__.return_value = mock_local_path

        # Setup built-in store mock
        mock_agent_store = MagicMock()
        mock_profile_file = MagicMock()
        mock_profile_file.is_file.return_value = True
        mock_profile_file.read_text.return_value = "---\nname: builtin-agent\ndescription: Builtin agent\n---\nBuiltin prompt"
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
