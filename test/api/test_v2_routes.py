"""Tests for V2 API routes."""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from pathlib import Path
import tempfile
import json

from cli_agent_orchestrator.api.main import app

client = TestClient(app)


class TestAgentsAPI:
    """Tests for /api/v2/agents endpoints."""

    def test_list_agents_empty(self):
        """Test listing agents when directory is empty."""
        with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR') as mock_dir:
            mock_dir.exists.return_value = False
            response = client.get("/api/v2/agents")
            assert response.status_code == 200
            assert response.json() == []

    def test_list_agents_with_files(self):
        """Test listing agents with agent files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir)
            # discover_agents() looks for .json files
            (agents_dir / "test-agent.json").write_text('{"name": "test-agent", "description": "Test Agent"}')
            
            with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR', agents_dir):
                response = client.get("/api/v2/agents")
                assert response.status_code == 200
                agents = response.json()
                assert len(agents) == 1
                assert agents[0]["name"] == "test-agent"

    def test_create_agent(self):
        """Test creating a new agent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir)
            
            with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR', agents_dir):
                response = client.post("/api/v2/agents", json={
                    "name": "new-agent",
                    "description": "A new agent",
                    "steering": "Some steering content"
                })
                assert response.status_code == 201
                assert response.json()["name"] == "new-agent"
                assert (agents_dir / "new-agent.md").exists()

    def test_create_agent_duplicate(self):
        """Test creating duplicate agent fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir)
            (agents_dir / "existing.md").write_text("# Existing")
            
            with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR', agents_dir):
                response = client.post("/api/v2/agents", json={
                    "name": "existing",
                    "description": "Duplicate"
                })
                assert response.status_code == 400

    def test_get_agent(self):
        """Test getting agent details."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir)
            (agents_dir / "my-agent.json").write_text('{"name": "my-agent", "description": "Content here"}')
            
            with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR', agents_dir):
                response = client.get("/api/v2/agents/my-agent")
                assert response.status_code == 200
                assert response.json()["name"] == "my-agent"
                # get_agent returns config, not content
                assert response.json()["config"]["description"] == "Content here"

    def test_get_agent_not_found(self):
        """Test getting non-existent agent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR', Path(tmpdir)):
                response = client.get("/api/v2/agents/nonexistent")
                assert response.status_code == 404

    def test_delete_agent(self):
        """Test deleting an agent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir)
            agent_file = agents_dir / "to-delete.md"
            agent_file.write_text("# To Delete")
            
            with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR', agents_dir):
                response = client.delete("/api/v2/agents/to-delete")
                assert response.status_code == 200
                assert not agent_file.exists()


class TestSessionStatusDetection:
    """Tests for session status detection."""

    def test_detect_waiting_input(self):
        """Test detecting WAITING_INPUT status."""
        from cli_agent_orchestrator.api.v2 import detect_session_status
        
        output = "Some output\nWhat would you like me to do?\n"
        assert detect_session_status(output) == "WAITING_INPUT"

    def test_detect_error(self):
        """Test detecting ERROR status."""
        from cli_agent_orchestrator.api.v2 import detect_session_status
        
        output = "Processing...\nTraceback (most recent call last):\n  Error occurred"
        assert detect_session_status(output) == "ERROR"

    def test_detect_processing(self):
        """Test detecting PROCESSING status."""
        from cli_agent_orchestrator.api.v2 import detect_session_status
        
        output = "Working on the task...\nAnalyzing files..."
        assert detect_session_status(output) == "PROCESSING"

    def test_detect_idle(self):
        """Test detecting IDLE status."""
        from cli_agent_orchestrator.api.v2 import detect_session_status
        
        output = "Task completed successfully.\n"
        assert detect_session_status(output) == "IDLE"


class TestActivityExtraction:
    """Tests for activity extraction from terminal output."""

    def test_extract_tool_calls(self):
        """Test extracting tool calls from output."""
        from cli_agent_orchestrator.api.v2 import extract_activity
        
        output = '<invoke name="fs_read">\n<parameter>test.py</parameter>\n</invoke>'
        activities = extract_activity(output, "session-1")
        
        assert len(activities) >= 1
        tool_calls = [a for a in activities if a["type"] == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool"] == "fs_read"


class TestBeadsAssignment:
    """Tests for beads assignment."""

    def test_assign_bead_not_found(self):
        """Test assigning non-existent bead."""
        response = client.post("/api/v2/beads/nonexistent/assign", json={
            "session_id": "session-1"
        })
        assert response.status_code == 404


class TestAutoMode:
    """Tests for auto-mode toggle."""

    def test_toggle_auto_mode_on(self):
        """Test enabling auto-mode."""
        response = client.post("/api/v2/sessions/test-session/auto-mode", json={
            "enabled": True
        })
        assert response.status_code == 200
        assert response.json()["auto_mode"] == True

    def test_toggle_auto_mode_off(self):
        """Test disabling auto-mode."""
        # First enable
        client.post("/api/v2/sessions/test-session/auto-mode", json={"enabled": True})
        # Then disable
        response = client.post("/api/v2/sessions/test-session/auto-mode", json={
            "enabled": False
        })
        assert response.status_code == 200
        assert response.json()["auto_mode"] == False

    def test_get_auto_mode(self):
        """Test getting auto-mode status."""
        response = client.get("/api/v2/sessions/new-session/auto-mode")
        assert response.status_code == 200
        assert "auto_mode" in response.json()


class TestContextLearning:
    """Tests for context learning endpoints."""

    def test_list_proposals_empty(self):
        """Test listing proposals when empty."""
        response = client.get("/api/v2/learn/proposals")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_approve_proposal_not_found(self):
        """Test approving non-existent proposal."""
        response = client.post("/api/v2/learn/proposals/nonexistent/approve")
        assert response.status_code == 404

    def test_reject_proposal_not_found(self):
        """Test rejecting non-existent proposal."""
        response = client.post("/api/v2/learn/proposals/nonexistent/reject")
        assert response.status_code == 404


class TestTaskDecomposition:
    """Tests for task decomposition."""

    def test_decompose_simple_list(self):
        """Test decomposing a simple numbered list."""
        response = client.post("/api/v2/beads/decompose", json={
            "text": "1. First task\n2. Second task\n3. Third task"
        })
        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 3
        assert len(result["tasks"]) == 3

    def test_decompose_empty(self):
        """Test decomposing empty text."""
        response = client.post("/api/v2/beads/decompose", json={
            "text": ""
        })
        assert response.status_code == 200
        assert response.json()["count"] == 0


class TestActivityFeed:
    """Tests for activity feed."""

    def test_get_activity(self):
        """Test getting activity feed."""
        response = client.get("/api/v2/activity")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_get_activity_with_filter(self):
        """Test getting activity with session filter."""
        response = client.get("/api/v2/activity?session_id=test-session")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestAgentUpdate:
    """Tests for agent update endpoint."""

    def test_update_agent(self):
        """Test updating an existing agent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir)
            (agents_dir / "update-me.md").write_text("# Old Content")
            
            with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR', agents_dir):
                response = client.put("/api/v2/agents/update-me", json={
                    "name": "update-me",
                    "description": "Updated description",
                    "steering": "New steering"
                })
                assert response.status_code == 200
                content = (agents_dir / "update-me.md").read_text()
                assert "Updated description" in content

    def test_update_agent_not_found(self):
        """Test updating non-existent agent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('cli_agent_orchestrator.api.v2.KIRO_AGENTS_DIR', Path(tmpdir)):
                response = client.put("/api/v2/agents/nonexistent", json={
                    "name": "nonexistent",
                    "description": "Test"
                })
                assert response.status_code == 404


class TestSessionInputOutput:
    """Tests for session input/output endpoints."""

    def test_send_input_no_session(self):
        """Test sending input to non-existent session."""
        with patch('cli_agent_orchestrator.api.v2.session_service') as mock_svc:
            mock_svc.get_session.side_effect = ValueError("Session not found")
            response = client.post("/api/v2/sessions/fake-session/input?message=hello")
            assert response.status_code == 404

    def test_get_output_no_session(self):
        """Test getting output from non-existent session."""
        with patch('cli_agent_orchestrator.api.v2.session_service') as mock_svc:
            mock_svc.get_session.side_effect = ValueError("Session not found")
            response = client.get("/api/v2/sessions/fake-session/output")
            assert response.status_code == 404


class TestContextLearningTrigger:
    """Tests for context learning trigger."""

    def test_trigger_learning_no_session(self):
        """Test triggering learning for non-existent session."""
        with patch('cli_agent_orchestrator.api.v2.session_service') as mock_svc:
            mock_svc.get_session.side_effect = ValueError("Session not found")
            response = client.post("/api/v2/learn/fake-session")
            assert response.status_code == 404


class TestActivityExtraction2:
    """Additional activity extraction tests."""

    def test_extract_file_operations(self):
        """Test extracting file operations from output."""
        from cli_agent_orchestrator.api.v2 import extract_activity
        
        output = "fs_write to /path/to/file.py\nfs_read from config.json"
        activities = extract_activity(output, "session-2")
        
        file_ops = [a for a in activities if a["type"] == "file_op"]
        assert len(file_ops) >= 1

    def test_extract_bash_commands(self):
        """Test extracting bash commands from output."""
        from cli_agent_orchestrator.api.v2 import extract_activity
        
        output = "execute_bash: ls -la\nRunning command..."
        activities = extract_activity(output, "session-3")
        
        assert len(activities) >= 1


class TestStatusDetectionEdgeCases:
    """Edge case tests for status detection."""

    def test_detect_waiting_input_variations(self):
        """Test various waiting input patterns."""
        from cli_agent_orchestrator.api.v2 import detect_session_status
        
        patterns = [
            "Enter your response:",
            "Type your message here",
            "How can I help you today?",
            "Waiting for user input..."
        ]
        for p in patterns:
            assert detect_session_status(p) == "WAITING_INPUT", f"Failed for: {p}"

    def test_detect_error_variations(self):
        """Test various error patterns."""
        from cli_agent_orchestrator.api.v2 import detect_session_status
        
        patterns = [
            "Exception: Something went wrong",
            "Error: File not found",
            "Failed to connect"
        ]
        for p in patterns:
            assert detect_session_status(p) == "ERROR", f"Failed for: {p}"

    def test_empty_output(self):
        """Test status detection with empty output."""
        from cli_agent_orchestrator.api.v2 import detect_session_status
        
        assert detect_session_status("") == "IDLE"
        assert detect_session_status("\n\n") == "IDLE"


class TestSessionAnalysis:
    """Tests for session output analysis."""

    def test_analyze_session_output_tools(self):
        """Test extracting tools from session output."""
        from cli_agent_orchestrator.api.v2 import analyze_session_output
        
        output = '<invoke name="fs_read">\n<invoke name="execute_bash">\n<invoke name="fs_write">'
        result = analyze_session_output(output)
        
        assert "fs_read" in result["tools_used"]
        assert "execute_bash" in result["tools_used"]
        assert "fs_write" in result["tools_used"]

    def test_analyze_session_output_errors(self):
        """Test extracting errors from session output."""
        from cli_agent_orchestrator.api.v2 import analyze_session_output
        
        output = "Error: File not found\nException: Connection timeout"
        result = analyze_session_output(output)
        
        assert len(result["errors"]) >= 1

    def test_analyze_session_output_files(self):
        """Test extracting file paths from session output."""
        from cli_agent_orchestrator.api.v2 import analyze_session_output
        
        output = "fs_write: /path/to/file.py\nmodified: config.json"
        result = analyze_session_output(output)
        
        assert len(result["files_modified"]) >= 1

    def test_analyze_session_output_patterns(self):
        """Test detecting patterns in session output."""
        from cli_agent_orchestrator.api.v2 import analyze_session_output
        
        output = "Let me retry this...\nTrying again now"
        result = analyze_session_output(output)
        
        assert len(result["patterns"]) >= 1
        assert any("retri" in p.lower() for p in result["patterns"])


class TestWebSocketEndpoints:
    """Tests for WebSocket endpoints."""

    def test_terminal_stream_endpoint_exists(self):
        """Test that terminal stream WebSocket endpoint is defined."""
        from cli_agent_orchestrator.api.v2 import router
        routes = [r.path for r in router.routes]
        assert "/v2/sessions/{session_id}/stream" in routes

    def test_activity_stream_endpoint_exists(self):
        """Test that activity stream WebSocket endpoint is defined."""
        from cli_agent_orchestrator.api.v2 import router
        routes = [r.path for r in router.routes]
        assert "/v2/activity/stream" in routes


class TestIntegrationSessionLifecycle:
    """Integration tests for session lifecycle."""

    def test_session_list_returns_list(self):
        """Test that session list returns a list."""
        with patch('cli_agent_orchestrator.api.v2.session_service') as mock_svc:
            mock_svc.list_sessions.return_value = []
            response = client.get("/api/v2/sessions")
            assert response.status_code == 200
            assert isinstance(response.json(), list)

    def test_session_create_and_status(self):
        """Test creating session and checking status."""
        with patch('cli_agent_orchestrator.api.v2.terminal_service') as mock_svc:
            mock_terminal = MagicMock()
            mock_terminal.session_name = "test-123"
            mock_terminal.id = "term-456"
            mock_svc.create_terminal.return_value = mock_terminal
            response = client.post("/api/v2/sessions", json={
                "agent_name": "test-agent"
            })
            assert response.status_code == 201
            assert "id" in response.json()


class TestBeadsCRUD:
    """Integration tests for beads CRUD operations."""

    def test_beads_list(self):
        """Test listing beads."""
        response = client.get("/api/v2/beads")
        assert response.status_code == 200

    def test_beads_assign(self):
        """Test assigning a bead to a session."""
        with patch('cli_agent_orchestrator.api.v2.beads') as mock_beads:
            mock_task = MagicMock()
            mock_task.id = "test-bead"
            mock_task.title = "Test"
            mock_task.description = "Test desc"
            mock_beads.get.return_value = mock_task
            mock_beads.wip.return_value = mock_task
            
            with patch('cli_agent_orchestrator.api.v2.session_service') as mock_sess:
                mock_sess.get_session.return_value = {"terminals": []}
                response = client.post("/api/v2/beads/test-bead-id/assign", json={
                    "session_id": "test-session"
                })
                assert response.status_code == 200


class TestAutoModeIntegration:
    """Integration tests for auto-mode functionality."""

    def test_auto_mode_state_persistence(self):
        """Test that auto-mode state persists across requests."""
        session_id = "persist-test"
        
        # Enable auto-mode
        client.post(f"/api/v2/sessions/{session_id}/auto-mode", json={"enabled": True})
        
        # Check it's enabled
        response = client.get(f"/api/v2/sessions/{session_id}/auto-mode")
        assert response.json()["auto_mode"] == True
        
        # Disable
        client.post(f"/api/v2/sessions/{session_id}/auto-mode", json={"enabled": False})
        
        # Check it's disabled
        response = client.get(f"/api/v2/sessions/{session_id}/auto-mode")
        assert response.json()["auto_mode"] == False


class TestContextLearningIntegration:
    """Integration tests for context learning."""

    def test_learning_workflow(self):
        """Test the learning trigger workflow."""
        with patch('cli_agent_orchestrator.api.v2.session_service') as mock_sess, \
             patch('cli_agent_orchestrator.api.v2.terminal_service') as mock_term:
            mock_sess.get_session.return_value = {
                "terminals": [{"id": "term-123", "agent_profile": "test-agent"}]
            }
            mock_term.get_output.return_value = 'Some output with <invoke name="fs_read"> tool calls'
            
            response = client.post("/api/v2/learn/learn-test")
            assert response.status_code == 200
            result = response.json()
            assert "learnings" in result


class TestActivityFeedIntegration:
    """Integration tests for activity feed."""

    def test_activity_extraction_integration(self):
        """Test activity extraction from real-like output."""
        from cli_agent_orchestrator.api.v2 import extract_activity
        
        output = '<invoke name="fs_read"><parameter name="path">/test/file.py</parameter></invoke>'
        activities = extract_activity(output, "test-session")
        assert isinstance(activities, list)
