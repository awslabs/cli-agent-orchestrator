"""Tests for config_service — the unified ConfigService (issue #357).

Covers the precedence chain (env > file > default), legacy config.json +
settings.json migration, and the memory.compile_mode env/file conflict named
in the issue.
"""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import config_service as cs
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.settings_service import SettingsUnreadableError
from cli_agent_orchestrator.utils import atomic_file


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

    def test_terminal_pane_fields_are_resolved_not_defaulted(self, _isolated_settings, monkeypatch):
        """A field the model carries but never populates reads as a default that is a lie."""
        _isolated_settings["settings"].write_text(
            json.dumps({"terminal": {"spawn_mode": "pane", "pane_window": "fleet"}})
        )
        monkeypatch.setenv("CAO_TERMINAL_PANE_LAYOUT", "even-vertical")

        cfg = ConfigService.get_config()

        assert cfg.terminal.spawn_mode == "pane"
        assert cfg.terminal.pane_window == "fleet"
        assert cfg.terminal.pane_layout == "even-vertical"

    def test_vault_env_var_can_disable_but_not_enable(
        self, _isolated_settings, monkeypatch, tmp_path
    ):
        vault = {
            "enabled": False,
            "vaults": [
                {
                    "id": "primary",
                    "root": str(tmp_path),
                    "managed_folder": "CAO",
                    "mappings": [
                        {
                            "folder": "CAO",
                            "scope": "agent",
                            "scope_id": "writer",
                            "writable": True,
                        }
                    ],
                }
            ],
        }
        _isolated_settings["settings"].write_text(json.dumps({"memory": {"vault": vault}}))

        monkeypatch.setenv("CAO_MEMORY_VAULT_ENABLED", "true")
        assert ConfigService.get("memory.vault.enabled") is False

        vault["enabled"] = True
        _isolated_settings["settings"].write_text(json.dumps({"memory": {"vault": vault}}))
        monkeypatch.setenv("CAO_MEMORY_VAULT_ENABLED", "false")
        assert ConfigService.get("memory.vault.enabled") is False
        assert ConfigService.get("memory.vault")["enabled"] is False

    def test_list_disables_invalid_vault_without_hiding_other_settings(
        self, _isolated_settings, caplog
    ):
        _isolated_settings["settings"].write_text(
            json.dumps(
                {
                    "memory": {
                        "vault": {
                            "enabled": True,
                            "vaults": [{"id": "primary", "root": "/missing-vault"}],
                        }
                    },
                    "terminal": {"backend": "herdr"},
                }
            )
        )

        values = ConfigService.list_all()

        assert values["memory.vault"] == {
            "enabled": False,
            "max_recall_body_chars": 4096,
            "vaults": [],
        }
        assert values["memory.vault.enabled"] is False
        assert values["terminal.backend"] == "herdr"
        assert caplog.text.count("Invalid vault configuration disabled") == 1


class TestSetAndPath:
    def test_set_persists_and_get_reads_it_back(self, _isolated_settings):
        ConfigService.set("terminal.backend", "herdr")
        assert ConfigService.get("terminal.backend") == "herdr"
        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["terminal"]["backend"] == "herdr"

    def test_set_atomically_replaces_settings_and_enforces_private_mode(
        self, _isolated_settings, monkeypatch
    ):
        settings_file = _isolated_settings["settings"]
        settings_file.write_text('{"workflow": {"require_approval": true}}')
        settings_file.chmod(0o640)
        monkeypatch.setattr(atomic_file, "LOCK_DIR", settings_file.parent / "locks")
        real_replace = atomic_file.os.replace
        replacements = []

        def _record_replace(source, target):
            replacements.append((Path(source), Path(target)))
            real_replace(source, target)

        monkeypatch.setattr(atomic_file.os, "replace", _record_replace)

        ConfigService.set("workflow.require_approval", False)

        assert len(replacements) == 1
        assert replacements[0][1] == settings_file
        assert json.loads(settings_file.read_text())["workflow"]["require_approval"] is False
        assert settings_file.stat().st_mode & 0o777 == 0o600

    def test_set_replaces_settings_symlink_and_preserves_target(
        self, _isolated_settings, monkeypatch
    ):
        settings_file = _isolated_settings["settings"]
        target = settings_file.with_name("dotfiles-settings.json")
        target.write_text('{"workflow": {"require_approval": true}}')
        target.chmod(0o640)
        settings_file.symlink_to(target)
        monkeypatch.setattr(atomic_file, "LOCK_DIR", settings_file.parent / "locks")

        ConfigService.set("workflow.require_approval", False)

        assert not settings_file.is_symlink()
        assert json.loads(settings_file.read_text())["workflow"]["require_approval"] is False
        assert json.loads(target.read_text()) == {"workflow": {"require_approval": True}}
        assert target.stat().st_mode & 0o777 == 0o640

    def test_set_replaces_dangling_settings_symlink(self, _isolated_settings, monkeypatch):
        settings_file = _isolated_settings["settings"]
        target = settings_file.with_name("dotfiles-settings.json")
        settings_file.symlink_to(target)
        monkeypatch.setattr(atomic_file, "LOCK_DIR", settings_file.parent / "locks")

        ConfigService.set("workflow.require_approval", False)

        assert not settings_file.is_symlink()
        assert json.loads(settings_file.read_text())["workflow"]["require_approval"] is False
        assert not target.exists()

    def test_config_and_settings_writers_share_lock_through_symlinked_home(
        self, tmp_path, monkeypatch
    ):
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        settings_file = linked_home / "settings.json"
        monkeypatch.setattr(settings_service, "CAO_HOME_DIR", linked_home)
        monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)
        monkeypatch.setattr(atomic_file, "LOCK_DIR", tmp_path / "locks")
        lock_paths = []
        real_file_lock = atomic_file._file_lock

        @contextmanager
        def _record_file_lock(lock_path, timeout):
            lock_paths.append(lock_path)
            with real_file_lock(lock_path, timeout):
                yield

        monkeypatch.setattr(atomic_file, "_file_lock", _record_file_lock)

        cs._save_raw({"writer": "config"})
        settings_service._save({"writer": "settings"})

        assert lock_paths == [
            atomic_file._lock_path_for(real_home / "settings.json"),
            atomic_file._lock_path_for(real_home / "settings.json"),
        ]

    def test_delegation_and_migration_do_not_reenter_settings_lock(
        self, _isolated_settings, monkeypatch
    ):
        monkeypatch.setattr(
            atomic_file, "LOCK_DIR", _isolated_settings["settings"].parent / "locks"
        )
        real_file_lock = atomic_file._file_lock
        lock_held = False

        @contextmanager
        def _reject_reentry(lock_path, timeout):
            nonlocal lock_held
            assert not lock_held, "settings lock was re-entered"
            lock_held = True
            try:
                with real_file_lock(lock_path, timeout):
                    yield
            finally:
                lock_held = False

        monkeypatch.setattr(atomic_file, "_file_lock", _reject_reentry)

        ConfigService.set("agents.dirs.kiro_cli", "/tmp/agents")
        ConfigService.set("skills.extra_dirs", ["/tmp/skills"])
        ConfigService.set("memory.enabled", False)
        _isolated_settings["legacy"].write_text(json.dumps({"terminal_backend": "herdr"}))

        assert ConfigService.get("terminal.backend") == "herdr"

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


class TestSavePrivateAndSymlinkSafe:
    """``cao config set`` is the second writer of settings.json and must match the
    first: 0600, atomic, never through a symlink. ``ConfigService.set`` reaches
    ``_save_raw`` for every key settings_service does not own (``terminal.backend``
    here), so this pins that path rather than the settings_service one."""

    def test_set_creates_file_private(self, _isolated_settings):
        settings = _isolated_settings["settings"]
        ConfigService.set("terminal.backend", "tmux")
        assert oct(settings.stat().st_mode & 0o777) == oct(0o600)
        assert json.loads(settings.read_text())["terminal"]["backend"] == "tmux"

    def test_set_tightens_an_existing_world_readable_file(self, _isolated_settings):
        settings = _isolated_settings["settings"]
        settings.write_text(json.dumps({"terminal": {"backend": "herdr"}}))
        settings.chmod(0o644)
        ConfigService.set("terminal.backend", "tmux")
        assert oct(settings.stat().st_mode & 0o777) == oct(0o600)
        assert json.loads(settings.read_text())["terminal"]["backend"] == "tmux"

    def test_set_does_not_follow_a_symlink(self, _isolated_settings, tmp_path):
        settings = _isolated_settings["settings"]
        victim = tmp_path / "victim.json"
        # Parseable content: since GH #737 a write over a settings.json whose
        # content cannot be read is refused outright, so an unparseable victim
        # would pass this test for the wrong reason (nothing written at all).
        # The unparseable case is pinned separately below.
        victim.write_text(json.dumps({"terminal": {"backend": "herdr"}}))
        before = victim.read_bytes()
        settings.symlink_to(victim)
        ConfigService.set("terminal.backend", "tmux")
        assert victim.read_bytes() == before
        assert not settings.is_symlink()
        assert oct(settings.stat().st_mode & 0o777) == oct(0o600)
        assert json.loads(settings.read_text())["terminal"]["backend"] == "tmux"

    def test_a_symlink_to_unparseable_content_is_left_alone(self, _isolated_settings, tmp_path):
        """Refusing (GH #737) must not reintroduce following the link: neither
        the victim nor the symlink itself is touched."""
        settings = _isolated_settings["settings"]
        victim = tmp_path / "victim.txt"
        victim.write_text("do not touch")
        settings.symlink_to(victim)
        with pytest.raises(SettingsUnreadableError):
            ConfigService.set("terminal.backend", "tmux")
        assert victim.read_text() == "do not touch"
        assert settings.is_symlink()


# Every ConfigService.set path that persists: the generic nested writer, the
# agents.roles dual-format writer, and the three that delegate to
# settings_service setters.
_SET_PATHS = [
    pytest.param("logging.level", "INFO", id="generic-nested"),
    pytest.param("terminal.backend", "tmux", id="legacy-key-map"),
    pytest.param("agents.roles.reviewer", "x", id="agents-roles"),
    pytest.param("agents.extra_dirs", ["/x"], id="delegated-extra-dirs"),
    pytest.param("memory.enabled", True, id="delegated-memory"),
    pytest.param("skills.extra_dirs", ["/x"], id="delegated-skill-dirs"),
]


class TestSetFailsClosedOverAnUnreadableFile:
    """GH #737: ``cao config set`` over a settings.json that exists but cannot
    be read loaded it as ``{}``, added its own key, and published the result —
    deleting every other section while exiting 0. Every setter now refuses and
    leaves the file byte-identical.
    """

    @pytest.mark.parametrize(
        "path,value",
        [(p.values[0], p.values[1]) for p in _SET_PATHS],
        ids=[p.id for p in _SET_PATHS],
    )
    def test_unparseable_file_is_refused_and_preserved(self, _isolated_settings, path, value):
        settings = _isolated_settings["settings"]
        # The reporter's file: valid settings plus one trailing comma.
        settings.write_text(
            "{\n"
            '  "terminal": {"backend": "tmux", "herdr_session": "cao"},\n'
            '  "logging": {"level": "DEBUG"},\n'
            '  "agents": {"extra_dirs": ["/important-agent-store"]},\n'
            '  "memory": {"enabled": true, "flush_threshold": 0.5,}\n'
            "}\n"
        )
        before = settings.read_bytes()
        with pytest.raises(SettingsUnreadableError):
            ConfigService.set(path, value)
        assert settings.read_bytes() == before

    @pytest.mark.parametrize(
        "path,value",
        [(p.values[0], p.values[1]) for p in _SET_PATHS],
        ids=[p.id for p in _SET_PATHS],
    )
    def test_undecodable_file_is_refused_and_preserved(self, _isolated_settings, path, value):
        """Not just bad JSON — bytes that are not even text. The read fails
        inside the atomic-rewrite helper, ahead of the strict parse, so this
        pins that the helper's own failure is translated rather than escaping
        as a bare UnicodeDecodeError.
        """
        settings = _isolated_settings["settings"]
        settings.write_bytes(b"\xff\xfe{}")
        before = settings.read_bytes()
        with pytest.raises(SettingsUnreadableError):
            ConfigService.set(path, value)
        assert settings.read_bytes() == before

    @pytest.mark.parametrize("content", ['"just a string"', "null", "42", "[1, 2, 3]"])
    def test_a_json_scalar_is_refused_too(self, _isolated_settings, content):
        """A file holding a valid JSON non-object parsed fine and was ignored,
        so the write replaced it with an object. Unknown content is unknown
        content: refuse. Includes JSON ``null``, which used to slip through
        because ``elif loaded is not None`` treated it as "nothing parsed"
        rather than "not a dict"."""
        settings = _isolated_settings["settings"]
        settings.write_text(content)
        with pytest.raises(SettingsUnreadableError):
            ConfigService.set("logging.level", "INFO")
        assert settings.read_text() == content

    def test_settings_path_as_a_directory_is_refused_cleanly(self, _isolated_settings):
        """OSError-shaped unreadability (e.g. the path being a directory, or
        a permission fault) must translate to SettingsUnreadableError, not
        escape as a bare OSError traceback. ``locked_atomic_rewrite``
        pre-reads the target before ``_mutate_raw``'s own strict load runs."""
        settings = _isolated_settings["settings"]
        settings.mkdir()
        with pytest.raises(SettingsUnreadableError):
            ConfigService.set("logging.level", "INFO")
        assert settings.is_dir()

    def test_the_cli_reports_it_and_exits_non_zero(self, _isolated_settings):
        """The pre-fix command echoed the new value and exited 0; the only
        trace was a logger.warning invisible at the default level."""
        from click.testing import CliRunner

        from cli_agent_orchestrator.cli.commands.config import config as config_group

        settings = _isolated_settings["settings"]
        settings.write_text('{"logging": {"level": "DEBUG"},}')
        before = settings.read_bytes()
        result = CliRunner().invoke(config_group, ["set", "logging.level", "INFO"])
        assert result.exit_code != 0
        assert "could not be read" in result.output
        assert "Nothing was written" in result.output
        assert settings.read_bytes() == before

    def test_a_readable_file_still_writes_and_keeps_its_siblings(self, _isolated_settings):
        """The guard must not cost the happy path: a well-formed file is still
        updated in place with every other section intact."""
        settings = _isolated_settings["settings"]
        settings.write_text(
            json.dumps({"terminal": {"backend": "tmux"}, "logging": {"level": "DEBUG"}})
        )
        ConfigService.set("logging.level", "INFO")
        data = json.loads(settings.read_text())
        assert data["logging"]["level"] == "INFO"
        assert data["terminal"] == {"backend": "tmux"}

    def test_an_absent_file_is_still_created(self, _isolated_settings):
        """ "Absent" is not "unreadable": first-run writes must keep working."""
        settings = _isolated_settings["settings"]
        assert not settings.exists()
        ConfigService.set("logging.level", "INFO")
        assert json.loads(settings.read_text())["logging"]["level"] == "INFO"

    def test_migration_survives_a_strict_write(self, _isolated_settings):
        """GH #737 follow-up: a strict write (``ConfigService.set``, which
        calls ``_load_raw(strict=True)``) must not suppress the legacy
        migration outright. A write that touches only ``terminal.spawn_mode``
        used to persist a partial ``{"terminal": {"spawn_mode": ...}}``
        section whose mere presence then permanently blocked the migration,
        silently resetting backend/herdr_session to defaults forever after.
        """
        _isolated_settings["legacy"].write_text(
            json.dumps({"terminal_backend": "herdr", "herdr_session": "my-sess"})
        )
        ConfigService.set("terminal.spawn_mode", "pane")

        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["terminal"] == {
            "backend": "herdr",
            "herdr_session": "my-sess",
            "spawn_mode": "pane",
        }
        assert ConfigService.get("terminal.backend") == "herdr"
        assert ConfigService.get("terminal.herdr_session") == "my-sess"

    def test_legacy_migration_does_not_run_over_an_unreadable_file(self, _isolated_settings):
        """``_load_raw``'s config.json migration is itself a write. Over an
        unreadable settings.json the lenient load saw no ``terminal`` key and
        would have published ``{"terminal": ...}`` over it."""
        settings = _isolated_settings["settings"]
        _isolated_settings["legacy"].write_text(json.dumps({"terminal_backend": "herdr"}))
        settings.write_text('{"logging": {"level": "DEBUG"},}')
        before = settings.read_bytes()
        ConfigService.get("logging.level")  # lenient read path, must not write
        assert settings.read_bytes() == before
