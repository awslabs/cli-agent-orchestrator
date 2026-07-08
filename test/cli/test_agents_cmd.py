"""Tests for cao agents command group."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.agents import (
    _validate_frontmatter,
    agents,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def sample_profile_valid(tmp_path: Path) -> Path:
    """Create a valid agent profile .md file."""
    content = """---
name: test-agent
description: A test agent
allowedTools:
  - execute_bash
  - fs_read
mcpServers:
  cao-mcp-server:
    type: stdio
    command: uvx
    args:
      - "--from"
      - "git+https://github.com/awslabs/cli-agent-orchestrator.git@main"
      - "cao-mcp-server"
---

# Test Agent

This is a test agent.
"""
    p = tmp_path / "test-agent.md"
    p.write_text(content)
    return p


@pytest.fixture
def sample_profile_deprecated(tmp_path: Path) -> Path:
    """Create a profile with deprecated autoApproveTools."""
    content = """---
name: bad-agent
description: Uses deprecated field
autoApproveTools: true
---

# Bad Agent
"""
    p = tmp_path / "bad-agent.md"
    p.write_text(content)
    return p


@pytest.fixture
def sample_profile_invalid_role(tmp_path: Path) -> Path:
    """Create a profile with an invalid role."""
    content = """---
name: wrong-role
description: Invalid role value
role: worker
allowedTools:
  - execute_bash
---

# Wrong Role Agent
"""
    p = tmp_path / "wrong-role.md"
    p.write_text(content)
    return p


@pytest.fixture
def sample_profile_bad_tools(tmp_path: Path) -> Path:
    """Create a profile with unrecognized allowedTools vocabulary."""
    content = """---
name: bad-tools
description: Uses shell syntax that CAO doesnt recognize
allowedTools:
  - "shell:aws sqs*"
  - "shell:jq*"
---

# Bad Tools Agent
"""
    p = tmp_path / "bad-tools.md"
    p.write_text(content)
    return p


class TestValidateFrontmatter:
    """Unit tests for _validate_frontmatter."""

    def test_valid_profile(self):
        meta = {
            "name": "test-agent",
            "description": "A test",
            "allowedTools": ["execute_bash", "fs_read"],
        }
        assert _validate_frontmatter(meta) == []

    def test_missing_name(self):
        meta = {"description": "no name"}
        msgs = _validate_frontmatter(meta)
        assert any("[error]" in m and "name" in m for m in msgs)

    def test_deprecated_field(self):
        meta = {"name": "x", "autoApproveTools": True}
        msgs = _validate_frontmatter(meta)
        assert any("deprecated" in m for m in msgs)

    def test_invalid_role(self):
        meta = {"name": "x", "role": "worker"}
        msgs = _validate_frontmatter(meta)
        assert any("[error]" in m and "role" in m for m in msgs)

    def test_valid_role(self):
        meta = {"name": "x", "role": "developer"}
        assert _validate_frontmatter(meta) == []

    def test_unrecognized_tool(self):
        meta = {"name": "x", "allowedTools": ["shell:aws*"]}
        msgs = _validate_frontmatter(meta)
        assert any("[warn]" in m and "shell:aws*" in m for m in msgs)

    def test_valid_tools(self):
        meta = {"name": "x", "allowedTools": ["execute_bash", "@cao-mcp-server"]}
        assert _validate_frontmatter(meta) == []


class TestAgentsListCommand:
    """Tests for cao agents list."""

    def test_list_runs(self, runner: CliRunner):
        """Test that list command runs without error."""
        result = runner.invoke(agents, ["list"])
        assert result.exit_code == 0

    def test_list_shows_header(self, runner: CliRunner):
        """Test that list shows column headers."""
        result = runner.invoke(agents, ["list"])
        # Either shows profiles or 'No agent profiles found'
        assert "NAME" in result.output or "No agent profiles" in result.output


class TestAgentsShowCommand:
    """Tests for cao agents show."""

    def test_show_valid_file(self, runner: CliRunner, sample_profile_valid: Path):
        result = runner.invoke(agents, ["show", str(sample_profile_valid)])
        assert result.exit_code == 0
        assert "test-agent" in result.output
        assert "allowedTools" in result.output

    def test_show_not_found(self, runner: CliRunner):
        result = runner.invoke(agents, ["show", "nonexistent-agent-xyz"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestAgentsValidateCommand:
    """Tests for cao agents validate."""

    def test_validate_valid_profile(self, runner: CliRunner, sample_profile_valid: Path):
        result = runner.invoke(agents, ["validate", str(sample_profile_valid)])
        assert result.exit_code == 0
        assert "✓" in result.output

    def test_validate_deprecated_field(self, runner: CliRunner, sample_profile_deprecated: Path):
        result = runner.invoke(agents, ["validate", str(sample_profile_deprecated)])
        # autoApproveTools triggers additionalProperties error (blocking)
        assert result.exit_code == 1
        assert "autoApproveTools" in result.output

    def test_validate_invalid_role(self, runner: CliRunner, sample_profile_invalid_role: Path):
        result = runner.invoke(agents, ["validate", str(sample_profile_invalid_role)])
        assert result.exit_code == 1
        assert "role" in result.output

    def test_validate_bad_tools(self, runner: CliRunner, sample_profile_bad_tools: Path):
        result = runner.invoke(agents, ["validate", str(sample_profile_bad_tools)])
        # Bad tools are warnings, not errors, so exit 0
        assert result.exit_code == 0
        assert "shell:aws" in result.output

    def test_validate_not_found(self, runner: CliRunner):
        result = runner.invoke(agents, ["validate", "nonexistent.md"])
        assert result.exit_code == 1


class TestAgentsRemoveCommand:
    """Tests for cao agents remove."""

    def test_remove_not_found(self, runner: CliRunner):
        result = runner.invoke(agents, ["remove", "nonexistent-agent-xyz", "-y"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestAgentsTemplatesCommand:
    """Tests for cao agents templates."""

    def test_templates_lists_all(self, runner: CliRunner):
        result = runner.invoke(agents, ["templates"])
        assert result.exit_code == 0
        assert "aws/stepfunction" in result.output
        assert "aws/cloudwatch-logs" in result.output
        assert "7 template(s) available" in result.output

    def test_templates_shows_description(self, runner: CliRunner):
        result = runner.invoke(agents, ["templates"])
        assert "Trigger and monitor" in result.output


class TestAgentsCreateCommand:
    """Tests for cao agents create."""

    def test_create_writes_file(self, runner: CliRunner, tmp_path: Path):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({
            "profile": "test",
            "region": "us-east-1",
            "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:X",
        }))
        result = runner.invoke(agents, [
            "create", "-t", "aws/stepfunction",
            "-c", str(config), "-o", str(tmp_path),
        ])
        assert result.exit_code == 0
        assert "Generated" in result.output
        output_file = tmp_path / "stepfunction-agent.md"
        assert output_file.exists()
        content = output_file.read_text()
        assert "test" in content
        assert "{{" not in content

    def test_create_invalid_config(self, runner: CliRunner, tmp_path: Path):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"profile": "x"}))
        result = runner.invoke(agents, [
            "create", "-t", "aws/stepfunction",
            "-c", str(config), "-o", str(tmp_path),
        ])
        assert result.exit_code != 0
        assert "state_machine_arn" in result.output

    def test_create_invalid_json(self, runner: CliRunner, tmp_path: Path):
        config = tmp_path / "config.json"
        config.write_text("not json {{{")
        result = runner.invoke(agents, [
            "create", "-t", "aws/stepfunction",
            "-c", str(config), "-o", str(tmp_path),
        ])
        assert result.exit_code != 0
        assert "Invalid JSON" in result.output

    def test_create_nonexistent_template(self, runner: CliRunner, tmp_path: Path):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"profile": "x"}))
        result = runner.invoke(agents, [
            "create", "-t", "aws/nonexistent",
            "-c", str(config), "-o", str(tmp_path),
        ])
        assert result.exit_code != 0


class TestPathTraversal:
    """Tests for path traversal prevention."""

    def test_scaffold_rejects_traversal(self):
        from cli_agent_orchestrator.services.agent_scaffold import render_template
        with pytest.raises(FileNotFoundError, match="escapes"):
            render_template("../../etc/passwd", {})

    def test_scaffold_schema_rejects_traversal(self):
        from cli_agent_orchestrator.services.agent_scaffold import get_template_schema
        with pytest.raises(FileNotFoundError, match="escapes"):
            get_template_schema("../../etc/passwd")
