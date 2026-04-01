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

        # Execute and verify — FileNotFoundError passes through directly
        with pytest.raises(FileNotFoundError, match="Agent profile not found"):
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
        mock_load.return_value = AgentProfile(name="reviewer", description="Reviewer agent")

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

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_fallback_when_provider_is_empty_string(self, mock_load):
        """Empty string provider should be treated as absent and fall back."""
        mock_load.return_value = AgentProfile(
            name="developer", description="Dev agent", provider=""
        )

        result = resolve_provider("developer", fallback_provider="kiro_cli")

        assert result == "kiro_cli"


class TestListAgentProfiles:
    """Tests for list_agent_profiles function."""

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_discovers_from_multiple_directories(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that list_agent_profiles discovers profiles from multiple directories."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Setup built-in store with one profile
        mock_builtin_file = MagicMock()
        mock_builtin_file.name = "builtin-agent.md"
        mock_builtin_file.read_text.return_value = "---\ndescription: A built-in agent\n---\nPrompt"
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_builtin_file]
        mock_resources.files.return_value = mock_agent_store

        # Setup local store dir
        mock_local_dir.exists.return_value = True
        mock_local_dir.resolve.return_value = Path(
            "/home/user/.aws/cli-agent-orchestrator/agent-store"
        )

        # Setup provider dirs: kiro_cli dir with a third profile
        mock_get_agent_dirs.return_value = {
            "kiro_cli": "/home/user/.kiro/agents",
        }

        def fake_scan(directory, source_label, profiles):
            if source_label == "local":
                profiles["local-agent"] = {
                    "name": "local-agent",
                    "description": "A local agent",
                    "source": "local",
                }
            elif source_label == "kiro":
                profiles["kiro-agent"] = {
                    "name": "kiro-agent",
                    "description": "A Kiro agent",
                    "source": "kiro",
                }

        mock_scan.side_effect = fake_scan

        result = list_agent_profiles()

        # Should have built-in + local + kiro profiles
        names = [p["name"] for p in result]
        assert "builtin-agent" in names
        assert "local-agent" in names
        assert "kiro-agent" in names

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_deduplicates_profiles_with_same_name(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that profiles with the same name are deduplicated (first wins)."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Built-in store has "developer" profile
        mock_builtin_file = MagicMock()
        mock_builtin_file.name = "developer.md"
        mock_builtin_file.read_text.return_value = (
            "---\ndescription: Built-in developer\n---\nPrompt"
        )
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_builtin_file]
        mock_resources.files.return_value = mock_agent_store

        # Local store also has "developer" profile — should be skipped (built-in scanned first)
        mock_local_dir.exists.return_value = True
        mock_local_dir.resolve.return_value = Path(
            "/home/user/.aws/cli-agent-orchestrator/agent-store"
        )

        def fake_scan(directory, source_label, profiles):
            if source_label == "local":
                # _scan_directory respects dedup: only adds if not present
                if "developer" not in profiles:
                    profiles["developer"] = {
                        "name": "developer",
                        "description": "Local developer",
                        "source": "local",
                    }

        mock_scan.side_effect = fake_scan

        result = list_agent_profiles()

        # Should have exactly one "developer" profile, from built-in (scanned first)
        developer_profiles = [p for p in result if p["name"] == "developer"]
        assert len(developer_profiles) == 1
        assert developer_profiles[0]["source"] == "built-in"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_includes_builtin_profiles(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that built-in profiles are included and marked with source 'built-in'."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Setup two built-in profiles
        mock_file1 = MagicMock()
        mock_file1.name = "developer.md"
        mock_file1.read_text.return_value = "---\ndescription: Developer agent\n---\nPrompt"
        mock_file2 = MagicMock()
        mock_file2.name = "reviewer.md"
        mock_file2.read_text.return_value = "---\ndescription: Reviewer agent\n---\nPrompt"
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_file1, mock_file2]
        mock_resources.files.return_value = mock_agent_store

        # No local, provider, or extra dirs
        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        # Verify built-in profiles are present with correct source
        names = {p["name"] for p in result}
        assert "developer" in names
        assert "reviewer" in names
        for p in result:
            assert p["source"] == "built-in"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_handles_nonexistent_directories(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that nonexistent directories are handled gracefully without errors."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # No built-in profiles (simulate error)
        mock_resources.files.side_effect = Exception("Package not found")

        # Local store does not exist
        mock_local_dir.exists.return_value = False

        # Provider dirs point to nonexistent paths
        mock_get_agent_dirs.return_value = {
            "kiro_cli": "/nonexistent/kiro/agents",
            "q_cli": "/nonexistent/q/agents",
        }
        mock_get_extra_dirs.return_value = ["/nonexistent/extra/dir"]

        result = list_agent_profiles()

        # Should return empty list without raising any exceptions
        assert isinstance(result, list)

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_scans_extra_dirs_from_settings(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that extra_dirs from settings service are scanned for profiles."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # No built-in profiles
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = []
        mock_resources.files.return_value = mock_agent_store

        # No local store
        mock_local_dir.exists.return_value = False

        # Extra dirs from settings
        mock_get_extra_dirs.return_value = [
            "/custom/agents/dir1",
            "/custom/agents/dir2",
        ]

        scan_calls = []

        def track_scan(directory, source_label, profiles):
            scan_calls.append((str(directory), source_label))
            if str(directory) == "/custom/agents/dir1":
                profiles["custom-agent1"] = {
                    "name": "custom-agent1",
                    "description": "Custom agent 1",
                    "source": "custom",
                }
            elif str(directory) == "/custom/agents/dir2":
                profiles["custom-agent2"] = {
                    "name": "custom-agent2",
                    "description": "Custom agent 2",
                    "source": "custom",
                }

        mock_scan.side_effect = track_scan

        result = list_agent_profiles()

        # Verify extra dirs were scanned with "custom" source label
        extra_scans = [(d, l) for d, l in scan_calls if l == "custom"]
        assert len(extra_scans) == 2
        assert ("/custom/agents/dir1", "custom") in extra_scans
        assert ("/custom/agents/dir2", "custom") in extra_scans

        # Verify profiles from extra dirs are returned
        names = [p["name"] for p in result]
        assert "custom-agent1" in names
        assert "custom-agent2" in names

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_returns_sorted_by_name(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that returned profiles are sorted alphabetically by name."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Built-in profiles in non-alphabetical order
        mock_file_z = MagicMock()
        mock_file_z.name = "zebra.md"
        mock_file_z.read_text.return_value = "---\ndescription: Zebra\n---\nPrompt"
        mock_file_a = MagicMock()
        mock_file_a.name = "alpha.md"
        mock_file_a.read_text.return_value = "---\ndescription: Alpha\n---\nPrompt"
        mock_file_m = MagicMock()
        mock_file_m.name = "middle.md"
        mock_file_m.read_text.return_value = "---\ndescription: Middle\n---\nPrompt"
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_file_z, mock_file_a, mock_file_m]
        mock_resources.files.return_value = mock_agent_store

        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        names = [p["name"] for p in result]
        assert names == sorted(names)
        assert names == ["alpha", "middle", "zebra"]


class TestScanDirectory:
    """Tests for _scan_directory helper function."""

    def test_scan_directory_skips_nonexistent_directory(self, tmp_path):
        """Test that _scan_directory does nothing for nonexistent directories."""
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        nonexistent = tmp_path / "does_not_exist"
        profiles = {}
        _scan_directory(nonexistent, "test", profiles)
        assert profiles == {}

    def test_scan_directory_finds_md_files(self, tmp_path):
        """Test that _scan_directory finds .md files in a directory."""
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create a .md file with frontmatter
        md_file = tmp_path / "my-agent.md"
        md_file.write_text("---\ndescription: My agent\n---\nSystem prompt")

        profiles = {}
        _scan_directory(tmp_path, "local", profiles)

        assert "my-agent" in profiles
        assert profiles["my-agent"]["name"] == "my-agent"
        assert profiles["my-agent"]["description"] == "My agent"
        assert profiles["my-agent"]["source"] == "local"

    def test_scan_directory_finds_subdirectory_profiles(self, tmp_path):
        """Test that _scan_directory finds agent.md inside subdirectories."""
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create a subdirectory with agent.md
        agent_dir = tmp_path / "sub-agent"
        agent_dir.mkdir()
        agent_md = agent_dir / "agent.md"
        agent_md.write_text("---\ndescription: Sub agent\n---\nPrompt content")

        profiles = {}
        _scan_directory(tmp_path, "kiro", profiles)

        assert "sub-agent" in profiles
        assert profiles["sub-agent"]["description"] == "Sub agent"
        assert profiles["sub-agent"]["source"] == "kiro"

    def test_scan_directory_does_not_overwrite_existing_profile(self, tmp_path):
        """Test that _scan_directory does not overwrite an already-discovered profile."""
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        md_file = tmp_path / "existing.md"
        md_file.write_text("---\ndescription: New version\n---\nPrompt")

        profiles = {
            "existing": {
                "name": "existing",
                "description": "Original version",
                "source": "built-in",
            }
        }
        _scan_directory(tmp_path, "local", profiles)

        # Should retain the original profile
        assert profiles["existing"]["description"] == "Original version"
        assert profiles["existing"]["source"] == "built-in"
