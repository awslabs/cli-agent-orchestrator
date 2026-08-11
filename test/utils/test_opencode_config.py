"""Unit tests for the opencode.json read-modify-write helper."""

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import cli_agent_orchestrator.utils.opencode_config as cfg_module
from cli_agent_orchestrator.utils.opencode_config import (
    disable_mcp_server,
    ensure_skills_symlink,
    entry_within_roots,
    is_cao_owned_mcp_entry,
    read_config,
    remove_agent_tools,
    translate_mcp_server_config,
    upsert_agent_tools,
    upsert_mcp_server,
    write_config,
)


@pytest.fixture()
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect OPENCODE_CONFIG_FILE to a temp directory for isolation."""
    config_file = tmp_path / "opencode_cli" / "opencode.json"
    monkeypatch.setattr(cfg_module, "OPENCODE_CONFIG_FILE", config_file)
    return config_file


class TestEnsureSkillsSymlink:
    """ensure_skills_symlink() creates/validates the skills → SKILLS_DIR symlink."""

    @pytest.fixture()
    def symlink_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Redirect OPENCODE_CONFIG_DIR and SKILLS_DIR to isolated tmp locations."""
        config_dir = tmp_path / "opencode_cli"
        skills_dir = tmp_path / "cao_skills"
        skills_dir.mkdir()  # SKILLS_DIR must exist for resolve() to work consistently

        monkeypatch.setattr(cfg_module, "OPENCODE_CONFIG_DIR", config_dir)
        monkeypatch.setattr(cfg_module, "SKILLS_DIR", skills_dir)
        return {"config_dir": config_dir, "skills_dir": skills_dir}

    def test_creates_symlink_when_target_missing(self, symlink_env):
        config_dir = symlink_env["config_dir"]
        skills_dir = symlink_env["skills_dir"]
        target = config_dir / "skills"

        ensure_skills_symlink()

        assert target.is_symlink()
        assert target.resolve() == skills_dir.resolve()

    def test_noop_when_correct_symlink_exists(self, symlink_env):
        config_dir = symlink_env["config_dir"]
        skills_dir = symlink_env["skills_dir"]
        target = config_dir / "skills"

        # Create the correct symlink first
        config_dir.mkdir(parents=True, exist_ok=True)
        target.symlink_to(skills_dir)
        mtime_before = target.lstat().st_mtime

        ensure_skills_symlink()

        assert target.is_symlink()
        assert target.lstat().st_mtime == mtime_before  # unchanged

    def test_warns_and_skips_when_target_is_directory(self, symlink_env, caplog):
        config_dir = symlink_env["config_dir"]
        target = config_dir / "skills"

        # Pre-create a real directory (not a symlink)
        target.mkdir(parents=True, exist_ok=True)

        import logging

        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.utils.opencode_config"
        ):
            ensure_skills_symlink()

        # Warning was logged — no write attempted, directory still intact
        assert any("not a symlink" in rec.message for rec in caplog.records)
        assert target.is_dir() and not target.is_symlink()

    def test_warns_and_skips_when_symlink_points_elsewhere(self, symlink_env, caplog):
        config_dir = symlink_env["config_dir"]
        other_dir = symlink_env["config_dir"].parent / "other_skills"
        other_dir.mkdir()
        target = config_dir / "skills"

        # Create a symlink pointing at a different directory
        config_dir.mkdir(parents=True, exist_ok=True)
        target.symlink_to(other_dir)

        import logging

        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.utils.opencode_config"
        ):
            ensure_skills_symlink()

        # Warning was logged and symlink is unchanged (still points at other_dir)
        assert any("skipping" in rec.message for rec in caplog.records)
        assert target.is_symlink()
        assert target.resolve() == other_dir.resolve()


class TestTranslateMcpServerConfig:
    """translate_mcp_server_config converts CAO mcpServer dicts to OpenCode format."""

    def test_basic_stdio_command_and_args(self):
        result = translate_mcp_server_config(
            {"type": "stdio", "command": "uvx", "args": ["--from", "pkg", "cao-mcp-server"]}
        )
        assert result["type"] == "local"
        assert result["command"] == ["uvx", "--from", "pkg", "cao-mcp-server"]
        assert result["enabled"] is True

    def test_command_only_no_args(self):
        result = translate_mcp_server_config({"type": "stdio", "command": "my-server"})
        assert result["command"] == ["my-server"]
        assert result["enabled"] is True

    def test_args_only_no_command(self):
        result = translate_mcp_server_config({"args": ["server"]})
        assert result["command"] == ["server"]

    def test_env_translated_to_environment(self):
        result = translate_mcp_server_config({"command": "srv", "env": {"FOO": "bar"}})
        assert result["environment"] == {"FOO": "bar"}
        assert "env" not in result

    def test_no_env_key_absent(self):
        result = translate_mcp_server_config({"command": "srv"})
        assert "environment" not in result
        assert "env" not in result

    def test_custom_uvx_entry_passes_through(self):
        """A user-defined uvx-based MCP server is translated verbatim.

        Only the bundled ``cao-mcp-server`` command is rewritten (see
        test_bundled_cao_mcp_server_command_is_resolved); any other command —
        including a user's own ``uvx --from ...`` server — flattens unchanged.
        """
        cao_cfg = {
            "type": "stdio",
            "command": "uvx",
            "args": ["--from", "some-pkg", "my-mcp-server"],
        }
        result = translate_mcp_server_config(cao_cfg)
        assert result == {
            "type": "local",
            "command": ["uvx", "--from", "some-pkg", "my-mcp-server"],
            "enabled": True,
        }

    def test_bundled_cao_mcp_server_command_is_resolved(self):
        """The bare cao-mcp-server command is resolved to a PATH-independent form.

        The bundled profiles declare ``command: cao-mcp-server``; the translator
        resolves it so OpenCode launches it without depending on
        the script being on the agent subprocess's PATH.
        """
        result = translate_mcp_server_config(
            {"type": "stdio", "command": "cao-mcp-server", "args": []}
        )
        assert result["type"] == "local"
        assert result["enabled"] is True
        # Resolved away from the bare console-script name into a concrete
        # invocation (abs path to the script, or `<python> -m ...`).
        assert result["command"][0] != "cao-mcp-server"
        assert result["command"]  # non-empty


class TestReadConfig:
    def test_missing_file_returns_skeleton(self, tmp_config: Path):
        assert not tmp_config.exists()
        data = read_config()
        assert data == {"$schema": "https://opencode.ai/config.json"}

    def test_existing_file_is_parsed(self, tmp_config: Path):
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        data = read_config()
        assert data["foo"] == "bar"


class TestWriteConfig:
    def test_creates_file_and_parent_dirs(self, tmp_config: Path):
        assert not tmp_config.parent.exists()
        write_config({"key": "value"})
        assert tmp_config.exists()
        assert json.loads(tmp_config.read_text()) == {"key": "value"}

    def test_overwrites_existing_content(self, tmp_config: Path):
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(json.dumps({"old": True}), encoding="utf-8")
        write_config({"new": True})
        assert json.loads(tmp_config.read_text()) == {"new": True}

    def test_file_ends_with_newline(self, tmp_config: Path):
        write_config({"x": 1})
        assert tmp_config.read_text(encoding="utf-8").endswith("\n")


class TestUpsertMcpServer:
    def test_fresh_file_creation(self, tmp_config: Path):
        assert not tmp_config.exists()
        upsert_mcp_server("cao-mcp-server", {"type": "local", "command": ["cao-mcp-server"]})
        data = json.loads(tmp_config.read_text())
        assert data["mcp"]["cao-mcp-server"]["type"] == "local"
        assert data["tools"]["cao-mcp-server*"] is False

    def test_idempotent_re_upsert(self, tmp_config: Path):
        server_cfg = {"type": "local", "command": ["cao-mcp-server"]}
        upsert_mcp_server("cao-mcp-server", server_cfg)
        upsert_mcp_server("cao-mcp-server", server_cfg)
        data = json.loads(tmp_config.read_text())
        # Only one entry in mcp
        assert list(data["mcp"].keys()) == ["cao-mcp-server"]
        assert data["tools"]["cao-mcp-server*"] is False

    def test_default_deny_added_to_tools(self, tmp_config: Path):
        upsert_mcp_server("my-server", {"type": "local", "command": ["my-server"]})
        data = json.loads(tmp_config.read_text())
        assert data["tools"]["my-server*"] is False

    def test_existing_user_entries_preserved(self, tmp_config: Path):
        """Pre-existing mcp/tools entries survive an unrelated upsert."""
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "mcp": {"user-server": {"type": "local", "command": ["x"]}},
                    "tools": {"user-server*": False, "existing-setting": True},
                }
            ),
            encoding="utf-8",
        )
        upsert_mcp_server("new-server", {"type": "local", "command": ["new"]})
        data = json.loads(tmp_config.read_text())
        assert "user-server" in data["mcp"]
        assert data["tools"]["existing-setting"] is True
        assert "new-server" in data["mcp"]


class TestUpsertAgentTools:
    def test_creates_agent_tools_section(self, tmp_config: Path):
        upsert_agent_tools("developer", ["cao-mcp-server"])
        data = json.loads(tmp_config.read_text())
        assert data["agent"]["developer"]["tools"] == {"cao-mcp-server*": True}

    def test_idempotent_re_upsert(self, tmp_config: Path):
        upsert_agent_tools("developer", ["cao-mcp-server"])
        upsert_agent_tools("developer", ["cao-mcp-server"])
        data = json.loads(tmp_config.read_text())
        assert data["agent"]["developer"]["tools"] == {"cao-mcp-server*": True}

    def test_multiple_mcp_servers(self, tmp_config: Path):
        upsert_agent_tools("supervisor", ["cao-mcp-server", "other-server"])
        data = json.loads(tmp_config.read_text())
        tools = data["agent"]["supervisor"]["tools"]
        assert tools["cao-mcp-server*"] is True
        assert tools["other-server*"] is True

    def test_missing_parent_dir_auto_created(self, tmp_config: Path):
        assert not tmp_config.parent.exists()
        upsert_agent_tools("developer", ["cao-mcp-server"])
        assert tmp_config.exists()

    def test_existing_agent_keys_preserved(self, tmp_config: Path):
        """A prior ``model:`` key on the agent entry survives tools upsert."""
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(
            json.dumps(
                {
                    "agent": {
                        "developer": {
                            "model": "anthropic/claude-sonnet-4-6",
                            "tools": {},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        upsert_agent_tools("developer", ["cao-mcp-server"])
        data = json.loads(tmp_config.read_text())
        assert data["agent"]["developer"]["model"] == "anthropic/claude-sonnet-4-6"
        assert data["agent"]["developer"]["tools"] == {"cao-mcp-server*": True}

    def test_other_agents_preserved(self, tmp_config: Path):
        upsert_agent_tools("developer", ["cao-mcp-server"])
        upsert_agent_tools("supervisor", ["cao-mcp-server"])
        data = json.loads(tmp_config.read_text())
        assert "developer" in data["agent"]
        assert "supervisor" in data["agent"]


class TestRemoveAgentTools:
    def test_removes_existing_agent(self, tmp_config: Path):
        upsert_agent_tools("developer", ["cao-mcp-server"])
        remove_agent_tools("developer")
        data = json.loads(tmp_config.read_text())
        assert "developer" not in data.get("agent", {})

    def test_noop_on_missing_agent(self, tmp_config: Path):
        write_config({"$schema": "https://opencode.ai/config.json"})
        remove_agent_tools("nonexistent")  # should not raise
        data = json.loads(tmp_config.read_text())
        assert "agent" not in data or "nonexistent" not in data.get("agent", {})

    def test_noop_on_completely_missing_file(self, tmp_config: Path):
        """remove_agent_tools when opencode.json does not exist yet should not raise."""
        assert not tmp_config.exists()
        remove_agent_tools("anything")  # triggers read_config() skeleton path
        # The function writes back whatever read_config() returns (skeleton); the file
        # may or may not exist afterward — what matters is no exception was raised.
        # If a file was written it must not contain the requested agent key.
        if tmp_config.exists():
            data = json.loads(tmp_config.read_text())
            assert "anything" not in data.get("agent", {})

    def test_other_agents_preserved(self, tmp_config: Path):
        upsert_agent_tools("developer", ["cao-mcp-server"])
        upsert_agent_tools("supervisor", ["cao-mcp-server"])
        remove_agent_tools("developer")
        data = json.loads(tmp_config.read_text())
        assert "supervisor" in data["agent"]
        assert "developer" not in data["agent"]


# ---------------------------------------------------------------------------
# design.md §10a — removal (Finding 1) + install-side ownership guard (Finding 2)
# ---------------------------------------------------------------------------


class TestDisableMcpServer:
    """``disable_mcp_server`` sets ``mcp.<name>.enabled`` to a JSON boolean false, in place.

    The boolean *type* is the entire point of Finding 1's fix: OpenCode 1.18.15
    gates the subprocess spawn on a strict ``enabled === false``, so a string
    ``"false"``, a ``0``, or a ``null`` would NOT stop it from trying to launch a
    binary whose ``PLUGIN_ROOT`` removal has just deleted. See design.md §10a and
    ``docs/issues/573-agent-plugins/opencode-verification.md``.
    """

    def _seed(self, config_file: Path, entry: dict, *, name: str = "demo") -> None:
        """Write an ``opencode.json`` with a single ``mcp.<name>`` entry."""
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {name: entry}}),
            encoding="utf-8",
        )

    def test_writes_a_real_json_boolean_false_not_a_string(self, tmp_config: Path):
        self._seed(tmp_config, {"type": "local", "command": ["demo-server"], "enabled": True})

        disable_mcp_server("demo")

        # (1) json round-trip yields the singleton ``False`` — this alone rejects
        # "false"/0/null, none of which *is* the ``False`` object.
        parsed = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert parsed["mcp"]["demo"]["enabled"] is False

        # (2) the serialized text carries a bare ``false`` token, never a quoted
        # string. Belt-and-braces against a future change that stringifies it.
        text = tmp_config.read_text(encoding="utf-8")
        assert '"enabled": false' in text
        assert '"enabled": "false"' not in text

    def test_leaves_every_other_field_untouched(self, tmp_config: Path):
        self._seed(
            tmp_config,
            {
                "type": "local",
                "command": ["demo-server", "--root", "/plugins/demo"],
                "environment": {"PLUGIN_ROOT": "/plugins/demo"},
                "enabled": True,
            },
        )

        disable_mcp_server("demo")

        entry = json.loads(tmp_config.read_text())["mcp"]["demo"]
        assert entry["command"] == ["demo-server", "--root", "/plugins/demo"]
        assert entry["environment"] == {"PLUGIN_ROOT": "/plugins/demo"}
        assert entry["type"] == "local"
        assert entry["enabled"] is False

    def test_noop_when_config_file_absent_leaves_it_absent(self, tmp_config: Path):
        assert not tmp_config.exists()

        disable_mcp_server("demo")  # must neither raise nor create the file

        assert not tmp_config.exists()

    def test_noop_when_mcp_section_absent(self, tmp_config: Path):
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(
            json.dumps({"$schema": "https://opencode.ai/config.json"}), encoding="utf-8"
        )
        before = tmp_config.read_bytes()

        disable_mcp_server("demo")

        assert tmp_config.read_bytes() == before  # not rewritten

    def test_noop_when_named_entry_absent(self, tmp_config: Path):
        self._seed(
            tmp_config, {"type": "local", "command": ["other"], "enabled": True}, name="other"
        )
        before = tmp_config.read_bytes()

        disable_mcp_server("demo")  # a name that is not present

        assert tmp_config.read_bytes() == before

    def test_noop_when_already_disabled(self, tmp_config: Path):
        self._seed(tmp_config, {"type": "local", "command": ["demo-server"], "enabled": False})
        before = tmp_config.read_bytes()

        disable_mcp_server("demo")

        # Already ``false`` → returns before writing, so the file is byte-identical
        # (it is not even re-serialized).
        assert tmp_config.read_bytes() == before


class TestEntryWithinRoots:
    """``entry_within_roots`` is a purely lexical containment test over the entry's paths."""

    def test_true_for_a_command_path_inside_a_root(self, tmp_path: Path):
        root = tmp_path / "plugins"
        cfg = {"command": [str(root / "demo" / "bin" / "server")]}
        assert entry_within_roots(cfg, [root]) is True

    def test_true_for_an_environment_value_inside_a_root(self, tmp_path: Path):
        root = tmp_path / "plugin-data"
        cfg = {"command": ["demo-server"], "environment": {"PLUGIN_DATA": str(root / "demo")}}
        assert entry_within_roots(cfg, [root]) is True

    def test_true_for_a_cwd_inside_a_root(self, tmp_path: Path):
        root = tmp_path / "plugins"
        cfg = {"cwd": str(root / "demo")}
        assert entry_within_roots(cfg, [root]) is True

    def test_false_when_no_path_is_inside_any_root(self, tmp_path: Path):
        root = tmp_path / "plugins"
        cfg = {"command": ["/usr/local/bin/user-thing"], "environment": {"HOME": "/home/user"}}
        assert entry_within_roots(cfg, [root]) is False

    def test_false_when_roots_is_empty(self, tmp_path: Path):
        cfg = {"command": [str(tmp_path / "anything")]}
        assert entry_within_roots(cfg, []) is False

    def test_still_true_after_the_root_directory_is_deleted(self, tmp_path: Path):
        """The post-uninstall state: ``PLUGIN_ROOT`` is gone, the recorded path is not.

        Containment is decided textually (``Path.relative_to``), so deleting the
        directory on disk cannot change the answer — which is exactly why the
        reconcile still recognises a withdrawn plugin's orphaned entry.
        """
        root = tmp_path / "plugins"
        inside = root / "demo"
        inside.mkdir(parents=True)
        cfg = {"command": [str(inside / "server")], "environment": {"PLUGIN_ROOT": str(inside)}}
        assert entry_within_roots(cfg, [root]) is True

        shutil.rmtree(root)
        assert not root.exists()
        assert entry_within_roots(cfg, [root]) is True  # unchanged — purely lexical


class TestIsCaoOwnedMcpEntry:
    """``is_cao_owned_mcp_entry`` decides whether CAO may overwrite an existing entry."""

    def test_true_when_existing_is_byte_equal_to_the_candidate(self, tmp_path: Path):
        """Idempotent replay: re-writing CAO's own entry is a safe no-op overwrite."""
        entry = {"type": "local", "command": ["demo-server"], "enabled": True}
        assert (
            is_cao_owned_mcp_entry(dict(entry), dict(entry), plugin_store_roots=[tmp_path]) is True
        )

    def test_true_for_a_disabled_entry_whose_command_is_in_the_plugin_store(self, tmp_path: Path):
        """Finding 1's post-removal state: a CAO-disabled plugin server, safe to re-enable."""
        root = tmp_path / "plugins"
        existing = {"type": "local", "command": [str(root / "demo" / "server")], "enabled": False}
        candidate = {"type": "local", "command": [str(root / "demo" / "server")], "enabled": True}
        assert is_cao_owned_mcp_entry(existing, candidate, plugin_store_roots=[root]) is True

    def test_false_for_a_users_differing_entry(self, tmp_path: Path):
        """The load-bearing negative: a foreign entry must never be overwritten."""
        existing = {"type": "local", "command": ["/usr/local/bin/user-thing"], "enabled": True}
        candidate = {"type": "local", "command": ["demo-server"], "enabled": True}
        assert is_cao_owned_mcp_entry(existing, candidate, plugin_store_roots=[tmp_path]) is False

    def test_false_for_a_disabled_entry_outside_the_plugin_store(self, tmp_path: Path):
        """Disabled is not enough — a user may have disabled their own server."""
        existing = {"type": "local", "command": ["/opt/user/server"], "enabled": False}
        candidate = {"type": "local", "command": ["demo-server"], "enabled": True}
        assert (
            is_cao_owned_mcp_entry(existing, candidate, plugin_store_roots=[tmp_path / "plugins"])
            is False
        )

    def test_true_for_an_enabled_store_entry_that_differs(self, tmp_path: Path):
        """A force update to CAO's own entry must be recognised as CAO's.

        This assertion was inverted before review on #584. Treating an in-store
        *enabled* entry as user-owned meant a force update that changed the
        command, args or env left the stale command in ``opencode.json`` and
        dropped the agent's tool grant, because CAO skipped what was in fact its
        own entry. A command rooted in the CAO-managed plugin store cannot be
        user-authored.
        """
        root = tmp_path / "plugins"
        existing = {"type": "local", "command": [str(root / "demo" / "server")], "enabled": True}
        candidate = {"type": "local", "command": [str(root / "demo" / "other")], "enabled": True}
        assert is_cao_owned_mcp_entry(existing, candidate, plugin_store_roots=[root]) is True

    def test_a_v1_to_v2_force_update_is_owned_across_command_args_and_env(self, tmp_path: Path):
        """The reviewer's reproduction, with all three fields changing at once."""
        root = tmp_path / "plugins"
        existing = {
            "type": "local",
            "command": [str(root / "demo" / "v1" / "server")],
            "environment": {"VERSION": "1"},
            "enabled": True,
        }
        candidate = {
            "type": "local",
            "command": [str(root / "demo" / "v2" / "server"), "--flag"],
            "environment": {"VERSION": "2"},
            "enabled": True,
        }
        assert is_cao_owned_mcp_entry(existing, candidate, plugin_store_roots=[root]) is True

    def test_a_user_entry_is_still_never_overwritten(self, tmp_path: Path):
        """The guard must not have been weakened for genuinely foreign entries."""
        root = tmp_path / "plugins"
        existing = {"type": "local", "command": ["/home/someone/bin/their-server"], "enabled": True}
        candidate = {"type": "local", "command": [str(root / "demo" / "server")], "enabled": True}
        assert is_cao_owned_mcp_entry(existing, candidate, plugin_store_roots=[root]) is False
