"""Database client for CAO — terminal metadata, inbox, flows, and memory metadata."""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, declarative_base, sessionmaker

from cli_agent_orchestrator.constants import DATABASE_URL, DB_DIR, DEFAULT_PROVIDER
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus

logger = logging.getLogger(__name__)

Base: Any = declarative_base()


class TerminalModel(Base):
    """SQLAlchemy model for terminal metadata only."""

    __tablename__ = "terminals"

    id = Column(String, primary_key=True)  # "abc123ef"
    tmux_session = Column(String, nullable=False)  # "cao-session-name"
    tmux_window = Column(String, nullable=False)  # "window-name"
    provider = Column(String, nullable=False)  # "q_cli", "claude_code"
    agent_profile = Column(String)  # "developer", "reviewer" (optional)
    allowed_tools = Column(String, nullable=True)  # JSON-encoded list of CAO tool names
    last_active = Column(DateTime, default=datetime.now)


class InboxModel(Base):
    """SQLAlchemy model for inbox messages."""

    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sender_id = Column(String, nullable=False)
    receiver_id = Column(String, nullable=False)
    message = Column(String, nullable=False)
    status = Column(String, nullable=False)  # MessageStatus enum value
    created_at = Column(DateTime, default=datetime.now)


class FlowModel(Base):
    """SQLAlchemy model for flow metadata."""

    __tablename__ = "flows"

    name = Column(String, primary_key=True)
    file_path = Column(String, nullable=False)
    schedule = Column(String, nullable=False)
    agent_profile = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    script = Column(String, nullable=True)
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    enabled = Column(Boolean, default=True)


class MemoryMetadataModel(Base):
    """SQLAlchemy model for memory metadata (Phase 2)."""

    __tablename__ = "memory_metadata"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String, nullable=False)
    memory_type = Column(String, nullable=False)
    scope = Column(String, nullable=False)
    scope_id = Column(String, nullable=True)
    file_path = Column(String, nullable=False)
    tags = Column(String, nullable=False, default="")
    source_provider = Column(String, nullable=True)
    source_terminal_id = Column(String, nullable=True)
    token_estimate = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("key", "scope", "scope_id", name="uq_memory_key_scope"),)


class SessionEventModel(Base):
    """Append-only event log for session lifecycle tracking (Phase 2)."""

    __tablename__ = "session_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_name = Column(String, nullable=False, index=True)
    terminal_id = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    summary = Column(String, nullable=False, default="")
    metadata_json = Column(String, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)


class ProjectAliasModel(Base):
    """SQLAlchemy model for project identity aliases (Phase 2.5 U6).

    Maps historical/alternate project identifiers to a canonical project_id so
    memory recall survives directory rename and worktree layouts.
    """

    __tablename__ = "project_aliases"

    project_id = Column(String, primary_key=True)
    alias = Column(String, primary_key=True)
    kind = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# Module-level singletons
DB_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Enable WAL mode for concurrent write safety


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_db() -> None:
    """Initialize database tables and apply schema migrations."""
    Base.metadata.create_all(bind=engine)
    _migrate_add_allowed_tools()
    _migrate_add_memory_indexes()


def _migrate_add_allowed_tools() -> None:
    """Add allowed_tools column to terminals table if missing (schema migration)."""
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        conn = sqlite3.connect(str(DATABASE_FILE))
        cursor = conn.execute("PRAGMA table_info(terminals)")
        columns = {row[1] for row in cursor.fetchall()}
        if "allowed_tools" not in columns:
            conn.execute("ALTER TABLE terminals ADD COLUMN allowed_tools TEXT")
            conn.commit()
            logger.info("Migration: added allowed_tools column to terminals table")
        conn.close()
    except Exception as e:
        logger.warning(f"Migration check for allowed_tools failed: {e}")


def _migrate_add_memory_indexes() -> None:
    """Add explicit indexes on memory_metadata table for query performance."""
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        conn = sqlite3.connect(str(DATABASE_FILE))
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_metadata (scope, scope_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_metadata (updated_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_metadata (memory_type)")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"Memory index migration (may be first run): {e}")


def create_terminal(
    terminal_id: str,
    tmux_session: str,
    tmux_window: str,
    provider: str,
    agent_profile: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create terminal metadata record."""
    import json as _json

    with SessionLocal() as db:
        terminal = TerminalModel(
            id=terminal_id,
            tmux_session=tmux_session,
            tmux_window=tmux_window,
            provider=provider,
            agent_profile=agent_profile,
            allowed_tools=_json.dumps(allowed_tools) if allowed_tools else None,
        )
        db.add(terminal)
        db.commit()
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "allowed_tools": allowed_tools,
        }


def get_terminal_metadata(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Get terminal metadata by ID."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            logger.warning(f"Terminal metadata not found for terminal_id: {terminal_id}")
            return None
        logger.debug(
            f"Retrieved terminal metadata for {terminal_id}: provider={terminal.provider}, session={terminal.tmux_session}"
        )
        allowed_tools = _json.loads(terminal.allowed_tools) if terminal.allowed_tools else None
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "allowed_tools": allowed_tools,
            "last_active": terminal.last_active,
        }


def list_terminals_by_session(tmux_session: str) -> List[Dict[str, Any]]:
    """List all terminals in a tmux session."""
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).all()
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "last_active": t.last_active,
            }
            for t in terminals
        ]


def update_last_active(terminal_id: str) -> bool:
    """Update last active timestamp."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.last_active = datetime.now()
            db.commit()
            return True
        return False


def list_all_terminals() -> List[Dict[str, Any]]:
    """List all terminals."""
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).all()
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "last_active": t.last_active,
            }
            for t in terminals
        ]


def delete_terminal(terminal_id: str) -> bool:
    """Delete terminal metadata."""
    with SessionLocal() as db:
        deleted = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).delete()
        db.commit()
        return deleted > 0


def delete_terminals_by_session(tmux_session: str) -> int:
    """Delete all terminals in a session."""
    with SessionLocal() as db:
        deleted = (
            db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).delete()
        )
        db.commit()
        return deleted


def create_inbox_message(sender_id: str, receiver_id: str, message: str) -> InboxMessage:
    """Create inbox message with status=MessageStatus.PENDING."""
    with SessionLocal() as db:
        inbox_msg = InboxModel(
            sender_id=sender_id,
            receiver_id=receiver_id,
            message=message,
            status=MessageStatus.PENDING.value,
        )
        db.add(inbox_msg)
        db.commit()
        db.refresh(inbox_msg)
        return InboxMessage(
            id=inbox_msg.id,
            sender_id=inbox_msg.sender_id,
            receiver_id=inbox_msg.receiver_id,
            message=inbox_msg.message,
            status=MessageStatus(inbox_msg.status),
            created_at=inbox_msg.created_at,
        )


def get_pending_messages(receiver_id: str, limit: int = 1) -> List[InboxMessage]:
    """Get pending messages ordered by created_at ASC (oldest first)."""
    return get_inbox_messages(receiver_id, limit=limit, status=MessageStatus.PENDING)


def get_inbox_messages(
    receiver_id: str, limit: int = 10, status: Optional[MessageStatus] = None
) -> List[InboxMessage]:
    """Get inbox messages with optional status filter ordered by created_at ASC (oldest first).

    Args:
        receiver_id: Terminal ID to get messages for
        limit: Maximum number of messages to return (default: 10)
        status: Optional filter by message status (None = all statuses)

    Returns:
        List of inbox messages ordered by creation time (oldest first)
    """
    with SessionLocal() as db:
        query = db.query(InboxModel).filter(InboxModel.receiver_id == receiver_id)

        if status is not None:
            query = query.filter(InboxModel.status == status.value)

        messages = query.order_by(InboxModel.created_at.asc()).limit(limit).all()

        return [
            InboxMessage(
                id=msg.id,
                sender_id=msg.sender_id,
                receiver_id=msg.receiver_id,
                message=msg.message,
                status=MessageStatus(msg.status),
                created_at=msg.created_at,
            )
            for msg in messages
        ]


def update_message_status(message_id: int, status: MessageStatus) -> bool:
    """Update message status to MessageStatus.DELIVERED or MessageStatus.FAILED."""
    with SessionLocal() as db:
        message = db.query(InboxModel).filter(InboxModel.id == message_id).first()
        if message:
            message.status = status.value
            db.commit()
            return True
        return False


# Flow database functions


def create_flow(
    name: str,
    file_path: str,
    schedule: str,
    agent_profile: str,
    provider: str,
    script: str,
    next_run: datetime,
) -> Flow:
    """Create flow record."""
    with SessionLocal() as db:
        flow = FlowModel(
            name=name,
            file_path=file_path,
            schedule=schedule,
            agent_profile=agent_profile,
            provider=provider,
            script=script,
            next_run=next_run,
        )
        db.add(flow)
        db.commit()
        db.refresh(flow)
        return Flow(
            name=flow.name,
            file_path=flow.file_path,
            schedule=flow.schedule,
            agent_profile=flow.agent_profile,
            provider=flow.provider,
            script=flow.script,
            last_run=flow.last_run,
            next_run=flow.next_run,
            enabled=flow.enabled,
        )


def get_flow(name: str) -> Optional[Flow]:
    """Get flow by name."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if not flow:
            return None
        return Flow(
            name=flow.name,
            file_path=flow.file_path,
            schedule=flow.schedule,
            agent_profile=flow.agent_profile,
            provider=flow.provider,
            script=flow.script,
            last_run=flow.last_run,
            next_run=flow.next_run,
            enabled=flow.enabled,
        )


def list_flows() -> List[Flow]:
    """List all flows."""
    with SessionLocal() as db:
        flows = db.query(FlowModel).order_by(FlowModel.next_run).all()
        return [
            Flow(
                name=f.name,
                file_path=f.file_path,
                schedule=f.schedule,
                agent_profile=f.agent_profile,
                provider=f.provider,
                script=f.script,
                last_run=f.last_run,
                next_run=f.next_run,
                enabled=f.enabled,
            )
            for f in flows
        ]


def update_flow_run_times(name: str, last_run: datetime, next_run: datetime) -> bool:
    """Update flow run times after execution."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if flow:
            flow.last_run = last_run
            flow.next_run = next_run
            db.commit()
            return True
        return False


def update_flow_enabled(name: str, enabled: bool, next_run: Optional[datetime] = None) -> bool:
    """Update flow enabled status and optionally next_run."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if flow:
            flow.enabled = enabled
            if next_run is not None:
                flow.next_run = next_run
            db.commit()
            return True
        return False


def delete_flow(name: str) -> bool:
    """Delete flow."""
    with SessionLocal() as db:
        deleted = db.query(FlowModel).filter(FlowModel.name == name).delete()
        db.commit()
        return deleted > 0


def get_flows_to_run() -> List[Flow]:
    """Get enabled flows where next_run <= now."""
    with SessionLocal() as db:
        now = datetime.now()
        flows = (
            db.query(FlowModel).filter(FlowModel.enabled == True, FlowModel.next_run <= now).all()
        )
        return [
            Flow(
                name=f.name,
                file_path=f.file_path,
                schedule=f.schedule,
                agent_profile=f.agent_profile,
                provider=f.provider,
                script=f.script,
                last_run=f.last_run,
                next_run=f.next_run,
                enabled=f.enabled,
            )
            for f in flows
        ]


# =============================================================================
# Memory metadata database functions
# =============================================================================


def upsert_memory_metadata(
    key: str,
    memory_type: str,
    scope: str,
    scope_id: Optional[str],
    file_path: str,
    tags: str = "",
    source_provider: Optional[str] = None,
    source_terminal_id: Optional[str] = None,
    token_estimate: Optional[int] = None,
) -> MemoryMetadataModel:
    """Insert or update a memory_metadata row. Returns the model instance."""
    with SessionLocal() as db:
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
            existing.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(existing)
            return existing
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
            db.refresh(row)
            return row


def query_memory_metadata(
    query: Optional[str] = None,
    scope: Optional[str] = None,
    scope_id: Optional[str] = None,
    memory_type: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Query memory_metadata rows with optional filters. Returns list of dicts."""
    with SessionLocal() as db:
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


def delete_memory_metadata(
    key: str,
    scope: str,
    scope_id: Optional[str] = None,
) -> bool:
    """Delete a memory_metadata row. Returns True if a row was deleted."""
    with SessionLocal() as db:
        q = db.query(MemoryMetadataModel).filter(
            MemoryMetadataModel.key == key,
            MemoryMetadataModel.scope == scope,
        )
        if scope_id is not None:
            q = q.filter(MemoryMetadataModel.scope_id == scope_id)
        else:
            q = q.filter(MemoryMetadataModel.scope_id.is_(None))
        deleted = q.delete()
        db.commit()
        return deleted > 0


def get_expired_memory_metadata(
    session_retention_days: int = 14,
    project_retention_days: int = 90,
) -> List[Dict[str, Any]]:
    """Get memory_metadata rows that have exceeded their retention period.

    Raises on failure so callers can fall back to file-based cleanup.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    results: List[Dict[str, Any]] = []
    conn = sqlite3.connect(str(DATABASE_FILE))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT id, key, memory_type, scope, scope_id, file_path
        FROM memory_metadata
        WHERE (scope = 'session' AND updated_at < datetime('now', ?))
           OR (memory_type IN ('project', 'reference') AND updated_at < datetime('now', ?))
        """,
        (f"-{session_retention_days} days", f"-{project_retention_days} days"),
    )
    for row in cursor.fetchall():
        results.append(dict(row))
    conn.close()
    return results


# =============================================================================
# Session event database functions
# =============================================================================


def log_session_event(
    session_name: str,
    terminal_id: str,
    provider: str,
    event_type: str,
    summary: str = "",
    metadata_json: str = "{}",
) -> None:
    """Insert an append-only session event row. Non-blocking — failures are logged, not raised."""
    try:
        with SessionLocal() as db:
            row = SessionEventModel(
                id=str(uuid.uuid4()),
                session_name=session_name,
                terminal_id=terminal_id,
                provider=provider,
                event_type=event_type,
                summary=summary,
                metadata_json=metadata_json,
            )
            db.add(row)
            db.commit()
    except Exception as e:
        logger.warning(f"Failed to log session event ({event_type}): {e}")


def get_session_timeline(session_name: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Get ordered event timeline for a session."""
    with SessionLocal() as db:
        rows = (
            db.query(SessionEventModel)
            .filter(SessionEventModel.session_name == session_name)
            .order_by(SessionEventModel.created_at.asc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "session_name": r.session_name,
                "terminal_id": r.terminal_id,
                "provider": r.provider,
                "event_type": r.event_type,
                "summary": r.summary,
                "metadata_json": r.metadata_json,
                "created_at": r.created_at,
            }
            for r in rows
        ]


def get_all_memory_metadata_for_scope(
    scope: str,
    scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get all memory_metadata rows for a given scope (for index regeneration)."""
    with SessionLocal() as db:
        q = db.query(MemoryMetadataModel).filter(MemoryMetadataModel.scope == scope)
        if scope_id is not None:
            q = q.filter(MemoryMetadataModel.scope_id == scope_id)
        else:
            q = q.filter(MemoryMetadataModel.scope_id.is_(None))
        rows = q.order_by(MemoryMetadataModel.updated_at.desc()).all()
        return [
            {
                "id": r.id,
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


# =============================================================================
# Project alias database functions (Phase 2.5 U6)
# =============================================================================


def record_project_alias(project_id: str, alias: str, kind: str) -> bool:
    """Record (or no-op upsert) a project_id ↔ alias mapping.

    Returns True when a new row was inserted; False when the alias already
    existed under that project_id. Alias-to-different-project collisions are
    not rewritten here — callers must decide the policy.
    """
    with SessionLocal() as db:
        existing = (
            db.query(ProjectAliasModel)
            .filter(
                ProjectAliasModel.project_id == project_id,
                ProjectAliasModel.alias == alias,
            )
            .first()
        )
        if existing:
            return False
        row = ProjectAliasModel(project_id=project_id, alias=alias, kind=kind)
        db.add(row)
        db.commit()
        return True


def get_project_id_by_alias(alias: str) -> Optional[str]:
    """Return the canonical project_id for an alias, or None if not recorded."""
    with SessionLocal() as db:
        row = db.query(ProjectAliasModel).filter(ProjectAliasModel.alias == alias).first()
        return str(row.project_id) if row else None


def list_aliases_for_project(project_id: str) -> List[Dict[str, Any]]:
    """List every alias recorded for a canonical project_id."""
    with SessionLocal() as db:
        rows = (
            db.query(ProjectAliasModel)
            .filter(ProjectAliasModel.project_id == project_id)
            .order_by(ProjectAliasModel.created_at.asc())
            .all()
        )
        return [
            {
                "project_id": r.project_id,
                "alias": r.alias,
                "kind": r.kind,
                "created_at": r.created_at,
            }
            for r in rows
        ]
