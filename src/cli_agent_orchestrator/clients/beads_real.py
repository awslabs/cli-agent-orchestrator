"""BeadsClient - Wrapper around bd CLI for CAO integration."""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List


# --- Label utility functions ---

def extract_label_value(labels: Optional[List[str]], prefix: str) -> Optional[str]:
    """Extract value from a label with format 'prefix:value'.

    Example: extract_label_value(["workspace:/foo", "type:epic"], "workspace") -> "/foo"
    """
    if not labels:
        return None
    for label in labels:
        if label.startswith(f"{prefix}:"):
            return label[len(prefix) + 1:]
    return None


def extract_context_files(labels: Optional[List[str]]) -> List[str]:
    """Extract all context file paths from labels.

    Labels like 'context:/path/to/file.md' -> ['/path/to/file.md']
    """
    if not labels:
        return []
    return [label.split(":", 1)[1] for label in labels if label.startswith("context:")]


def resolve_workspace(task: "Task", beads_client: Optional["BeadsClient"], default: Optional[str] = None) -> Optional[str]:
    """Resolve workspace for a task by walking parent chain.

    Checks task's own labels first, then parent's, then default.
    """
    workspace = extract_label_value(task.labels, "workspace")
    if workspace:
        return workspace
    if task.parent_id and beads_client:
        parent = beads_client.get(task.parent_id)
        if parent:
            return resolve_workspace(parent, beads_client, default)
    return default


def resolve_context_files(task: "Task", beads_client: Optional["BeadsClient"]) -> List[str]:
    """Collect context file paths from task + parent chain, deduplicated.

    Walks up the parent_id chain, collecting context: labels from each level.
    Task's own files come first, then parent's.
    """
    files = extract_context_files(task.labels)
    if task.parent_id and beads_client:
        parent = beads_client.get(task.parent_id)
        if parent:
            parent_files = resolve_context_files(parent, beads_client)
            # Dedupe: parent files after task files, preserving order
            seen = set(files)
            for f in parent_files:
                if f not in seen:
                    seen.add(f)
                    files.append(f)
    return files


@dataclass
class Task:
    """CAO-compatible task wrapper around Beads Issue."""

    id: str
    title: str
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
    type: Optional[str] = None  # "epic", "task", "bug", etc. (from bd issue_type)


class BeadsClient:
    """CAO-compatible client wrapping bd CLI."""

    def __init__(self, working_dir: Optional[str] = None):
        self.working_dir = working_dir

    def _run_bd(self, *args) -> str:
        """Run bd command and return output."""
        cmd = ["bd", "--no-daemon"] + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.working_dir)
        if result.returncode != 0 and result.stderr:
            raise RuntimeError(f"bd error: {result.stderr}")
        return result.stdout.strip()

    def _parse_create_output(self, output: str) -> str:
        """Extract task ID from bd create output."""
        match = re.search(r"Created issue:\s*(\S+)", output)
        return match.group(1) if match else ""

    def _parse_json(self, output: str) -> list | dict:
        """Parse JSON output from bd commands."""
        if not output:
            return []
        # Strip non-JSON lines (e.g., "Note: ..." warnings)
        lines = output.split('\n')
        json_start = next((i for i, l in enumerate(lines) if l.strip().startswith(('[', '{'))), 0)
        json_str = '\n'.join(lines[json_start:])
        try:
            result = json.loads(json_str)
            return result if isinstance(result, (list, dict)) else []
        except json.JSONDecodeError:
            return []

    def _run_bd_json(self, *args) -> list | dict:
        """Run bd command with JSON output."""
        output = self._run_bd(*args, "--json")
        return self._parse_json(output)

    def _cao_to_beads_priority(self, priority: int) -> int:
        """Convert CAO priority (1-3) to Beads priority (0-4)."""
        if priority in (1, 2, 3):
            return priority
        return 2

    def _beads_to_cao_priority(self, priority: int) -> int:
        """Convert Beads priority (0-4) to CAO priority (1-3)."""
        if priority <= 1:
            return 1
        if priority == 2:
            return 2
        if priority in (3, 4):
            return 3
        return 2

    def _cao_to_beads_status(self, status: str) -> str:
        """Convert CAO status to Beads status."""
        mapping = {"open": "open", "wip": "in_progress", "closed": "closed"}
        return mapping.get(status, "open")

    def _beads_to_cao_status(self, status: str) -> str:
        """Convert Beads status to CAO status."""
        mapping = {"open": "open", "in_progress": "wip", "closed": "closed"}
        return mapping.get(status, "open")

    def _issue_to_task(self, issue: dict) -> Task:
        """Convert Beads Issue dict to CAO Task."""
        # Extract blocked_by from dependencies
        blocked_by = None
        deps = issue.get("dependencies", [])
        if deps:
            blocked_by = [d.get("id") for d in deps if d.get("dependency_type") == "blocks"]
        return Task(
            id=issue.get("id", ""),
            title=issue.get("title", ""),
            description=issue.get("description", "") or "",
            priority=self._beads_to_cao_priority(issue.get("priority", 2)),
            status=self._beads_to_cao_status(issue.get("status", "open")),
            assignee=issue.get("assignee"),
            created_at=issue.get("created_at"),
            updated_at=issue.get("updated_at"),
            closed_at=issue.get("closed_at"),
            parent_id=issue.get("parent") or issue.get("parent_id"),
            blocked_by=blocked_by if blocked_by else None,
            labels=issue.get("labels") or None,
            type=issue.get("issue_type") or None,
        )

    def list(self, status: Optional[str] = None, priority: Optional[int] = None) -> list[Task]:
        """List tasks, optionally filtered by status/priority."""
        args = ["list"]
        if status:
            args.extend(["--status", self._cao_to_beads_status(status)])
        issues = self._run_bd_json(*args)
        if not isinstance(issues, list):
            issues = []
        tasks = [self._issue_to_task(i) for i in issues]
        if priority:
            tasks = [t for t in tasks if t.priority == priority]
        return sorted(tasks, key=lambda t: (t.priority, t.created_at or ""))

    def next(self, priority: Optional[int] = None) -> Optional[Task]:
        """Get next ready task (no blockers)."""
        issues = self._run_bd_json("ready")
        if not isinstance(issues, list) or not issues:
            return None
        tasks = [self._issue_to_task(i) for i in issues]
        if priority:
            tasks = [t for t in tasks if t.priority == priority]
        return tasks[0] if tasks else None

    def get(self, task_id: str) -> Optional[Task]:
        """Get a single task by ID."""
        try:
            result = self._run_bd_json("show", task_id)
            if isinstance(result, list) and result:
                return self._issue_to_task(result[0])
        except Exception:
            pass
        return None

    def add(self, title: str, description: str = "", priority: int = 2, tags: str = "[]") -> Task:
        """Create a new task."""
        bd_priority = self._cao_to_beads_priority(priority)
        args = ["create", title, "-p", str(bd_priority)]
        if description:
            args.extend(["-d", description])
        output = self._run_bd(*args)
        task_id = self._parse_create_output(output)
        return self.get(task_id) or Task(id=task_id, title=title, priority=priority)

    def wip(self, task_id: str, assignee: Optional[str] = None) -> Optional[Task]:
        """Mark task as work-in-progress."""
        self._run_bd("update", task_id, "--status", "in_progress")
        if assignee:
            self._run_bd("update", task_id, "--assignee", assignee)
        return self.get(task_id)

    def close(self, task_id: str) -> Optional[Task]:
        """Close a task."""
        self._run_bd("close", task_id)
        return self.get(task_id)

    def delete(self, task_id: str) -> bool:
        """Delete a task."""
        try:
            self._run_bd("delete", task_id, "--force")
            return True
        except Exception:
            return False

    def update(self, task_id: str, **kwargs) -> Optional[Task]:
        """Update task fields."""
        args = ["update", task_id]
        if "title" in kwargs:
            args.extend(["--title", kwargs["title"]])
        if "description" in kwargs:
            args.extend(["--description", kwargs["description"]])
        if "priority" in kwargs:
            args.extend(["-p", str(self._cao_to_beads_priority(kwargs["priority"]))])
        if "status" in kwargs:
            args.extend(["--status", self._cao_to_beads_status(kwargs["status"])])
        if "assignee" in kwargs:
            args.extend(["--assignee", kwargs["assignee"]])
        if len(args) > 2:
            self._run_bd(*args)
        return self.get(task_id)

    def clear_assignee_by_session(self, session_id: str) -> int:
        """Clear assignee from tasks assigned to session."""
        tasks = self.list(status="wip")
        count = 0
        for task in tasks:
            if task.assignee == session_id:
                self._run_bd("update", task.id, "--status", "open", "--assignee", "")
                count += 1
        return count

    def create_child(self, parent_id: str, title: str, description: str = "", priority: int = 2) -> Task:
        """Create a child task under a parent."""
        bd_priority = self._cao_to_beads_priority(priority)
        args = ["create", title, "-p", str(bd_priority), "--parent", parent_id]
        if description:
            args.extend(["-d", description])
        output = self._run_bd(*args)
        task_id = self._parse_create_output(output)
        return self.get(task_id) or Task(id=task_id, title=title, priority=priority, parent_id=parent_id)

    def get_children(self, parent_id: str) -> List[Task]:
        """Get child tasks of a parent."""
        all_tasks = self.list()
        # Match by parent_id or by ID prefix (e.g., parent-id.1 is child of parent-id)
        return [t for t in all_tasks if t.parent_id == parent_id or t.id.startswith(f"{parent_id}.")]

    def get_comments(self, task_id: str) -> List[dict]:
        """Get comments on a bead (used for agent findings/notes)."""
        result = self._run_bd_json("comments", task_id)
        if not isinstance(result, list):
            return []
        return result

    def add_comment(self, task_id: str, comment: str) -> bool:
        """Add a comment to a bead."""
        try:
            self._run_bd("comments", "add", task_id, comment)
            return True
        except Exception:
            return False

    def add_dependency(self, task_id: str, depends_on_id: str) -> bool:
        """Add a dependency: task_id is blocked by depends_on_id."""
        try:
            self._run_bd("dep", "add", task_id, depends_on_id)
            return True
        except Exception:
            return False

    def remove_dependency(self, task_id: str, depends_on_id: str) -> bool:
        """Remove a dependency."""
        try:
            self._run_bd("dep", "remove", task_id, depends_on_id)
            return True
        except Exception:
            return False

    def update_notes(self, task_id: str, notes: str) -> Optional[Task]:
        """Update notes on a bead."""
        self._run_bd("update", task_id, "--notes", notes)
        return self.get(task_id)

    def add_label(self, task_id: str, label: str) -> bool:
        """Add a label to a bead."""
        try:
            self._run_bd("label", "add", task_id, label)
            return True
        except Exception:
            return False

    def remove_label(self, task_id: str, label: str) -> bool:
        """Remove a label from a bead."""
        try:
            self._run_bd("label", "remove", task_id, label)
            return True
        except Exception:
            return False

    def is_epic(self, task_id: str) -> bool:
        """Check if a task is an epic (has children)."""
        children = self.get_children(task_id)
        return len(children) > 0

    def ready(self, parent_id: Optional[str] = None) -> List[Task]:
        """Get ready (unblocked, open) tasks. If parent_id given, only children of that parent."""
        issues = self._run_bd_json("ready")
        if not isinstance(issues, list):
            return []
        tasks = [self._issue_to_task(i) for i in issues]
        if parent_id:
            tasks = [t for t in tasks if t.parent_id == parent_id or t.id.startswith(f"{parent_id}.")]
        return tasks

    def create_epic(
        self, title: str, steps: List[str], priority: int = 2,
        sequential: bool = True, max_concurrent: int = 3,
        labels: Optional[List[str]] = None, description: str = ""
    ) -> Task:
        """Create an epic (parent bead) with child beads for each step.

        If sequential=True, each child after the first is blocked_by the previous one.
        Adds type:epic and max_concurrent labels to parent.
        Returns the parent Task.
        """
        # Create parent
        parent = self.add(title, description=description, priority=priority)

        # Add labels to parent
        self.add_label(parent.id, "type:epic")
        if max_concurrent > 0:
            self.add_label(parent.id, f"max_concurrent:{max_concurrent}")
        for label in (labels or []):
            self.add_label(parent.id, label)

        # Create children with sequential deps
        prev_child_id = None
        for step in steps:
            child = self.create_child(parent.id, step, priority=priority)
            if sequential and prev_child_id:
                self.add_dependency(child.id, prev_child_id)
            prev_child_id = child.id

        return self.get(parent.id) or parent
