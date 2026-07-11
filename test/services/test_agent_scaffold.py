"""Tests for agent_scaffold service."""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.agent_scaffold import (
    get_template_schema,
    list_templates,
    render_template,
    validate_config,
)


class TestListTemplates:
    def test_returns_all_aws_templates(self):
        templates = list_templates()
        names = [t["name"] for t in templates]
        assert "aws/stepfunction" in names
        assert "aws/cloudwatch-logs" in names
        assert "aws/dynamodb-query" in names
        assert "aws/dynamodb-delete" in names
        assert "aws/sqs-monitor" in names
        assert "aws/sqs-send" in names
        assert "aws/sqs-dlq-check" in names

    def test_returns_expected_fields(self):
        templates = list_templates()
        for t in templates:
            assert "name" in t
            assert "description" in t
            assert "path" in t
            assert t["description"]  # not empty

    def test_count(self):
        templates = list_templates()
        assert len(templates) == 7


class TestGetTemplateSchema:
    def test_returns_schema_for_valid_template(self):
        schema = get_template_schema("aws/stepfunction")
        assert schema is not None
        assert schema["type"] == "object"
        assert "profile" in schema["properties"]

    def test_returns_none_for_missing(self):
        schema = get_template_schema("aws/nonexistent")
        assert schema is None


class TestValidateConfig:
    def test_valid_config(self):
        config = {
            "profile": "my-profile",
            "region": "us-east-1",
            "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:MyMachine",
        }
        errors = validate_config("aws/stepfunction", config)
        assert errors == []

    def test_missing_required_field(self):
        config = {"profile": "my-profile", "region": "us-east-1"}
        errors = validate_config("aws/stepfunction", config)
        assert any("state_machine_arn" in e for e in errors)

    def test_invalid_region_format(self):
        config = {
            "profile": "my-profile",
            "region": "not-a-region",
            "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:X",
        }
        errors = validate_config("aws/stepfunction", config)
        assert any("region" in e for e in errors)

    def test_extra_property_rejected(self):
        config = {
            "profile": "my-profile",
            "region": "us-east-1",
            "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:X",
            "unknown_field": "value",
        }
        errors = validate_config("aws/stepfunction", config)
        assert any("additional" in e.lower() or "unknown_field" in e for e in errors)

    def test_missing_template_schema(self):
        errors = validate_config("aws/nonexistent", {"foo": "bar"})
        assert any("No schema found" in e for e in errors)


class TestRenderTemplate:
    def test_renders_stepfunction(self):
        config = {
            "profile": "my-profile",
            "region": "us-east-1",
            "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:MyMachine",
            "execution_name_prefix": "test-exec",
            "input_payload": "{}",
            "poll_interval_seconds": 10,
            "timeout_seconds": 300,
        }
        result = render_template("aws/stepfunction", config)

        # Check frontmatter
        assert "name: stepfunction-agent" in result
        assert "execute_bash" in result
        assert "cao-mcp-server" in result

        # Check config values are injected
        assert "my-profile" in result
        assert "us-east-1" in result
        assert "MyMachine" in result
        assert "test-exec" in result

        # Check no Jinja2 artifacts remain
        assert "{{" not in result
        assert "}}" not in result
        assert "{%" not in result

    def test_renders_cloudwatch_logs(self):
        config = {
            "profile": "prod-readonly",
            "region": "eu-west-1",
            "log_group": "/aws/lambda/MyFunc",
            "search_time_window_minutes": 30,
            "max_events": 100,
        }
        result = render_template("aws/cloudwatch-logs", config)
        assert "prod-readonly" in result
        assert "/aws/lambda/MyFunc" in result
        assert "{{" not in result

    def test_renders_sqs_monitor(self):
        config = {
            "profile": "test-profile",
            "region": "us-west-2",
            "queue_url": "https://sqs.us-west-2.amazonaws.com/123/TestQueue",
            "poll_interval_seconds": 5,
            "timeout_seconds": 120,
        }
        result = render_template("aws/sqs-monitor", config)
        assert "test-profile" in result
        assert "TestQueue" in result
        assert "{{" not in result

    def test_renders_dynamodb_delete_with_sort_key(self):
        config = {
            "profile": "dev",
            "region": "us-east-1",
            "table_name": "TestTable",
            "partition_key_name": "pk",
            "partition_key_value": "test-123",
            "partition_key_type": "S",
            "sort_key_name": "sk",
            "sort_key_type": "N",
            "max_delete": 50,
        }
        result = render_template("aws/dynamodb-delete", config)
        assert "TestTable" in result
        assert "test-123" in result
        assert "sk" in result
        assert "{{" not in result

    def test_renders_sqs_send_with_fifo(self):
        config = {
            "profile": "my-profile",
            "region": "us-east-1",
            "queue_url": "https://sqs.us-east-1.amazonaws.com/123/MyQueue.fifo",
            "message_body": '{"event": "test"}',
            "message_group_id": "group-1",
        }
        result = render_template("aws/sqs-send", config)
        assert "group-1" in result
        assert "FIFO" in result
        assert "{{" not in result

    def test_renders_sqs_send_without_fifo(self):
        config = {
            "profile": "my-profile",
            "region": "us-east-1",
            "queue_url": "https://sqs.us-east-1.amazonaws.com/123/MyQueue",
            "message_body": '{"event": "test"}',
            "message_group_id": "",
        }
        result = render_template("aws/sqs-send", config)
        # FIFO section should not appear
        assert "FIFO" not in result
        assert "{{" not in result

    def test_missing_template_raises(self):
        with pytest.raises(FileNotFoundError):
            render_template("aws/nonexistent", {"profile": "x"})

    def test_invalid_config_raises(self):
        with pytest.raises(ValueError, match="validation failed"):
            render_template("aws/stepfunction", {"profile": "x"})
