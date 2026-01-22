"""Unit tests for web dashboard API routes."""
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from cli_agent_orchestrator.clients.beads import BeadsClient, Task
from cli_agent_orchestrator.clients.ralph import RalphRunner, RalphState


@pytest.fixture
def beads_client():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield BeadsClient(Path(tmpdir) / "test.db")


@pytest.fixture
def ralph_runner():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield RalphRunner(Path(tmpdir) / "ralph.json")


@pytest.fixture
def client(beads_client, ralph_runner):
    with patch("cli_agent_orchestrator.api.web.beads", beads_client), \
         patch("cli_agent_orchestrator.api.web.ralph", ralph_runner):
        from cli_agent_orchestrator.api.web import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router, prefix="/api")
        yield TestClient(app)


def test_list_tasks_empty(client):
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_task(client):
    resp = client.post("/api/tasks", json={"title": "Test task"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Test task"
    assert data["status"] == "open"


def test_get_task(client):
    create = client.post("/api/tasks", json={"title": "Test"})
    task_id = create.json()["id"]
    resp = client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Test"


def test_get_task_not_found(client):
    resp = client.get("/api/tasks/nonexistent")
    assert resp.status_code == 404


def test_next_task(client):
    client.post("/api/tasks", json={"title": "P2", "priority": 2})
    client.post("/api/tasks", json={"title": "P1", "priority": 1})
    resp = client.get("/api/tasks/next")
    assert resp.status_code == 200
    assert resp.json()["title"] == "P1"


def test_next_task_empty(client):
    resp = client.get("/api/tasks/next")
    assert resp.status_code == 404


def test_wip_task(client):
    create = client.post("/api/tasks", json={"title": "Test"})
    task_id = create.json()["id"]
    resp = client.post(f"/api/tasks/{task_id}/wip")
    assert resp.status_code == 200
    assert resp.json()["status"] == "wip"


def test_close_task(client):
    create = client.post("/api/tasks", json={"title": "Test"})
    task_id = create.json()["id"]
    resp = client.post(f"/api/tasks/{task_id}/close")
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"


def test_delete_task(client):
    create = client.post("/api/tasks", json={"title": "Test"})
    task_id = create.json()["id"]
    resp = client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert client.get(f"/api/tasks/{task_id}").status_code == 404


def test_ralph_status_no_loop(client):
    resp = client.get("/api/ralph")
    assert resp.status_code == 200
    assert resp.json()["active"] is False


def test_ralph_start(client):
    resp = client.post("/api/ralph", json={"prompt": "Test prompt"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["prompt"] == "Test prompt"
    assert data["status"] == "running"


def test_ralph_stop(client):
    client.post("/api/ralph", json={"prompt": "Test"})
    resp = client.post("/api/ralph/stop")
    assert resp.status_code == 200
    status = client.get("/api/ralph").json()
    assert status["status"] == "stopped"


def test_ralph_feedback(client):
    client.post("/api/ralph", json={"prompt": "Test"})
    resp = client.post("/api/ralph/feedback", json={"score": 7, "summary": "Good"})
    assert resp.status_code == 200
    assert resp.json()["iteration"] == 2
