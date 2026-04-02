"""API integration tests — full HTTP workflows for epic/bead operations.

Uses FastAPI TestClient with stateful mocked BeadsClient that simulates
real epic lifecycle (children track status, ready() respects deps).
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db_mod


@dataclass
class FakeTask:
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


class FakeBeadsState:
    """Stateful mock that simulates real beads lifecycle."""

    def __init__(self):
        self.beads: Dict[str, FakeTask] = {}
        self.deps: Dict[str, List[str]] = {}  # task_id -> [blocked_by_ids]
        self._counter = 0

    def _next_id(self, prefix="fake"):
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def add(self, title, description="", priority=2, tags="[]"):
        tid = self._next_id()
        t = FakeTask(id=tid, title=title, description=description, priority=priority)
        self.beads[tid] = t
        return t

    def get(self, task_id):
        return self.beads.get(task_id)

    def get_children(self, parent_id):
        return [t for t in self.beads.values() if t.parent_id == parent_id]

    def create_epic(self, title, steps, description="", priority=2, sequential=True, max_concurrent=3, labels=None):
        epic = FakeTask(id=self._next_id("epic"), title=title, type="epic",
                        labels=["type:epic", f"max_concurrent:{max_concurrent}"] + (labels or []))
        self.beads[epic.id] = epic
        prev = None
        for s in steps:
            child = FakeTask(id=self._next_id(epic.id), title=s, parent_id=epic.id)
            self.beads[child.id] = child
            if sequential and prev:
                self.deps.setdefault(child.id, []).append(prev)
            prev = child.id
        return epic

    def ready(self, parent_id=None):
        result = []
        for t in self.beads.values():
            if t.status != "open":
                continue
            blockers = self.deps.get(t.id, [])
            if all(self.beads.get(b, FakeTask(id=b)).status == "closed" for b in blockers):
                if parent_id is None or t.parent_id == parent_id:
                    result.append(t)
        return result

    def wip(self, task_id, assignee=None):
        t = self.beads.get(task_id)
        if t:
            t.status = "wip"
            t.assignee = assignee
        return t

    def close(self, task_id):
        t = self.beads.get(task_id)
        if t:
            t.status = "closed"
        return t

    def add_dependency(self, task_id, depends_on):
        self.deps.setdefault(task_id, []).append(depends_on)
        return True

    def remove_dependency(self, task_id, depends_on):
        if task_id in self.deps:
            self.deps[task_id] = [d for d in self.deps[task_id] if d != depends_on]
        return True

    def clear_assignee_by_session(self, session_id):
        count = 0
        for t in self.beads.values():
            if t.assignee == session_id:
                t.assignee = None
                t.status = "open"
                count += 1
        return count


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db_file = tmp_path / "cao.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    sf = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    orig_e, orig_s = db_mod.engine, db_mod.SessionLocal
    db_mod.engine = engine
    db_mod.SessionLocal = sf
    db_mod.Base.metadata.create_all(bind=engine)
    yield
    db_mod.engine = orig_e
    db_mod.SessionLocal = orig_s


@pytest.fixture
def fake_state():
    return FakeBeadsState()


@pytest.fixture
def client(fake_state):
    with patch("cli_agent_orchestrator.api.v2.beads", fake_state):
        from cli_agent_orchestrator.api.main import app
        yield TestClient(app)


# ── Full Epic API Flow ───────────────────────────────────────

class TestEpicApiFlow:
    def test_create_epic_then_get_progress(self, client, fake_state):
        """Create epic → GET progress shows 0 completed."""
        res = client.post("/api/v2/epics", json={"title": "Flow Epic", "steps": ["A", "B", "C"]})
        assert res.status_code == 201
        epic_id = res.json()["epic"]["id"]

        res2 = client.get(f"/api/v2/epics/{epic_id}")
        assert res2.status_code == 200
        p = res2.json()["progress"]
        assert p["total"] == 3
        assert p["completed"] == 0
        assert p["open"] == 3

    def test_create_epic_then_get_ready(self, client, fake_state):
        """Create sequential epic → only first step ready."""
        res = client.post("/api/v2/epics", json={"title": "Ready Epic", "steps": ["First", "Second"]})
        epic_id = res.json()["epic"]["id"]

        res2 = client.get(f"/api/v2/epics/{epic_id}/ready")
        assert res2.status_code == 200
        ready = res2.json()
        assert len(ready) == 1
        assert ready[0]["title"] == "First"

    def test_progress_updates_as_children_close(self, client, fake_state):
        """Closing children updates progress count."""
        res = client.post("/api/v2/epics", json={"title": "Progress Epic", "steps": ["X", "Y", "Z"]})
        epic_id = res.json()["epic"]["id"]
        children = res.json()["children"]

        # Close first child
        fake_state.close(children[0]["id"])
        p1 = client.get(f"/api/v2/epics/{epic_id}").json()["progress"]
        assert p1["completed"] == 1
        assert p1["open"] == 2

        # Close second
        fake_state.close(children[1]["id"])
        p2 = client.get(f"/api/v2/epics/{epic_id}").json()["progress"]
        assert p2["completed"] == 2

        # Close third
        fake_state.close(children[2]["id"])
        p3 = client.get(f"/api/v2/epics/{epic_id}").json()["progress"]
        assert p3["completed"] == 3
        assert p3["open"] == 0

    def test_sequential_closing_unblocks_next(self, client, fake_state):
        """Sequential epic: closing step 1 unblocks step 2."""
        res = client.post("/api/v2/epics", json={"title": "Seq", "steps": ["S1", "S2", "S3"]})
        epic_id = res.json()["epic"]["id"]
        children = res.json()["children"]

        # Only S1 ready
        ready1 = client.get(f"/api/v2/epics/{epic_id}/ready").json()
        assert len(ready1) == 1 and ready1[0]["title"] == "S1"

        # Close S1 → S2 ready
        fake_state.close(children[0]["id"])
        ready2 = client.get(f"/api/v2/epics/{epic_id}/ready").json()
        assert any(t["title"] == "S2" for t in ready2)


# ── Assignment Workflows ────────────────────────────────────

class TestAssignmentWorkflows:
    def test_assign_agent_creates_bead_binding(self, client, fake_state):
        """assign-agent stores bead_id on terminal."""
        bead = fake_state.add("Assign Test")
        mock_term = MagicMock()
        mock_term.session_name = "cao-test"
        mock_term.id = "term-1"
        with patch("cli_agent_orchestrator.api.v2.terminal_service") as ts:
            ts.create_terminal.return_value = mock_term
            res = client.post(f"/api/v2/beads/{bead.id}/assign-agent",
                              json={"agent_name": "dev", "provider": "q_cli"})
            assert res.status_code == 200
            assert ts.create_terminal.call_args.kwargs["bead_id"] == bead.id

    def test_assign_then_lookup(self, client, fake_state):
        """Assign bead to session → GET /beads/{id}/session works."""
        bead = fake_state.add("Lookup Test")
        db_mod.create_terminal("t-lu", "cao-lu", "w", "q_cli", bead_id=bead.id)

        res = client.get(f"/api/v2/beads/{bead.id}/session")
        assert res.status_code == 200
        assert res.json()["id"] == "t-lu"

    def test_assign_already_assigned_409(self, client, fake_state):
        """Assigning already-assigned bead returns 409."""
        bead = fake_state.add("Double")
        bead.assignee = "cao-other"

        res = client.post(f"/api/v2/beads/{bead.id}/assign-agent",
                          json={"agent_name": "dev", "provider": "q_cli"})
        assert res.status_code == 409

    def test_assign_nonexistent_404(self, client, fake_state):
        res = client.post("/api/v2/beads/bad-id/assign-agent",
                          json={"agent_name": "dev", "provider": "q_cli"})
        assert res.status_code == 404

    def test_assign_stores_bead_id_on_terminal_via_assign_endpoint(self, client, fake_state):
        """POST /beads/{id}/assign stores bead_id on the session's terminal."""
        bead = fake_state.add("Direct Assign")
        db_mod.create_terminal("t-da", "cao-da", "w", "q_cli")

        with patch("cli_agent_orchestrator.api.v2.session_service") as ss, \
             patch("cli_agent_orchestrator.api.v2.terminal_service"):
            ss.get_session.return_value = {"terminals": [{"id": "t-da"}]}
            res = client.post(f"/api/v2/beads/{bead.id}/assign",
                              json={"session_id": "cao-da"})
            assert res.status_code == 200

        meta = db_mod.get_terminal_metadata("t-da")
        assert meta["bead_id"] == bead.id


# ── Error Handling ──────────────────────────────────────────

class TestErrorHandling:
    def test_get_epic_nonexistent_404(self, client, fake_state):
        assert client.get("/api/v2/epics/nope").status_code == 404

    def test_create_epic_no_steps_400(self, client, fake_state):
        assert client.post("/api/v2/epics", json={"title": "E", "steps": []}).status_code == 400

    def test_get_bead_session_no_binding_404(self, client, fake_state):
        assert client.get("/api/v2/beads/unbound/session").status_code == 404

    def test_add_dep_via_api(self, client, fake_state):
        a = fake_state.add("A")
        b = fake_state.add("B")
        res = client.post(f"/api/v2/beads/{b.id}/dep", json={"depends_on": a.id})
        assert res.status_code == 201

        # B should now be blocked
        ready = fake_state.ready()
        assert b.id not in [t.id for t in ready]
