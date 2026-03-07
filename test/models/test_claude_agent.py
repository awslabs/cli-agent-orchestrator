"""Tests for Claude Code agent configuration model."""

import json

import pytest

from cli_agent_orchestrator.models.claude_agent import ClaudeAgentConfig


class TestClaudeAgentConfigConstruction:
    """Tests for ClaudeAgentConfig model construction."""

    def test_minimal_config(self):
        """Test constructing config with only required fields."""
        config = ClaudeAgentConfig(name="test", description="Test agent")
        assert config.name == "test"
        assert config.description == "Test agent"
        assert config.model is None
        assert config.allowedTools is None
        assert config.tools is None
        assert config.mcpServers is None
        assert config.hooks is None

    def test_full_config(self):
        """Test constructing config with all fields populated."""
        config = ClaudeAgentConfig(
            name="full-agent",
            description="Full agent",
            model="claude-sonnet-4-6",
            allowedTools=["Read", "Edit", "Bash(git *)"],
            disallowedTools=["Bash(git log *)", "Bash(git diff *)", "Edit"],
            tools=["Bash", "Edit", "Read"],
            mcpServers={"my-server": {"command": "my-cmd"}},
            hooks={
                "PreToolUse": [
                    {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo test"}]}
                ]
            },
        )
        assert config.model == "claude-sonnet-4-6"
        assert len(config.allowedTools) == 3
        assert len(config.disallowedTools) == 3
        assert len(config.tools) == 3
        assert "my-server" in config.mcpServers
        assert "PreToolUse" in config.hooks


class TestToCliFlags:
    """Tests for ClaudeAgentConfig.to_cli_flags() method."""

    def test_empty_flags_when_no_optionals(self):
        """Test that to_cli_flags returns empty list when no optional fields are set."""
        config = ClaudeAgentConfig(name="test", description="Test")
        assert config.to_cli_flags() == []

    def test_model_flag(self):
        """Test --model flag generation."""
        config = ClaudeAgentConfig(name="test", description="Test", model="sonnet")
        flags = config.to_cli_flags()
        assert flags == ["--model", "sonnet"]

    def test_allowed_tools_flag(self):
        """Test --allowedTools flag generates space-separated list (each tool is a separate arg)."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            allowedTools=["Read", "Edit", "Bash(git *)"],
        )
        flags = config.to_cli_flags()
        assert flags == ["--allowedTools", "Read", "Edit", "Bash(git *)"]

    def test_disallowed_tools_flag(self):
        """Test --disallowedTools flag generates space-separated list (each tool is a separate arg)."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            disallowedTools=["Bash(git log *)", "Bash(git diff *)", "Edit"],
        )
        flags = config.to_cli_flags()
        assert flags == ["--disallowedTools", "Bash(git log *)", "Bash(git diff *)", "Edit"]

    def test_tools_flag(self):
        """Test --tools flag generation with comma-separated list."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            tools=["Bash", "Edit", "Read"],
        )
        flags = config.to_cli_flags()
        assert flags == ["--tools", "Bash,Edit,Read"]

    def test_hooks_flag_via_settings(self):
        """Test hooks passed via --settings flag as JSON."""
        hook_config = {
            "PreToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo pre"}]}
            ]
        }
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            hooks=hook_config,
        )
        flags = config.to_cli_flags()
        assert flags[0] == "--settings"
        parsed = json.loads(flags[1])
        assert "hooks" in parsed
        assert parsed["hooks"] == hook_config

    def test_mcp_servers_excluded_without_terminal_id(self):
        """Test that mcpServers are NOT included in CLI flags when terminal_id is not provided."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            mcpServers={"server1": {"command": "test-cmd"}},
        )
        flags = config.to_cli_flags()
        assert flags == []
        assert "--mcp-config" not in flags

    def test_mcp_servers_included_with_terminal_id(self):
        """Test that mcpServers ARE included when terminal_id is provided."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            mcpServers={"server1": {"command": "test-cmd"}},
        )
        flags = config.to_cli_flags(terminal_id="term-42")
        assert "--mcp-config" in flags
        mcp_json = flags[flags.index("--mcp-config") + 1]
        parsed = json.loads(mcp_json)
        assert "mcpServers" in parsed
        assert parsed["mcpServers"]["server1"]["env"]["CAO_TERMINAL_ID"] == "term-42"

    def test_mcp_preserves_existing_env(self):
        """Test that existing env vars are preserved when injecting CAO_TERMINAL_ID."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            mcpServers={"srv": {"command": "cmd", "env": {"MY_VAR": "val"}}},
        )
        flags = config.to_cli_flags(terminal_id="term-1")
        mcp_json = flags[flags.index("--mcp-config") + 1]
        parsed = json.loads(mcp_json)
        env = parsed["mcpServers"]["srv"]["env"]
        assert env["MY_VAR"] == "val"
        assert env["CAO_TERMINAL_ID"] == "term-1"

    def test_mcp_does_not_override_existing_terminal_id(self):
        """Test that existing CAO_TERMINAL_ID is not overwritten."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            mcpServers={"srv": {"command": "cmd", "env": {"CAO_TERMINAL_ID": "user-id"}}},
        )
        flags = config.to_cli_flags(terminal_id="term-99")
        mcp_json = flags[flags.index("--mcp-config") + 1]
        parsed = json.loads(mcp_json)
        assert parsed["mcpServers"]["srv"]["env"]["CAO_TERMINAL_ID"] == "user-id"

    def test_all_flags_combined(self):
        """Test generating all supported flags together."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            model="opus",
            allowedTools=["Read"],
            disallowedTools=["Edit"],
            tools=["Bash", "Read"],
            hooks={"PostToolUse": []},
            mcpServers={"s1": {"command": "cmd"}},
        )
        flags = config.to_cli_flags(terminal_id="term-1")
        assert "--model" in flags
        assert "opus" in flags
        assert "--allowedTools" in flags
        assert "Read" in flags
        assert "--disallowedTools" in flags
        assert "Edit" in flags
        assert "--tools" in flags
        assert "Bash,Read" in flags
        assert "--settings" in flags
        assert "--mcp-config" in flags

    def test_flag_ordering(self):
        """Test flag order: model, allowedTools, disallowedTools, tools, settings."""
        config = ClaudeAgentConfig(
            name="test",
            description="Test",
            model="haiku",
            allowedTools=["Read"],
            disallowedTools=["Edit"],
            tools=["Bash"],
            hooks={"PreToolUse": []},
        )
        flags = config.to_cli_flags()
        model_idx = flags.index("--model")
        allowed_idx = flags.index("--allowedTools")
        disallowed_idx = flags.index("--disallowedTools")
        tools_idx = flags.index("--tools")
        settings_idx = flags.index("--settings")
        assert model_idx < allowed_idx < disallowed_idx < tools_idx < settings_idx


class TestJsonSerialization:
    """Tests for ClaudeAgentConfig JSON serialization."""

    def test_exclude_none(self):
        """Test that None fields are excluded from JSON serialization."""
        config = ClaudeAgentConfig(name="test", description="Test", model="sonnet")
        data = json.loads(config.model_dump_json(exclude_none=True))
        assert "model" in data
        assert "allowedTools" not in data
        assert "disallowedTools" not in data
        assert "tools" not in data
        assert "hooks" not in data
        assert "mcpServers" not in data
