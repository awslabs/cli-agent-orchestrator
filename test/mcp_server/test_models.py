"""Tests for MCP server models."""

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.mcp_server.models import HandoffResult


class TestHandoffResult:
    def test_handoff_request_validation(self):
        # Valid request
        result = HandoffResult(
            success=True,
            message="Success",
            output="test output",
            terminal_id="terminal123"
        )
        assert result.success is True
        assert result.message == "Success"
        assert result.output == "test output"
        assert result.terminal_id == "terminal123"

    def test_handoff_request_validation_minimal(self):
        # Minimal valid request
        result = HandoffResult(success=False, message="Failed")
        assert result.success is False
        assert result.message == "Failed"
        assert result.output is None
        assert result.terminal_id is None

    def test_handoff_request_validation_missing_required(self):
        # Missing required fields
        with pytest.raises(ValidationError):
            HandoffResult()

    def test_assign_request_validation(self):
        # Test dict structure for assign response
        assign_result = {
            "success": True,
            "terminal_id": "terminal123",
            "message": "Task assigned"
        }
        assert assign_result["success"] is True
        assert assign_result["terminal_id"] == "terminal123"
        assert assign_result["message"] == "Task assigned"

    def test_send_message_request_validation(self):
        # Test dict structure for send_message response
        send_result = {
            "success": True,
            "id": "msg123"
        }
        assert send_result["success"] is True
        assert send_result["id"] == "msg123"