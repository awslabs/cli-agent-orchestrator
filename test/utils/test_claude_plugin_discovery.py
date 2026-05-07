"""Tests for Claude Code plugin marketplace auto-discovery."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.utils.agent_profiles import (
    _discover_claude_plugin_agent_dirs,
    list_agent_profiles,
)


def _setup_marketplace(home: Path, plugins: list, enabled: dict, mkt_name="aim",
                       mkt_source_type="directory", extra_settings=None):
    """Helper to create a fake marketplace structure under a fake home."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    mkt_path = home / ".aim" / "cc-plugins"
    mkt_path.mkdir(parents=True)

    settings = {"extraKnownMarketplaces": {mkt_name: {"source": {
        "path": str(mkt_path), "source": mkt_source_type
    }}}, "enabledPlugins": enabled}
    if extra_settings:
        settings.update(extra_settings)
    (claude_dir / "settings.json").write_text(json.dumps(settings))

    # Create marketplace.json
    mkt_meta_dir = mkt_path / ".claude-plugin"
    mkt_meta_dir.mkdir(parents=True)
    mkt_json_plugins = []
    for p in plugins:
        name = p["name"]
        source = p.get("source", f"./{name}")
        mkt_json_plugins.append({"name": name, "source": source, "version": "1.0"})
        # Create plugin agents dir if requested
        if p.get("has_agents", False):
            agents_dir = mkt_path / source.lstrip("./") / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / f"{name}-agent.md").write_text(
                f"---\nname: {name}-agent\ndescription: Agent from {name}\n---\nPrompt"
            )
    (mkt_meta_dir / "marketplace.json").write_text(json.dumps({
        "name": mkt_name, "plugins": mkt_json_plugins
    }))
    return mkt_path


class TestDiscoverClaudePluginAgentDirs:
    """Unit tests for _discover_claude_plugin_agent_dirs."""

    def test_settings_json_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _discover_claude_plugin_agent_dirs() == []

    def test_no_extra_known_marketplaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(json.dumps({"someOtherKey": True}))
        assert _discover_claude_plugin_agent_dirs() == []

    def test_malformed_settings_json(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text("not valid json{{{")
        import logging
        with caplog.at_level(logging.WARNING):
            result = _discover_claude_plugin_agent_dirs()
        assert result == []
        assert "Failed to read" in caplog.text

    def test_non_directory_source_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}],
                           {"p1@aim": True}, mkt_source_type="git")
        assert _discover_claude_plugin_agent_dirs() == []

    def test_marketplace_path_not_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(json.dumps({
            "extraKnownMarketplaces": {"aim": {"source": {
                "path": "/nonexistent/path", "source": "directory"
            }}},
            "enabledPlugins": {}
        }))
        assert _discover_claude_plugin_agent_dirs() == []

    def test_marketplace_json_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / ".aim" / "cc-plugins"
        mkt_path.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(json.dumps({
            "extraKnownMarketplaces": {"aim": {"source": {
                "path": str(mkt_path), "source": "directory"
            }}},
            "enabledPlugins": {"p1@aim": True}
        }))
        assert _discover_claude_plugin_agent_dirs() == []

    def test_plugin_not_enabled_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}],
                           {"p1@aim": False})
        assert _discover_claude_plugin_agent_dirs() == []

    def test_plugin_enabled_agents_dir_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mkt_path = _setup_marketplace(
            tmp_path, [{"name": "myplugin", "has_agents": True}],
            {"myplugin@aim": True}
        )
        result = _discover_claude_plugin_agent_dirs()
        assert len(result) == 1
        assert result[0] == (mkt_path / "myplugin" / "agents").resolve()

    def test_plugin_enabled_agents_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Plugin exists but no agents/ subdir
        _setup_marketplace(tmp_path, [{"name": "noagents", "has_agents": False}],
                           {"noagents@aim": True})
        assert _discover_claude_plugin_agent_dirs() == []

    def test_multiple_marketplaces_multiple_plugins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)

        # Marketplace 1
        mkt1 = tmp_path / "mkt1"
        mkt1.mkdir()
        (mkt1 / ".claude-plugin").mkdir()
        (mkt1 / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "name": "m1", "plugins": [
                {"name": "a", "source": "./a", "version": "1"},
                {"name": "b", "source": "./b", "version": "1"},
            ]
        }))
        (mkt1 / "a" / "agents").mkdir(parents=True)
        (mkt1 / "a" / "agents" / "a-agent.md").write_text("---\nname: a-agent\n---\n")
        (mkt1 / "b" / "agents").mkdir(parents=True)
        (mkt1 / "b" / "agents" / "b-agent.md").write_text("---\nname: b-agent\n---\n")

        # Marketplace 2
        mkt2 = tmp_path / "mkt2"
        mkt2.mkdir()
        (mkt2 / ".claude-plugin").mkdir()
        (mkt2 / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "name": "m2", "plugins": [
                {"name": "c", "source": "./c", "version": "1"},
            ]
        }))
        (mkt2 / "c" / "agents").mkdir(parents=True)
        (mkt2 / "c" / "agents" / "c-agent.md").write_text("---\nname: c-agent\n---\n")

        (claude_dir / "settings.json").write_text(json.dumps({
            "extraKnownMarketplaces": {
                "m1": {"source": {"path": str(mkt1), "source": "directory"}},
                "m2": {"source": {"path": str(mkt2), "source": "directory"}},
            },
            "enabledPlugins": {"a@m1": True, "b@m1": True, "c@m2": True}
        }))

        result = _discover_claude_plugin_agent_dirs()
        assert len(result) == 3

    def test_path_traversal_blocked(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / "mkt"
        mkt_path.mkdir()
        (mkt_path / ".claude-plugin").mkdir()
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "name": "evil", "plugins": [
                {"name": "bad", "source": "../../../../etc", "version": "1"},
            ]
        }))
        # Create agents dir at the traversal target to ensure it would match
        # if containment check didn't exist
        (tmp_path / "etc" / "agents").mkdir(parents=True, exist_ok=True)

        (claude_dir / "settings.json").write_text(json.dumps({
            "extraKnownMarketplaces": {"evil": {"source": {
                "path": str(mkt_path), "source": "directory"
            }}},
            "enabledPlugins": {"bad@evil": True}
        }))

        import logging
        with caplog.at_level(logging.WARNING):
            result = _discover_claude_plugin_agent_dirs()
        assert result == []
        assert "escapes marketplace root" in caplog.text


class TestListAgentProfilesPluginIntegration:
    """Integration tests: plugin agents appear in list_agent_profiles."""

    def test_plugin_agent_appears_with_claude_plugin_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mkt_path = _setup_marketplace(
            tmp_path, [{"name": "testpkg", "has_agents": True}],
            {"testpkg@aim": True}
        )
        # Patch out other sources
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        from unittest.mock import MagicMock
        mock_store = MagicMock()
        mock_store.iterdir.return_value = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: mock_store,
        )

        result = list_agent_profiles()
        names = {p["name"] for p in result}
        assert "testpkg-agent" in names
        plugin_profile = next(p for p in result if p["name"] == "testpkg-agent")
        assert plugin_profile["source"] == "claude_plugin"

    def test_local_agent_beats_plugin_agent_in_dedup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(
            tmp_path, [{"name": "mypkg", "has_agents": True}],
            {"mypkg@aim": True}
        )
        # Create a local agent with the same name as the plugin agent
        local_store = tmp_path / "local-store"
        local_store.mkdir()
        (local_store / "mypkg-agent.md").write_text(
            "---\nname: mypkg-agent\ndescription: Local override\n---\nLocal prompt"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        from unittest.mock import MagicMock
        mock_store = MagicMock()
        mock_store.iterdir.return_value = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: mock_store,
        )

        result = list_agent_profiles()
        plugin_profile = next(p for p in result if p["name"] == "mypkg-agent")
        assert plugin_profile["source"] == "local"


class TestDiscoverEdgeCases:
    """Additional edge-case tests for _discover_claude_plugin_agent_dirs."""

    def test_empty_enabled_plugins_map(self, tmp_path, monkeypatch):
        """enabledPlugins present but empty → no dirs returned."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}],
                           enabled={})
        assert _discover_claude_plugin_agent_dirs() == []

    def test_orphan_enabled_plugin_no_matching_marketplace(self, tmp_path, monkeypatch):
        """enabledPlugins has plugin@X but no marketplace named X."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}],
                           enabled={"p1@nonexistent": True}, mkt_name="aim")
        assert _discover_claude_plugin_agent_dirs() == []

    def test_plugin_source_is_file_not_directory(self, tmp_path, monkeypatch):
        """Plugin source resolves to a regular file — agents/ can't exist."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / "mkt"
        mkt_path.mkdir()
        (mkt_path / ".claude-plugin").mkdir()
        # Create a file (not dir) at the plugin source path
        (mkt_path / "plugin-file").write_text("I am a file")
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "name": "aim", "plugins": [{"name": "fp", "source": "./plugin-file", "version": "1"}]
        }))
        (claude_dir / "settings.json").write_text(json.dumps({
            "extraKnownMarketplaces": {"aim": {"source": {"path": str(mkt_path), "source": "directory"}}},
            "enabledPlugins": {"fp@aim": True}
        }))
        assert _discover_claude_plugin_agent_dirs() == []

    def test_cross_marketplace_name_collision_first_wins(self, tmp_path, monkeypatch):
        """Two marketplaces each have a plugin with same agent name — first scanned wins."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)

        # Marketplace A (listed first in dict)
        mkt_a = tmp_path / "mkt_a"
        mkt_a.mkdir()
        (mkt_a / ".claude-plugin").mkdir()
        (mkt_a / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "name": "alpha", "plugins": [{"name": "shared", "source": "./shared", "version": "1"}]
        }))
        (mkt_a / "shared" / "agents").mkdir(parents=True)
        (mkt_a / "shared" / "agents" / "dup.md").write_text(
            "---\nname: dup\ndescription: from alpha\n---\nAlpha prompt")

        # Marketplace B
        mkt_b = tmp_path / "mkt_b"
        mkt_b.mkdir()
        (mkt_b / ".claude-plugin").mkdir()
        (mkt_b / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "name": "beta", "plugins": [{"name": "shared", "source": "./shared", "version": "1"}]
        }))
        (mkt_b / "shared" / "agents").mkdir(parents=True)
        (mkt_b / "shared" / "agents" / "dup.md").write_text(
            "---\nname: dup\ndescription: from beta\n---\nBeta prompt")

        (claude_dir / "settings.json").write_text(json.dumps({
            "extraKnownMarketplaces": {
                "alpha": {"source": {"path": str(mkt_a), "source": "directory"}},
                "beta": {"source": {"path": str(mkt_b), "source": "directory"}},
            },
            "enabledPlugins": {"shared@alpha": True, "shared@beta": True}
        }))

        # Both dirs returned — dedup happens at list_agent_profiles level
        result = _discover_claude_plugin_agent_dirs()
        assert len(result) == 2

        # Now test that list_agent_profiles dedup picks first
        from unittest.mock import MagicMock
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        mock_store = MagicMock()
        mock_store.iterdir.return_value = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: mock_store,
        )
        profiles = list_agent_profiles()
        dup_profile = next(p for p in profiles if p["name"] == "dup")
        assert dup_profile["description"] == "from alpha"

    def test_symlink_escape_in_agents_dir(self, tmp_path, monkeypatch):
        """Symlink inside agents/ pointing outside plugin root — file is still served.

        Note: containment check is on the plugin dir, not individual agent files.
        This test documents current behavior (no per-file containment).
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / "mkt"
        mkt_path.mkdir()
        (mkt_path / ".claude-plugin").mkdir()
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "name": "aim", "plugins": [{"name": "sym", "source": "./sym", "version": "1"}]
        }))
        (mkt_path / "sym" / "agents").mkdir(parents=True)

        # Create an external file and symlink to it from agents/
        external = tmp_path / "external"
        external.mkdir()
        (external / "evil.md").write_text("---\nname: evil\ndescription: escaped\n---\nEvil")
        (mkt_path / "sym" / "agents" / "evil.md").symlink_to(external / "evil.md")

        (claude_dir / "settings.json").write_text(json.dumps({
            "extraKnownMarketplaces": {"aim": {"source": {"path": str(mkt_path), "source": "directory"}}},
            "enabledPlugins": {"sym@aim": True}
        }))

        # The agents dir IS returned (plugin dir containment passes)
        result = _discover_claude_plugin_agent_dirs()
        assert len(result) == 1
        # The symlinked file IS scanned (no per-file containment check)
        from unittest.mock import MagicMock
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        mock_store = MagicMock()
        mock_store.iterdir.return_value = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: mock_store,
        )
        profiles = list_agent_profiles()
        names = {p["name"] for p in profiles}
        assert "evil" in names  # DEFECT: symlink escape not blocked at agent-file level


class TestReadAgentProfileSourcePluginIntegration:
    """Integration: _read_agent_profile_source falls through plugin dirs."""

    def test_agent_found_in_plugin_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from cli_agent_orchestrator.utils.agent_profiles import _read_agent_profile_source
        _setup_marketplace(tmp_path, [{"name": "pkg", "has_agents": True}],
                           {"pkg@aim": True})
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        from unittest.mock import MagicMock
        mock_store = MagicMock()
        mock_store.__truediv__ = lambda self, name: MagicMock(is_file=lambda: False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: mock_store,
        )
        result = _read_agent_profile_source("pkg-agent")
        assert "Agent from pkg" in result

    def test_agent_not_found_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from cli_agent_orchestrator.utils.agent_profiles import _read_agent_profile_source
        _setup_marketplace(tmp_path, [{"name": "pkg", "has_agents": True}],
                           {"pkg@aim": True})
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        from unittest.mock import MagicMock
        mock_store = MagicMock()
        mock_store.__truediv__ = lambda self, name: MagicMock(is_file=lambda: False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: mock_store,
        )
        with pytest.raises(FileNotFoundError):
            _read_agent_profile_source("nonexistent-agent")
