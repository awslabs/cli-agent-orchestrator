"""Additional agent_profiles tests for coverage gaps.

Covers: _scan_directory for .md files and subdirs with frontmatter errors,
load_agent_profile from provider/extra dirs, built-in fallback with missing fields,
and list_agent_profiles with frontmatter parse errors.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile


class TestScanDirectory:
    """Test _scan_directory for .md files and subdirectories."""

    def test_scan_md_files(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create .md file with frontmatter
        md_file = tmp_path / "my-agent.md"
        md_file.write_text("---\ndescription: My agent\n---\nPrompt content")

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        assert "my-agent" in profiles
        assert profiles["my-agent"]["description"] == "My agent"
        assert profiles["my-agent"]["source"] == "test"

    def test_scan_subdirectory_with_agent_md(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create subdirectory with agent.md
        agent_dir = tmp_path / "sub-agent"
        agent_dir.mkdir()
        (agent_dir / "agent.md").write_text("---\ndescription: Sub agent\n---\nPrompt")

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        assert "sub-agent" in profiles
        assert profiles["sub-agent"]["description"] == "Sub agent"

    def test_scan_subdirectory_without_agent_md(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create subdirectory without agent.md
        (tmp_path / "bare-agent").mkdir()

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        assert "bare-agent" in profiles
        assert profiles["bare-agent"]["description"] == ""

    def test_scan_md_file_with_bad_frontmatter(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create .md file with invalid frontmatter
        md_file = tmp_path / "broken.md"
        md_file.write_text("not valid yaml frontmatter ::::")

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        # Should still be added with empty description
        assert "broken" in profiles
        assert profiles["broken"]["description"] == ""

    def test_scan_subdirectory_with_bad_agent_md(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        agent_dir = tmp_path / "bad-sub"
        agent_dir.mkdir()
        (agent_dir / "agent.md").write_text("invalid: [yaml: broken")

        profiles = {}
        _scan_directory(tmp_path, "test", profiles)

        # Should still be added with empty description
        assert "bad-sub" in profiles
        assert profiles["bad-sub"]["description"] == ""

    def test_scan_nonexistent_directory(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        profiles = {}
        _scan_directory(tmp_path / "nonexistent", "test", profiles)

        assert profiles == {}

    def test_scan_deduplicates(self, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        (tmp_path / "agent.md").write_text("---\ndescription: First\n---\n")

        profiles = {"agent": {"name": "agent", "description": "Existing", "source": "other"}}
        _scan_directory(tmp_path, "test", profiles)

        # Should keep existing entry
        assert profiles["agent"]["source"] == "other"
        assert profiles["agent"]["description"] == "Existing"


class TestLoadAgentProfileFromProviderDirs:
    """Test load_agent_profile searching provider and extra directories."""

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    def test_load_from_provider_flat_file(self, mock_local, mock_get_dirs, mock_extra, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        # Local store doesn't have it
        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local.__truediv__.return_value = mock_local_path

        # Provider dir has flat .md file
        agent_md = tmp_path / "my-agent.md"
        agent_md.write_text("---\nname: my-agent\ndescription: Provider agent\n---\nPrompt")
        mock_get_dirs.return_value = {"kiro_cli": str(tmp_path)}

        result = load_agent_profile("my-agent")

        assert result.name == "my-agent"
        assert result.description == "Provider agent"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    def test_load_from_provider_directory_style(
        self, mock_local, mock_get_dirs, mock_extra, tmp_path
    ):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local.__truediv__.return_value = mock_local_path

        # Provider dir has directory-style: my-agent/agent.md
        (tmp_path / "my-agent").mkdir()
        (tmp_path / "my-agent" / "agent.md").write_text("---\ndescription: Dir agent\n---\nPrompt")
        mock_get_dirs.return_value = {"kiro_cli": str(tmp_path)}

        result = load_agent_profile("my-agent")

        assert result.name == "my-agent"  # Filled in because missing from frontmatter
        assert result.description == "Dir agent"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    def test_load_from_extra_dirs_flat(self, mock_local, mock_get_dirs, mock_extra, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local.__truediv__.return_value = mock_local_path

        # Extra dir has the agent
        agent_md = tmp_path / "custom-agent.md"
        agent_md.write_text("---\nname: custom-agent\ndescription: Custom\n---\nPrompt")
        mock_extra.return_value = [str(tmp_path)]

        result = load_agent_profile("custom-agent")

        assert result.name == "custom-agent"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    def test_load_from_extra_dirs_directory_style(
        self, mock_local, mock_get_dirs, mock_extra, tmp_path
    ):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local.__truediv__.return_value = mock_local_path

        (tmp_path / "dir-agent").mkdir()
        (tmp_path / "dir-agent" / "agent.md").write_text("---\ndescription: Dir style\n---\nPrompt")
        mock_extra.return_value = [str(tmp_path)]

        result = load_agent_profile("dir-agent")

        assert result.name == "dir-agent"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    def test_skips_nonexistent_provider_dir(self, mock_local, mock_get_dirs, mock_extra, tmp_path):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local.__truediv__.return_value = mock_local_path

        # Provider dir doesn't exist
        mock_get_dirs.return_value = {"kiro_cli": "/nonexistent/path/xyz"}

        # Extra dir also doesn't exist
        mock_extra.return_value = ["/also/nonexistent"]

        # Should fall through to built-in store and raise FileNotFoundError
        with pytest.raises(FileNotFoundError, match="Agent profile not found"):
            load_agent_profile("missing-agent")

    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    def test_builtin_fills_missing_name_and_description(
        self, mock_local, mock_get_dirs, mock_extra, mock_resources
    ):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = False
        mock_local.__truediv__.return_value = mock_local_path

        # Built-in store has profile without name/description in frontmatter
        mock_profile_file = MagicMock()
        mock_profile_file.is_file.return_value = True
        mock_profile_file.read_text.return_value = "---\n---\nJust a prompt"
        mock_agent_store = MagicMock()
        mock_agent_store.__truediv__.return_value = mock_profile_file
        mock_resources.files.return_value = mock_agent_store

        result = load_agent_profile("bare-agent")

        assert result.name == "bare-agent"
        assert result.description == ""


class TestListAgentProfilesEdgeCases:
    """Test list_agent_profiles edge cases for coverage."""

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_builtin_profile_with_parse_error_still_added(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Built-in profile with bad frontmatter should still be added with empty description."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_file = MagicMock()
        mock_file.name = "broken.md"
        mock_file.read_text.side_effect = Exception("read error")
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_file]
        mock_resources.files.return_value = mock_agent_store

        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        names = [p["name"] for p in result]
        assert "broken" in names
        broken_profile = [p for p in result if p["name"] == "broken"][0]
        assert broken_profile["description"] == ""
        assert broken_profile["source"] == "built-in"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_builtin_store_exception_handled(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Exception scanning built-in store should be caught, not crash."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_resources.files.side_effect = Exception("No built-in store")
        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        assert result == []

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_skips_provider_dir_same_as_local(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Provider directory that matches local store should be skipped."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_resources.files.return_value = MagicMock(iterdir=MagicMock(return_value=[]))

        # Use a real, resolved path so Path(dir_path).resolve() matches
        # the mock's resolve() return value (avoids macOS symlink issues
        # where e.g. /home -> /System/Volumes/Data/home).
        resolved_path = Path(__file__).resolve().parent
        mock_local_dir.exists.return_value = True
        mock_local_dir.resolve.return_value = resolved_path

        # Provider dir resolves to same as local
        mock_get_dirs.return_value = {"kiro_cli": str(resolved_path)}

        list_agent_profiles()

        # _scan_directory should be called for local but NOT for kiro_cli
        scan_calls = [c[0][1] for c in mock_scan.call_args_list]
        assert "local" in scan_calls
        assert "kiro" not in scan_calls

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_extra_dirs_scanned(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Extra user directories should be scanned with 'custom' label."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_resources.files.return_value = MagicMock(iterdir=MagicMock(return_value=[]))
        mock_local_dir.exists.return_value = False
        mock_get_extra.return_value = ["/extra/dir1", "/extra/dir2"]

        list_agent_profiles()

        custom_calls = [c for c in mock_scan.call_args_list if c[0][1] == "custom"]
        assert len(custom_calls) == 2

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_non_md_builtin_files_skipped(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_dirs, mock_get_extra
    ):
        """Non-md files in built-in store should be ignored."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_py_file = MagicMock()
        mock_py_file.name = "__init__.py"
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_py_file]
        mock_resources.files.return_value = mock_agent_store
        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        assert result == []
