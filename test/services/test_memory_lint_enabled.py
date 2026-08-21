"""Tests for the memory.lint_enabled fail-closed settings contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import config_service as cs
from cli_agent_orchestrator.services.config_service import ConfigService


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
        settings_file,
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(cs, "LEGACY_CONFIG_FILE", tmp_path / "config.json")
    for env_name in cs.ENV_REGISTRY:
        monkeypatch.delenv(env_name, raising=False)
    return settings_file


def test_lint_enabled_defaults_true(_isolated_settings: Path) -> None:
    from cli_agent_orchestrator.services.settings_service import is_memory_lint_enabled

    assert is_memory_lint_enabled() is True
    assert ConfigService.get("memory.lint_enabled") is True


def test_env_false_disables_even_when_persisted_true(
    _isolated_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.services.settings_service import is_memory_lint_enabled

    _isolated_settings.write_text(json.dumps({"memory": {"lint_enabled": True}}))
    monkeypatch.setenv("CAO_MEMORY_LINT_ENABLED", "false")

    assert is_memory_lint_enabled() is False
    assert ConfigService.get("memory.lint_enabled") is False


def test_persisted_false_disables_even_when_env_true(
    _isolated_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.services.settings_service import is_memory_lint_enabled

    _isolated_settings.write_text(json.dumps({"memory": {"lint_enabled": False}}))
    monkeypatch.setenv("CAO_MEMORY_LINT_ENABLED", "true")

    assert is_memory_lint_enabled() is False
    assert ConfigService.get("memory.lint_enabled") is False


def test_invalid_env_value_falls_back_to_file_default(
    _isolated_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.services.settings_service import is_memory_lint_enabled

    monkeypatch.setenv("CAO_MEMORY_LINT_ENABLED", "definitely")

    assert is_memory_lint_enabled() is True


def test_config_list_and_set_preserve_unknown_keys(_isolated_settings: Path) -> None:
    _isolated_settings.write_text(
        json.dumps({"memory": {"unknown_future_key": "keep-me"}, "other": {"x": 1}})
    )

    assert ConfigService.list_all()["memory.lint_enabled"] is True
    ConfigService.set("memory.lint_enabled", False)

    on_disk = json.loads(_isolated_settings.read_text())
    assert on_disk["memory"]["lint_enabled"] is False
    assert on_disk["memory"]["unknown_future_key"] == "keep-me"
    assert on_disk["other"] == {"x": 1}
    assert ConfigService.get("memory.lint_enabled") is False
    assert ConfigService.list_all()["memory.lint_enabled"] is False


def test_config_set_rejects_non_bool(_isolated_settings: Path) -> None:
    with pytest.raises(ValueError, match="lint_enabled must be a bool"):
        ConfigService.set("memory.lint_enabled", "false")
