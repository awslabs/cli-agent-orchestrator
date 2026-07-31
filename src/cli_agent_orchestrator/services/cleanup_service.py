"""Cleanup service for old terminals, messages, and logs."""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_agent_orchestrator.clients.database import (
    CallbackRecoveryModel,
    InboxModel,
    SessionLocal,
    TerminalModel,
)
from cli_agent_orchestrator.constants import (
    LOG_DIR,
    MEMORY_BASE_DIR,
    RETENTION_DAYS,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.memory_format import parse_index_entry
from cli_agent_orchestrator.services.status_monitor import status_monitor

logger = logging.getLogger(__name__)


def _callback_recovery_table_absent(exc: Exception) -> bool:
    return "no such table: callback_recovery_operations" in str(exc).lower()


def _terminal_has_open_callback_recovery(db, terminal_id, generation) -> bool:
    query = db.query(CallbackRecoveryModel).filter(
        CallbackRecoveryModel.source_terminal_id == terminal_id,
        CallbackRecoveryModel.state.in_(("intent", "pending", "recovery-submitted", "ambiguous")),
    )
    if generation is not None:
        query = query.filter(CallbackRecoveryModel.source_generation == generation)
    try:
        return query.first() is not None
    except Exception as exc:  # old databases legitimately predate this table
        if _callback_recovery_table_absent(exc):
            return False
        raise


def _held_callback_recovery_inbox_ids(db) -> set[int]:
    try:
        return {
            row[0]
            for row in db.query(CallbackRecoveryModel.inbox_message_id)
            .filter(
                CallbackRecoveryModel.state.in_(
                    ("intent", "pending", "recovery-submitted", "ambiguous")
                )
                & CallbackRecoveryModel.inbox_message_id.is_not(None)
            )
            .all()
        }
    except Exception as exc:  # old databases legitimately predate this table
        if _callback_recovery_table_absent(exc):
            return set()
        raise


def _v2_terminal_identities(db) -> tuple[set, set]:
    """The terminal ids/generations owned by the isolated v2 surface.

    Old cleanup paths must have zero visibility into v2 state; if the v2
    tables are unreadable the shape guard below still applies, so the
    failure mode is preservation, never deletion.
    """
    from cli_agent_orchestrator.clients.database import (
        ManagedLaunchV2ReservationModel,
        ManagedLaunchV2TerminalModel,
    )

    ids: set = set()
    generations: set = set()
    try:
        for row in db.query(ManagedLaunchV2TerminalModel.id).all():
            ids.add(row[0])
        for row in db.query(ManagedLaunchV2TerminalModel.generation).all():
            generations.add(row[0])
        for row in db.query(ManagedLaunchV2ReservationModel.terminal_id).all():
            ids.add(row[0])
        for row in db.query(ManagedLaunchV2ReservationModel.generation).all():
            generations.add(row[0])
    except Exception:  # noqa: BLE001 - shape guard still applies; never delete on doubt
        logger.warning("v2 vintage surface unreadable during cleanup; relying on shape guard")
    return ids, generations


def _is_v2_owned_row(terminal, v2_ids: set, v2_generations: set) -> bool:
    """Whether a shared-table row belongs to a managed/v2 incarnation.

    Managed windows embed the generation in their name (``managed-*``);
    their lifecycle is owned by the generation-claimed delete path, never
    by unfiltered retention cleanup.  Rows whose id or generation appears
    in the v2 surface are likewise invisible to this old path.
    """
    if terminal.id in v2_ids:
        return True
    if terminal.generation and terminal.generation in v2_generations:
        return True
    return bool((terminal.tmux_window or "").startswith("managed-"))


_REGISTRY_LOOKUP_UNKNOWN = "registry-lookup-unknown"


def _registry_or_unknown():
    """The on-disk resource registry, or a fail-closed sentinel.

    Registry-first retention: a missing registry DB owns nothing (None);
    a present but unreadable one can neither confirm nor deny ownership,
    so the caller must preserve (sentinel). The registry is opened
    read-intent from its canonical path — the singleton is NOT used, so
    cleanup never creates the registry as a side effect.
    """
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    db_path = CAO_HOME_DIR / "resource-registry.sqlite"
    if not db_path.exists():
        return None
    try:
        from cli_agent_orchestrator.services import resource_registry as rr

        return rr.ResourceRegistry(db_path)
    except Exception:  # noqa: BLE001 - unknown ownership fails closed
        logger.warning("resource registry unreadable during cleanup", exc_info=True)
        return _REGISTRY_LOOKUP_UNKNOWN


def _terminal_id_from_log_name(name: str) -> str | None:
    """The terminal id embedded in a terminal-log file name, if any."""
    for suffix in (".snapshot.json", ".scrollback", ".log"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return stem or None
    return None


def _legacy_file_delete_blocked(log_file: Path, v2_ids: set, registry) -> bool:
    """Whether legacy retention must preserve this file.

    Registry-first: a live registry entry owning the path blocks deletion
    (the registry is the ownership authority for every v2 identity). An
    unreadable registry fails closed. The v2 name-shape guard then blocks
    any file naming a v2-owned terminal id, so v2 resources stay
    invisible to legacy cleanup even before registration.
    """
    if registry is _REGISTRY_LOOKUP_UNKNOWN:
        return True
    if registry is not None:
        try:
            if registry.resolve_fs_path(log_file) is not None:
                return True
        except Exception:  # noqa: BLE001 - unknown ownership fails closed
            logger.warning(
                "registry ownership lookup failed for %s; preserving", log_file, exc_info=True
            )
            return True
    terminal_id = _terminal_id_from_log_name(log_file.name)
    return terminal_id is not None and terminal_id in v2_ids


def cleanup_old_data():
    """Clean up terminals, inbox messages, and log files older than RETENTION_DAYS."""
    try:
        cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)
        logger.info(
            f"Starting cleanup of data older than {RETENTION_DAYS} days (before {cutoff_date})"
        )

        # Clean up old terminals (stop FIFO readers and clear state first).
        # v2-owned/managed rows are invisible to this legacy path: they are
        # skipped, never deleted (zero old-binary visibility into v2 state).
        # Close the aged-row snapshot before any lifecycle claim.  Retention
        # must never retain a SQLite/ORM transaction while waiting for a
        # recovery/session teardown lock.
        with SessionLocal() as db:
            v2_ids, v2_generations = _v2_terminal_identities(db)
            old_terminals = [
                {
                    "id": terminal.id,
                    "generation": terminal.generation,
                    "callback_target_generation": terminal.callback_target_generation,
                    "pane_id": terminal.pane_id,
                    "last_active": terminal.last_active,
                }
                for terminal in db.query(TerminalModel)
                .filter(TerminalModel.last_active < cutoff_date)
                .all()
            ]
        deleted_terminals = 0
        from cli_agent_orchestrator.services import callback_recovery

        for snapshot in old_terminals:
            with callback_recovery.generation_lifecycle_claims(
                callback_recovery.terminal_lifecycle_claim_set(snapshot)
            ):
                with SessionLocal() as db:
                    # Re-open and revalidate after the claim.  A newer terminal
                    # incarnation, activity update, or callback operation wins.
                    terminal = db.get(TerminalModel, snapshot["id"])
                    if (
                        terminal is None
                        or terminal.last_active >= cutoff_date
                        or callback_recovery.terminal_lifecycle_claim_set(terminal)
                        != callback_recovery.terminal_lifecycle_claim_set(snapshot)
                    ):
                        continue
                    if callback_recovery.terminal_has_open_recovery(
                        terminal.id, terminal.generation
                    ):
                        logger.warning(
                            "cleanup skipped terminal %s; callback recovery is open",
                            terminal.id,
                        )
                        continue
                    if _is_v2_owned_row(terminal, v2_ids, v2_generations):
                        logger.warning(
                            f"cleanup skipped managed/v2-owned terminal row {terminal.id}; "
                            "its lifecycle is generation-claimed"
                        )
                        continue
                    fifo_manager.stop_reader(terminal.id)
                    status_monitor.clear_terminal(terminal.id)
                    db.delete(terminal)
                    db.commit()
                    deleted_terminals += 1
        # Preserve the legacy cleanup transaction boundary even when no aged
        # terminal survived revalidation; inbox retention remains a separate
        # durable boundary below.
        with SessionLocal() as db:
            db.commit()
        logger.info(f"Deleted {deleted_terminals} old terminals from database")

        # Clean up old inbox messages
        try:
            held_ids = callback_recovery.held_inbox_ids()
        except Exception:
            logger.warning(
                "callback recovery lifecycle unreadable during retention; "
                "preserving all inbox rows",
                exc_info=True,
            )
            with SessionLocal() as db:
                held_ids = {row[0] for row in db.query(InboxModel.id).all()}
        with SessionLocal() as db:
            query = db.query(InboxModel).filter(InboxModel.created_at < cutoff_date)
            if held_ids:
                query = query.filter(InboxModel.id.not_in(held_ids))
            deleted_messages = query.delete(synchronize_session=False)
            db.commit()
            logger.info(f"Deleted {deleted_messages} old inbox messages from database")

        # Clean up old terminal log files — registry-first: any file whose
        # identity is registry-owned (or unverifiable, or v2-name-shaped)
        # is preserved; only unowned aged files are unlinked.
        registry = _registry_or_unknown()
        terminal_logs_deleted = 0
        if TERMINAL_LOG_DIR.exists():
            for pattern in ("*.log", "*.scrollback", "*.snapshot.json"):
                for log_file in TERMINAL_LOG_DIR.glob(pattern):
                    if log_file.stat().st_mtime < cutoff_date.timestamp():
                        if _legacy_file_delete_blocked(log_file, v2_ids, registry):
                            logger.warning(
                                f"cleanup skipped registry/v2-owned file {log_file}; "
                                "its lifecycle is generation-claimed"
                            )
                            continue
                        log_file.unlink()
                        terminal_logs_deleted += 1
        logger.info(f"Deleted {terminal_logs_deleted} old terminal log files")

        # Clean up old server log files (same registry-first ownership rule)
        server_logs_deleted = 0
        if LOG_DIR.exists():
            for log_file in LOG_DIR.glob("*.log"):
                if log_file.stat().st_mtime < cutoff_date.timestamp():
                    if _legacy_file_delete_blocked(log_file, v2_ids, registry):
                        logger.warning(
                            f"cleanup skipped registry/v2-owned file {log_file}; "
                            "its lifecycle is generation-claimed"
                        )
                        continue
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
    "federated": None,
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
            # "global"/"federated" dirs → scope_id=None (flat, machine-wide),
            # project hash dirs → scope_id=hash
            project_dir_name = index_path.parent.parent.name
            scope_id = None if project_dir_name in ("global", "federated") else project_dir_name

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
            if section in ("global", "project", "session", "agent", "federated"):
                current_scope = section
            continue

        if not current_scope:
            continue

        # Parse entry: - [key](scope/key.md) — type:X tags:Y ~Ntok updated:Z
        match = parse_index_entry(line)
        if not match:
            continue

        key = match.group("key")
        relative_path = match.group("path")
        memory_type = match.group("type")
        updated_str = match.group("updated")

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
