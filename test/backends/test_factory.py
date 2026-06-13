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


class TestBackendFactoryOverride:
    """Test the backend_override parameter (e.g. cao-server --terminal)."""

    def test_override_herdr_wins_over_tmux_config(self, tmp_path):
        """backend_override='herdr' beats terminal_backend='tmux' in config."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"terminal_backend": "tmux"}))
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=True,
        ):
            backend = BackendFactory.create(config_path=config_file, backend_override="herdr")
        assert isinstance(backend, HerdrBackend)

    def test_override_tmux_wins_over_herdr_config(self, tmp_path):
        """backend_override='tmux' beats terminal_backend='herdr' in config."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"terminal_backend": "herdr"}))
        backend = BackendFactory.create(config_path=config_file, backend_override="tmux")
        assert isinstance(backend, TmuxBackend)

    def test_override_works_without_config_file(self, tmp_path):
        """backend_override selects the backend even when no config file exists."""
        nonexistent = tmp_path / "config.json"
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=True,
        ):
            backend = BackendFactory.create(config_path=nonexistent, backend_override="herdr")
        assert isinstance(backend, HerdrBackend)

    def test_override_herdr_still_reads_herdr_session_from_config(self, tmp_path):
        """Override picks the backend; other config keys (herdr_session) still apply."""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"terminal_backend": "tmux", "herdr_session": "my-session"})
        )
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=True,
        ):
            backend = BackendFactory.create(config_path=config_file, backend_override="herdr")
        assert isinstance(backend, HerdrBackend)
        assert backend.herdr_session == "my-session"

    def test_unknown_override_raises_configuration_error(self, tmp_path):
        """An unrecognized override name raises ConfigurationError."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"terminal_backend": "tmux"}))
        with pytest.raises(ConfigurationError, match="Unknown terminal_backend.*screen"):
            BackendFactory.create(config_path=config_file, backend_override="screen")


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
