"""BeadsClient - Wrapper around bd CLI for CAO integration."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List


# --- Label utility functions ---

def extract_label_value(labels: Optional[List[str]], prefix: str) -> Optional[str]:
    """Extract value from a label with format 'prefix:value'."""
    if not labels:
        return None
    for label in labels:
        if label.startswith(f"{prefix}:"):
            return label[len(prefix) + 1:]
    return None


def extract_context_files(labels: Optional[List[str]]) -> List[str]:
    """Extract all context file paths from labels."""
    if not labels:
        return []
    return [label.split(":", 1)[1] for label in labels if label.startswith("context:")]


def resolve_workspace(task: "Task", beads_client: Optional["BeadsClient"], default: Optional[str] = None) -> Optional[str]:
    """Resolve workspace by walking parent chain."""
    workspace = extract_label_value(task.labels, "workspace")
    if workspace:
        return workspace
    if task.parent_id and beads_client:
        parent = beads_client.get(task.parent_id)
        if parent:
            return resolve_workspace(parent, beads_client, default)
    return default


def resolve_context_files(task: "Task", beads_client: Optional["BeadsClient"]) -> List[str]:
    """Collect context files from task + parent chain, deduplicated."""
    files = extract_context_files(task.labels)
    if task.parent_id and beads_client:
        parent = beads_client.get(task.parent_id)
        if parent:
            parent_files = resolve_context_files(parent, beads_client)
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
    type: Optional[str] = None


class BeadsClient:
    """CAO-compatible client wrapping bd CLI."""

    def __init__(self, working_dir: Optional[str] = None):
        self.working_dir = working_dir

    def _run_bd(self, *args) -> str:
        cmd = ["bd", "--no-daemon"] + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.working_dir)
        if result.returncode != 0 and result.stderr:
            raise RuntimeError(f"bd error: {result.stderr}")
        return result.stdout.strip()

    def _parse_create_output(self, output: str) -> str:
        match = re.search(r"Created issue:\s*(\S+)", output)
        return match.group(1) if match else ""

    def _parse_json(self, output: str) -> list | dict:
        if not output:
            return []
        lines = output.split('\n')
        json_start = next((i for i, l in enumerate(lines) if l.strip().startswith(('[', '{'))), 0)
        json_str = '\n'.join(lines[json_start:])
        try:
            result = json.loads(json_str)
            return result if isinstance(result, (list, dict)) else []
        except json.JSONDecodeError:
            return []

    def _run_bd_json(self, *args) -> list | dict:
        output = self._run_bd(*args, "--json")
        return self._parse_json(output)

    def _cao_to_beads_priority(self, priority: int) -> int:
        return priority if priority in (1, 2, 3) else 2

    def _beads_to_cao_priority(self, priority: int) -> int:
        if priority <= 1: return 1
        if priority == 2: return 2
        if priority in (3, 4): return 3
        return 2

    def _cao_to_beads_status(self, status: str) -> str:
        return {"open": "open", "wip": "in_progress", "closed": "closed"}.get(status, "open")

    def _beads_to_cao_status(self, status: str) -> str:
        return {"open": "open", "in_progress": "wip", "closed": "closed"}.get(status, "open")

    def _issue_to_task(self, issue: dict) -> Task:
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
        issues = self._run_bd_json("ready")
        if not isinstance(issues, list) or not issues:
            return None
        tasks = [self._issue_to_task(i) for i in issues]
        if priority:
            tasks = [t for t in tasks if t.priority == priority]
        return tasks[0] if tasks else None

    def get(self, task_id: str) -> Optional[Task]:
        try:
            result = self._run_bd_json("show", task_id)
            if isinstance(result, list) and result:
                return self._issue_to_task(result[0])
        except Exception:
            pass
        return None

    def add(self, title: str, description: str = "", priority: int = 2, tags: str = "[]") -> Task:
        bd_priority = self._cao_to_beads_priority(priority)
        args = ["create", title, "-p", str(bd_priority)]
        if description:
            args.extend(["-d", description])
        output = self._run_bd(*args)
        task_id = self._parse_create_output(output)
        return self.get(task_id) or Task(id=task_id, title=title, priority=priority)

    def wip(self, task_id: str, assignee: Optional[str] = None) -> Optional[Task]:
        self._run_bd("update", task_id, "--status", "in_progress")
        if assignee:
            self._run_bd("update", task_id, "--assignee", assignee)
        return self.get(task_id)

    def close(self, task_id: str) -> Optional[Task]:
        self._run_bd("close", task_id)
        return self.get(task_id)

    def delete(self, task_id: str) -> bool:
        try:
            self._run_bd("delete", task_id, "--force")
            return True
        except Exception:
            return False

    def update(self, task_id: str, **kwargs) -> Optional[Task]:
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
        tasks = self.list(status="wip")
        count = 0
        for task in tasks:
            if task.assignee == session_id:
                self._run_bd("update", task.id, "--status", "open", "--assignee", "")
                count += 1
        return count

    def create_child(self, parent_id: str, title: str, description: str = "", priority: int = 2) -> Task:
        bd_priority = self._cao_to_beads_priority(priority)
        args = ["create", title, "-p", str(bd_priority), "--parent", parent_id]
        if description:
            args.extend(["-d", description])
        output = self._run_bd(*args)
        task_id = self._parse_create_output(output)
        return self.get(task_id) or Task(id=task_id, title=title, priority=priority, parent_id=parent_id)

    def get_children(self, parent_id: str) -> List[Task]:
        all_tasks = self.list()
        return [t for t in all_tasks if t.parent_id == parent_id or t.id.startswith(f"{parent_id}.")]

    def get_comments(self, task_id: str) -> List[dict]:
        result = self._run_bd_json("comments", task_id)
        return result if isinstance(result, list) else []

    def add_comment(self, task_id: str, comment: str) -> bool:
        try:
            self._run_bd("comments", "add", task_id, comment)
            return True
        except Exception:
            return False

    def add_dependency(self, task_id: str, depends_on_id: str) -> bool:
        try:
            self._run_bd("dep", "add", task_id, depends_on_id)
            return True
        except Exception:
            return False

    def remove_dependency(self, task_id: str, depends_on_id: str) -> bool:
        try:
            self._run_bd("dep", "remove", task_id, depends_on_id)
            return True
        except Exception:
            return False

    def update_notes(self, task_id: str, notes: str) -> Optional[Task]:
        self._run_bd("update", task_id, "--notes", notes)
        return self.get(task_id)

    def add_label(self, task_id: str, label: str) -> bool:
        try:
            self._run_bd("label", "add", task_id, label)
            return True
        except Exception:
            return False

    def remove_label(self, task_id: str, label: str) -> bool:
        try:
            self._run_bd("label", "remove", task_id, label)
            return True
        except Exception:
            return False

    def is_epic(self, task_id: str) -> bool:
        return len(self.get_children(task_id)) > 0

    def ready(self, parent_id: Optional[str] = None) -> List[Task]:
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
        parent = self.add(title, description=description, priority=priority)
        self.add_label(parent.id, "type:epic")
        if max_concurrent > 0:
            self.add_label(parent.id, f"max_concurrent:{max_concurrent}")
        for label in (labels or []):
            self.add_label(parent.id, label)
        prev_child_id = None
        for step in steps:
            child = self.create_child(parent.id, step, priority=priority)
            if sequential and prev_child_id:
                self.add_dependency(child.id, prev_child_id)
            prev_child_id = child.id
        return self.get(parent.id) or parent
