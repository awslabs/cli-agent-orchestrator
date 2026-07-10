"""Memory service for CAO memory system (Phase 2 — wiki + SQLite metadata)."""

import asyncio
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
from typing import Any, Optional, Set

from cli_agent_orchestrator.constants import (
    MEMORY_BASE_DIR,
    MEMORY_MAX_PER_SCOPE,
    MEMORY_SCOPE_BUDGET_CHARS,
)
from cli_agent_orchestrator.models.memory import Memory, MemoryScope, MemoryType
from cli_agent_orchestrator.services.memory_archive.base import ExportReport, ImportReport
from cli_agent_orchestrator.utils.path_validation import (
    safe_join_under_base,
    validate_path_component,
)

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
        # Strong refs to in-flight background compile tasks (event-loop path)
        # so they are not garbage-collected before completion.
        self._compile_tasks: Set[Any] = set()
        # Per-topic debounce: at most one compile in flight per
        # (scope, scope_id, key). Guarded by a threading lock because
        # schedule/complete can run on the event loop or a worker thread.
        self._compile_inflight: Set[Any] = set()
        self._compile_inflight_lock = threading.Lock()
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
        last_compiled_at: Optional[datetime] = None,
        related_keys: Optional[str] = None,
        preserve_provenance: bool = False,
    ) -> None:
        """Insert or update the metadata row for (key, scope, scope_id).

        Symmetric upsert: every field set on insert is also set on update —
        ``memory_type``, ``tags``, ``file_path``, ``source_provider``,
        ``source_terminal_id``, ``token_estimate``, ``updated_at``.

        ``preserve_provenance`` leaves ``source_provider`` and
        ``source_terminal_id`` untouched on update. The compile write-back
        sets it: a background rewrite is not a new store, so it must not
        erase who stored the memory. (None provenance from store() itself
        IS written — the latest store genuinely had no terminal context.)

        ``last_compiled_at`` is only written when non-None — a
        plain append or fallback must not clobber a prior successful compile
        timestamp, so we leave the existing value intact when it is None.

        ``related_keys`` follows the same rule: None means
        "never attempted" and leaves any prior value intact; ``""`` means
        "computed, none found" and IS written (distinct from None so the
        second pass doesn't retry endlessly).
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
                if not preserve_provenance:
                    existing.source_provider = source_provider
                    existing.source_terminal_id = source_terminal_id
                existing.token_estimate = token_estimate
                if last_compiled_at is not None:
                    # A compile write: pin updated_at to the same instant so
                    # the staleness check (last_compiled_at >= updated_at)
                    # sees the topic as freshly compiled, not perpetually
                    # stale by the microseconds between two now() calls.
                    existing.updated_at = last_compiled_at
                    existing.last_compiled_at = last_compiled_at
                else:
                    existing.updated_at = datetime.now(timezone.utc)
                if related_keys is not None:
                    existing.related_keys = related_keys
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
                    last_compiled_at=last_compiled_at,
                    related_keys=related_keys,
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

    @staticmethod
    def resolve_caller_scope(terminal_context: Optional[dict]) -> str:
        """Derive the caller's effective scope.

        Used by ``store()`` as the upper bound for cross-scope writes. The
        caller may declare an explicit ``caller_scope`` in
        ``terminal_context`` (trust-equivalent to the operator); otherwise
        we default to ``"global"`` (the most permissive, operator/CLI
        semantics).

        Inferred downgrade from ``terminal_id``/``agent_profile``/
        ``session_name`` was deliberately rejected: it broke legacy callers
        that pass full terminal contexts while legitimately writing
        ``scope="global"``. The threat-model intent is preserved — a
        caller that must be constrained sets ``caller_scope`` explicitly.
        """
        ctx = terminal_context or {}
        explicit = ctx.get("caller_scope")
        if isinstance(explicit, str) and explicit in {
            "session",
            "project",
            "agent",
            "global",
            "federated",
        }:
            return explicit
        return "global"

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

        # ``federated`` is a single machine-wide tier with no per-id
        # isolation — like ``global``, its scope_id is always None.
        if scope == MemoryScope.FEDERATED.value:
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
            # ``scope_id`` is user/context-derived (project-identity slug,
            # override, or cwd hash). Validate it as a single safe path
            # segment and confine the container under the memory base via
            # realpath containment so a crafted scope_id cannot escape.
            return Path(safe_join_under_base(str(self.base_dir), scope_id, description="scope_id"))
        # ``federated`` is a machine-wide shared tier living in its own
        # top-level container, a sibling of ``global``.
        if scope == MemoryScope.FEDERATED.value:
            return self.base_dir / "federated"
        return self.base_dir / "global"

    def get_wiki_path(self, scope: str, scope_id: Optional[str], key: str) -> Path:
        """Get the path to a wiki topic file.

        For session and agent scopes, ``scope_id`` is nested into the
        path so that two sessions (or two agent profiles) with the same
        key do not collide on disk.

        Every user-derived segment (``scope``, ``scope_id``, ``key``) is
        validated as a single safe path component and the assembled path is
        confined under MEMORY_BASE_DIR via realpath containment, so a
        crafted value cannot traverse out of the memory base directory.
        """
        # Validate every user-derived segment as a single safe path
        # component up front. ``key`` is validated in its raw form (not just
        # the ``{key}.md`` filename) so empty/``.``/``..`` keys are rejected
        # rather than turning into benign-but-unintended filenames.
        validate_path_component(scope, "scope")
        validate_path_component(key, "key")
        if scope_id is not None:
            validate_path_component(scope_id, "scope_id")

        # Container segment: ``project`` scope keys off the scope_id;
        # session/agent memories live under the ``global`` container with
        # scope_id nested into the wiki path; federated is its own tier.
        if scope == MemoryScope.PROJECT.value and scope_id:
            container = scope_id
        elif scope == MemoryScope.FEDERATED.value:
            container = "federated"
        else:
            container = "global"

        if scope in (MemoryScope.SESSION.value, MemoryScope.AGENT.value) and scope_id:
            components = [container, "wiki", scope, scope_id, f"{key}.md"]
        else:
            components = [container, "wiki", scope, f"{key}.md"]

        return Path(
            safe_join_under_base(
                str(self.base_dir), *components, description="memory path component"
            )
        )

    def get_index_path(self, scope: str, scope_id: Optional[str]) -> Path:
        """Get the path to the index.md file."""
        project_dir = self._get_project_dir(scope, scope_id)
        return project_dir / "wiki" / "index.md"

    # -------------------------------------------------------------------------
    # Store
    # -------------------------------------------------------------------------

    @staticmethod
    def _occurred_at_would_clamp(
        occurred_at: Optional[datetime],
        latest_section_at: Optional[datetime],
        now: datetime,
    ) -> bool:
        """D5 clamp decision, shared by ``store()`` and the OKF import dry-run.

        Clamp when ``occurred_at`` is in the future, or older than the
        topic's latest section timestamp (merges only — pass ``None`` for
        new topics). ``None`` ``occurred_at`` never clamps. Keeping this
        rule in one place guarantees the dry-run report cannot drift from
        what a real ``store()`` would do.
        """
        if occurred_at is None:
            return False
        return occurred_at > now or (
            latest_section_at is not None and occurred_at < latest_section_at
        )

    async def store(
        self,
        content: str,
        scope: str = "project",
        memory_type: str = "project",
        key: Optional[str] = None,
        tags: str = "",
        terminal_context: Optional[dict] = None,
        occurred_at: Optional[datetime] = None,
    ) -> Memory:
        """Store or update a memory. Upserts wiki file + index.md.

        Declared async for compatibility with async callers (MCP server, FastAPI).
        File I/O is synchronous; a future improvement would use aiofiles.

        ``occurred_at`` (archive import, #345 D5): optional original entry
        timestamp. ``None`` keeps today's behavior byte-identical. An
        in-order value (not in the future and, for an existing topic, not
        older than its latest section timestamp) is used verbatim for the
        ``## <ts>`` section heading (and ``created_at`` when the topic is
        new). An out-of-order or future value is CLAMPED: the heading uses
        now() — preserving the append-only contract that the last section
        is the latest — and the original timestamp is recorded as a first
        body line ``_Originally recorded: <ISO-ts>_``. The returned Memory
        sets ``timestamp_clamped=True`` when clamping happened so the
        importer can count it.

        Raises ``MemoryDisabledError`` when ``memory.enabled`` is False
        (U5 / SC-6) — no filesystem or SQLite writes happen.
        """
        if not _is_memory_enabled():
            raise MemoryDisabledError(MEMORY_DISABLED_MESSAGE)

        # Validate
        MemoryScope(scope)
        MemoryType(memory_type)

        # Federated writes are credential-gated. The machine-wide shared
        # tier rejects content matching common secret patterns. The log
        # line carries the pattern NAME only — never content bytes.
        if scope == MemoryScope.FEDERATED.value:
            from cli_agent_orchestrator.services.secret_gate import scan_for_secrets

            hit = scan_for_secrets(content)
            if hit:
                # Do not log detector output; emit only a constant event marker.
                logger.warning("federated_secret_rejected")
                raise ValueError(f"federated write rejected: matched credential pattern {hit!r}")

        # Store-time cross-scope write guard. A caller may
        # only write a scope it is authorised for (SCOPE_RANK). The caller
        # scope defaults to "global" (operator) unless terminal_context sets
        # an explicit ``caller_scope`` — so existing CLI/operator callers are
        # unaffected. The rejection log carries no content bytes.
        from cli_agent_orchestrator.services.memory_scoring import scope_write_allowed

        caller_scope = self.resolve_caller_scope(terminal_context)
        if not scope_write_allowed(caller_scope, scope):
            logger.warning(
                "cross_scope_store_rejected caller=%s target=%s key=%s",
                caller_scope,
                scope,
                key,
            )
            raise PermissionError(
                f"caller scope {caller_scope!r} may not write target scope {scope!r}"
            )

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

        # Normalize occurred_at to UTC-aware for the ordering comparisons
        # below. A future value is clamped for new topics AND merges (D5);
        # the older-than-latest-section check needs the existing file and
        # runs inside the topic lock.
        if occurred_at is not None:
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            else:
                occurred_at = occurred_at.astimezone(timezone.utc)
        timestamp_clamped = False

        wiki_path = self.get_wiki_path(scope, scope_id, key)
        wiki_path.parent.mkdir(parents=True, exist_ok=True)

        # Per-topic lock around the read-modify-write cycle. Without
        # this, two concurrent store() calls for the same
        # (scope, scope_id, key) can both read the old content and
        # then overwrite each other, losing one update. Mirrors the
        # .index.lock pattern in _update_index.
        topic_lock_path = wiki_path.parent / f".{wiki_path.stem}.lock"
        topic_lock_fd = open(topic_lock_path, "w")
        try:
            fcntl.flock(topic_lock_fd, fcntl.LOCK_EX)

            # Check if topic file already exists (upsert)
            is_update = wiki_path.exists()
            memory_id = str(uuid.uuid4())
            created_at = now
            latest_section_at: Optional[datetime] = None

            if is_update:
                # Read existing file to get original created_at and id from comment
                existing_content = wiki_path.read_text(encoding="utf-8")
                # Try to extract original id
                id_match = re.search(r"<!-- id: ([a-f0-9\-]+)", existing_content)
                if id_match:
                    memory_id = id_match.group(1)
                # Extract original created_at (first section) and the latest
                # section timestamp (last) for the occurred_at ordering rule.
                # The regex is deliberately unanchored, matching
                # _parse_wiki_file's existing behavior: a timestamp-shaped line
                # inside an untrusted body can match. Import-time escaping
                # (Unit 3) is the mitigation; deferred here, no behavior change.
                existing_ts = re.findall(
                    r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", existing_content
                )
                if existing_ts:
                    created_at = datetime.strptime(existing_ts[0], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                    latest_section_at = datetime.strptime(
                        existing_ts[-1], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
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

            # D5 ordering rule: an in-order, non-future occurred_at is used
            # verbatim for the section heading; a future value (new topics
            # AND merges) or a value older than the topic's latest section
            # is clamped to now(), with provenance preserved as a first
            # body line. occurred_at=None keeps the pre-#345 bytes exactly.
            entry_body = content
            if occurred_at is not None:
                if self._occurred_at_would_clamp(occurred_at, latest_section_at, now):
                    timestamp_clamped = True
                    original_iso = occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    entry_body = f"_Originally recorded: {original_iso}_\n{content}"
                else:
                    timestamp = occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if not is_update:
                        created_at = occurred_at

            if is_update:
                # Append new timestamped entry
                new_content = existing_content.rstrip("\n") + f"\n\n## {timestamp}\n{entry_body}\n"
            else:
                new_content = (
                    f"# {key}\n"
                    f"<!-- id: {memory_id} | scope: {scope} | type: {memory_type} | tags: {tags} -->\n"
                    f"\n## {timestamp}\n{entry_body}\n"
                )

            # Atomic write of the append version: write to tmp then os.replace.
            # This is always the immediate, durable result of store(). LLM
            # compaction (below) is deferred and rewrites the file later.
            #
            # CodeQL note (py/clear-text-storage-sensitive-data): this write is
            # flagged only because ``new_content`` embeds the topic ``key`` and
            # CodeQL's name-based heuristic classifies any variable named
            # ``key`` as a secret. It is a FALSE POSITIVE, not a leak: ``key``
            # is a user-authored topic slug (see ``_sanitize_key``), never a
            # credential. The memory wiki is intentionally human-readable
            # plaintext markdown — that is the product contract, and encrypting
            # it would defeat the feature (agents and humans read these files
            # directly). No secret is persisted here. Dismiss the alert as
            # "won't fix / by-design" in the Security tab with this rationale;
            # do NOT add encryption. As an additional safeguard, content
            # written to the machine-wide federated tier is screened by the
            # secret gate (``scan_for_secrets``) earlier in ``store()`` and
            # rejected if it matches a credential pattern.
            tmp_path = wiki_path.parent / f".{wiki_path.stem}.tmp"
            tmp_path.write_text(new_content, encoding="utf-8")
            os.replace(str(tmp_path), str(wiki_path))

            # LLM wiki compilation, deferred. A coding-agent CLI cold-starts in
            # tens of seconds — far too slow to block store() — so on an "llm"
            # mode UPDATE we schedule a background task that merges the article
            # and rewrites the file once the CLI returns. The append version
            # just written is the safe, byte-identical baseline; the background
            # compile only ever upgrades it (or no-ops on failure). A brand-new
            # topic is never compiled: the LLM has nothing to merge into and
            # would only regurgitate the canonical ``# {key}`` header.
            last_compiled_at: Optional[datetime] = None
            try:
                from cli_agent_orchestrator.services.settings_service import get_compile_mode

                if is_update and get_compile_mode() == "llm":
                    self._schedule_background_compile(
                        scope=scope,
                        scope_id=scope_id,
                        key=key,
                        # Pass the clamped entry_body (not raw content) so the
                        # LLM rewrite sees the "_Originally recorded:" provenance
                        # line when the timestamp was clamped.
                        new_entry=entry_body,
                        # Compile input is the PRE-append article — the append
                        # version just written already contains the new entry,
                        # so merging into it would feed the LLM a duplicate.
                        pre_append_content=existing_content,  # noqa: F821
                        # Concurrency token: the bytes this store() wrote. If
                        # the file differs when the compile finishes, a newer
                        # store won the race and the result is dropped.
                        expected_content=new_content,
                        provider_hint=(terminal_context or {}).get("provider"),
                    )
            except Exception as e:  # noqa: BLE001 — scheduling is best-effort
                logger.warning(f"wiki compile not scheduled (key={key}): {e}")

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
                    last_compiled_at=last_compiled_at,
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
            timestamp_clamped=timestamp_clamped,
        )

    # -------------------------------------------------------------------------
    # Deferred LLM compaction
    # -------------------------------------------------------------------------

    def _schedule_background_compile(
        self,
        *,
        scope: str,
        scope_id: Optional[str],
        key: str,
        new_entry: str,
        pre_append_content: str,
        expected_content: str,
        provider_hint: Optional[str],
    ) -> None:
        """Fire-and-forget the LLM compaction for one topic.

        Runs on the event loop when store() is awaited inside one (the API
        server), else on a worker thread for synchronous callers (CLI, tests).
        Never blocks store() and never raises into it.

        Debounce: at most one compile in flight per (scope, scope_id, key).
        A store that arrives while one is running is skipped — its content is
        already durable in the append version, and the racing compile's
        stale-check will drop the old result, so nothing is lost except a
        CLI invocation we'd have discarded anyway.
        """
        topic_token = (scope, scope_id, key)
        with self._compile_inflight_lock:
            if topic_token in self._compile_inflight:
                logger.debug(f"compile already in flight, skipping (key={key})")
                return
            self._compile_inflight.add(topic_token)

        async def _guarded() -> None:
            try:
                await self._run_background_compile(
                    scope=scope,
                    scope_id=scope_id,
                    key=key,
                    new_entry=new_entry,
                    pre_append_content=pre_append_content,
                    expected_content=expected_content,
                    provider_hint=provider_hint,
                )
            finally:
                with self._compile_inflight_lock:
                    self._compile_inflight.discard(topic_token)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        coro_factory = _guarded
        if loop is not None:
            try:
                task = loop.create_task(coro_factory())
                # Hold a reference so the task is not garbage-collected mid-run.
                self._compile_tasks.add(task)
                task.add_done_callback(self._compile_tasks.discard)
                return
            except Exception as e:  # noqa: BLE001
                logger.debug(f"background compile create_task failed (key={key}): {e}")
        # No running loop: drain on a daemon thread so the CLI subprocess does
        # not block the synchronous caller.
        try:
            threading.Thread(
                target=lambda: asyncio.run(coro_factory()),
                name=f"cao-compile-{key}",
                daemon=True,
            ).start()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"background compile thread failed (key={key}): {e}")

    async def _run_background_compile(
        self,
        *,
        scope: str,
        scope_id: Optional[str],
        key: str,
        new_entry: str,
        pre_append_content: str,
        expected_content: str,
        provider_hint: Optional[str],
    ) -> str:
        """Compile one topic via the CLI and write the result back if unchanged.

        The LLM merges ``new_entry`` into ``pre_append_content`` (the article
        as it stood BEFORE the append) — the on-disk file already contains the
        appended entry, so compiling from the file would feed a duplicate.
        The manual sweep (``compact``) passes the whole on-disk article as
        ``pre_append_content`` with an empty ``new_entry`` instead.

        Optimistic concurrency: the file must still hold ``expected_content``
        (the bytes the scheduling store() wrote) when the compile finishes,
        otherwise a newer store() won the race and this result is dropped.

        Returns a status string ("applied", "stale", "error", or
        "fallback:<reason>") — background callers ignore it; the sweep reports it.
        """
        from cli_agent_orchestrator.services import wiki_compiler
        from cli_agent_orchestrator.services.settings_service import get_compile_timeout_s

        def _audit(event_type: str, summary: str, **fields: str) -> None:
            try:
                from cli_agent_orchestrator.services.audit_log import write_audit_nowait

                write_audit_nowait(event_type, summary, key=key, **fields)
            except ImportError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.debug(f"compile audit write failed (key={key}): {e}")

        wiki_path = self.get_wiki_path(scope, scope_id, key)

        try:
            result = await wiki_compiler.compile(
                pre_append_content,
                new_entry,
                topic_key=key,
                timeout_s=get_compile_timeout_s(),
                provider_hint=provider_hint,
            )
        except Exception as e:  # noqa: BLE001 — compile is best-effort
            logger.warning(f"background wiki compile failed (key={key}): {e}")
            _audit("compile_error", "background compile raised")
            return "error"

        if not result.used_llm:
            _audit(
                "compile_fallback",
                f"compile fell back: {result.fallback_reason}",
                fallback_reason=str(result.fallback_reason or ""),
                elapsed_ms=str(result.elapsed_ms),
            )
            return f"fallback:{result.fallback_reason}"

        compiled_content = result.compiled_content

        # Second-pass cross-reference detection (See-Also). Only runs when
        # the first pass actually used the LLM — append mode and fallbacks
        # never reach this point. Failure is silent (non-blocking promise).
        # ``related_keys_value`` stays None on never-attempted/error so the
        # metadata upsert leaves any prior value intact; ``""`` on
        # success-with-empty is written (prevents endless retries).
        related_keys_value: Optional[str] = None
        try:
            cands = self._candidate_keys_for_topic(scope, scope_id, key)
            related_result = await wiki_compiler.find_related(
                compiled_content,
                candidate_keys=cands,
                topic_key=key,
                timeout_s=get_compile_timeout_s(),
                provider_hint=provider_hint,
            )
            if related_result.used_llm:
                related_keys_value = ",".join(related_result.related_keys)
                if related_result.related_keys:
                    see_also = self._render_see_also(
                        related_result.related_keys,
                        topic_scope=scope,
                        topic_scope_id=scope_id,
                    )
                    stripped = self._strip_existing_see_also(compiled_content)
                    compiled_content = stripped + "\n" + see_also if see_also else stripped
            else:
                logger.info("find_related_fallback reason=%s", related_result.fallback_reason)
            _audit(
                "find_related_completed",
                f"related: {len(related_result.related_keys)} keys",
                n_related=str(len(related_result.related_keys)),
                used_llm=str(related_result.used_llm).lower(),
            )
        except Exception as e:  # noqa: BLE001 — non-blocking promise
            logger.warning(f"find_related raised, ignoring: {e}")

        topic_lock_path = wiki_path.parent / f".{wiki_path.stem}.lock"
        try:
            topic_lock_fd = open(topic_lock_path, "w")
        except OSError:
            return "error"
        try:
            fcntl.flock(topic_lock_fd, fcntl.LOCK_EX)
            # Drop the result if a concurrent store() changed the file.
            try:
                current = wiki_path.read_text(encoding="utf-8")
            except OSError:
                return "error"
            if current != expected_content:
                logger.info(f"background compile stale, dropping (key={key})")
                _audit("compile_stale_dropped", "newer store won the race")
                return "stale"
            tmp_path = wiki_path.parent / f".{wiki_path.stem}.compile.tmp"
            tmp_path.write_text(compiled_content, encoding="utf-8")
            os.replace(str(tmp_path), str(wiki_path))
            try:
                self._upsert_metadata(
                    key=key,
                    memory_type=self._memory_type_from_file(compiled_content),
                    scope=scope,
                    scope_id=scope_id,
                    file_path=str(wiki_path),
                    tags=self._tags_from_file(compiled_content),
                    source_provider=None,
                    source_terminal_id=None,
                    token_estimate=len(compiled_content) // 4,
                    last_compiled_at=datetime.now(timezone.utc),
                    related_keys=related_keys_value,
                    preserve_provenance=True,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(f"compile metadata stamp failed (key={key}): {e}")
            logger.info(f"background compile applied (key={key})")
            _audit(
                "compile_completed",
                "compile used_llm=true",
                used_llm="true",
                elapsed_ms=str(result.elapsed_ms),
            )
            return "applied"
        finally:
            try:
                fcntl.flock(topic_lock_fd, fcntl.LOCK_UN)
            finally:
                topic_lock_fd.close()

    async def compact(
        self,
        scope: str = "global",
        scope_id: Optional[str] = None,
        key: Optional[str] = None,
        terminal_context: Optional[dict] = None,
    ) -> dict:
        """Manually compact wiki topics via the LLM compiler (repair sweep).

        Catches every topic the fire-and-forget background path missed —
        dropped daemon threads, timeouts, stale races. A topic needs
        compaction when its metadata row has ``last_compiled_at`` NULL or
        older than ``updated_at``. With ``key`` set, compacts that single
        topic unconditionally.

        Runs compiles sequentially (one CLI process at a time) and AWAITS
        them — this is the explicit, interactive path, not the deferred one.
        Returns ``{key: status}`` per topic plus a ``_summary`` count.
        """
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        valid_scopes = {s.value for s in MemoryScope}
        if scope not in valid_scopes:
            raise ValueError(f"scope must be one of {valid_scopes}, got '{scope}'")
        provider_hint = (terminal_context or {}).get("provider")

        candidates: list = []
        try:
            with self._get_db_session() as db:
                q = db.query(MemoryMetadataModel).filter(MemoryMetadataModel.scope == scope)
                if scope_id is not None:
                    q = q.filter(MemoryMetadataModel.scope_id == scope_id)
                if key is not None:
                    q = q.filter(MemoryMetadataModel.key == self._sanitize_key(key))
                for row in q.all():
                    if key is None and row.last_compiled_at is not None:
                        last_compiled = row.last_compiled_at
                        updated = row.updated_at
                        if last_compiled.tzinfo is None:
                            last_compiled = last_compiled.replace(tzinfo=timezone.utc)
                        if updated.tzinfo is None:
                            updated = updated.replace(tzinfo=timezone.utc)
                        if last_compiled >= updated:
                            continue
                    candidates.append((row.key, row.scope_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"compact: candidate query failed: {e}")
            return {"_summary": {"error": str(e)}}

        results: dict = {}
        for topic_key, topic_scope_id in candidates:
            wiki_path = self.get_wiki_path(scope, topic_scope_id, topic_key)
            try:
                article = wiki_path.read_text(encoding="utf-8")
            except OSError:
                results[topic_key] = "missing-file"
                continue
            # Whole-article compaction: the article is the input, no new
            # entry. The optimistic-concurrency token is the article itself.
            results[topic_key] = await self._run_background_compile(
                scope=scope,
                scope_id=topic_scope_id,
                key=topic_key,
                new_entry="",
                pre_append_content=article,
                expected_content=article,
                provider_hint=provider_hint,
            )

        summary: dict = {}
        for status in results.values():
            summary[status] = summary.get(status, 0) + 1
        results["_summary"] = summary
        return results

    @staticmethod
    def _memory_type_from_file(content: str) -> str:
        m = re.search(r"<!-- id: [a-f0-9\-]+ \| scope: [^|]+ \| type: ([^|]+) \|", content)
        return m.group(1).strip() if m else "reference"

    @staticmethod
    def _tags_from_file(content: str) -> str:
        m = re.search(r"\| tags: ([^>]*)-->", content)
        return m.group(1).strip() if m else ""

    # -------------------------------------------------------------------------
    # Cross-references (See-Also)
    # -------------------------------------------------------------------------

    RELATED_FANOUT_CAP = 5
    """Per-build fanout cap for ``get_memory_context_for_terminal``.

    Tuned to ``MEMORY_MAX_PER_SCOPE * 0.5`` so related expansion never
    exceeds 50% of any scope's slot count. ``recall(include_related=True)``
    is NOT subject to this cap (per-primary parse cap of 3 is sufficient).
    """

    @staticmethod
    def _parse_related_keys(raw: Optional[str], scope: str = "<unknown>") -> list:
        """Defence-in-depth read of ``MemoryMetadataModel.related_keys``.

        Short-circuit if oversized, drop any token that fails the sanitiser
        round-trip, dedup, cap at 3. NEVER raises; returns ``[]`` for
        NULL/empty.
        """
        if not raw:
            return []
        if len(raw) > 1024:
            logger.warning("related_keys_oversized scope=%s len=%d", scope, len(raw))
            raw = raw[:1024]
        out: list = []
        seen: set = set()
        for part in raw.split(","):
            p = part.strip()
            if not p:
                continue
            try:
                canonical = MemoryService._sanitize_key(p)
            except ValueError:
                logger.warning("related_keys_unsanitised scope=%s", scope)
                continue
            if canonical != p:
                logger.warning("related_keys_unsanitised scope=%s", scope)
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            out.append(canonical)
            if len(out) >= 3:
                break
        return out

    def _candidate_keys_for_topic(
        self, scope: str, scope_id: Optional[str], exclude_key: str
    ) -> list:
        """Build the candidate-key set fed to ``find_related``.

        Pulls all keys from SQLite metadata for the same scope/scope_id —
        ``index.md`` is derived state, SQLite is source of truth. Excludes
        the topic key (self-reference). Caps at the 200 most-recently
        updated rows.
        """
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        try:
            with self._get_db_session() as db:
                q = db.query(MemoryMetadataModel).filter(MemoryMetadataModel.scope == scope)
                if scope_id is not None:
                    q = q.filter(MemoryMetadataModel.scope_id == scope_id)
                else:
                    q = q.filter(MemoryMetadataModel.scope_id.is_(None))
                rows = q.order_by(MemoryMetadataModel.updated_at.desc()).limit(200).all()
                return [r.key for r in rows if r.key != exclude_key]
        except Exception as e:
            logger.warning(f"candidate-key load failed: {e}")
            return []

    def _render_see_also(
        self,
        related_keys: list,
        *,
        topic_scope: str,
        topic_scope_id: Optional[str],
    ) -> str:
        """Render the ``## See Also`` block for atomic-write inclusion.

        Every emitted line MUST match ``wiki_compiler.SEE_ALSO_LINK_RE``.
        Lines failing the regex are dropped with a WARNING (defence in
        depth). Empty input → empty string (no empty section).
        """
        from cli_agent_orchestrator.services.wiki_compiler import SEE_ALSO_LINK_RE

        if not related_keys:
            return ""

        lines: list = ["## See Also"]
        for k in related_keys:
            try:
                canonical = self._sanitize_key(k)
            except ValueError:
                logger.warning("see_also: dropping unsanitisable key")
                continue
            # The source article and its related siblings share the same
            # scope directory because candidates are limited to the same
            # scope. Render a relative path the caller can resolve against
            # the source file's directory: ``../<scope_id>/<key>.md``.
            scope_id_seg = topic_scope_id or topic_scope
            line = f"- [{canonical}](../{scope_id_seg}/{canonical}.md)"
            if not SEE_ALSO_LINK_RE.match(line):
                logger.warning("see_also: dropping non-canonical line shape")
                continue
            lines.append(line)
        if len(lines) == 1:
            return ""
        return "\n".join(lines) + "\n"

    @staticmethod
    def _strip_existing_see_also(content: str) -> str:
        """Remove an existing ``## See Also`` section (heading + bullet list).

        Each compile rewrites the section from scratch, so any pre-existing
        block must be excised first (idempotency). The block extends from
        ``## See Also`` to the next ``##`` heading or EOF. Trailing
        whitespace is normalised.
        """
        lines = content.splitlines(keepends=False)
        out: list = []
        skip = False
        for line in lines:
            stripped = line.rstrip()
            if stripped == "## See Also":
                skip = True
                continue
            if skip and stripped.startswith("## "):
                skip = False
            if skip:
                continue
            out.append(line)
        # Drop trailing empty lines created by the strip; reproduce a
        # single trailing newline shape compatible with append-mode bytes.
        rendered = "\n".join(out).rstrip() + "\n"
        return rendered

    def _related_keys_lookup(self, keys: list, scope: str, scope_id: Optional[str]) -> dict:
        """Return ``{key: related_keys_raw}`` for the given keys in scope.

        Enriches recall/injection primaries (built from wiki files, which
        carry no cross-reference data) with their SQLite ``related_keys``
        cell. Missing rows are simply absent from the returned dict —
        callers iterate via ``.get(k)``.
        """
        if not keys:
            return {}
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        try:
            with self._get_db_session() as db:
                q = db.query(MemoryMetadataModel).filter(
                    MemoryMetadataModel.key.in_(list(set(keys))),
                    MemoryMetadataModel.scope == scope,
                )
                if scope_id is not None:
                    q = q.filter(MemoryMetadataModel.scope_id == scope_id)
                else:
                    q = q.filter(MemoryMetadataModel.scope_id.is_(None))
                return {r.key: r.related_keys for r in q.all()}
        except Exception as e:
            logger.debug(f"_related_keys_lookup failed: {e}")
            return {}

    def _load_related_memory(
        self, key: str, scope: str, scope_id: Optional[str]
    ) -> "Optional[Memory]":
        """Load a single related Memory with the same guards as primary recall.

        The key is sanitised and the wiki path is resolved through
        ``get_wiki_path`` (which enforces base-dir containment, including
        symlink escapes). Silent skip on any failure — missing or denied
        returns None with no differential errors. ALWAYS returns a fresh
        Memory instance (no cache across recalls).
        """
        try:
            sanitised = self._sanitize_key(key)
        except ValueError:
            return None
        if sanitised != key:
            return None
        try:
            wiki_path = self.get_wiki_path(scope, scope_id, sanitised)
        except ValueError:
            return None
        try:
            if not wiki_path.exists():
                return None
            resolved = Path(os.path.realpath(str(wiki_path)))
            # Validate against the scope directory the key legitimately lives
            # in, not the global memory base — a symlink planted inside one
            # scope's tree must not leak another scope's (or project's)
            # memory even when its target stays under the base dir. The
            # boundary is constructed from scope/scope_id, never derived from
            # the on-disk path (get_wiki_path resolves THROUGH a symlink, so
            # the path's own parent would be the symlink target's directory).
            scope_dir = self._get_project_dir(scope, scope_id) / "wiki" / scope
            if scope in (MemoryScope.SESSION.value, MemoryScope.AGENT.value) and scope_id:
                scope_dir = scope_dir / scope_id
            scope_dir_real = os.path.realpath(str(scope_dir))
            if not str(resolved).startswith(scope_dir_real + os.sep):
                return None
            file_content = resolved.read_text(encoding="utf-8")
            entry = {
                "key": sanitised,
                "scope": scope,
                "scope_id": scope_id,
                "memory_type": "",
                "tags": "",
            }
            return self._parse_wiki_file(resolved, file_content, entry)
        except Exception as e:
            logger.debug(f"_load_related_memory failed key={sanitised}: {e}")
            return None

    def _effective_scope_id(self, memory: Memory) -> Optional[str]:
        """Resolve the SQLite scope_id for a wiki-parsed Memory.

        Memories built from wiki files carry ``scope_id`` only for session/
        agent scopes (embedded in the path). Project rows in SQLite store the
        project hash, which for a parsed Memory must be recovered from the
        file's container directory (``<base>/<project-hash>/wiki/...``).
        """
        if memory.scope_id is not None:
            return memory.scope_id
        if memory.scope == MemoryScope.PROJECT.value and memory.file_path:
            try:
                rel = Path(memory.file_path).resolve().relative_to(self.base_dir.resolve())
                container = rel.parts[0]
                if container != "global":
                    return container
            except Exception:
                return None
        return None

    def _expand_related(self, primaries: list) -> list:
        """One-level cross-reference traversal for ``recall(include_related=True)``.

        Appended AFTER the limit slice — the caller asked for related, we
        deliver in addition. ``visited`` is seeded with primary keys for
        cycle/dedup prevention. Depth = 1; no recursion. NOT subject to
        ``RELATED_FANOUT_CAP`` (the per-primary parse cap of 3 suffices).
        """
        if not primaries:
            return primaries
        visited: set = {m.key for m in primaries}
        extras: list = []
        # Group primaries by (scope, scope_id) so each SQLite lookup is
        # batched per scope rather than per row.
        groups: dict = {}
        for m in primaries:
            groups.setdefault((m.scope, self._effective_scope_id(m)), []).append(m)
        lookups: dict = {}
        for (g_scope, g_scope_id), members in groups.items():
            lookups[(g_scope, g_scope_id)] = self._related_keys_lookup(
                [m.key for m in members], g_scope, g_scope_id
            )
        for primary in primaries:
            primary_scope_id = self._effective_scope_id(primary)
            raw = lookups.get((primary.scope, primary_scope_id), {}).get(primary.key)
            for rk in self._parse_related_keys(raw, scope=primary.scope):
                if rk in visited:
                    continue
                visited.add(rk)
                related_mem = self._load_related_memory(rk, primary.scope, primary_scope_id)
                if related_mem is None:
                    continue
                related_mem.is_related = True
                extras.append(related_mem)
        return primaries + extras

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
        sort_by: str = "recency",
        include_related: bool = False,
    ) -> list[Memory]:
        """Recall memories matching query and filters.

        ``search_mode``:
          - ``metadata``: substring match against key/tags/content via index.md walk.
          - ``bm25``: BM25 ranking over wiki bodies (content-aware).
          - ``hybrid``: metadata results first, then BM25 fills with what metadata missed.

        ``sort_by`` (orthogonal to ``search_mode``):
          - ``recency``: Phase 1/2 ordering (``-updated_at`` + scope precedence).
            DEFAULT — byte-identical to the pre-scoring behaviour.
          - ``score``: composite 3-factor (BM25 + recency + usage).
          - ``usage``: most-accessed first.

        ``include_related``: when True, each result's
        ``related_keys`` cross-references are loaded (one level, same scope)
        and appended after the primary results, marked ``is_related=True``.
        Default False is byte-identical to the non-expanded path.

        Returns ``[]`` when ``memory.enabled`` is False (U5 / SC-6).
        """
        if not _is_memory_enabled():
            return []

        if search_mode not in VALID_SEARCH_MODES:
            raise ValueError(
                f"Invalid search_mode {search_mode!r}; expected one of {VALID_SEARCH_MODES}"
            )

        # Whitelist sort_by BEFORE any DB/file work so a
        # typo surfaces to the caller (MCP tool) rather than silently
        # falling back.
        from cli_agent_orchestrator.services.memory_scoring import validate_sort_by

        validate_sort_by(sort_by)

        if search_mode == "bm25":
            if not query:
                return []
            scope_id = (
                self.resolve_scope_id(scope, terminal_context)
                if scope and scope != MemoryScope.GLOBAL.value and terminal_context
                else None
            )
            bm25_only = self._bm25_search(
                query=query,
                scope=scope,
                scope_id=scope_id,
                memory_type=memory_type,
                limit=limit,
                exclude_keys=set(),
                terminal_context=terminal_context,
                scan_all=scan_all,
            )
            return self._apply_sort_and_increment(
                bm25_only,
                scope,
                sort_by,
                limit,
                include_related=include_related,
                query=query,
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
            return self._apply_sort_and_increment(
                metadata_results,
                scope,
                sort_by,
                limit,
                include_related=include_related,
                query=query,
                terminal_context=terminal_context,
                scan_all=scan_all,
            )

        # hybrid: top up with BM25 hits not already in metadata results
        exclude_keys = {m.key for m in metadata_results}
        remaining = max(0, limit - len(metadata_results))
        if remaining == 0:
            return self._apply_sort_and_increment(
                metadata_results,
                scope,
                sort_by,
                limit,
                include_related=include_related,
                query=query,
                terminal_context=terminal_context,
                scan_all=scan_all,
            )

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
        return self._apply_sort_and_increment(
            metadata_results + bm25_results,
            scope,
            sort_by,
            limit,
            include_related=include_related,
            query=query,
            terminal_context=terminal_context,
            scan_all=scan_all,
        )

    def _apply_sort_and_increment(
        self,
        results: list,
        scope: Optional[str],
        sort_by: str,
        limit: int,
        include_related: bool = False,
        query: Optional[str] = None,
        terminal_context: Optional[dict] = None,
        scan_all: bool = False,
    ) -> list:
        """Apply sort mode, slice, then best-effort access bump.

        ``recency`` is the byte-identical Phase 1/2 path: the per-source
        sorts in ``_metadata_recall``/``_bm25_search`` already produced that
        order, so we leave ``results`` untouched and only slice. ``score``/
        ``usage`` enrich ``access_count`` from SQLite (recall builds Memory
        objects from wiki files, which carry no usage data), compute the
        composite, then apply a stable scope-precedence pass so scope
        dominance holds in every mode.
        """
        from cli_agent_orchestrator.services.memory_scoring import (
            SCOPE_PRECEDENCE,
            normalise_bm25_scores,
            score_memory,
        )

        if sort_by != "recency":
            self._enrich_access_counts(results)
            now_utc = datetime.now(timezone.utc)
            if sort_by == "usage":
                results.sort(
                    key=lambda m: (
                        -int(getattr(m, "access_count", 0) or 0),
                        -m.updated_at.timestamp(),
                        m.key,
                    )
                )
            else:  # "score"
                # Genuine 3-factor: score the WHOLE candidate set against the
                # query in one corpus, normalise per-batch, and feed that into
                # the lexical factor. Keyed by full identity so two same-slug
                # rows in different scopes never collide. No query / no
                # rank_bm25 → empty map → degrades to recency+usage.
                norm_bm25 = normalise_bm25_scores(
                    self._bm25_relevance(query, results, terminal_context, scope, scan_all)
                )
                composite = {
                    self._identity(m): score_memory(
                        norm_bm25.get(self._identity(m), 0.0),
                        m.updated_at,
                        int(getattr(m, "access_count", 0) or 0),
                        now=now_utc,
                    )
                    for m in results
                }
                results.sort(
                    key=lambda m: (
                        -composite[self._identity(m)],
                        -m.updated_at.timestamp(),
                        -int(getattr(m, "access_count", 0) or 0),
                        m.key,
                    )
                )
            if not scope:
                # Stable scope-precedence sort AFTER score/usage preserves
                # within-scope ordering while enforcing scope dominance.
                results.sort(key=lambda m: SCOPE_PRECEDENCE.get(m.scope, 99))

        sliced = results[:limit]

        # Best-effort access_count increment (non-blocking).
        try:
            self._increment_access_count(sliced)
        except Exception as e:  # noqa: BLE001 — non-blocking promise
            logger.warning(
                "memory_increment_failed attempted=%d scope=%s",
                len(sliced),
                scope or "<all>",
            )
            logger.debug(f"recall increment failure detail: {e}")

        # Opt-in cross-reference expansion, appended AFTER the
        # limit slice (the caller asked for related; we deliver in addition).
        if include_related and sliced:
            try:
                sliced = self._expand_related(sliced)
            except Exception as e:  # noqa: BLE001 — non-blocking promise
                logger.warning(f"related expansion failed, returning primaries: {e}")

        return sliced

    def _identity(self, m) -> tuple:
        """Full memory identity ``(key, scope, scope_id)`` matching SQLite.

        Matches the metadata table's uniqueness constraint. Used to key
        usage enrich/increment and the composite-score dict so same-slug
        memories in different scopes never collide.

        Uses ``_effective_scope_id`` (not raw ``m.scope_id``) so PROJECT rows
        — whose parsed Memory carries ``scope_id=None`` but whose SQLite row
        stores the project hash — match correctly. Mirrors the recovery the
        related-key expansion already performs.
        """
        return (m.key, m.scope, self._effective_scope_id(m))

    def _enrich_access_counts(self, memories: list) -> None:
        """Populate ``access_count`` on Memory objects from SQLite.

        Recall builds Memory objects by parsing wiki markdown files, which do
        not carry usage data; the scoring/usage sort needs ``access_count``
        from the metadata table. Best-effort: on any DB error the objects keep
        their default ``0`` and scoring degrades to recency-like behaviour.
        """
        if not memories:
            return
        try:
            from cli_agent_orchestrator.clients.database import MemoryMetadataModel

            keys = list({m.key for m in memories})
            with self._get_db_session() as db:
                rows = db.query(MemoryMetadataModel).filter(MemoryMetadataModel.key.in_(keys)).all()
                # Key the lookup by FULL identity (key, scope, scope_id) — the
                # table's uniqueness constraint. A bare-key or (key, scope)
                # match would read a same-slug row from another scope/project.
                by_identity = {(r.key, r.scope, r.scope_id): int(r.access_count or 0) for r in rows}
            for m in memories:
                m.access_count = by_identity.get(self._identity(m), 0)
        except Exception as e:  # noqa: BLE001 — best-effort enrichment
            logger.debug(f"access_count enrichment skipped: {e}")

    def _increment_access_count(self, memories: list) -> None:
        """Batch UPDATE access_count and last_accessed_at.

        60s rate-limit via server-side ``julianday("now")``,
        not caller clock. Each row increments at most once per recall and at
        most once per 60s. Filtered rows still appear in results — only the
        increment is suppressed.

        Re-raises on DB error so the caller's handler produces the counts-only
        WARNING with row counts only (no content bytes).
        """
        if not memories:
            return
        from sqlalchemy import and_, func, or_, update

        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        # Match on FULL identity (key, scope, scope_id), not bare key — the
        # table's uniqueness constraint. A key-only UPDATE would bump every
        # same-slug row across scopes/projects (cross-scope contamination).
        # OR-of-ANDs is used over tuple_().in_() because SQLite tuple-IN
        # compares scope_id with ``=``, which never matches the NULL that
        # global rows carry; ``.is_(None)`` is required for those.
        identities = {self._identity(m) for m in memories}
        identity_clause = or_(
            *[
                and_(
                    MemoryMetadataModel.key == k,
                    MemoryMetadataModel.scope == s,
                    (
                        MemoryMetadataModel.scope_id == sid
                        if sid is not None
                        else MemoryMetadataModel.scope_id.is_(None)
                    ),
                )
                for (k, s, sid) in identities
            ]
        )
        with self._get_db_session() as db:
            stmt = (
                update(MemoryMetadataModel)
                .where(identity_clause)
                .where(
                    or_(
                        MemoryMetadataModel.last_accessed_at.is_(None),
                        func.julianday("now") - func.julianday(MemoryMetadataModel.last_accessed_at)
                        >= 60.0 / 86400.0,
                    )
                )
                .values(
                    access_count=MemoryMetadataModel.access_count + 1,
                    last_accessed_at=func.datetime("now"),
                )
            )
            db.execute(stmt)
            db.commit()

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

            wiki_dir = project_dir / "wiki"
            wiki_resolved = os.path.realpath(str(wiki_dir))

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
                wiki_file = wiki_dir / entry["relative_path"]
                resolved_wiki = Path(os.path.realpath(str(wiki_file)))
                # Guard against a crafted/corrupted index entry (e.g.
                # ``[x](../../../../etc/passwd)``) escaping this scope's wiki
                # directory and leaking an arbitrary out-of-base file as a
                # "memory". Mirrors get_memory_context_for_terminal: skip the
                # escaping entry rather than raising, keeping recall resilient
                # to a corrupted index.
                if not str(resolved_wiki).startswith(wiki_resolved + os.sep):
                    logger.warning(
                        f"Path traversal in index entry rejected: {entry.get('relative_path')}"
                    )
                    continue
                if not resolved_wiki.exists():
                    continue

                file_content = resolved_wiki.read_text(encoding="utf-8")

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
                MemoryScope.FEDERATED.value: 4,
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

    def _bm25_relevance(
        self,
        query: Optional[str],
        memories: list,
        terminal_context: Optional[dict],
        scope: Optional[str],
        scan_all: bool,
    ) -> dict:
        """Raw BM25 score per memory identity for the score-mode lexical factor.

        Scores against ``query`` over the FULL wiki corpus in the search dirs
        (the same corpus ``_bm25_search`` builds), not just the candidate
        ``memories``. The full corpus is essential for correct IDF: a candidate-
        only corpus collapses when every candidate contains the query term
        (df == N → negative IDF → all-zero). Scores are returned only for the
        identities present in ``memories``, keyed by ``(key, scope, scope_id)``,
        so the relevance magnitude is comparable across rows regardless of
        whether each arrived via metadata substring match or BM25 top-up.

        Returns ``{}`` when not applicable (no query/candidates, ``rank_bm25``
        absent, no corpus); callers treat a missing key as ``0.0`` so score
        degrades to recency+usage. A document only scores when at least one
        query token actually appears in it — BM25 IDF can go negative on tiny
        corpora, so we cannot gate on score > 0 alone.
        """
        if not query or not memories:
            return {}
        try:
            from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("rank_bm25 not installed; score-mode BM25 factor disabled")
            return {}

        query_tokens = self._bm25_tokenize(query)
        if not query_tokens:
            return {}
        query_token_set = set(query_tokens)

        # Build the corpus over every wiki file in the search dirs so IDF is
        # computed against the real document population, not just the matches.
        # A candidate-only corpus collapses when every candidate contains the
        # query term (df == N → negative IDF → all-zero).
        search_dirs = self._get_search_dirs(scope, terminal_context, scan_all=scan_all)
        wanted = {self._identity(m) for m in memories}
        identities: list[Optional[tuple]] = []  # parallel to corpus_tokens
        corpus_tokens: list[list[str]] = []
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
                try:
                    tokens = self._bm25_tokenize(wiki_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                file_scope = rel_parts[0]
                file_scope_id: Optional[str] = None
                if (
                    file_scope in (MemoryScope.SESSION.value, MemoryScope.AGENT.value)
                    and len(rel_parts) >= 3
                ):
                    file_scope_id = rel_parts[1]
                elif file_scope == MemoryScope.PROJECT.value:
                    # Project rows store the project-hash container as scope_id.
                    file_scope_id = project_dir.name if project_dir.name != "global" else None
                corpus_tokens.append(tokens)
                # Only track identities we'll report; others stay in the corpus
                # (for IDF) but map to None so they're skipped on readback.
                identity = (wiki_file.stem, file_scope, file_scope_id)
                identities.append(identity if identity in wanted else None)

        if not corpus_tokens:
            return {}

        try:
            scores = BM25Okapi(corpus_tokens).get_scores(query_tokens)
        except Exception as e:  # noqa: BLE001 — best-effort lexical factor
            logger.debug(f"score-mode BM25 scoring skipped: {e}")
            return {}

        out: dict = {}
        for i, ident in enumerate(identities):
            # Report a wanted row only when it is a real lexical match.
            if ident is not None and query_token_set & set(corpus_tokens[i]):
                out[ident] = float(scores[i])
        return out

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

        # Include the machine-wide federated tier when present. The
        # ``.exists()`` guard preserves the byte-identical search-dir
        # invariant: with no federated memories on disk, the dir list is
        # unchanged from pre-federation behaviour.
        federated_dir = self.base_dir / "federated"
        if federated_dir.exists() and federated_dir not in dirs:
            dirs.append(federated_dir)

        if scan_all:
            # Enumerate all project-hash dirs (for CLI use where user owns the filesystem)
            if self.base_dir.exists():
                for child in sorted(self.base_dir.iterdir()):
                    if (
                        child.is_dir()
                        and child.name != "global"
                        and child.name != "federated"
                        and child not in dirs
                    ):
                        dirs.append(child)
        elif terminal_context:
            # Include the specific project dir for this terminal's cwd
            project_scope_id = self.resolve_scope_id("project", terminal_context)
            if project_scope_id:
                # ``project_scope_id`` is context-derived; validate + confine
                # under the base before touching the filesystem.
                try:
                    project_dir = Path(
                        safe_join_under_base(
                            str(self.base_dir), project_scope_id, description="scope_id"
                        )
                    )
                except ValueError:
                    project_dir = None
                if project_dir is not None and project_dir.exists() and project_dir not in dirs:
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
                        # Alias names originate from stored git/cwd identities;
                        # validate + confine each before filesystem access.
                        try:
                            alias_dir = Path(
                                safe_join_under_base(
                                    str(self.base_dir),
                                    alias["alias"],
                                    description="alias",
                                )
                            )
                        except (ValueError, KeyError, TypeError):
                            continue
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
        related_added_total = 0  # global per-build fanout cap

        for scope_val in scopes_in_order:
            scope_id = self.resolve_scope_id(scope_val, terminal_context)
            project_dir = self._get_project_dir(scope_val, scope_id)
            wiki_dir = project_dir / "wiki"
            wiki_resolved = os.path.realpath(str(wiki_dir))
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
                resolved_wiki = Path(os.path.realpath(str(wiki_file)))
                # Guard against a crafted/corrupted index entry (e.g.
                # ``../<other-project>/wiki/...``) escaping this scope's wiki
                # directory and leaking another project's memory. Validate
                # against the per-scope wiki dir, not the global memory base.
                if not str(resolved_wiki).startswith(wiki_resolved + os.sep):
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

            # One-level cross-reference expansion. Looks up ``related_keys``
            # for primary entries via SQLite (source of truth, not the
            # rendered ``## See Also`` markdown), expands within the same
            # scope budget, dedups + cycle-blocks via ``visited``, and caps
            # the total related articles added across this entire context
            # build at ``RELATED_FANOUT_CAP``. Any lookup failure is silent.
            try:
                related_lookup = self._related_keys_lookup(
                    [m.key for m in scope_memories], scope_val, scope_id
                )
            except Exception as e:  # noqa: BLE001 — non-blocking
                logger.debug(f"related_keys lookup failed: {e}")
                related_lookup = {}

            visited: set = {m.key for m in scope_memories}
            primary_snapshot = list(scope_memories)
            for primary in primary_snapshot:
                if related_added_total >= self.RELATED_FANOUT_CAP:
                    logger.info("related_fanout_cap_reached added=%d", related_added_total)
                    break
                raw = related_lookup.get(primary.key)
                for rk in self._parse_related_keys(raw, scope=scope_val):
                    if related_added_total >= self.RELATED_FANOUT_CAP:
                        break
                    if rk in visited:
                        continue
                    visited.add(rk)
                    related_mem = self._load_related_memory(rk, scope_val, scope_id)
                    if related_mem is None:
                        continue
                    related_mem.is_related = True  # transient render label
                    scope_memories.append(related_mem)
                    related_added_total += 1

            scope_used_chars = 0
            for mem in scope_memories:
                tag = " [related]" if getattr(mem, "is_related", False) else ""
                line = f"- [{mem.scope}] {mem.key}{tag}: {mem.content}"
                line_len = len(line) + 1
                if scope_used_chars + line_len > scope_char_cap:
                    if getattr(mem, "is_related", False):
                        # Never truncate mid-list for a related extra; skip
                        # it and try the next (possibly shorter) one.
                        continue
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
            from cli_agent_orchestrator.services.status_monitor import status_monitor

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
                if status_monitor.get_status(cm["id"]) != TerminalStatus.IDLE:
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
                    if status_monitor.get_status(cm["id"]) in (
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

    # -------------------------------------------------------------------------
    # Archive export/import (#345 D6) — thin delegators, no format logic here
    # -------------------------------------------------------------------------

    def export_memories(
        self,
        fmt: str,
        scope: str,
        scope_id: Optional[str],
        dest: Path,
        include_history: bool = False,
        redact: bool = False,
        prune: bool = False,
    ) -> ExportReport:
        """Export one scope through the archive backend registered as ``fmt``.

        Raises ``ValueError`` on unknown format names (registry contract);
        the CLI/API boundary maps it to a user-facing error.
        """
        from cli_agent_orchestrator.services.memory_archive import get_backend

        backend = get_backend(fmt)(self)
        return backend.export_bundle(scope, scope_id, dest, include_history, redact, prune=prune)

    def import_memories(
        self,
        fmt: str,
        src: Path,
        target_scope: str,
        conflict_policy: str = "skip",
        dry_run: bool = False,
        terminal_context: Optional[dict] = None,
    ) -> ImportReport:
        """Import an archive bundle through the backend registered as ``fmt``."""
        from cli_agent_orchestrator.services.memory_archive import get_backend

        backend = get_backend(fmt)(self)
        return backend.import_bundle(
            src, target_scope, conflict_policy, dry_run, terminal_context=terminal_context
        )

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
                ctx: dict[str, Any] = {
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
