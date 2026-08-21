"""Tests for settings_service module."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.settings_service import (
    _DEFAULTS,
    _load,
    _save,
    get_agent_dirs,
    get_extra_agent_dirs,
    get_extra_skill_dirs,
    set_agent_dirs,
    set_extra_agent_dirs,
    set_extra_skill_dirs,
)


@pytest.fixture
def settings_file(tmp_path):
    """Patch SETTINGS_FILE and CAO_HOME_DIR to use a temp directory.

    Also resets get_server_settings()'s module-global cache
    (_server_settings_cache/_server_settings_mtime_ns), which is keyed only
    on SETTINGS_FILE's st_mtime_ns. Without this reset, back-to-back tests
    that each write their own tmp_path/settings.json can collide on a
    coarse-clock filesystem (two writes to DIFFERENT files landing on the
    same st_mtime_ns tick) and a test would silently read the PRIOR test's
    cached settings instead of its own -- flaky (~3-4 failures per run on
    WSL2), not reproducible on every host, and each failing test passes in
    isolation, which is exactly what a stale-cache bug looks like.
    """
    fake_settings = tmp_path / "settings.json"
    settings_service._server_settings_cache = None
    settings_service._server_settings_mtime_ns = -1
    with (
        patch(
            "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
            fake_settings,
        ),
        patch(
            "cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR",
            tmp_path,
        ),
    ):
        yield fake_settings
    settings_service._server_settings_cache = None
    settings_service._server_settings_mtime_ns = -1


class TestLoad:
    """Tests for _load function."""

    def test_load_returns_empty_dict_when_file_does_not_exist(self, settings_file):
        """_load returns {} when the settings file does not exist."""
        assert not settings_file.exists()
        result = _load()
        assert result == {}

    def test_load_returns_empty_dict_when_file_is_corrupt_json(self, settings_file):
        """_load returns {} when file contains invalid JSON."""
        settings_file.write_text("not valid json {{{")
        result = _load()
        assert result == {}

    def test_load_returns_data_when_file_is_valid(self, settings_file):
        """_load returns parsed dict from a valid settings file."""
        data = {"agent_dirs": {"kiro_cli": "/custom/path"}, "extra_agent_dirs": ["/extra"]}
        settings_file.write_text(json.dumps(data))
        result = _load()
        assert result == data


class TestSave:
    """Tests for _save function."""

    def test_save_creates_file(self, settings_file):
        """_save writes JSON to the settings file."""
        data = {"key": "value"}
        _save(data)
        assert settings_file.exists()
        assert json.loads(settings_file.read_text()) == data

    def test_save_creates_parent_directory_if_needed(self, tmp_path):
        """_save creates parent directories if they don't exist yet."""
        nested_dir = tmp_path / "a" / "b" / "c"
        fake_settings = nested_dir / "settings.json"
        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
                fake_settings,
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR",
                nested_dir,
            ),
        ):
            _save({"hello": "world"})
            assert fake_settings.exists()
            assert json.loads(fake_settings.read_text()) == {"hello": "world"}

    def test_save_overwrites_existing_file(self, settings_file):
        """_save overwrites a previous settings file."""
        _save({"old": True})
        _save({"new": True})
        assert json.loads(settings_file.read_text()) == {"new": True}


class TestGetAgentDirs:
    """Tests for get_agent_dirs function."""

    def test_returns_defaults_when_no_settings_file(self, settings_file):
        """get_agent_dirs returns all default dirs when no settings file exists."""
        result = get_agent_dirs()
        assert result == _DEFAULTS

    def test_returns_saved_overrides_merged_with_defaults(self, settings_file):
        """get_agent_dirs merges saved overrides on top of defaults."""
        custom = {"kiro_cli": "/my/custom/kiro"}
        settings_file.write_text(json.dumps({"agent_dirs": custom}))
        result = get_agent_dirs()
        # The overridden key should have the custom value
        assert result["kiro_cli"] == "/my/custom/kiro"
        # Other defaults should be preserved
        assert result["claude_code"] == _DEFAULTS["claude_code"]
        assert result["codex"] == _DEFAULTS["codex"]

    def test_returns_all_default_keys(self, settings_file):
        """get_agent_dirs always returns all known provider keys."""
        result = get_agent_dirs()
        for key in _DEFAULTS:
            assert key in result


class TestSetAgentDirs:
    """Tests for set_agent_dirs function."""

    def test_updates_known_provider(self, settings_file):
        """set_agent_dirs updates a known provider and returns merged result."""
        result = set_agent_dirs({"codex": "/new/codex/path"})
        assert result["codex"] == "/new/codex/path"
        # Other defaults preserved
        assert result["kiro_cli"] == _DEFAULTS["kiro_cli"]

    def test_ignores_unknown_providers(self, settings_file):
        """set_agent_dirs ignores provider names not in _DEFAULTS."""
        result = set_agent_dirs({"unknown_provider": "/some/path"})
        assert "unknown_provider" not in result
        # All defaults unchanged
        assert result == _DEFAULTS

    def test_persists_to_disk_and_can_be_read_back(self, settings_file):
        """set_agent_dirs writes to disk; get_agent_dirs reads it back."""
        set_agent_dirs({"claude_code": "/persisted/path"})
        fresh = get_agent_dirs()
        assert fresh["claude_code"] == "/persisted/path"

    def test_multiple_updates_accumulate(self, settings_file):
        """Successive set_agent_dirs calls accumulate overrides."""
        set_agent_dirs({"kiro_cli": "/first"})
        set_agent_dirs({"codex": "/second"})
        result = get_agent_dirs()
        assert result["kiro_cli"] == "/first"
        assert result["codex"] == "/second"

    def test_mixed_known_and_unknown_providers(self, settings_file):
        """set_agent_dirs stores known and ignores unknown in a single call."""
        result = set_agent_dirs({"kiro_cli": "/yes", "bogus": "/no"})
        assert result["kiro_cli"] == "/yes"
        assert "bogus" not in result


class TestGetExtraAgentDirs:
    """Tests for get_extra_agent_dirs function."""

    def test_returns_empty_list_when_none_set(self, settings_file):
        """get_extra_agent_dirs returns [] when no extra dirs configured."""
        result = get_extra_agent_dirs()
        assert result == []

    def test_returns_saved_extra_dirs(self, settings_file):
        """get_extra_agent_dirs returns the saved list."""
        settings_file.write_text(json.dumps({"extra_agent_dirs": ["/a", "/b"]}))
        result = get_extra_agent_dirs()
        assert result == ["/a", "/b"]


class TestSetExtraAgentDirs:
    """Tests for set_extra_agent_dirs function."""

    def test_saves_and_returns_dirs(self, settings_file):
        """set_extra_agent_dirs saves dirs and returns them."""
        result = set_extra_agent_dirs(["/dir1", "/dir2"])
        assert result == ["/dir1", "/dir2"]

    def test_strips_empty_strings(self, settings_file):
        """set_extra_agent_dirs removes empty or whitespace-only strings."""
        result = set_extra_agent_dirs(["/valid", "", "  ", "/also-valid"])
        assert result == ["/valid", "/also-valid"]

    def test_persists_to_disk(self, settings_file):
        """set_extra_agent_dirs persists to disk so get_extra_agent_dirs reads it."""
        set_extra_agent_dirs(["/persisted"])
        assert get_extra_agent_dirs() == ["/persisted"]

    def test_replaces_previous_list(self, settings_file):
        """set_extra_agent_dirs replaces the entire previous list."""
        set_extra_agent_dirs(["/first"])
        set_extra_agent_dirs(["/second"])
        assert get_extra_agent_dirs() == ["/second"]

    def test_empty_list_clears_previous(self, settings_file):
        """Setting an empty list clears all extra dirs."""
        set_extra_agent_dirs(["/something"])
        set_extra_agent_dirs([])
        assert get_extra_agent_dirs() == []


class TestGetExtraSkillDirs:
    """Tests for get_extra_skill_dirs function."""

    def test_returns_empty_list_when_none_set(self, settings_file):
        """get_extra_skill_dirs returns [] when no extra dirs configured."""
        result = get_extra_skill_dirs()
        assert result == []

    def test_returns_saved_extra_dirs(self, settings_file):
        """get_extra_skill_dirs returns the saved list."""
        settings_file.write_text(json.dumps({"extra_skill_dirs": ["/a", "/b"]}))
        result = get_extra_skill_dirs()
        assert result == ["/a", "/b"]

    def test_filters_non_string_and_empty_entries(self, settings_file):
        """Malformed persisted entries (null/numbers/blank) are dropped, not returned.

        Otherwise Path(extra) in _skill_search_dirs() would raise TypeError and
        break skill listing/loading.
        """
        settings_file.write_text(
            json.dumps({"extra_skill_dirs": ["/valid", None, 123, "", "  ", "/also-valid"]})
        )
        assert get_extra_skill_dirs() == ["/valid", "/also-valid"]


class TestSetExtraSkillDirs:
    """Tests for set_extra_skill_dirs function."""

    def test_saves_and_returns_dirs(self, settings_file):
        """set_extra_skill_dirs saves dirs and returns them."""
        result = set_extra_skill_dirs(["/dir1", "/dir2"])
        assert result == ["/dir1", "/dir2"]

    def test_strips_empty_strings(self, settings_file):
        """set_extra_skill_dirs removes empty or whitespace-only strings."""
        result = set_extra_skill_dirs(["/valid", "", "  ", "/also-valid"])
        assert result == ["/valid", "/also-valid"]

    def test_ignores_non_string_entries(self, settings_file):
        """Non-string entries are dropped rather than crashing on .strip()."""
        result = set_extra_skill_dirs(["/valid", None, 123, "/also-valid"])
        assert result == ["/valid", "/also-valid"]

    def test_persists_to_disk(self, settings_file):
        """set_extra_skill_dirs persists to disk so get_extra_skill_dirs reads it."""
        set_extra_skill_dirs(["/persisted"])
        assert get_extra_skill_dirs() == ["/persisted"]

    def test_replaces_previous_list(self, settings_file):
        """set_extra_skill_dirs replaces the entire previous list."""
        set_extra_skill_dirs(["/first"])
        set_extra_skill_dirs(["/second"])
        assert get_extra_skill_dirs() == ["/second"]

    def test_empty_list_clears_previous(self, settings_file):
        """Setting an empty list clears all extra dirs."""
        set_extra_skill_dirs(["/something"])
        set_extra_skill_dirs([])
        assert get_extra_skill_dirs() == []


class TestExtraAgentAndSkillDirsAreIndependent:
    """The agent and skill extra-dir lists are stored under separate keys."""

    def test_independent_keys(self, settings_file):
        set_extra_agent_dirs(["/agents"])
        set_extra_skill_dirs(["/skills"])
        assert get_extra_agent_dirs() == ["/agents"]
        assert get_extra_skill_dirs() == ["/skills"]


class TestGetServerSettings:
    """Tests for get_server_settings function."""

    def test_returns_defaults_when_no_settings(self, settings_file):
        """Returns default values when no server section exists."""
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        result = get_server_settings()
        assert result == {
            "mcp_request_timeout": 30,
            "event_bus_max_queue_size": 1024,
            "provider_init_timeout": 60,
            "startup_prompt_handler_timeout": 20,
            "state_buffer_max": 32768,
        }

    def test_reads_custom_values(self, settings_file):
        """Reads custom values from settings.json."""
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"mcp_request_timeout": 120, "provider_init_timeout": 90}})
        result = get_server_settings()
        assert result["mcp_request_timeout"] == 120
        assert result["provider_init_timeout"] == 90
        # Unset keys keep defaults
        assert result["event_bus_max_queue_size"] == 1024
        assert result["startup_prompt_handler_timeout"] == 20

    def test_ignores_unknown_keys(self, settings_file):
        """Unknown keys in server section are ignored."""
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"unknown_key": 999, "mcp_request_timeout": 60}})
        result = get_server_settings()
        assert "unknown_key" not in result
        assert result["mcp_request_timeout"] == 60

    def test_invalid_type_falls_back_to_default(self, settings_file):
        """Invalid types fall back to defaults with warning."""
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"mcp_request_timeout": "not_a_number"}})
        result = get_server_settings()
        assert result["mcp_request_timeout"] == 30

    def test_negative_value_falls_back_to_default(self, settings_file):
        """Negative values fall back to defaults."""
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"provider_init_timeout": -5}})
        result = get_server_settings()
        assert result["provider_init_timeout"] == 60

    def test_state_buffer_max_reads_custom_value(self, settings_file):
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"state_buffer_max": 65536}})
        result = get_server_settings()
        assert result["state_buffer_max"] == 65536

    def test_state_buffer_max_is_int(self, settings_file):
        """A settings.json float (e.g. 32768.0) must come back as int -- it's
        used as a slice bound (``buffer[-state_buffer_max:]``), which raises
        TypeError on a float."""
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"state_buffer_max": 65536.0}})
        result = get_server_settings()
        assert result["state_buffer_max"] == 65536
        assert isinstance(result["state_buffer_max"], int)

    def test_state_buffer_max_zero_falls_back_to_default(self, settings_file):
        """0 must not silently disable truncation: ``buffer[-0:]`` is the
        whole buffer (``-0 == 0``, and ``s[0:]`` is everything), not an empty
        slice -- unbounded per-terminal memory from a config typo."""
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"state_buffer_max": 0}})
        result = get_server_settings()
        assert result["state_buffer_max"] == 32768

    def test_state_buffer_max_negative_falls_back_to_default(self, settings_file):
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"state_buffer_max": -1}})
        result = get_server_settings()
        assert result["state_buffer_max"] == 32768

    def test_state_buffer_max_fractional_below_one_falls_back_to_default(self, settings_file):
        """0.5 passes the naive ``val <= 0`` check (0.5 > 0) but truncates to
        0 once coerced to int for the slice bound -- ``buffer[-0:]`` is the
        same unbounded-buffer failure mode as the zero case above, just
        reached through the float door instead. The guard must check
        ``int(val) <= 0``, not ``val <= 0``."""
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"state_buffer_max": 0.5}})
        result = get_server_settings()
        assert result["state_buffer_max"] == 32768

    def test_state_buffer_max_invalid_type_falls_back_to_default(self, settings_file):
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"state_buffer_max": "not_a_number"}})
        result = get_server_settings()
        assert result["state_buffer_max"] == 32768

    def test_state_buffer_max_env_override(self, settings_file, monkeypatch):
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        # Write the (empty) file so SETTINGS_FILE.exists() is True and this
        # test's mtime is distinct from a prior test's -- get_server_settings()
        # caches purely on file mtime, and a nonexistent file always hashes to
        # mtime_ns=-1, so two tests that never call _save() could otherwise
        # collide on the cache and silently return a stale prior result.
        _save({})
        monkeypatch.setenv("CAO_STATE_BUFFER_MAX", "65536")
        result = get_server_settings()
        assert result["state_buffer_max"] == 65536

    def test_state_buffer_max_env_override_beats_settings_file(self, settings_file, monkeypatch):
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({"server": {"state_buffer_max": 16384}})
        monkeypatch.setenv("CAO_STATE_BUFFER_MAX", "65536")
        result = get_server_settings()
        assert result["state_buffer_max"] == 65536

    def test_state_buffer_max_env_zero_falls_back_to_default(self, settings_file, monkeypatch):
        from cli_agent_orchestrator.services.settings_service import get_server_settings

        _save({})  # see test_state_buffer_max_env_override for why
        monkeypatch.setenv("CAO_STATE_BUFFER_MAX", "0")
        result = get_server_settings()
        assert result["state_buffer_max"] == 32768


# ===========================================================================
# PR 526 human review — IMPORTANT: the four workflow_journal_* settings had ZERO
# write-path test coverage.
#
# set_memory_setting validates these keys (bool-vs-int discrimination, per-key
# minimums), but every retention test monkeypatches get_memory_settings to a plain
# dict, so the REAL write path — validate, persist, read back — was never
# exercised. A regression that dropped the bool guard, or the >= 1 minimum on
# output_cap_bytes, would have gone unnoticed.
#
# These drive set_memory_setting directly against the isolated tmp settings file
# (the `settings_file` fixture patches SETTINGS_FILE + CAO_HOME_DIR and resets the
# get_server_settings cache), so no test here can read or write a real ~/.cao.
# ===========================================================================
_WF_INT_KEYS = [
    "workflow_journal_output_cap_bytes",
    "workflow_journal_retention_days",
    "workflow_journal_retention_count",
]
# Hard-coded rather than derived from the production constants under test: a
# fixture sourced from `_WORKFLOW_JOURNAL_INT_KEYS` would follow a bad rename and
# stay green. Minimums are likewise literal — 1 for the cap (a 0-byte cap would
# truncate every output to just the marker), 0 for the two retention bounds
# (where 0 means "bound disabled").
_WF_INT_MINIMUMS = {
    "workflow_journal_output_cap_bytes": 1,
    "workflow_journal_retention_days": 0,
    "workflow_journal_retention_count": 0,
}
_WF_BOOL_KEY = "workflow_journal_capture_output"


def test_literal_key_tables_still_match_the_production_frozensets():
    """The literals above are the point — but they must still COVER production.

    Keeping them literal stops a rename from dragging the tests along silently.
    The gap that leaves (PR #526 review fix cycle 1): if a key were DELETED from
    the production frozenset, every rejection test would still pass — for the
    wrong reason, via the ``else: raise ValueError("Unknown memory setting")``
    branch, which raises the same exception type the tests assert on. This
    equality check closes that hole without weakening the literals: it is the one
    place allowed to compare against the production value, and it asserts
    coverage, not behaviour.
    """
    assert set(_WF_INT_KEYS) == settings_service._WORKFLOW_JOURNAL_INT_KEYS
    assert set(_WF_INT_MINIMUMS) == set(_WF_INT_KEYS)
    assert {_WF_BOOL_KEY} == settings_service._WORKFLOW_JOURNAL_BOOL_KEYS


class TestWorkflowJournalSettingsWritePath:
    """set_memory_setting validation + persistence for the four #504 keys."""

    def test_the_four_keys_are_accepted_by_the_write_path(self, settings_file):
        """All four are in the allow-list — an unknown key raises, these do not."""
        settings_service.set_memory_setting(_WF_BOOL_KEY, True)
        for key in _WF_INT_KEYS:
            settings_service.set_memory_setting(key, 7)
        stored = settings_service.get_memory_settings()
        assert stored[_WF_BOOL_KEY] is True
        for key in _WF_INT_KEYS:
            assert stored[key] == 7

    def test_round_trip_persists_to_disk_not_just_memory(self, settings_file):
        """The value survives a re-read of the file — a real persisted write."""
        settings_service.set_memory_setting("workflow_journal_retention_days", 45)
        on_disk = json.loads(settings_file.read_text())
        assert on_disk["memory"]["workflow_journal_retention_days"] == 45
        assert settings_service.get_memory_settings()["workflow_journal_retention_days"] == 45

    @pytest.mark.parametrize("key", _WF_INT_KEYS)
    def test_int_key_rejects_a_string(self, settings_file, key):
        with pytest.raises(ValueError, match="must be an int"):
            settings_service.set_memory_setting(key, "30")

    @pytest.mark.parametrize("key", _WF_INT_KEYS)
    @pytest.mark.parametrize("bad", [True, False])
    def test_int_key_rejects_a_bool_masquerading_as_an_int(self, settings_file, key, bad):
        """bool is an int SUBCLASS, so a naive isinstance(value, int) would admit
        True/False and persist 1/0 for a retention bound. The guard must reject it
        by type, not coerce it."""
        with pytest.raises(ValueError, match="must be an int"):
            settings_service.set_memory_setting(key, bad)

    @pytest.mark.parametrize("key", _WF_INT_KEYS)
    def test_int_key_rejects_a_float(self, settings_file, key):
        with pytest.raises(ValueError, match="must be an int"):
            settings_service.set_memory_setting(key, 30.5)

    @pytest.mark.parametrize("key", _WF_INT_KEYS)
    def test_int_key_rejects_a_value_below_its_minimum(self, settings_file, key):
        """One below each key's own floor must be rejected, and the rejection must
        leave NOTHING persisted (a partial write would be worse than the error)."""
        below = _WF_INT_MINIMUMS[key] - 1
        with pytest.raises(ValueError, match="must be >="):
            settings_service.set_memory_setting(key, below)
        assert key not in settings_service.get_memory_settings()

    @pytest.mark.parametrize("key", _WF_INT_KEYS)
    def test_int_key_accepts_exactly_its_minimum(self, settings_file, key):
        """The boundary is inclusive: 1 for the cap, 0 for the two retention
        bounds (0 = bound disabled, which sweep_runs honours as a no-op)."""
        minimum = _WF_INT_MINIMUMS[key]
        settings_service.set_memory_setting(key, minimum)
        assert settings_service.get_memory_settings()[key] == minimum

    def test_output_cap_bytes_rejects_zero_but_retention_accepts_it(self, settings_file):
        """The asymmetry is deliberate and load-bearing, so pin it directly: a
        0-byte output cap is meaningless, while 0 on a retention bound means
        'unlimited'. A single shared minimum would break one of the two."""
        with pytest.raises(ValueError, match="must be >= 1"):
            settings_service.set_memory_setting("workflow_journal_output_cap_bytes", 0)
        settings_service.set_memory_setting("workflow_journal_retention_days", 0)
        settings_service.set_memory_setting("workflow_journal_retention_count", 0)
        stored = settings_service.get_memory_settings()
        assert stored["workflow_journal_retention_days"] == 0
        assert stored["workflow_journal_retention_count"] == 0

    @pytest.mark.parametrize("bad", [1, 0, "true", None, 1.0])
    def test_bool_key_rejects_a_non_bool(self, settings_file, bad):
        """capture_output gates whether payloads are retained at all, so a truthy
        1 or "true" must NOT be coerced into enabling capture."""
        with pytest.raises(ValueError, match="must be a bool"):
            settings_service.set_memory_setting(_WF_BOOL_KEY, bad)
        assert _WF_BOOL_KEY not in settings_service.get_memory_settings()

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_key_round_trips_both_values(self, settings_file, value):
        settings_service.set_memory_setting(_WF_BOOL_KEY, value)
        assert settings_service.get_memory_settings()[_WF_BOOL_KEY] is value

    def test_retention_getters_read_the_persisted_values(self, settings_file):
        """The seam the retention tests monkeypatch away: prove the real write path
        feeds workflow_retention's getters, so a persisted value actually takes
        effect end-to-end."""
        from cli_agent_orchestrator.services import workflow_retention

        settings_service.set_memory_setting("workflow_journal_retention_days", 12)
        settings_service.set_memory_setting("workflow_journal_retention_count", 34)
        settings_service.set_memory_setting("workflow_journal_output_cap_bytes", 2048)
        settings_service.set_memory_setting(_WF_BOOL_KEY, True)

        assert workflow_retention.retention_days() == 12
        assert workflow_retention.retention_count() == 34
        assert workflow_retention.output_cap_bytes() == 2048
        assert workflow_retention.capture_enabled() is True

    def test_a_rejected_write_leaves_a_previously_stored_value_intact(self, settings_file):
        """A failed validation must not clobber what was already persisted."""
        settings_service.set_memory_setting("workflow_journal_retention_days", 30)
        with pytest.raises(ValueError):
            settings_service.set_memory_setting("workflow_journal_retention_days", -5)
        assert settings_service.get_memory_settings()["workflow_journal_retention_days"] == 30

    def test_an_unknown_workflow_journal_like_key_is_still_rejected(self, settings_file):
        """The allow-list is exact, not a prefix match — a typo'd key must raise
        rather than being silently persisted as an unread setting."""
        with pytest.raises(ValueError, match="Unknown memory setting"):
            settings_service.set_memory_setting("workflow_journal_retention_dayz", 30)
