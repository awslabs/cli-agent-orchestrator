"""Unit tests for BackendFactory — config-driven backend selection.

BackendFactory reads ``terminal.backend`` / ``terminal.herdr_session`` via
ConfigService rather than a hardcoded config.json path (issue #357). Tests
patch ``settings_service.SETTINGS_FILE`` (the file ConfigService delegates to)
and clear the relevant CAO_* env vars so each test is isolated.
"""

import json

import pytest

from cli_agent_orchestrator.backends.factory import BackendFactory, ConfigurationError
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Point the unified settings file at a temp path and clear CAO_TERMINAL_BACKEND.

    Also redirects the legacy config.json path to a nonexistent temp file so a
    real ``~/.aws/cli-agent-orchestrator/config.json`` on the dev/CI machine
    can't leak into these tests via the legacy-migration path.
    """
    fake_settings = tmp_path / "settings.json"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake_settings
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.config_service.LEGACY_CONFIG_FILE",
        tmp_path / "config.json",
    )
    monkeypatch.delenv("CAO_TERMINAL_BACKEND", raising=False)
    monkeypatch.delenv("CAO_HERDR_SESSION", raising=False)
    return fake_settings


def _write_terminal_config(settings_file, backend=None, herdr_session=None):
    terminal = {}
    if backend is not None:
        terminal["backend"] = backend
    if herdr_session is not None:
        terminal["herdr_session"] = herdr_session
    settings_file.write_text(json.dumps({"terminal": terminal}))


class TestBackendFactoryDefaults:
    """Test default behavior when config is absent or incomplete."""

    def test_returns_tmux_when_config_missing(self, _isolated_settings):
        """TmuxBackend is returned when the settings file doesn't exist."""
        assert not _isolated_settings.exists()
        backend = BackendFactory.create()
        assert isinstance(backend, TmuxBackend)

    def test_returns_tmux_when_key_absent(self, _isolated_settings):
        """TmuxBackend is returned when terminal.backend key is missing."""
        _isolated_settings.write_text(json.dumps({"other_setting": "value"}))
        backend = BackendFactory.create()
        assert isinstance(backend, TmuxBackend)

    def test_returns_tmux_when_value_is_tmux(self, _isolated_settings):
        """TmuxBackend is returned when terminal.backend is explicitly 'tmux'."""
        _write_terminal_config(_isolated_settings, backend="tmux")
        backend = BackendFactory.create()
        assert isinstance(backend, TmuxBackend)


class TestBackendFactoryHerdr:
    """Test herdr backend selection."""

    def test_returns_herdr_when_configured(self, _isolated_settings):
        """HerdrBackend is returned when terminal.backend is 'herdr'."""
        _write_terminal_config(_isolated_settings, backend="herdr")
        # Patch os.path.exists so HerdrBackend.__init__ -> _ensure_session_running
        # finds the session socket and skips the subprocess.Popen(["herdr", ...])
        # startup, which would raise FileNotFoundError where herdr is not installed
        # (e.g. CI). Mirrors the fixture in test_herdr_backend.py.
        from unittest.mock import patch

        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=True,
        ):
            backend = BackendFactory.create()
        assert isinstance(backend, HerdrBackend)


class TestBackendFactoryOverride:
    """Test the backend_override parameter (e.g. cao-server --terminal)."""

    def test_override_herdr_wins_over_tmux_config(self, _isolated_settings):
        """backend_override='herdr' beats terminal.backend='tmux' in config."""
        from unittest.mock import patch

        _write_terminal_config(_isolated_settings, backend="tmux")
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=True,
        ):
            backend = BackendFactory.create(backend_override="herdr")
        assert isinstance(backend, HerdrBackend)

    def test_override_tmux_wins_over_herdr_config(self, _isolated_settings):
        """backend_override='tmux' beats terminal.backend='herdr' in config."""
        _write_terminal_config(_isolated_settings, backend="herdr")
        backend = BackendFactory.create(backend_override="tmux")
        assert isinstance(backend, TmuxBackend)

    def test_override_works_without_config_file(self, _isolated_settings):
        """backend_override selects the backend even when no config file exists."""
        from unittest.mock import patch

        assert not _isolated_settings.exists()
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=True,
        ):
            backend = BackendFactory.create(backend_override="herdr")
        assert isinstance(backend, HerdrBackend)

    def test_override_herdr_still_reads_herdr_session_from_config(self, _isolated_settings):
        """Override picks the backend; other config keys (herdr_session) still apply."""
        from unittest.mock import patch

        _write_terminal_config(_isolated_settings, backend="tmux", herdr_session="my-session")
        with patch(
            "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
            return_value=True,
        ):
            backend = BackendFactory.create(backend_override="herdr")
        assert isinstance(backend, HerdrBackend)
        assert backend.herdr_session == "my-session"

    def test_unknown_override_raises_configuration_error(self, _isolated_settings):
        """An unrecognized override name raises ConfigurationError."""
        _write_terminal_config(_isolated_settings, backend="tmux")
        with pytest.raises(ConfigurationError, match="Unknown terminal_backend.*screen"):
            BackendFactory.create(backend_override="screen")


class TestBackendFactoryErrors:
    """Test error handling for invalid configs."""

    def test_raises_configuration_error_for_unknown_backend(self, _isolated_settings):
        """ConfigurationError raised for unrecognized backend names."""
        _write_terminal_config(_isolated_settings, backend="screen")
        with pytest.raises(ConfigurationError, match="Unknown terminal_backend.*screen"):
            BackendFactory.create()

    def test_handles_malformed_json_gracefully(self, _isolated_settings):
        """Malformed JSON falls back to tmux default with a warning."""
        _isolated_settings.write_text("not valid json {{{")
        backend = BackendFactory.create()
        assert isinstance(backend, TmuxBackend)

    def test_handles_empty_file_gracefully(self, _isolated_settings):
        """Empty file falls back to tmux default."""
        _isolated_settings.write_text("")
        backend = BackendFactory.create()
        assert isinstance(backend, TmuxBackend)
