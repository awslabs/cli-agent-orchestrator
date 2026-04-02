"""Integration tests for epic workflows.

Uses FakeBeadsState for reliable lifecycle testing + real CAO DB for
bead-session binding tests. Tests full workflows end-to-end.
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db_mod
from cli_agent_orchestrator.clients.beads_real import (
    Task, extract_label_value, extract_context_files,
    resolve_workspace, resolve_context_files,
)


# ── Fake Beads State (reliable lifecycle simulation) ─────────

@dataclass
class FakeTask:
    id: str
    title: str = ""
    description: str = ""
    priority: int = 2
    status: str = "open"
    assignee: Optional[str] = None
    parent_id: Optional[str] = None
    labels: Optional[List[str]] = None
    type: Optional[str] = None
    blocked_by: Optional[List[str]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    closed_at: Optional[str] = None
    tags: str = "[]"
    metadata: str = "{}"


class FakeBeadsState:
    def __init__(self):
        self.beads: Dict[str, FakeTask] = {}
        self.deps: Dict[str, List[str]] = {}
        self.comments: Dict[str, List[str]] = {}
        self._n = 0

    def _id(self, prefix="t"):
        self._n += 1
        return f"{prefix}-{self._n}"

    def add(self, title, description="", priority=2, **kw):
        t = FakeTask(id=self._id(), title=title, description=description, priority=priority)
        self.beads[t.id] = t
        return t

    def get(self, tid):
        return self.beads.get(tid)

    def get_children(self, pid):
        return [t for t in self.beads.values() if t.parent_id == pid]

    def create_epic(self, title, steps, sequential=True, **kw):
        epic = FakeTask(id=self._id("epic"), title=title, type="epic",
                        labels=["type:epic"])
        self.beads[epic.id] = epic
        prev = None
        for s in steps:
            child = FakeTask(id=self._id(epic.id), title=s, parent_id=epic.id)
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
            if all(self.beads[b].status == "closed" for b in blockers if b in self.beads):
                if parent_id is None or t.parent_id == parent_id:
                    result.append(t)
        return result

    def close(self, tid):
        if tid in self.beads:
            self.beads[tid].status = "closed"
        return self.beads.get(tid)

    def wip(self, tid, assignee=None):
        if tid in self.beads:
            self.beads[tid].status = "wip"
            self.beads[tid].assignee = assignee
        return self.beads.get(tid)

    def is_epic(self, tid):
        return len(self.get_children(tid)) > 0

    def add_dependency(self, tid, dep):
        self.deps.setdefault(tid, []).append(dep)
        return True

    def remove_dependency(self, tid, dep):
        if tid in self.deps:
            self.deps[tid] = [d for d in self.deps[tid] if d != dep]
        return True

    def add_comment(self, tid, text):
        self.comments.setdefault(tid, []).append(text)
        return True

    def get_comments(self, tid):
        return [{"body": c} for c in self.comments.get(tid, [])]


# ── Fixtures ─────────────────────────────────────────────────

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
def state():
    return FakeBeadsState()


# ── Epic Lifecycle Tests ─────────────────────────────────────

class TestEpicLifecycle:
    def test_create_epic_produces_parent_with_children(self, state):
        epic = state.create_epic("Test Epic", ["Step A", "Step B", "Step C"])
        assert epic.type == "epic"
        children = state.get_children(epic.id)
        assert len(children) == 3
        assert {c.title for c in children} == {"Step A", "Step B", "Step C"}

    def test_sequential_epic_only_first_child_ready(self, state):
        epic = state.create_epic("Seq", ["First", "Second", "Third"])
        ready = state.ready(parent_id=epic.id)
        assert len(ready) == 1
        assert ready[0].title == "First"

    def test_closing_child_unblocks_next(self, state):
        epic = state.create_epic("Unblock", ["Do A", "Do B"])
        r1 = state.ready(parent_id=epic.id)
        assert r1[0].title == "Do A"
        state.close(r1[0].id)
        r2 = state.ready(parent_id=epic.id)
        assert any(t.title == "Do B" for t in r2)

    def test_close_all_children_100_percent(self, state):
        epic = state.create_epic("Full", ["X", "Y", "Z"])
        children = state.get_children(epic.id)
        for c in children:
            state.close(c.id)
        closed = [c for c in state.get_children(epic.id) if c.status == "closed"]
        assert len(closed) == 3

    def test_parallel_epic_all_ready(self, state):
        epic = state.create_epic("Par", ["A", "B", "C"], sequential=False)
        ready = state.ready(parent_id=epic.id)
        assert len(ready) == 3

    def test_is_epic_true_for_parent(self, state):
        epic = state.create_epic("E", ["Sub"])
        assert state.is_epic(epic.id)

    def test_is_epic_false_for_leaf(self, state):
        leaf = state.add("Leaf")
        assert not state.is_epic(leaf.id)

    def test_epic_children_have_parent_id(self, state):
        epic = state.create_epic("Parent", ["C1", "C2"])
        for c in state.get_children(epic.id):
            assert c.parent_id == epic.id


# ── Dependency Chain Tests ───────────────────────────────────

class TestDependencyChains:
    def test_add_dependency_blocks_task(self, state):
        a = state.add("A")
        b = state.add("B")
        state.add_dependency(b.id, a.id)
        ready = state.ready()
        ready_ids = [t.id for t in ready]
        assert a.id in ready_ids
        assert b.id not in ready_ids

    def test_remove_dependency_unblocks(self, state):
        a = state.add("Blocker")
        b = state.add("Blocked")
        state.add_dependency(b.id, a.id)
        state.remove_dependency(b.id, a.id)
        ready = state.ready()
        assert b.id in [t.id for t in ready]

    def test_chain_a_b_c(self, state):
        a, b, c = state.add("A"), state.add("B"), state.add("C")
        state.add_dependency(b.id, a.id)
        state.add_dependency(c.id, b.id)

        assert a.id in [t.id for t in state.ready()]
        assert b.id not in [t.id for t in state.ready()]

        state.close(a.id)
        assert b.id in [t.id for t in state.ready()]
        assert c.id not in [t.id for t in state.ready()]

        state.close(b.id)
        assert c.id in [t.id for t in state.ready()]

    def test_multiple_blockers(self, state):
        """Task blocked by two deps — both must close."""
        a, b, c = state.add("A"), state.add("B"), state.add("C")
        state.add_dependency(c.id, a.id)
        state.add_dependency(c.id, b.id)

        assert c.id not in [t.id for t in state.ready()]
        state.close(a.id)
        assert c.id not in [t.id for t in state.ready()]  # still blocked by B
        state.close(b.id)
        assert c.id in [t.id for t in state.ready()]

    def test_sequential_epic_full_walkthrough(self, state):
        """Walk through a 4-step sequential epic step by step."""
        epic = state.create_epic("Walk", ["S1", "S2", "S3", "S4"])
        children = state.get_children(epic.id)

        # Only S1 ready
        r = state.ready(parent_id=epic.id)
        assert len(r) == 1 and r[0].title == "S1"

        # Close S1 → S2 ready
        state.close(children[0].id)
        r = state.ready(parent_id=epic.id)
        assert len(r) == 1 and r[0].title == "S2"

        # Close S2 → S3 ready
        state.close(children[1].id)
        r = state.ready(parent_id=epic.id)
        assert len(r) == 1 and r[0].title == "S3"

        # Close S3 → S4 ready
        state.close(children[2].id)
        r = state.ready(parent_id=epic.id)
        assert len(r) == 1 and r[0].title == "S4"

        # Close S4 → nothing ready
        state.close(children[3].id)
        r = state.ready(parent_id=epic.id)
        assert len(r) == 0


# ── Label + Context Resolution Tests ────────────────────────

class TestLabelResolution:
    def test_workspace_from_parent(self):
        parent = Task(id="p", title="P", labels=["workspace:/project"])
        child = Task(id="c", title="C", parent_id="p")
        mock = MagicMock()
        mock.get.return_value = parent
        assert resolve_workspace(child, mock, "/default") == "/project"

    def test_child_workspace_overrides_parent(self):
        parent = Task(id="p", title="P", labels=["workspace:/parent"])
        child = Task(id="c", title="C", parent_id="p", labels=["workspace:/child"])
        mock = MagicMock()
        mock.get.return_value = parent
        assert resolve_workspace(child, mock) == "/child"

    def test_context_files_from_parent_chain(self):
        parent = Task(id="p", title="P", labels=["context:/parent.md"])
        child = Task(id="c", title="C", parent_id="p", labels=["context:/child.md"])
        mock = MagicMock()
        mock.get.return_value = parent
        assert resolve_context_files(child, mock) == ["/child.md", "/parent.md"]

    def test_context_files_deduplicated(self):
        parent = Task(id="p", title="P", labels=["context:/shared.md", "context:/extra.md"])
        child = Task(id="c", title="C", parent_id="p", labels=["context:/shared.md"])
        mock = MagicMock()
        mock.get.return_value = parent
        assert resolve_context_files(child, mock) == ["/shared.md", "/extra.md"]

    def test_deep_parent_chain(self):
        gp = Task(id="gp", title="GP", labels=["context:/gp.md"])
        p = Task(id="p", title="P", parent_id="gp", labels=["context:/p.md"])
        c = Task(id="c", title="C", parent_id="p", labels=["context:/c.md"])
        mock = MagicMock()
        mock.get.side_effect = lambda tid: {"p": p, "gp": gp}.get(tid)
        assert resolve_context_files(c, mock) == ["/c.md", "/p.md", "/gp.md"]

    def test_no_labels_returns_empty(self):
        task = Task(id="t", title="T")
        assert resolve_context_files(task, None) == []

    def test_extract_label_value_multiple(self):
        labels = ["workspace:/a", "context:/b.md", "type:epic"]
        assert extract_label_value(labels, "workspace") == "/a"
        assert extract_label_value(labels, "type") == "epic"
        assert extract_label_value(labels, "missing") is None


# ── Bead-Session Binding Tests ──────────────────────────────

class TestBeadSessionBinding:
    def test_create_terminal_with_bead_id(self):
        r = db_mod.create_terminal("t1", "s", "w", "q_cli", bead_id="b1")
        assert r["bead_id"] == "b1"

    def test_bead_id_in_all_query_functions(self):
        db_mod.create_terminal("t2", "s", "w", "q_cli", bead_id="b2")
        assert db_mod.get_terminal_metadata("t2")["bead_id"] == "b2"
        assert any(t["bead_id"] == "b2" for t in db_mod.list_all_terminals())
        assert any(t["bead_id"] == "b2" for t in db_mod.list_terminals_by_session("s"))
        assert db_mod.get_terminal_by_bead("b2")["id"] == "t2"

    def test_set_and_clear_bead_id(self):
        db_mod.create_terminal("t3", "s", "w", "q_cli")
        db_mod.set_terminal_bead("t3", "b3")
        assert db_mod.get_terminal_by_bead("b3")["id"] == "t3"
        db_mod.set_terminal_bead("t3", None)
        assert db_mod.get_terminal_by_bead("b3") is None

    def test_delete_terminals_clears_lookup(self):
        db_mod.create_terminal("t4", "del-sess", "w", "q_cli", bead_id="b4")
        db_mod.delete_terminals_by_session("del-sess")
        assert db_mod.get_terminal_by_bead("b4") is None

    def test_multiple_terminals_different_beads(self):
        db_mod.create_terminal("t5", "s", "w1", "q_cli", bead_id="bx")
        db_mod.create_terminal("t6", "s", "w2", "q_cli", bead_id="by")
        assert db_mod.get_terminal_by_bead("bx")["id"] == "t5"
        assert db_mod.get_terminal_by_bead("by")["id"] == "t6"

    def test_reassign_bead_after_clear(self):
        """Clear bead from one terminal, assign to another."""
        db_mod.create_terminal("t7", "s1", "w", "q_cli", bead_id="bz")
        db_mod.set_terminal_bead("t7", None)
        db_mod.create_terminal("t8", "s2", "w", "q_cli", bead_id="bz")
        assert db_mod.get_terminal_by_bead("bz")["id"] == "t8"


# ── Comments Tests ──────────────────────────────────────────

class TestComments:
    def test_add_and_read_comments(self, state):
        bead = state.add("Comment bead")
        state.add_comment(bead.id, "Root cause in auth.py")
        comments = state.get_comments(bead.id)
        assert len(comments) == 1
        assert "Root cause" in comments[0]["body"]

    def test_multiple_comments(self, state):
        bead = state.add("Multi comment")
        state.add_comment(bead.id, "Finding 1")
        state.add_comment(bead.id, "Finding 2")
        assert len(state.get_comments(bead.id)) == 2

    def test_no_comments_returns_empty(self, state):
        bead = state.add("No comments")
        assert state.get_comments(bead.id) == []
