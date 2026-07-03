"""Tests for config_service — the unified ConfigService (issue #357).

Covers the precedence chain (env > file > default), legacy config.json +
settings.json migration, and the memory.compile_mode env/file conflict named
in the issue.
"""

import json

import pytest

from cli_agent_orchestrator.services import config_service as cs
from cli_agent_orchestrator.services.config_service import ConfigService


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Redirect both the unified file and the legacy config.json to temp paths.

    Prevents a real dev/CI machine's ``~/.aws/cli-agent-orchestrator/{settings,config}.json``
    from leaking into these tests, and clears every env var this module reads
    so each test starts from a clean slate.
    """
    fake_settings = tmp_path / "settings.json"
    fake_legacy = tmp_path / "config.json"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake_settings
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(cs, "LEGACY_CONFIG_FILE", fake_legacy)
    for env_name in cs.ENV_REGISTRY:
        monkeypatch.delenv(env_name, raising=False)
    return {"settings": fake_settings, "legacy": fake_legacy}


class TestPrecedence:
    """CLI override > CAO_* env var > settings.json > built-in default."""

    def test_returns_builtin_default_when_nothing_set(self, _isolated_settings):
        assert ConfigService.get("terminal.backend") == "tmux"

    def test_file_value_beats_default(self, _isolated_settings):
        _isolated_settings["settings"].write_text(json.dumps({"terminal": {"backend": "herdr"}}))
        assert ConfigService.get("terminal.backend") == "herdr"

    def test_env_var_beats_file_value(self, _isolated_settings, monkeypatch):
        _isolated_settings["settings"].write_text(json.dumps({"terminal": {"backend": "herdr"}}))
        monkeypatch.setenv("CAO_TERMINAL_BACKEND", "tmux")
        assert ConfigService.get("terminal.backend") == "tmux"

    def test_cli_override_beats_env_var(self, _isolated_settings, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_BACKEND", "herdr")
        assert ConfigService.get("terminal.backend", override="tmux") == "tmux"

    def test_env_var_beats_default_when_no_file(self, _isolated_settings, monkeypatch):
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        assert ConfigService.get("apps.enabled") is True

    def test_invalid_env_value_falls_back_to_file(self, _isolated_settings, monkeypatch):
        """A malformed env value (e.g. non-numeric int) is ignored, not raised."""
        _isolated_settings["settings"].write_text(
            json.dumps({"server": {"mcp_request_timeout": 45}})
        )
        monkeypatch.setenv("CAO_MCP_REQUEST_TIMEOUT", "not-a-number")
        assert ConfigService.get("server.mcp_request_timeout") == 45


class TestLegacyMigration:
    """On first read, legacy config.json's terminal_backend/herdr_session
    should be folded into the unified settings.json under 'terminal'."""

    def test_migrates_legacy_config_json_into_settings_json(self, _isolated_settings):
        _isolated_settings["legacy"].write_text(
            json.dumps({"terminal_backend": "herdr", "herdr_session": "my-sess"})
        )
        assert ConfigService.get("terminal.backend") == "herdr"
        assert ConfigService.get("terminal.herdr_session") == "my-sess"

        # The migration persisted into settings.json itself.
        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["terminal"] == {"backend": "herdr", "herdr_session": "my-sess"}

    def test_no_migration_when_settings_json_already_has_terminal_section(self, _isolated_settings):
        """Once migrated (or hand-configured), the legacy file is not re-read."""
        _isolated_settings["settings"].write_text(json.dumps({"terminal": {"backend": "tmux"}}))
        _isolated_settings["legacy"].write_text(json.dumps({"terminal_backend": "herdr"}))
        assert ConfigService.get("terminal.backend") == "tmux"

    def test_missing_legacy_file_is_a_noop(self, _isolated_settings):
        assert not _isolated_settings["legacy"].exists()
        assert ConfigService.get("terminal.backend") == "tmux"

    def test_malformed_legacy_file_falls_back_to_default(self, _isolated_settings):
        _isolated_settings["legacy"].write_text("not valid json {{{")
        assert ConfigService.get("terminal.backend") == "tmux"


class TestMemoryCompileModeConflict:
    """CAO_MEMORY_COMPILE_MODE must win over a conflicting settings.json value."""

    def test_env_var_wins_over_file_value(self, _isolated_settings, monkeypatch):
        _isolated_settings["settings"].write_text(json.dumps({"memory": {"compile_mode": "llm"}}))
        monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "append")
        assert ConfigService.get("memory.compile_mode") == "append"

    def test_file_value_used_when_no_env_var(self, _isolated_settings):
        _isolated_settings["settings"].write_text(
            json.dumps({"memory": {"compile_mode": "append"}})
        )
        assert ConfigService.get("memory.compile_mode") == "append"

    def test_default_llm_when_neither_set(self, _isolated_settings):
        assert ConfigService.get("memory.compile_mode") == "llm"


class TestGetConfig:
    """ConfigService.get_config() assembles a validated CAOConfig."""

    def test_assembles_full_typed_config(self, _isolated_settings, monkeypatch):
        _isolated_settings["settings"].write_text(
            json.dumps(
                {
                    "terminal": {"backend": "herdr", "herdr_session": "s1"},
                    "server": {"mcp_request_timeout": 99},
                }
            )
        )
        monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "append")
        cfg = ConfigService.get_config()
        assert cfg.terminal.backend == "herdr"
        assert cfg.terminal.herdr_session == "s1"
        assert cfg.server.mcp_request_timeout == 99
        assert cfg.memory.compile_mode == "append"
        # Untouched sections keep built-in defaults.
        assert cfg.apps.enabled is False
        assert cfg.logging.level == "INFO"


class TestSetAndPath:
    def test_set_persists_and_get_reads_it_back(self, _isolated_settings):
        ConfigService.set("terminal.backend", "herdr")
        assert ConfigService.get("terminal.backend") == "herdr"
        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["terminal"]["backend"] == "herdr"

    def test_set_agents_extra_dirs_routes_through_settings_service(self, _isolated_settings):
        ConfigService.set("agents.extra_dirs", ["/a", "/b"])
        assert ConfigService.get("agents.extra_dirs") == ["/a", "/b"]

    def test_path_returns_settings_service_settings_file(self, _isolated_settings):
        assert ConfigService.path() == _isolated_settings["settings"]


class TestListAll:
    def test_list_all_includes_known_sections(self, _isolated_settings):
        result = ConfigService.list_all()
        assert "terminal.backend" in result
        assert "memory.compile_mode" in result
        assert "server.mcp_request_timeout" in result
        assert result["terminal.backend"] == "tmux"
