"""BeadsClient - SQLite-backed task queue for CAO."""
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

DEFAULT_DB_PATH = Path.home() / ".beads-planning" / "beads.db"

@dataclass
class Task:
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
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    priority INTEGER DEFAULT 2,
                    status TEXT DEFAULT 'open',
                    assignee TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    closed_at TEXT,
                    tags TEXT DEFAULT '[]',
                    metadata TEXT DEFAULT '{}'
                )
            """)

    def _row_to_task(self, row) -> Task:
        return Task(*row)

    def list(self, status: Optional[str] = None, priority: Optional[int] = None) -> list[Task]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if priority:
            query += " AND priority = ?"
            params.append(priority)
        query += " ORDER BY priority ASC, created_at ASC"
        with sqlite3.connect(self.db_path) as conn:
            return [self._row_to_task(r) for r in conn.execute(query, params).fetchall()]

    def next(self, priority: Optional[int] = None) -> Optional[Task]:
        query = "SELECT * FROM tasks WHERE status = 'open'"
        params = []
        if priority:
            query += " AND priority = ?"
            params.append(priority)
        query += " ORDER BY priority ASC, created_at ASC LIMIT 1"
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(query, params).fetchone()
            return self._row_to_task(row) if row else None

    def get(self, task_id: str) -> Optional[Task]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._row_to_task(row) if row else None

    def add(self, title: str, description: str = "", priority: int = 2, tags: str = "[]") -> Task:
        task_id = str(uuid.uuid4())[:8]
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, description, priority, status, created_at, updated_at, tags) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
                (task_id, title, description, priority, now, now, tags)
            )
        return self.get(task_id)

    def wip(self, task_id: str, assignee: Optional[str] = None) -> Optional[Task]:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = 'wip', assignee = ?, updated_at = ? WHERE id = ?",
                (assignee, now, task_id)
            )
        return self.get(task_id)

    def close(self, task_id: str) -> Optional[Task]:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = 'closed', assignee = NULL, closed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, task_id)
            )
        return self.get(task_id)

    def delete(self, task_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cursor.rowcount > 0

    def update(self, task_id: str, **kwargs) -> Optional[Task]:
        allowed = {"title", "description", "priority", "status", "assignee", "tags", "metadata"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get(task_id)
        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", (*updates.values(), task_id))
        return self.get(task_id)
