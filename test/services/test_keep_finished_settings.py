"""``terminal.keep_finished`` settings: defaults, validation, and ``cao config`` (#1225)."""

import json

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.config import config as config_cli
from cli_agent_orchestrator.services import keep_service, settings_service
from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.settings_service import (
    KeepFinishedSettings,
    KeepFinishedSettingsError,
    get_keep_finished_settings,
    parse_keep_finished_block,
)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", path)
    return path


def _write(path, block):
    path.write_text(json.dumps({"terminal": {"keep_finished": block}}))


def test_absent_block_is_off_with_the_workshop_defaults(settings_file):
    assert get_keep_finished_settings() == KeepFinishedSettings(
        enabled=False, idle_window_minutes=60, max_per_session=5, on_cap="teardown_oldest"
    )


def test_absent_settings_file_and_other_terminal_keys_are_off(settings_file):
    settings_file.write_text(json.dumps({"terminal": {"backend": "tmux"}}))
    assert get_keep_finished_settings().enabled is False


def test_full_valid_block_is_read(settings_file):
    _write(
        settings_file,
        {"enabled": True, "idle_window_minutes": 5, "max_per_session": 2, "on_cap": "dont_keep"},
    )
    assert get_keep_finished_settings() == KeepFinishedSettings(True, 5, 2, "dont_keep")


def test_partial_block_takes_defaults_for_the_rest(settings_file):
    _write(settings_file, {"enabled": True})
    assert get_keep_finished_settings() == KeepFinishedSettings(True, 60, 5, "teardown_oldest")


@pytest.mark.parametrize(
    "block, words",
    [
        ({"enabled": "yes"}, ["terminal.keep_finished.enabled", "true or false", "'yes'"]),
        ({"enabled": 1}, ["terminal.keep_finished.enabled", "true or false"]),
        ({"idle_window_minutes": 0}, ["idle_window_minutes", "1 to 10080", "0"]),
        ({"idle_window_minutes": 10081}, ["idle_window_minutes", "1 to 10080"]),
        ({"idle_window_minutes": -5}, ["idle_window_minutes", "-5"]),
        ({"idle_window_minutes": 1.5}, ["idle_window_minutes", "whole number"]),
        ({"idle_window_minutes": True}, ["idle_window_minutes", "whole number"]),
        ({"idle_window_minutes": "60"}, ["idle_window_minutes", "'60'"]),
        ({"max_per_session": 0}, ["max_per_session", "1 to 100"]),
        ({"max_per_session": 101}, ["max_per_session", "1 to 100"]),
        ({"max_per_session": None}, ["max_per_session", "None"]),
        ({"on_cap": "evict"}, ["on_cap", "teardown_oldest, dont_keep", "'evict'"]),
        ({"on_cap": None}, ["on_cap"]),
        ({"enabled": True, "window": 5}, ["unknown key", "window", "idle_window_minutes"]),
    ],
)
def test_bad_values_are_refused_at_load_time_with_a_clear_message(settings_file, block, words):
    _write(settings_file, block)
    with pytest.raises(KeepFinishedSettingsError) as exc:
        get_keep_finished_settings()
    for word in words:
        assert word in str(exc.value)


@pytest.mark.parametrize("block", [True, "on", 5, ["enabled"]])
def test_a_non_object_block_is_refused(block):
    with pytest.raises(KeepFinishedSettingsError, match="must be an object"):
        parse_keep_finished_block(block)


@pytest.mark.parametrize(
    "key, value",
    [
        ("idle_window_minutes", 1),
        ("idle_window_minutes", 10080),
        ("max_per_session", 1),
        ("max_per_session", 100),
    ],
)
def test_range_ends_are_accepted(key, value):
    assert getattr(parse_keep_finished_block({key: value}), key) == value


def test_an_invalid_block_reads_as_off_for_keep_decisions(settings_file, caplog):
    _write(settings_file, {"enabled": True, "max_per_session": 0})
    with caplog.at_level("WARNING"):
        settings = keep_service.current_settings()
    assert settings.enabled is False
    assert "max_per_session" in caplog.text and "OFF" in caplog.text


def test_startup_posture_logs_an_invalid_block_as_an_error(settings_file, caplog):
    _write(settings_file, {"enabled": "true"})
    with caplog.at_level("INFO"):
        keep_service.log_startup_posture()
    assert any(r.levelname == "ERROR" and "stays OFF" in r.getMessage() for r in caplog.records)


def test_startup_posture_is_silent_when_off_and_reports_when_on(settings_file, caplog):
    with caplog.at_level("INFO"):
        keep_service.log_startup_posture()
    assert "keeping finished agents" not in caplog.text
    _write(settings_file, {"enabled": True})
    with caplog.at_level("INFO"):
        keep_service.log_startup_posture()
    assert "keeping finished agents: ON" in caplog.text


class TestCaoConfig:
    def test_set_writes_a_valid_value(self, settings_file):
        result = CliRunner().invoke(config_cli, ["set", "terminal.keep_finished.enabled", "true"])
        assert result.exit_code == 0, result.output
        saved = json.loads(settings_file.read_text())
        assert saved["terminal"]["keep_finished"] == {"enabled": True}
        assert get_keep_finished_settings().enabled is True

    @pytest.mark.parametrize(
        "key, value, word",
        [
            ("terminal.keep_finished.idle_window_minutes", "0", "idle_window_minutes"),
            ("terminal.keep_finished.max_per_session", "-1", "max_per_session"),
            ("terminal.keep_finished.on_cap", "evict", "on_cap"),
            ("terminal.keep_finished.enabled", "maybe", "enabled"),
            ("terminal.keep_finished.windw", "5", "unknown key"),
        ],
    )
    def test_set_refuses_a_bad_value_and_leaves_the_file_unchanged(
        self, settings_file, key, value, word
    ):
        before = json.dumps({"terminal": {"backend": "tmux", "keep_finished": {"enabled": True}}})
        settings_file.write_text(before)
        # "--" so click reads "-1" as the value, not an option.
        result = CliRunner().invoke(config_cli, ["set", "--", key, value])
        assert result.exit_code != 0
        assert word in result.output
        assert settings_file.read_text() == before

    def test_get_and_list_show_the_defaults(self, settings_file):
        assert ConfigService.get("terminal.keep_finished.enabled") is False
        assert ConfigService.get("terminal.keep_finished.idle_window_minutes") == 60
        assert ConfigService.get("terminal.keep_finished.max_per_session") == 5
        assert ConfigService.get("terminal.keep_finished.on_cap") == "teardown_oldest"
        listed = ConfigService.list_all()
        assert listed["terminal.keep_finished.enabled"] is False
        assert listed["terminal.keep_finished.on_cap"] == "teardown_oldest"

    def test_typed_config_reads_the_block_and_an_invalid_one_as_off(self, settings_file):
        _write(settings_file, {"enabled": True, "max_per_session": 3})
        block = ConfigService.get_config().terminal.keep_finished
        assert (block.enabled, block.max_per_session) == (True, 3)
        _write(settings_file, {"enabled": True, "max_per_session": 0})
        assert ConfigService.get_config().terminal.keep_finished.enabled is False
