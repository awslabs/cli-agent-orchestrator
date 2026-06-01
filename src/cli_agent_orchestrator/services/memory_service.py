"""Memory service for CAO memory system (Phase 2 — wiki + SQLite metadata)."""

import fcntl
import hashlib
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.constants import (
    MEMORY_BASE_DIR,
    MEMORY_MAX_PER_SCOPE,
    MEMORY_SCOPE_BUDGET_CHARS,
)
from cli_agent_orchestrator.models.memory import Memory, MemoryScope, MemoryType

logger = logging.getLogger(__name__)

VALID_SEARCH_MODES = ("metadata", "bm25", "hybrid")


MEMORY_DISABLED_MESSAGE = (
    "memory subsystem is disabled. Set memory.enabled=true in settings.json " "to re-enable."
)


class MemoryDisabledError(RuntimeError):
    """Raised when a write entry point is called while memory is disabled.

    Read paths (recall, get_memory_context_for_terminal) instead return an
    empty result, since silent empty reads are a safer no-op than raising.
    """


def _is_memory_enabled() -> bool:
    """Module-level guard for memory entry points.

    Imported lazily to avoid a settings → memory_service circular import at
    module load time. Defaults to True if the import or read fails so a
    broken settings file never silently disables memory.
    """
    try:
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        return is_memory_enabled()
    except Exception:
        return True


# Per-curator dispatch locks. A worker that fails to acquire its session's
# curator lock falls back to Phase 1 rather than queueing — context injection
# is best-effort and must never block the worker.
_curator_locks: dict[str, threading.Lock] = {}


# -----------------------------------------------------------------------------
# Phase 2.5 U6 — Module-level project identity resolver
#
# Exposed at module scope (not on MemoryService) so non-service callers can
# reuse the same precedence chain without instantiating a MemoryService.
# -----------------------------------------------------------------------------

_PROJECT_ID_OVERRIDE_PATTERN = re.compile(r"^[a-zA-Z0-9._\-]{1,128}$")


class ProjectIdentityResolutionError(RuntimeError):
    """Raised when no project identity can be derived from any source."""


def _validate_project_id_override(raw: str) -> str:
    """Validate an explicit ``project_id`` override; raise on reject.

    Rejects rather than sanitizes — silent sanitization of an explicit
    user-supplied config value hides typos and buries the contract.
    """
    if "\x00" in raw:
        raise ValueError("project_id override contains null byte")
    if not _PROJECT_ID_OVERRIDE_PATTERN.match(raw):
        raise ValueError(
            "project_id override must match ^[a-zA-Z0-9._\\-]{1,128}$; " f"got {raw!r}"
        )
    return raw


def _read_project_id_override() -> Optional[str]:
    """Read and validate the explicit ``project_id`` override.

    Precedence: ``CAO_PROJECT_ID`` env → ``memory.project_id`` settings key.
    """
    raw: Optional[str] = os.environ.get("CAO_PROJECT_ID")
    if not raw:
        try:
            from cli_agent_orchestrator.services.settings_service import (
                get_memory_settings,
            )

            raw = get_memory_settings().get("project_id")
        except Exception as e:
            logger.debug(f"Failed to read project_id override, skipping: {e}")
            raw = None
    if not raw:
        return None
    return _validate_project_id_override(raw)


def _normalize_git_remote(url: str) -> str:
    """Normalize a git remote URL into a stable slug id.

    Rules: lowercase, strip protocol, strip auth, SCP→host/path, strip
    trailing ``.git``, collapse non-alnum runs to ``-``. Empty input → ``"unknown"``.
    """
    if not url:
        return "unknown"
    u = url.strip().lower()
    for proto in ("git+ssh://", "ssh://", "git://", "https://", "http://"):
        if u.startswith(proto):
            u = u[len(proto) :]
            break
    if "@" in u:
        u = u.split("@", 1)[1]
    if ":" in u:
        head, _, tail = u.partition(":")
        if "/" not in head:
            u = f"{head}/{tail}"
    if u.endswith(".git"):
        u = u[:-4]
    u = re.sub(r"[^a-z0-9]+", "-", u).strip("-")
    return u or "unknown"


def _git_remote_identity(cwd: Path) -> Optional[str]:
    """Return ``remote.origin.url`` for ``cwd``, or ``None`` when absent.

    ``timeout=2`` keeps laggy NFS from blocking the resolver.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"git remote lookup failed in {cwd}: {e}")
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _record_alias_safe(project_id: str, alias: str, kind: str) -> None:
    """Opportunistically record an alias row; swallow DB errors.

    The alias table is a nice-to-have for future migration; a DB hiccup must
    never block identity resolution.
    """
    if not project_id or not alias or project_id == alias:
        return
    try:
        from cli_agent_orchestrator.clients.database import record_project_alias

        record_project_alias(project_id, alias, kind)
    except Exception as e:
        logger.debug(f"record_project_alias failed (non-fatal): {e}")


def resolve_project_id(cwd: Optional[Path]) -> str:
    """Resolve canonical project identity via the U6 precedence chain.

    Precedence:
        1. explicit override (``CAO_PROJECT_ID`` env → ``memory.project_id`` settings)
        2. ``git config --get remote.origin.url`` (normalized)
        3. ``sha256(realpath(cwd))[:12]`` — Phase 2 parity fallback

    Sources 1 and 2 opportunistically record the current ``cwd_hash`` into
    ``ProjectAliasModel`` (kind=``cwd_hash``) so legacy directories stay
    recallable. The raw git remote URL is never persisted — it can embed
    credentials, and the auth-stripped ``canonical`` id covers identity.
    Alias writes never block.

    Raises ``ProjectIdentityResolutionError`` when all three sources fail.
    """
    cwd_hash: Optional[str] = None
    if cwd is not None:
        try:
            cwd_hash = hashlib.sha256(os.path.realpath(str(cwd)).encode()).hexdigest()[:12]
        except Exception as e:
            logger.debug(f"cwd-hash derivation failed for {cwd}: {e}")

    override = _read_project_id_override()
    if override:
        if cwd_hash and override != cwd_hash:
            _record_alias_safe(override, cwd_hash, "cwd_hash")
        return override

    if cwd is not None:
        remote_url = _git_remote_identity(cwd)
        if remote_url:
            canonical = _normalize_git_remote(remote_url)
            if cwd_hash and canonical != cwd_hash:
                _record_alias_safe(canonical, cwd_hash, "cwd_hash")
            # Deliberately do NOT persist the raw remote URL as an alias: git
            # remotes can embed credentials (https://user:token@host/...), and
            # nothing reads a ``git_remote`` row back — the cwd-hash alias
            # already covers legacy-dir lookup. ``canonical`` is auth-stripped
            # by ``_normalize_git_remote``, so the returned id is safe.
            return canonical

    if cwd_hash:
        return cwd_hash

    raise ProjectIdentityResolutionError(
        "Cannot resolve project identity: no override, no git remote, and no cwd provided"
    )


class MemoryService:
    """Memory service backed by wiki markdown files and SQLite metadata.

    Wiki files remain the content store. SQLite mirrors metadata
    (key, scope, type, tags, file_path, timestamps) for fast filtered
    lookup. ``index.md`` is regenerated as a human-readable view.
    """

    def __init__(self, base_dir: Optional[Path] = None, db_engine: Any = None):
        self.base_dir = base_dir or MEMORY_BASE_DIR
        self._db_engine = db_engine
        self._db_session_factory: Any = None
        if db_engine is not None:
            from sqlalchemy.orm import sessionmaker

            self._db_session_factory = sessionmaker(
                autocommit=False, autoflush=False, bind=db_engine
            )

    # -------------------------------------------------------------------------
    # SQLite metadata operations
    # -------------------------------------------------------------------------

    def _get_db_session(self) -> Any:
        """Get a SQLAlchemy session — uses test engine if provided, else global."""
        if self._db_session_factory:
            return self._db_session_factory()
        from cli_agent_orchestrator.clients.database import SessionLocal

        return SessionLocal()

    def _upsert_metadata(
        self,
        key: str,
        memory_type: str,
        scope: str,
        scope_id: Optional[str],
        file_path: str,
        tags: str,
        source_provider: Optional[str],
        source_terminal_id: Optional[str],
        token_estimate: Optional[int],
    ) -> None:
        """Insert or update the metadata row for (key, scope, scope_id).

        Symmetric upsert: every field set on insert is also set on update —
        ``memory_type``, ``tags``, ``file_path``, ``source_provider``,
        ``source_terminal_id``, ``token_estimate``, ``updated_at``.
        """
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        with self._get_db_session() as db:
            existing = (
                db.query(MemoryMetadataModel)
                .filter(
                    MemoryMetadataModel.key == key,
                    MemoryMetadataModel.scope == scope,
                    (
                        MemoryMetadataModel.scope_id == scope_id
                        if scope_id is not None
                        else MemoryMetadataModel.scope_id.is_(None)
                    ),
                )
                .first()
            )
            if existing:
                existing.memory_type = memory_type
                existing.tags = tags
                existing.file_path = file_path
                existing.source_provider = source_provider
                existing.source_terminal_id = source_terminal_id
                existing.token_estimate = token_estimate
                existing.updated_at = datetime.now(timezone.utc)
                db.commit()
            else:
                row = MemoryMetadataModel(
                    id=str(uuid.uuid4()),
                    key=key,
                    memory_type=memory_type,
                    scope=scope,
                    scope_id=scope_id,
                    file_path=file_path,
                    tags=tags,
                    source_provider=source_provider,
                    source_terminal_id=source_terminal_id,
                    token_estimate=token_estimate,
                )
                db.add(row)
                db.commit()

    def _delete_metadata(self, key: str, scope: str, scope_id: Optional[str]) -> bool:
        """Delete the metadata row for (key, scope, scope_id). Returns True if removed."""
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        with self._get_db_session() as db:
            q = db.query(MemoryMetadataModel).filter(
                MemoryMetadataModel.key == key,
                MemoryMetadataModel.scope == scope,
            )
            if scope_id is not None:
                q = q.filter(MemoryMetadataModel.scope_id == scope_id)
            else:
                q = q.filter(MemoryMetadataModel.scope_id.is_(None))
            deleted: int = q.delete()
            db.commit()
            return deleted > 0

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
            cwd_path = Path(cwd) if cwd else None
            try:
                return resolve_project_id(cwd_path)
            except ProjectIdentityResolutionError:
                return None

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
    # Storage migration (Phase 2.5 U6)
    # -------------------------------------------------------------------------

    def plan_project_dir_migration(self, canonical_id: str, alias: str) -> dict:
        """Describe (without mutating) a migration from ``alias/`` to ``canonical_id/``.

        Returns a dict with ``dry_run`` (always True), ``canonical_id``, ``alias``,
        ``source_exists``, ``destination_exists``, ``action`` (``"none"``,
        ``"rename"``, ``"merge"``, ``"conflict"``), and ``files``.
        """
        source = self._get_project_dir(MemoryScope.PROJECT.value, alias)
        dest = self._get_project_dir(MemoryScope.PROJECT.value, canonical_id)
        source_exists = source.exists() and source.is_dir()
        dest_exists = dest.exists() and dest.is_dir()
        files: list[str] = []
        if source_exists:
            for p in sorted(source.rglob("*")):
                if p.is_file():
                    try:
                        files.append(str(p.relative_to(source)))
                    except ValueError:
                        continue
        if not source_exists:
            action = "none"
        elif canonical_id == alias:
            action = "none"
        elif not dest_exists:
            action = "rename"
        elif files:
            action = "merge"
        else:
            action = "conflict"
        return {
            "dry_run": True,
            "canonical_id": canonical_id,
            "alias": alias,
            "source_exists": source_exists,
            "destination_exists": dest_exists,
            "action": action,
            "files": files,
        }

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
        # ``project`` scope uses the resolved cwd-hash as its container.
        # ``session`` and ``agent`` always live under the ``global``
        # container in Phase 1 — their scope_id is nested into the
        # wiki path (see get_wiki_path) for isolation, not into the
        # container directory.
        if scope == MemoryScope.PROJECT.value and scope_id:
            return self.base_dir / scope_id
        return self.base_dir / "global"

    def get_wiki_path(self, scope: str, scope_id: Optional[str], key: str) -> Path:
        """Get the path to a wiki topic file.

        For session and agent scopes, ``scope_id`` is nested into the
        path so that two sessions (or two agent profiles) with the same
        key do not collide on disk.

        Validates the resolved path stays within MEMORY_BASE_DIR to
        prevent path traversal.
        """
        project_dir = self._get_project_dir(scope, scope_id)
        if scope in (MemoryScope.SESSION.value, MemoryScope.AGENT.value) and scope_id:
            wiki_path = (project_dir / "wiki" / scope / scope_id / f"{key}.md").resolve()
        else:
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

        Raises ``MemoryDisabledError`` when ``memory.enabled`` is False
        (U5 / SC-6) — no filesystem or SQLite writes happen.
        """
        if not _is_memory_enabled():
            raise MemoryDisabledError(MEMORY_DISABLED_MESSAGE)

        # Validate
        MemoryScope(scope)
        MemoryType(memory_type)

        scope_id = self.resolve_scope_id(scope, terminal_context)
        # Non-global scoped memories require a resolvable scope_id for
        # on-disk isolation. Without it, writes could collapse into a
        # shared scope directory and leak memories across projects,
        # sessions, or agents.
        if scope == MemoryScope.PROJECT.value and scope_id is None:
            raise ValueError(
                "Cannot store project-scoped memory without a working "
                "directory. Pass terminal_context with 'cwd' set."
            )
        if scope == MemoryScope.SESSION.value and scope_id is None:
            raise ValueError(
                "Cannot store session-scoped memory without a session "
                "identifier. Pass terminal_context with 'session_name' "
                "or 'session' set."
            )
        if scope == MemoryScope.AGENT.value and scope_id is None:
            raise ValueError(
                "Cannot store agent-scoped memory without an agent "
                "profile. Pass terminal_context with 'agent_profile' set."
            )
        if key is None:
            key = self.auto_generate_key(content)
        else:
            key = self._sanitize_key(key)

        # Normalize tags: strip whitespace and rejoin with commas. The
        # index entry format expects tags to contain no spaces (the
        # parser uses ``tags:(\S*)``), so ``"ci, deploy"`` would
        # otherwise become unrecallable through the index.
        tags = ",".join(t.strip() for t in tags.split(",") if t.strip())

        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        wiki_path = self.get_wiki_path(scope, scope_id, key)
        wiki_path.parent.mkdir(parents=True, exist_ok=True)

        # Per-topic lock around the read-modify-write cycle. Without
        # this, two concurrent store() calls for the same
        # (scope, scope_id, key) can both read the old content and
        # then overwrite each other, losing one update. Mirrors the
        # .index.lock pattern in _update_index.
        topic_lock_path = wiki_path.parent / f".{key}.lock"
        topic_lock_fd = open(topic_lock_path, "w")
        try:
            fcntl.flock(topic_lock_fd, fcntl.LOCK_EX)

            # Check if topic file already exists (upsert)
            is_update = wiki_path.exists()
            memory_id = str(uuid.uuid4())
            created_at = now

            if is_update:
                # Read existing file to get original created_at and id from comment
                existing_content = wiki_path.read_text(encoding="utf-8")
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
                # Rewrite the header line so updated memory_type/tags stay
                # in sync with index.md (recall() reads the file header).
                new_header = (
                    f"<!-- id: {memory_id} | scope: {scope} | "
                    f"type: {memory_type} | tags: {tags} -->"
                )
                existing_content = re.sub(
                    r"<!-- id: [a-f0-9\-]+ \| scope: [^|]+ \| type: [^|]+ \| tags: [^>]*-->",
                    new_header,
                    existing_content,
                    count=1,
                )
                # Append new timestamped entry
                new_content = existing_content.rstrip("\n") + f"\n\n## {timestamp}\n{content}\n"
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

            # Mirror metadata into SQLite. Token estimate is char-based
            # (len(content) / 4) — a coarse proxy used by the context-budget
            # planner; it differs from the word-based estimate written into
            # index.md, which is purely a human-readable hint.
            source_provider_in_ctx: Optional[str] = None
            source_terminal_id_in_ctx: Optional[str] = None
            if terminal_context:
                source_provider_in_ctx = terminal_context.get("provider")
                source_terminal_id_in_ctx = terminal_context.get("terminal_id")
            try:
                self._upsert_metadata(
                    key=key,
                    memory_type=memory_type,
                    scope=scope,
                    scope_id=scope_id,
                    file_path=str(wiki_path),
                    tags=tags,
                    source_provider=source_provider_in_ctx,
                    source_terminal_id=source_terminal_id_in_ctx,
                    token_estimate=len(content) // 4,
                )
            except Exception as e:
                logger.warning(f"Memory metadata SQLite upsert failed (key={key}): {e}")

            logger.info(f"Memory {action}: key={key} scope={scope} scope_id={scope_id}")
        finally:
            try:
                fcntl.flock(topic_lock_fd, fcntl.LOCK_UN)
            finally:
                topic_lock_fd.close()

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
            action=action,
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

            # Build the new entry line. Session and agent scopes nest
            # scope_id into the path so different sessions/agents do
            # not collide on the same key.
            if scope in (MemoryScope.SESSION.value, MemoryScope.AGENT.value) and scope_id:
                relative_path = f"{scope}/{scope_id}/{key}.md"
            else:
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
                lines = [ln for ln in lines if not (f"[{key}](" in ln and f"{relative_path}" in ln)]
            else:
                # Remove existing entry for this key if present (for update)
                lines = [ln for ln in lines if not (f"[{key}](" in ln and f"{relative_path}" in ln)]
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
        search_mode: str = "hybrid",
    ) -> list[Memory]:
        """Recall memories matching query and filters.

        ``search_mode``:
          - ``metadata``: substring match against key/tags/content via index.md walk.
          - ``bm25``: BM25 ranking over wiki bodies (content-aware).
          - ``hybrid``: metadata results first, then BM25 fills with what metadata missed.

        Returns ``[]`` when ``memory.enabled`` is False (U5 / SC-6).
        """
        if not _is_memory_enabled():
            return []

        if search_mode not in VALID_SEARCH_MODES:
            raise ValueError(
                f"Invalid search_mode {search_mode!r}; expected one of {VALID_SEARCH_MODES}"
            )

        if search_mode == "bm25":
            if not query:
                return []
            scope_id = (
                self.resolve_scope_id(scope, terminal_context)
                if scope and scope != MemoryScope.GLOBAL.value and terminal_context
                else None
            )
            return self._bm25_search(
                query=query,
                scope=scope,
                scope_id=scope_id,
                memory_type=memory_type,
                limit=limit,
                exclude_keys=set(),
                terminal_context=terminal_context,
                scan_all=scan_all,
            )

        metadata_results = await self._metadata_recall(
            query=query,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
            terminal_context=terminal_context,
            scan_all=scan_all,
        )

        if search_mode == "metadata" or not query:
            return metadata_results

        # hybrid: top up with BM25 hits not already in metadata results
        exclude_keys = {m.key for m in metadata_results}
        remaining = max(0, limit - len(metadata_results))
        if remaining == 0:
            return metadata_results

        scope_id = (
            self.resolve_scope_id(scope, terminal_context)
            if scope and scope != MemoryScope.GLOBAL.value and terminal_context
            else None
        )
        bm25_results = self._bm25_search(
            query=query,
            scope=scope,
            scope_id=scope_id,
            memory_type=memory_type,
            limit=remaining,
            exclude_keys=exclude_keys,
            terminal_context=terminal_context,
            scan_all=scan_all,
        )
        return metadata_results + bm25_results

    async def _metadata_recall(
        self,
        query: Optional[str] = None,
        scope: Optional[str] = None,
        memory_type: Optional[str] = None,
        limit: int = 10,
        terminal_context: Optional[dict] = None,
        scan_all: bool = False,
    ) -> list[Memory]:
        """Substring-match recall via index.md walk (Phase 1 path)."""
        results: list[Memory] = []

        # Determine which project dirs to search
        search_dirs = self._get_search_dirs(scope, terminal_context, scan_all=scan_all)

        # For session/agent scopes, all entries share the global
        # index. If the caller passes a terminal_context that resolves
        # to a scope_id, narrow the result set to memories for THAT
        # session/agent — otherwise a recall would leak memories
        # across sessions or agents that happen to share keys.
        # ``scan_all`` (CLI inspection) and missing context still see
        # every entry.
        scope_id_filter: Optional[str] = None
        if (
            scope in (MemoryScope.SESSION.value, MemoryScope.AGENT.value)
            and not scan_all
            and terminal_context
        ):
            scope_id_filter = self.resolve_scope_id(scope, terminal_context)

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
                # Filter session/agent entries by scope_id when caller
                # has a relevant context (see scope_id_filter above).
                if scope_id_filter and entry.get("scope_id") != scope_id_filter:
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

    # -------------------------------------------------------------------------
    # BM25 fallback search
    # -------------------------------------------------------------------------

    @staticmethod
    def _bm25_tokenize(text: str) -> list[str]:
        """Lowercase, split on non-alphanumeric, drop empties."""
        return [t for t in re.split(r"[^a-zA-Z0-9]+", text.lower()) if t]

    def _bm25_search(
        self,
        query: str,
        scope: Optional[str],
        scope_id: Optional[str],
        memory_type: Optional[str],
        limit: int,
        exclude_keys: set,
        terminal_context: Optional[dict],
        scan_all: bool,
    ) -> list[Memory]:
        """Rank wiki bodies by BM25 against ``query``.

        Returns ``[]`` (and logs at debug) if ``rank_bm25`` is unavailable —
        callers must continue gracefully without it.
        """
        try:
            from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("rank_bm25 not installed; BM25 search disabled")
            return []

        query_tokens = self._bm25_tokenize(query)
        if not query_tokens:
            return []

        search_dirs = self._get_search_dirs(scope, terminal_context, scan_all=scan_all)

        candidates: list[tuple[Path, dict]] = []
        seen: set[Path] = set()
        for project_dir in search_dirs:
            wiki_root = project_dir / "wiki"
            if not wiki_root.exists():
                continue
            for wiki_file in wiki_root.rglob("*.md"):
                if wiki_file.name == "index.md" or wiki_file in seen:
                    continue
                seen.add(wiki_file)
                rel_parts = wiki_file.relative_to(wiki_root).parts
                if not rel_parts:
                    continue
                file_scope = rel_parts[0]
                entry_scope_id: Optional[str] = None
                if (
                    file_scope
                    in (
                        MemoryScope.SESSION.value,
                        MemoryScope.AGENT.value,
                    )
                    and len(rel_parts) >= 3
                ):
                    entry_scope_id = rel_parts[1]

                if scope and file_scope != scope:
                    continue
                if scope_id and entry_scope_id != scope_id:
                    continue
                key = wiki_file.stem
                if key in exclude_keys:
                    continue

                # Peek at memory_type from header comment for filtering
                if memory_type:
                    head = wiki_file.read_text(encoding="utf-8")[:512]
                    type_match = re.search(r"type: (\S+)", head)
                    file_type = type_match.group(1).rstrip(" |") if type_match else ""
                    if file_type != memory_type:
                        continue

                entry = {
                    "key": key,
                    "scope": file_scope,
                    "scope_id": entry_scope_id,
                    "memory_type": "",
                    "tags": "",
                    "relative_path": "/".join(rel_parts),
                }
                candidates.append((wiki_file, entry))

        if not candidates:
            return []

        corpus_tokens: list[list[str]] = []
        contents: list[str] = []
        for wiki_file, _entry in candidates:
            text = wiki_file.read_text(encoding="utf-8")
            contents.append(text)
            corpus_tokens.append(self._bm25_tokenize(text))

        bm25 = BM25Okapi(corpus_tokens)
        scores = bm25.get_scores(query_tokens)

        # BM25 IDF can go negative on tiny corpora when df > N/2, so we cannot
        # gate on score > 0 alone. A document only counts as a match when at
        # least one query token actually appears in it; ranking among matches
        # then uses the BM25 score directly.
        query_token_set = set(query_tokens)
        ranked = sorted(
            (
                (scores[i], i)
                for i in range(len(candidates))
                if query_token_set & set(corpus_tokens[i])
            ),
            key=lambda x: x[0],
            reverse=True,
        )[:limit]

        results: list[Memory] = []
        for _score, idx in ranked:
            wiki_file, entry = candidates[idx]
            memory = self._parse_wiki_file(wiki_file, contents[idx], entry)
            if memory:
                results.append(memory)
        return results

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
                # Also include legacy cwd-hash dirs recorded as aliases so
                # pre-U6 memories survive the canonical-id transition.
                try:
                    from cli_agent_orchestrator.clients.database import (
                        list_aliases_for_project,
                    )

                    for alias in list_aliases_for_project(project_scope_id):
                        if alias.get("kind") != "cwd_hash":
                            continue
                        alias_dir = self.base_dir / alias["alias"]
                        if alias_dir.exists() and alias_dir not in dirs:
                            dirs.append(alias_dir)
                except Exception as e:
                    logger.debug(f"alias dir enumeration failed: {e}")
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
                relative_path = match.group(2)
                # Session/agent entries embed scope_id in the path
                # (e.g. ``session/<scope_id>/<key>.md``). Extract it
                # here so callers (CLI clear, recall→forget) can
                # target the right file without reconstructing the
                # original terminal_context.
                entry_scope_id: Optional[str] = None
                path_parts = relative_path.split("/")
                if len(path_parts) >= 3 and path_parts[0] in (
                    MemoryScope.SESSION.value,
                    MemoryScope.AGENT.value,
                ):
                    entry_scope_id = path_parts[1]

                entries.append(
                    {
                        "key": match.group(1),
                        "relative_path": relative_path,
                        "memory_type": match.group(3),
                        "tags": match.group(4),
                        "updated_at": match.group(5),
                        "scope": current_scope,
                        "scope_id": entry_scope_id,
                    }
                )

        return entries

    def _parse_wiki_file(self, wiki_file: Path, file_content: str, entry: dict) -> Optional[Memory]:
        """Parse a wiki topic file into a Memory object."""
        # Extract id from comment
        id_match = re.search(r"<!-- id: ([a-f0-9\-]+)", file_content)
        memory_id = id_match.group(1) if id_match else str(uuid.uuid4())

        # Extract tags from comment
        tags_match = re.search(r"tags: ([^\n|]*?)(?:\s*-->|\s*\|)", file_content)
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
            scope_id=entry.get("scope_id"),
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

        Raises ``MemoryDisabledError`` when ``memory.enabled`` is False
        (U5 / SC-6) — no filesystem or SQLite writes happen.
        """
        if not _is_memory_enabled():
            raise MemoryDisabledError(MEMORY_DISABLED_MESSAGE)

        key = self._sanitize_key(key)
        if scope_id is None:
            scope_id = self.resolve_scope_id(scope, terminal_context)
        wiki_path = self.get_wiki_path(scope, scope_id, key)

        if not wiki_path.exists():
            # Drop any stale SQLite row so metadata stays consistent
            # with the wiki even when the file vanished out-of-band.
            try:
                self._delete_metadata(key, scope, scope_id)
            except Exception as e:
                logger.warning(f"Memory metadata SQLite delete failed (key={key}): {e}")
            return False

        # Delete the wiki file
        wiki_path.unlink()
        logger.info(f"Deleted memory file: {wiki_path}")

        # Update index.md. Pass the current timestamp so the index header
        # reflects the time of the most recent change (a delete is a
        # change too).
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._update_index(scope, scope_id, key, "", "", "", now_ts, "remove")

        # Drop the SQLite metadata row alongside the file.
        try:
            self._delete_metadata(key, scope, scope_id)
        except Exception as e:
            logger.warning(f"Memory metadata SQLite delete failed (key={key}): {e}")

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

        Scope precedence: session > project > global (preserved in output).

        Each scope is independently capped at ``MEMORY_MAX_PER_SCOPE`` entries
        and at ``min(MEMORY_SCOPE_BUDGET_CHARS, budget_chars // n_scopes)``
        characters. Unused budget from an empty scope is NOT reallocated to
        other scopes (keeps cache-friendly scope boundaries intact).

        Returns ``""`` when ``memory.enabled`` is False (U5 / SC-6) — never
        reads index.md or wiki files.
        """
        if not _is_memory_enabled():
            return ""

        terminal_context = self._get_terminal_context(terminal_id)
        if not terminal_context:
            return ""

        scopes_in_order = [
            MemoryScope.SESSION.value,
            MemoryScope.PROJECT.value,
            MemoryScope.GLOBAL.value,
        ]

        scope_char_cap = min(
            MEMORY_SCOPE_BUDGET_CHARS,
            max(0, budget_chars // len(scopes_in_order)),
        )

        lines: list[str] = []

        for scope_val in scopes_in_order:
            scope_id = self.resolve_scope_id(scope_val, terminal_context)
            project_dir = self._get_project_dir(scope_val, scope_id)
            wiki_dir = project_dir / "wiki"
            wiki_resolved = wiki_dir.resolve()
            index_path = wiki_dir / "index.md"
            if not index_path.exists():
                continue

            scope_entries = []
            for e in self._parse_index(index_path):
                if e["scope"] != scope_val:
                    continue
                # Session/agent entries embed scope_id in the wiki path and
                # share index.md with global, so the scope_id must match the
                # caller's. Project entries already live in a per-project
                # directory and global has no scope_id by design.
                if scope_val in (MemoryScope.SESSION.value, MemoryScope.AGENT.value):
                    if e.get("scope_id") != scope_id:
                        continue
                scope_entries.append(e)
            scope_entries.sort(key=lambda e: e.get("updated_at", ""), reverse=True)

            scope_memories: list[Memory] = []
            for entry in scope_entries:
                if len(scope_memories) >= MEMORY_MAX_PER_SCOPE:
                    break
                wiki_file = wiki_dir / entry["relative_path"]
                resolved_wiki = wiki_file.resolve()
                # Guard against a crafted/corrupted index entry (e.g.
                # ``../<other-project>/wiki/...``) escaping this scope's wiki
                # directory and leaking another project's memory. Validate
                # against the per-scope wiki dir, not the global memory base.
                if not str(resolved_wiki).startswith(str(wiki_resolved) + os.sep):
                    logger.warning(
                        f"Path traversal in index entry rejected: {entry.get('relative_path')}"
                    )
                    continue
                if not resolved_wiki.exists():
                    continue
                file_content = resolved_wiki.read_text(encoding="utf-8")
                memory = self._parse_wiki_file(resolved_wiki, file_content, entry)
                if memory:
                    scope_memories.append(memory)

            scope_used_chars = 0
            for mem in scope_memories:
                line = f"- [{mem.scope}] {mem.key}: {mem.content}"
                line_len = len(line) + 1
                if scope_used_chars + line_len > scope_char_cap:
                    break
                lines.append(line)
                scope_used_chars += line_len

        if not lines:
            return ""

        context = "## Context from CAO Memory\n" + "\n".join(lines)
        return f"<cao-memory>\n{context}\n</cao-memory>"

    # -------------------------------------------------------------------------
    # U9 — Curated injection via context-manager agent
    # -------------------------------------------------------------------------

    def _find_context_manager_terminal(self, session_name: Optional[str]) -> Optional[dict]:
        """Find an IDLE memory_manager terminal in ``session_name``.

        Sessions are isolated: a worker in session A must never dispatch to a
        curator in session B (same bug class as the scope_id leak).
        """
        if not session_name:
            return None
        try:
            from cli_agent_orchestrator.clients.database import list_all_terminals

            for t in list_all_terminals():
                if (
                    t.get("agent_profile") == "memory_manager"
                    and t.get("session_name") == session_name
                ):
                    return t
        except Exception as e:
            logger.debug(f"_find_context_manager_terminal failed: {e}")
        return None

    def get_curated_memory_context(
        self, terminal_id: str, task_description: Optional[str] = None
    ) -> str:
        """Curated injection path with Phase 1 fallback.

        If a memory_manager terminal in the same session is IDLE, dispatch the
        task description and read back its ``<cao-memory>`` block. On any
        failure (no manager, busy, timeout, missing provider, parse failure),
        fall back to the deterministic Phase 1 path so injection never blocks
        the worker agent.
        """
        if not _is_memory_enabled():
            return ""
        try:
            ctx = self._get_terminal_context(terminal_id)
            session_name = ctx.get("session_name") if ctx else None
            cm = self._find_context_manager_terminal(session_name)
            if not cm:
                return self.get_memory_context_for_terminal(terminal_id)

            from cli_agent_orchestrator.models.terminal import TerminalStatus
            from cli_agent_orchestrator.providers.manager import provider_manager

            provider = provider_manager.get_provider(cm["id"])
            if provider is None:
                return self.get_memory_context_for_terminal(terminal_id)

            # Serialize concurrent dispatches to the same curator: two workers
            # racing would otherwise both see IDLE, both send_input, and the
            # second would clobber the first's request mid-flight.
            lock = _curator_locks.setdefault(cm["id"], threading.Lock())
            if not lock.acquire(blocking=False):
                return self.get_memory_context_for_terminal(terminal_id)
            try:
                if provider.get_status() != TerminalStatus.IDLE:
                    return self.get_memory_context_for_terminal(terminal_id)

                from cli_agent_orchestrator.services.terminal_service import (
                    get_output,
                    send_input,
                )

                send_input(cm["id"], task_description or "")

                # Poll up to ~15s for the curator to finish responding. Without
                # the sleep this loop spins in microseconds and we always read
                # stale output.
                for _ in range(30):
                    if provider.get_status() in (
                        TerminalStatus.COMPLETED,
                        TerminalStatus.IDLE,
                    ):
                        break
                    time.sleep(0.5)

                output = get_output(cm["id"])
            finally:
                lock.release()

            if isinstance(output, dict):
                output = output.get("output", "")
            if output and "<cao-memory>" in output:
                start = output.rfind("<cao-memory>")
                end = output.rfind("</cao-memory>")
                if 0 <= start < end:
                    return output[start : end + len("</cao-memory>")]
        except Exception as e:
            logger.debug(f"get_curated_memory_context failed, falling back: {e}")

        return self.get_memory_context_for_terminal(terminal_id)

    def _get_terminal_context(self, terminal_id: str) -> Optional[dict]:
        """Get terminal context for scope resolution.

        Reads from the database via terminal service. Resolves the
        terminal's current working directory through tmux so that
        project-scope resolution works in production. Returns None if
        terminal not found.
        """
        try:
            from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

            with SessionLocal() as db:
                terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
                if not terminal:
                    return None
                ctx = {
                    "terminal_id": terminal.id,
                    "session_name": terminal.tmux_session,
                    "provider": terminal.provider,
                    "agent_profile": terminal.agent_profile,
                    "cwd": None,
                }

            # Resolve cwd via tmux pane lookup. Lazy-import to avoid
            # importing terminal_service at module load (circular import).
            try:
                from cli_agent_orchestrator.services.terminal_service import (
                    get_working_directory,
                )

                ctx["cwd"] = get_working_directory(terminal_id)
            except Exception as e:
                logger.warning(f"Could not resolve working directory for {terminal_id}: {e}")
            return ctx
        except Exception as e:
            logger.warning(f"Could not get terminal context for {terminal_id}: {e}")
            return None
