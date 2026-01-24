"""BeadsClient - Wrapper around bd CLI for CAO integration."""
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Optional


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
        try:
            return json.loads(output)
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
        bd_priority = issue.get("priority", 2)
        state = issue.get("status", "open")
        return Task(
            id=issue.get("id", ""),
            title=issue.get("title", ""),
            description=issue.get("description", "") or "",
            priority=self._beads_to_cao_priority(bd_priority),
            status=self._beads_to_cao_status(state),
            assignee=issue.get("assignee"),
            created_at=issue.get("created_at"),
            updated_at=issue.get("updated_at"),
            closed_at=issue.get("closed_at"),
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
            issue = self._run_bd_json("show", task_id)
            if isinstance(issue, dict):
                return self._issue_to_task(issue)
        except Exception:
            pass
        return None
    
    def add(self, title: str, description: str = "", priority: int = 2, tags: str = "[]") -> Task:
        """Create a new task."""
        # Map CAO priority (1-3) to beads priority (0-4)
        bd_priority = {1: 1, 2: 2, 3: 3}.get(priority, 2)
        args = ["create", title, "-p", str(bd_priority)]
        if description:
            args.extend(["-d", description])
        output = self._run_bd(*args)
        task_id = self._parse_create_output(output)
        return self.get(task_id) or Task(id=task_id, title=title, priority=priority)
    
    def wip(self, task_id: str, assignee: Optional[str] = None) -> Optional[Task]:
        """Mark task as work-in-progress."""
        self._run_bd("update", task_id, "--state", "in_progress")
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
            self._run_bd("delete", task_id, "-y")
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
            bd_priority = {1: 1, 2: 2, 3: 3}.get(kwargs["priority"], 2)
            args.extend(["-p", str(bd_priority)])
        if "status" in kwargs:
            state_map = {"open": "open", "wip": "in_progress", "closed": "closed"}
            args.extend(["--state", state_map.get(kwargs["status"], kwargs["status"])])
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
                self._run_bd("update", task.id, "--state", "open")
                count += 1
        return count
