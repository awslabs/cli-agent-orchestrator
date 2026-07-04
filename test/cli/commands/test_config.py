"""Tests for the `cao config` CLI command group (issue #357)."""

import json

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.config import _coerce, config
from cli_agent_orchestrator.services import config_service as cs


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Redirect the unified/legacy files to temp paths and clear known env vars.

    Prevents a real dev/CI machine's ``~/.aws/cli-agent-orchestrator/*.json``
    from leaking into these tests.
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


@pytest.fixture
def runner():
    return CliRunner()


class TestCoerce:
    """_coerce() converts a raw CLI string into bool/int/float/JSON/str."""

    def test_coerces_true_to_bool(self):
        assert _coerce("true") is True

    def test_coerces_false_to_bool(self):
        assert _coerce("false") is False

    def test_coerces_true_case_insensitive(self):
        assert _coerce("TRUE") is True

    def test_coerces_int(self):
        assert _coerce("30") == 30
        assert isinstance(_coerce("30"), int)

    def test_int_checked_before_float(self):
        """A bare integer string must not become a float (30 != 30.0 in JSON output)."""
        result = _coerce("30")
        assert result == 30
        assert not isinstance(result, float)

    def test_coerces_float(self):
        result = _coerce("0.85")
        assert result == 0.85
        assert isinstance(result, float)

    def test_coerces_json_list(self):
        assert _coerce('["a", "b"]') == ["a", "b"]

    def test_coerces_json_dict(self):
        assert _coerce('{"k": "v"}') == {"k": "v"}

    def test_invalid_json_list_falls_back_to_string(self):
        """A value that merely starts with '[' but isn't valid JSON stays a string."""
        assert _coerce("[not valid") == "[not valid"

    def test_plain_string_passes_through(self):
        assert _coerce("tmux") == "tmux"

    def test_empty_string_passes_through(self):
        assert _coerce("") == ""


class TestConfigGet:
    def test_get_returns_builtin_default(self, runner, _isolated_settings):
        result = runner.invoke(config, ["get", "terminal.backend"])
        assert result.exit_code == 0
        assert json.loads(result.output) == "tmux"

    def test_get_reflects_env_override(self, runner, _isolated_settings, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_BACKEND", "herdr")
        result = runner.invoke(config, ["get", "terminal.backend"])
        assert result.exit_code == 0
        assert json.loads(result.output) == "herdr"

    def test_get_unknown_key_returns_null(self, runner, _isolated_settings):
        result = runner.invoke(config, ["get", "not.a.real.key"])
        assert result.exit_code == 0
        assert json.loads(result.output) is None


class TestConfigSet:
    def test_set_persists_string_value(self, runner, _isolated_settings):
        result = runner.invoke(config, ["set", "terminal.backend", "herdr"])
        assert result.exit_code == 0
        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["terminal"]["backend"] == "herdr"

    def test_set_persists_coerced_int(self, runner, _isolated_settings):
        result = runner.invoke(config, ["set", "server.mcp_request_timeout", "99"])
        assert result.exit_code == 0
        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["server"]["mcp_request_timeout"] == 99

    def test_set_invalid_memory_value_errors(self, runner, _isolated_settings):
        """set_memory_setting validates flush_threshold to (0.0, 1.0]; an
        out-of-range value must surface as a CLI error, not succeed silently."""
        result = runner.invoke(config, ["set", "memory.flush_threshold", "5.0"])
        assert result.exit_code != 0
        # Clean ClickException, not a leaked ValueError traceback (issue #357).
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "flush_threshold must be between 0.0 and 1.0" in result.output

    def test_set_unknown_memory_key_errors(self, runner, _isolated_settings):
        result = runner.invoke(config, ["set", "memory.not_a_real_key", "1"])
        assert result.exit_code != 0
        # Clean ClickException, not a leaked ValueError traceback (issue #357).
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Unknown memory setting" in result.output

    def test_set_network_key_succeeds_persists_and_warns(self, runner, _isolated_settings):
        """network.* is schema-only (no runtime effect yet) — set() still
        succeeds and persists, but must warn the operator on stderr."""
        result = runner.invoke(config, ["set", "network.allowed_hosts", '["cao.internal"]'])
        assert result.exit_code == 0
        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["network"]["allowed_hosts"] == ["cao.internal"]
        assert "no runtime effect yet" in result.stderr
        assert "network.allowed_hosts" in result.stderr

    def test_set_auth_key_warns_but_terminal_key_does_not(self, runner, _isolated_settings):
        auth_result = runner.invoke(
            config, ["set", "auth.jwks_uri", "https://idp.example/jwks.json"]
        )
        assert "no runtime effect yet" in auth_result.stderr

        terminal_result = runner.invoke(config, ["set", "terminal.backend", "herdr"])
        assert terminal_result.stderr == ""


class TestConfigList:
    def test_list_includes_known_keys(self, runner, _isolated_settings):
        result = runner.invoke(config, ["list"])
        assert result.exit_code == 0
        assert "terminal.backend = " in result.output
        assert "memory.compile_mode = " in result.output

    def test_list_surfaces_network_and_auth_values_despite_being_inert(
        self, runner, _isolated_settings
    ):
        """network.*/auth.* are stored-but-inert — `list` must still surface
        them so an operator can see what's set, even though it has no effect."""
        runner.invoke(config, ["set", "network.cors_origins", '["http://example.test"]'])
        runner.invoke(config, ["set", "auth.audience", "https://api.example"])

        result = runner.invoke(config, ["list"])
        assert result.exit_code == 0
        assert 'network.cors_origins = ["http://example.test"]' in result.output
        assert 'auth.audience = "https://api.example"' in result.output


class TestConfigPath:
    def test_path_prints_settings_file(self, runner, _isolated_settings):
        result = runner.invoke(config, ["path"])
        assert result.exit_code == 0
        assert result.output.strip() == str(_isolated_settings["settings"])
