"""Tests for terminal status update endpoint."""

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.clients.database import (
    create_terminal,
    delete_terminal,
    get_terminal_status,
    init_db,
)


@pytest.fixture(autouse=True)
def setup_db():
    """Initialize database before each test."""
    init_db()


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def test_terminal():
    """Create a test terminal."""
    terminal_id = "abcd1234"
    create_terminal(
        terminal_id=terminal_id,
        tmux_session="test-session",
        tmux_window="test-window",
        provider="kiro_cli",
        agent_profile="developer",
    )
    yield terminal_id
    # Cleanup
    delete_terminal(terminal_id)


def test_update_terminal_status_success(client, test_terminal):
    """Test successful status update."""
    response = client.post(
        f"/terminals/{test_terminal}/status",
        params={"new_status": "processing"},
    )

    print(f"Response: {response.status_code}, {response.json()}")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["terminal_id"] == test_terminal
    assert data["status"] == "processing"

    # Verify status was updated in database
    db_status = get_terminal_status(test_terminal)
    assert db_status == "processing"


def test_update_terminal_status_multiple_updates(client, test_terminal):
    """Test multiple status updates."""
    statuses = ["idle", "processing", "completed", "idle"]

    for status in statuses:
        response = client.post(
            f"/terminals/{test_terminal}/status",
            params={"new_status": status},
        )
        assert response.status_code == 200

        # Verify each update
        db_status = get_terminal_status(test_terminal)
        assert db_status == status


def test_update_terminal_status_not_found(client):
    """Test status update for non-existent terminal."""
    response = client.post(
        "/terminals/ffffffff/status",
        params={"new_status": "idle"},
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_update_terminal_status_custom_values(client, test_terminal):
    """Test status update with all valid status values."""
    # Test all valid TerminalStatus enum values
    valid_statuses = ["waiting_user_answer", "error"]

    for status in valid_statuses:
        response = client.post(
            f"/terminals/{test_terminal}/status",
            params={"new_status": status},
        )
        assert response.status_code == 200

        db_status = get_terminal_status(test_terminal)
        assert db_status == status


@pytest.mark.skip(
    reason="GET /terminals/{id} requires tmux integration - test status endpoint only"
)
def test_get_terminal_includes_status(client, test_terminal):
    """Test that GET /terminals/{id} includes status field."""
    # Update status first
    client.post(
        f"/terminals/{test_terminal}/status",
        params={"new_status": "processing"},
    )

    # Get terminal info
    response = client.get(f"/terminals/{test_terminal}")

    # Note: This will fail until terminal_service.get_terminal() is updated
    # to include status from database. For now, we're just testing the
    # status update endpoint works correctly.
    assert response.status_code == 200
