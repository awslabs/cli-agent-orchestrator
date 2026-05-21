"""Tests for Claude Code plugin marketplace auto-discovery."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.utils.agent_profiles import (
    _discover_claude_plugin_agent_dirs,
    _reset_plugin_discovery_cache,
    list_agent_profiles,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the plugin discovery cache before each test."""
    _reset_plugin_discovery_cache()
    yield
    _reset_plugin_discovery_cache()


def _setup_marketplace(
    home: Path,
    plugins: list,
    enabled: dict,
    mkt_name="aim",
    mkt_source_type="directory",
    extra_settings=None,
):
    """Helper to create a fake marketplace structure under a fake home."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    mkt_path = home / ".aim" / "cc-plugins"
    mkt_path.mkdir(parents=True)

    settings = {
        "extraKnownMarketplaces": {
            mkt_name: {"source": {"path": str(mkt_path), "source": mkt_source_type}}
        },
        "enabledPlugins": enabled,
    }
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
    (mkt_meta_dir / "marketplace.json").write_text(
        json.dumps({"name": mkt_name, "plugins": mkt_json_plugins})
    )
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
        _setup_marketplace(
            tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": True}, mkt_source_type="git"
        )
        assert _discover_claude_plugin_agent_dirs() == []

    def test_marketplace_path_not_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "aim": {"source": {"path": "/nonexistent/path", "source": "directory"}}
                    },
                    "enabledPlugins": {},
                }
            )
        )
        assert _discover_claude_plugin_agent_dirs() == []

    def test_marketplace_json_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / ".aim" / "cc-plugins"
        mkt_path.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "aim": {"source": {"path": str(mkt_path), "source": "directory"}}
                    },
                    "enabledPlugins": {"p1@aim": True},
                }
            )
        )
        assert _discover_claude_plugin_agent_dirs() == []

    def test_plugin_not_enabled_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": False})
        assert _discover_claude_plugin_agent_dirs() == []

    def test_plugin_enabled_agents_dir_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mkt_path = _setup_marketplace(
            tmp_path, [{"name": "myplugin", "has_agents": True}], {"myplugin@aim": True}
        )
        result = _discover_claude_plugin_agent_dirs()
        assert len(result) == 1
        agents_dir, plugin_root = result[0]
        assert agents_dir == (mkt_path / "myplugin" / "agents").resolve()
        assert plugin_root == mkt_path.resolve()

    def test_plugin_enabled_agents_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Plugin exists but no agents/ subdir
        _setup_marketplace(
            tmp_path, [{"name": "noagents", "has_agents": False}], {"noagents@aim": True}
        )
        assert _discover_claude_plugin_agent_dirs() == []

    def test_multiple_marketplaces_multiple_plugins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)

        # Marketplace 1
        mkt1 = tmp_path / "mkt1"
        mkt1.mkdir()
        (mkt1 / ".claude-plugin").mkdir()
        (mkt1 / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "m1",
                    "plugins": [
                        {"name": "a", "source": "./a", "version": "1"},
                        {"name": "b", "source": "./b", "version": "1"},
                    ],
                }
            )
        )
        (mkt1 / "a" / "agents").mkdir(parents=True)
        (mkt1 / "a" / "agents" / "a-agent.md").write_text("---\nname: a-agent\n---\n")
        (mkt1 / "b" / "agents").mkdir(parents=True)
        (mkt1 / "b" / "agents" / "b-agent.md").write_text("---\nname: b-agent\n---\n")

        # Marketplace 2
        mkt2 = tmp_path / "mkt2"
        mkt2.mkdir()
        (mkt2 / ".claude-plugin").mkdir()
        (mkt2 / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "m2",
                    "plugins": [
                        {"name": "c", "source": "./c", "version": "1"},
                    ],
                }
            )
        )
        (mkt2 / "c" / "agents").mkdir(parents=True)
        (mkt2 / "c" / "agents" / "c-agent.md").write_text("---\nname: c-agent\n---\n")

        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "m1": {"source": {"path": str(mkt1), "source": "directory"}},
                        "m2": {"source": {"path": str(mkt2), "source": "directory"}},
                    },
                    "enabledPlugins": {"a@m1": True, "b@m1": True, "c@m2": True},
                }
            )
        )

        result = _discover_claude_plugin_agent_dirs()
        assert len(result) == 3

    def test_path_traversal_blocked(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / "mkt"
        mkt_path.mkdir()
        (mkt_path / ".claude-plugin").mkdir()
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "evil",
                    "plugins": [
                        {"name": "bad", "source": "../../../../etc", "version": "1"},
                    ],
                }
            )
        )
        # Create agents dir at the traversal target to ensure it would match
        # if containment check didn't exist
        (tmp_path / "etc" / "agents").mkdir(parents=True, exist_ok=True)

        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "evil": {"source": {"path": str(mkt_path), "source": "directory"}}
                    },
                    "enabledPlugins": {"bad@evil": True},
                }
            )
        )

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
            tmp_path, [{"name": "testpkg", "has_agents": True}], {"testpkg@aim": True}
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
        _setup_marketplace(tmp_path, [{"name": "mypkg", "has_agents": True}], {"mypkg@aim": True})
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
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}], enabled={})
        assert _discover_claude_plugin_agent_dirs() == []

    def test_orphan_enabled_plugin_no_matching_marketplace(self, tmp_path, monkeypatch):
        """enabledPlugins has plugin@X but no marketplace named X."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(
            tmp_path,
            [{"name": "p1", "has_agents": True}],
            enabled={"p1@nonexistent": True},
            mkt_name="aim",
        )
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
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "aim",
                    "plugins": [{"name": "fp", "source": "./plugin-file", "version": "1"}],
                }
            )
        )
        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "aim": {"source": {"path": str(mkt_path), "source": "directory"}}
                    },
                    "enabledPlugins": {"fp@aim": True},
                }
            )
        )
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
        (mkt_a / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "alpha",
                    "plugins": [{"name": "shared", "source": "./shared", "version": "1"}],
                }
            )
        )
        (mkt_a / "shared" / "agents").mkdir(parents=True)
        (mkt_a / "shared" / "agents" / "dup.md").write_text(
            "---\nname: dup\ndescription: from alpha\n---\nAlpha prompt"
        )

        # Marketplace B
        mkt_b = tmp_path / "mkt_b"
        mkt_b.mkdir()
        (mkt_b / ".claude-plugin").mkdir()
        (mkt_b / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "beta",
                    "plugins": [{"name": "shared", "source": "./shared", "version": "1"}],
                }
            )
        )
        (mkt_b / "shared" / "agents").mkdir(parents=True)
        (mkt_b / "shared" / "agents" / "dup.md").write_text(
            "---\nname: dup\ndescription: from beta\n---\nBeta prompt"
        )

        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "alpha": {"source": {"path": str(mkt_a), "source": "directory"}},
                        "beta": {"source": {"path": str(mkt_b), "source": "directory"}},
                    },
                    "enabledPlugins": {"shared@alpha": True, "shared@beta": True},
                }
            )
        )

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

    def test_symlink_escape_in_agents_dir(self, tmp_path, monkeypatch, caplog):
        """Symlink inside agents/ pointing outside plugin root — file is now blocked.

        Per-file containment check rejects files that resolve outside the
        marketplace root.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / "mkt"
        mkt_path.mkdir()
        (mkt_path / ".claude-plugin").mkdir()
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {"name": "aim", "plugins": [{"name": "sym", "source": "./sym", "version": "1"}]}
            )
        )
        (mkt_path / "sym" / "agents").mkdir(parents=True)

        # Create an external file and symlink to it from agents/
        external = tmp_path / "external"
        external.mkdir()
        (external / "evil.md").write_text("---\nname: evil\ndescription: escaped\n---\nEvil")
        (mkt_path / "sym" / "agents" / "evil.md").symlink_to(external / "evil.md")

        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "aim": {"source": {"path": str(mkt_path), "source": "directory"}}
                    },
                    "enabledPlugins": {"sym@aim": True},
                }
            )
        )

        # The agents dir IS returned (plugin dir containment passes)
        result = _discover_claude_plugin_agent_dirs()
        assert len(result) == 1
        # The symlinked file is now blocked by per-file containment
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

        import logging

        with caplog.at_level(logging.WARNING):
            profiles = list_agent_profiles()
        names = {p["name"] for p in profiles}
        assert "evil" not in names
        assert "resolves outside plugin root" in caplog.text


class TestReadAgentProfileSourcePluginIntegration:
    """Integration: _read_agent_profile_source falls through plugin dirs."""

    def test_agent_found_in_plugin_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from cli_agent_orchestrator.utils.agent_profiles import _read_agent_profile_source

        _setup_marketplace(tmp_path, [{"name": "pkg", "has_agents": True}], {"pkg@aim": True})
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

        _setup_marketplace(tmp_path, [{"name": "pkg", "has_agents": True}], {"pkg@aim": True})
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


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
class TestPluginPerFileContainment:
    """Tests for per-file path containment in plugin agent scanning."""

    def test_symlink_outside_plugin_root_rejected(self, tmp_path, monkeypatch, caplog):
        """Symlink inside agents/ pointing outside marketplace root is skipped with warning."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / "mkt"
        mkt_path.mkdir()
        (mkt_path / ".claude-plugin").mkdir()
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {"name": "aim", "plugins": [{"name": "p1", "source": "./p1", "version": "1"}]}
            )
        )
        agents_dir = mkt_path / "p1" / "agents"
        agents_dir.mkdir(parents=True)
        # Real file inside plugin root
        (agents_dir / "safe.md").write_text("---\nname: safe\ndescription: Safe\n---\nOK")
        # Symlink escaping to outside
        external = tmp_path / "outside"
        external.mkdir()
        (external / "escape.md").write_text("---\nname: escape\ndescription: Bad\n---\nEvil")
        (agents_dir / "escape.md").symlink_to(external / "escape.md")

        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "aim": {"source": {"path": str(mkt_path), "source": "directory"}}
                    },
                    "enabledPlugins": {"p1@aim": True},
                }
            )
        )

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

        import logging

        with caplog.at_level(logging.WARNING):
            profiles = list_agent_profiles()
        names = {p["name"] for p in profiles}
        assert "safe" in names
        assert "escape" not in names
        assert "resolves outside plugin root" in caplog.text

    def test_symlink_within_plugin_root_accepted(self, tmp_path, monkeypatch):
        """Symlink inside agents/ pointing within marketplace root is accepted."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / "mkt"
        mkt_path.mkdir()
        (mkt_path / ".claude-plugin").mkdir()
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {"name": "aim", "plugins": [{"name": "p1", "source": "./p1", "version": "1"}]}
            )
        )
        agents_dir = mkt_path / "p1" / "agents"
        agents_dir.mkdir(parents=True)
        # Real file
        (agents_dir / "real.md").write_text("---\nname: real\ndescription: Real\n---\nBody")
        # Symlink within the marketplace root (points to sibling)
        (agents_dir / "alias.md").symlink_to(agents_dir / "real.md")

        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "aim": {"source": {"path": str(mkt_path), "source": "directory"}}
                    },
                    "enabledPlugins": {"p1@aim": True},
                }
            )
        )

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

        profiles = list_agent_profiles()
        names = {p["name"] for p in profiles}
        # Both real and alias are within the root, both accepted
        assert "real" in names
        assert "alias" in names


class TestPluginDiscoveryCache:
    """Tests for mtime-based caching of _discover_claude_plugin_agent_dirs."""

    def test_cache_hit_avoids_recomputation(self, tmp_path, monkeypatch):
        """Second call with unchanged filesystem returns cached result without recomputing."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": True})

        # First call — populates cache
        result1 = _discover_claude_plugin_agent_dirs()
        assert len(result1) == 1

        # Spy on _compute_plugin_discovery
        call_count = {"n": 0}
        from cli_agent_orchestrator.utils.agent_profiles import _compute_plugin_discovery

        original_compute = _compute_plugin_discovery

        def counting_compute(*args, **kwargs):
            call_count["n"] += 1
            return original_compute(*args, **kwargs)

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles._compute_plugin_discovery",
            counting_compute,
        )

        # Second call — should hit cache
        result2 = _discover_claude_plugin_agent_dirs()
        assert result2 == result1
        assert call_count["n"] == 0

    def test_cache_invalidated_on_settings_mtime_change(self, tmp_path, monkeypatch):
        """Touching settings.json invalidates the cache."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": True})

        # First call
        result1 = _discover_claude_plugin_agent_dirs()
        assert len(result1) == 1

        # Touch settings.json to bump mtime
        settings_path = tmp_path / ".claude" / "settings.json"
        import time

        time.sleep(0.01)
        os.utime(settings_path, None)

        # Spy on _compute_plugin_discovery
        call_count = {"n": 0}
        from cli_agent_orchestrator.utils.agent_profiles import _compute_plugin_discovery

        original_compute = _compute_plugin_discovery

        def counting_compute(*args, **kwargs):
            call_count["n"] += 1
            return original_compute(*args, **kwargs)

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles._compute_plugin_discovery",
            counting_compute,
        )

        # Second call — should recompute
        result2 = _discover_claude_plugin_agent_dirs()
        assert call_count["n"] == 1
        assert result2 == result1

    def test_cache_invalidated_on_marketplace_json_mtime_change(self, tmp_path, monkeypatch):
        """Touching marketplace.json invalidates the cache."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mkt_path = _setup_marketplace(
            tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": True}
        )

        # First call
        result1 = _discover_claude_plugin_agent_dirs()
        assert len(result1) == 1

        # Touch marketplace.json
        marketplace_json = mkt_path / ".claude-plugin" / "marketplace.json"
        import time

        time.sleep(0.01)
        os.utime(marketplace_json, None)

        # Spy on _compute_plugin_discovery
        call_count = {"n": 0}
        from cli_agent_orchestrator.utils.agent_profiles import _compute_plugin_discovery

        original_compute = _compute_plugin_discovery

        def counting_compute(*args, **kwargs):
            call_count["n"] += 1
            return original_compute(*args, **kwargs)

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles._compute_plugin_discovery",
            counting_compute,
        )

        # Second call — should recompute
        result2 = _discover_claude_plugin_agent_dirs()
        assert call_count["n"] == 1
        assert result2 == result1

    def test_reset_plugin_discovery_cache_clears_state(self, tmp_path, monkeypatch):
        """Explicit test that _reset_plugin_discovery_cache() forces recomputation."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": True})

        # First call populates cache
        result1 = _discover_claude_plugin_agent_dirs()
        assert len(result1) == 1

        # Explicitly clear the cache
        _reset_plugin_discovery_cache()

        # Spy on _compute_plugin_discovery
        call_count = {"n": 0}
        from cli_agent_orchestrator.utils.agent_profiles import _compute_plugin_discovery

        original_compute = _compute_plugin_discovery

        def counting_compute(*args, **kwargs):
            call_count["n"] += 1
            return original_compute(*args, **kwargs)

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles._compute_plugin_discovery",
            counting_compute,
        )

        # Second call should recompute because cache was reset
        result2 = _discover_claude_plugin_agent_dirs()
        assert call_count["n"] == 1
        assert result2 == result1

    def test_cache_invalidates_when_new_marketplace_added(self, tmp_path, monkeypatch):
        """Adding a new marketplace (new settings.json entry) triggers re-discovery."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mkt_path = _setup_marketplace(
            tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": True}
        )

        # First call — one marketplace visible
        result1 = _discover_claude_plugin_agent_dirs()
        assert len(result1) == 1

        # Add a second marketplace to settings.json (bumps settings.json mtime)
        mkt2_path = tmp_path / ".aim" / "cc-plugins-b"
        mkt2_path.mkdir(parents=True)
        (mkt2_path / ".claude-plugin").mkdir()
        (mkt2_path / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {"name": "bim", "plugins": [{"name": "p2", "source": "./p2", "version": "1.0"}]}
            )
        )
        (mkt2_path / "p2" / "agents").mkdir(parents=True)
        (mkt2_path / "p2" / "agents" / "p2-agent.md").write_text(
            "---\nname: p2-agent\ndescription: From bim\n---\nPrompt"
        )
        # Rewrite settings.json with both marketplaces
        import time

        time.sleep(0.01)
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "aim": {"source": {"path": str(mkt_path), "source": "directory"}},
                        "bim": {"source": {"path": str(mkt2_path), "source": "directory"}},
                    },
                    "enabledPlugins": {"p1@aim": True, "p2@bim": True},
                }
            )
        )

        # Second call — cache invalidated by settings.json mtime change
        result2 = _discover_claude_plugin_agent_dirs()
        assert len(result2) == 2
        # Both agent dirs present, by plugin_root
        roots = {str(pr) for _, pr in result2}
        assert str(mkt_path.resolve()) in roots
        assert str(mkt2_path.resolve()) in roots

    def test_cache_invalidates_when_marketplace_json_disappears(self, tmp_path, monkeypatch):
        """A marketplace.json that disappears between calls triggers re-discovery."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mkt_path = _setup_marketplace(
            tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": True}
        )

        # First call — populates cache with one marketplace.json mtime
        result1 = _discover_claude_plugin_agent_dirs()
        assert len(result1) == 1

        # Delete the marketplace.json (settings.json unchanged)
        marketplace_json = mkt_path / ".claude-plugin" / "marketplace.json"
        marketplace_json.unlink()

        # Second call — the cached mtime was a float; _get_mtime now returns None.
        # Cache key no longer matches → re-discover. Result shrinks to [].
        result2 = _discover_claude_plugin_agent_dirs()
        assert result2 == []


class TestReadAgentProfileSourcePerFileContainment:
    """Fix A: _read_agent_profile_source per-file containment check for plugin agents."""

    @pytest.mark.skipif(
        sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
    )
    def test_read_agent_profile_source_rejects_symlink_escape(self, tmp_path, monkeypatch, caplog):
        """load_agent_profile('escape') where escape.md is a symlink outside plugin root.

        NOTE: The per-file check added in Fix A to _read_agent_profile_source
        (the logger.warning + continue branch) is effectively defense-in-depth
        behind _safe_join, which already filters symlink escapes during the
        _lookup_in_directory call. So the user-visible effect is the same
        (FileNotFoundError) regardless of which layer rejects — test asserts
        the correct end behavior.
        """
        from cli_agent_orchestrator.utils.agent_profiles import _read_agent_profile_source

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        mkt_path = tmp_path / "mkt"
        mkt_path.mkdir()
        (mkt_path / ".claude-plugin").mkdir()
        (mkt_path / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {"name": "aim", "plugins": [{"name": "p1", "source": "./p1", "version": "1"}]}
            )
        )
        agents_dir = mkt_path / "p1" / "agents"
        agents_dir.mkdir(parents=True)
        # Target outside the marketplace root
        external = tmp_path / "outside"
        external.mkdir()
        (external / "escape.md").write_text("---\nname: escape\n---\nEvil")
        (agents_dir / "escape.md").symlink_to(external / "escape.md")

        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "aim": {"source": {"path": str(mkt_path), "source": "directory"}}
                    },
                    "enabledPlugins": {"p1@aim": True},
                }
            )
        )

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
            _read_agent_profile_source("escape")

    def test_read_agent_profile_source_accepts_regular_plugin_file(self, tmp_path, monkeypatch):
        """Sanity check: a regular (non-symlinked) plugin agent file is returned."""
        from cli_agent_orchestrator.utils.agent_profiles import _read_agent_profile_source

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _setup_marketplace(tmp_path, [{"name": "p1", "has_agents": True}], {"p1@aim": True})
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

        result = _read_agent_profile_source("p1-agent")
        assert "Agent from p1" in result


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlinks require elevated privileges on Windows"
)
class TestFixAScopeIsNarrow:
    """Fix A stays narrow: only claude_plugin path gets per-file containment.

    A symlink escape inside a non-plugin agent dir (e.g. ~/.kiro/agents/) must
    NOT be rejected, because _scan_directory (the non-plugin path) has no
    containment check. If a future refactor accidentally broadens Fix A to all
    callers, this test catches the regression.
    """

    def test_symlink_escape_in_non_plugin_dir_not_blocked(self, tmp_path, monkeypatch):
        """Symlink in a provider dir (not a plugin) is still picked up by _scan_directory."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Provider agents dir containing a symlink that escapes
        kiro_agents = tmp_path / "kiro-agents"
        kiro_agents.mkdir()
        external = tmp_path / "outside-kiro"
        external.mkdir()
        (external / "evil.md").write_text("---\nname: evil\ndescription: Escaped\n---\nOK")
        (kiro_agents / "evil.md").symlink_to(external / "evil.md")

        # No plugin discovery (we want to isolate the _scan_directory path)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles._discover_claude_plugin_agent_dirs",
            lambda: [],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"kiro_cli": str(kiro_agents)},
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

        profiles = list_agent_profiles()
        names = {p["name"] for p in profiles}
        # 'evil' IS present — the fix deliberately did not broaden the check to
        # non-plugin paths. This asserts the scope stays narrow.
        assert "evil" in names


class TestParseAgentProfileTextMultipleComments:
    """Fix C extra coverage: three+ consecutive HTML comments stripped."""

    def test_three_leading_html_comments_stripped(self):
        from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

        text = (
            "<!-- one -->\n"
            "<!-- two -->\n"
            "<!-- three -->\n"
            "---\n"
            "name: triple\n"
            "description: Triple\n"
            "---\n"
            "body"
        )
        profile = parse_agent_profile_text(text, "triple")
        assert profile.name == "triple"
        assert profile.description == "Triple"
        assert profile.system_prompt == "body"
