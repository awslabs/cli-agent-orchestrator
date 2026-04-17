"""Cleanup service for old terminals, messages, and logs."""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_agent_orchestrator.clients.database import InboxModel, SessionLocal, TerminalModel
from cli_agent_orchestrator.constants import CAO_HOME_DIR, LOG_DIR, MEMORY_BASE_DIR, RETENTION_DAYS, TERMINAL_LOG_DIR

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
            for log_file in TERMINAL_LOG_DIR.glob("*.log"):
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

# Retention policy: days until expiry (None = indefinite)
RETENTION_POLICY: dict[str, int | None] = {
    "user": None,
    "feedback": None,
    "project": 90,
    "reference": 90,
}
SESSION_SCOPE_RETENTION_DAYS = 14


async def cleanup_expired_memories() -> None:
    """Delete expired memories based on tiered retention policy.

    - session-scoped memories (any type): 14 days
    - project/reference type memories: 90 days
    - user/feedback type memories: indefinite (never expire)

    Idempotent — safe to run multiple times.
    """
    try:
        now = datetime.now(timezone.utc)
        expired_count = 0

        if not MEMORY_BASE_DIR.exists():
            return

        # Lazy-import to avoid circular imports at module level
        from cli_agent_orchestrator.services.memory_service import MemoryService

        memory_service = MemoryService(base_dir=MEMORY_BASE_DIR)

        # Walk project dirs: {MEMORY_BASE_DIR}/{project_dir}/wiki/index.md
        for index_path in MEMORY_BASE_DIR.glob("*/wiki/index.md"):
            expired_entries = _find_expired_entries(index_path, now)
            if not expired_entries:
                continue

            # Extract scope_id from path: .../memory/{scope_id}/wiki/index.md
            # "global" dir → scope_id=None, project hash dirs → scope_id=hash
            project_dir_name = index_path.parent.parent.name
            scope_id = None if project_dir_name == "global" else project_dir_name

            for entry in expired_entries:
                try:
                    await memory_service.forget(
                        key=entry["key"],
                        scope=entry["scope"],
                        scope_id=scope_id,
                    )
                    expired_count += 1
                    logger.info(
                        f"Expired memory: key={entry['key']} scope={entry['scope']} "
                        f"type={entry['memory_type']}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to expire memory key={entry['key']}: {e}"
                    )

        if expired_count > 0:
            logger.info(f"Memory cleanup: expired {expired_count} memories")
        else:
            logger.debug("Memory cleanup: no expired memories found")

        # Clean up stale stop-hook flag files (crash recovery)
        _cleanup_stale_hook_flags(now)

    except Exception as e:
        logger.error(f"Error during memory cleanup: {e}")


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
        memory_type = match.group(3)
        updated_str = match.group(4)

        # Parse updated_at timestamp
        try:
            updated_at = datetime.strptime(updated_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

        age_days = (now - updated_at).days

        # Session-scoped: 14-day retention regardless of type
        if current_scope == "session":
            if age_days > SESSION_SCOPE_RETENTION_DAYS:
                expired.append({
                    "key": key,
                    "scope": current_scope,
                    "memory_type": memory_type,
                })
            continue

        # Type-based retention
        retention_days = RETENTION_POLICY.get(memory_type)
        if retention_days is not None and age_days > retention_days:
            expired.append({
                "key": key,
                "scope": current_scope,
                "memory_type": memory_type,
            })

    return expired


# =============================================================================
# Stale hook flag file cleanup
# =============================================================================

HOOK_FLAG_STALE_MINUTES = 5


def _cleanup_stale_hook_flags(now: datetime) -> None:
    """Remove stale stop-hook flag files left behind by crashed agents.

    Flag files are at ~/.aws/cli-agent-orchestrator/hooks/.cao_stop_hook_active_*
    They should be short-lived; anything older than 5 minutes is stale.
    """
    hooks_dir = CAO_HOME_DIR / "hooks"
    if not hooks_dir.exists():
        return

    cutoff = now - timedelta(minutes=HOOK_FLAG_STALE_MINUTES)
    stale_count = 0

    for flag_file in hooks_dir.glob(".cao_stop_hook_active_*"):
        try:
            mtime = datetime.fromtimestamp(flag_file.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                flag_file.unlink()
                stale_count += 1
                logger.debug(f"Removed stale hook flag: {flag_file.name}")
        except OSError:
            pass  # File already removed — idempotent

    if stale_count > 0:
        logger.info(f"Removed {stale_count} stale hook flag file(s)")
