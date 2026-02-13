"""Tests for MCP server module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.mcp_server.server import (
    _assign_impl,
    _create_terminal,
    _handoff_impl,
    _send_direct_input,
    _send_to_inbox,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus


@pytest.fixture
def mock_terminal_service():
    with patch("cli_agent_orchestrator.mcp_server.server.terminal_service") as mock:
        yield mock


@pytest.fixture
def mock_inbox_service():
    with patch("cli_agent_orchestrator.mcp_server.server.inbox_service") as mock:
        yield mock


@pytest.fixture
def mock_requests():
    with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock:
        yield mock


@pytest.fixture
def mock_wait_until_terminal_status():
    with patch("cli_agent_orchestrator.mcp_server.server.wait_until_terminal_status") as mock:
        yield mock


class TestHandoff:
    @pytest.mark.asyncio
    async def test_handoff_success(self, mock_requests, mock_wait_until_terminal_status):
        # Setup mocks
        mock_requests.get.return_value.json.return_value = {"output": "test output"}
        mock_requests.post.return_value.raise_for_status = MagicMock()
        mock_wait_until_terminal_status.return_value = True
        
        with patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as mock_create:
            mock_create.return_value = ("terminal123", "test_provider")
            
            result = await _handoff_impl("developer", "test message")
            
            assert result.success is True
            assert result.terminal_id == "terminal123"
            assert result.output == "test output"

    @pytest.mark.asyncio
    async def test_handoff_timeout(self, mock_requests, mock_wait_until_terminal_status):
        # Mock the first wait_until_terminal_status call to succeed (IDLE status)
        # and the second call to fail (COMPLETED status timeout)
        mock_wait_until_terminal_status.side_effect = [True, False]
        
        with patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as mock_create:
            mock_create.return_value = ("terminal123", "test_provider")
            
            result = await _handoff_impl("developer", "test message", timeout=1)
            
            assert result.success is False
            assert "timed out" in result.message

    @pytest.mark.asyncio
    async def test_handoff_with_working_directory(self, mock_requests, mock_wait_until_terminal_status):
        mock_requests.get.return_value.json.return_value = {"output": "test output"}
        mock_requests.post.return_value.raise_for_status = MagicMock()
        mock_wait_until_terminal_status.return_value = True
        
        with patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as mock_create:
            mock_create.return_value = ("terminal123", "test_provider")
            
            result = await _handoff_impl("developer", "test message", working_directory="/test/dir")
            
            mock_create.assert_called_once_with("developer", "/test/dir")
            assert result.success is True


class TestAssign:
    def test_assign_success(self):
        with patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as mock_create, \
             patch("cli_agent_orchestrator.mcp_server.server._send_direct_input") as mock_send:
            mock_create.return_value = ("terminal123", "test_provider")
            
            result = _assign_impl("developer", "test message")
            
            assert result["success"] is True
            assert result["terminal_id"] == "terminal123"
            mock_send.assert_called_once_with("terminal123", "test message")

    def test_assign_with_working_directory(self):
        with patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as mock_create, \
             patch("cli_agent_orchestrator.mcp_server.server._send_direct_input") as mock_send:
            mock_create.return_value = ("terminal123", "test_provider")
            
            result = _assign_impl("developer", "test message", "/test/dir")
            
            mock_create.assert_called_once_with("developer", "/test/dir")
            assert result["success"] is True

    def test_assign_failure(self):
        with patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as mock_create:
            mock_create.side_effect = Exception("Creation failed")
            
            result = _assign_impl("developer", "test message")
            
            assert result["success"] is False
            assert "Creation failed" in result["message"]


class TestSendMessage:
    def test_send_message_success(self, mock_requests):
        mock_requests.post.return_value.json.return_value = {"id": "msg123"}
        mock_requests.post.return_value.raise_for_status = MagicMock()
        
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sender123"}):
            result = _send_to_inbox("receiver123", "test message")
            
            assert result == {"id": "msg123"}
            mock_requests.post.assert_called_once()

    def test_send_message_no_terminal_id(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="CAO_TERMINAL_ID not set"):
                _send_to_inbox("receiver123", "test message")


class TestCreateTerminal:
    def test_create_terminal_new_session(self, mock_requests):
        mock_requests.post.return_value.json.return_value = {"id": "terminal123"}
        mock_requests.post.return_value.raise_for_status = MagicMock()
        
        with patch.dict(os.environ, {}, clear=True), \
             patch("cli_agent_orchestrator.mcp_server.server.generate_session_name") as mock_gen:
            mock_gen.return_value = "session123"
            
            terminal_id, provider = _create_terminal("developer")
            
            assert terminal_id == "terminal123"
            assert provider == "q_cli"

    def test_create_terminal_existing_session(self, mock_requests):
        mock_requests.get.return_value.json.return_value = {
            "provider": "test_provider",
            "session_name": "existing_session"
        }
        mock_requests.post.return_value.json.return_value = {"id": "terminal123"}
        mock_requests.post.return_value.raise_for_status = MagicMock()
        
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "current123"}):
            terminal_id, provider = _create_terminal("developer")
            
            assert terminal_id == "terminal123"
            assert provider == "test_provider"


class TestSendDirectInput:
    def test_send_direct_input(self, mock_requests):
        mock_requests.post.return_value.raise_for_status = MagicMock()
        
        _send_direct_input("terminal123", "test message")
        
        mock_requests.post.assert_called_once()


class TestSendToInbox:
    def test_send_to_inbox(self, mock_requests):
        mock_requests.post.return_value.json.return_value = {"id": "msg123"}
        mock_requests.post.return_value.raise_for_status = MagicMock()
        
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "sender123"}):
            result = _send_to_inbox("receiver123", "test message")
            
            assert result == {"id": "msg123"}