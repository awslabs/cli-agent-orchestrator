"""Unit tests for BackendFactory — config-driven backend selection."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.backends.factory import BackendFactory, ConfigurationError
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend


class TestBackendFactoryDefaults:
    """Test default behavior when config is absent or incomplete."""

    def test_returns_tmux_when_config_missing(self, tmp_path):
        """TmuxBackend is returned when config file doesn't exist."""
        nonexistent = tmp_path / "config.json"
        backend = BackendFactory.create(config_path=nonexistent)
        assert isinstance(backend, TmuxBackend)

    def test_returns_tmux_when_key_absent(self, tmp_path):
        """TmuxBackend is returned when terminal_backend key is missing."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"other_setting": "value"}))
        backend = BackendFactory.create(config_path=config_file)
        assert isinstance(backend, TmuxBackend)

    def test_returns_tmux_when_value_is_tmux(self, tmp_path):
        """TmuxBackend is returned when terminal_backend is explicitly 'tmux'."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"terminal_backend": "tmux"}))
        backend = BackendFactory.create(config_path=config_file)
        assert isinstance(backend, TmuxBackend)


class TestBackendFactoryHerdr:
    """Test herdr backend selection."""

    def test_returns_herdr_when_configured(self, tmp_path):
        """HerdrBackend is returned when terminal_backend is 'herdr'."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"terminal_backend": "herdr"}))
        # Patch os.path.exists so HerdrBackend.__init__ -> _ensure_session_running
        # finds the session socket and skips the subprocess.Popen(["herdr", ...])
        # startup, which would raise FileNotFoundError where herdr is not installed
        # (e.g. CI). Mirrors the fixture in test_herdr_backend.py.
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=True,
        ):
            backend = BackendFactory.create(config_path=config_file)
        assert isinstance(backend, HerdrBackend)


class TestBackendFactoryErrors:
    """Test error handling for invalid configs."""

    def test_raises_configuration_error_for_unknown_backend(self, tmp_path):
        """ConfigurationError raised for unrecognized backend names."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"terminal_backend": "screen"}))
        with pytest.raises(ConfigurationError, match="Unknown terminal_backend.*screen"):
            BackendFactory.create(config_path=config_file)

    def test_handles_malformed_json_gracefully(self, tmp_path):
        """Malformed JSON falls back to tmux default with a warning."""
        config_file = tmp_path / "config.json"
        config_file.write_text("not valid json {{{")
        backend = BackendFactory.create(config_path=config_file)
        assert isinstance(backend, TmuxBackend)

    def test_handles_empty_file_gracefully(self, tmp_path):
        """Empty file falls back to tmux default."""
        config_file = tmp_path / "config.json"
        config_file.write_text("")
        backend = BackendFactory.create(config_path=config_file)
        assert isinstance(backend, TmuxBackend)
