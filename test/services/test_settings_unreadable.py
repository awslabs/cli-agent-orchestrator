"""Unreadable settings.json must not masquerade as a deliberate opt-out.

``is_learning_enabled()`` fails closed, which is correct — but it used to answer
False for a settings.json it merely could not READ, so agents announced
"workflow self-learning is disabled" for what was a filesystem permissions
fault. These tests pin the distinction:

- ``_load_or_raise()`` separates *absent* (defaults are right) from *unreadable*
  (the answer is unknown), including the case that hid the bug: ``Path.exists()``
  swallows a ``PermissionError`` from the PARENT directory, so a test that only
  chmods the FILE would have passed all along.
- ``_load()`` stays lenient, so its 14 existing callers are unaffected.
- ``learning_status()`` reports ``unreadable`` while STILL failing closed.
- ``is_memory_enabled()`` / ``is_memory_lint_enabled()`` still fail **open** —
  their asymmetry is deliberate and must not be "fixed" by a later change here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

# chmod-based denial is meaningless as root, which CI containers often are.
requires_unprivileged = pytest.mark.skipif(
    os.geteuid() == 0, reason="chmod-based read denial does not apply to root"
)


@pytest.fixture
def settings_file(tmp_path: Path) -> Iterator[Path]:
    """Patch settings_service paths to an isolated settings.json."""
    fake_settings = tmp_path / "settings.json"
    with (
        patch("cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake_settings),
        patch("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", tmp_path),
    ):
        yield fake_settings


@pytest.fixture
def unreadable_parent(tmp_path: Path) -> Iterator[Path]:
    """settings.json inside a directory the process cannot read.

    This is the shape the sandbox produced, and the one ``Path.exists()`` hides.
    """
    home = tmp_path / "cao-home"
    home.mkdir()
    fake_settings = home / "settings.json"
    fake_settings.write_text(json.dumps({"memory": {"learning_enabled": True}}))
    home.chmod(0o000)
    try:
        with (
            patch("cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake_settings),
            patch("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", home),
        ):
            yield fake_settings
    finally:
        home.chmod(0o700)


class TestLoadOrRaise:
    def test_absent_file_returns_defaults(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import _load_or_raise

        assert not settings_file.exists()
        assert _load_or_raise() == {}

    def test_readable_file_returns_contents(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import _load_or_raise

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        assert _load_or_raise() == {"memory": {"learning_enabled": True}}

    @requires_unprivileged
    def test_unreadable_file_raises(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            SettingsUnreadableError,
            _load_or_raise,
        )

        settings_file.write_text("{}")
        settings_file.chmod(0o000)
        try:
            with pytest.raises(SettingsUnreadableError):
                _load_or_raise()
        finally:
            settings_file.chmod(0o600)

    @requires_unprivileged
    def test_unreadable_parent_directory_raises(self, unreadable_parent: Path) -> None:
        """The case Path.exists() hid: denial comes from the directory, not the file."""
        from cli_agent_orchestrator.services.settings_service import (
            SettingsUnreadableError,
            _load_or_raise,
        )

        with pytest.raises(SettingsUnreadableError):
            _load_or_raise()

    def test_directory_in_place_of_file_raises(self, tmp_path: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            SettingsUnreadableError,
            _load_or_raise,
        )

        as_dir = tmp_path / "settings.json"
        as_dir.mkdir()
        with patch("cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", as_dir):
            with pytest.raises(SettingsUnreadableError):
                _load_or_raise()

    def test_malformed_json_raises(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            SettingsUnreadableError,
            _load_or_raise,
        )

        settings_file.write_text("{not json")
        with pytest.raises(SettingsUnreadableError):
            _load_or_raise()

    def test_non_dict_top_level_raises(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            SettingsUnreadableError,
            _load_or_raise,
        )

        settings_file.write_text(json.dumps(["not", "an", "object"]))
        with pytest.raises(SettingsUnreadableError):
            _load_or_raise()

    def test_redacted_detail_omits_the_path(self, settings_file: Path) -> None:
        """API responses must not leak server filesystem paths."""
        from cli_agent_orchestrator.services.settings_service import SettingsUnreadableError

        err = SettingsUnreadableError(settings_file, PermissionError(13, "Permission denied"))
        detail = err.redacted_detail()
        assert str(settings_file) not in detail
        assert "PermissionError" in detail
        assert str(settings_file) in str(err)  # the full message still logs the path


class TestLoadStaysLenient:
    """_load() keeps its old contract so its 14 callers are untouched."""

    def test_malformed_json_returns_empty(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import _load

        settings_file.write_text("{not json")
        assert _load() == {}

    @requires_unprivileged
    def test_unreadable_returns_empty(self, unreadable_parent: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import _load

        assert _load() == {}


class TestSettingsReadable:
    def test_true_when_absent(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import settings_readable

        assert settings_readable() is True

    @requires_unprivileged
    def test_false_when_unreadable(self, unreadable_parent: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import settings_readable

        assert settings_readable() is False


class TestLearningStatus:
    def test_reports_enabled_when_readable(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import learning_status

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        st = learning_status()
        assert (st.enabled, st.unreadable) == (True, False)

    def test_reports_disabled_when_absent(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import learning_status

        st = learning_status()
        assert (st.enabled, st.unreadable) == (False, False)

    @requires_unprivileged
    def test_reports_unreadable_and_still_fails_closed(
        self, unreadable_parent: Path, monkeypatch: Any
    ) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            is_learning_enabled,
            learning_status,
        )

        monkeypatch.delenv("CAO_MEMORY_LEARNING_ENABLED", raising=False)
        st = learning_status()
        assert st.unreadable is True
        assert st.enabled is False, "must still fail closed when the answer is unknown"
        assert st.detail
        assert is_learning_enabled() is False

    @requires_unprivileged
    def test_env_override_is_decisive_so_not_reported_unreadable(
        self, unreadable_parent: Path, monkeypatch: Any
    ) -> None:
        """An env-driven install is unaffected by an unreadable file."""
        from cli_agent_orchestrator.services.settings_service import (
            is_learning_enabled,
            learning_status,
        )

        monkeypatch.setenv("CAO_MEMORY_LEARNING_ENABLED", "true")
        st = learning_status()
        assert (st.enabled, st.unreadable) == (True, False)
        assert is_learning_enabled() is True


class TestFailOpenFlagsUnchanged:
    """Regression pins: memory flags fail OPEN, deliberately. Do not "fix" these."""

    @requires_unprivileged
    def test_is_memory_enabled_stays_open(self, unreadable_parent: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        assert is_memory_enabled() is True

    @requires_unprivileged
    def test_is_memory_lint_enabled_stays_open(self, unreadable_parent: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_memory_lint_enabled

        assert is_memory_lint_enabled() is True
