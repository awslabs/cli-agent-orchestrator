"""Unit tests for the opencode.json read-modify-write helper."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import cli_agent_orchestrator.utils.opencode_config as cfg_module
from cli_agent_orchestrator.utils.opencode_config import (
    ensure_skills_symlink,
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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Creating symlinks on Windows requires Developer Mode or elevated privileges",
    )
    def test_creates_symlink_when_target_missing(self, symlink_env):
        config_dir = symlink_env["config_dir"]
        skills_dir = symlink_env["skills_dir"]
        target = config_dir / "skills"

        ensure_skills_symlink()

        assert target.is_symlink()
        assert target.resolve() == skills_dir.resolve()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Creating symlinks on Windows requires Developer Mode or elevated privileges",
    )
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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Creating symlinks on Windows requires Developer Mode or elevated privileges",
    )
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

    def test_real_cao_mcp_server_entry(self):
        """Full translation of the cao-mcp-server entry as it appears in agent profiles."""
        cao_cfg = {
            "type": "stdio",
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/awslabs/cli-agent-orchestrator.git@main",
                "cao-mcp-server",
            ],
        }
        result = translate_mcp_server_config(cao_cfg)
        assert result == {
            "type": "local",
            "command": [
                "uvx",
                "--from",
                "git+https://github.com/awslabs/cli-agent-orchestrator.git@main",
                "cao-mcp-server",
            ],
            "enabled": True,
        }


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
