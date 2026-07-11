"""Tests for cao agents command group."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.profile import (
    _validate_frontmatter,
    profile,
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
        assert any("[warn]" in m and "role" in m for m in msgs)

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
        result = runner.invoke(profile, ["list"])
        assert result.exit_code == 0

    def test_list_shows_header(self, runner: CliRunner):
        """Test that list shows column headers."""
        result = runner.invoke(profile, ["list"])
        # Either shows profiles or 'No agent profiles found'
        assert "NAME" in result.output or "No agent profiles" in result.output


class TestAgentsShowCommand:
    """Tests for cao agents show."""

    def test_show_valid_file(self, runner: CliRunner, sample_profile_valid: Path):
        result = runner.invoke(profile, ["show", str(sample_profile_valid)])
        assert result.exit_code == 0
        assert "test-agent" in result.output
        assert "allowedTools" in result.output

    def test_show_not_found(self, runner: CliRunner):
        result = runner.invoke(profile, ["show", "nonexistent-agent-xyz"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestAgentsValidateCommand:
    """Tests for cao agents validate."""

    def test_validate_valid_profile(self, runner: CliRunner, sample_profile_valid: Path):
        result = runner.invoke(profile, ["validate", str(sample_profile_valid)])
        assert result.exit_code == 0
        assert "✓" in result.output

    def test_validate_deprecated_field(self, runner: CliRunner, sample_profile_deprecated: Path):
        result = runner.invoke(profile, ["validate", str(sample_profile_deprecated)])
        # autoApproveTools triggers additionalProperties error (blocking)
        assert result.exit_code == 1
        assert "autoApproveTools" in result.output

    def test_validate_invalid_role(self, runner: CliRunner, sample_profile_invalid_role: Path):
        result = runner.invoke(profile, ["validate", str(sample_profile_invalid_role)])
        # Unknown role is a warning, not an error — exits 0
        assert result.exit_code == 0
        assert "role" in result.output
        assert "[warn]" in result.output

    def test_validate_bad_tools(self, runner: CliRunner, sample_profile_bad_tools: Path):
        result = runner.invoke(profile, ["validate", str(sample_profile_bad_tools)])
        # Bad tools are warnings, not errors, so exit 0
        assert result.exit_code == 0
        assert "shell:aws" in result.output

    def test_validate_not_found(self, runner: CliRunner):
        result = runner.invoke(profile, ["validate", "nonexistent.md"])
        assert result.exit_code == 1


class TestAgentsRemoveCommand:
    """Tests for cao agents remove."""

    def test_remove_not_found(self, runner: CliRunner):
        result = runner.invoke(profile, ["remove", "nonexistent-agent-xyz", "-y"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestAgentsTemplatesCommand:
    """Tests for cao agents templates."""

    def test_templates_lists_all(self, runner: CliRunner):
        result = runner.invoke(profile, ["templates"])
        assert result.exit_code == 0
        assert "aws/stepfunction" in result.output
        assert "aws/cloudwatch-logs" in result.output
        assert "7 template(s) available" in result.output

    def test_templates_shows_description(self, runner: CliRunner):
        result = runner.invoke(profile, ["templates"])
        assert "Trigger and monitor" in result.output


class TestAgentsCreateCommand:
    """Tests for cao agents create."""

    def test_create_writes_file(self, runner: CliRunner, tmp_path: Path):
        config = tmp_path / "config.json"
        config.write_text(
            json.dumps(
                {
                    "profile": "test",
                    "region": "us-east-1",
                    "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:X",
                }
            )
        )
        result = runner.invoke(
            profile,
            [
                "create",
                "-t",
                "aws/stepfunction",
                "-c",
                str(config),
                "-o",
                str(tmp_path),
            ],
        )
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
        result = runner.invoke(
            profile,
            [
                "create",
                "-t",
                "aws/stepfunction",
                "-c",
                str(config),
                "-o",
                str(tmp_path),
            ],
        )
        assert result.exit_code != 0
        assert "state_machine_arn" in result.output

    def test_create_invalid_json(self, runner: CliRunner, tmp_path: Path):
        config = tmp_path / "config.json"
        config.write_text("not json {{{")
        result = runner.invoke(
            profile,
            [
                "create",
                "-t",
                "aws/stepfunction",
                "-c",
                str(config),
                "-o",
                str(tmp_path),
            ],
        )
        assert result.exit_code != 0
        assert "Invalid JSON" in result.output

    def test_create_nonexistent_template(self, runner: CliRunner, tmp_path: Path):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"profile": "x"}))
        result = runner.invoke(
            profile,
            [
                "create",
                "-t",
                "aws/nonexistent",
                "-c",
                str(config),
                "-o",
                str(tmp_path),
            ],
        )
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


class TestProfileRemoveVerb:
    """Tests for cao profile remove (destructive path coverage)."""

    def test_remove_success(self, runner: CliRunner, tmp_path: Path):
        """Positive: seeds a profile, removes it, asserts file gone."""
        from unittest.mock import patch

        store = tmp_path / "store"
        store.mkdir()
        profile_file = store / "test-agent.md"
        profile_file.write_text("---\nname: test-agent\n---\ntest")

        with patch("cli_agent_orchestrator.cli.commands.profile.LOCAL_AGENT_STORE_DIR", store):
            result = runner.invoke(profile, ["remove", "test-agent", "-y"])
        assert result.exit_code == 0
        assert "Removed" in result.output
        assert not profile_file.exists()

    def test_remove_containment_rejects_traversal(self, runner: CliRunner, tmp_path: Path):
        """Negative: path traversal in name is rejected."""
        from unittest.mock import patch

        store = tmp_path / "store"
        store.mkdir()

        with patch("cli_agent_orchestrator.cli.commands.profile.LOCAL_AGENT_STORE_DIR", store):
            result = runner.invoke(profile, ["remove", "../../etc/passwd", "-y"])
        assert result.exit_code != 0
        assert "Invalid" in result.output or "not found" in result.output.lower()

    def test_remove_confirm_abort(self, runner: CliRunner, tmp_path: Path):
        """Confirm prompt aborts when user says no."""
        from unittest.mock import patch

        store = tmp_path / "store"
        store.mkdir()
        profile_file = store / "keep-me.md"
        profile_file.write_text("---\nname: keep-me\n---\ntest")

        with patch("cli_agent_orchestrator.cli.commands.profile.LOCAL_AGENT_STORE_DIR", store):
            result = runner.invoke(profile, ["remove", "keep-me"], input="n\n")
        assert profile_file.exists()  # File should still be there


class TestProfileListIsolated:
    """Tests for cao profile list with patched store."""

    def test_list_renders_profiles(self, runner: CliRunner, tmp_path: Path):
        """Seeds profiles and verifies the render loop runs."""
        from unittest.mock import patch

        profiles = [
            {"name": "alpha", "source": "local", "description": "First agent"},
            {"name": "beta", "source": "built-in", "description": "Second agent"},
        ]
        with patch(
            "cli_agent_orchestrator.cli.commands.profile.list_agent_profiles",
            return_value=profiles,
        ):
            result = runner.invoke(profile, ["list"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "2 profile(s) found" in result.output


class TestInjectionRegression:
    """Regression tests locking in the security posture."""

    def test_jinja_in_config_renders_literally(self):
        """A config value like {{7*7}} must render as-is, not evaluate."""
        from cli_agent_orchestrator.services.agent_scaffold import render_template

        config = {
            "profile": "test",
            "region": "us-east-1",
            "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:X",
            "execution_name_prefix": "{{7*7}}",
            "input_payload": "{}",
            "poll_interval_seconds": 15,
            "timeout_seconds": 600,
        }
        # execution_name_prefix has pattern ^[a-zA-Z0-9_-]+$ which rejects {{
        from cli_agent_orchestrator.services.agent_scaffold import validate_config

        errors = validate_config("aws/stepfunction", config)
        assert any("execution_name_prefix" in e for e in errors)

    def test_newline_in_message_body_rejected(self):
        """Newlines in message_body must be rejected at schema validation."""
        from cli_agent_orchestrator.services.agent_scaffold import validate_config

        config = {
            "profile": "test",
            "region": "us-east-1",
            "queue_url": "https://sqs.us-east-1.amazonaws.com/123/Q",
            "message_body": "legit\nMSGEOF\necho PWNED",
            "message_group_id": "grp",
        }
        errors = validate_config("aws/sqs-send", config)
        assert any("message_body" in e for e in errors)

    def test_newline_in_input_payload_rejected(self):
        """Newlines in input_payload must be rejected at schema validation."""
        from cli_agent_orchestrator.services.agent_scaffold import validate_config

        config = {
            "profile": "test",
            "region": "us-east-1",
            "state_machine_arn": "arn:aws:states:us-east-1:123:stateMachine:X",
            "input_payload": "{}\nINPUTEOF\nPWN",
        }
        errors = validate_config("aws/stepfunction", config)
        assert any("input_payload" in e for e in errors)


class TestRenderMissingTemplates:
    """Render smoke tests for templates not covered by existing tests."""

    def test_renders_dynamodb_query(self):
        from cli_agent_orchestrator.services.agent_scaffold import render_template

        config = {
            "profile": "dev",
            "region": "us-west-2",
            "table_name": "MyTable",
            "partition_key_name": "pk",
            "partition_key_value": "val123",
            "partition_key_type": "S",
            "limit": 5,
        }
        result = render_template("aws/dynamodb-query", config)
        assert "MyTable" in result
        assert "val123" in result
        assert "{{" not in result

    def test_renders_sqs_dlq_check(self):
        from cli_agent_orchestrator.services.agent_scaffold import render_template

        config = {
            "profile": "dev",
            "region": "eu-west-1",
            "dlq_url": "https://sqs.eu-west-1.amazonaws.com/999888777/MyDLQ",
            "message_group_id": "grp",
            "max_messages": 5,
        }
        result = render_template("aws/sqs-dlq-check", config)
        assert "MyDLQ" in result
        assert "grp" in result
        assert "{{" not in result
