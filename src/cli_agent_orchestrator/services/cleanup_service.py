"""Cleanup service for old terminals, messages, and logs."""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_agent_orchestrator.clients.database import InboxModel, SessionLocal, TerminalModel
from cli_agent_orchestrator.constants import (
    LOG_DIR,
    MEMORY_BASE_DIR,
    RETENTION_DAYS,
    TERMINAL_LOG_DIR,
)

logger = logging.getLogger(__name__)


def cleanup_old_data():
    """Clean up terminals, inbox messages, and log files older than RETENTION_DAYS."""
    try:
        cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
        logger.info(
            f"Starting cleanup of data older than {RETENTION_DAYS} days (before {cutoff_date})"
        )

        # Clean up old terminals
        with SessionLocal() as db:
            deleted_terminals = (
                db.query(TerminalModel).filter(TerminalModel.last_active < cutoff_date).delete()
            )
            db.commit()
            logger.info(f"Deleted {deleted_terminals} old terminals from database")

        # Clean up old inbox messages
        with SessionLocal() as db:
            deleted_messages = (
                db.query(InboxModel).filter(InboxModel.created_at < cutoff_date).delete()
            )
            db.commit()
            logger.info(f"Deleted {deleted_messages} old inbox messages from database")

        # Clean up old terminal log files
        terminal_logs_deleted = 0
        if TERMINAL_LOG_DIR.exists():
            for pattern in ("*.log", "*.scrollback", "*.snapshot.json"):
                for log_file in TERMINAL_LOG_DIR.glob(pattern):
                    if log_file.stat().st_mtime < cutoff_date.timestamp():
                        log_file.unlink()
                        terminal_logs_deleted += 1
        logger.info(f"Deleted {terminal_logs_deleted} old terminal log files")

        # Clean up old server log files
        server_logs_deleted = 0
        if LOG_DIR.exists():
            for log_file in LOG_DIR.glob("*.log"):
                if log_file.stat().st_mtime < cutoff_date.timestamp():
                    log_file.unlink()
                    server_logs_deleted += 1
        logger.info(f"Deleted {server_logs_deleted} old server log files")

        logger.info("Cleanup completed successfully")

    except Exception as e:
        logger.error(f"Error during cleanup: {e}")


# =============================================================================
# Memory Cleanup — tiered retention
# =============================================================================

# Scope-keyed retention policy. ``user`` and ``feedback`` memory_types
# are operator-curated and stay forever regardless of scope. Anything
# else expires per the scope of the entry.
SCOPE_RETENTION_DAYS: dict[str, int | None] = {
    "global": None,
    "agent": None,
    "project": 90,
    "session": 14,
}
PERMANENT_MEMORY_TYPES: frozenset[str] = frozenset({"user", "feedback"})


async def cleanup_expired_memories() -> None:
    """Delete expired memories based on scope-keyed retention policy.

    - session scope: 14 days
    - project scope: 90 days
    - global scope:  never expires
    - agent scope:   never expires
    - memory_type ``user`` or ``feedback``: never expires (regardless of scope)

    Idempotent — safe to run multiple times.
    """
    import asyncio

    try:
        now = datetime.now(timezone.utc)
        expired_count = 0

        if not MEMORY_BASE_DIR.exists():
            return

        # Lazy-import to avoid circular imports at module level
        from cli_agent_orchestrator.services.memory_service import MemoryService

        memory_service = MemoryService(base_dir=MEMORY_BASE_DIR)

        # Walk project dirs: {MEMORY_BASE_DIR}/{project_dir}/wiki/index.md
        # Glob and parse are sync I/O; offload to a thread so the event
        # loop stays responsive when there are many projects.
        index_paths = await asyncio.to_thread(lambda: list(MEMORY_BASE_DIR.glob("*/wiki/index.md")))
        for index_path in index_paths:
            expired_entries = await asyncio.to_thread(_find_expired_entries, index_path, now)
            if not expired_entries:
                continue

            # Extract scope_id from path: .../memory/{scope_id}/wiki/index.md
            # "global" dir → scope_id=None, project hash dirs → scope_id=hash
            project_dir_name = index_path.parent.parent.name
            scope_id = None if project_dir_name == "global" else project_dir_name

            for entry in expired_entries:
                try:
                    # Prefer the entry's own scope_id (parsed from the
                    # nested wiki path) so per-session and per-agent
                    # files resolve correctly. Fall back to the
                    # container's scope_id otherwise.
                    effective_scope_id = entry.get("scope_id") or scope_id
                    # ``forget()`` is declared async but its body is
                    # sync FS work (unlink + flock + index rewrite).
                    # Offload to a thread so the event loop stays
                    # responsive when many entries expire.
                    await asyncio.to_thread(
                        _forget_sync,
                        memory_service,
                        entry["key"],
                        entry["scope"],
                        effective_scope_id,
                    )
                    expired_count += 1
                    logger.info(
                        f"Expired memory: key={entry['key']} scope={entry['scope']} "
                        f"type={entry['memory_type']}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to expire memory key={entry['key']}: {e}")

        if expired_count > 0:
            logger.info(f"Memory cleanup: expired {expired_count} memories")
        else:
            logger.debug("Memory cleanup: no expired memories found")

    except Exception as e:
        logger.error(f"Error during memory cleanup: {e}")


def _forget_sync(memory_service, key: str, scope: str, scope_id: str | None) -> None:
    """Run MemoryService.forget() synchronously in a worker thread.

    forget() is declared async but its body is sync; we invoke it
    via asyncio.run inside the worker thread so the outer event
    loop is not blocked by the unlink + flock + index rewrite.
    """
    import asyncio as _asyncio

    _asyncio.run(memory_service.forget(key=key, scope=scope, scope_id=scope_id))


def _find_expired_entries(index_path: Path, now: datetime) -> list[dict]:
    """Parse an index.md and return entries that have exceeded their retention."""
    expired: list[dict] = []

    try:
        content = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return expired

    current_scope: str | None = None

    for line in content.splitlines():
        # Detect scope section headers: ## global, ## session, etc.
        if line.startswith("## "):
            section = line[3:].strip()
            if section in ("global", "project", "session", "agent"):
                current_scope = section
            continue

        if not current_scope:
            continue

        # Parse entry: - [key](scope/key.md) — type:X tags:Y ~Ntok updated:Z
        match = re.match(
            r"^- \[([^\]]+)\]\(([^)]+)\) — type:(\S+) tags:\S* ~\d+tok updated:(\S+)$",
            line,
        )
        if not match:
            continue

        key = match.group(1)
        relative_path = match.group(2)
        memory_type = match.group(3)
        updated_str = match.group(4)

        # Extract scope_id from nested path for session/agent scopes:
        #   session/<scope_id>/<key>.md  →  scope_id
        # Flat paths (project, global) leave entry_scope_id = None.
        entry_scope_id: str | None = None
        path_parts = relative_path.split("/")
        if len(path_parts) >= 3 and path_parts[0] in ("session", "agent"):
            entry_scope_id = path_parts[1]

        # Parse updated_at timestamp
        try:
            updated_at = datetime.strptime(updated_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

        age_days = (now - updated_at).days

        # ``user`` and ``feedback`` memory_types are curated knowledge;
        # they never expire regardless of scope.
        if memory_type in PERMANENT_MEMORY_TYPES:
            continue

        retention_days = SCOPE_RETENTION_DAYS.get(current_scope)
        if retention_days is not None and age_days > retention_days:
            expired.append(
                {
                    "key": key,
                    "scope": current_scope,
                    "scope_id": entry_scope_id,
                    "memory_type": memory_type,
                }
            )

    return expired
