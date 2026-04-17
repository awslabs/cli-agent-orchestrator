"""Memory service for CAO memory system (Phase 1 — file-based, no SQLite)."""

import fcntl
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.constants import MEMORY_BASE_DIR
from cli_agent_orchestrator.models.memory import Memory, MemoryScope, MemoryType

logger = logging.getLogger(__name__)


class MemoryService:
    """File-based memory service. All storage uses wiki markdown files and index.md."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or MEMORY_BASE_DIR

    # -------------------------------------------------------------------------
    # Scope resolution
    # -------------------------------------------------------------------------

    def resolve_scope_id(
        self,
        scope: str,
        terminal_context: Optional[dict] = None,
    ) -> Optional[str]:
        """Resolve scope_id from terminal context.

        global  → None
        project → SHA256[:12] of realpath(cwd)
        session → session_name
        agent   → agent_profile
        """
        if scope == MemoryScope.GLOBAL.value:
            return None

        ctx = terminal_context or {}

        if scope == MemoryScope.PROJECT.value:
            cwd = ctx.get("cwd") or ctx.get("working_directory")
            if not cwd:
                return None
            real = os.path.realpath(cwd)
            return hashlib.sha256(real.encode()).hexdigest()[:12]

        if scope == MemoryScope.SESSION.value:
            raw = ctx.get("session_name") or ctx.get("session")
            return self._sanitize_scope_id(raw) if raw else None

        if scope == MemoryScope.AGENT.value:
            raw = ctx.get("agent_profile")
            return self._sanitize_scope_id(raw) if raw else None

        return None

    @staticmethod
    def _sanitize_scope_id(value: str) -> str:
        """Sanitize a scope_id to prevent path traversal.

        Only allows alphanumeric, hyphens, and underscores.
        """
        sanitized = re.sub(r"[^a-zA-Z0-9\-_]", "", value)
        sanitized = re.sub(r"-+", "-", sanitized).strip("-_")
        return sanitized or "unknown"

    # -------------------------------------------------------------------------
    # Key generation
    # -------------------------------------------------------------------------

    @staticmethod
    def auto_generate_key(content: str) -> str:
        """Generate a slug key from the first 6 words of content.

        Lowercase, spaces→hyphens, strip punctuation, max 60 chars.
        """
        words = content.split()[:6]
        slug = "-".join(words).lower()
        slug = re.sub(r"[^a-z0-9\-]", "", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:60]

    @staticmethod
    def _sanitize_key(key: str) -> str:
        """Sanitize a user-provided key to prevent path traversal.

        Lowercase slugs only: [a-z0-9\\-], max 60 chars.
        Consistent with auto_generate_key() output format.
        """
        # Remove null bytes
        key = key.replace("\x00", "")
        # Strip directory components — only the basename matters
        key = os.path.basename(key)
        # Lowercase, then strip to safe slug characters only
        key = key.lower()
        key = re.sub(r"[^a-z0-9\-]", "", key)
        key = re.sub(r"-+", "-", key).strip("-")
        if not key:
            raise ValueError("Key is empty after sanitization")
        return key[:60]

    # -------------------------------------------------------------------------
    # Path helpers
    # -------------------------------------------------------------------------

    def _get_project_dir(self, scope: str, scope_id: Optional[str]) -> Path:
        """Get the project-level directory that holds the wiki/ dir."""
        if scope == MemoryScope.GLOBAL.value:
            return self.base_dir / "global"
        # For project/session/agent scopes, we need a project hash.
        # scope_id for project IS the project hash. For session/agent, we
        # use "global" as the project container (they can exist without a project).
        if scope == MemoryScope.PROJECT.value and scope_id:
            return self.base_dir / scope_id
        # session and agent scopes also live under a project dir when project context exists
        return self.base_dir / "global"

    def get_wiki_path(self, scope: str, scope_id: Optional[str], key: str) -> Path:
        """Get the path to a wiki topic file.

        Validates the resolved path stays within MEMORY_BASE_DIR to prevent path traversal.
        """
        project_dir = self._get_project_dir(scope, scope_id)
        wiki_path = (project_dir / "wiki" / scope / f"{key}.md").resolve()
        base_resolved = self.base_dir.resolve()
        if (
            not str(wiki_path).startswith(str(base_resolved) + os.sep)
            and wiki_path != base_resolved
        ):
            raise ValueError(
                f"Path traversal detected: resolved path escapes memory base directory"
            )
        return wiki_path

    def get_index_path(self, scope: str, scope_id: Optional[str]) -> Path:
        """Get the path to the index.md file."""
        project_dir = self._get_project_dir(scope, scope_id)
        return project_dir / "wiki" / "index.md"

    # -------------------------------------------------------------------------
    # Store
    # -------------------------------------------------------------------------

    async def store(
        self,
        content: str,
        scope: str = "project",
        memory_type: str = "project",
        key: Optional[str] = None,
        tags: str = "",
        terminal_context: Optional[dict] = None,
    ) -> Memory:
        """Store or update a memory. Upserts wiki file + index.md.

        Declared async for compatibility with async callers (MCP server, FastAPI).
        File I/O is synchronous; a future improvement would use aiofiles.
        """
        # Validate
        MemoryScope(scope)
        MemoryType(memory_type)

        scope_id = self.resolve_scope_id(scope, terminal_context)
        if key is None:
            key = self.auto_generate_key(content)
        else:
            key = self._sanitize_key(key)

        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        wiki_path = self.get_wiki_path(scope, scope_id, key)
        wiki_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if topic file already exists (upsert)
        is_update = wiki_path.exists()
        memory_id = str(uuid.uuid4())
        created_at = now

        if is_update:
            # Read existing file to get original created_at and id from comment
            existing_content = wiki_path.read_text(encoding="utf-8")
            # Append new timestamped entry
            new_content = existing_content.rstrip("\n") + f"\n\n## {timestamp}\n{content}\n"
            # Try to extract original id
            id_match = re.search(r"<!-- id: ([a-f0-9\-]+)", existing_content)
            if id_match:
                memory_id = id_match.group(1)
            # Extract original created_at
            ts_match = re.search(r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", existing_content)
            if ts_match:
                created_at = datetime.strptime(ts_match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
        else:
            new_content = (
                f"# {key}\n"
                f"<!-- id: {memory_id} | scope: {scope} | type: {memory_type} | tags: {tags} -->\n"
                f"\n## {timestamp}\n{content}\n"
            )

        # Atomic write: write to tmp then os.replace
        tmp_path = wiki_path.parent / f".{key}.tmp"
        tmp_path.write_text(new_content, encoding="utf-8")
        os.replace(str(tmp_path), str(wiki_path))

        # Update index.md
        action = "updated" if is_update else "created"
        self._update_index(scope, scope_id, key, memory_type, tags, content, timestamp, action)

        logger.info(f"Memory {action}: key={key} scope={scope} scope_id={scope_id}")

        source_provider = None
        source_terminal_id = None
        if terminal_context:
            source_provider = terminal_context.get("provider")
            source_terminal_id = terminal_context.get("terminal_id")

        return Memory(
            id=memory_id,
            key=key,
            memory_type=memory_type,
            scope=scope,
            scope_id=scope_id,
            file_path=str(wiki_path),
            tags=tags,
            source_provider=source_provider,
            source_terminal_id=source_terminal_id,
            created_at=created_at,
            updated_at=now,
            content=content,
        )

    # -------------------------------------------------------------------------
    # Index maintenance
    # -------------------------------------------------------------------------

    def _update_index(
        self,
        scope: str,
        scope_id: Optional[str],
        key: str,
        memory_type: str,
        tags: str,
        content: str,
        timestamp: str,
        action: str,
    ) -> None:
        """Update index.md with the memory entry.

        Uses fcntl.flock() to prevent concurrent writes from corrupting the index.
        """
        index_path = self.get_index_path(scope, scope_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)

        lock_path = index_path.parent / ".index.lock"

        # Acquire exclusive lock for the read-modify-write cycle
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # NOTE: lock_fd is always released/closed in the finally block below.

            est_tokens = int(len(content.split()) * 1.3)

            # Build the new entry line
            relative_path = f"{scope}/{key}.md"
            entry_line = (
                f"- [{key}]({relative_path}) — "
                f"type:{memory_type} tags:{tags} ~{est_tokens}tok updated:{timestamp}"
            )

            if index_path.exists():
                lines = index_path.read_text(encoding="utf-8").splitlines()
            else:
                lines = [
                    "# CAO Memory Index",
                    f"<!-- Updated: {timestamp} -->",
                    "",
                ]

            # Update the "Updated" timestamp in header
            for i, line in enumerate(lines):
                if line.startswith("<!-- Updated:"):
                    lines[i] = f"<!-- Updated: {timestamp} -->"
                    break

            # Find the scope section, or create it
            section_header = f"## {scope}"
            section_idx = None
            for i, line in enumerate(lines):
                if line.strip() == section_header:
                    section_idx = i
                    break

            if section_idx is None:
                # Add new section at end
                lines.append("")
                lines.append(section_header)
                section_idx = len(lines) - 1

            if action == "remove":
                # Remove existing entry for this key
                lines = [
                    ln for ln in lines if not (f"[{key}](" in ln and f"{scope}/{key}.md" in ln)
                ]
            else:
                # Remove existing entry for this key if present (for update)
                lines = [
                    ln for ln in lines if not (f"[{key}](" in ln and f"{scope}/{key}.md" in ln)
                ]
                # Re-find section after removal
                section_idx = None
                for i, line in enumerate(lines):
                    if line.strip() == section_header:
                        section_idx = i
                        break
                if section_idx is None:
                    lines.append("")
                    lines.append(section_header)
                    section_idx = len(lines) - 1

                # Insert entry after section header
                lines.insert(section_idx + 1, entry_line)

            # Atomic write
            new_content = "\n".join(lines) + "\n"
            tmp_path = index_path.parent / ".index.md.tmp"
            tmp_path.write_text(new_content, encoding="utf-8")
            os.replace(str(tmp_path), str(index_path))

        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                lock_fd.close()

    # -------------------------------------------------------------------------
    # Recall
    # -------------------------------------------------------------------------

    async def recall(
        self,
        query: Optional[str] = None,
        scope: Optional[str] = None,
        memory_type: Optional[str] = None,
        limit: int = 10,
        terminal_context: Optional[dict] = None,
        scan_all: bool = False,
    ) -> list[Memory]:
        """Recall memories matching query and filters. File-based search."""
        results: list[Memory] = []

        # Determine which project dirs to search
        search_dirs = self._get_search_dirs(scope, terminal_context, scan_all=scan_all)

        for project_dir in search_dirs:
            index_path = project_dir / "wiki" / "index.md"
            if not index_path.exists():
                continue

            entries = self._parse_index(index_path)

            for entry in entries:
                # Filter by scope
                if scope and entry["scope"] != scope:
                    continue
                # Filter by memory_type
                if memory_type and entry["memory_type"] != memory_type:
                    continue

                # Read the wiki file
                wiki_file = project_dir / "wiki" / entry["relative_path"]
                if not wiki_file.exists():
                    continue

                file_content = wiki_file.read_text(encoding="utf-8")

                # Query matching: check if query terms appear in content (case-insensitive)
                if query:
                    query_lower = query.lower()
                    terms = query_lower.split()
                    content_lower = file_content.lower()
                    if not all(term in content_lower for term in terms):
                        continue

                # Parse memory from file
                memory = self._parse_wiki_file(wiki_file, file_content, entry)
                if memory:
                    results.append(memory)

        # Sort by updated_at descending
        results.sort(key=lambda m: m.updated_at, reverse=True)

        # Apply scope precedence ordering when no scope filter
        if not scope:
            precedence = {
                MemoryScope.SESSION.value: 0,
                MemoryScope.PROJECT.value: 1,
                MemoryScope.GLOBAL.value: 2,
                MemoryScope.AGENT.value: 3,
            }
            results.sort(key=lambda m: (precedence.get(m.scope, 99), -m.updated_at.timestamp()))

        return results[:limit]

    def _get_search_dirs(
        self,
        scope: Optional[str],
        terminal_context: Optional[dict],
        scan_all: bool = False,
    ) -> list[Path]:
        """Determine which project directories to search."""
        dirs: list[Path] = []

        # Always include global
        global_dir = self.base_dir / "global"
        if global_dir.exists():
            dirs.append(global_dir)

        if scan_all:
            # Enumerate all project-hash dirs (for CLI use where user owns the filesystem)
            if self.base_dir.exists():
                for child in sorted(self.base_dir.iterdir()):
                    if child.is_dir() and child.name != "global" and child not in dirs:
                        dirs.append(child)
        elif terminal_context:
            # Include the specific project dir for this terminal's cwd
            project_scope_id = self.resolve_scope_id("project", terminal_context)
            if project_scope_id:
                project_dir = self.base_dir / project_scope_id
                if project_dir.exists() and project_dir not in dirs:
                    dirs.append(project_dir)
        # Without context and scan_all=False, only global is safe (agent/MCP context).

        return dirs

    def _parse_index(self, index_path: Path) -> list[dict]:
        """Parse index.md and return entry metadata."""
        entries: list[dict] = []
        content = index_path.read_text(encoding="utf-8")
        current_scope: Optional[str] = None

        for line in content.splitlines():
            # Detect scope section headers
            if line.startswith("## "):
                section = line[3:].strip()
                # Section might be just "global", "project", "session", "agent"
                for s in MemoryScope:
                    if section == s.value or section.startswith(s.value):
                        current_scope = s.value
                        break
                continue

            # Parse entry lines: - [key](scope/key.md) — type:X tags:Y ~Ntok updated:Z
            match = re.match(
                r"^- \[([^\]]+)\]\(([^)]+)\) — type:(\S+) tags:(\S*) ~\d+tok updated:(\S+)$",
                line,
            )
            if match and current_scope:
                entries.append(
                    {
                        "key": match.group(1),
                        "relative_path": match.group(2),
                        "memory_type": match.group(3),
                        "tags": match.group(4),
                        "updated_at": match.group(5),
                        "scope": current_scope,
                    }
                )

        return entries

    def _parse_wiki_file(self, wiki_file: Path, file_content: str, entry: dict) -> Optional[Memory]:
        """Parse a wiki topic file into a Memory object."""
        # Extract id from comment
        id_match = re.search(r"<!-- id: ([a-f0-9\-]+)", file_content)
        memory_id = id_match.group(1) if id_match else str(uuid.uuid4())

        # Extract tags from comment
        tags_match = re.search(r"tags: ([^-\n|]+?)(?:\s*-->|\s*\|)", file_content)
        tags = tags_match.group(1).strip() if tags_match else entry.get("tags", "")

        # Extract scope from comment
        scope_match = re.search(r"scope: (\S+)", file_content)
        scope = scope_match.group(1) if scope_match else entry.get("scope", "global")

        # Extract type from comment
        type_match = re.search(r"type: (\S+)", file_content)
        memory_type = type_match.group(1) if type_match else entry.get("memory_type", "project")

        # Clean up scope/type that may have trailing pipe
        scope = scope.rstrip(" |")
        memory_type = memory_type.rstrip(" |")

        # Extract all timestamped entries
        timestamps = re.findall(r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", file_content)
        if not timestamps:
            return None

        created_at = datetime.strptime(timestamps[0], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        updated_at = datetime.strptime(timestamps[-1], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

        # Extract content (everything after the last ## timestamp line)
        last_section = file_content.rsplit(f"## {timestamps[-1]}", 1)
        latest_content = last_section[-1].strip() if len(last_section) > 1 else ""

        return Memory(
            id=memory_id,
            key=entry["key"],
            memory_type=memory_type,
            scope=scope,
            scope_id=None,  # Not stored in file; resolved at query time
            file_path=str(wiki_file),
            tags=tags,
            source_provider=None,
            source_terminal_id=None,
            created_at=created_at,
            updated_at=updated_at,
            content=latest_content,
        )

    # -------------------------------------------------------------------------
    # Forget
    # -------------------------------------------------------------------------

    async def forget(
        self,
        key: str,
        scope: str = "project",
        terminal_context: Optional[dict] = None,
        scope_id: Optional[str] = None,
    ) -> bool:
        """Remove a memory. Deletes wiki file and updates index.md.

        If scope_id is provided directly it is used as-is (for cleanup).
        Otherwise it is resolved from terminal_context.
        """
        key = self._sanitize_key(key)
        if scope_id is None:
            scope_id = self.resolve_scope_id(scope, terminal_context)
        wiki_path = self.get_wiki_path(scope, scope_id, key)

        if not wiki_path.exists():
            return False

        # Delete the wiki file
        wiki_path.unlink()
        logger.info(f"Deleted memory file: {wiki_path}")

        # Update index.md
        self._update_index(scope, scope_id, key, "", "", "", "", "remove")

        return True

    # -------------------------------------------------------------------------
    # Context for terminal injection
    # -------------------------------------------------------------------------

    def get_memory_context_for_terminal(
        self,
        terminal_id: str,
        budget_chars: int = 3000,
    ) -> str:
        """Build the memory context block for terminal injection.

        Scope precedence: session > project > global.
        Fills up to budget_chars, dropping oldest entries first if over budget.
        """
        # We need terminal context to resolve scopes. Import here to avoid circular imports.
        terminal_context = self._get_terminal_context(terminal_id)
        if not terminal_context:
            return ""

        # Collect memories in precedence order
        all_memories: list[Memory] = []
        scopes_in_order = [
            MemoryScope.SESSION.value,
            MemoryScope.PROJECT.value,
            MemoryScope.GLOBAL.value,
        ]

        for scope_val in scopes_in_order:
            scope_id = self.resolve_scope_id(scope_val, terminal_context)
            project_dir = self._get_project_dir(scope_val, scope_id)
            index_path = project_dir / "wiki" / "index.md"

            if not index_path.exists():
                continue

            entries = self._parse_index(index_path)
            for entry in entries:
                if entry["scope"] != scope_val:
                    continue
                wiki_file = project_dir / "wiki" / entry["relative_path"]
                if not wiki_file.exists():
                    continue
                file_content = wiki_file.read_text(encoding="utf-8")
                memory = self._parse_wiki_file(wiki_file, file_content, entry)
                if memory:
                    all_memories.append(memory)

        if not all_memories:
            return ""

        # Build context block within budget
        lines: list[str] = []
        used_chars = 0

        for mem in all_memories:
            line = f"- [{mem.scope}] {mem.key}: {mem.content}"
            line_len = len(line)
            if used_chars + line_len > budget_chars:
                break
            lines.append(line)
            used_chars += line_len

        if not lines:
            return ""

        context = "## Context from CAO Memory\n" + "\n".join(lines)
        return f"<cao-memory>\n{context}\n</cao-memory>"

    def _get_terminal_context(self, terminal_id: str) -> Optional[dict]:
        """Get terminal context for scope resolution.

        Reads from the database via terminal service.
        Returns None if terminal not found.
        """
        try:
            from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

            with SessionLocal() as db:
                terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
                if not terminal:
                    return None
                return {
                    "terminal_id": terminal.id,
                    "session_name": terminal.tmux_session,
                    "provider": terminal.provider,
                    "agent_profile": terminal.agent_profile,
                    "cwd": None,  # working_directory not in TerminalModel; resolved dynamically via tmux
                }
        except Exception as e:
            logger.warning(f"Could not get terminal context for {terminal_id}: {e}")
            return None
