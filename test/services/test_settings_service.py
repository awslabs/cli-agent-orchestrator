"""Tests for settings_service module."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.settings_service import (
    _DEFAULTS,
    _load,
    _save,
    get_agent_dirs,
    get_extra_agent_dirs,
    set_agent_dirs,
    set_extra_agent_dirs,
)


@pytest.fixture
def settings_file(tmp_path):
    """Patch SETTINGS_FILE and CAO_HOME_DIR to use a temp directory."""
    fake_settings = tmp_path / "settings.json"
    with (
        patch(
            "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
            fake_settings,
        ),
        patch(
            "cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR",
            tmp_path,
        ),
    ):
        yield fake_settings


class TestLoad:
    """Tests for _load function."""

    def test_load_returns_empty_dict_when_file_does_not_exist(self, settings_file):
        """_load returns {} when the settings file does not exist."""
        assert not settings_file.exists()
        result = _load()
        assert result == {}

    def test_load_returns_empty_dict_when_file_is_corrupt_json(self, settings_file):
        """_load returns {} when file contains invalid JSON."""
        settings_file.write_text("not valid json {{{")
        result = _load()
        assert result == {}

    def test_load_returns_data_when_file_is_valid(self, settings_file):
        """_load returns parsed dict from a valid settings file."""
        data = {"agent_dirs": {"kiro_cli": "/custom/path"}, "extra_agent_dirs": ["/extra"]}
        settings_file.write_text(json.dumps(data))
        result = _load()
        assert result == data


class TestSave:
    """Tests for _save function."""

    def test_save_creates_file(self, settings_file):
        """_save writes JSON to the settings file."""
        data = {"key": "value"}
        _save(data)
        assert settings_file.exists()
        assert json.loads(settings_file.read_text()) == data

    def test_save_creates_parent_directory_if_needed(self, tmp_path):
        """_save creates parent directories if they don't exist yet."""
        nested_dir = tmp_path / "a" / "b" / "c"
        fake_settings = nested_dir / "settings.json"
        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
                fake_settings,
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR",
                nested_dir,
            ),
        ):
            _save({"hello": "world"})
            assert fake_settings.exists()
            assert json.loads(fake_settings.read_text()) == {"hello": "world"}

    def test_save_overwrites_existing_file(self, settings_file):
        """_save overwrites a previous settings file."""
        _save({"old": True})
        _save({"new": True})
        assert json.loads(settings_file.read_text()) == {"new": True}


class TestGetAgentDirs:
    """Tests for get_agent_dirs function."""

    def test_returns_defaults_when_no_settings_file(self, settings_file):
        """get_agent_dirs returns all default dirs when no settings file exists."""
        result = get_agent_dirs()
        assert result == _DEFAULTS

    def test_returns_saved_overrides_merged_with_defaults(self, settings_file):
        """get_agent_dirs merges saved overrides on top of defaults."""
        custom = {"kiro_cli": "/my/custom/kiro"}
        settings_file.write_text(json.dumps({"agent_dirs": custom}))
        result = get_agent_dirs()
        # The overridden key should have the custom value
        assert result["kiro_cli"] == "/my/custom/kiro"
        # Other defaults should be preserved
        assert result["q_cli"] == _DEFAULTS["q_cli"]
        assert result["claude_code"] == _DEFAULTS["claude_code"]
        assert result["codex"] == _DEFAULTS["codex"]

    def test_returns_all_default_keys(self, settings_file):
        """get_agent_dirs always returns all known provider keys."""
        result = get_agent_dirs()
        for key in _DEFAULTS:
            assert key in result


class TestSetAgentDirs:
    """Tests for set_agent_dirs function."""

    def test_updates_known_provider(self, settings_file):
        """set_agent_dirs updates a known provider and returns merged result."""
        result = set_agent_dirs({"q_cli": "/new/q/path"})
        assert result["q_cli"] == "/new/q/path"
        # Other defaults preserved
        assert result["kiro_cli"] == _DEFAULTS["kiro_cli"]

    def test_ignores_unknown_providers(self, settings_file):
        """set_agent_dirs ignores provider names not in _DEFAULTS."""
        result = set_agent_dirs({"unknown_provider": "/some/path"})
        assert "unknown_provider" not in result
        # All defaults unchanged
        assert result == _DEFAULTS

    def test_persists_to_disk_and_can_be_read_back(self, settings_file):
        """set_agent_dirs writes to disk; get_agent_dirs reads it back."""
        set_agent_dirs({"claude_code": "/persisted/path"})
        fresh = get_agent_dirs()
        assert fresh["claude_code"] == "/persisted/path"

    def test_multiple_updates_accumulate(self, settings_file):
        """Successive set_agent_dirs calls accumulate overrides."""
        set_agent_dirs({"kiro_cli": "/first"})
        set_agent_dirs({"q_cli": "/second"})
        result = get_agent_dirs()
        assert result["kiro_cli"] == "/first"
        assert result["q_cli"] == "/second"

    def test_mixed_known_and_unknown_providers(self, settings_file):
        """set_agent_dirs stores known and ignores unknown in a single call."""
        result = set_agent_dirs({"kiro_cli": "/yes", "bogus": "/no"})
        assert result["kiro_cli"] == "/yes"
        assert "bogus" not in result


class TestGetExtraAgentDirs:
    """Tests for get_extra_agent_dirs function."""

    def test_returns_empty_list_when_none_set(self, settings_file):
        """get_extra_agent_dirs returns [] when no extra dirs configured."""
        result = get_extra_agent_dirs()
        assert result == []

    def test_returns_saved_extra_dirs(self, settings_file):
        """get_extra_agent_dirs returns the saved list."""
        settings_file.write_text(json.dumps({"extra_agent_dirs": ["/a", "/b"]}))
        result = get_extra_agent_dirs()
        assert result == ["/a", "/b"]


class TestSetExtraAgentDirs:
    """Tests for set_extra_agent_dirs function."""

    def test_saves_and_returns_dirs(self, settings_file):
        """set_extra_agent_dirs saves dirs and returns them."""
        result = set_extra_agent_dirs(["/dir1", "/dir2"])
        assert result == ["/dir1", "/dir2"]

    def test_strips_empty_strings(self, settings_file):
        """set_extra_agent_dirs removes empty or whitespace-only strings."""
        result = set_extra_agent_dirs(["/valid", "", "  ", "/also-valid"])
        assert result == ["/valid", "/also-valid"]

    def test_persists_to_disk(self, settings_file):
        """set_extra_agent_dirs persists to disk so get_extra_agent_dirs reads it."""
        set_extra_agent_dirs(["/persisted"])
        assert get_extra_agent_dirs() == ["/persisted"]

    def test_replaces_previous_list(self, settings_file):
        """set_extra_agent_dirs replaces the entire previous list."""
        set_extra_agent_dirs(["/first"])
        set_extra_agent_dirs(["/second"])
        assert get_extra_agent_dirs() == ["/second"]

    def test_empty_list_clears_previous(self, settings_file):
        """Setting an empty list clears all extra dirs."""
        set_extra_agent_dirs(["/something"])
        set_extra_agent_dirs([])
        assert get_extra_agent_dirs() == []
