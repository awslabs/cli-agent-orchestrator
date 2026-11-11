"""Unit tests for the opencode.json read-modify-write helper."""

import contextlib
import json
import logging
import os
import shutil
from pathlib import Path

import pytest

import cli_agent_orchestrator.utils.opencode_config as cfg_module
from cli_agent_orchestrator.utils.opencode_config import (
    OpenCodeAgentIdCollisionError,
    disable_mcp_server,
    ensure_skills_symlink,
    entry_within_roots,
    is_cao_owned_mcp_entry,
    is_orphaned_recorded_mcp_entry,
    is_recorded_mcp_entry,
    mcp_entry_fingerprint,
    read_config,
    record_mcp_entry,
    remove_agent_tools,
    to_opencode_agent_id,
    translate_mcp_server_config,
    upsert_agent_tools,
    upsert_mcp_server,
    write_config,
)


class TestToOpencodeAgentId:
    """The id becomes the ``<id>.md`` filename under OPENCODE_AGENTS_DIR, so no
    path separator may survive it.

    Security regression for GHSA-6m35-gcf5-xm75: only ``/`` was folded, leaving a
    resolved profile name like ``..\\..\\evil`` able to traverse on Windows.
    """

    @pytest.mark.parametrize(
        "hostile_name",
        ["..\\..\\evil", "a\\b", "..\\../mixed", "C:\\Windows\\evil", "../../evil"],
    )
    def test_no_separator_survives(self, hostile_name):
        produced = to_opencode_agent_id(hostile_name)
        assert "/" not in produced
        assert "\\" not in produced

    def test_plain_name_is_unchanged(self):
        assert to_opencode_agent_id("developer") == "developer"


@pytest.fixture()
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect OPENCODE_CONFIG_FILE to a temp directory for isolation."""
    config_file = tmp_path / "opencode_cli" / "opencode.json"
    monkeypatch.setattr(cfg_module, "OPENCODE_CONFIG_FILE", config_file)
    return config_file


class TestToOpencodeAgentId:
    """to_opencode_agent_id rewrites '/' -> '__' and nothing else."""

    def test_slash_replaced_with_double_underscore(self):
        assert to_opencode_agent_id("a/b") == "a__b"

    def test_no_slash_unchanged(self):
        assert to_opencode_agent_id("developer") == "developer"

    def test_spaces_and_punctuation_untouched(self):
        # Documents that spaces/dashes are NOT rewritten (only '/'), so they
        # cannot cause a collision in this scheme.
        assert to_opencode_agent_id("foo bar") == "foo bar"
        assert to_opencode_agent_id("foo-bar") == "foo-bar"


class TestOpenCodeAgentIdCollisionError:
    """The collision error type contract the install guard relies on."""

    def test_collision_is_valueerror_subclass(self):
        # _guard_installed_copy_ownership raises this; install_agent()'s
        # broad `except Exception` turns it into a clean CLI "Error:" message.
        # (The end-to-end collision behaviour is covered in
        # test/cli/commands/test_install_opencode.py against the real CLI.)
        assert issubclass(OpenCodeAgentIdCollisionError, ValueError)


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

    def test_cwd_is_copied_through(self):
        """Reproduced by review 3 on #584 — the working directory was dropped.

        OpenCode's ``McpLocalConfig.cwd`` is the field its spawn honours, so a
        plugin whose ``command``/``args`` are relative to its working directory
        depends on this surviving translation.
        """
        result = translate_mcp_server_config({"command": "srv", "cwd": "/plugins/demo"})
        assert result["cwd"] == "/plugins/demo"

    def test_absent_cwd_is_not_invented(self):
        """Inventing one would change where a profile-declared server runs."""
        assert "cwd" not in translate_mcp_server_config({"command": "srv"})

    def test_an_empty_cwd_is_dropped(self):
        """An empty string is not a directory; emitting it would be noise."""
        assert "cwd" not in translate_mcp_server_config({"command": "srv", "cwd": ""})

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

    def test_a_users_own_tools_keys_are_merged_not_replaced(self, tmp_config: Path):
        """Reproduced by review 3 on #584: the whole ``tools`` map was replaced.

        A user's hand-written policy on a CAO-installed agent — a denied builtin
        and a grant for their own server — was destroyed on every install.
        """
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(
            json.dumps({"agent": {"developer": {"tools": {"bash": False, "user-srv*": True}}}}),
            encoding="utf-8",
        )
        upsert_agent_tools("developer", ["cao-mcp-server"])
        tools = json.loads(tmp_config.read_text())["agent"]["developer"]["tools"]
        assert tools == {"bash": False, "user-srv*": True, "cao-mcp-server*": True}

    def test_a_grant_that_leaves_the_desired_set_is_withdrawn(self, tmp_config: Path):
        """CAO's own stale grant is removed on the next install, via the sidecar."""
        upsert_agent_tools("developer", ["cao-mcp-server", "demo-tools"])
        upsert_agent_tools("developer", ["cao-mcp-server"])
        tools = json.loads(tmp_config.read_text())["agent"]["developer"]["tools"]
        assert tools == {"cao-mcp-server*": True}

    def test_an_unprovable_grant_is_left_alone_rather_than_deleted(self, tmp_config: Path):
        """No sidecar row and no plugin-store proof ⇒ conservative: keep the key.

        ``other*`` looks exactly like a grant CAO might have written, but CAO has
        no record of writing it and nothing on disk proves it. Deleting on a guess
        is the failure mode review 3 reported; leaving it is the safe direction.
        """
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(
            json.dumps({"agent": {"developer": {"tools": {"other*": True}}}}),
            encoding="utf-8",
        )
        upsert_agent_tools("developer", ["cao-mcp-server"])
        tools = json.loads(tmp_config.read_text())["agent"]["developer"]["tools"]
        assert tools == {"other*": True, "cao-mcp-server*": True}

    def test_other_agent_fields_survive_an_upsert(self, tmp_config: Path):
        """A regression fence, not a reproduction — the base already preserved these.

        Corrected after independent review: the pre-fix ``upsert_agent_tools``
        already left sibling keys alone (it replaced only ``tools``); it was
        ``remove_agent_tools`` that popped the whole entry. This test pins the half
        that was already right, so the merge rewrite cannot regress it.
        """
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(
            json.dumps(
                {
                    "agent": {
                        "developer": {
                            "model": "custom/model",
                            "prompt": "stay terse",
                            "permission": {"bash": "deny"},
                            "tools": {},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        upsert_agent_tools("developer", ["cao-mcp-server"])
        entry = json.loads(tmp_config.read_text())["agent"]["developer"]
        assert entry["model"] == "custom/model"
        assert entry["prompt"] == "stay terse"
        assert entry["permission"] == {"bash": "deny"}
        assert entry["tools"] == {"cao-mcp-server*": True}

    def test_a_plugin_store_server_is_withdrawable_without_a_sidecar_row(
        self, tmp_config: Path, tmp_path: Path
    ):
        """Pre-sidecar installs stay cleanable: containment is the second proof."""
        store_root = tmp_path / "plugins"
        tmp_config.parent.mkdir(parents=True)
        tmp_config.write_text(
            json.dumps(
                {
                    "mcp": {
                        "demo-tools": {
                            "type": "local",
                            "command": [str(store_root / "demo" / "bin" / "srv")],
                            "enabled": True,
                        }
                    },
                    "agent": {"developer": {"tools": {"demo-tools*": True, "bash": False}}},
                }
            ),
            encoding="utf-8",
        )
        upsert_agent_tools("developer", ["cao-mcp-server"], plugin_store_roots=[store_root])
        tools = json.loads(tmp_config.read_text())["agent"]["developer"]["tools"]
        assert tools == {"bash": False, "cao-mcp-server*": True}


class TestGrantRecord:
    """The ``cao-grants.json`` sidecar is what makes withdrawal provable."""

    def test_the_sidecar_records_the_granted_keys_beside_the_config(self, tmp_config: Path):
        upsert_agent_tools("developer", ["cao-mcp-server", "demo-tools"])
        sidecar = tmp_config.with_name("cao-grants.json")
        assert sidecar.exists()
        record = json.loads(sidecar.read_text())
        assert record["version"] == 1
        assert record["agents"]["developer"] == ["cao-mcp-server*", "demo-tools*"]

    def test_a_corrupt_sidecar_warns_and_degrades_instead_of_raising(
        self, tmp_config: Path, caplog
    ):
        """A damaged record must not fail an install — only reduce what CAO can prove."""
        tmp_config.parent.mkdir(parents=True)
        tmp_config.with_name("cao-grants.json").write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", ["cao-mcp-server"])
        assert any("grant record" in r.getMessage() for r in caplog.records)
        tools = json.loads(tmp_config.read_text())["agent"]["developer"]["tools"]
        assert tools == {"cao-mcp-server*": True}

    @pytest.mark.parametrize(
        "content",
        [
            b'{"version": 1, "agents": {"other": ["other-tools*"]}',
            b'["not", "a", "record"]',
            b'{"version": 1, "agents": ["other"]}',
            b'{"version": 1, "agents": {"other": ["other-tools*"]}, "n": "\xff"}',
        ],
        ids=["bad-json", "not-an-object", "agents-not-an-object", "invalid-utf-8"],
    )
    @pytest.mark.parametrize("call", ["upsert", "remove"])
    def test_a_corrupt_sidecar_is_left_intact_and_named(
        self, tmp_config: Path, caplog, content: bytes, call: str
    ):
        """#1271: the grant writer fails closed, like record_mcp_entry (GH #737).

        Writing over the damaged file would drop every other agent's row, here
        "other"'s. It stays byte-identical, a warning names it, and the agent's
        own grant still lands in opencode.json.
        """
        tmp_config.parent.mkdir(parents=True)
        if call == "remove":
            upsert_agent_tools("developer", ["cao-mcp-server"])
        sidecar = tmp_config.with_name("cao-grants.json")
        sidecar.write_bytes(content)
        with caplog.at_level(logging.WARNING):
            if call == "upsert":
                upsert_agent_tools("developer", ["cao-mcp-server"])
            else:
                remove_agent_tools("developer")
        assert sidecar.read_bytes() == content
        warnings = [
            r.getMessage() for r in caplog.records if "not recording the grants" in r.getMessage()
        ]
        assert warnings and str(sidecar) in warnings[0], [r.getMessage() for r in caplog.records]
        tools = (
            json.loads(tmp_config.read_text()).get("agent", {}).get("developer", {}).get("tools")
        )
        if call == "upsert":
            assert tools == {"cao-mcp-server*": True}

    @pytest.mark.parametrize("damaged_first", [True, False], ids=["damaged", "intact"])
    def test_a_profile_grant_added_while_damaged_outlives_the_repair(
        self, tmp_config: Path, caplog, damaged_first: bool
    ):
        """Known behaviour (#1273 review): nothing records a grant added while the
        sidecar is damaged, so once the profile drops that server no proof covers
        its key, and it stays after the repair until it is removed by hand. With
        the sidecar intact, the same steps withdraw it. A second profile server the
        profile keeps is unaffected either way, and its row is pinned too.
        """
        tmp_config.parent.mkdir(parents=True)
        sidecar = tmp_config.with_name("cao-grants.json")
        if damaged_first:
            sidecar.write_text("{not json", encoding="utf-8")
        # An install while damaged (or not) adds two profile servers' grants.
        with caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", ["cao-mcp-server", "x-server"])
        if damaged_first:
            assert any("stays until removed by hand" in r.getMessage() for r in caplog.records), [
                r.getMessage() for r in caplog.records
            ]
            # Repaired by hand: the operator restores the row they know about.
            sidecar.write_text(
                '{"version": 1, "agents": {"developer": ["cao-mcp-server*"]}}', encoding="utf-8"
            )
        # The profile drops x-server, keeps cao-mcp-server, and is reinstalled twice.
        upsert_agent_tools("developer", ["cao-mcp-server"])
        upsert_agent_tools("developer", ["cao-mcp-server"])
        tools = json.loads(tmp_config.read_text())["agent"]["developer"]["tools"]
        assert ("x-server*" in tools) is damaged_first, tools
        assert tools["cao-mcp-server*"] is True
        assert json.loads(sidecar.read_text())["agents"] == {"developer": ["cao-mcp-server*"]}

    @pytest.mark.parametrize(
        "writer, content",
        [("grants", "{not json"), ("grants", '["not", "a", "record"]'), ("mcp", "{not json")],
        ids=["grants-unreadable", "grants-wrong-shape", "mcp-unreadable"],
    )
    def test_every_damaged_sidecar_warning_says_to_repair_it_by_hand(
        self, tmp_config: Path, caplog, writer: str, content: str
    ):
        """#1273 review: deleting the file is the easy fix, and it drops every
        agent's recorded grants, so each warning says to repair it by hand."""
        tmp_config.parent.mkdir(parents=True)
        tmp_config.with_name("cao-grants.json").write_text(content, encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            if writer == "grants":
                upsert_agent_tools("developer", ["cao-mcp-server"])
            else:
                record_mcp_entry("demo-tools", {"type": "local", "command": ["/x"]})
        messages = [r.getMessage() for r in caplog.records if "grant record" in r.getMessage()]
        assert messages
        for message in messages:
            assert "Repair the file by hand" in message, message
            assert "drops the recorded grants of every agent" in message, message

    @staticmethod
    def _read_only_dir(config_file: Path):
        """Make the directory that holds ``opencode.json`` and ``cao-grants.json`` read-only (mode 0500).

        ``opencode.json`` is written first, because the directory is read-only afterwards: the file itself
        stays writable in place, which is what lets an install update it while the sidecar cannot be created.
        """

        @contextlib.contextmanager
        def ctx():
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                pytest.skip("root writes into a read-only directory")
            config_file.parent.chmod(0o500)
            try:
                yield
            finally:
                config_file.parent.chmod(0o700)

        return ctx()

    @staticmethod
    def _write_failures(caplog) -> list:
        return [
            r.getMessage()
            for r in caplog.records
            if "Could not write the OpenCode grant record" in r.getMessage()
        ]

    def test_an_unwritable_sidecar_warning_says_a_profile_grant_can_stay(
        self, tmp_config: Path, caplog
    ):
        """#1283 review: when the sidecar cannot be written, only plugin servers'
        grants fall back to containment; a profile server's grant has no proof.
        Through ``upsert_agent_tools``, the caller that text is about (#1285 review)."""
        write_config({})
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", ["x-server"])
        messages = self._write_failures(caplog)
        assert len(messages) == 1, [r.getMessage() for r in caplog.records]
        message = messages[0]
        assert "Agent developer's recorded grants keep their old value:" in message
        assert "A plugin server's grant is still withdrawn by plugin-store containment" in message
        assert (
            "a profile server's grant added now stays if the profile drops that server before"
            " the agent is next installed with a writable sidecar." in message
        )
        assert "removed now" not in message, "nothing is removed unrecorded any more (#1289)"
        assert "will fall back to plugin-store containment when withdrawing them" not in message
        assert "x-server" not in message, "the warning must not carry a recorded value"
        assert "mcp." not in message, "no mcp fingerprint was being written"
        assert cfg_module.GRANT_RECORD_REPAIR_HINT not in message, "unwritable, not damaged"

    def test_the_grant_warning_through_upsert_agent_tools(self, tmp_config: Path, caplog):
        """#1285 review (haofeif): through the install step, so it shows which caller gets the text."""
        write_config({})
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", ["cao-mcp-server"])
        messages = self._write_failures(caplog)
        assert len(messages) == 1, [r.getMessage() for r in caplog.records]
        assert "Agent developer's recorded grants keep their old value:" in messages[0]
        assert "cao-grants.json" in messages[0] and "Permission denied" in messages[0]
        assert "fingerprint" not in messages[0] and "mcp." not in messages[0]
        # The install itself still completed: the grant is in opencode.json.
        assert read_config()["agent"]["developer"]["tools"]["cao-mcp-server*"] is True

    def test_an_unwritable_sidecar_warning_through_record_mcp_entry_names_the_entry(
        self, tmp_config: Path, caplog
    ):
        """#1285 review (fanhongy, P2): record_mcp_entry writes a fingerprint, not a grant. Its failure must say
        what is lost: after a CAO_HOME_DIR move the fingerprint is the only proof the entry is CAO's.
        """
        write_config({})
        entry = {
            "type": "local",
            "command": ["SENTINEL-CMD"],
            "environment": {"PLUGIN_ROOT": "/SENTINEL-ROOT"},
        }
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            record_mcp_entry("plug-srv", entry)
        messages = self._write_failures(caplog)
        assert len(messages) == 1, [r.getMessage() for r in caplog.records]
        message = messages[0]
        assert (
            "mcp.plug-srv goes unrecorded, so CAO falls back to the containment proof for it;"
            " after a CAO_HOME_DIR move CAO cannot prove the entry is its own." in message
        )
        assert "cao-grants.json" in message and "Permission denied" in message
        for wrong in ("recorded grants", "profile server's grant", "withdrawn by plugin-store"):
            assert (
                wrong not in message
            ), f"grant text leaked into the fingerprint warning: {wrong!r}"
        assert cfg_module.GRANT_RECORD_REPAIR_HINT not in message, "unwritable, not damaged"
        assert "SENTINEL" not in message, "the warning must not carry the entry's values"

    def test_a_failed_write_keeps_the_old_record_so_only_a_grant_added_now_loses_its_proof(
        self, tmp_config: Path, caplog
    ):
        """#1285 review (haofeif, P3-1 case 2 and probe P2): "written now has no proof left" was untrue for a grant
        the OLD record already has. A failed write keeps the old record; only what this install adds is unproven.
        """
        write_config({})
        upsert_agent_tools("developer", ["cao-mcp-server"])  # recorded: cao-mcp-server*
        assert cfg_module.read_grant_record()["agents"] == {"developer": ["cao-mcp-server*"]}
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_agent_tools(
                "developer", ["cao-mcp-server", "y-server"]
            )  # adds y-server*, cannot record it
        messages = self._write_failures(caplog)
        assert len(messages) == 1
        assert "a profile server's grant added now stays" in messages[0]
        assert "written now" not in messages[0] and "no proof left" not in messages[0]
        # The failed write kept the old record exactly...
        assert cfg_module.read_grant_record()["agents"] == {"developer": ["cao-mcp-server*"]}
        # ...so when the profile drops both servers, the old grant is withdrawn (it is still proven) and only the
        # grant the failed install added stays.
        upsert_agent_tools("developer", [])
        tools = read_config()["agent"]["developer"]["tools"]
        assert "cao-mcp-server*" not in tools
        assert tools["y-server*"] is True

    # -- #1289: a withdrawal is recorded BEFORE the key is removed -----------------------------------------------

    @staticmethod
    def _tools() -> dict:
        return read_config().get("agent", {}).get("developer", {}).get("tools", {})

    @staticmethod
    def _row():
        return cfg_module.read_grant_record()["agents"].get("developer")

    def test_a_withdrawal_that_cannot_be_recorded_leaves_the_grant_in_place(
        self, tmp_config: Path, caplog
    ):
        """#1289 (haofeif's probe P5 on #1285, step 2): the profile drops z-server while the sidecar is read-only.
        Taking the key out would leave the old row listing a key CAO had removed, so the key stays, and the warning
        says so."""
        write_config({})
        upsert_agent_tools("developer", ["z-server"])
        assert self._row() == ["z-server*"]
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", [])
        messages = self._write_failures(caplog)
        assert len(messages) == 1, [r.getMessage() for r in caplog.records]
        message = messages[0]
        assert "grants that the profile no longer names were left in opencode.json" in message
        assert "a grant is not removed while the record would still list it" in message
        assert "The next install tries again." in message
        assert "removed now" not in message and "go unrecorded" not in message
        assert "z-server" not in message, "the warning must not carry a recorded value"
        assert self._tools() == {"z-server*": True}, "fail closed: the grant stays"
        assert self._row() == ["z-server*"], "and the record still says it is CAO's"

    def test_after_the_withdrawal_is_finally_recorded_a_key_the_user_adds_stays_alone(
        self, tmp_config: Path
    ):
        """#1289, the end of the issue's story (haofeif's P5). The literal step 3 ("the user adds the key") is a no-op now:
        after the failed withdrawal the key is still there, so it is CAO's until the record can say otherwise. The next
        install that can write the record withdraws it, in order, and only after that is recorded is a key the user adds theirs. This
        passes on main as well (its writable install rewrites the stale row); it guards the end state.
        """
        write_config({})
        upsert_agent_tools("developer", ["z-server"])  # 1: recorded
        with self._read_only_dir(tmp_config):
            upsert_agent_tools("developer", [])  # 2: the record cannot be written
        upsert_agent_tools(
            "developer", []
        )  # the next install that can write it withdraws, in order
        assert "z-server*" not in self._tools()
        assert self._row() is None, "withdrawn and recorded"
        data = read_config()
        data["agent"]["developer"].setdefault("tools", {})[
            "z-server*"
        ] = True  # 3: the user adds it
        write_config(data)
        upsert_agent_tools("developer", [])  # 4: the next install
        assert self._tools() == {"z-server*": True}, "the user's key is left alone"

    def test_a_second_read_only_install_does_not_remove_a_key_added_by_hand(
        self, tmp_config: Path, caplog
    ):
        """#1289, haofeif's probe A on #1290 (review 5851860126, P3-1): after a failed withdrawal and a hand-added key,
        EVERY following install removed it until one could write the record, so a second read-only install was
        enough. Now the key is CAO's grant until the record can say otherwise: read-only installs keep it, and only
        the first install that can write the record withdraws it.
        """
        write_config({})
        upsert_agent_tools("developer", ["z-server"])
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", [])
            data = read_config()
            data["agent"]["developer"]["tools"][
                "z-server*"
            ] = True  # "added by hand": it never left
            write_config(data)
            upsert_agent_tools("developer", [])  # the second read-only install
        assert self._tools() == {"z-server*": True}
        assert self._row() == ["z-server*"]
        assert len(self._write_failures(caplog)) == 2, "each failed write warns again"

    def test_remove_agent_tools_records_the_withdrawal_first_too(self, tmp_config: Path, caplog):
        """#1289, haofeif's probe A2: the install path for an agent with no MCP servers."""
        write_config({})
        upsert_agent_tools("developer", ["z-server"])
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            remove_agent_tools("developer")
        assert len(self._write_failures(caplog)) == 1
        assert self._tools() == {"z-server*": True}
        assert self._row() == ["z-server*"]
        remove_agent_tools("developer")
        assert "developer" not in read_config().get("agent", {}), "withdrawn, entry pruned"
        assert self._row() is None

    def test_a_mixed_install_adds_the_new_grant_and_holds_the_old_one_back(
        self, tmp_config: Path, caplog
    ):
        write_config({})
        upsert_agent_tools("developer", ["z-server"])
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", ["a-server"])  # drops z-server, adds a-server
        assert self._tools() == {"a-server*": True, "z-server*": True}
        assert self._row() == ["z-server*"]
        messages = self._write_failures(caplog)
        assert len(messages) == 2, "two different losses, two different texts"
        assert "were left in opencode.json" in messages[0]
        assert "a profile server's grant added now stays" in messages[1]
        upsert_agent_tools("developer", ["a-server"])  # a writable install converges
        assert self._tools() == {"a-server*": True}
        assert self._row() == ["a-server*"]

    def test_when_opencode_json_cannot_be_written_the_old_row_is_put_back(
        self, tmp_config: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1289: the record was written first, then opencode.json could not be. The install fails (as it always did),
        and the old row is put back so the key and its row agree and the next install withdraws it.
        """
        write_config({})
        upsert_agent_tools("developer", ["z-server"])

        def boom(data):
            raise OSError("disk full")

        with (
            monkeypatch.context() as patched
        ):  # scoped: undo() would also undo tmp_config's redirect
            patched.setattr(cfg_module, "write_config", boom)
            with pytest.raises(OSError, match="disk full"):
                upsert_agent_tools("developer", [])
        assert self._tools() == {"z-server*": True}
        assert self._row() == ["z-server*"], "restored"
        upsert_agent_tools("developer", [])
        assert "z-server*" not in self._tools()
        assert self._row() is None

    def test_if_the_old_row_cannot_be_put_back_either_the_warning_says_the_grant_stays(
        self, tmp_config: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ):
        """The worst case of #1289, reached for real: the record was written, opencode.json then could not be, and the
        directory is no longer writable for the restore either. The grant stays without a row, and the warning says so
        (that is the one state CAO cannot repair: delete the key by hand)."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root writes into a read-only directory")
        write_config({})
        upsert_agent_tools("developer", ["z-server"])

        def boom(data):
            tmp_config.parent.chmod(0o500)  # the restore that follows cannot write the sidecar
            raise OSError("disk full")

        try:
            with monkeypatch.context() as patched, caplog.at_level(logging.WARNING):
                patched.setattr(cfg_module, "write_config", boom)
                with pytest.raises(OSError, match="disk full"):
                    upsert_agent_tools("developer", [])
        finally:
            tmp_config.parent.chmod(0o700)
        messages = self._write_failures(caplog)
        assert len(messages) == 1, [r.getMessage() for r in caplog.records]
        assert (
            "could not be put back in the record, so they stay in opencode.json with no row until removed by hand"
            in messages[0]
        )
        assert "z-server" not in messages[0], "the warning must not carry a recorded value"
        assert self._tools() == {"z-server*": True}
        assert self._row() is None, "withdrawn in the record, still in the file: the lingering case"

    def test_a_failed_config_write_that_is_not_a_withdrawal_restores_nothing(
        self, tmp_config: Path, monkeypatch: pytest.MonkeyPatch
    ):
        write_config({})

        def boom(data):
            raise OSError("disk full")

        with monkeypatch.context() as patched:
            patched.setattr(cfg_module, "write_config", boom)
            with pytest.raises(OSError, match="disk full"):
                upsert_agent_tools("developer", ["a-server"])
        assert self._row() is None, "no row was written for a grant that never landed"

    def test_a_grant_that_only_containment_proves_is_withdrawn_even_when_the_record_cannot_be_written(
        self, tmp_config: Path, tmp_path: Path, caplog
    ):
        """Containment is a proof that does not need the record, so it is not held back."""
        store_root = tmp_path / "plugins"
        entry = {"type": "local", "command": [str(store_root / "demo" / "bin" / "srv")]}
        write_config(
            {
                "mcp": {"demo-tools": entry},
                "agent": {"developer": {"tools": {"demo-tools*": True, "bash": False}}},
            }
        )
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", ["cao-mcp-server"], plugin_store_roots=[store_root])
        assert self._tools() == {"bash": False, "cao-mcp-server*": True}
        messages = self._write_failures(caplog)
        assert len(messages) == 1 and "added now stays" in messages[0]

    def test_a_grant_that_the_record_and_containment_both_prove_is_not_held_back(
        self, tmp_config: Path, tmp_path: Path, caplog
    ):
        store_root = tmp_path / "plugins"
        entry = {"type": "local", "command": [str(store_root / "demo" / "bin" / "srv")]}
        write_config({"mcp": {"demo-tools": entry}})
        upsert_agent_tools("developer", ["demo-tools"], plugin_store_roots=[store_root])
        assert self._row() == ["demo-tools*"]
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_agent_tools("developer", [], plugin_store_roots=[store_root])
        assert "demo-tools*" not in self._tools()
        assert not any(
            "were left in opencode.json" in m for m in self._write_failures(caplog)
        ), "nothing was held back"

    def test_an_unreadable_sidecar_holds_nothing_back_and_still_warns(
        self, tmp_config: Path, caplog
    ):
        """No readable record, no proof from it: nothing it could vouch for is withdrawn, as before (#1273).
        Nothing is held back either: with no readable row there is no withdrawal to hold back. The file is
        left byte-for-byte as it was."""
        write_config({})
        upsert_agent_tools("developer", ["z-server"])
        sidecar = tmp_config.with_name("cao-grants.json")
        sidecar.write_text("{not json", encoding="utf-8")
        before = sidecar.read_bytes()
        with caplog.at_level(logging.WARNING):
            upsert_agent_tools(
                "developer", ["a-server"]
            )  # non-empty, so a lenient writer WOULD write
        assert self._tools() == {
            "z-server*": True,
            "a-server*": True,
        }, "z unprovable, so left in place"
        assert sidecar.read_bytes() == before, "the damaged record is not replaced"
        assert any("grant record" in r.getMessage() for r in caplog.records)

    def test_the_record_functions_report_whether_the_record_was_written(self, tmp_config: Path):
        """The answer a withdrawal depends on."""
        write_config({})
        record = cfg_module._record_grants
        assert record("developer", ["x*"], lost="l") is True  # written
        assert record("developer", ["x*"], lost="l") is True  # already says so
        assert record("developer", [], lost="l") is True  # cleared
        assert record("developer", [], lost="l") is True  # nothing to clear
        with self._read_only_dir(tmp_config):
            assert record("developer", ["y*"], lost="l") is False  # cannot write
        tmp_config.with_name("cao-grants.json").write_text("{not json", encoding="utf-8")
        assert record("developer", ["y*"], lost="l") is False  # unreadable

    def test_one_plugin_servers_install_logs_two_different_warnings_and_one_names_the_entry(
        self, tmp_config: Path, caplog
    ):
        """#1285 review (haofeif, P3-1 and probe P3): the install steps for one plugin server (upsert_mcp_server,
        record_mcp_entry, upsert_agent_tools) logged the SAME grant text twice and never named the entry.
        """
        write_config({})
        entry = {"type": "local", "command": ["x"], "environment": {"PLUGIN_ROOT": "/p"}}
        with self._read_only_dir(tmp_config), caplog.at_level(logging.WARNING):
            upsert_mcp_server("plug-srv", entry)
            record_mcp_entry("plug-srv", entry)
            upsert_agent_tools("developer", ["plug-srv"])
        messages = self._write_failures(caplog)
        assert len(messages) == 2, messages
        assert messages[0] != messages[1]
        assert "mcp.plug-srv goes unrecorded" in messages[0]
        assert "Agent developer's recorded grants keep their old value:" in messages[1]
        assert sum("mcp.plug-srv" in m for m in messages) == 1

    @pytest.mark.parametrize("damage", ["mode-000", "directory"])
    def test_an_unreadable_sidecar_is_left_intact_and_named(self, tmp_config: Path, caplog, damage):
        if damage == "mode-000" and hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        tmp_config.parent.mkdir(parents=True)
        sidecar = tmp_config.with_name("cao-grants.json")
        if damage == "directory":
            sidecar.mkdir()
        else:
            sidecar.write_text('{"version": 1, "agents": {"other": ["x*"]}}', encoding="utf-8")
            sidecar.chmod(0)
        try:
            with caplog.at_level(logging.WARNING):
                upsert_agent_tools("developer", ["cao-mcp-server"])
        finally:
            if not sidecar.is_dir():
                sidecar.chmod(0o600)
        if damage == "directory":
            assert sidecar.is_dir() and not any(sidecar.iterdir())
        else:
            assert json.loads(sidecar.read_text()) == {"version": 1, "agents": {"other": ["x*"]}}
        assert any(
            "not recording the grants" in r.getMessage() and str(sidecar) in r.getMessage()
            for r in caplog.records
        )


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

    def test_a_true_noop_does_not_add_an_empty_agent_section(self, tmp_config: Path):
        """Reported by independent review: the reconcile materialised ``"agent": {}``.

        The pre-fix ``remove_agent_tools`` left a config with no ``agent`` section
        completely untouched. Rewriting the file to add an empty one is a visible
        change to a user's config in exchange for nothing.
        """
        write_config({"$schema": "https://opencode.ai/config.json"})
        before = tmp_config.read_text(encoding="utf-8")

        remove_agent_tools("nonexistent")

        assert tmp_config.read_text(encoding="utf-8") == before

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

    def test_a_users_agent_configuration_survives_removal(self, tmp_config: Path):
        """Reproduced by review 3 on #584: the whole ``agent.<id>`` entry was popped.

        Uninstalling a CAO agent discarded the user's ``model``, ``prompt`` and
        tool policy for it. Only CAO's grant may be withdrawn.
        """
        upsert_agent_tools("developer", ["cao-mcp-server"])
        data = json.loads(tmp_config.read_text())
        data["agent"]["developer"]["model"] = "custom/model"
        data["agent"]["developer"]["tools"]["bash"] = False
        write_config(data)

        remove_agent_tools("developer")
        entry = json.loads(tmp_config.read_text())["agent"]["developer"]
        assert entry["model"] == "custom/model"
        assert entry["tools"] == {"bash": False}

    def test_an_entry_holding_nothing_but_caos_grant_is_dropped_entirely(self, tmp_config: Path):
        """No user state to preserve ⇒ restore the pre-install shape, no litter."""
        upsert_agent_tools("developer", ["cao-mcp-server"])
        remove_agent_tools("developer")
        data = json.loads(tmp_config.read_text())
        assert "developer" not in data.get("agent", {})
        # The sidecar row goes with it, so a later install starts from a clean slate.
        record = json.loads(tmp_config.with_name("cao-grants.json").read_text())
        assert "developer" not in record["agents"]


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

    def test_a_pre_fix_entry_lacking_cwd_is_still_owned_through_its_environment(
        self, tmp_path: Path
    ):
        """Adding ``cwd`` must not turn CAO's own older entries into collisions.

        An entry written before review 3's ``cwd`` fix has no ``cwd``, so the
        byte-equality clause now fails against the candidate. Ownership still holds
        through the in-store clause, because the mapper injects in-store
        ``PLUGIN_ROOT``/``PLUGIN_DATA`` into ``environment``. Without that, every
        pre-fix plugin entry would be reported as a user collision on the next
        refresh and never gain its ``cwd``.
        """
        root = tmp_path / "plugins"
        plugin_root = root / "demo"
        existing = {
            "type": "local",
            "command": ["demo-server"],
            "enabled": True,
            "environment": {"PLUGIN_ROOT": str(plugin_root)},
        }
        candidate = {**existing, "cwd": str(plugin_root)}
        assert existing != candidate  # the byte-equality clause cannot fire
        assert is_cao_owned_mcp_entry(existing, candidate, plugin_store_roots=[root]) is True

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


class TestMcpEntryRecord:
    """#1154: ``mcp`` entry ownership is recorded beside the grant record."""

    ENTRY = {"type": "local", "command": ["/old-home/agent-plugins/demo/server"], "enabled": True}

    def test_the_fingerprint_ignores_enabled_and_nothing_else(self):
        disabled = {**self.ENTRY, "enabled": False}
        assert mcp_entry_fingerprint(disabled) == mcp_entry_fingerprint(self.ENTRY)
        for edit in ({"command": ["/other"]}, {"cwd": "/w"}, {"environment": {"A": "1"}}):
            assert mcp_entry_fingerprint({**self.ENTRY, **edit}) != mcp_entry_fingerprint(
                self.ENTRY
            )

    @staticmethod
    def _entry_rooted_at(root: Path) -> dict:
        return {
            "type": "local",
            "command": [str(root / "server"), "--root", str(root)],
            "environment": {"PLUGIN_ROOT": str(root), "PLUGIN_DATA": str(root) + "-data"},
            "cwd": str(root),
            "enabled": True,
        }

    def test_a_recorded_entry_is_owned_only_once_its_plugin_root_is_gone(
        self, tmp_config: Path, tmp_path: Path
    ):
        """#1253 review: while another home's plugin root exists, the record proves nothing."""
        old_root = tmp_path / "home-a" / "agent-plugins" / "demo"
        old_root.mkdir(parents=True)
        entry = self._entry_rooted_at(old_root)
        record_mcp_entry("demo-tools", entry)
        candidate = self._entry_rooted_at(tmp_path / "home-b" / "agent-plugins" / "demo")
        roots = [tmp_path / "home-b" / "agent-plugins"]

        # Home A is live: its plugin root exists, so home B may not take the entry.
        assert is_recorded_mcp_entry("demo-tools", entry)
        assert not is_orphaned_recorded_mcp_entry("demo-tools", entry)
        assert not is_cao_owned_mcp_entry(
            entry, candidate, plugin_store_roots=roots, name="demo-tools"
        )

        # Home A is gone: now the record alone proves CAO wrote it.
        shutil.rmtree(tmp_path / "home-a")
        assert is_orphaned_recorded_mcp_entry("demo-tools", entry)
        assert is_cao_owned_mcp_entry(entry, candidate, plugin_store_roots=roots, name="demo-tools")
        # Without the name, the record is not consulted at all.
        assert not is_cao_owned_mcp_entry(entry, candidate, plugin_store_roots=roots)
        # A CAO-disabled copy of the recorded entry is still CAO's.
        assert is_orphaned_recorded_mcp_entry("demo-tools", {**entry, "enabled": False})

    def test_an_unrecorded_entry_is_never_orphaned_even_when_its_root_is_gone(
        self, tmp_config: Path, tmp_path: Path
    ):
        entry = self._entry_rooted_at(tmp_path / "never-existed" / "demo")
        assert not is_orphaned_recorded_mcp_entry("demo-tools", entry)

    @pytest.mark.parametrize(
        "environment",
        [None, {}, {"PLUGIN_ROOT": ""}, {"PLUGIN_ROOT": "relative/demo"}, {"PLUGIN_ROOT": 7}],
        ids=["no-environment", "no-plugin-root", "empty", "relative", "not-a-string"],
    )
    def test_an_entry_without_an_absolute_plugin_root_is_never_orphaned(
        self, tmp_config: Path, environment
    ):
        entry = {"type": "local", "command": ["/gone/server"], "enabled": True}
        if environment is not None:
            entry["environment"] = environment
        record_mcp_entry("demo-tools", entry)
        assert is_recorded_mcp_entry("demo-tools", entry)
        assert not is_orphaned_recorded_mcp_entry("demo-tools", entry)

    def test_a_dangling_symlink_plugin_root_counts_as_present(
        self, tmp_config: Path, tmp_path: Path
    ):
        root = tmp_path / "demo"
        root.symlink_to(tmp_path / "nowhere")
        entry = self._entry_rooted_at(root)
        record_mcp_entry("demo-tools", entry)
        assert not is_orphaned_recorded_mcp_entry("demo-tools", entry)

    def test_the_record_is_per_name_and_per_content(self, tmp_config: Path):
        record_mcp_entry("demo-tools", self.ENTRY)
        assert not is_recorded_mcp_entry("other-tools", self.ENTRY)
        assert not is_recorded_mcp_entry("demo-tools", {**self.ENTRY, "command": ["/user"]})

    def test_mcp_and_grant_rows_share_the_sidecar_without_clobbering(self, tmp_config: Path):
        record_mcp_entry("demo-tools", self.ENTRY)
        upsert_agent_tools("developer", ["demo-tools"])
        record_mcp_entry("more-tools", {**self.ENTRY, "command": ["/x"]})
        record = json.loads(tmp_config.with_name("cao-grants.json").read_text())
        assert record["version"] == 1
        assert record["agents"] == {"developer": ["demo-tools*"]}
        assert set(record["mcp"]) == {"demo-tools", "more-tools"}

    def test_a_malformed_mcp_row_is_ignored_not_raised(self, tmp_config: Path):
        tmp_config.parent.mkdir(parents=True)
        tmp_config.with_name("cao-grants.json").write_text(
            json.dumps({"version": 1, "agents": {}, "mcp": ["demo-tools"]}), encoding="utf-8"
        )
        assert not is_recorded_mcp_entry("demo-tools", self.ENTRY)
        record_mcp_entry("demo-tools", self.ENTRY)
        assert is_recorded_mcp_entry("demo-tools", self.ENTRY)

    def test_a_corrupt_sidecar_is_left_intact_not_rewritten(self, tmp_config: Path, caplog):
        """Fail closed (GH #737): recording must not replace an unreadable sidecar."""
        tmp_config.parent.mkdir(parents=True)
        sidecar = tmp_config.with_name("cao-grants.json")
        sidecar.write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            record_mcp_entry("demo-tools", self.ENTRY)
        assert sidecar.read_text(encoding="utf-8") == "{not json"
        assert any("not recording mcp.demo-tools" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("damage", ["invalid-utf-8", "mode-000", "directory"])
    def test_an_unreadable_sidecar_is_left_intact_not_raised(
        self, tmp_config: Path, caplog, damage: str
    ):
        """#1253 review: not UTF-8, unreadable, or a directory is refused like bad JSON."""
        if damage == "mode-000" and hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        tmp_config.parent.mkdir(parents=True)
        sidecar = tmp_config.with_name("cao-grants.json")
        content = b'{"version": 1, "agents": {"other": ["x*"]}, "n": "\xff"}'
        if damage == "directory":
            sidecar.mkdir()
        else:
            sidecar.write_bytes(content)
            if damage == "mode-000":
                sidecar.chmod(0)
        try:
            with caplog.at_level(logging.WARNING):
                record_mcp_entry("demo-tools", self.ENTRY)
        finally:
            if not sidecar.is_dir():
                sidecar.chmod(0o600)
        if damage == "directory":
            assert sidecar.is_dir() and not any(sidecar.iterdir())
        else:
            assert sidecar.read_bytes() == content
        warnings = [
            r.getMessage()
            for r in caplog.records
            if "not recording mcp.demo-tools" in r.getMessage()
        ]
        assert warnings and str(sidecar) in warnings[0], warnings

    @pytest.mark.parametrize(
        "content",
        ['["not", "a", "record"]', '{"version": 1, "agents": ["developer"]}'],
        ids=["not-an-object", "agents-not-an-object"],
    )
    def test_a_wrongly_shaped_sidecar_is_left_intact_not_rewritten(
        self, tmp_config: Path, caplog, content: str
    ):
        """Valid JSON that is not a grant record is refused the same way as invalid JSON."""
        tmp_config.parent.mkdir(parents=True)
        sidecar = tmp_config.with_name("cao-grants.json")
        sidecar.write_text(content, encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            record_mcp_entry("demo-tools", self.ENTRY)
        assert sidecar.read_text(encoding="utf-8") == content
        assert any("unexpected shape" in r.getMessage() for r in caplog.records)
