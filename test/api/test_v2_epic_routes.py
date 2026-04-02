"""Integration tests for epic API endpoints and bead-session wiring."""
import json
from unittest.mock import MagicMock, patch
from dataclasses import dataclass
from typing import Optional, List
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db_mod


@dataclass
class FakeTask:
    """Lightweight Task stand-in for tests (avoids MagicMock __dict__ issues)."""
    id: str
    title: str = ""
    description: str = ""
    priority: int = 2
    status: str = "open"
    assignee: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    closed_at: Optional[str] = None
    tags: str = "[]"
    metadata: str = "{}"
    parent_id: Optional[str] = None
    blocked_by: Optional[List[str]] = None
    labels: Optional[List[str]] = None
    type: Optional[str] = None


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    """Isolated temp DB for each test."""
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    orig_engine, orig_session = db_mod.engine, db_mod.SessionLocal
    db_mod.engine = engine
    db_mod.SessionLocal = session_factory
    db_mod.Base.metadata.create_all(bind=engine)
    yield
    db_mod.engine = orig_engine
    db_mod.SessionLocal = orig_session


@pytest.fixture
def mock_beads():
    with patch("cli_agent_orchestrator.api.v2.beads") as mock:
        yield mock


@pytest.fixture
def client():
    from cli_agent_orchestrator.api.main import app
    return TestClient(app)


# ==================== Epic Creation ====================


class TestCreateEpicEndpoint:
    def test_create_epic_returns_201(self, client, mock_beads):
        mock_beads.create_epic.return_value = FakeTask(id="e1", title="My Epic")
        mock_beads.get_children.return_value = [
            FakeTask(id="e1.1", title="Step A"),
            FakeTask(id="e1.2", title="Step B"),
        ]
        res = client.post("/api/v2/epics", json={"title": "My Epic", "steps": ["Step A", "Step B"]})
        assert res.status_code == 201
        data = res.json()
        assert data["epic"]["id"] == "e1"
        assert len(data["children"]) == 2

    def test_create_epic_passes_all_params(self, client, mock_beads):
        mock_beads.create_epic.return_value = FakeTask(id="e2")
        mock_beads.get_children.return_value = []
        client.post("/api/v2/epics", json={
            "title": "E", "steps": ["X"], "description": "desc",
            "priority": 1, "sequential": False, "max_concurrent": 5,
            "labels": ["custom:tag"]
        })
        mock_beads.create_epic.assert_called_once_with(
            title="E", steps=["X"], description="desc",
            priority=1, sequential=False, max_concurrent=5, labels=["custom:tag"]
        )

    def test_create_epic_empty_steps_returns_400(self, client, mock_beads):
        res = client.post("/api/v2/epics", json={"title": "Empty", "steps": []})
        assert res.status_code == 400

    def test_create_epic_returns_children_with_all_fields(self, client, mock_beads):
        mock_beads.create_epic.return_value = FakeTask(id="e3", title="E", labels=["type:epic"], type="epic")
        mock_beads.get_children.return_value = [
            FakeTask(id="e3.1", title="Child A", parent_id="e3"),
        ]
        res = client.post("/api/v2/epics", json={"title": "E", "steps": ["A"]})
        data = res.json()
        assert data["epic"]["labels"] == ["type:epic"]
        assert data["epic"]["type"] == "epic"
        assert data["children"][0]["parent_id"] == "e3"


# ==================== Get Epic ====================


class TestGetEpicEndpoint:
    def test_get_epic_returns_progress(self, client, mock_beads):
        mock_beads.get.return_value = FakeTask(id="e1", title="Epic")
        mock_beads.get_children.return_value = [
            FakeTask(id="1", status="closed"),
            FakeTask(id="2", status="wip"),
            FakeTask(id="3", status="open"),
        ]
        res = client.get("/api/v2/epics/e1")
        assert res.status_code == 200
        p = res.json()["progress"]
        assert p == {"total": 3, "completed": 1, "wip": 1, "open": 1}

    def test_get_epic_404(self, client, mock_beads):
        mock_beads.get.return_value = None
        assert client.get("/api/v2/epics/nonexistent").status_code == 404

    def test_get_epic_empty_children(self, client, mock_beads):
        mock_beads.get.return_value = FakeTask(id="e1")
        mock_beads.get_children.return_value = []
        p = client.get("/api/v2/epics/e1").json()["progress"]
        assert p == {"total": 0, "completed": 0, "wip": 0, "open": 0}

    def test_get_epic_all_completed(self, client, mock_beads):
        mock_beads.get.return_value = FakeTask(id="e1")
        mock_beads.get_children.return_value = [
            FakeTask(id="1", status="closed"),
            FakeTask(id="2", status="closed"),
        ]
        p = client.get("/api/v2/epics/e1").json()["progress"]
        assert p == {"total": 2, "completed": 2, "wip": 0, "open": 0}


# ==================== Ready Endpoint ====================


class TestGetEpicReady:
    def test_returns_unblocked_children(self, client, mock_beads):
        mock_beads.get.return_value = FakeTask(id="e1")
        mock_beads.ready.return_value = [FakeTask(id="e1.1", title="Step 1")]
        res = client.get("/api/v2/epics/e1/ready")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 1
        assert data[0]["title"] == "Step 1"
        mock_beads.ready.assert_called_with(parent_id="e1")

    def test_epic_not_found_404(self, client, mock_beads):
        mock_beads.get.return_value = None
        assert client.get("/api/v2/epics/bad/ready").status_code == 404

    def test_empty_ready_list(self, client, mock_beads):
        mock_beads.get.return_value = FakeTask(id="e1")
        mock_beads.ready.return_value = []
        assert client.get("/api/v2/epics/e1/ready").json() == []


# ==================== Dependency Endpoints ====================


class TestDependencyEndpoints:
    def test_add_dependency(self, client, mock_beads):
        mock_beads.add_dependency.return_value = True
        res = client.post("/api/v2/beads/b2/dep", json={"depends_on": "b1"})
        assert res.status_code == 201
        mock_beads.add_dependency.assert_called_with("b2", "b1")

    def test_add_dependency_failure_400(self, client, mock_beads):
        mock_beads.add_dependency.return_value = False
        assert client.post("/api/v2/beads/b2/dep", json={"depends_on": "b1"}).status_code == 400

    def test_remove_dependency(self, client, mock_beads):
        mock_beads.remove_dependency.return_value = True
        assert client.delete("/api/v2/beads/b2/dep/b1").status_code == 200

    def test_remove_dependency_failure_400(self, client, mock_beads):
        mock_beads.remove_dependency.return_value = False
        assert client.delete("/api/v2/beads/b2/dep/b1").status_code == 400


# ==================== Bead-Session Lookup ====================


class TestBeadSessionLookup:
    def test_get_bead_session_returns_terminal(self, client):
        db_mod.create_terminal("t1", "cao-sess", "win", "q_cli", bead_id="bead-1")
        res = client.get("/api/v2/beads/bead-1/session")
        assert res.status_code == 200
        data = res.json()
        assert data["id"] == "t1"
        assert data["bead_id"] == "bead-1"

    def test_get_bead_session_404(self, client):
        assert client.get("/api/v2/beads/nonexistent/session").status_code == 404


# ==================== Assign-Agent Stores bead_id ====================


class TestAssignAgentBeadBinding:
    def test_assign_agent_passes_bead_id(self, client, mock_beads):
        mock_beads.get.return_value = FakeTask(id="bead-1", title="Task", description="Do stuff")
        mock_beads.wip.return_value = FakeTask(id="bead-1", status="wip")

        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_terminal.id = "term-99"

        with patch("cli_agent_orchestrator.api.v2.terminal_service") as mock_ts:
            mock_ts.create_terminal.return_value = mock_terminal
            res = client.post("/api/v2/beads/bead-1/assign-agent",
                              json={"agent_name": "dev", "provider": "q_cli"})
            assert res.status_code == 200
            assert mock_ts.create_terminal.call_args.kwargs.get("bead_id") == "bead-1"

    def test_assign_agent_409_if_already_assigned(self, client, mock_beads):
        mock_beads.get.return_value = FakeTask(id="bead-1", assignee="cao-other")
        res = client.post("/api/v2/beads/bead-1/assign-agent",
                          json={"agent_name": "dev", "provider": "q_cli"})
        assert res.status_code == 409

    def test_assign_agent_404_if_bead_missing(self, client, mock_beads):
        mock_beads.get.return_value = None
        assert client.post("/api/v2/beads/bad/assign-agent",
                           json={"agent_name": "dev", "provider": "q_cli"}).status_code == 404


# ==================== Assign Bead Stores bead_id ====================


class TestAssignBeadStoresBeadId:
    def test_assign_sets_bead_id_on_terminal(self, client, mock_beads):
        db_mod.create_terminal("t-existing", "cao-existing", "win", "q_cli")

        mock_beads.get.return_value = FakeTask(id="bead-x", title="Task")
        mock_beads.wip.return_value = FakeTask(id="bead-x", status="wip")

        with patch("cli_agent_orchestrator.api.v2.session_service") as mock_ss, \
             patch("cli_agent_orchestrator.api.v2.terminal_service"):
            mock_ss.get_session.return_value = {
                "terminals": [{"id": "t-existing"}]
            }
            res = client.post("/api/v2/beads/bead-x/assign",
                              json={"session_id": "cao-existing"})
            assert res.status_code == 200

        # Verify bead_id stored in DB
        meta = db_mod.get_terminal_metadata("t-existing")
        assert meta["bead_id"] == "bead-x"

        # Verify lookup works
        terminal = db_mod.get_terminal_by_bead("bead-x")
        assert terminal["id"] == "t-existing"
