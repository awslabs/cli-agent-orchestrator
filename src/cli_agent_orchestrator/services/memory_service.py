"""Memory service for CAO memory system (Phase 2 — SQLite-backed with wiki files)."""

import fcntl
import hashlib
import logging
import os
import re
import subprocess
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


class MemoryDisabledError(RuntimeError):
    """Raised when a memory subsystem operation is attempted while disabled.

    Callers (e.g. the MCP memory tools) should catch this and surface an
    explicit "memory disabled" message to the agent rather than a generic
    failure. See U5 / SC-6.
    """


class ProjectIdentityResolutionError(RuntimeError):
    """Raised when no project identity can be resolved via the U6 precedence chain.

    The resolver walks explicit override → git remote → cwd-hash; this error is
    raised only when all three fail (e.g. caller passed no cwd and has no
    explicit override set). Callers must not silently paper over this — it
    signals a config or invocation bug.
    """


# Phase 2.5 U6 — whitelist for explicit project_id overrides. Flat slug only:
# alphanumerics, ``.``, ``_``, ``-``. No slashes (project_id is an opaque
# identifier, not a hierarchy). Max length 128 matches the team-lead spec.
_PROJECT_ID_OVERRIDE_PATTERN = re.compile(r"^[a-zA-Z0-9._\-]{1,128}$")


def _validate_project_id_override(raw: str) -> str:
    """Validate an explicit ``project_id`` override; raise on reject.

    Rejects rather than sanitizes (U6 reviewer rule): silent sanitization of an
    explicit user-supplied config value hides typos and buries the contract.
    """
    if "\x00" in raw:
        raise ValueError("project_id override contains null byte")
    if not _PROJECT_ID_OVERRIDE_PATTERN.match(raw):
        raise ValueError(
            "project_id override must match ^[a-zA-Z0-9._\\-]{1,128}$; "
            f"got {raw!r}"
        )
    return raw


def _is_memory_enabled() -> bool:
    """Check the ``memory.enabled`` settings flag.

    Imported lazily so tests that monkeypatch ``settings_service`` don't race
    with module import order. Defaults to True on any error — the read path
    must never brick the service.
    """
    try:
        from cli_agent_orchestrator.services.settings_service import (
            is_memory_enabled,
        )

        return is_memory_enabled()
    except Exception as e:
        logger.warning(f"Failed to read memory.enabled, defaulting to True: {e}")
        return True


# -----------------------------------------------------------------------------
# Phase 2.5 U6 — Module-level project identity resolver
#
# Exposed at module scope (not on MemoryService) so non-service callers — most
# notably ``BaseProvider`` in U7 — can reuse the same precedence chain without
# instantiating a MemoryService. The MemoryService instance keeps using this
# via ``resolve_scope_id``.
# -----------------------------------------------------------------------------


def _read_project_id_override() -> Optional[str]:
    """Read and validate the explicit ``project_id`` override.

    Precedence (in ``settings_service.get_project_id_override``):
        1. ``CAO_PROJECT_ID`` env var
        2. ``memory.project_id`` nested key in settings.json

    The raw value is validated by ``_validate_project_id_override`` — a bad
    value raises ``ValueError`` rather than being silently sanitized, so
    misconfigured overrides fail loudly at resolve time.
    """
    try:
        from cli_agent_orchestrator.services.settings_service import (
            get_project_id_override,
        )

        raw = get_project_id_override()
    except Exception as e:
        logger.debug(f"Failed to read project_id override, skipping: {e}")
        return None
    if not raw:
        return None
    return _validate_project_id_override(raw)


def _normalize_git_remote(url: str) -> str:
    """Normalize a git remote URL into a stable slug id.

    Rules (applied in order):
        1. lowercase, strip whitespace
        2. strip protocol prefix (``https://``, ``http://``, ``ssh://``, ``git://``, ``git+ssh://``)
        3. strip ``user@`` or ``user:pass@`` auth
        4. SCP form (``host:path``) → ``host/path``
        5. strip trailing ``.git``
        6. collapse non-alphanumeric runs to ``-``

    Empty or unparseable input returns ``"unknown"``.
    """
    if not url:
        return "unknown"
    u = url.strip().lower()
    for proto in ("git+ssh://", "ssh://", "git://", "https://", "http://"):
        if u.startswith(proto):
            u = u[len(proto):]
            break
    if "@" in u:
        u = u.split("@", 1)[1]
    # SCP-form ``host:path`` (no slash before the colon) → ``host/path``.
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

    Origin-only per U6 team-lead decision #8 — fallback chains add test
    surface without value. ``timeout=2`` keeps laggy NFS from blocking the
    resolver; subprocess errors fall through to cwd-hash.
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
    """Opportunistically record an alias row; swallow DB errors to debug.

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

    Sources 1 and 2 opportunistically write the current ``cwd_hash`` into
    ``ProjectAliasModel`` (kind=``cwd_hash``). Source 2 additionally records
    the raw git URL (kind=``git_remote``). These writes never block.

    Raises ``ProjectIdentityResolutionError`` only when all three sources
    fail (no override, no git remote, no cwd passed). Callers must decide
    whether to catch-and-return-None or surface the error.
    """
    cwd_hash: Optional[str] = None
    if cwd is not None:
        try:
            cwd_hash = hashlib.sha256(
                os.path.realpath(str(cwd)).encode()
            ).hexdigest()[:12]
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
            _record_alias_safe(canonical, remote_url, "git_remote")
            return canonical

    if cwd_hash:
        return cwd_hash

    raise ProjectIdentityResolutionError(
        "Cannot resolve project identity: no override, no git remote, and no cwd provided"
    )


class MemoryService:
    """SQLite-backed memory service with wiki markdown files.

    Phase 2: SQLite is the source of truth for metadata queries.
    Wiki files remain the content store. index.md is a derived view regenerated from SQLite.
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

    def _get_db_session(self):
        """Get a DB session — uses custom engine if provided, else global."""
        if self._db_session_factory:
            return self._db_session_factory()
        from cli_agent_orchestrator.clients.database import SessionLocal

        return SessionLocal()

    # -------------------------------------------------------------------------
    # SQLite metadata operations (Phase 2)
    # -------------------------------------------------------------------------

    def _upsert_metadata(
        self,
        key: str,
        memory_type: str,
        scope: str,
        scope_id: Optional[str],
        file_path: str,
        tags: str = "",
        source_provider: Optional[str] = None,
        source_terminal_id: Optional[str] = None,
        token_estimate: Optional[int] = None,
    ) -> None:
        """Insert or update a memory_metadata row."""
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
                existing.tags = tags
                existing.source_provider = source_provider
                existing.source_terminal_id = source_terminal_id
                existing.token_estimate = token_estimate
                existing.file_path = file_path
                existing.memory_type = memory_type
                existing.updated_at = datetime.utcnow()
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

    def _query_metadata(
        self,
        query: Optional[str] = None,
        scope: Optional[str] = None,
        scope_id: Optional[str] = None,
        memory_type: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Query memory_metadata rows with optional filters."""
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        with self._get_db_session() as db:
            q = db.query(MemoryMetadataModel)
            if scope is not None:
                q = q.filter(MemoryMetadataModel.scope == scope)
            if scope_id is not None:
                q = q.filter(MemoryMetadataModel.scope_id == scope_id)
            elif scope is not None:
                q = q.filter(MemoryMetadataModel.scope_id.is_(None))
            if memory_type is not None:
                q = q.filter(MemoryMetadataModel.memory_type == memory_type)
            if query:
                escaped = query.replace("%", r"\%").replace("_", r"\_")
                pattern = f"%{escaped}%"
                q = q.filter(
                    (MemoryMetadataModel.key.like(pattern, escape="\\"))
                    | (MemoryMetadataModel.tags.like(pattern, escape="\\"))
                )

            rows = q.order_by(MemoryMetadataModel.updated_at.desc()).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "key": r.key,
                    "memory_type": r.memory_type,
                    "scope": r.scope,
                    "scope_id": r.scope_id,
                    "file_path": r.file_path,
                    "tags": r.tags,
                    "source_provider": r.source_provider,
                    "source_terminal_id": r.source_terminal_id,
                    "token_estimate": r.token_estimate,
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]

    def _delete_metadata(self, key: str, scope: str, scope_id: Optional[str] = None) -> bool:
        """Delete a memory_metadata row."""
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

    def _get_all_metadata_for_scope(self, scope: str, scope_id: Optional[str] = None) -> list[dict]:
        """Get all memory_metadata rows for a scope (for index regeneration)."""
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        with self._get_db_session() as db:
            q = db.query(MemoryMetadataModel).filter(MemoryMetadataModel.scope == scope)
            if scope_id is not None:
                q = q.filter(MemoryMetadataModel.scope_id == scope_id)
            else:
                q = q.filter(MemoryMetadataModel.scope_id.is_(None))
            rows = q.order_by(MemoryMetadataModel.updated_at.desc()).all()
            return [
                {
                    "key": r.key,
                    "memory_type": r.memory_type,
                    "scope": r.scope,
                    "scope_id": r.scope_id,
                    "file_path": r.file_path,
                    "tags": r.tags,
                    "token_estimate": r.token_estimate,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]

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
        project → canonical project identity (see ``resolve_project_id``)
        session → session_name
        agent   → agent_profile

        Project scope delegates to the module-level ``resolve_project_id``
        helper so non-service callers (e.g. ``BaseProvider`` in U7) can reuse
        the same precedence chain without dragging in a ``MemoryService``.
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
                # Call-sites that can tolerate a missing project scope (e.g.
                # agent-only context) expect ``None`` — the hard raise is for
                # callers that explicitly asked for project scope resolution.
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

        Returns a dict with:
            ``dry_run`` — always True for this method.
            ``canonical_id`` / ``alias``
            ``source_exists`` — whether the alias dir exists on disk.
            ``destination_exists`` — whether the canonical dir exists.
            ``action`` — ``"none"`` | ``"rename"`` | ``"merge"`` | ``"conflict"``.
            ``files`` — list of relative paths under the alias dir.

        Callers should inspect ``action`` and review ``files`` before invoking
        ``apply_project_dir_migration``. Conforms to the U6 risk note:
        *"Ship a dry-run mode first; never delete old dirs until alias table is
        populated."*
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
        """Store or update a memory. Upserts wiki file + SQLite row.

        Phase 2: SQLite is the source of truth for metadata.
        Wiki file is the content store. index.md is regenerated from SQLite.

        When ``memory.enabled`` is False (U5 / SC-6), raises
        ``MemoryDisabledError`` BEFORE any filesystem or SQLite write.
        """
        if not _is_memory_enabled():
            raise MemoryDisabledError("memory disabled")

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
            existing_content = wiki_path.read_text(encoding="utf-8")
            new_content = existing_content.rstrip("\n") + f"\n\n## {timestamp}\n{content}\n"
            id_match = re.search(r"<!-- id: ([a-f0-9\-]+)", existing_content)
            if id_match:
                memory_id = id_match.group(1)
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

        source_provider = None
        source_terminal_id = None
        if terminal_context:
            source_provider = terminal_context.get("provider")
            source_terminal_id = terminal_context.get("terminal_id")

        # U1.7: token estimate — char-based (len / 4)
        token_estimate = int(len(content) / 4)

        # U1.3: Upsert SQLite row
        sqlite_ok = False
        try:
            self._upsert_metadata(
                key=key,
                memory_type=memory_type,
                scope=scope,
                scope_id=scope_id,
                file_path=str(wiki_path),
                tags=tags,
                source_provider=source_provider,
                source_terminal_id=source_terminal_id,
                token_estimate=token_estimate,
            )
            sqlite_ok = True
        except Exception as e:
            logger.warning(f"Failed to upsert memory metadata to SQLite: {e}")

        # U1.8: Regenerate index.md from SQLite (derived view)
        action = "updated" if is_update else "created"
        if sqlite_ok:
            self._regenerate_scope_index(scope, scope_id)
        else:
            # Fallback: Phase 1 manual line-patching
            self._update_index(scope, scope_id, key, memory_type, tags, content, timestamp, action)

        logger.info(f"Memory {action}: key={key} scope={scope} scope_id={scope_id}")

        # U2.4: Log memory_stored event (non-blocking)
        if terminal_context:
            try:
                from cli_agent_orchestrator.clients.database import log_session_event

                log_session_event(
                    session_name=terminal_context.get("session_name", ""),
                    terminal_id=terminal_context.get("terminal_id", ""),
                    provider=source_provider or "",
                    event_type="memory_stored",
                    summary=f"Stored memory: {key} ({scope}/{memory_type})",
                )
            except Exception as e:
                logger.debug(f"Non-blocking event log failed: {e}")

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

    def _regenerate_scope_index(self, scope: str, scope_id: Optional[str]) -> None:
        """Regenerate index.md for a scope entirely from SQLite (U1.8).

        This replaces manual line-patching with a full rewrite from the DB.
        Falls back to keeping existing index.md if SQLite query fails.
        """
        try:
            rows = self._get_all_metadata_for_scope(scope=scope, scope_id=scope_id)
        except Exception as e:
            logger.warning(f"Cannot regenerate index from SQLite, keeping existing: {e}")
            return

        index_path = self.get_index_path(scope, scope_id)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = index_path.parent / ".index.lock"

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            lines = [
                "# CAO Memory Index",
                f"<!-- Updated: {now_str} -->",
                "",
            ]

            # Group rows by scope
            scopes_seen: dict[str, list[dict]] = {}
            for row in rows:
                s = row["scope"]
                scopes_seen.setdefault(s, []).append(row)

            for s in sorted(scopes_seen.keys()):
                lines.append(f"## {s}")
                for row in scopes_seen[s]:
                    relative_path = f"{row['scope']}/{row['key']}.md"
                    tok = row.get("token_estimate") or 0
                    updated = row.get("updated_at")
                    if updated:
                        if isinstance(updated, datetime):
                            ts = updated.strftime("%Y-%m-%dT%H:%M:%SZ")
                        else:
                            ts = str(updated)
                    else:
                        ts = now_str
                    entry_line = (
                        f"- [{row['key']}]({relative_path}) — "
                        f"type:{row['memory_type']} tags:{row.get('tags', '')} "
                        f"~{tok}tok updated:{ts}"
                    )
                    lines.append(entry_line)
                lines.append("")

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
        search_mode: str = "hybrid",
        terminal_context: Optional[dict] = None,
        scan_all: bool = False,
    ) -> list[Memory]:
        """Recall memories matching query and filters.

        Phase 2: Uses SQLite metadata query + optional BM25 content search.

        search_mode:
            "metadata" — SQLite key/tag matching only (Phase 1 behavior)
            "bm25"     — BM25 full-text search over wiki file content only
            "hybrid"   — SQLite first, BM25 to fill remaining slots up to limit

        When ``memory.enabled`` is False (U5 / SC-6) returns ``[]`` — never
        opens the SQLite session or scans wiki files.
        """
        if not _is_memory_enabled():
            return []

        if search_mode not in ("metadata", "bm25", "hybrid"):
            raise ValueError(f"Invalid search_mode: {search_mode!r}")

        # Resolve scope_id for scoped queries
        scope_id = None
        if scope and terminal_context and not scan_all:
            scope_id = self.resolve_scope_id(scope, terminal_context)

        results: list[Memory] = []

        # Step 1: SQLite metadata search (unless bm25-only)
        if search_mode in ("metadata", "hybrid"):
            try:
                rows = self._query_metadata(
                    query=query,
                    scope=scope,
                    scope_id=scope_id if scope and not scan_all else None,
                    memory_type=memory_type,
                    limit=limit * 3,  # over-fetch for post-filtering
                )
                results = self._rows_to_memories(rows)
            except Exception as e:
                logger.warning(f"SQLite recall failed, falling back to file scan: {e}")
                results = self._recall_file_fallback(
                    query=query,
                    scope=scope,
                    memory_type=memory_type,
                    limit=limit,
                    terminal_context=terminal_context,
                    scan_all=scan_all,
                )

        # Step 2: BM25 content search to fill remaining slots
        if query and search_mode in ("bm25", "hybrid"):
            existing_keys = {m.key for m in results}
            remaining = limit - len(results)
            if search_mode == "bm25" or remaining > 0:
                slots = limit if search_mode == "bm25" else remaining
                bm25_results = self._bm25_search(
                    query=query,
                    scope=scope,
                    scope_id=scope_id if scope and not scan_all else None,
                    memory_type=memory_type,
                    limit=slots,
                    exclude_keys=existing_keys,
                    terminal_context=terminal_context,
                    scan_all=scan_all,
                )
                results.extend(bm25_results)

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
        """BM25 full-text search over wiki file content.

        Builds an ephemeral BM25 index over wiki files in scope,
        scores each against the query, returns top matches.
        Gracefully returns [] if rank-bm25 is not installed.
        """
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            logger.warning("rank-bm25 not installed — BM25 search unavailable, returning empty")
            return []

        # Collect candidate wiki files
        search_dirs = self._get_search_dirs(scope, terminal_context, scan_all=scan_all)
        candidates: list[tuple[Path, str, dict]] = []  # (path, content, entry)
        base_resolved = self.base_dir.resolve()

        for project_dir in search_dirs:
            wiki_dir = project_dir / "wiki"
            if not wiki_dir.exists():
                continue
            # Scan scope subdirectories for .md files (excluding index.md)
            for md_file in sorted(wiki_dir.rglob("*.md")):
                if md_file.name == "index.md":
                    continue
                # Path traversal guard — check BEFORE reading file content
                resolved = md_file.resolve()
                if not str(resolved).startswith(str(base_resolved) + os.sep):
                    logger.warning(f"Path traversal detected in BM25 scan: {md_file}")
                    continue
                # Extract key from filename
                key = md_file.stem
                if key in exclude_keys:
                    continue
                # Determine scope from parent directory name
                file_scope = md_file.parent.name
                if scope and file_scope != scope:
                    continue

                try:
                    content = md_file.read_text(encoding="utf-8")
                except Exception:
                    continue

                # Extract memory_type from frontmatter if filtering
                if memory_type:
                    type_match = re.search(r"^type:\s*(.+)$", content, re.MULTILINE)
                    if type_match and type_match.group(1).strip() != memory_type:
                        continue

                candidates.append((md_file, content, {"key": key, "scope": file_scope}))

        if not candidates:
            return []

        # Tokenize and build BM25 index
        tokenized_corpus = [c[1].lower().split() for c in candidates]
        bm25 = BM25Okapi(tokenized_corpus)
        query_tokens = query.lower().split()
        scores = bm25.get_scores(query_tokens)

        # Rank by score descending, take top `limit`.
        # Note: BM25Okapi can produce negative IDF scores with very small corpora,
        # so we filter by checking that at least one query token appears in the doc
        # rather than relying on score > 0.
        scored = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)

        results: list[Memory] = []
        query_tokens_set = set(query_tokens)
        for score, (md_file, content, entry) in scored:
            if len(results) >= limit:
                break

            # Skip documents that contain none of the query tokens
            doc_tokens_set = set(content.lower().split())
            if not query_tokens_set & doc_tokens_set:
                continue

            memory = self._parse_wiki_file(md_file, content, entry)
            if memory:
                results.append(memory)

        return results

    def _rows_to_memories(self, rows: list[dict]) -> list[Memory]:
        """Convert SQLite query result rows to Memory objects by reading wiki files."""
        results: list[Memory] = []
        base_resolved = self.base_dir.resolve()
        for row in rows:
            file_path = Path(row["file_path"]).resolve()
            # Path traversal guard: file must be under base_dir
            if not str(file_path).startswith(str(base_resolved) + os.sep):
                logger.warning(f"Path traversal detected in DB row: {file_path}")
                continue
            if not file_path.exists():
                continue
            file_content = file_path.read_text(encoding="utf-8")

            # Extract latest content section
            timestamps = re.findall(r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", file_content)
            if not timestamps:
                continue
            last_section = file_content.rsplit(f"## {timestamps[-1]}", 1)
            latest_content = last_section[-1].strip() if len(last_section) > 1 else ""

            created_at = row.get("created_at") or datetime.now(timezone.utc)
            updated_at = row.get("updated_at") or datetime.now(timezone.utc)
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            if isinstance(updated_at, str):
                updated_at = datetime.fromisoformat(updated_at)
            # Ensure timezone-aware
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)

            results.append(
                Memory(
                    id=row["id"],
                    key=row["key"],
                    memory_type=row["memory_type"],
                    scope=row["scope"],
                    scope_id=row.get("scope_id"),
                    file_path=str(file_path),
                    tags=row.get("tags", ""),
                    source_provider=row.get("source_provider"),
                    source_terminal_id=row.get("source_terminal_id"),
                    created_at=created_at,
                    updated_at=updated_at,
                    content=latest_content,
                )
            )
        return results

    def _recall_file_fallback(
        self,
        query: Optional[str],
        scope: Optional[str],
        memory_type: Optional[str],
        limit: int,
        terminal_context: Optional[dict],
        scan_all: bool,
    ) -> list[Memory]:
        """Phase 1 file-based recall — used as fallback when SQLite is unavailable."""
        results: list[Memory] = []
        search_dirs = self._get_search_dirs(scope, terminal_context, scan_all=scan_all)

        for project_dir in search_dirs:
            index_path = project_dir / "wiki" / "index.md"
            if not index_path.exists():
                continue

            entries = self._parse_index(index_path)
            for entry in entries:
                if scope and entry["scope"] != scope:
                    continue
                if memory_type and entry["memory_type"] != memory_type:
                    continue

                wiki_file = project_dir / "wiki" / entry["relative_path"]
                # U6 (U2 INFO-1 absorption, team-lead decision #9): the
                # ``relative_path`` is parsed from index.md — an attacker who
                # tampered with index.md could embed ``../../../etc/passwd``.
                # Resolve and require containment within base_dir before any
                # read. Applied at both file-fallback and inject call sites.
                resolved_wiki = wiki_file.resolve()
                base_resolved = self.base_dir.resolve()
                if not str(resolved_wiki).startswith(str(base_resolved) + os.sep):
                    logger.warning(
                        f"Path traversal in index entry rejected: {entry.get('relative_path')}"
                    )
                    continue
                if not resolved_wiki.exists():
                    continue

                file_content = resolved_wiki.read_text(encoding="utf-8")
                if query:
                    terms = query.lower().split()
                    content_lower = file_content.lower()
                    if not all(term in content_lower for term in terms):
                        continue

                memory = self._parse_wiki_file(resolved_wiki, file_content, entry)
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
                # U6 legacy-path reader (team-lead decision #4): when a
                # canonical id is in use, also walk any cwd-hash alias dirs
                # that existed before the resolver was introduced. Keeps
                # pre-U6 memories readable until ``cao memory migrate-project-ids``
                # (U9) consolidates them.
                self._append_legacy_alias_dirs(project_scope_id, dirs)
        # Without context and scan_all=False, only global is safe (agent/MCP context).

        return dirs

    def _append_legacy_alias_dirs(
        self, canonical_id: str, dirs: list[Path]
    ) -> None:
        """Append legacy ``<cwd_hash>/`` dirs that alias to ``canonical_id``.

        Safe no-op when the alias table or base_dir are unavailable. Only
        ``kind='cwd_hash'`` aliases are considered — git-remote aliases are
        the raw URL, not a directory name.
        """
        try:
            from cli_agent_orchestrator.clients.database import (
                list_aliases_for_project,
            )

            aliases = list_aliases_for_project(canonical_id)
        except Exception as e:
            logger.debug(f"legacy-dir alias lookup failed (non-fatal): {e}")
            return
        for row in aliases:
            if row.get("kind") != "cwd_hash":
                continue
            alias = row.get("alias")
            if not alias or alias == canonical_id:
                continue
            legacy_dir = self.base_dir / alias
            if legacy_dir.exists() and legacy_dir not in dirs:
                dirs.append(legacy_dir)

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
        """Remove a memory. Deletes wiki file, SQLite row, and regenerates index.md.

        If scope_id is provided directly it is used as-is (for cleanup).
        Otherwise it is resolved from terminal_context.

        When ``memory.enabled`` is False (U5 / SC-6), raises
        ``MemoryDisabledError`` BEFORE any filesystem or SQLite mutation.
        """
        if not _is_memory_enabled():
            raise MemoryDisabledError("memory disabled")

        MemoryScope(scope)
        key = self._sanitize_key(key)
        if scope_id is None:
            scope_id = self.resolve_scope_id(scope, terminal_context)
        wiki_path = self.get_wiki_path(scope, scope_id, key)

        if not wiki_path.exists():
            # Still try to clean up SQLite row if wiki file already gone
            try:
                self._delete_metadata(key=key, scope=scope, scope_id=scope_id)
            except Exception:
                pass
            return False

        # Delete the wiki file
        wiki_path.unlink()
        logger.info(f"Deleted memory file: {wiki_path}")

        # U1.5: Delete SQLite row
        sqlite_ok = False
        try:
            self._delete_metadata(key=key, scope=scope, scope_id=scope_id)
            sqlite_ok = True
        except Exception as e:
            logger.warning(f"Failed to delete memory metadata from SQLite: {e}")

        # U1.8: Regenerate index.md from SQLite (or fallback)
        if sqlite_ok:
            self._regenerate_scope_index(scope, scope_id)
        else:
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

        Scope precedence: session > project > global (preserved in output).

        SC-2 caps (U2): each scope independently capped at
        ``MEMORY_MAX_PER_SCOPE`` entries and at a per-scope character budget.
        The per-scope char cap is ``min(MEMORY_SCOPE_BUDGET_CHARS,
        budget_chars // len(scopes_in_order))`` so the caller's overall
        ``budget_chars`` is still respected and one scope cannot monopolize
        the budget. Unused budget from an empty scope is NOT reallocated to
        other scopes (keeps Phase 2 U7 cache-friendly boundaries intact).

        When ``memory.enabled`` is False (U5 / SC-6), returns ``""`` —
        never reads ``index.md`` or wiki files.
        """
        if not _is_memory_enabled():
            return ""

        # We need terminal context to resolve scopes. Import here to avoid circular imports.
        terminal_context = self._get_terminal_context(terminal_id)
        if not terminal_context:
            return ""

        scopes_in_order = [
            MemoryScope.SESSION.value,
            MemoryScope.PROJECT.value,
            MemoryScope.GLOBAL.value,
        ]

        # Per-scope character cap: the smaller of the hard per-scope ceiling
        # and an even slice of the caller-supplied overall budget. Guards
        # against a caller passing a very small ``budget_chars``.
        scope_char_cap = min(
            MEMORY_SCOPE_BUDGET_CHARS,
            max(0, budget_chars // len(scopes_in_order)),
        )

        lines: list[str] = []

        for scope_val in scopes_in_order:
            scope_id = self.resolve_scope_id(scope_val, terminal_context)
            project_dir = self._get_project_dir(scope_val, scope_id)
            index_path = project_dir / "wiki" / "index.md"
            if not index_path.exists():
                continue

            # Sort entries for this scope by updated_at desc so the newest
            # memories are considered first within the per-scope cap.
            scope_entries = [
                e for e in self._parse_index(index_path) if e["scope"] == scope_val
            ]
            scope_entries.sort(key=lambda e: e.get("updated_at", ""), reverse=True)

            scope_memories: list[Memory] = []
            base_resolved = self.base_dir.resolve()
            for entry in scope_entries:
                if len(scope_memories) >= MEMORY_MAX_PER_SCOPE:
                    break
                wiki_file = project_dir / "wiki" / entry["relative_path"]
                # U6 / U2 INFO-1 absorption: same containment guard as the
                # recall-fallback path — reject any relative_path that would
                # resolve outside base_dir before reading.
                resolved_wiki = wiki_file.resolve()
                if not str(resolved_wiki).startswith(str(base_resolved) + os.sep):
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

            # Fill this scope's slice up to its own char cap. Each scope gets
            # its own accumulator so an earlier scope cannot eat into a later
            # scope's slice.
            scope_used_chars = 0
            for mem in scope_memories:
                line = f"- [{mem.scope}] {mem.key}: {mem.content}"
                line_len = len(line) + 1  # +1 for the newline between lines
                if scope_used_chars + line_len > scope_char_cap:
                    break
                lines.append(line)
                scope_used_chars += line_len

        if not lines:
            return ""

        context = "## Context from CAO Memory\n" + "\n".join(lines)
        return f"<cao-memory>\n{context}\n</cao-memory>"

    def get_curated_memory_context(
        self,
        terminal_id: str,
        task_description: str = "",
        timeout: float = 10.0,
    ) -> str:
        """Get curated memory context from the context-manager agent.

        If a context-manager terminal (agent_profile='memory_manager') exists
        in the same session and is IDLE, sends it the task description, waits
        for its curated response, and returns the ``<cao-memory>`` block.

        Falls back to ``get_memory_context_for_terminal()`` (Phase 1) when:
        - No context-manager terminal exists in the session
        - Context-manager is not IDLE (busy with another request)
        - Context-manager does not respond within timeout
        - Any error occurs during the interaction

        When ``memory.enabled`` is False (U5 / SC-6), returns ``""`` — never
        pings the context-manager or reads wiki files.

        Args:
            terminal_id: The terminal that needs memory context.
            task_description: Description of the incoming task (sent to context-manager).
            timeout: Maximum seconds to wait for context-manager response.

        Returns:
            A ``<cao-memory>...</cao-memory>`` block string, or empty string.
        """
        if not _is_memory_enabled():
            return ""

        try:
            cm_terminal = self._find_context_manager_terminal(terminal_id)
            if not cm_terminal:
                return self.get_memory_context_for_terminal(terminal_id)

            # U9.5: Heartbeat check — context-manager must be IDLE
            from cli_agent_orchestrator.providers.manager import provider_manager

            cm_provider = provider_manager.get_provider(cm_terminal["id"])
            if not cm_provider:
                return self.get_memory_context_for_terminal(terminal_id)

            from cli_agent_orchestrator.models.terminal import TerminalStatus

            cm_status = cm_provider.get_status()
            if cm_status != TerminalStatus.IDLE and cm_status != TerminalStatus.COMPLETED:
                logger.info(
                    f"Context-manager {cm_terminal['id']} is {cm_status.value}, "
                    f"falling back to Phase 1 injection"
                )
                return self.get_memory_context_for_terminal(terminal_id)

            # Send task description to context-manager
            from cli_agent_orchestrator.services.terminal_service import send_input

            prompt = (
                f"Curate memory context for terminal {terminal_id}. "
                f"Task: {task_description}" if task_description else
                f"Curate memory context for terminal {terminal_id}."
            )
            send_input(cm_terminal["id"], prompt)

            # Wait for context-manager to complete
            import time

            start = time.time()
            while time.time() - start < timeout:
                status = cm_provider.get_status()
                if status == TerminalStatus.COMPLETED:
                    break
                time.sleep(0.5)
            else:
                logger.warning(
                    f"Context-manager {cm_terminal['id']} timed out after {timeout}s, "
                    f"falling back to Phase 1"
                )
                return self.get_memory_context_for_terminal(terminal_id)

            # Extract response
            try:
                from cli_agent_orchestrator.services.terminal_service import OutputMode, get_output

                response = get_output(cm_terminal["id"], mode=OutputMode.LAST)
                if response and "<cao-memory>" in response:
                    return response.strip()
                elif response:
                    # Wrap bare response in cao-memory tags
                    return f"<cao-memory>\n{response.strip()}\n</cao-memory>"
            except Exception as e:
                logger.debug(f"Failed to extract context-manager response: {e}")

            return self.get_memory_context_for_terminal(terminal_id)

        except Exception as e:
            logger.debug(f"Curated memory context failed: {e}")
            return self.get_memory_context_for_terminal(terminal_id)

    def _find_context_manager_terminal(self, terminal_id: str) -> Optional[dict]:
        """Find the context-manager terminal in the same session as terminal_id.

        Returns terminal metadata dict or None if not found.
        """
        try:
            from cli_agent_orchestrator.clients.database import (
                get_terminal_metadata,
                list_terminals_by_session,
            )

            metadata = get_terminal_metadata(terminal_id)
            if not metadata:
                return None

            session_name = metadata["tmux_session"]
            terminals = list_terminals_by_session(session_name)

            for t in terminals:
                if t["agent_profile"] == "memory_manager" and t["id"] != terminal_id:
                    return t

            return None
        except Exception as e:
            logger.debug(f"Failed to find context-manager terminal: {e}")
            return None

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
